"""STM32WB FUS + BLE Stack install tools.

Wraps STM32_Programmer_CLI `-fwupgrade` to automate first-time bringup on
bare STM32WB boards. See FUS_INSTALL.md in the ryco/ccb repo for the raw
procedure this codifies.

Only STM32WB1x (chipid 0x494) is wired in v1. Add entries to _WB_INSTALL_MAP
for WB5x / WB3x when that hardware is on the bench.
"""

import argparse
import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from .board_map import resolve_probe_full
from .toolchain import find_stm32_programmer_cli, find_wireless_binaries_dir

_executor = ThreadPoolExecutor(max_workers=2)

FUS_STEP_TIMEOUT = 120   # seconds per -fwupgrade call
BOOTSTRAP_TIMEOUT = 300  # seconds for the combined two-step tool


# Per-chip install data. Addresses from FUS_INSTALL.md (WB1x, confirmed on bench).
# Other WB variants live at different addresses; see STM32WB Release_Notes.html.
_WB_INSTALL_MAP: dict[int, dict[str, str]] = {
    0x494: {
        "family": "STM32WB1x",
        "subdir": "STM32WB1x",
        "fus_bin": "stm32wb1x_FUS_fw.bin",
        "fus_addr": "0x08040000",
        "stack_full_bin": "stm32wb1x_BLE_Stack_full_fw.bin",
        "stack_full_addr": "0x08022800",
    },
}


# ---------------------------------------------------------------------------
# Helpers (run in executor thread)
# ---------------------------------------------------------------------------

def _resolve_wireless_dir(wireless_dir: str) -> tuple[str | None, str]:
    """Return (resolved_path, error_message). One is None."""
    if wireless_dir:
        if not os.path.isdir(wireless_dir):
            return None, f"ERROR: wireless_dir does not exist: {wireless_dir}"
        return wireless_dir, ""
    found = find_wireless_binaries_dir()
    if not found:
        return None, (
            "ERROR: Wireless binaries folder not found. Set "
            "$STM32_WIRELESS_BINARIES or pass wireless_dir explicitly. See "
            "FUS_INSTALL.md for the sparse-checkout command."
        )
    return found, ""


def _validate_chip(chipid: int) -> tuple[dict | None, str]:
    """Return (install_entry, error). One is None."""
    if chipid == 0 or chipid == -1:
        return None, (
            "ERROR: Could not read target chipid. Check power and SWD "
            "connection, then stm32_list_probes."
        )
    entry = _WB_INSTALL_MAP.get(chipid)
    if not entry:
        return None, (
            f"ERROR: chipid 0x{chipid:X} not in FUS install map. Only WB1x "
            "(0x494) is supported. Add an entry to _WB_INSTALL_MAP with the "
            "correct binary names + addresses from the STM32WB Release_Notes."
        )
    return entry, ""


