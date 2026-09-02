"""ARM ELF inspection and disassembly tools."""

from __future__ import annotations

import asyncio
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .toolchain import find_objdump, find_readelf

_executor = ThreadPoolExecutor(max_workers=2)
ELF_TIMEOUT = 30

READELF_MODES = {
    "headers": ["-h"],
    "sections": ["-S"],
    "symbols": ["-s"],
    "segments": ["-l"],
    "dynamic": ["-d"],
    "notes": ["-n"],
    "debug_info": ["--debug-dump=info"],
    "debug_line": ["--debug-dump=decodedline"],
    "all": ["-a"],
}


def _output_text(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def as_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value

    out = as_text(stdout)
    err = as_text(stderr)
    if out and err:
        return f"{out}\n{err}"
    return out or err


def _format_result(tool: str, returncode: int, output: str) -> str:
    text = output.strip()
    if returncode != 0:
        if text:
            return f"ERROR: {tool} exited with code {returncode}.\n{text}"
        return f"ERROR: {tool} exited with code {returncode}."
    return text or f"{tool} completed successfully."


def _clean_arg(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"Invalid {name}.")
    if value != value.strip():
        raise ValueError(f"Invalid {name}.")
    return value


def _clean_extra_args(extra_args: list[str] | None) -> list[str]:
    if not extra_args:
        return []
    return [_clean_arg(arg, "extra argument") for arg in extra_args]


def _validate_elf(elf_path: str) -> str:
    path = _clean_arg(elf_path, "ELF path")
    if not os.path.isfile(path):
        raise ValueError(f"ELF file not found: {path}")
    return path


def _run_tool(tool: str, executable: str | None, arguments: list[str], elf_path: str, timeout: int) -> str:
    if not executable:
        return (
            f"ERROR: {tool} not found. Set STM32_ARM_TOOLCHAIN_BIN to the "
            "CubeIDE tools/bin directory or add the ARM toolchain to PATH."
        )
    try:
        result = subprocess.run(
            [executable, *arguments, elf_path],
            capture_output=True,
            shell=False,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = _output_text(exc.stdout, exc.stderr).strip()
        suffix = f"\n{output}" if output else ""
        return f"ERROR: {tool} timed out after {timeout}s.{suffix}"
    except OSError as exc:
        return f"ERROR: Could not run {tool}: {exc}"
    return _format_result(tool, result.returncode, _output_text(result.stdout, result.stderr))


def _do_readelf(
    elf_path: str,
    mode: str,
    wide: bool,
    extra_args: list[str] | None,
    timeout: int,
) -> str:
    try:
        path = _validate_elf(elf_path)
        if mode not in READELF_MODES:
            raise ValueError(
                f"Unsupported readelf mode {mode!r}. Choose one of: "
                f"{', '.join(sorted(READELF_MODES))}."
            )
        arguments = list(READELF_MODES[mode])
        if wide:
            arguments.insert(0, "-W")
        arguments.extend(_clean_extra_args(extra_args))
    except ValueError as exc:
        return f"ERROR: {exc}"

    return _run_tool("arm-none-eabi-readelf", find_readelf(), arguments, path, timeout)


async def stm32_readelf(
    elf_path: str,
    mode: str = "headers",
    wide: bool = True,
    extra_args: list[str] | None = None,
    timeout: int = ELF_TIMEOUT,
) -> str:
    """Inspect an ARM ELF with ``arm-none-eabi-readelf``.

    Modes include headers, sections, symbols, segments, dynamic, notes,
    debug_info, debug_line, and all. ``extra_args`` supports options specific
    to the installed binutils release.
    """
    bounded_timeout = max(1, timeout)
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            _do_readelf,
            elf_path,
            mode,
            wide,
            extra_args,
            bounded_timeout,
        ),
        timeout=bounded_timeout + 5,
    )


def _do_disassemble(
    elf_path: str,
    function: str,
    start_address: str,
    stop_address: str,
    section: str,
    source: bool,
    all_sections: bool,
    demangle: bool,
    raw_instructions: bool,
    line_numbers: bool,
    extra_args: list[str] | None,
    timeout: int,
) -> str:
    try:
        path = _validate_elf(elf_path)
        arguments = []
        if function:
            arguments.append(f"--disassemble={_clean_arg(function, 'function')}")
        else:
            arguments.append("-D" if all_sections else "-d")
        if source:
            arguments.append("-S")
        if demangle:
            arguments.append("-C")
        if not raw_instructions:
            arguments.append("--no-show-raw-insn")
        if line_numbers:
            arguments.append("-l")
        if section:
            arguments.extend(["-j", _clean_arg(section, "section")])
        if start_address:
            arguments.append(
                f"--start-address={_clean_arg(start_address, 'start address')}"
            )
        if stop_address:
            arguments.append(
                f"--stop-address={_clean_arg(stop_address, 'stop address')}"
            )
        arguments.extend(_clean_extra_args(extra_args))
    except ValueError as exc:
        return f"ERROR: {exc}"

    return _run_tool("arm-none-eabi-objdump", find_objdump(), arguments, path, timeout)


async def stm32_disassemble(
    elf_path: str,
    function: str = "",
    start_address: str = "",
    stop_address: str = "",
    section: str = "",
    source: bool = False,
    all_sections: bool = False,
    demangle: bool = True,
    raw_instructions: bool = True,
    line_numbers: bool = True,
    extra_args: list[str] | None = None,
    timeout: int = ELF_TIMEOUT,
) -> str:
    """Disassemble ARM firmware with ``arm-none-eabi-objdump``.

    Use ``function`` for one symbol or ``start_address``/``stop_address`` for
    an address range. By default the output includes source line mappings and
    raw instruction bytes when the ELF contains debug information.
    """
    bounded_timeout = max(1, timeout)
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            _do_disassemble,
            elf_path,
            function,
            start_address,
            stop_address,
            section,
            source,
            all_sections,
            demangle,
            raw_instructions,
            line_numbers,
            extra_args,
            bounded_timeout,
        ),
        timeout=bounded_timeout + 5,
    )
