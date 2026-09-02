"""Board & probe nickname mapping with multi-target support.

Persistence: ~/.stm32-mcp/board_map.json
Session cache: module-level dict mapping ST-Link SN -> {mcu_uid, device_name, stlink_fw, chipid}

Enumeration uses st-info (open-source stlink) for probe discovery and
OpenOCD for per-probe UID reads. This avoids the STM32_Programmer_CLI
libusb bug that cross-connects SWD targets with multiple ST-Link V3 probes.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from .toolchain import find_st_info, openocd_target_cfg, run_openocd

_executor = ThreadPoolExecutor(max_workers=2)

ENUMERATE_TIMEOUT = 10  # seconds per probe

# Session cache: ST-Link SN -> {mcu_uid, device_name, stlink_fw, chipid}
_probe_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# UID base address (keyed by chipid)
# ---------------------------------------------------------------------------

_CHIPID_TO_UID_ADDR: dict[int, int] = {
    # STM32F0
    0x440: 0x1FFFF7AC, 0x442: 0x1FFFF7AC, 0x444: 0x1FFFF7AC,
    0x445: 0x1FFFF7AC, 0x448: 0x1FFFF7AC,
    # STM32F1
    0x410: 0x1FFFF7E8, 0x412: 0x1FFFF7E8, 0x414: 0x1FFFF7E8,
    0x418: 0x1FFFF7E8, 0x420: 0x1FFFF7E8, 0x428: 0x1FFFF7E8,
    0x430: 0x1FFFF7E8,
    # STM32F2
    0x411: 0x1FFF7A10,
    # STM32F3
    0x422: 0x1FFFF7AC, 0x432: 0x1FFFF7AC, 0x438: 0x1FFFF7AC,
    0x439: 0x1FFFF7AC, 0x446: 0x1FFFF7AC,
    # STM32F4
    0x413: 0x1FFF7A10, 0x419: 0x1FFF7A10, 0x421: 0x1FFF7A10,
    0x423: 0x1FFF7A10, 0x431: 0x1FFF7A10, 0x433: 0x1FFF7A10,
    0x434: 0x1FFF7A10, 0x441: 0x1FFF7A10, 0x458: 0x1FFF7A10,
    # STM32F7
    0x449: 0x1FFF7A10, 0x451: 0x1FFF7A10, 0x452: 0x1FFF7A10,
    # STM32G0
    0x456: 0x1FFF7590, 0x460: 0x1FFF7590, 0x466: 0x1FFF7590,
    0x467: 0x1FFF7590,
    # STM32G4
    0x468: 0x1FFF7590, 0x469: 0x1FFF7590, 0x479: 0x1FFF7590,
    # STM32H7
    0x450: 0x1FF1E800, 0x480: 0x1FF1E800, 0x483: 0x1FF1E800,
    # STM32L0
    0x425: 0x1FF80050, 0x417: 0x1FF80050, 0x447: 0x1FF80050,
    0x457: 0x1FF80050,
    # STM32L1
    0x416: 0x1FF80050, 0x427: 0x1FF80050, 0x429: 0x1FF80050,
    0x436: 0x1FF80050, 0x437: 0x1FF80050,
    # STM32L4
    0x415: 0x1FFF7590, 0x435: 0x1FFF7590, 0x461: 0x1FFF7590,
    0x462: 0x1FFF7590, 0x464: 0x1FFF7590, 0x470: 0x1FFF7590,
    0x471: 0x1FFF7590, 0x472: 0x1FFF7590,
    # STM32L5
    0x472: 0x0BFA0590,
    # STM32WB
    0x494: 0x1FFF7590, 0x495: 0x1FFF7590, 0x496: 0x1FFF7590,
    # STM32WL
    0x497: 0x1FFF7590,
    # STM32C0
    0x443: 0x1FFF7590,
    # STM32U5
    0x455: 0x0BFA0590, 0x476: 0x0BFA0590, 0x481: 0x0BFA0590,
    0x482: 0x0BFA0590,
    # STM32H5
    0x474: 0x08FFF800, 0x478: 0x08FFF800, 0x484: 0x08FFF800,
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _map_path() -> str:
    """Return path to ~/.stm32-mcp/board_map.json, creating dir if needed."""
    dir_path = os.path.expanduser("~/.stm32-mcp")
    os.makedirs(dir_path, exist_ok=True)
    return os.path.join(dir_path, "board_map.json")


def load_map() -> dict:
    """Read board map JSON. Returns empty structure if missing/corrupt."""
    path = _map_path()
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if "boards" not in data:
            data["boards"] = {}
        if "probes" not in data:
            data["probes"] = {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"boards": {}, "probes": {}}


def save_map(data: dict) -> None:
    """Write board map JSON atomically (temp file + rename)."""
    path = _map_path()
    dir_path = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_board_nickname(uid: str) -> str | None:
    """Look up board nickname by MCU UID."""
    data = load_map()
    entry = data["boards"].get(uid)
    return entry["nickname"] if entry else None


def set_board_nickname(uid: str, nickname: str) -> None:
    """Set or remove a board nickname."""
    data = load_map()
    if nickname:
        data["boards"][uid] = {"nickname": nickname}
    else:
        data["boards"].pop(uid, None)
    save_map(data)


def get_probe_nickname(sn: str) -> str | None:
    """Look up probe nickname by ST-Link serial number."""
    data = load_map()
    entry = data["probes"].get(sn)
    return entry["nickname"] if entry else None


def set_probe_nickname(sn: str, nickname: str) -> None:
    """Set or remove a probe nickname."""
    data = load_map()
    if nickname:
        data["probes"][sn] = {"nickname": nickname}
    else:
        data["probes"].pop(sn, None)
    save_map(data)


# ---------------------------------------------------------------------------
# Enumeration (st-info + OpenOCD)
# ---------------------------------------------------------------------------

def _enumerate_probes() -> list[dict]:
    """Enumerate all connected ST-Link probes and their MCU targets.

    1. st-info --probe -> parse all ST-Link SNs, FW versions, chipids
    2. For each probe, use OpenOCD to read UID from memory
    3. Update _probe_cache
    4. Return list of probe info dicts
    """
    global _probe_cache

    st_info = find_st_info()
    if not st_info:
        return []

    # Step 1: List all probes with st-info
    try:
        result = subprocess.run(
            [st_info, "--probe"],
            capture_output=True, text=True, timeout=ENUMERATE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    output = result.stdout + "\n" + result.stderr

    # Parse probe blocks from st-info output. Format:
    #   Found 3 stlink programmers
    #   1.
    #     version:    V3Jxx
    #     serial:     000000000000000000000001
    #     flash:      0 (pagesize: 0)
    #     sram:       0
    #     chipid:     0x494
    probes_raw = []
    # Split by numbered probe headers
    probe_blocks = re.split(r'^\d+\.\s*$', output, flags=re.MULTILINE)
    for block in probe_blocks:
        sn_match = re.search(r'serial:\s+([0-9A-Fa-f]+)', block)
        chipid_match = re.search(r'chipid:\s+(0x[0-9A-Fa-f]+)', block)
        version_match = re.search(r'version:\s+(\S+)', block)

        if sn_match and chipid_match:
            chipid = int(chipid_match.group(1), 16)
            probes_raw.append({
                "stlink_sn": sn_match.group(1),
                "chipid": chipid,
                "stlink_version": version_match.group(1) if version_match else "unknown",
            })

    # Step 2: For each probe, use OpenOCD to read UID
    results = []
    for probe in probes_raw:
        sn = probe["stlink_sn"]
        chipid = probe["chipid"]
        target_cfg = openocd_target_cfg(chipid)

        info = {
            "stlink_sn": sn,
            "stlink_fw": probe["stlink_version"],
            "chipid": chipid,
            "mcu_uid": None,
            "device_name": None,
            "error": None,
        }

        if not target_cfg:
            info["error"] = f"Unknown chipid 0x{chipid:04X} — no OpenOCD target config"
            results.append(info)
            continue

        uid_addr = _CHIPID_TO_UID_ADDR.get(chipid)
        if not uid_addr:
            info["error"] = f"Unknown UID address for chipid 0x{chipid:04X}"
            results.append(info)
            continue

        try:
            ocd_out = run_openocd(
                sn, target_cfg,
                ["init", f"mdw 0x{uid_addr:08X} 3"],
                timeout=ENUMERATE_TIMEOUT,
                chipid=chipid,
            )

            # Parse ST-Link FW from OpenOCD connect output (more detailed than st-info)
            fw_match = re.search(r'STLINK\s+(V\S+)', ocd_out)
            if fw_match:
                info["stlink_fw"] = fw_match.group(1)

            # Build device name from OpenOCD output
            # e.g. "stm32wbx.cpu" -> "STM32WBx", plus CPU core
            target_match = re.search(r'(stm32\w+)\.cpu', ocd_out, re.IGNORECASE)
            cpu_match = re.search(r'(Cortex-M\d\+?)', ocd_out)
            if target_match:
                # Capitalize: "stm32wbx" -> "STM32WBx"
                raw = target_match.group(1)
                name = raw[:5].upper() + raw[5:].upper()
                if cpu_match:
                    info["device_name"] = f"{name} ({cpu_match.group(1)})"
                else:
                    info["device_name"] = name
            elif cpu_match:
                info["device_name"] = f"STM32 ({cpu_match.group(1)})"

            # Parse 3 hex words from mdw output
            # Format: 0x1fff7590: 005d004d 37365004 20303041
            uid_match = re.search(
                r'0x[0-9a-fA-F]+:\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)',
                ocd_out,
            )
            if uid_match:
                w0 = uid_match.group(1).upper()
                w1 = uid_match.group(2).upper()
                w2 = uid_match.group(3).upper()
                info["mcu_uid"] = f"0x{w0}-{w1}-{w2}"

        except subprocess.TimeoutExpired:
            info["error"] = "Timed out reading UID via OpenOCD"
        except FileNotFoundError:
            info["error"] = "OpenOCD not found"
        except OSError as e:
            info["error"] = str(e)

        # Update session cache
        if info["mcu_uid"]:
            _probe_cache[sn] = {
                "mcu_uid": info["mcu_uid"],
                "device_name": info["device_name"],
                "stlink_fw": info["stlink_fw"],
                "chipid": chipid,
            }

        results.append(info)

    return results


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_probe(probe: str) -> str:
    """Resolve a nickname or SN to an ST-Link serial number.

    Case-insensitive matching. Returns "" if probe is empty.

    Priority:
    1. Probe nickname -> direct ST-Link SN (fast, no enumeration)
    2. Board nickname -> MCU UID -> session cache -> ST-Link SN
       (if cache miss, enumerate all probes to find it)
    3. Raw ST-Link SN -> passthrough
    """
    if not probe:
        return ""

    data = load_map()
    probe_lower = probe.lower()

    # 1. Check probe nicknames
    for sn, entry in data["probes"].items():
        if entry.get("nickname", "").lower() == probe_lower:
            return sn

    # 2. Check board nicknames -> need MCU UID -> find in cache
    target_uid = None
    for uid, entry in data["boards"].items():
        if entry.get("nickname", "").lower() == probe_lower:
            target_uid = uid
            break

    if target_uid:
        # Search session cache for a probe connected to this MCU
        for sn, cached in _probe_cache.items():
            if cached.get("mcu_uid") == target_uid:
                return sn

        # Cache miss — enumerate to find it
        _enumerate_probes()
        for sn, cached in _probe_cache.items():
            if cached.get("mcu_uid") == target_uid:
                return sn

    # 3. Passthrough — assume it's a raw ST-Link SN
    return probe


def resolve_probe_full(probe: str) -> tuple[str, str, int]:
    """Resolve a nickname or SN to (ST-Link serial number, OpenOCD target config, chipid).

    Returns (sn, target_cfg, chipid). target_cfg may be "" and chipid 0
    if unknown (caller should enumerate to find it).
    """
    sn = resolve_probe(probe)

    # Try to get target_cfg from cache
    cached = _probe_cache.get(sn)
    if cached and cached.get("chipid"):
        chipid = cached["chipid"]
        target_cfg = openocd_target_cfg(chipid) or ""
        return sn, target_cfg, chipid

    # Cache miss — enumerate probes and retry
    if sn:
        probes = _enumerate_probes()
        cached = _probe_cache.get(sn)
        if cached and cached.get("chipid"):
            chipid = cached["chipid"]
            target_cfg = openocd_target_cfg(chipid) or ""
            return sn, target_cfg, chipid

        # Probe SN was resolved (e.g. from nickname) but enumeration couldn't
        # determine the target config. Check if the probe is physically present
        # but has no target MCU (chipid 0x000).
        for p in probes:
            if p["stlink_sn"] == sn and p["chipid"] == 0:
                # Probe is connected but no target MCU detected
                return sn, "", -1  # sentinel: probe found, no target

    return sn, "", 0


# ---------------------------------------------------------------------------
# Cache helpers (for serial_tools enrichment)
# ---------------------------------------------------------------------------

def get_board_nickname_for_probe_sn(sn: str) -> str | None:
    """Look up board nickname via session cache. Returns None if cache cold."""
    cached = _probe_cache.get(sn)
    if not cached or not cached.get("mcu_uid"):
        return None
    return get_board_nickname(cached["mcu_uid"])


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

def _do_list_probes() -> str:
    """Synchronous probe listing."""
    probes = _enumerate_probes()
    if not probes:
        return "No ST-Link probes detected. Check USB connections."

    data = load_map()
    lines = ["Connected boards:", ""]

    for i, p in enumerate(probes, 1):
        sn = p["stlink_sn"]
        uid = p.get("mcu_uid")
        device = p.get("device_name") or "unknown"
        fw = p.get("stlink_fw") or "unknown"
        error = p.get("error")

        # Board nickname
        board_nick = None
        if uid:
            board_entry = data["boards"].get(uid)
            if board_entry:
                board_nick = board_entry["nickname"]

        # Probe nickname
        probe_entry = data["probes"].get(sn)
        probe_nick = probe_entry["nickname"] if probe_entry else None

        # Format board line
        if board_nick:
            board_str = f'"{board_nick}"'
        else:
            board_str = "[unnamed board]"

        if uid:
            board_str += f"  MCU={device}  UID={uid}"
        elif error:
            board_str += f"  ({error})"

        # Format probe line
        if probe_nick:
            probe_str = f'probe "{probe_nick}"'
        else:
            probe_str = "[unnamed probe]"

        probe_str += f" (ST-Link SN={sn}, FW {fw})"

        lines.append(f"{i}. {board_str}")
        lines.append(f"   via {probe_str}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _do_set_nickname(nickname: str, mcu_uid: str, stlink_sn: str) -> str:
    """Synchronous nickname setter."""
    if mcu_uid and stlink_sn:
        return "ERROR: Provide exactly one of mcu_uid or stlink_sn, not both."
    if not mcu_uid and not stlink_sn:
        return "ERROR: Provide either mcu_uid or stlink_sn."

    if mcu_uid:
        if nickname:
            set_board_nickname(mcu_uid, nickname)
            return f'Board {mcu_uid} nicknamed "{nickname}".'
        else:
            set_board_nickname(mcu_uid, "")
            return f"Board {mcu_uid} nickname removed."

    if stlink_sn:
        if nickname:
            set_probe_nickname(stlink_sn, nickname)
            return f'Probe {stlink_sn} nicknamed "{nickname}".'
        else:
            set_probe_nickname(stlink_sn, "")
            return f"Probe {stlink_sn} nickname removed."

    return "ERROR: Unexpected state."


async def stm32_list_probes() -> str:
    """Show all connected ST-Link probes and boards with nicknames.

    Enumerates all connected ST-Link probes, reads MCU info from each,
    and enriches the output with board and probe nicknames from the
    persistent board map (~/.stm32-mcp/board_map.json).

    Returns:
        Formatted list of connected boards and probes with nicknames,
        MCU device names, UIDs, and ST-Link serial numbers.
    """
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_executor, _do_list_probes),
        timeout=ENUMERATE_TIMEOUT * 10,
    )


async def stm32_set_nickname(
    nickname: str,
    mcu_uid: str = "",
    stlink_sn: str = "",
) -> str:
    """Name a board (by MCU UID) or probe (by ST-Link serial number).

    Board nicknames follow the physical MCU — they persist across probe
    swaps. Probe nicknames follow the ST-Link — label your probes
    physically (colored dots, tape) to match.

    Exactly one of mcu_uid or stlink_sn must be provided.
    If nickname is empty, removes the mapping.

    Args:
        nickname: Human-readable name (e.g., "blinky", "sensor-board").
        mcu_uid: MCU unique device ID (for board nicknames).
        stlink_sn: ST-Link serial number (for probe nicknames).

    Returns:
        Confirmation message.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, lambda: _do_set_nickname(nickname, mcu_uid, stlink_sn)
    )