def _run_fwupgrade(
    cli: str,
    sn: str,
    bin_path: str,
    addr: str,
    firstinstall: int,
    timeout: int = FUS_STEP_TIMEOUT,
) -> str:
    """Shell out to STM32_Programmer_CLI -fwupgrade. Returns combined output."""
    cmd = [
        cli,
        "-c", "port=swd", f"sn={sn}", "mode=UR",
        "-fwupgrade", bin_path, addr, f"firstinstall={firstinstall}",
        "-v",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout + "\n" + result.stderr


def _classify_fwupgrade(output: str, is_stack_install: bool) -> tuple[str, str]:
    """Return (status, note). status is one of OK / WARN / FAIL."""
    lower = output.lower()
    if "firmware upgrade success" in lower:
        return "OK", ""
    if "fus_state_img_not_found" in lower:
        if "flash already empty" in lower:
            return "OK", "benign: no prior image to delete"
        if is_stack_install:
            return "FAIL", (
                "FUS_STATE_IMG_NOT_FOUND during stack install — FUS is "
                "probably too old. Run stm32_fus_upgrade first."
            )
        return "FAIL", "FUS_STATE_IMG_NOT_FOUND and flash was not empty"
    if "read protection" in lower:
        return "FAIL", "chip is read-protected; mass erase needed"
    if "no stlink" in lower or "no device found" in lower:
        return "FAIL", "ST-Link not detected"
    return "FAIL", "no success marker in CLI output"


def _tail(output: str, n: int = 20) -> str:
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _fus_upgrade_sync(sn: str, chipid: int, wireless_dir: str) -> str:
    entry, err = _validate_chip(chipid)
    if err:
        return err
    wdir, err = _resolve_wireless_dir(wireless_dir)
    if err:
        return err
    cli = find_stm32_programmer_cli()
    if not cli:
        return "ERROR: STM32_Programmer_CLI not found. Install STM32CubeProgrammer."

    bin_path = os.path.join(wdir, entry["subdir"], entry["fus_bin"])
    if not os.path.isfile(bin_path):
        return f"ERROR: FUS binary missing: {bin_path}"

    try:
        output = _run_fwupgrade(cli, sn, bin_path, entry["fus_addr"], firstinstall=0)
    except subprocess.TimeoutExpired:
        return f"ERROR: FUS upgrade timed out after {FUS_STEP_TIMEOUT}s."
    except FileNotFoundError as e:
        return f"ERROR: {e}"

    status, note = _classify_fwupgrade(output, is_stack_install=False)
    header = f"FUS upgrade ({entry['family']}): {status}"
    if note:
        header += f" ({note})"
    return header + "\n---\n" + _tail(output)


def _ble_stack_install_sync(sn: str, chipid: int, variant: str, wireless_dir: str) -> str:
    if variant != "full":
        return (
            f"ERROR: BLE stack variant '{variant}' not wired. Only 'full' is "
            "supported. Add variant binary/address entries to _WB_INSTALL_MAP."
        )
    entry, err = _validate_chip(chipid)
    if err:
        return err
    wdir, err = _resolve_wireless_dir(wireless_dir)
    if err:
        return err
    cli = find_stm32_programmer_cli()
    if not cli:
        return "ERROR: STM32_Programmer_CLI not found. Install STM32CubeProgrammer."

    bin_path = os.path.join(wdir, entry["subdir"], entry["stack_full_bin"])
    if not os.path.isfile(bin_path):
        return f"ERROR: BLE stack binary missing: {bin_path}"

    try:
        output = _run_fwupgrade(
            cli, sn, bin_path, entry["stack_full_addr"], firstinstall=1,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: BLE stack install timed out after {FUS_STEP_TIMEOUT}s."
    except FileNotFoundError as e:
        return f"ERROR: {e}"

    status, note = _classify_fwupgrade(output, is_stack_install=True)
    header = f"BLE stack install ({entry['family']}, {variant}): {status}"
    if note:
        header += f" ({note})"
    return header + "\n---\n" + _tail(output)


def _fus_bootstrap_sync(sn: str, chipid: int, wireless_dir: str) -> str:
    step1 = _fus_upgrade_sync(sn, chipid, wireless_dir)
    parts = ["=== Step 1: FUS upgrade ===", step1]
    if step1.startswith("ERROR") or ": FAIL" in step1.splitlines()[0]:
        parts.append("\nAborting bootstrap: FUS upgrade did not succeed.")
        return "\n".join(parts)

    step2 = _ble_stack_install_sync(sn, chipid, "full", wireless_dir)
    parts += ["", "=== Step 2: BLE stack install ===", step2]
    parts += [
        "",
        "=== Next step ===",
        "Re-flash user firmware — FUS ops wipe flash sectors [0..3]. Use:",
        '  stm32_build_and_flash(project_path="...", probe="...")',
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Async MCP tool wrappers
# ---------------------------------------------------------------------------

async def stm32_fus_upgrade(probe: str = "", wireless_dir: str = "") -> str:
    """Upgrade FUS (Firmware Upgrade Service) on an STM32WB board.

    Step 1 of first-time STM32WB bringup. Must run before installing a modern
    BLE stack on a board shipped with an older FUS. Uses mode=UR and
    firstinstall=0 per FUS_INSTALL.md.

    Args:
        probe: Board nickname, probe nickname, or ST-Link SN.
        wireless_dir: Optional path to STM32WB_Copro_Wireless_Binaries. Falls
            back to $STM32_WIRELESS_BINARIES then
            ~/stm32cube-wb/Projects/STM32WB_Copro_Wireless_Binaries.

    Returns:
        Status header plus tail of the STM32_Programmer_CLI output.
    """
    sn, _target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor, lambda: _fus_upgrade_sync(sn, chipid, wireless_dir)
        ),
        timeout=FUS_STEP_TIMEOUT + 10,
    )


async def stm32_ble_stack_install(
    probe: str = "",
    variant: str = "full",
    wireless_dir: str = "",
) -> str:
    """Install BLE stack on an STM32WB board (requires FUS already upgraded).

    Step 2 of first-time bringup. Uses firstinstall=1. If this reports
    FUS_STATE_IMG_NOT_FOUND, the FUS is too old — run stm32_fus_upgrade first.

    Args:
        probe: Board nickname, probe nickname, or ST-Link SN.
        variant: BLE stack variant. Only 'full' is currently wired. Other
            variants (light, hci, lld) live at different addresses.
        wireless_dir: Optional path to STM32WB_Copro_Wireless_Binaries.

    Returns:
        Status header plus tail of the STM32_Programmer_CLI output.
    """
    sn, _target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            lambda: _ble_stack_install_sync(sn, chipid, variant, wireless_dir),
        ),
        timeout=FUS_STEP_TIMEOUT + 10,
    )


