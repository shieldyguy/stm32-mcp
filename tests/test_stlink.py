import asyncio
import unittest
from tempfile import NamedTemporaryFile
from unittest.mock import Mock
from unittest.mock import patch

from stm32_mcp import stlink


class StlinkCommandTests(unittest.TestCase):
    def test_info_args_include_connection_options(self):
        self.assertEqual(
            stlink._build_info_args(
                "probe", "1800k", True, False, "127.0.0.1:4500"
            ),
            [
                "--connect-under-reset",
                "--freq",
                "1800k",
                "--remote",
                "127.0.0.1:4500",
                "--probe",
            ],
        )

    def test_flash_write_args_are_argument_list(self):
        with NamedTemporaryFile() as firmware:
            args = stlink._build_flash_args(
                operation="write",
                file_path=firmware.name,
                address="0x08000000",
                size="",
                value="",
                freq="1800k",
                flash_size="1M",
                file_format="binary",
                area="main",
                opt=True,
                mass_erase=True,
                reset=True,
                connect_under_reset=False,
                hot_plug=False,
                debug=False,
                serial="ABC123",
                remote="",
            )

        self.assertEqual(
            args,
            [
                "--freq",
                "1800k",
                "--flash",
                "1M",
                "--format",
                "binary",
                "--area",
                "main",
                "--serial",
                "ABC123",
                "--opt",
                "--mass-erase",
                "--reset",
                "write",
                firmware.name,
                "0x08000000",
            ],
        )

    def test_flash_rejects_conflicting_connection_modes(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            stlink._build_flash_args(
                operation="erase",
                file_path="",
                address="",
                size="",
                value="",
                freq="",
                flash_size="",
                file_format="",
                area="",
                opt=False,
                mass_erase=False,
                reset=False,
                connect_under_reset=True,
                hot_plug=True,
                debug=False,
                serial="",
                remote="",
            )

    def test_command_uses_no_shell(self):
        completed = stlink.subprocess.CompletedProcess(
            ["/usr/bin/st-info", "--version"], 0, "v1.8.0\n", ""
        )
        with (
            patch.object(stlink, "find_stlink_tool", return_value="/usr/bin/st-info"),
            patch.object(stlink.subprocess, "run", return_value=completed) as run,
        ):
            result = stlink._do_stlink_command("st-info", ["--version"], 2)

        self.assertEqual(result, "v1.8.0")
        run.assert_called_once_with(
            ["/usr/bin/st-info", "--version"],
            capture_output=True,
            shell=False,
            text=True,
            errors="replace",
            timeout=2,
        )

    def test_async_command_reports_unknown_tool(self):
        result = asyncio.run(stlink.stlink_command("not-stlink", []))
        self.assertIn("Unsupported stlink tool", result)

    def test_debug_command_does_not_inherit_mcp_stdin(self):
        process = Mock()
        process.poll.return_value = None
        session = stlink._StlinkSession(
            session_id="debug123",
            kind="debug",
            tool="st-util",
            process=process,
            port=4242,
        )
        stlink._sessions[session.session_id] = session
        completed = stlink.subprocess.CompletedProcess([], 0, "stopped\n", "")
        try:
            with (
                patch.object(stlink, "find_gdb", return_value="/usr/bin/arm-none-eabi-gdb"),
                patch.object(stlink.subprocess, "run", return_value=completed) as run,
            ):
                result = stlink._do_debug_command("debug123", "info registers", "", 2)
        finally:
            stlink._sessions.pop(session.session_id, None)

        self.assertEqual(result, "stopped")
        self.assertEqual(run.call_args.kwargs["stdin"], stlink.subprocess.DEVNULL)
        self.assertIn("target extended-remote localhost:4242", run.call_args.args[0])

    def test_mass_erase_is_write_only(self):
        with self.assertRaisesRegex(ValueError, "only valid for a write"):
            stlink._build_flash_args(
                operation="erase",
                file_path="",
                address="",
                size="",
                value="",
                freq="",
                flash_size="",
                file_format="",
                area="",
                opt=False,
                mass_erase=True,
                reset=False,
                connect_under_reset=False,
                hot_plug=False,
                debug=False,
                serial="",
                remote="",
            )

    def test_debug_actions_map_to_gdb_commands(self):
        self.assertEqual(
            stlink._build_debug_action("breakpoint", "main", "", "", 1, False),
            "break main",
        )
        self.assertEqual(
            stlink._build_debug_action("step", "", "", "", 3, False),
            "step 3",
        )
        self.assertEqual(
            stlink._build_debug_action(
                "registers", "", "", "r0", 1, False
            ),
            "p/x $r0",
        )
        self.assertEqual(
            stlink._build_debug_action(
                "memory", "0x20000000", "", "", 4, False
            ),
            "x/4wx 0x20000000",
        )


if __name__ == "__main__":
    unittest.main()
