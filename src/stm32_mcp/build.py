"""Build tools  -  CubeIDE headless build, ELF discovery, output summarization."""

import asyncio
import glob
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .toolchain import find_cubeide, get_project_name, validate_project_path

_executor = ThreadPoolExecutor(max_workers=2)

# Track which projects have been imported to skip -import on subsequent builds
_imported_projects: dict[str, bool] = {}

WORKSPACE_PATH = "/tmp/stm32-mcp-workspace"
WORKSPACE_LOCK = os.path.join(WORKSPACE_PATH, ".metadata", ".lock")

BUILD_TIMEOUT = 60  # seconds


def _check_and_clear_workspace_lock() -> str | None:
    """Check workspace lock. Clear if stale. Returns error message or None.

    Our temp workspace (/tmp/stm32-mcp-workspace) is never used by CubeIDE GUI,
    so we only need to check if another MCP headless build is using it  -  not
    whether CubeIDE is running in general (it uses its own workspace).
    """
    if not os.path.isfile(WORKSPACE_LOCK):
        return None

    # Check if another headless build is using OUR temp workspace specifically
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"stm32-mcp-workspace"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return (
                "Another MCP headless build is already running. "
                "Wait for it to finish."
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # No process using our workspace  -  stale lock. Kill any orphaned
    # CubeIDE headless processes and remove the lock.
    try:
        subprocess.run(
            ["pkill", "-f", "headlessbuild.*stm32-mcp-workspace"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    try:
        os.remove(WORKSPACE_LOCK)
    except OSError:
        pass
    return None


LOG_DIR = "/tmp/stm32-mcp-logs"

# Patterns for diagnostics and linker errors
_DIAGNOSTIC_RE = re.compile(r":\d+:\d+:\s*(fatal\s+error|error|warning|note):")
_LINKER_ERROR_PATTERNS = [
    re.compile(r"undefined reference"),
    re.compile(r"ld returned"),
    re.compile(r"multiple definition"),
    re.compile(r"cannot find -l"),
    re.compile(r"recipe for target .* failed"),
]


def _save_build_log(raw: str, project_name: str) -> str:
    """Save full build output to a log file. Returns the file path."""
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, f"{project_name}_{timestamp}.log")
    with open(path, "w") as f:
        f.write(raw)
    return path


def _summarize_build(raw: str, project_name: str) -> dict:
    """Parse raw build output into a compact summary dict.

    Returns dict with keys: summary (str), log_path (str),
    error_count (int), warning_count (int).
    """
    log_path = _save_build_log(raw, project_name)
    lines = raw.splitlines()

    # Collect diagnostics (error/warning lines from compiler)
    errors = []
    warnings = []
    linker_errors = []
    for line in lines:
        if _DIAGNOSTIC_RE.search(line):
            if ": error:" in line or ": fatal error:" in line:
                errors.append(line.strip())
            elif ": warning:" in line:
                warnings.append(line.strip())
        else:
            for pat in _LINKER_ERROR_PATTERNS:
                if pat.search(line):
                    linker_errors.append(line.strip())
                    break

    # Parse size table: "  16384    512   2048  18944   4A00 Pebbles.elf"
    size_line = None
    for i, line in enumerate(lines):
        if re.match(r"\s*text\s+data\s+bss\s+dec\s+hex", line):
            # Next non-empty line is the data row
            for j in range(i + 1, min(i + 3, len(lines))):
                m = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", lines[j])
                if m:
                    size_line = f"{m.group(1)} text, {m.group(2)} data, {m.group(3)} bss"
                    break
            break

    # Parse build time from "Build Finished. 0 errors, 0 warnings. (took 4s.876ms)"
    time_str = ""
    time_match = re.search(r"\(took\s+([^)]+)\)", raw)
    if time_match:
        time_str = f", {time_match.group(1)}"

    # Build the summary
    parts = []

    error_count = len(errors) + len(linker_errors)
    warning_count = len(warnings)

    if error_count == 0:
        parts.append(f"Build: OK ({error_count} errors, {warning_count} warnings{time_str})")
    else:
        parts.append(f"Build: FAILED ({error_count} errors, {warning_count} warnings{time_str})")

    # Show warnings on success, errors+warnings on failure
    for line in errors + linker_errors:
        parts.append(f"  {line}")
    for line in warnings:
        parts.append(f"  {line}")

    if size_line:
        parts.append(f"Size: {size_line}")

    # Include log path on failure so the LLM can dig in if needed
    if error_count > 0:
        parts.append(f"Full log: {log_path}")

    return {
        "summary": "\n".join(parts),
        "log_path": log_path,
        "error_count": error_count,
        "warning_count": warning_count,
    }


def _find_elf(project_path: str, config: str, build_output: str) -> str | None:
    """Find the .elf file  -  glob by mtime, fallback to parsing build output."""
    # Glob for .elf files in the build config directory
    elf_pattern = os.path.join(project_path, config, "*.elf")
    elfs = glob.glob(elf_pattern)
    if elfs:
        # Pick newest by mtime
        return max(elfs, key=os.path.getmtime)

    # Fallback: scan build output for .elf references
    matches = re.findall(r'[\w./-]+\.elf\b', build_output)
    for match in matches:
        # Try as absolute path
        if os.path.isfile(match):
            return match
        # Try relative to project
        candidate = os.path.join(project_path, match)
        if os.path.isfile(candidate):
            return candidate

    return None


def _do_build(
    project_path: str,
    configuration: str = "Debug",
    clean: bool = False,
    _retry: bool = False,
) -> dict:
    """Synchronous build  -  runs in executor thread."""
    # Find CubeIDE
    cubeide = find_cubeide()
    if not cubeide:
        return {"success": False, "summary": "Build: FAILED\n  STM32CubeIDE not found. Install it or check the path."}

    # Validate project
    try:
        project_path = validate_project_path(project_path)
        project_name = get_project_name(project_path)
    except (FileNotFoundError, ValueError) as e:
        return {"success": False, "summary": f"Build: FAILED\n  {e}"}

    # Check workspace lock
    lock_err = _check_and_clear_workspace_lock()
    if lock_err:
        return {"success": False, "summary": f"Build: FAILED\n  {lock_err}"}

    # Build the command
    build_target = f"{project_name}/{configuration}"
    build_flag = "-cleanBuild" if clean else "-build"

    cmd = [
        cubeide,
        "--launcher.suppressErrors",
        "-nosplash",
        "-application", "org.eclipse.cdt.managedbuilder.core.headlessbuild",
        "-data", WORKSPACE_PATH,
    ]

    # Import only if not previously imported (or on retry)
    cache_key = project_path
    if cache_key not in _imported_projects or _retry:
        cmd.extend(["-import", project_path])

    cmd.extend([build_flag, build_target])

    # Run build  -  use Popen so we can guarantee cleanup on any exit path.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return {"success": False, "summary": f"Build: FAILED\n  Build timed out after {BUILD_TIMEOUT}s."}
    except Exception:
        proc.kill()
        proc.wait()
        raise

    raw_output = stdout + "\n" + stderr

    # Detect success
    has_build_finished = bool(re.search(r"Build Finished", raw_output, re.IGNORECASE))
    has_errors = bool(re.search(r":\d+:\d+:\s*error:", raw_output))
    has_build_failed = bool(re.search(r"Build Failed", raw_output, re.IGNORECASE))
    was_skipped = bool(re.search(r"Skipping\.\.\.", raw_output))
    success = has_build_finished and not has_errors and not has_build_failed and not was_skipped

    # If import failed because project already in workspace, retry without -import
    if not success and "already exists in the workspace" in raw_output:
        _imported_projects[cache_key] = True
        return _do_build(project_path, configuration, clean)

    # If build failed because project not found, retry with -import
    if not success and not _retry:
        not_found = (
            "not found in workspace" in raw_output.lower()
            or "could not find" in raw_output.lower()
            or "no project matched" in raw_output.lower()
            or "doesn't appear to be a cdt project" in raw_output.lower()
        )
        if not_found:
            _imported_projects.pop(cache_key, None)
            return _do_build(project_path, configuration, clean, _retry=True)

    # Mark as imported on success
    if success:
        _imported_projects[cache_key] = True

    # Summarize build output
    build_summary = _summarize_build(raw_output, project_name)

    # Find ELF
    elf_path = None
    if success:
        elf_path = _find_elf(project_path, configuration, raw_output)

    return {
        "success": success,
        "summary": build_summary["summary"],
        "log_path": build_summary["log_path"],
        "elf_path": elf_path,
        "project_path": project_path,
        "configuration": configuration,
    }


async def stm32_build(
    project_path: str,
    configuration: str = "Debug",
    clean: bool = False,
) -> str:
    """Build STM32 firmware using CubeIDE headless builder.

    Compiles the project at project_path using the specified build configuration.
    CubeIDE headless mode automatically detects new/deleted source files  -  no
    Makefile maintenance needed.

    Args:
        project_path: Absolute path to the CubeIDE project root (must contain .project and .cproject).
        configuration: Build configuration  -  "Debug" or "Release".
        clean: If true, clean before building (slower but ensures full rebuild).

    Returns:
        Build result with filtered compiler output, errors/warnings, ELF size,
        and the path to the built .elf file on success.
    """
    loop = asyncio.get_event_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            lambda: _do_build(project_path, configuration, clean),
        ),
        timeout=BUILD_TIMEOUT + 10,
    )

    # Format output
    parts = []
    parts.append(result["summary"])
    if result["success"] and result.get("elf_path"):
        parts.append(f"ELF: {result['elf_path']}")
    return "\n".join(parts)


