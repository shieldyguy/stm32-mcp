"""Flash tools — stlink-preferred flash with an OpenOCD fallback."""

import asyncio
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from .board_map import resolve_probe_full
from .stlink import stlink_flash
from .toolchain import find_objcopy, run_openocd

_executor = ThreadPoolExecutor(max_workers=2)

FLASH_TIMEOUT = 30  # seconds
INFO_TIMEOUT = 10   # seconds
OBJCOPY_TIMEOUT = 30  # seconds


def _is_elf(path: str) -> bool:
    if path.lower().endswith(".elf"):
        return True
    try:
        with open(path, "rb") as firmware:
            return firmware.read(4) == b"\x7fELF"
    except OSError:
        return False


def _convert_elf_to_ihex(elf_path: str, ihex_path: str) -> str | None:
    """Convert ELF loadable sections to Intel HEX for st-flash."""
    objcopy = find_objcopy()
    if not objcopy:
        return (
            "ERROR: arm-none-eabi-objcopy not found. Set "
            "STM32_ARM_TOOLCHAIN_BIN or add the ARM toolchain to PATH."
        )
    try:
        result = subprocess.run(
            [objcopy, "-O", "ihex", elf_path, ihex_path],
            capture_output=True,
            shell=False,
            text=True,
            errors="replace",
            timeout=OBJCOPY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: ELF conversion timed out after {OBJCOPY_TIMEOUT}s."
    except OSError as exc:
        return f"ERROR: Could not run arm-none-eabi-objcopy: {exc}"

    if result.returncode != 0:
        output = (result.stdout + "\n" + result.stderr).strip()
        return (
            f"ERROR: ELF conversion failed.\n{output}"
            if output
            else "ERROR: ELF conversion failed."
        )
    return None


async def _flash_with_stlink(
    file_path: str,
    reset: bool,
    verify: bool,
    probe: str,
) -> str:
    """Flash ELF, binary, or Intel HEX using the direct stlink backend."""
    if not os.path.isfile(file_path):
        return f"ERROR: File not found: {file_path}"

    flash_path = file_path
    file_format = ""
    temporary_path = ""
    loop = asyncio.get_running_loop()
    try:
        if _is_elf(file_path):
            temporary = tempfile.NamedTemporaryFile(suffix=".hex", delete=False)
            temporary.close()
            temporary_path = temporary.name
            conversion_error = await loop.run_in_executor(
                _executor, _convert_elf_to_ihex, file_path, temporary_path
            )
            if conversion_error:
                return conversion_error
            flash_path = temporary_path
            file_format = "ihex"
        elif file_path.lower().endswith((".hex", ".ihex")):
            file_format = "ihex"
        else:
            file_format = "binary"

        result = await stlink_flash(
            operation="write",
            file_path=flash_path,
            address="" if file_format == "ihex" else "0x08000000",
            probe=probe,
            file_format=file_format,
            reset=reset,
        )
        if verify and not result.startswith("ERROR:"):
            result = "Backend: stlink/st-flash\n" + result
        elif not verify and not result.startswith("ERROR:"):
            result = "Backend: stlink/st-flash (verification option unavailable)\n" + result
        return result
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _do_flash(
    elf_path: str,
    reset: bool = True,
    verify: bool = True,
    sn: str = "",
    target_cfg: str = "",
    chipid: int = 0,
) -> str:
    """Synchronous flash via OpenOCD — runs in executor thread."""
    if chipid == -1:
        return "ERROR: Probe connected but no target MCU detected. Check board power and SWD connection."
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    if not os.path.isfile(elf_path):
        return f"ERROR: File not found: {elf_path}"

    # Build OpenOCD program command
    # "program <file> [verify] [reset] exit"
    prog_parts = [f"program {{{elf_path}}}"]
    if verify:
        prog_parts.append("verify")
    if reset:
        prog_parts.append("reset")
    prog_parts.append("exit")
    prog_cmd = " ".join(prog_parts)

    try:
        output = run_openocd(sn, target_cfg, [prog_cmd], timeout=FLASH_TIMEOUT, chipid=chipid)
    except subprocess.TimeoutExpired:
        return f"ERROR: Flash timed out after {FLASH_TIMEOUT}s."
    except FileNotFoundError as e:
        return f"ERROR: {e}"

    # Parse for common errors
    if "no device found" in output.lower() or "unable to find" in output.lower():
        return "ERROR: No ST-Link detected. Check USB connection and board power."

    if "read protection" in output.lower():
        return (
            "ERROR: Chip is read-protected. Mass erase needed to unlock.\n"
            "WARNING: This erases all flash contents."
        )

    # Extract key info
    parts = []

    # ST-LINK info from OpenOCD connect output
    stlink_match = re.search(r'serial:\s+([0-9A-Fa-f]+)', output)
    if stlink_match:
        parts.append(f"ST-LINK: {stlink_match.group(1)}")

    # Flash result
    if "** Programming Finished **" in output:
        parts.append("Flash: OK")
    elif "Error" in output or "error" in output.lower():
        parts.append("Flash: FAILED")

    # Verify result
    if verify:
        if "** Verified OK **" in output:
            parts.append("Verify: OK")
        elif "** Verify Failed **" in output:
            parts.append("Verify: FAILED")

    if reset:
        parts.append("Reset: OK")

    # If we couldn't parse anything useful, return raw output
    if not parts:
        return output.strip()

    # Add any error lines from output
    for line in output.splitlines():
        stripped = line.strip()
        if "error" in stripped.lower() and "error:" not in "\n".join(parts).lower():
            if stripped and not stripped.startswith("Open On-Chip"):
                parts.append(f"  {stripped}")

    return "\n".join(parts)


def _do_board_info(sn: str = "", target_cfg: str = "", chipid: int = 0) -> str:
    """Synchronous board info via OpenOCD — runs in executor thread."""
    if chipid == -1:
        return "ERROR: Probe connected but no target MCU detected. Check board power and SWD connection."
    if not sn or not target_cfg:
        return "ERROR: Could not resolve probe. Use stm32_list_probes to see connected boards."

    try:
        output = run_openocd(sn, target_cfg, ["init"], timeout=INFO_TIMEOUT, chipid=chipid)
    except subprocess.TimeoutExpired:
        return f"ERROR: Board info timed out after {INFO_TIMEOUT}s."
    except FileNotFoundError as e:
        return f"ERROR: {e}"

    if "no device found" in output.lower() or "unable to find" in output.lower():
        return "ERROR: No ST-Link detected. Check USB connection and board power."

    # Extract useful fields from OpenOCD connect output
    info = []

    # ST-LINK version: "STLINK V3Jxx"
    stlink_ver = re.search(r'STLINK\s+(V\S+)', output)
    if stlink_ver:
        info.append(f"ST-LINK FW: {stlink_ver.group(1)}")

    # Serial
    sn_match = re.search(r'serial:\s+([0-9A-Fa-f]+)', output)
    if sn_match:
        info.append(f"ST-LINK SN: {sn_match.group(1)}")

    # Target voltage: "Target voltage: 3.288"
    voltage = re.search(r'Target voltage:\s+([\d.]+)', output)
    if voltage:
        info.append(f"Voltage: {voltage.group(1)}V")

    # CPU type: "cortex_m4" or "Cortex-M4"
    cpu = re.search(r'(Cortex-M\d\+?|cortex_m\d\+?)', output, re.IGNORECASE)
    if cpu:
        info.append(f"CPU: {cpu.group(1)}")

    # Flash size from OpenOCD: "flash size = 512kb"
    flash_size = re.search(r'flash\s+size\s*=\s*(\d+\s*[kKmM]?[bB]?)', output)
    if flash_size:
        info.append(f"Flash: {flash_size.group(1)}")

    # Chip ID
    chip_match = re.search(r'chip\s+id.*?(0x[0-9a-fA-F]+)', output, re.IGNORECASE)
    if chip_match:
        info.append(f"Chip ID: {chip_match.group(1)}")

    # Add board/probe nicknames
    from .board_map import get_board_nickname_for_probe_sn, get_probe_nickname, _probe_cache
    probe_nick = get_probe_nickname(sn)
    board_nick = get_board_nickname_for_probe_sn(sn)
    if board_nick:
        info.append(f'Board: "{board_nick}"')
    if probe_nick:
        info.append(f'Probe: "{probe_nick}"')

    if info:
        return "\n".join(info)

    # Fallback: return trimmed output
    return output.strip()


async def stm32_flash(
    elf_path: str,
    reset: bool = True,
    verify: bool = True,
    probe: str = "",
    backend: str = "stlink",
) -> str:
    """Flash firmware to STM32 board, preferring direct stlink by default.

    Writes the specified .elf (or .bin/.hex) file to the connected STM32's
    flash memory. The default ``stlink`` backend uses st-flash; ``openocd``
    remains available for ELF files or workflows that explicitly require it.

    Args:
        elf_path: Absolute path to the firmware file (.elf, .bin, or .hex).
        reset: If true, hard-reset the board after flashing.
        verify: If true, verify flash contents match the file.
        probe: Board nickname, probe nickname, or ST-Link SN to target a specific board.
        backend: ``stlink`` (default) or ``openocd``.

    Returns:
        Flash result — ST-LINK info, download status, verify result.
    """
    if backend == "stlink":
        return await _flash_with_stlink(elf_path, reset, verify, probe)
    if backend != "openocd":
        return "ERROR: backend must be 'stlink' or 'openocd'."

    sn, target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor, lambda: _do_flash(elf_path, reset, verify, sn, target_cfg, chipid)
        ),
        timeout=FLASH_TIMEOUT + 10,
    )


async def stm32_board_info(probe: str = "") -> str:
    """Read board information via ST-Link.

    Connects to the STM32 via SWD and reads device info: ST-LINK version,
    voltage, CPU type, and flash size. Useful for verifying the ST-Link
    connection before building/flashing.

    Args:
        probe: Board nickname, probe nickname, or ST-Link SN to target a specific board.

    Returns:
        Board info — ST-LINK firmware, serial, voltage, CPU, flash size.
    """
    sn, target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor, lambda: _do_board_info(sn, target_cfg, chipid)
        ),
        timeout=INFO_TIMEOUT + 10,
    )
