import asyncio
import unittest
from tempfile import NamedTemporaryFile
from unittest.mock import AsyncMock, patch

from stm32_mcp import flash


class FlashBackendTests(unittest.TestCase):
    def test_stlink_is_default_flash_backend(self):
        with patch.object(flash, "_flash_with_stlink", new_callable=AsyncMock) as stlink:
            stlink.return_value = "stlink ok"
            result = asyncio.run(flash.stm32_flash("firmware.elf"))

        self.assertEqual(result, "stlink ok")
        stlink.assert_awaited_once_with("firmware.elf", True, True, "")

    def test_elf_conversion_uses_objcopy_ihex(self):
        completed = flash.subprocess.CompletedProcess([], 0, "", "")
        with NamedTemporaryFile() as elf, NamedTemporaryFile() as ihex:
            with (
                patch.object(flash, "find_objcopy", return_value="/tools/objcopy"),
                patch.object(flash.subprocess, "run", return_value=completed) as run,
            ):
                error = flash._convert_elf_to_ihex(elf.name, ihex.name)

        self.assertIsNone(error)
        self.assertEqual(
            run.call_args.args[0],
            ["/tools/objcopy", "-O", "ihex", elf.name, ihex.name],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_openocd_backend_remains_explicit(self):
        with (
            patch.object(flash, "resolve_probe_full", return_value=("sn", "target.cfg", 1)),
            patch.object(flash, "_do_flash", return_value="openocd ok"),
        ):
            result = asyncio.run(flash.stm32_flash("firmware.elf", backend="openocd"))

        self.assertEqual(result, "openocd ok")


if __name__ == "__main__":
    unittest.main()
