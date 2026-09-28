"""Offline tests for complete reads and compact results.

Real tool_* formatting over the real host-driver ops on a temporary volume; no SSH
and no game server.
"""
import json
import re
import time
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


class MonitorDeltaTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.f.ns["MONITOR_ROOT"] = str(self.f.root / "monitors")

    def record(self, count):
        """A pattern monitor that has seen `count` matches; the ring keeps the last 50."""
        ring = [[float(i), "[ERROR] sv.lua:%d: boom" % i] for i in range(max(1, count - 49), count + 1)]
        state = {"pid": 1, "nonce": "n", "mode": "pattern", "pattern": "ERROR",
                 "armed": time.time() - 60, "phase": "triggered", "match_count": count,
                 "matches": ring, "history": [], "note": ""}
        root = self.f.root / "monitors"
        root.mkdir(exist_ok=True)
        (root / "unit_abcd1234.json").write_text(json.dumps(state))

    def check(self, **args):
        text, error = self.h.call("tool_monitor", id="abcd1234", **args)
        self.assertFalse(error, text)
        return text

    def test_after_returns_only_unseen_matches(self):
        self.record(11)
        text = self.check(after=10)
        self.assertIn("matches=11, 1 new after #10", text)
        self.assertIn("#11 [+11.0s] [ERROR] sv.lua:11: boom", text)
        self.assertNotIn("#10 ", text)
        self.assertIn("next check: after=11", text)
        idle = self.check(after=11)
        self.assertIn("matches=11, 0 new after #11", idle)
        self.assertNotIn("next check", idle)

    def test_pages_oldest_first_and_counts_evicted_matches(self):
        self.record(200)
        text = self.check(after=100)
        self.assertIn("matches=200, 100 new after #100", text)
        self.assertIn("50 unseen match(es) were evicted", text)
        self.assertIn("#151 ", text)
        self.assertIn("#165 ", text)
        self.assertNotIn("#166 ", text)
        self.assertIn("next check: after=165 (35 more waiting)", text)
        self.assertIn("#166 ", self.check(after=165))


class HistoryTests(ToolTestCase):
    def deploy(self, *entries):
        return self.f.deploy(*entries)

    def history(self, **args):
        text, error = self.h.call("tool_fetch", what="history", **args)
        self.assertFalse(error, text)
        return text

    def test_one_line_per_deployment_even_for_large_batches(self):
        big = self.deploy(*[self.f.entry("addons/a/lua/f%03d.lua" % i, b"return %d" % i, "missing")
                            for i in range(400)])
        self.assertTrue(big["ok"], big)
        text = self.history(lines=1)
        lines = text.split("\n")
        self.assertEqual(len(lines), 2, text)
        self.assertIn(big["deployment_id"], lines[1])
        self.assertIn("deploy complete", lines[1])
        self.assertIn("400 file(s) (400 new)", lines[1])
        self.assertIn("addons/a/lua/f000.lua +399", lines[1])
        self.assertLess(len(text), 400)

    def test_detail_pages_files_with_full_hashes(self):
        big = self.deploy(*[self.f.entry("addons/a/lua/f%03d.lua" % i, b"return %d" % i, "missing")
                            for i in range(120)])
        text = self.history(deployment_id=big["deployment_id"])
        self.assertIn("files 1-50 of 120", text)
        self.assertIn("addons/a/lua/f000.lua  %s  new" % sha(b"return 0"), text)
        self.assertIn("next: offset=51", text)
        tail = self.history(deployment_id=big["deployment_id"], offset=101)
        self.assertIn("files 101-120 of 120", tail)
        self.assertNotIn("next: offset", tail)

    def test_replaced_unchanged_rejected_and_uncertain_outcomes(self):
        self.write("data/a", b"a0")
        self.write("data/b", b"b0")
        mixed = self.deploy(self.f.entry("data/a", b"a1", sha(b"a0")), self.f.entry("data/b", b"b0", sha(b"b0")))
        stale = self.deploy(self.f.entry("data/a", b"a2", sha(b"a0")))
        self.assertFalse(stale["ok"])
        listing = self.history()
        self.assertIn("deploy rejected  1 file(s)  data/a  1 stale-base conflict(s)", listing)
        self.assertIn("deploy complete  2 file(s) (1 replaced, 1 unchanged)", listing)
        detail = self.history(deployment_id=mixed["deployment_id"])
        self.assertIn("restorable with backup_id=%s" % mixed["deployment_id"], detail)
        self.assertIn("data/a  %s  was %s" % (sha(b"a1"), sha(b"a0")[:16]), detail)
        self.assertIn("data/b  %s  unchanged" % sha(b"b0"), detail)
        conflict = self.history(deployment_id=stale["deployment_id"])
        self.assertIn("conflict data/a: expected=%s actual=%s" % (sha(b"a0"), sha(b"a1")), conflict)
        (self.f.history / (mixed["deployment_id"] + ".result.json")).unlink()
        self.assertIn("deploy uncertain", self.history())
        self.assertIn("inspect live hashes before retrying", self.history())

    def test_pagination_and_pre_2_1_cursor(self):
        ids = [self.deploy(self.f.entry("data/p%d" % i, b"x", "missing"))["deployment_id"] for i in range(3)]
        page = self.history(lines=2)
        self.assertIn("next page: before=%s" % ids[1], page)
        rest = self.history(before=ids[1])
        self.assertIn(ids[0], rest)
        self.assertNotIn(ids[1], rest)
        self.assertNotIn("next page", rest)
        self.assertNotIn(ids[1], self.history(before=ids[1] + ".prepared.json"))

    def test_path_filter_and_bad_deployment_id(self):
        self.deploy(self.f.entry("data/one", b"1", "missing"))
        self.deploy(self.f.entry("lua/two.lua", b"2", "missing"))
        text = self.history(path="lua/")
        self.assertIn("lua/two.lua", text)
        self.assertNotIn("data/one", text)
        message, error = self.h.call("tool_fetch", what="history", deployment_id="bogus")
        self.assertTrue(error)
        self.assertIn("deployment_id", message)


if __name__ == "__main__":
    unittest.main()
