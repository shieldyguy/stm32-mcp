# stm32-mcp TODO

## Build result false positives

### "Skipping..." should be a build failure
When CubeIDE headless builder can't parse `.cproject` (e.g. malformed XML), it prints
`Project: Foo doesn't appear to be a CDT project. Skipping...` and exits cleanly.
`_summarize_build` finds no error/warning diagnostics, so it reports "Build: OK" even
though nothing was compiled. The stale ELF (if one exists) gets flashed — silent
regression.

Fix: detect `Skipping...` or `doesn't appear to be a CDT project` in `_do_build`
and treat as `success = False`.

### "Workspace already in use" should surface as an error
When the workspace lock is held (orphaned process, rapid back-to-back builds),
CubeIDE prints `Workspace already in use!` and exits code 1. The MCP tool sometimes
swallows this and reports "Build: OK" with no ELF, causing the flash step to silently
not happen. The clean step may have already deleted the old ELF, leaving nothing.

Fix: check for `Workspace already in use` in raw output (or nonzero exit code with
no "Build Finished") and return a clear error.
