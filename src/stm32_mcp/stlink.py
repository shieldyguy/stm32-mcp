"""Direct wrappers for the user-installed stlink command-line tools.

The existing STM32 tools use OpenOCD for SWD operations.  This module exposes
the upstream stlink tools as a separate backend so users can choose the tool
that matches their installed stlink version.  Commands are always passed as an
argument list; no shell is used.
"""

from __future__ import annotations

import atexit
import asyncio
import collections
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .board_map import resolve_probe
from .gdb_debug import stlink_gdb_command
from .toolchain import find_gdb, find_stlink_tool

_executor = ThreadPoolExecutor(max_workers=4)

STLINK_TOOLS = {"st-info", "st-flash", "st-util", "st-trace", "st-server"}
INFO_QUERIES = {
    "version",
    "probe",
    "serial",
    "flash",
    "pagesize",
    "sram",
    "chipid",
    "descr",
    "voltage",
}
FLASH_OPERATIONS = {"read", "write", "erase", "reset"}
FLASH_FORMATS = {"binary", "ihex"}
FLASH_AREAS = {
    "main",
    "system",
    "otp",
    "option",
    "option_boot_add",
    "optcr",
    "optcr1",
}

COMMAND_TIMEOUT = 60
FLASH_TIMEOUT = 300
INFO_TIMEOUT = 30
DEBUG_TIMEOUT = 120
TRACE_OUTPUT_LINES = 2000


# ---------------------------------------------------------------------------
# Common command execution
# ---------------------------------------------------------------------------


def _output_text(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    """Combine command output without losing stderr diagnostics."""
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


def _execute_stlink(
    tool: str,
    arguments: list[str],
    timeout: int,
) -> tuple[int | None, str]:
    """Run one stlink command and return ``(exit_code, output)``.

    ``None`` is used for a tool lookup, OS, or timeout failure.  The caller can
    format that failure with the operation-specific context it has available.
    """
    if tool not in STLINK_TOOLS:
        return None, f"ERROR: Unsupported stlink tool: {tool}"

    executable = find_stlink_tool(tool)
    if not executable:
        return (
            None,
            f"ERROR: {tool} not found on PATH. Install the stlink toolset "
            "separately; stm32-mcp does not install it.",
        )

    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            shell=False,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = _output_text(exc.stdout, exc.stderr).strip()
        suffix = f"\n{output}" if output else ""
        return None, f"ERROR: {tool} timed out after {timeout}s.{suffix}"
    except OSError as exc:
        return None, f"ERROR: Could not run {tool}: {exc}"

    return result.returncode, _output_text(result.stdout, result.stderr)


def _format_result(tool: str, returncode: int | None, output: str) -> str:
    """Format a one-shot command result for an MCP response."""
    if returncode is None:
        return output.strip()

    text = output.strip()
    if returncode != 0:
        if text:
            return f"ERROR: {tool} exited with code {returncode}.\n{text}"
        return f"ERROR: {tool} exited with code {returncode}."
    return text or f"{tool} completed successfully."


async def _terminate_async_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        await process.communicate()
    except (BrokenPipeError, ConnectionError):
        pass


async def _execute_stlink_async(
    tool: str,
    arguments: list[str],
    timeout: int,
) -> str:
    """Run a hardware-changing one-shot command with cancellation cleanup."""
    executable = find_stlink_tool(tool)
    if not executable:
        return (
            f"ERROR: {tool} not found on PATH. Install the stlink toolset "
            "separately; stm32-mcp does not install it."
        )

    kwargs = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            executable, *arguments, **kwargs
        )
    except OSError as exc:
        return f"ERROR: Could not run {tool}: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_async_process(process)
        return f"ERROR: {tool} timed out after {timeout}s."
    except asyncio.CancelledError:
        await _terminate_async_process(process)
        raise

    output = _output_text(stdout, stderr)
    return _format_result(tool, process.returncode, output)


async def _execute_gdb_async(arguments: list[str], timeout: int) -> str:
    kwargs = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            **kwargs,
        )
    except OSError as exc:
        return f"ERROR: Could not run arm-none-eabi-gdb: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_async_process(process)
        return f"ERROR: GDB command timed out after {timeout}s."
    except asyncio.CancelledError:
        await _terminate_async_process(process)
        raise

    return _format_result(
        "arm-none-eabi-gdb", process.returncode, _output_text(stdout, stderr)
    )


