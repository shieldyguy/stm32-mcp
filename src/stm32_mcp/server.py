"""stm32-mcp server — entry point and tool registration."""

import logging
import signal

from mcp.server.fastmcp import FastMCP

from .board_map import stm32_list_probes, stm32_set_nickname
from .serial_bridge import start_bridge
from .build import stm32_build, stm32_build_and_flash
from .debug_tools import stm32_read_memory, stm32_write_memory
from .elf_tools import stm32_disassemble, stm32_readelf
from .flash import stm32_board_info, stm32_flash
from .fus import stm32_ble_stack_install, stm32_fus_bootstrap, stm32_fus_upgrade
from .gdb_debug import (
    shutdown_gdb_sessions,
    stlink_gdb_command,
    stlink_gdb_start,
    stlink_gdb_status,
    stlink_gdb_stop,
)
from .serial_tools import (
    serial_connect,
    serial_disconnect,
    serial_list_ports,
    serial_read,
    serial_send,
)
from .serial_sequence import serial_sequence
from .live_memory import live_memory_start, live_memory_stop, live_memory_read
from .stlink import (
    stlink_command,
    stlink_debug_command,
    stlink_debug_action,
    stlink_debug_start,
    stlink_debug_status,
    stlink_debug_stop,
    stlink_flash,
    stlink_info,
    stlink_server_start,
    stlink_server_status,
    stlink_server_stop,
    stlink_session_list,
    stlink_trace_read,
    stlink_trace_start,
    stlink_trace_status,
    stlink_trace_stop,
    shutdown_stlink_sessions,
)

