import importlib.util
import io
import unittest


@unittest.skipUnless(importlib.util.find_spec("rich"), "install .[ui] for terminal rendering tests")
class TerminalTest(unittest.TestCase):
    def test_markdown_and_paths_are_literal(self):
        from foldjay.terminal import TerminalChat
        from rich.console import Console
        stream = io.StringIO()
        view = TerminalChat()
        view.out = view.err = Console(file=stream, width=64, color_system=None)
        view.answer("**文件**\n\n```python\nprint('ok')\n```")
        view.event("tool_start", {"name": "read_file", "path": "[blue].md"})
        text = stream.getvalue()
        self.assertIn("文件", text)
        self.assertIn("print('ok')", text)
        self.assertIn("[blue].md", text)
        self.assertNotIn("**", text)
        self.assertNotIn("```", text)

    def test_terminal_controls_do_not_reach_model_markdown(self):
        from foldjay.terminal import visible
        self.assertEqual(visible("A\x1bB\r\x07\x9b\n中\t👩‍💻"), "AB\n中\t👩‍💻")
