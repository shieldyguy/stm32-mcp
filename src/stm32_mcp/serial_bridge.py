"""TCP bridge — lets a standalone terminal share the MCP serial connection."""

import asyncio
import logging
import threading

import serial.tools.list_ports

from .board_map import (
    get_board_nickname_for_probe_sn,
    get_probe_nickname,
    load_map,
    _probe_cache,
)
from .serial_tools import (
    ST_VID,
    ST_PIDS,
    _connections,
    _do_connect,
    _do_send,
    _do_list_ports,
)

log = logging.getLogger(__name__)

# One lock per connection_id prevents interleaved MCP + bridge sends.
_send_locks: dict[str, threading.Lock] = {}

DEFAULT_PORT = 8765

HELP_TEXT = """\
Bridge commands:
  help                  Show this help
  ports                 List available serial ports (with nicknames)
  connect <port|nick>   Connect by port path or board/probe nickname
  status                Show active connection
  quit / exit           Disconnect

Anything else is sent as a VCP command to the active serial connection.
Examples: PING, GET_BLINK_STATE, SET_TURN_ON"""


def _get_send_lock(connection_id: str) -> threading.Lock:
    if connection_id not in _send_locks:
        _send_locks[connection_id] = threading.Lock()
    return _send_locks[connection_id]


def _first_connection_id() -> str | None:
    """Return the first open connection_id, or None."""
    for cid, ser in _connections.items():
        if ser.is_open:
            return cid
    return None


def _port_nickname(port_path: str) -> str | None:
    """Return a human-friendly label for a port, e.g. 'dev myboard via probe1'."""
    # Find the pyserial port info for this path
    for port in serial.tools.list_ports.comports():
        if port.device == port_path and port.serial_number:
            parts = []
            board_nick = get_board_nickname_for_probe_sn(port.serial_number)
            probe_nick = get_probe_nickname(port.serial_number)
            if board_nick:
                parts.append(f'"{board_nick}"')
            if probe_nick:
                parts.append(f'via "{probe_nick}"')
            return " ".join(parts) if parts else None
    return None


def _resolve_nickname_to_port(name: str) -> str | None:
    """Resolve a board or probe nickname to a serial port path.

    Checks board nicknames first (via board_map → MCU UID → probe cache → ST-Link SN → port),
    then probe nicknames (via board_map → ST-Link SN → port).
    Case-insensitive.
    """
    name_lower = name.lower()
    data = load_map()

    # Collect ST-Link VCP ports keyed by serial number
    stlink_ports: dict[str, str] = {}
    for port in serial.tools.list_ports.comports():
        if port.vid == ST_VID and port.pid in ST_PIDS and port.serial_number:
            stlink_ports[port.serial_number] = port.device

    # 1. Board nickname → MCU UID → probe cache → ST-Link SN → port
    for uid, entry in data["boards"].items():
        if entry.get("nickname", "").lower() == name_lower:
            # Find which probe SN is connected to this MCU UID
            for sn, cached in _probe_cache.items():
                if cached.get("mcu_uid") == uid and sn in stlink_ports:
                    return stlink_ports[sn]

    # 2. Probe nickname → ST-Link SN → port
    for sn, entry in data["probes"].items():
        if entry.get("nickname", "").lower() == name_lower and sn in stlink_ports:
            return stlink_ports[sn]

    return None


def _auto_connect() -> str | None:
    """Connect to the first ST-Link VCP port found. Returns connection_id or None."""
    for port in sorted(serial.tools.list_ports.comports(), key=lambda p: p.device):
        if port.vid == ST_VID and port.pid in ST_PIDS:
            result = _do_connect(port.device, 115200)
            if not result.startswith("ERROR"):
                return port.device
    return None


def _connection_label(cid: str) -> str:
    """Format a connection_id with its nickname if available."""
    nick = _port_nickname(cid)
    if nick:
        return f"{cid} ({nick})"
    return cid


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    log.info("Bridge client connected: %s", peer)

    # Welcome banner
    cid = _first_connection_id()
    if cid is None:
        cid = _auto_connect()
        if cid:
            banner = f"Auto-connected: {_connection_label(cid)}\n"
        else:
            banner = "No serial connections open. Use 'connect <port>' or 'connect <nickname>'.\n"
    else:
        banner = f"Active connection: {_connection_label(cid)}\n"

    banner += "Type 'help' for commands.\n> "
    writer.write(banner.encode())
    await writer.drain()

    try:
        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                writer.write(b"\n> ")
                await writer.drain()
                continue

            cmd = line.lower()

            # Special commands
            if cmd in ("quit", "exit"):
                writer.write(b"Bye.\n")
                await writer.drain()
                break

            if cmd == "help":
                writer.write(f"{HELP_TEXT}\n> ".encode())
                await writer.drain()
                continue

            if cmd == "ports":
                result = _do_list_ports()
                writer.write(f"{result}\n> ".encode())
                await writer.drain()
                continue

            if cmd == "status":
                active = cid or _first_connection_id()
                if active:
                    msg = f"Connected: {_connection_label(active)}"
                else:
                    msg = "No active connection."
                writer.write(f"{msg}\n> ".encode())
                await writer.drain()
                continue

            if cmd.startswith("connect "):
                target = line.split(None, 1)[1].strip("\"'")

                # Try nickname resolution first
                resolved = _resolve_nickname_to_port(target)
                if resolved:
                    port_path = resolved
                else:
                    port_path = target  # assume raw port path

                result = _do_connect(port_path, 115200)
                if not result.startswith("ERROR"):
                    cid = port_path
                    result = f"{result} ({_port_nickname(port_path) or port_path})"
                writer.write(f"{result}\n> ".encode())
                await writer.drain()
                continue

            # Send VCP command
            active = cid or _first_connection_id()
            if active is None:
                writer.write(b"No serial connection. Use 'connect <port>' or 'connect <nickname>'.\n> ")
                await writer.drain()
                continue

            lock = _get_send_lock(active)
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: _locked_send(lock, active, line),
                )
            except Exception as e:
                writer.write(f"ERROR: {e}\n> ".encode())
                await writer.drain()
                continue

            # Extract just the response text (skip the "Sent: ..." line)
            response_lines = result.split("\n")
            output = "\n".join(
                l.removeprefix("Response: ") for l in response_lines if l.startswith("Response:")
            )
            if not output:
                output = result  # fallback: show everything

            writer.write(f"{output}\n> ".encode())
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        log.exception("Bridge handler crashed")
        try:
            writer.write(f"\nBRIDGE CRASH: {type(e).__name__}: {e}\n".encode())
            await writer.drain()
        except Exception:
            pass
    finally:
        log.info("Bridge client disconnected: %s", peer)
        writer.close()


def _locked_send(lock: threading.Lock, connection_id: str, data: str) -> str:
    with lock:
        return _do_send(connection_id, data, read_response=True, read_timeout=2.0, line_ending="lf")


async def _serve(port: int):
    server = await asyncio.start_server(_handle_client, "127.0.0.1", port)
    addr = server.sockets[0].getsockname()
    log.info("Serial bridge listening on %s:%d", addr[0], addr[1])
    async with server:
        await server.serve_forever()


def start_bridge(port: int = DEFAULT_PORT):
    """Start the TCP bridge in a daemon thread. Non-blocking."""
    def _run():
        try:
            asyncio.run(_serve(port))
        except Exception:
            log.exception("Serial bridge crashed")

    t = threading.Thread(target=_run, daemon=True, name="serial-bridge")
    t.start()
    log.info("Serial bridge thread started (port %d)", port)
