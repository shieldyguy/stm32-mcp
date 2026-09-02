import asyncio
import unittest
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from stm32_mcp import elf_tools


class ElfToolTests(unittest.TestCase):
    def test_readelf_builds_arm_command(self):
        completed = elf_tools.subprocess.CompletedProcess([], 0, "ELF Header\n", "")
        with NamedTemporaryFile() as elf:
            with (
                patch.object(elf_tools, "find_readelf", return_value="/tools/readelf"),
                patch.object(elf_tools.subprocess, "run", return_value=completed) as run,
            ):
                result = elf_tools._do_readelf(
                    elf.name, "headers", True, ["--wide"], 2
                )

        self.assertEqual(result, "ELF Header")
        self.assertEqual(run.call_args.args[0], [
            "/tools/readelf", "-W", "-h", "--wide", elf.name
        ])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_disassemble_builds_function_command(self):
        completed = elf_tools.subprocess.CompletedProcess([], 0, "main:\n", "")
        with NamedTemporaryFile() as elf:
            with (
                patch.object(elf_tools, "find_objdump", return_value="/tools/objdump"),
                patch.object(elf_tools.subprocess, "run", return_value=completed) as run,
            ):
                result = elf_tools._do_disassemble(
                    elf.name,
                    "main",
                    "",
                    "",
                    "",
                    False,
                    False,
                    True,
                    True,
                    True,
                    None,
                    2,
                )

        self.assertEqual(result, "main:")
        self.assertEqual(run.call_args.args[0], [
            "/tools/objdump", "--disassemble=main", "-C", "-l", elf.name
        ])

    def test_async_readelf_uses_supplied_cubeide_tool(self):
        completed = elf_tools.subprocess.CompletedProcess([], 0, "ELF Header\n", "")
        with NamedTemporaryFile() as elf:
            with (
                patch.object(elf_tools, "find_readelf", return_value="/tools/readelf"),
                patch.object(elf_tools.subprocess, "run", return_value=completed),
            ):
                result = asyncio.run(elf_tools.stm32_readelf(elf.name, mode="headers"))
        self.assertIn("ELF Header", result)


if __name__ == "__main__":
    unittest.main()
