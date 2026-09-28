"""Offline tests for complete reads and compact results.

Real tool_* formatting over the real host-driver ops on a temporary volume; no SSH
and no game server.
"""
import json
import re
import unittest
from unittest import mock

from test_deploy_guard import MCP, Fixture, sha


class Harness:
    """Route run_driver to the isolated host namespace and resolve to its volume."""
    OPS = {"fetch": "op_fetch", "deploy": "op_deploy", "diff": "op_diff",
           "grep": "op_grep", "monitor": "op_monitor"}

    def __init__(self, fixture):
        self.f = fixture
        self.srv = {"logical": MCP.SERVER_NAMES[0], "uuid": "unit", "running": True, "port": 27015}

    def run_driver(self, req, timeout=45, _retried=False):
        req = json.loads(json.dumps(req))
        return json.loads(json.dumps(self.f.ns[self.OPS[req["op"]]](req)))

    def call(self, tool, **args):
        args.setdefault("server", MCP.SERVER_NAMES[0])
        with mock.patch.object(MCP, "run_driver", self.run_driver), \
             mock.patch.object(MCP, "resolve", return_value=self.srv), \
             mock.patch.object(MCP, "log_event"):
            return getattr(MCP, tool)(args)


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.f = Fixture().__enter__()
        self.addCleanup(self.f.__exit__, None, None, None)
        self.h = Harness(self.f)

    def write(self, rel, data):
        path = self.f.gm / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path


class FileReadTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.body = "".join("hook.Add()\n" if i in (10, 900) else "line %d\n" % i
                            for i in range(1, 1001))
        self.write("lua/big.lua", self.body)

    def read(self, **args):
        text, error = self.h.call("tool_fetch", what="file", path="lua/big.lua", **args)
        self.assertFalse(error, text)
        return text

    def test_default_read_starts_at_line_one_and_names_next_offset(self):
        lines = self.read().split("\n")
        self.assertIn("lines 1-200 of 1000", lines[0])
        self.assertIn("next offset=201", lines[0])
        self.assertEqual(lines[1], "full_file_sha256=" + sha(self.body.encode()))
        self.assertEqual(lines[2], "1\tline 1")
        self.assertEqual(lines[-1], "200\tline 200")
        self.assertEqual(len(lines), 202)

    def test_offset_pages_forward_and_negative_offset_reads_the_end(self):
        text = self.read(offset=990)
        self.assertIn("lines 990-1000 of 1000", text)
        self.assertNotIn("next offset", text)
        self.assertTrue(text.endswith("1000\tline 1000"))
        self.assertIn("lines 998-1000 of 1000", self.read(offset=-3))
        self.assertIn("no lines from offset 2000 (file has 1000 lines)", self.read(offset=2000))

    def test_grep_searches_the_whole_file_with_line_numbers(self):
        text = self.read(grep="hook.Add")
        self.assertIn('grep "hook.Add" matched 2 of 1000 lines; showing 2', text)
        self.assertEqual(text.split("\n")[2:], ["10\thook.Add()", "900\thook.Add()"])

    def test_grep_pages_with_offset(self):
        self.assertIn("next offset=11", self.read(grep="hook.Add", lines=1))
        text = self.read(grep="hook.Add", offset=11)
        self.assertIn("1 from line 11", text)
        self.assertEqual(text.split("\n")[2:], ["900\thook.Add()"])

    def test_byte_cap_keeps_whole_lines_and_names_next_offset(self):
        text = self.read(maxbytes=100)
        lines = text.split("\n")
        self.assertIn("byte cap reached", lines[2])
        last = int(lines[-1].split("\t")[0])
        self.assertIn("lines 1-%d of 1000" % last, lines[0])
        self.assertIn("next offset=%d" % (last + 1), lines[0])
        self.assertLess(sum(len(line) + 1 for line in lines[3:]), 101)

    def test_results_name_volume_relative_paths(self):
        volroot = self.f.ns["VOLROOT"]
        self.assertIn("[%s] garrysmod/lua/big.lua:" % MCP.SERVER_NAMES[0].upper(), self.read())
        text, error = self.h.call("tool_fetch", what="file", path="lua/none.lua")
        self.assertTrue(error)
        self.assertIn("garrysmod/lua/none.lua", text)
        self.write("console.log", "boot\n")
        console, _ = self.h.call("tool_fetch", what="console")
        for out in (self.read(), text, console):
            self.assertNotIn(volroot, out)
            self.assertNotIn("unit/", out)

    def test_binary_file_is_summarized_not_dumped(self):
        self.write("data/blob.bin", b"\x00\x01binary")
        text, error = self.h.call("tool_fetch", what="file", path="data/blob.bin")
        self.assertFalse(error, text)
        self.assertIn("binary file (8 bytes)", text)
        self.assertIn("full_file_sha256=" + sha(b"\x00\x01binary"), text)
        self.assertEqual(len(text.split("\n")), 2)

    def test_long_lines_are_cut_and_reported(self):
        self.write("data/min.json", "x" * 5000 + "\nshort\n")
        text, _ = self.h.call("tool_fetch", what="file", path="data/min.json")
        self.assertIn("1 long line(s) cut at 2000 chars", text)
        self.assertIn("1\t" + "x" * 2000 + "...<+3000 bytes>", text)
        self.assertTrue(text.endswith("2\tshort"))

    def test_crlf_final_line_without_newline_and_empty_file(self):
        self.write("data/crlf.txt", b"a\r\nb\r\nc")
        text, _ = self.h.call("tool_fetch", what="file", path="data/crlf.txt")
        self.assertIn("lines 1-3 of 3", text)
        self.assertTrue(text.endswith("1\ta\n2\tb\n3\tc"))
        self.write("data/empty.txt", b"")
        text, _ = self.h.call("tool_fetch", what="file", path="data/empty.txt")
        self.assertIn("no lines from offset 1 (file has 0 lines)", text)

    def test_non_integer_paging_is_rejected_before_transport(self):
        with mock.patch.object(MCP, "run_driver") as run, \
             mock.patch.object(MCP, "resolve", return_value=self.h.srv):
            text, error = MCP.tool_fetch({"server": MCP.SERVER_NAMES[0], "what": "file",
                                          "path": "lua/big.lua", "offset": "ten"})
        self.assertTrue(error)
        self.assertIn("integers", text)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
