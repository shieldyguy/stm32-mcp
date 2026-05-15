"""stm32-mcp server — entry point and tool registration."""

import logging

from mcp.server.fastmcp import FastMCP

from .board_map import stm32_list_probes, stm32_set_nickname
from .serial_bridge import start_bridge
from .build import stm32_build, stm32_build_and_flash
from .debug_tools import stm32_read_memory, stm32_write_memory
from .flash import stm32_board_info, stm32_flash
from .fus import stm32_ble_stack_install, stm32_fus_bootstrap, stm32_fus_upgrade
from .serial_tools import (
    serial_connect,
    serial_disconnect,
    serial_list_ports,
    serial_read,
    serial_send,
)
from .serial_sequence import serial_sequence
from .live_memory import live_memory_start, live_memory_stop, live_memory_read

INSTRUCTIONS = """\
stm32-mcp: Build, flash, and communicate with STM32 hardware.

## Available Tools

- stm32_build          — Compile firmware using CubeIDE headless builder
- stm32_flash          — Flash .elf/.bin/.hex to board via ST-Link SWD
- stm32_build_and_flash — Build + flash in one step (use this most of the time)
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
- live_memory_start    — Start continuous background memory monitoring via SWD
- live_memory_stop     — Stop a live memory session
- live_memory_read     — Read recent entries from a live memory session
- stm32_fus_upgrade    — Upgrade FUS on a bare STM32WB board (step 1 of bringup)
- stm32_ble_stack_install — Install BLE stack on STM32WB (step 2 of bringup)
- stm32_fus_bootstrap  — One-shot: FUS upgrade + BLE stack full install

## Typical Workflow

1. Edit source files (Core/Src/*.c, Core/Inc/*.h)
2. stm32_build_and_flash(project_path="/path/to/project") — build + flash
3. serial_connect(port="/dev/cu.usbmodemXXXX") — open VCP
4. serial_send(connection_id="...", data="PING") — test firmware
5. serial_disconnect(connection_id="...") — clean up

## Rules

- **ARM toolchain** (arm-none-eabi-nm, arm-none-eabi-gdb) must be on PATH.
  These come from the CubeIDE-bundled toolchain directory added to ~/.zshrc.
  If symbol resolution or struct expansion fails with "not found", CubeIDE was
  likely updated and the versioned plugin path in ~/.zshrc needs updating.
  Look for the new path under: /Applications/STM32CubeIDE.app/Contents/Eclipse/
  plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*/tools/bin/
  **Tell the user to update their PATH in ~/.zshrc if this happens.**
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

# Register all 17 tools
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
mcp.tool()(live_memory_start)
mcp.tool()(live_memory_stop)
mcp.tool()(live_memory_read)
mcp.tool()(stm32_fus_upgrade)
mcp.tool()(stm32_ble_stack_install)
mcp.tool()(stm32_fus_bootstrap)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    start_bridge()  # daemon thread — TCP bridge on localhost:8765
    mcp.run()


if __name__ == "__main__":
    main()
