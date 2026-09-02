# stm32-mcp

MCP server that lets Claude Code build, flash, and communicate with STM32 hardware.

stm32-mcp is pretty specific to how I tend to approach hardware development, but it is likely useful to others, too! It could be massaged to fit lots of workflows, but this is laser focused on mine (stlink-v3 mini, VCP on that header, STM32 microcontroller). 

You can do things like:

> **me:** hey who is plugged in right now?
>
> **claude:** two unnamed probes connected to two unnamed PCBs
>
> **me:** k ask them who they are and give them a nickname based on their response 
>
> **claude:** got it, do you want to nickname the probes too? your boards are 'doorbell A' and 'synthesizer B'
>
> **me:** yep, I put paint marker on those probes. call doorbell's 'blue' and the synth's 'red'
>
> **claude:** done. what's next?
>
> **me:** give them both VCP commands so they can talk to each other, then have the doorbell ask the synth on a date
>
> **claude:** *thinking...* done, synth declined. plenty of fish in the sea, doorbell!

[MCP (Model Context Protocol)](https://modelcontextprotocol.io) is an open standard that lets AI assistants like Claude use external tools. This server gives Claude the ability to compile your firmware, flash it to a board, talk to it over serial, and read memory via SWD. It is flexible and conversational. 

> [!WARNING]
> This server gives an AI direct access to your compiler, debug probe, and serial ports. It can flash firmware, overwrite memory, and send arbitrary data to your hardware. This is powerful and useful, but it is not a sandbox. Know what's connected before you let it rip.

## Prerequisites

- **STM32CubeIDE** installed at `/Applications/STM32CubeIDE.app` (macOS) or `/opt/st/stm32cubeide_*` (Linux)
- **Python 3.10+**
- **OpenOCD** (`brew install open-ocd`) — for memory read/write, live monitoring,
  board info, and the optional OpenOCD flash backend
- **open-source stlink tools** — `st-info`, `st-flash`, and optionally
  `st-util`, `st-trace`, and `st-server` must already be installed and on
  `PATH`; this MCP does not install them
- **ST-Link** connected via USB (for flash/board info)
- **Serial port** available (ST-Link VCP or USB-UART adapter)

The upstream stlink project dropped macOS support in v1.8.0. On macOS, use a
compatible older stlink release or use the existing OpenOCD-backed MCP tools.

## Installation

```bash
git clone https://github.com/shieldyguy/stm32-mcp.git
cd stm32-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Register with Claude Code

### Option A: CLI

```bash
claude mcp add stm32 -- /path/to/stm32-mcp/.venv/bin/python -m stm32_mcp.server
```

### Option B: Project config

Add to your project's `.claude/settings.json` or `.claude.json`:

```json
{
  "mcpServers": {
    "stm32": {
      "command": "/path/to/stm32-mcp/.venv/bin/python",
      "args": ["-m", "stm32_mcp.server"]
    }
  }
}
```

## Self-serve CLI 

`bin/` contains four thin wrappers over the same code the MCP
tools use

| Command       | Usage                                                        |
| ------------- | ------------------------------------------------------------ |
| `stm32-list`  | List connected probes + boards with nicknames                |
| `stm32-flash` | `stm32-flash <probe\|board> <file.elf> [--noverify] [--noreset]` |
| `stm32-build` | `stm32-build <project_path> [Debug\|Release] [--clean]`      |
| `stm32-bf`    | `stm32-bf <project_path> <probe\|board> [Debug\|Release] [--clean]` |
| `stm32-help`  | List these commands with their usage (auto-generated from the scripts) |

Add `bin/` to your PATH:

```bash
export PATH="/path/to/stm32-mcp/bin:$PATH"
```

Probe nicknames and board nicknames resolve.

Builds share the MCP's headless CubeIDE workspace lock, so a
`stm32-build`/`stm32-bf` racing an agent-driven build will queue behind it.


## Available Tools

### Build & Flash

| Tool                    | Description                                            |
| ----------------------- | ------------------------------------------------------ |
| `stm32_build`           | Compile firmware using CubeIDE headless builder        |
| `stm32_flash`           | Flash .elf/.bin/.hex via stlink/st-flash by default   |
| `stm32_build_and_flash` | Build + stlink flash in one step (preferred workflow)  |
| `stm32_board_info`      | Read ST-Link/MCU info (device ID, flash size, voltage) |

`stm32_flash` and `stm32_build_and_flash` accept `backend="stlink"` (the
default) or `backend="openocd"` for the legacy OpenOCD path. ELF inputs are
converted to temporary Intel HEX files for stlink; binary inputs use the
standard flash base address `0x08000000`.

### Multi-Board Management

| Tool                 | Description                                          |
| -------------------- | ---------------------------------------------------- |
| `stm32_list_probes`  | Show all connected boards with nicknames and MCU IDs |
| `stm32_set_nickname` | Name a board (by MCU UID) or probe (by ST-Link SN)   |

Board nicknames follow the physical MCU (persist across probe swaps). Probe nicknames follow the ST-Link hardware. Use nicknames in any `probe` parameter across all tools.

### Serial Communication

| Tool                | Description                                                |
| ------------------- | ---------------------------------------------------------- |
| `serial_list_ports` | List serial ports (marks ST-Link VCP ports with nicknames) |
| `serial_connect`    | Open a serial connection                                   |
| `serial_send`       | Send data and read response                                |
| `serial_read`       | Read buffered serial data                                  |
| `serial_disconnect` | Close a serial connection                                  |
| `serial_sequence`   | Run multi-step send/delay/memory sequences in one call     |

### Debug & Monitoring

| Tool                 | Description                                                |
| -------------------- | ---------------------------------------------------------- |
| `stm32_read_memory`  | Read memory by address or variable name (from ELF symbols) |
| `stm32_write_memory` | Write memory by address or variable name                   |
| `stm32_readelf`      | Inspect ELF headers, sections, symbols, and DWARF data     |
| `stm32_disassemble`  | Disassemble ARM firmware with source and symbol context   |
| `live_memory_start`  | Start continuous background memory monitoring via SWD      |
| `live_memory_read`   | Read recent entries from a live memory session             |
| `live_memory_stop`   | Stop a live memory session                                 |

### Direct stlink Backend

The direct stlink tools use the user-installed executables from the stlink
project. They are separate from the existing OpenOCD-backed tools, so either
backend can be selected for a workflow.

| Tool                    | Description                                                     |
| ----------------------- | --------------------------------------------------------------- |
| `stlink_info`           | Run an `st-info` query                                          |
| `stlink_flash`          | Run `st-flash` read/write/erase/reset                          |
| `stlink_command`        | Run any supported stlink executable with an argument list       |
| `stlink_debug_start`    | Start an `st-util` GDB server                                  |
| `stlink_debug_command`  | Execute newline-separated ARM GDB commands against that server |
| `stlink_debug_action`   | Typed breakpoint, step, variable, register, and memory actions |
| `stlink_gdb_start`      | Start a persistent ARM GDB client                       |
| `stlink_gdb_command`    | Execute one command in persistent GDB state             |
| `stlink_gdb_status`     | Read persistent GDB state and output                    |
| `stlink_gdb_stop`       | Stop the persistent GDB client                          |
| `stlink_debug_status`   | Read GDB server state and recent output                        |
| `stlink_debug_stop`     | Stop the GDB server                                             |
| `stlink_trace_start`    | Start `st-trace`                                                |
| `stlink_trace_read`     | Read buffered trace output                                     |
| `stlink_trace_status`   | Read trace state                                                |
| `stlink_trace_stop`     | Stop trace                                                      |
| `stlink_server_start`   | Start the optional `st-server` proxy                           |
| `stlink_server_status`  | Read proxy state                                                |
| `stlink_server_stop`    | Stop the proxy                                                  |
| `stlink_session_list`   | List managed stlink sessions                                   |

#### Flashing with st-flash

`stlink_flash` accepts a `probe` nickname, board nickname, or raw ST-Link
serial number. Typical calls are:

```text
stlink_flash(operation="write", file_path="firmware.bin",
             address="0x08000000", probe="blue", reset=true)
stlink_flash(operation="read", file_path="backup.bin",
             address="0x08000000", size="0x10000", probe="blue")
stlink_flash(operation="erase", probe="blue")
```

For Intel HEX files, use `file_format="ihex"`; for option-byte and option
control-register operations, use `area` and `value` as documented by the
installed `st-flash --help`.

The `reset` flag maps directly to st-flash's `--reset` option. Leaving it false
does not suppress the reset behavior built into some stlink releases; it only
omits the additional `--reset` request.

#### Debugging with st-util

Start a server, then send GDB command sequences through the returned session:

```text
stlink_debug_start(probe="blue", elf_path="/path/to/firmware.elf")
stlink_debug_command(session_id="abcd1234",
                     commands="break main\ncontinue\ninfo registers")
stlink_debug_status(session_id="abcd1234")
stlink_debug_stop(session_id="abcd1234")
```

`stlink_debug_command` uses `arm-none-eabi-gdb` and supports normal GDB
commands, including breakpoints, stepping, variables, memory/register
inspection, backtraces, `load`, and `monitor reset`. For example:

```text
stlink_debug_command(session_id="abcd1234",
                     commands="break main\ncontinue\ninfo locals\ninfo registers\nstep")
```

The typed equivalent is useful when an action should be explicit:

```text
stlink_debug_action(session_id="abcd1234", action="breakpoint", location="main")
stlink_debug_action(session_id="abcd1234", action="continue")
stlink_debug_action(session_id="abcd1234", action="variables")
stlink_debug_action(session_id="abcd1234", action="registers")
stlink_debug_action(session_id="abcd1234", action="memory", location="0x20000000", count=4)
stlink_debug_action(session_id="abcd1234", action="step")
```

Each typed action is a batch GDB invocation. For a breakpoint and the
following stop/step operations that must share one GDB client state, send the
sequence together with `stlink_debug_command`.

The GDB executable is discovered from `STM32_ARM_TOOLCHAIN_BIN`, the supplied
CubeIDE bundle, or `PATH`. The `st-util` server remains alive between calls
when `multi=true` (the default). With `multi=false`, use one complete command
sequence before the first GDB client disconnects.

For persistent breakpoint and stepping state across separate MCP calls, start
the MI-backed GDB client after the server:

```text
stlink_gdb_start(port=4242, elf_path="/path/to/firmware.elf")
stlink_gdb_command(session_id="efgh5678", command="break main")
stlink_gdb_command(session_id="efgh5678", command="continue")
stlink_gdb_command(session_id="efgh5678", command="info registers")
stlink_gdb_command(session_id="efgh5678", command="info locals")
stlink_gdb_command(session_id="efgh5678", command="step")
stlink_gdb_stop(session_id="efgh5678")
```

`stlink_debug_action` accepts `gdb_session_id` to run its typed action through
this persistent client instead of creating a batch client.

GDB commands are intentionally trusted input: GDB itself can execute host
commands through commands such as `shell` or `source`. Do not expose this MCP
server to untrusted clients.

`stlink_server_start` binds to loopback by default. Binding elsewhere requires
`allow_non_loopback=true`; `st-server` has no authentication or encryption and
exposes full flash/debug control to clients that can reach the port.

Stop `live_memory` sessions and other OpenOCD users before opening an stlink
session on the same probe; those existing backends have separate process
lifecycles.

#### ELF Inspection and Disassembly

Use `stm32_readelf` for ELF metadata and DWARF data:

```text
stm32_readelf(elf_path="/path/to/firmware.elf", mode="headers")
stm32_readelf(elf_path="/path/to/firmware.elf", mode="sections")
stm32_readelf(elf_path="/path/to/firmware.elf", mode="symbols")
stm32_readelf(elf_path="/path/to/firmware.elf", mode="debug_info")
```

Use `stm32_disassemble` for code:

```text
stm32_disassemble(elf_path="/path/to/firmware.elf", function="main")
stm32_disassemble(elf_path="/path/to/firmware.elf",
                  start_address="0x08000100", stop_address="0x08000200",
                  source=true)
```

Both tools call the ARM-prefixed CubeIDE binaries and accept `extra_args` for
binutils options specific to the installed release.

#### Generic stlink Commands

Use `stlink_command` when a local stlink release exposes an option not covered
by the typed tools. `tool` must be one of `st-info`, `st-flash`, `st-util`,
`st-trace`, or `st-server`, and `arguments` is a JSON/list argument array, not
a shell command string. For example:

```text
stlink_command(tool="st-info", arguments=["--version"])
```

The server passes arguments without a shell and returns the command's stdout
and stderr, with exit codes included for failed commands and timeout errors.

## Hardware Sequences

`serial_sequence` schedules multiple steps (serial send, delay, webcam capture, and SWD memory read/write) in one tool call. delays use a `time.sleep()` in the executor thread. Claude can't reliably time individual tool calls, so this allows tight timing of commands and expectations. 

### Step types

```json
[
  { "send": "SIM_LEFT", "to": "/dev/cu.usbmodem11202" },
  { "delay_ms": 500 },
  {
    "send": "GET_BLINK_STATE",
    "to": "/dev/cu.usbmodem11402",
    "expect": "BLINK"
  },
  { "capture": true, "label": "post_brake" },
  {
    "mem_write": true,
    "address": "0x48000418",
    "value": "0x40",
    "probe": "yellow"
  },
  { "delay_ms": 1000 },
  {
    "mem_read": true,
    "address": "0x48000400",
    "count": 2,
    "probe": "yellow",
    "label": "gpio_post"
  }
]
```

- **Send step:** `{send, to, expect?, read_timeout?, line_ending?}` — `to` is the port path from `serial_connect`
- **Delay step:** `{delay_ms}` — real `time.sleep()`, not tool-call round-trips
- **Capture step:** `{capture: true, label?, device_index?}` — PNG saved to `/tmp/stm32-captures/`
- **Memory write step:** `{mem_write: true, address | symbol + elf_path, value, probe, width?}`
- **Memory read step:** `{mem_read: true, address | symbol + elf_path, probe, count?, width?, label?}`

Memory step notes:

- `probe` accepts ST-Link SN, probe nickname, or board nickname
- `address` is hex (e.g. `"0x48000418"`); alternatively use `symbol` + `elf_path` to resolve by name
- `width` is 8/16/32 bits, defaults to 32 (auto-detected from symbol size when using `symbol`)
- Each memory op currently launches a fresh OpenOCD process (~tens of ms overhead per op), so inter-memory-op timing below ~50ms is approximate. Delays themselves are accurate.

### Parameters

- **`on_failure`:** `"continue"` (default) runs all steps regardless. `"stop"` aborts on first failure.
- **`filter_responses`:** When `true`, `expect` patterns match only `>`-prefixed VCP response lines (ignores debug noise).

### Output

```
Step 1 [/dev/cu.usbmodem11202] SEND: SIM_LEFT
  Response: >OK:SIM_LEFT

Step 2 DELAY: 500ms

Step 3 [/dev/cu.usbmodem11402] SEND: GET_BLINK_STATE
  Response: >BLINK_STATE:BLINK
  Expect "BLINK": PASS

Step 4 [yellow] MEM_WRITE: Wrote 0x00000040 to 0x48000418

Step 5 DELAY: 1000ms

Step 6 [yellow] MEM_READ: gpio_post 0x48000400: 0xabffdfff 0x00000080

Summary: 2/2 sends OK, 1/1 assertions PASS, 1/1 mem_writes OK, 1/1 mem_reads OK
```

## Live Memory Monitoring

Monitor firmware variables in real time via SWD, without modifying firmware or using serial. OpenOCD runs as a persistent subprocess and polls variables over its built-in TCL socket.

### Start a session

```
live_memory_start(
    variables='["blink", "ts"]',       # symbol names from ELF
    elf_path="/path/to/firmware.elf",
    probe="taillight",                  # board/probe nickname
    interval_ms=500                     # min 250ms
)
```

Variables can be:

- **Symbol names** (strings): `"blink"` — resolved from the ELF via `arm-none-eabi-nm`
- **Dicts with symbol + type**: `{"symbol": "temperature", "type": "float"}` — interprets 32-bit value as IEEE 754
- **Dicts with raw address**: `{"address": "0x20000304", "name": "x", "width": 32}`

### Read recent values

```
live_memory_read(session_id="abc123", last_n=10)
```

Returns recent entries from an in-memory ring buffer (max 100 entries). Full history is written to the JSONL output file.

### JSONL output format

```json
{ "t": 1709830123.456, "elapsed_s": 1.002, "values": { "blink": 65539 } }
```

### Stop a session

```
live_memory_stop(session_id="abc123")
```

Returns stats: duration, read count, error count, output file path.

### Constraints

- **One session per probe** — this is a hardware constraint (single SWD connection)
- **Stop before flashing** — `live_memory` holds the SWD connection; `stm32_flash`, `stlink_flash`, and `stm32_read/write_memory` will fail if a session is active
- **TCL port 6666** — OpenOCD's default. Stop other OpenOCD instances first if there's a conflict

## Serial Defaults

- **Baud rate:** 115200
- **Line ending:** LF (`\n`)
- **Read polling:** 50ms inter-byte sleep, 200ms silence break
- **Buffer limits:** 4096 bytes max read

## Development

### MCP Inspector

```bash
source .venv/bin/activate
mcp dev src/stm32_mcp/server.py
```

### Loopback Testing

Serial tools can be tested without hardware using pyserial's loopback:

```python
import serial
ser = serial.serial_for_url("loop://", baudrate=115200, timeout=0.1)
ser.write(b"PING\n")
print(ser.read(100))  # b'PING\n'
```