async def stm32_fus_bootstrap(probe: str = "", wireless_dir: str = "") -> str:
    """One-shot STM32WB bringup: FUS upgrade, then BLE stack full install.

    Does NOT re-flash user firmware. FUS operations wipe flash sectors [0..3]
    via a helper binary, so after this call run stm32_build_and_flash with the
    correct project_path to restore your application.

    Args:
        probe: Board nickname, probe nickname, or ST-Link SN.
        wireless_dir: Optional path to STM32WB_Copro_Wireless_Binaries.

    Returns:
        Full transcript of both steps and a reminder to re-flash user firmware.
    """
    sn, _target_cfg, chipid = resolve_probe_full(probe)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor, lambda: _fus_bootstrap_sync(sn, chipid, wireless_dir)
        ),
        timeout=BOOTSTRAP_TIMEOUT + 10,
    )


# ---------------------------------------------------------------------------
# Shell CLI entry point — `stm32-fus <subcommand> --probe "<nick>"`
# ---------------------------------------------------------------------------

def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stm32-fus",
        description="STM32WB FUS + BLE stack install over ST-Link/SWD.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--probe", required=True,
            help="Board nickname, probe nickname, or ST-Link serial number.",
        )
        p.add_argument(
            "--wireless-dir", default="",
            help="Override wireless binaries folder.",
        )

    _add_common(sub.add_parser("upgrade", help="FUS upgrade only"))
    stack = sub.add_parser("stack", help="BLE stack install only")
    _add_common(stack)
    stack.add_argument("--variant", default="full", help="BLE stack variant")
    _add_common(sub.add_parser(
        "bootstrap", help="FUS upgrade + BLE stack full install",
    ))

    args = parser.parse_args(argv)
    sn, _target_cfg, chipid = resolve_probe_full(args.probe)

    if args.cmd == "upgrade":
        out = _fus_upgrade_sync(sn, chipid, args.wireless_dir)
    elif args.cmd == "stack":
        out = _ble_stack_install_sync(sn, chipid, args.variant, args.wireless_dir)
    else:
        out = _fus_bootstrap_sync(sn, chipid, args.wireless_dir)

    print(out)
    first_line = out.splitlines()[0] if out else ""
    return 0 if not first_line.startswith("ERROR") and ": FAIL" not in first_line else 1


if __name__ == "__main__":
    sys.exit(cli_main())
