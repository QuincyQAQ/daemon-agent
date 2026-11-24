"""Exercise the real terminal line discipline and the saved user message."""
import fcntl
import importlib.util
import json
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform.startswith("linux") and importlib.util.find_spec("readline"),
                     "Linux terminal with readline required")
class ReplEditingTest(unittest.TestCase):
    def check_edit(self, options, prompt):
        def wait_for_prompt():
            data = bytearray()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    data.extend(os.read(master, 8192))
                    if prompt in data:
                        return
            self.fail(f"Prompt missing: {data[-300:]!r}")

        with tempfile.TemporaryDirectory() as tmp:
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 20, 0, 0))
            env = {**os.environ, "FOLDJAY_PROVIDER": "demo", "LC_ALL": "C.UTF-8",
                   "TERM": "xterm-256color", "INPUTRC": os.devnull}
            with subprocess.Popen([sys.executable, "-m", "foldjay.main", "--workspace", tmp,
                                   "run", "--no-evolution", "--mode", "plan", *options],
                                  stdin=slave, stdout=slave, stderr=slave, env=env) as child:
                try:
                    wait_for_prompt()
                    # Wrap the line, erase 好, move left twice, delete P and insert 卡.
                    os.write(master, "今天查中文GPU好".encode() + b"\x7f\x1b[D\x1b[D\x1b[3~" + "卡\r".encode())
                    wait_for_prompt()
                    os.write(master, b"/exit\r")
                    self.assertEqual(child.wait(timeout=5), 0)
                finally:
                    if child.poll() is None:
                        child.kill()
            state, = (Path(tmp) / ".foldjay/sessions").glob("*.json")
            self.assertEqual(json.loads(state.read_text())["task"], "今天查中文G卡U")

    def test_plain_editing(self):
        self.check_edit(["--plain"], b"daemon-agent> ")

    @unittest.skipUnless(importlib.util.find_spec("rich"), "Rich required")
    def test_rich_editing(self):
        self.check_edit([], "› ".encode())
