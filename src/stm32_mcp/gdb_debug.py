"""Persistent arm-none-eabi-gdb sessions for st-util debug servers."""

from __future__ import annotations

import atexit
import asyncio
import collections
import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .toolchain import find_gdb

_executor = ThreadPoolExecutor(max_workers=2)
GDB_TIMEOUT = 30
OUTPUT_LINES = 500


@dataclass
class _GdbSession:
    session_id: str
    process: subprocess.Popen
    port: int
    elf_path: str
    started_at: float = field(default_factory=time.monotonic)
    next_token: int = 1
    output: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=OUTPUT_LINES)
    )
    incoming: queue.Queue[str] = field(default_factory=queue.Queue)
    command_lock: threading.Lock = field(default_factory=threading.Lock)
    output_lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None


_sessions: dict[str, _GdbSession] = {}
_sessions_lock = threading.Lock()


def _clean_value(value: str, name: str, required: bool = False) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if required and not clean:
        raise ValueError(f"{name} is required.")
    if "\x00" in clean or "\n" in clean or "\r" in clean:
        raise ValueError(f"{name} must be a single line without NUL bytes.")
    return clean


def _validate_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535.")
    return port


def _decode_mi_line(line: str) -> str:
    if len(line) > 1 and line[0] in "~@&" and line[1] == '"':
        try:
            return json.loads(line[1:])
        except json.JSONDecodeError:
            return line
    return line


def _record_output(session: _GdbSession, line: str) -> None:
    with session.output_lock:
        session.output.append(line)


def _read_output(session: _GdbSession) -> None:
    stream = session.process.stdout
    if stream is None:
        return
    try:
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            _record_output(session, _decode_mi_line(line))
            session.incoming.put(line)
    except (OSError, ValueError):
        pass


def _send_mi(
    session: _GdbSession,
    command: str,
    timeout: int = GDB_TIMEOUT,
    wait_for_stop: bool = False,
) -> str:
    if not session.command_lock.acquire(blocking=False):
        return "ERROR: This GDB session already has a command running."
    try:
        if session.process.poll() is not None:
            return f"ERROR: GDB exited with code {session.process.returncode}."
        stdin = session.process.stdin
        if stdin is None:
            return "ERROR: GDB stdin is unavailable."

        token = session.next_token
        session.next_token += 1
        stdin.write(f"{token}{command}\n")
        stdin.flush()

        lines: list[str] = []
        deadline = time.monotonic() + timeout
        result_prefix = f"{token}^"
        running = False
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = session.incoming.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if session.process.poll() is not None:
                    return f"ERROR: GDB exited with code {session.process.returncode}."
                continue
            if line == "(gdb)":
                continue
            decoded = _decode_mi_line(line)
            if decoded:
                lines.append(decoded)
            if line.startswith(result_prefix):
                if line.startswith(f"{token}^error"):
                    return "ERROR: GDB command failed.\n" + "\n".join(lines)
                if wait_for_stop and line.startswith(f"{token}^running"):
                    running = True
                    continue
                return "\n".join(lines).strip() or "GDB command completed."
            if running and line.startswith("*stopped"):
                return "\n".join(lines).strip() or "GDB target stopped."

        return f"ERROR: GDB command timed out after {timeout}s."
    except (BrokenPipeError, OSError) as exc:
        return f"ERROR: Could not communicate with GDB: {exc}"
    finally:
        session.command_lock.release()


def _terminate(session: _GdbSession) -> None:
    if session.process.poll() is None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
            else:
                session.process.terminate()
            session.process.wait(timeout=3)
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