def _cli_value(value: str, name: str) -> str:
    """Validate a scalar command-line value before passing it to a tool."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty.")
    if "\x00" in value or value != value.strip():
        raise ValueError(f"Invalid {name}.")
    return value


def _resolve_serial(probe: str) -> str:
    if not probe:
        return ""
    try:
        serial = resolve_probe(probe)
    except Exception as exc:
        raise ValueError(f"Could not resolve probe {probe!r}: {exc}") from exc
    if not serial:
        raise ValueError(f"Could not resolve probe {probe!r}.")
    return _cli_value(serial, "probe serial")


def _run_async(function, *args, timeout: int | None = None):
    """Run a blocking wrapper in the module executor."""
    async def runner():
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(_executor, function, *args)
        if timeout is None:
            return await future
        return await asyncio.wait_for(future, timeout=timeout)

    return runner()


# ---------------------------------------------------------------------------
# st-info
# ---------------------------------------------------------------------------


def _build_info_args(
    query: str,
    freq: str,
    connect_under_reset: bool,
    hot_plug: bool,
    remote: str,
) -> list[str]:
    if query not in INFO_QUERIES:
        raise ValueError(
            f"Unsupported st-info query {query!r}. Choose one of: "
            f"{', '.join(sorted(INFO_QUERIES))}."
        )
    if connect_under_reset and hot_plug:
        raise ValueError("connect_under_reset and hot_plug are mutually exclusive.")

    arguments: list[str] = []
    if connect_under_reset:
        arguments.append("--connect-under-reset")
    if hot_plug:
        arguments.append("--hot-plug")
    if freq:
        arguments.extend(["--freq", _cli_value(freq, "frequency")])
    if remote:
        arguments.extend(["--remote", _cli_value(remote, "remote endpoint")])
    arguments.append(f"--{query}")
    return arguments


def _select_info_block(output: str, serial: str) -> str | None:
    """Select one block from ``st-info --probe`` output."""
    blocks = re.split(r"^\d+\.\s*$", output, flags=re.MULTILINE)
    serial_pattern = re.compile(
        rf"^\s*serial:\s*{re.escape(serial)}\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    for block in blocks:
        if serial_pattern.search(block):
            return block.strip()
    return None


def _info_value(block: str, query: str) -> str | None:
    patterns = {
        "version": r"^\s*version:\s*(\S+)",
        "serial": r"^\s*serial:\s*(\S+)",
        "flash": r"^\s*flash:\s*(\S+)",
        "pagesize": r"pagesize:\s*(\S+)",
        "sram": r"^\s*sram:\s*(\S+)",
        "chipid": r"^\s*chipid:\s*(\S+)",
        "descr": r"^\s*dev-type:\s*(.+)",
        "voltage": r"^\s*voltage:\s*(.+)",
    }
    pattern = patterns.get(query)
    if not pattern:
        return None
    match = re.search(pattern, block, flags=re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _do_stlink_info(
    query: str,
    probe: str,
    freq: str,
    connect_under_reset: bool,
    hot_plug: bool,
    remote: str,
    timeout: int,
) -> str:
    try:
        if probe and remote:
            raise ValueError("probe and remote cannot be used together.")
        serial = "" if query == "version" else _resolve_serial(probe)
        if query != "version" and not remote and _probe_busy(""):
            return _probe_busy_message("")
        # st-info has no --serial selector.  Filter its --probe listing when a
        # local named probe is requested, so multi-board setups remain useful.
        if serial and not remote and query != "version":
            arguments = _build_info_args(
                "probe", freq, connect_under_reset, hot_plug, ""
            )
            returncode, output = _execute_stlink("st-info", arguments, timeout)
            if returncode is None or returncode != 0:
                return _format_result("st-info", returncode, output)
            block = _select_info_block(output, serial)
            if block is None:
                return f"ERROR: Probe {probe!r} ({serial}) was not found in st-info output."
            if query == "probe":
                return block
            return _info_value(block, query) or block

        arguments = _build_info_args(
            query, freq, connect_under_reset, hot_plug, remote
        )
        returncode, output = _execute_stlink("st-info", arguments, timeout)
        return _format_result("st-info", returncode, output)
    except ValueError as exc:
        return f"ERROR: {exc}"


async def stlink_info(
    query: str = "probe",
    probe: str = "",
    freq: str = "",
    connect_under_reset: bool = False,
    hot_plug: bool = False,
    remote: str = "",
    timeout: int = INFO_TIMEOUT,
) -> str:
    """Run an ``st-info`` query against the installed stlink toolset.

    Args:
        query: One of version, probe, serial, flash, pagesize, sram, chipid,
            descr, or voltage.
        probe: Probe nickname, board nickname, or ST-Link serial number.  For
            local probes, st-info's multi-probe listing is filtered by serial.
        freq: SWD/JTAG frequency accepted by the installed st-info version.
        connect_under_reset: Connect while holding the target in reset.
        hot_plug: Connect without resetting the target.
        remote: st-server endpoint such as ``127.0.0.1:4500``.
        timeout: Command timeout in seconds.
    """
    return await _run_async(
        _do_stlink_info,
        query,
        probe,
        freq,
        connect_under_reset,
        hot_plug,
        remote,
        max(1, timeout),
        timeout=max(1, timeout) + 5,
    )


# ---------------------------------------------------------------------------
# st-flash
# ---------------------------------------------------------------------------


def _build_flash_args(
    operation: str,
    file_path: str,
    address: str,
    size: str,
    value: str,
    freq: str,
    flash_size: str,
    file_format: str,
    area: str,
    opt: bool,
    mass_erase: bool,
    reset: bool,
    connect_under_reset: bool,
    hot_plug: bool,
    debug: bool,
    serial: str,
    remote: str,
) -> list[str]:
    if operation not in FLASH_OPERATIONS:
        raise ValueError(
            f"Unsupported st-flash operation {operation!r}. Choose read, write, "
            "erase, or reset."
        )
    if file_format and file_format not in FLASH_FORMATS:
        raise ValueError("file_format must be 'binary' or 'ihex'.")
    if area and area not in FLASH_AREAS:
        raise ValueError(f"Unsupported st-flash area {area!r}.")
    if connect_under_reset and hot_plug:
        raise ValueError("connect_under_reset and hot_plug are mutually exclusive.")
    if serial and remote:
        raise ValueError("probe and remote cannot be used together.")
    if mass_erase and operation != "write":
        raise ValueError("mass_erase is only valid for a write operation.")

    arguments: list[str] = []
    if freq:
        arguments.extend(["--freq", _cli_value(freq, "frequency")])
    if flash_size:
        arguments.extend(["--flash", _cli_value(flash_size, "flash size")])
    if file_format:
        arguments.extend(["--format", file_format])
    if area:
        arguments.extend(["--area", area])
    if serial:
        arguments.extend(["--serial", _cli_value(serial, "probe serial")])
    if remote:
        arguments.extend(["--remote", _cli_value(remote, "remote endpoint")])
    if connect_under_reset:
        arguments.append("--connect-under-reset")
    if hot_plug:
        arguments.append("--hot-plug")
    if opt:
        arguments.append("--opt")
    if mass_erase:
        arguments.append("--mass-erase")
    if reset:
        arguments.append("--reset")
    if debug:
        arguments.append("--debug")

    arguments.append(operation)

    if operation == "reset":
        if any((file_path, address, size, value)):
            raise ValueError("reset does not accept file, address, size, or value.")
        return arguments

    if operation == "erase":
        if any((file_path, value)):
            raise ValueError("erase does not accept file_path or value.")
        if bool(address) != bool(size):
            raise ValueError("erase requires both address and size, or neither.")
        if address:
            arguments.extend([_cli_value(address, "address"), _cli_value(size, "size")])
        return arguments

    if operation == "write":
        if value:
            if area not in {"option", "option_boot_add", "optcr", "optcr1"}:
                raise ValueError(
                    "value is only valid when writing option bytes or option control registers."
                )
            if file_path or address:
                raise ValueError("Use value instead of file_path or address for this area.")
            arguments.append(_cli_value(value, "value"))
            return arguments

        if area in {"option", "option_boot_add", "optcr", "optcr1"}:
            raise ValueError(f"value is required when writing the {area} area.")

        if not file_path:
            raise ValueError("file_path is required for a flash write.")
        if not os.path.isfile(file_path):
            raise ValueError(f"Input file not found: {file_path}")
        arguments.append(_cli_value(file_path, "file_path"))
        if file_format == "ihex":
            if address or size:
                raise ValueError("address and size are not used for ihex writes.")
        else:
            if not address:
                raise ValueError("address is required for a binary flash write.")
            arguments.append(_cli_value(address, "address"))
            if size:
                raise ValueError(
                    "size is not supported for binary writes by all stlink releases."
                )
        return arguments

    # read
    if value:
        raise ValueError("read does not accept value.")
    if area in {"option_boot_add", "optcr", "optcr1"}:
        if any((file_path, address, size)):
            raise ValueError(f"{area} reads do not accept file, address, or size.")
        return arguments
    if area == "otp":
        if not file_path:
            raise ValueError("file_path is required for an OTP read.")
        if address or size:
            raise ValueError("OTP reads use file_path only.")
        arguments.append(_cli_value(file_path, "file_path"))
        return arguments
    if area == "option":
        if address:
            raise ValueError("Option-byte reads do not accept address.")
        if file_path:
            arguments.append(_cli_value(file_path, "file_path"))
        if size:
            if not file_path:
                raise ValueError("file_path is required when size is provided.")
            arguments.append(_cli_value(size, "size"))
        return arguments

    if not file_path or not address or not size:
        raise ValueError("read requires file_path, address, and size.")
    arguments.extend(
        [
            _cli_value(file_path, "file_path"),
            _cli_value(address, "address"),
            _cli_value(size, "size"),
        ]
    )
    return arguments


def _prepare_stlink_flash(
    operation: str,
    file_path: str,
    address: str,
    size: str,
    value: str,
    probe: str,
    freq: str,
    flash_size: str,
    file_format: str,
    area: str,
    opt: bool,
    mass_erase: bool,
    reset: bool,
    connect_under_reset: bool,
    hot_plug: bool,
    debug: bool,
    remote: str,
) -> tuple[list[str] | None, str, str]:
    try:
        serial = _resolve_serial(probe)
        if not _reserve_probe(serial):
            return None, _probe_busy_message(serial), ""
        arguments = _build_flash_args(
            operation,
            file_path,
            address,
            size,
            value,
            freq,
            flash_size,
            file_format,
            area,
            opt,
            mass_erase,
            reset,
            connect_under_reset,
            hot_plug,
            debug,
            serial,
            remote,
        )
    except ValueError as exc:
        if "serial" in locals():
            _release_probe(serial)
        return None, f"ERROR: {exc}", ""
    return arguments, "", serial


def _do_stlink_flash(
    operation: str,
    file_path: str,
    address: str,
    size: str,
    value: str,
    probe: str,
    freq: str,
    flash_size: str,
    file_format: str,
    area: str,
    opt: bool,
    mass_erase: bool,
    reset: bool,
    connect_under_reset: bool,
    hot_plug: bool,
    debug: bool,
    remote: str,
    timeout: int,
) -> str:
    arguments, error, serial = _prepare_stlink_flash(
        operation,
        file_path,
        address,
        size,
        value,
        probe,
        freq,
        flash_size,
        file_format,
        area,
        opt,
        mass_erase,
        reset,
        connect_under_reset,
        hot_plug,
        debug,
        remote,
    )
    if arguments is None:
        return error
    try:
        returncode, output = _execute_stlink("st-flash", arguments, timeout)
        return _format_result("st-flash", returncode, output)
    finally:
        _release_probe(serial)


async def stlink_flash(
    operation: str,
    file_path: str = "",
    address: str = "",
    size: str = "",
    value: str = "",
    probe: str = "",
    freq: str = "",
    flash_size: str = "",
    file_format: str = "",
    area: str = "",
    opt: bool = False,
    mass_erase: bool = False,
    reset: bool = False,
    connect_under_reset: bool = False,
    hot_plug: bool = False,
    debug: bool = False,
    remote: str = "",
    timeout: int = FLASH_TIMEOUT,
) -> str:
    """Run an installed ``st-flash`` read, write, erase, or reset operation.

    ``write`` normally takes ``file_path`` and a binary ``address``.  Intel
    HEX writes use ``file_format="ihex"`` and take their address from the file.
    ``read`` takes ``file_path``, ``address``, and ``size``.  Option-byte areas
    also support ``value`` for writes and stdout-oriented reads.
    """
    bounded_timeout = max(1, timeout)
    loop = asyncio.get_running_loop()
    prepare = loop.run_in_executor(
        _executor,
        _prepare_stlink_flash,
        operation,
        file_path,
        address,
        size,
        value,
        probe,
        freq,
        flash_size,
        file_format,
        area,
        opt,
        mass_erase,
        reset,
        connect_under_reset,
        hot_plug,
        debug,
        remote,
    )
    try:
        arguments, error, serial = await asyncio.wait_for(prepare, timeout=INFO_TIMEOUT)
    except asyncio.TimeoutError:
        return f"ERROR: st-flash preparation timed out after {INFO_TIMEOUT}s."
    if arguments is None:
        return error
    try:
        return await _execute_stlink_async("st-flash", arguments, bounded_timeout)
    finally:
        _release_probe(serial)


# ---------------------------------------------------------------------------
# Generic stlink command access
# ---------------------------------------------------------------------------


def _do_stlink_command(tool: str, arguments: list[str], timeout: int) -> str:
    if tool not in STLINK_TOOLS:
        return (
            f"ERROR: Unsupported stlink tool {tool!r}. Choose one of: "
            f"{', '.join(sorted(STLINK_TOOLS))}."
        )
    try:
        clean_arguments = [_cli_value(arg, "argument") for arg in arguments]
    except ValueError as exc:
        return f"ERROR: {exc}"
    returncode, output = _execute_stlink(tool, clean_arguments, timeout)
    return _format_result(tool, returncode, output)


async def stlink_command(
    tool: str,
    arguments: list[str] | None = None,
    timeout: int = COMMAND_TIMEOUT,
) -> str:
    """Run a command from the installed stlink toolset.

    This is an escape hatch for stlink versions with options not represented
    by the typed tools.  ``arguments`` is passed verbatim as an argument list,
    never through a shell.  Use the typed tools for flashing and managed
    debug/trace sessions when possible.
    """
    bounded_timeout = max(1, timeout)
    if tool not in STLINK_TOOLS:
        return (
            f"ERROR: Unsupported stlink tool {tool!r}. Choose one of: "
            f"{', '.join(sorted(STLINK_TOOLS))}."
        )
    try:
        clean_arguments = [_cli_value(arg, "argument") for arg in (arguments or [])]
    except ValueError as exc:
        return f"ERROR: {exc}"
    return await _execute_stlink_async(tool, clean_arguments, bounded_timeout)


# ---------------------------------------------------------------------------
# Long-running stlink processes
# ---------------------------------------------------------------------------


@dataclass
class _StlinkSession:
    session_id: str
    kind: str
    tool: str
    process: subprocess.Popen
    port: int | None = None
    serial: str = ""
    elf_path: str = ""
    started_at: float = field(default_factory=time.monotonic)
    output: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=TRACE_OUTPUT_LINES)
    )
    output_lock: threading.Lock = field(default_factory=threading.Lock)
    command_lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None


_sessions: dict[str, _StlinkSession] = {}
_sessions_lock = threading.Lock()
_probe_reservations: set[str] = set()


def _read_session_output(session: _StlinkSession) -> None:
    stream = session.process.stdout
    if stream is None:
        return
    try:
        for line in stream:
            with session.output_lock:
                session.output.append(line.rstrip("\r\n"))
    except (OSError, ValueError):
        pass


def _start_session(
    kind: str,
    tool: str,
    arguments: list[str],
    port: int | None = None,
    serial: str = "",
    elf_path: str = "",
) -> tuple[_StlinkSession | None, str]:
    executable = find_stlink_tool(tool)
    if not executable:
        return (
            None,
            f"ERROR: {tool} not found on PATH. Install the stlink toolset "
            "separately; stm32-mcp does not install it.",
        )

    with _sessions_lock:
        for active in _sessions.values():
            if active.process.poll() is None:
                if port is not None and active.port == port:
                    return None, f"ERROR: stlink session already uses port {port}."
                if _serials_conflict(serial, active.serial):
                    return None, _probe_busy_message(serial)
        if any(_serials_conflict(serial, reserved) for reserved in _probe_reservations):
            return None, _probe_busy_message(serial)
        _probe_reservations.add(serial)

    try:
        popen_args = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "shell": False,
            "text": True,
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name != "nt":
            popen_args["start_new_session"] = True
        process = subprocess.Popen([executable, *arguments], **popen_args)
    except OSError as exc:
        with _sessions_lock:
            _probe_reservations.discard(serial)
        return None, f"ERROR: Could not start {tool}: {exc}"

    session = _StlinkSession(
        session_id=uuid.uuid4().hex[:8],
        kind=kind,
        tool=tool,
        process=process,
        port=port,
        serial=serial,
        elf_path=elf_path,
    )
    reader = threading.Thread(
        target=_read_session_output,
        args=(session,),
        daemon=True,
        name=f"stlink-{kind}-{session.session_id}",
    )
    session.reader = reader
    with _sessions_lock:
        _sessions[session.session_id] = session
        _probe_reservations.discard(serial)
    reader.start()

    # Catch immediate failures (bad options, unavailable USB device) before
    # returning a session that can never be used.
    time.sleep(0.2)
    if process.poll() is not None:
        with _sessions_lock:
            _sessions.pop(session.session_id, None)
        output = _session_output(session, 100)
        return (
            None,
            f"ERROR: {tool} exited with code {process.returncode}."
            + (f"\n{output}" if output else ""),
        )

    return session, ""


def _session_output(session: _StlinkSession, last_n: int = 100) -> str:
    with session.output_lock:
        lines = list(session.output)[-max(1, last_n):]
    return "\n".join(lines).strip()


def _session_for(session_id: str, kind: str | None = None) -> _StlinkSession | None:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None or (kind is not None and session.kind != kind):
        return None
    return session


def _terminate_session(session: _StlinkSession) -> bool:
    if session.process.poll() is None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
            else:
                session.process.terminate()
            session.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(session.process.pid), signal.SIGKILL)
                else:
                    session.process.kill()
                session.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
    if session.reader and session.reader.is_alive():
        session.reader.join(timeout=1)
    return session.process.poll() is not None


def _stop_session(session_id: str, kind: str) -> str:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None or session.kind != kind:
        return f"ERROR: {kind} session {session_id!r} not found."

    locked = False
    if kind == "debug":
        locked = session.command_lock.acquire(blocking=False)
        if not locked:
            return f"ERROR: Debug session {session_id!r} has an active GDB command."
    try:
        if not _terminate_session(session):
            return f"ERROR: Could not stop {kind} session {session_id!r}."
    finally:
        if locked:
            session.command_lock.release()
    with _sessions_lock:
        _sessions.pop(session_id, None)
    output = _session_output(session, 50)
    result = f"{kind.capitalize()} session {session_id} stopped."
    if output:
        result += f"\nOutput:\n{output}"
    return result


def _session_status(session_id: str, kind: str, last_n: int) -> str:
    session = _session_for(session_id, kind)
    if session is None:
        return f"ERROR: {kind} session {session_id!r} not found."
    returncode = session.process.poll()
    state = "running" if returncode is None else f"exited ({returncode})"
    elapsed = time.monotonic() - session.started_at
    result = (
        f"{kind.capitalize()} session {session.session_id}\n"
        f"State: {state}\n"
        f"Uptime: {elapsed:.1f}s"
    )
    if session.port is not None:
        result += f"\nPort: {session.port}"
    output = _session_output(session, last_n)
    if output:
        result += f"\nOutput:\n{output}"
    return result


def _list_sessions() -> str:
    with _sessions_lock:
        sessions = list(_sessions.values())
    if not sessions:
        return "No active stlink sessions."

    lines = ["Active stlink sessions:"]
    for session in sessions:
        returncode = session.process.poll()
        state = "running" if returncode is None else f"exited ({returncode})"
        target = f"port={session.port}" if session.port is not None else "no port"
        if session.serial:
            target += f", serial={session.serial}"
        lines.append(f"- {session.session_id}: {session.kind} ({state}, {target})")
    return "\n".join(lines)


async def stlink_session_list() -> str:
    """List managed st-util, st-trace, and st-server sessions."""
    return await _run_async(_list_sessions, timeout=5)


def _serials_conflict(left: str, right: str) -> bool:
    return not left or not right or left == right


def _reserve_probe(serial: str) -> bool:
    with _sessions_lock:
        for session in _sessions.values():
            if session.process.poll() is None and _serials_conflict(serial, session.serial):
                return False
        if any(_serials_conflict(serial, reserved) for reserved in _probe_reservations):
            return False
        _probe_reservations.add(serial)
        return True


def _release_probe(serial: str) -> None:
    with _sessions_lock:
        _probe_reservations.discard(serial)


def _probe_busy(serial: str) -> bool:
    """Return whether a managed session may already own the requested probe."""
    with _sessions_lock:
        active_sessions = [
            session
            for session in _sessions.values()
            if session.process.poll() is None
        ]
        reservations = list(_probe_reservations)
    return any(_serials_conflict(serial, session.serial) for session in active_sessions) or any(
        _serials_conflict(serial, reserved) for reserved in reservations
    )


def _probe_busy_message(serial: str) -> str:
    target = serial or "the default probe"
    return f"ERROR: {target} is busy with an active stlink session. Stop it first."


def _stop_all_sessions() -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
    for session in sessions:
        if _terminate_session(session):
            with _sessions_lock:
                _sessions.pop(session.session_id, None)


atexit.register(_stop_all_sessions)


def shutdown_stlink_sessions() -> None:
    """Stop all managed stlink processes during MCP server shutdown."""
    _stop_all_sessions()


def _validate_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535.")
    return port


# ---------------------------------------------------------------------------
# st-util / GDB server
# ---------------------------------------------------------------------------


def _do_debug_start(
    probe: str,
    port: int,
    multi: bool,
    reset: bool,
    connect_under_reset: bool,
    hot_plug: bool,
    freq: str,
    semihosting: bool,
    elf_path: str,
) -> str:
    try:
        port = _validate_port(port)
        if connect_under_reset and hot_plug:
            raise ValueError("connect_under_reset and hot_plug are mutually exclusive.")
        if elf_path and not os.path.isfile(elf_path):
            raise ValueError(f"ELF file not found: {elf_path}")
        serial = _resolve_serial(probe)
    except ValueError as exc:
        return f"ERROR: {exc}"

    arguments = ["--listen_port", str(port)]
    if serial:
        arguments.extend(["--serial", serial])
    if freq:
        arguments.extend(["--freq", _cli_value(freq, "frequency")])
    if multi:
        arguments.append("--multi")
    if not reset or hot_plug:
        arguments.append("--no-reset")
    if connect_under_reset:
        arguments.append("--connect-under-reset")
    if semihosting:
        arguments.append("--semihosting")

    session, error = _start_session(
        "debug", "st-util", arguments, port=port, serial=serial, elf_path=elf_path
    )
    if session is None:
        return error
    result = (
        "st-util GDB server started.\n"
        f"Session ID: {session.session_id}\n"
        f"Endpoint: localhost:{port}\n"
        "Use stlink_debug_command with this session ID to execute GDB commands."
    )
    if elf_path:
        result += f"\nELF: {elf_path}"
    return result


async def stlink_debug_start(
    probe: str = "",
    port: int = 4242,
    multi: bool = True,
    reset: bool = True,
    connect_under_reset: bool = False,
    hot_plug: bool = False,
    freq: str = "",
    semihosting: bool = False,
    elf_path: str = "",
) -> str:
    """Start ``st-util`` and expose a GDB debugging session.

    The returned session ID is used by ``stlink_debug_command``,
    ``stlink_debug_status``, and ``stlink_debug_stop``.  The optional ELF is
    supplied to GDB for symbol names and may be omitted for raw remote debug.
    """
    return await _run_async(
        _do_debug_start,
        probe,
        port,
        multi,
        reset,
        connect_under_reset,
        hot_plug,
        freq,
        semihosting,
        elf_path,
        timeout=DEBUG_TIMEOUT,
    )


def _build_gdb_arguments(
    session_id: str,
    commands: str,
    elf_path: str,
) -> tuple[_StlinkSession | None, list[str] | None, str]:
    session = _session_for(session_id, "debug")
    if session is None:
        return None, None, f"ERROR: Debug session {session_id!r} not found."
    if session.process.poll() is not None:
        return session, None, _session_status(session_id, "debug", 50)

    gdb = find_gdb()
    if not gdb:
        return (
            session,
            None,
            "ERROR: arm-none-eabi-gdb not found on PATH. Add the ARM toolchain "
            "from STM32CubeIDE to PATH.",
        )
    selected_elf = elf_path or session.elf_path
    if selected_elf and not os.path.isfile(selected_elf):
        return session, None, f"ERROR: ELF file not found: {selected_elf}"

    command_lines = [line.strip() for line in commands.splitlines() if line.strip()]
    if not command_lines:
        return session, None, "ERROR: commands must contain at least one GDB command."
    if any("\x00" in line for line in command_lines):
        return session, None, "ERROR: GDB commands may not contain NUL bytes."

    arguments = [gdb, "--nx", "--nh", "--batch"]
    if selected_elf:
        arguments.append(selected_elf)
    arguments.extend(
        [
            "-ex",
            "set pagination off",
            "-ex",
            f"target extended-remote localhost:{session.port}",
        ]
    )
    for command in command_lines:
        arguments.extend(["-ex", command])
    return session, arguments, ""


def _build_debug_action(
    action: str,
    location: str,
    expression: str,
    register: str,
    count: int,
    hardware: bool,
) -> str:
    if count < 1 or count > 1000:
        raise ValueError("count must be between 1 and 1000.")

    def required(value: str, name: str) -> str:
        clean = value.strip()
        if not clean or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"{name} is required and must be a single line.")
        return clean

    if action == "breakpoint":
        command = "hbreak" if hardware else "break"
        return f"{command} {required(location, 'location')}"
    if action == "watchpoint":
        return f"watch {required(expression, 'expression')}"
    if action == "delete_breakpoint":
        return f"delete {required(location, 'location')}"
    if action == "continue":
        return "continue"
    if action == "step":
        return "step" if count == 1 else f"step {count}"
    if action == "next":
        return "next" if count == 1 else f"next {count}"
    if action == "finish":
        return "finish"
    if action == "variables":
        return "info locals" if not expression else f"print {required(expression, 'expression')}"
    if action == "registers":
        if not register:
            return "info registers"
        return f"p/x ${required(register.lstrip('$'), 'register')}"
    if action == "memory":
        return f"x/{count}wx {required(location, 'address')}"
    if action == "backtrace":
        return "bt"
    if action == "breakpoints":
        return "info breakpoints"
    if action == "disassemble":
        return "disassemble" if not location else f"disassemble {required(location, 'location')}"
    if action == "reset":
        return "monitor reset"
    if action == "load":
        return "load"
    raise ValueError(
        "Unsupported debug action. Choose breakpoint, watchpoint, "
        "delete_breakpoint, continue, step, next, finish, variables, "
        "registers, memory, backtrace, breakpoints, disassemble, reset, or load."
    )


def _do_debug_command(
    session_id: str,
    commands: str,
    elf_path: str,
    timeout: int,
) -> str:
    session = _session_for(session_id, "debug")
    if session is None:
        return f"ERROR: Debug session {session_id!r} not found."
    if not session.command_lock.acquire(blocking=False):
        return f"ERROR: Debug session {session_id!r} already has a GDB command running."
    try:
        return _do_debug_command_locked(session_id, commands, elf_path, timeout)
    finally:
        session.command_lock.release()


def _do_debug_command_locked(
    session_id: str,
    commands: str,
    elf_path: str,
    timeout: int,
) -> str:
    session = _session_for(session_id, "debug")
    if session is None:
        return f"ERROR: Debug session {session_id!r} not found."
    if session.process.poll() is not None:
        return _session_status(session_id, "debug", 50)

    gdb = find_gdb()
    if not gdb:
        return (
            "ERROR: arm-none-eabi-gdb not found on PATH. Add the ARM toolchain "
            "from STM32CubeIDE to PATH."
        )
    selected_elf = elf_path or session.elf_path
    if selected_elf and not os.path.isfile(selected_elf):
        return f"ERROR: ELF file not found: {selected_elf}"

    command_lines = [line.strip() for line in commands.splitlines() if line.strip()]
    if not command_lines:
        return "ERROR: commands must contain at least one GDB command."
    if any("\x00" in line for line in command_lines):
        return "ERROR: GDB commands may not contain NUL bytes."

    arguments = [gdb, "--nx", "--nh", "--batch"]
    if selected_elf:
        arguments.append(selected_elf)
    arguments.extend(
        [
            "-ex",
            "set pagination off",
            "-ex",
            f"target extended-remote localhost:{session.port}",
        ]
    )
    for command in command_lines:
        arguments.extend(["-ex", command])

    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            shell=False,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = _output_text(exc.stdout, exc.stderr).strip()
        suffix = f"\n{output}" if output else ""
        return f"ERROR: GDB command timed out after {timeout}s.{suffix}"
    except OSError as exc:
        return f"ERROR: Could not run arm-none-eabi-gdb: {exc}"

    return _format_result("arm-none-eabi-gdb", result.returncode, _output_text(result.stdout, result.stderr))


async def _do_debug_command_async(
    session_id: str,
    commands: str,
    elf_path: str,
    timeout: int,
) -> str:
    session = _session_for(session_id, "debug")
    if session is None:
        return f"ERROR: Debug session {session_id!r} not found."
    if not session.command_lock.acquire(blocking=False):
        return f"ERROR: Debug session {session_id!r} already has a GDB command running."
    try:
        _, arguments, error = _build_gdb_arguments(session_id, commands, elf_path)
        if arguments is None:
            return error
        for attempt in range(3):
            result = await _execute_gdb_async(arguments, timeout)
            if "connection refused" not in result.lower() or attempt == 2:
                return result
            await asyncio.sleep(0.2)
        return result
    finally:
        session.command_lock.release()


async def stlink_debug_command(
    session_id: str,
    commands: str,
    elf_path: str = "",
    timeout: int = DEBUG_TIMEOUT,
) -> str:
    """Execute newline-separated GDB commands against an st-util session.

    Examples of commands are ``info registers``, ``x/16wx 0x20000000``,
    ``break main``, ``continue``, ``step``, ``load``, and ``monitor reset``.
    Each call uses an ARM GDB batch client while the st-util server remains
    alive for the next command sequence.
    """
    bounded_timeout = max(1, timeout)
    return await _do_debug_command_async(session_id, commands, elf_path, bounded_timeout)


async def stlink_debug_action(
    action: str,
    session_id: str = "",
    location: str = "",
    expression: str = "",
    register: str = "",
    count: int = 1,
    hardware: bool = False,
    gdb_session_id: str = "",
    elf_path: str = "",
    timeout: int = DEBUG_TIMEOUT,
) -> str:
    """Run a typed GDB debugging action in an st-util session.

    This convenience tool covers breakpoints, watchpoints, stepping, local
    variables, registers, memory, backtraces, disassembly, reset, and load.
    Use stlink_debug_command for any other GDB command or a multi-command
    sequence.
    """
    try:
        command = _build_debug_action(
            action, location, expression, register, count, hardware
        )
    except ValueError as exc:
        return f"ERROR: {exc}"
    if gdb_session_id:
        return await stlink_gdb_command(gdb_session_id, command, timeout)
    return await stlink_debug_command(session_id, command, elf_path, timeout)


async def stlink_debug_status(session_id: str, last_n: int = 50) -> str:
    """Return the state and recent output of an st-util debug session."""
    return await _run_async(
        _session_status, session_id, "debug", min(max(last_n, 1), 200), timeout=5
    )


async def stlink_debug_stop(session_id: str) -> str:
    """Stop an st-util debug session."""
    return await _run_async(_stop_session, session_id, "debug", timeout=10)


# ---------------------------------------------------------------------------
# st-trace
# ---------------------------------------------------------------------------


def _do_trace_start(
    probe: str,
    core_frequency: str,
    trace_frequency: str,
    no_reset: bool,
    force: bool,
    verbose: int,
    remote: str,
) -> str:
    try:
        serial = _resolve_serial(probe)
        if verbose < -1 or verbose > 99:
            raise ValueError("verbose must be -1 or an integer from 0 to 99.")
    except ValueError as exc:
        return f"ERROR: {exc}"

    arguments: list[str] = []
    if core_frequency:
        arguments.extend(["--clock", _cli_value(core_frequency, "core frequency")])
    if trace_frequency:
        arguments.extend(["--trace", _cli_value(trace_frequency, "trace frequency")])
    if serial:
        arguments.extend(["--serial", serial])
    if remote:
        arguments.extend(["--remote", _cli_value(remote, "remote endpoint")])
    if no_reset:
        arguments.append("--no-reset")
    if force:
        arguments.append("--force")
    if verbose >= 0:
        arguments.append(f"--verbose={verbose}")

    session, error = _start_session("trace", "st-trace", arguments, serial=serial)
    if session is None:
        return error
    return (
        "st-trace started.\n"
        f"Session ID: {session.session_id}\n"
        "Use stlink_trace_read to collect output and stlink_trace_stop to stop it."
    )


async def stlink_trace_start(
    probe: str = "",
    core_frequency: str = "",
    trace_frequency: str = "",
    no_reset: bool = False,
    force: bool = False,
    verbose: int = -1,
    remote: str = "",
) -> str:
    """Start the installed ``st-trace`` SWO trace reader."""
    return await _run_async(
        _do_trace_start,
        probe,
        core_frequency,
        trace_frequency,
        no_reset,
        force,
        verbose,
        remote,
        timeout=DEBUG_TIMEOUT,
    )


async def stlink_trace_read(session_id: str, last_n: int = 100) -> str:
    """Read buffered output from an active st-trace session."""
    session = _session_for(session_id, "trace")
    if session is None:
        return f"ERROR: Trace session {session_id!r} not found."
    output = _session_output(session, min(max(last_n, 1), TRACE_OUTPUT_LINES))
    if not output:
        return "No trace output yet."
    return output


async def stlink_trace_status(session_id: str, last_n: int = 50) -> str:
    """Return the state and recent output of an st-trace session."""
    return await _run_async(
        _session_status, session_id, "trace", min(max(last_n, 1), 200), timeout=5
    )


async def stlink_trace_stop(session_id: str) -> str:
    """Stop an st-trace session."""
    return await _run_async(_stop_session, session_id, "trace", timeout=10)


# ---------------------------------------------------------------------------
# st-server
# ---------------------------------------------------------------------------


def _do_server_start(
    probe: str,
    bind: str,
    port: int,
    freq: str,
    verbose: int,
    allow_non_loopback: bool,
) -> str:
    try:
        serial = _resolve_serial(probe)
        if port:
            _validate_port(port)
        if bind and ":" in bind and bind != "::1":
            raise ValueError("bind must contain an address only; use port for the TCP port.")
        if bind and not allow_non_loopback and bind not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "Non-loopback bind requires allow_non_loopback=true because st-server "
                "has no authentication."
            )
        if verbose < -1 or verbose > 99:
            raise ValueError("verbose must be -1 or an integer from 0 to 99.")
    except ValueError as exc:
        return f"ERROR: {exc}"

    arguments: list[str] = []
    if serial:
        arguments.extend(["--serial", serial])
    if bind:
        arguments.extend(["--bind", _cli_value(bind, "bind address")])
    if port:
        arguments.extend(["--port", str(port)])
    if freq:
        arguments.extend(["--freq", _cli_value(freq, "frequency")])
    if verbose >= 0:
        arguments.append(f"--verbose={verbose}")

    effective_port = port or 4500
    session, error = _start_session(
        "server", "st-server", arguments, port=effective_port, serial=serial
    )
    if session is None:
        return error
    endpoint = bind or "127.0.0.1"
    endpoint += f":{effective_port}"
    return (
        "st-server started.\n"
        f"Session ID: {session.session_id}\n"
        f"Endpoint: {endpoint}\n"
        "Use stlink_server_stop to stop it."
    )


async def stlink_server_start(
    probe: str = "",
    bind: str = "",
    port: int = 0,
    freq: str = "",
    verbose: int = -1,
    allow_non_loopback: bool = False,
) -> str:
    """Start the optional installed ``st-server`` TCP proxy.

    Non-loopback binding is disabled by default because st-server exposes
    unauthenticated flash and debug control to every client that can reach it.
    """
    return await _run_async(
        _do_server_start,
        probe,
        bind,
        port,
        freq,
        verbose,
        allow_non_loopback,
        timeout=DEBUG_TIMEOUT,
    )


async def stlink_server_status(session_id: str, last_n: int = 50) -> str:
    """Return the state and recent output of an st-server session."""
    return await _run_async(
        _session_status, session_id, "server", min(max(last_n, 1), 200), timeout=5
    )


async def stlink_server_stop(session_id: str) -> str:
    """Stop an st-server session."""
    return await _run_async(_stop_session, session_id, "server", timeout=10)