INSTRUCTIONS = """\
stm32-mcp: Build, flash, and communicate with STM32 hardware.

## Available Tools

- stm32_build          — Compile firmware using CubeIDE headless builder
- stm32_flash          — Flash .elf/.bin/.hex via stlink/st-flash by default
- stm32_build_and_flash — Build + stlink/st-flash in one step (preferred workflow)
- stm32_board_info     — Read ST-Link and MCU info (device ID, flash size, voltage)
- stm32_list_probes    — Show all connected boards with nicknames and MCU IDs
- stm32_set_nickname   — Name a board (by MCU UID) or probe (by ST-Link SN)
- serial_list_ports    — List serial ports (marks ST-Link VCP ports)
- serial_connect       — Open a serial connection
- serial_send          — Send data and read response
- serial_read          — Read buffered serial data
- serial_disconnect    — Close a serial connection
- serial_sequence     — Run multi-step serial+SWD-memory sequences in one call (timing-sensitive tests)
- stm32_read_memory    — Read memory by address or variable name (from ELF symbols)
- stm32_write_memory   — Write memory by address or variable name
- stm32_readelf        — Inspect ELF headers, sections, symbols, and DWARF data
- stm32_disassemble    — Disassemble ARM firmware with source and symbol context
- live_memory_start    — Start continuous background memory monitoring via SWD
- live_memory_stop     — Stop a live memory session
- live_memory_read     — Read recent entries from a live memory session
- stlink_info          — Query the installed st-info tool
- stlink_flash         — Read, write, erase, or reset with st-flash
- stlink_command       — Run an installed stlink command with explicit args
- stlink_debug_start   — Start an st-util GDB server
- stlink_debug_command — Execute GDB commands against an st-util session
- stlink_debug_action  — Typed breakpoint, step, variable, register, and memory actions
- stlink_gdb_start      — Start a persistent arm-none-eabi-gdb client
- stlink_gdb_command    — Execute one command in a persistent GDB session
- stlink_gdb_status     — Read persistent GDB session state
- stlink_gdb_stop       — Stop a persistent GDB client
- stlink_debug_status  — Read st-util session state and output
- stlink_debug_stop    — Stop an st-util GDB server
- stlink_trace_start   — Start the st-trace SWO reader
- stlink_trace_read    — Read buffered st-trace output
- stlink_trace_status  — Read st-trace session state
- stlink_trace_stop    — Stop st-trace
- stlink_server_start  — Start the optional st-server TCP proxy
- stlink_server_status — Read st-server session state
- stlink_server_stop   — Stop st-server
- stlink_session_list  — List managed stlink sessions
- stm32_fus_upgrade    — Upgrade FUS on a bare STM32WB board (step 1 of bringup)
- stm32_ble_stack_install — Install BLE stack on STM32WB (step 2 of bringup)
- stm32_fus_bootstrap  — One-shot: FUS upgrade + BLE stack full install

## Typical Workflow

1. Edit source files (Core/Src/*.c, Core/Inc/*.h)
2. stm32_build_and_flash(project_path="/path/to/project") — build + stlink flash
3. serial_connect(port="/dev/cu.usbmodemXXXX") — open VCP
4. serial_send(connection_id="...", data="PING") — test firmware
5. serial_disconnect(connection_id="...") — clean up

## Rules

- **ARM toolchain** (arm-none-eabi-nm, arm-none-eabi-gdb,
  arm-none-eabi-readelf, arm-none-eabi-objdump) is discovered from
  `$STM32_ARM_TOOLCHAIN_BIN`, the known STM32CubeIDE bundle, or PATH.
  If CubeIDE is installed elsewhere, set `$STM32_ARM_TOOLCHAIN_BIN` to its
  `tools/bin` directory.
- project_path must point to a directory containing .project and .cproject files
- Never edit files in Debug/, Release/, Drivers/, or .cproject
- New .c/.h files are automatically detected by the headless builder
- Always build before flashing
- Always verify behavior over serial after flashing
- Serial default: 115200 baud, LF line endings

## Multi-Board Setup

- stm32_list_probes    — Show all connected boards with nicknames and MCU IDs
- stm32_set_nickname   — Name a board (by MCU UID) or probe (by ST-Link SN)
- Use the probe parameter on stm32_flash, stm32_build_and_flash, and
  stm32_board_info to target by board nickname, probe nickname, or ST-Link SN
- Board nicknames follow the MCU — probe swaps don't affect them
- Probe nicknames follow the ST-Link — label your probes physically

## Debug Tools

- stm32_read_memory   — Read memory by address or variable name (from ELF symbols)
- stm32_write_memory  — Write memory by address or variable name
- Use symbol param with elf_path to read/write by name instead of hex address
- Width auto-detected from ELF symbol size when using symbol names

## Direct stlink Tools

- stlink tools are discovered on PATH and are never installed by this server
- stlink_flash exposes st-flash read/write/erase/reset operations
- stm32_flash and stm32_build_and_flash use backend="stlink" by default;
  pass backend="openocd" only when the OpenOCD backend is explicitly needed
- stlink_debug_start starts st-util; use stlink_debug_command with newline-separated
  GDB commands such as `break main`, `continue`, `info registers`, `step`, or
  `monitor reset`
- stlink_gdb_start/command/status/stop provides persistent GDB state across calls
- stlink_trace_start/read/stop manages the long-running st-trace process
- stlink_server_start/stop manages the optional st-server process; non-loopback
  binding requires explicit allow_non_loopback=true
- Stop live_memory sessions and other OpenOCD users before using stlink on the
  same probe
- stlink_command is an argument-list escape hatch for installed stlink options;
  it never invokes a shell
- arm-none-eabi-gdb, readelf, and objdump are discovered from
  $STM32_ARM_TOOLCHAIN_BIN, the bundled CubeIDE toolchain, or PATH

## GDB Debugging Workflow

1. Stop live_memory/OpenOCD sessions using the same probe.
2. Start the stlink GDB server:
   `stlink_debug_start(probe="...", port=4242, multi=true, elf_path="...")`
3. Start the persistent ARM GDB client:
   `stlink_gdb_start(port=4242, elf_path="...")`
4. Use the returned GDB session ID with `stlink_gdb_command`, for example:
   `break main`, `continue`, `info locals`, `info registers`, `step`,
   `x/4wx 0x20000000`, `print variable`, or `monitor reset`.
5. Stop the GDB client with `stlink_gdb_stop`, then stop the server with
   `stlink_debug_stop`.

- The ELF should contain debug information (`-g`) for source breakpoints,
  local variables, and source-level stepping.
- `stlink_debug_action` provides typed equivalents. Pass `gdb_session_id` to
  keep breakpoint and target state persistent; omit it to use the batch server
  session.
- Use `stlink_debug_command` for a one-call multi-command sequence when a
  persistent GDB session is not needed.
- `stlink_gdb_start` must use the same port returned by stlink_debug_start.

## ELF Inspection Workflow

- `stm32_readelf(elf_path="...", mode="headers|sections|symbols|segments|dynamic|notes|debug_info|debug_line|all")`
- `stm32_disassemble(elf_path="...", function="main")`
- Use `start_address` and `stop_address` for an address range, or `source=true`
  to include source context.
- Use `STM32_ARM_TOOLCHAIN_BIN` when the ARM tools are not in the bundled
  CubeIDE path. The MCP auto-discovers CubeIDE installations on Linux
  (`/opt/st/stm32cubeide_*/.../tools/bin`) and macOS
  (`/Applications/STM32CubeIDE.app/.../tools/bin`).

## Hardware Sequences (serial_sequence)

- serial_sequence runs multiple send/delay/capture/mem_write/mem_read steps in one tool call with real timing
- Serial step:     {"send": "CMD", "to": "/dev/cu.usbmodemXXXX"}
- Delay step:      {"delay_ms": 500}
- Capture step:    {"capture": true, "label": "name"}   # saves PNG to /tmp/stm32-captures/
- Mem write step:  {"mem_write": true, "address": "0x48000418", "value": "0x40", "probe": "yellow"}
- Mem read step:   {"mem_read": true, "address": "0x48000400", "count": 2, "probe": "yellow", "label": "pre"}
- Mem steps accept "symbol" + "elf_path" instead of "address" to read/write by name
- "probe" accepts board nickname, probe nickname, or ST-Link SN
- "width" is 8/16/32, defaults to 32 (auto-detected from symbol size)
- Optional on send: "expect" (substring match), "read_timeout", "line_ending"
- on_failure: "continue" (default) or "stop"
- filter_responses: true to match expect only against >-prefixed VCP lines
- Timing note: each mem op still launches OpenOCD (~tens of ms overhead), so
  very tight memory-to-memory timing is approximate. Delays themselves are accurate.

## Live Memory Monitoring

- live_memory_start opens a persistent OpenOCD connection and polls variables via SWD
- live_memory_read returns recent values from an in-memory ring buffer
- live_memory_stop kills OpenOCD and returns session stats
- Only one session per probe — stop before flashing or using stm32_read/write_memory
- Output is JSONL at the specified path (default /tmp/live_memory_<id>.jsonl)
- **Struct auto-expansion**: Pass a struct variable name (e.g. `"blink"`) and all
  fields are automatically expanded with dotted names (e.g. `blink.state`,
  `blink.prev_output.changed`). Nested structs are recursively expanded.
  Uses GDB DWARF info — works with `-fshort-enums`, padding, etc.
  To monitor a struct as raw bytes instead, use `{"symbol": "blink", "expand": false}`.
- Sessions auto-reconnect if ST-Link connection drops (up to 3 retries with backoff)

## STM32WB FUS / BLE Stack (first-time bringup)

- stm32_fus_bootstrap(probe="...") runs FUS upgrade then BLE stack full install
- stm32_fus_upgrade and stm32_ble_stack_install are the individual steps
- Only STM32WB1x (chipid 0x494) is wired — other WB chipids error out
- Wireless binaries folder is resolved from wireless_dir arg, then
  $STM32_WIRELESS_BINARIES, then ~/stm32cube-wb/Projects/STM32WB_Copro_Wireless_Binaries
- FUS ops wipe flash sectors [0..3], so always re-flash user firmware with
  stm32_build_and_flash afterward
- Same functions are reachable from a shell via `stm32-fus {upgrade,stack,bootstrap}`
"""