def _start_sync(
    port: int,
    elf_path: str,
    connect: bool,
    timeout: int,
) -> str:
    try:
        port = _validate_port(port)
        elf_path = _clean_value(elf_path, "ELF path")
        if elf_path and not os.path.isfile(elf_path):
            raise ValueError(f"ELF file not found: {elf_path}")
    except ValueError as exc:
        return f"ERROR: {exc}"

    gdb = find_gdb()
    if not gdb:
        return (
            "ERROR: arm-none-eabi-gdb not found. Set STM32_ARM_TOOLCHAIN_BIN "
            "to the CubeIDE tools/bin directory or add it to PATH."
        )

    arguments = [gdb, "--nx", "--nh", "--interpreter=mi2"]
    if elf_path:
        arguments.append(elf_path)
    popen_kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "shell": False,
        "text": True,
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(arguments, **popen_kwargs)
    except OSError as exc:
        return f"ERROR: Could not start arm-none-eabi-gdb: {exc}"

    session = _GdbSession(
        session_id=uuid.uuid4().hex[:8],
        process=process,
        port=port,
        elf_path=elf_path,
    )
    reader = threading.Thread(
        target=_read_output,
        args=(session,),
        daemon=True,
        name=f"stlink-gdb-{session.session_id}",
    )
    session.reader = reader
    with _sessions_lock:
        _sessions[session.session_id] = session
    reader.start()

    result = _send_mi(session, "-gdb-set pagination off", timeout=5)
    if result.startswith("ERROR:"):
        _stop_sync(session.session_id)
        return result
    if connect:
        result = _send_mi(
            session,
            f"-target-select extended-remote localhost:{port}",
            timeout=timeout,
        )
        if result.startswith("ERROR:"):
            _stop_sync(session.session_id)
            return result

    result_text = (
        "Persistent GDB session started.\n"
        f"Session ID: {session.session_id}\n"
        f"Endpoint: localhost:{port}\n"
        "Use stlink_gdb_command for breakpoints, stepping, variables, and registers."
    )
    if elf_path:
        result_text += f"\nELF: {elf_path}"
    if not connect:
        result_text += "\nTarget connection: not connected"
    return result_text


def _session(session_id: str) -> _GdbSession | None:
    with _sessions_lock:
        return _sessions.get(session_id)


def _stop_sync(session_id: str) -> str:
    session = _session(session_id)
    if session is None:
        return f"ERROR: GDB session {session_id!r} not found."
    _terminate(session)
    with _sessions_lock:
        _sessions.pop(session_id, None)
    return f"GDB session {session_id} stopped."


def _status_sync(session_id: str, last_n: int) -> str:
    session = _session(session_id)
    if session is None:
        return f"ERROR: GDB session {session_id!r} not found."
    returncode = session.process.poll()
    state = "running" if returncode is None else f"exited ({returncode})"
    with session.output_lock:
        output = list(session.output)[-max(1, min(last_n, OUTPUT_LINES)):]
    result = (
        f"GDB session {session.session_id}\n"
        f"State: {state}\n"
        f"Endpoint: localhost:{session.port}\n"
        f"Uptime: {time.monotonic() - session.started_at:.1f}s"
    )
    if output:
        result += "\nOutput:\n" + "\n".join(output)
    return result


def _shutdown() -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        _terminate(session)


atexit.register(_shutdown)


async def stlink_gdb_start(
    port: int = 4242,
    elf_path: str = "",
    connect: bool = True,
    timeout: int = GDB_TIMEOUT,
) -> str:
    """Start a persistent arm-none-eabi-gdb MI client.

    Start stlink_debug_start first so ``port`` has an st-util server listening.
    The GDB process remains connected between calls, preserving breakpoints,
    watchpoints, selected frames, and target state.
    """
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_executor, _start_sync, port, elf_path, connect, max(1, timeout)),
        timeout=max(1, timeout) + 5,
    )


async def stlink_gdb_command(
    session_id: str,
    command: str,
    timeout: int = GDB_TIMEOUT,
) -> str:
    """Execute one GDB console command in a persistent session."""
    try:
        command = _clean_value(command, "command", required=True)
    except ValueError as exc:
        return f"ERROR: {exc}"
    session = _session(session_id)
    if session is None:
        return f"ERROR: GDB session {session_id!r} not found."
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            _send_mi,
            session,
            f"-interpreter-exec console {json.dumps(command)}",
            max(1, timeout),
            command.split(None, 1)[0].lower()
            in {"continue", "step", "stepi", "next", "nexti", "finish", "until"},
        ),
        timeout=max(1, timeout) + 5,
    )


async def stlink_gdb_status(session_id: str, last_n: int = 50) -> str:
    """Return persistent GDB session state and recent MI output."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _status_sync, session_id, last_n)


async def stlink_gdb_stop(session_id: str) -> str:
    """Stop a persistent GDB session."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _stop_sync, session_id)


def shutdown_gdb_sessions() -> None:
    """Stop all persistent GDB clients during MCP shutdown."""
    _shutdown()