async def stm32_build_and_flash(
    project_path: str,
    configuration: str = "Debug",
    clean: bool = False,
    reset: bool = True,
    verify: bool = True,
    probe: str = "",
    backend: str = "stlink",
) -> str:
    """Build firmware and flash it to the board in one step.

    Compiles the project, then flashes the resulting .elf to the connected
    STM32. Direct stlink/st-flash is the default backend; use ``backend="openocd"``
    for the legacy OpenOCD path.

    Args:
        project_path: Absolute path to the CubeIDE project root.
        configuration: Build configuration  -  "Debug" or "Release".
        clean: If true, clean before building.
        reset: If true, reset the board after flashing.
        verify: If true, verify flash contents after writing.
        probe: Board nickname, probe nickname, or ST-Link SN to target a specific board.
        backend: ``stlink`` (default) or ``openocd``.

    Returns:
        Combined build and flash results.
    """
    if backend not in {"stlink", "openocd"}:
        return "ERROR: backend must be 'stlink' or 'openocd'."

    # Import here to avoid circular import
    from .flash import stm32_flash

    # Build first
    loop = asyncio.get_event_loop()
    build_result = await asyncio.wait_for(
        loop.run_in_executor(
            _executor,
            lambda: _do_build(project_path, configuration, clean),
        ),
        timeout=BUILD_TIMEOUT + 10,
    )

    parts = []
    parts.append(build_result["summary"])

    if not build_result["success"]:
        return "\n".join(parts)

    if build_result.get("elf_path"):
        parts.append(f"ELF: {build_result['elf_path']}")

    # Flash
    elf_path = build_result.get("elf_path")
    if not elf_path:
        parts.append("ERROR: Build succeeded but no .elf file found. Cannot flash.")
        return "\n".join(parts)

    flash_output = await stm32_flash(
        elf_path,
        reset=reset,
        verify=verify,
        probe=probe,
        backend=backend,
    )
    parts.append(flash_output)

    return "\n".join(parts)