mcp = FastMCP("stm32-mcp", instructions=INSTRUCTIONS)

# Register all MCP tools
mcp.tool()(stm32_build)
mcp.tool()(stm32_build_and_flash)
mcp.tool()(stm32_flash)
mcp.tool()(stm32_board_info)
mcp.tool()(stm32_list_probes)
mcp.tool()(stm32_set_nickname)
mcp.tool()(serial_list_ports)
mcp.tool()(serial_connect)
mcp.tool()(serial_send)
mcp.tool()(serial_read)
mcp.tool()(serial_disconnect)
mcp.tool()(serial_sequence)
mcp.tool()(stm32_read_memory)
mcp.tool()(stm32_write_memory)
mcp.tool()(stm32_readelf)
mcp.tool()(stm32_disassemble)
mcp.tool()(live_memory_start)
mcp.tool()(live_memory_stop)
mcp.tool()(live_memory_read)
mcp.tool()(stlink_info)
mcp.tool()(stlink_flash)
mcp.tool()(stlink_command)
mcp.tool()(stlink_debug_start)
mcp.tool()(stlink_debug_command)
mcp.tool()(stlink_debug_action)
mcp.tool()(stlink_gdb_start)
mcp.tool()(stlink_gdb_command)
mcp.tool()(stlink_gdb_status)
mcp.tool()(stlink_gdb_stop)
mcp.tool()(stlink_debug_status)
mcp.tool()(stlink_debug_stop)
mcp.tool()(stlink_trace_start)
mcp.tool()(stlink_trace_read)
mcp.tool()(stlink_trace_status)
mcp.tool()(stlink_trace_stop)
mcp.tool()(stlink_server_start)
mcp.tool()(stlink_server_status)
mcp.tool()(stlink_server_stop)
mcp.tool()(stlink_session_list)
mcp.tool()(stm32_fus_upgrade)
mcp.tool()(stm32_ble_stack_install)
mcp.tool()(stm32_fus_bootstrap)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    start_bridge()  # daemon thread — TCP bridge on localhost:8765
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum, _frame):
        shutdown_gdb_sessions()
        shutdown_stlink_sessions()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        mcp.run()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        shutdown_gdb_sessions()
        shutdown_stlink_sessions()


if __name__ == "__main__":
    main()
