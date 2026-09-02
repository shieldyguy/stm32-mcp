import unittest

from stm32_mcp.gdb_debug import _clean_value, _decode_mi_line


class GdbDebugTests(unittest.TestCase):
    def test_decodes_mi_console_stream_output(self):
        self.assertEqual(_decode_mi_line('~"value = 42\\n"'), "value = 42\n")

    def test_rejects_multiline_gdb_command(self):
        with self.assertRaisesRegex(ValueError, "single line"):
            _clean_value("break main\ncontinue", "command", required=True)


if __name__ == "__main__":
    unittest.main()
