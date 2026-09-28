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
        MCP._ONCE_SHOWN.clear()

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


class DiffAndDeployOutputTests(ToolTestCase):
    def local_copies(self, n, differ=()):
        """Remote data/fNN.lua plus local copies; indexes in differ get edited copies."""
        folder = self.f.root / "local"
        folder.mkdir(exist_ok=True)
        files = []
        for i in range(n):
            body = b"line a\nline b %d\n" % i
            self.write("data/f%02d.lua" % i, body)
            local = folder / ("f%02d.lua" % i)
            local.write_bytes(body.replace(b"line b", b"line B") if i in differ else body)
            files.append({"path": "data/f%02d.lua" % i, "local": str(local)})
        return files

    def test_batch_diff_counts_identical_files_instead_of_listing_them(self):
        files = self.local_copies(40)
        text, error = self.h.call("tool_diff", files=files, confirm=True)
        self.assertFalse(error, text)
        self.assertIn("40 files, 40 identical, 0 differ, 0 failed", text)
        self.assertIn("identical files are not listed", text)
        self.assertEqual(len(text.split("\n")), 1)
        again, _ = self.h.call("tool_diff", files=files, confirm=True)
        self.assertNotIn("not listed", again)
        self.assertLess(len(again), 120)

    def test_batch_diff_prints_each_differing_file_once(self):
        text, error = self.h.call("tool_diff", files=self.local_copies(3, differ={1}), confirm=True)
        self.assertFalse(error, text)
        server = MCP.SERVER_NAMES[0]
        self.assertIn("3 files, 2 identical, 1 differ, 0 failed", text)
        self.assertIn("[diff] %s:data/f01.lua vs local:f01.lua: DIFFER (16 vs 16 bytes)" % server, text)
        self.assertIn("sha256_a=%s sha256_b=%s" % (sha(b"line a\nline b 1\n"), sha(b"line a\nline B 1\n")), text)
        self.assertIn("-line b 1\n+line B 1", text)
        for absent in ("f00.lua", "f02.lua", "--- ", "+++ ", "sha1"):
            self.assertNotIn(absent, text)

    def test_single_identical_diff_prints_one_hash(self):
        entry = self.local_copies(1)[0]
        text, error = self.h.call("tool_diff", path=entry["path"], local=entry["local"], confirm=True)
        self.assertFalse(error, text)
        self.assertIn(": IDENTICAL (16 bytes)\nsha256=%s" % sha(b"line a\nline b 0\n"), text)
        self.assertNotIn("sha256_b", text)

    def test_deploy_lists_only_files_whose_hash_changed(self):
        self.write("data/keep", b"same")
        self.write("data/edit", b"old")
        files = [{"to": "data/keep", "content": "same", "expected_sha256": sha(b"same")},
                 {"to": "data/edit", "content": "new", "expected_sha256": sha(b"old")},
                 {"to": "lua/x.lua", "content": "return 1", "expected_sha256": "missing"}]
        text, error = self.h.call("tool_deploy", confirm=True, files=files)
        self.assertFalse(error, text)
        self.assertIn("deployed 3 file(s) (1 new, 1 replaced, 1 unchanged), 11 changed bytes", text)
        self.assertIn("\ndata/edit  %s\n" % sha(b"new"), text)
        self.assertIn("\nlua/x.lua  %s  new" % sha(b"return 1"), text)
        self.assertNotIn("data/keep", text)
        for guidance in ("backup_id=<deployment_id>", "Unchanged files are not listed", "runtime reload"):
            self.assertIn(guidance, text)
        files = [{"to": "data/edit", "content": "newer", "expected_sha256": sha(b"new")},
                 {"to": "lua/x.lua", "content": "return 1", "expected_sha256": sha(b"return 1")}]
        again, error = self.h.call("tool_deploy", confirm=True, files=files)
        self.assertFalse(error, again)
        self.assertEqual(len(again.split("\n")), 2, again)


class GrepOutputTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.write("addons/a/lua/sv_net.lua", "".join(
            '    net.Receive("msg_%03d", function(len, ply) end)\n' % i for i in range(200)))
        self.write("addons/a/lua/cl_hud.lua", "local x = 1\n\tHUD_HOOK()\nlocal y = 2\nlocal z = 3\nhud_hook()\n")
        self.write("addons/a/lua/dots.lua", "a.b\naxb\n")
        self.write("addons/a/lua/sub/x-1:odd.lua", "Foo|Bar\n")

    def grep(self, **args):
        text, error = self.h.call("tool_grep", **args)
        self.assertFalse(error, text)
        return text

    def test_matches_are_grouped_by_file_with_indentation_stripped(self):
        lines = self.grep(pattern="net.Receive", max=200).split("\n")
        self.assertIn("grep: 200 match(es) in 1 file(s)", lines[0])
        self.assertEqual(lines[1], "addons/a/lua/sv_net.lua")
        self.assertEqual(lines[2], '1:net.Receive("msg_000", function(len, ply) end)')
        self.assertEqual(len(lines), 202)
        self.assertLess(sum(len(line) + 1 for line in lines), 12500)

    def test_max_caps_shown_matches_but_counts_all(self):
        text = self.grep(pattern="net.Receive", max=5)
        self.assertIn("grep: 200 match(es), showing 5 in 1 file(s)", text)
        self.assertEqual(len(text.split("\n")), 7)

    def test_single_file_and_names_with_colons_and_dashes(self):
        self.assertEqual(self.grep(pattern="hud_hook", path="addons/a/lua/cl_hud.lua").split("\n")[1:],
                         ["addons/a/lua/cl_hud.lua", "5:hud_hook()"])
        self.assertEqual(self.grep(pattern="Foo", path="addons/a/lua/sub").split("\n")[1:],
                         ["addons/a/lua/sub/x-1:odd.lua", "1:Foo|Bar"])

    def test_ignore_case_and_regex_syntaxes(self):
        self.assertTrue(self.grep(pattern="hud_hook", ignore_case=True).endswith("2:HUD_HOOK()\n5:hud_hook()"))
        self.assertIn("grep: 0 match(es)", self.grep(pattern="HUD_HOOK|hud_hook"))
        self.assertIn("2 match(es)", self.grep(pattern="HUD_HOOK|hud_hook", regex="extended"))
        self.assertIn("2 match(es)", self.grep(pattern="a.b", path="addons/a/lua/dots.lua"))
        self.assertIn("1 match(es)", self.grep(pattern="a.b", path="addons/a/lua/dots.lua", regex="fixed"))
        self.assertIn("200 match(es)", self.grep(pattern=r'msg_\d{3}"', regex="perl", max=1))

    def test_context_lines_separators_and_cap(self):
        self.write("lua/ctx.lua", "a\nhit1\nb\nc\nd\ne\nhit2\nz\n")
        self.assertEqual(self.grep(pattern="hit", path="lua", context=1).split("\n")[1:],
                         ["lua/ctx.lua", "1-a", "2:hit1", "3-b", "--", "6-e", "7:hit2", "8-z"])
        capped = self.grep(pattern="hit", path="lua", context=1, max=1).split("\n")
        self.assertIn("grep: 2 match(es), showing 1 in 1 file(s)", capped[0])
        self.assertEqual(capped[1:], ["lua/ctx.lua", "1-a", "2:hit1", "3-b"])

    def test_files_output_lists_names_only(self):
        self.assertEqual(self.grep(pattern="local", output="files").split("\n")[1:],
                         ["addons/a/lua/cl_hud.lua"])
        self.assertIn("1 matching file(s)", self.grep(pattern="local", output="files"))

    def test_capture_cap_keeps_whole_records_and_marks_the_total(self):
        self.write("lua/rows.lua", "".join("row %04d %s\n" % (i, "x" * 40) for i in range(3000)))
        lines = self.grep(pattern="row", path="lua", max=2000).split("\n")
        self.assertIn("grep: >=", lines[0])
        self.assertRegex(lines[-1], r"^\d+:row \d{4} x{40}$")

    def test_bad_options_fail_before_transport(self):
        for bad in ({"regex": "glob"}, {"output": "count"}, {"context": "two"}):
            with mock.patch.object(MCP, "run_driver") as run, \
                 mock.patch.object(MCP, "resolve", return_value=self.h.srv):
                text, error = MCP.tool_grep(dict(server=MCP.SERVER_NAMES[0], pattern="x", **bad))
            self.assertTrue(error, bad)
            run.assert_not_called()


class DefaultBudgetTests(ToolTestCase):
    def sent(self, tool, result, **args):
        """The driver request a tool sends, given a canned driver result."""
        args.setdefault("server", MCP.SERVER_NAMES[0])
        with mock.patch.object(MCP, "run_driver", return_value=result) as run, \
             mock.patch.object(MCP, "resolve", return_value=self.h.srv), \
             mock.patch.object(MCP, "log_event"):
            text, error = getattr(MCP, tool)(args)
        self.assertFalse(error, text)
        return run.call_args.args[0], text

    def test_defaults_are_sized_for_repeated_calls(self):
        console = {"ok": True, "output": "x", "condebug": True}
        self.assertEqual(self.sent("tool_console", console, command="status")[0]["maxbytes"], 8000)
        fetched = {"ok": True, "content": "", "path": "garrysmod/console.log"}
        self.assertEqual(self.sent("tool_fetch", fetched, what="console")[0]["maxbytes"], 12000)
        self.assertEqual(self.sent("tool_fetch", fetched, what="docker")[0]["maxbytes"], 12000)
        self.assertEqual(self.sent("tool_grep", {"ok": True}, pattern="x")[0]["max"], 50)
        rows = {"ok": True, "output": "1"}
        self.assertEqual(self.sent("tool_db_query", rows, sql="SELECT 1")[0]["maxbytes"], 12000)
        self.assertEqual(self.sent("tool_db_schema", rows)[0]["maxbytes"], 12000)
        self.assertEqual(self.sent("tool_mongo_schema", rows)[0]["maxbytes"], 12000)
        self.assertEqual(self.sent("tool_db_query", rows, sql="SELECT 1", maxbytes=10 ** 9)[0]["maxbytes"], 200000)
        local = self.write("local.lua", b"x")
        diff = {"ok": True, "results": [{"ok": True, "equal": True}]}
        req, _ = self.sent("tool_diff", diff, files=[{"path": "lua/x.lua", "local": str(local)}], confirm=True)
        self.assertEqual(req["maxbytes"], 16000)

    def test_non_integer_db_budget_fails_before_transport(self):
        with mock.patch.object(MCP, "run_driver") as run:
            text, error = MCP.tool_db_query({"sql": "SELECT 1", "maxbytes": "lots"})
        self.assertTrue(error)
        run.assert_not_called()

    def test_standing_guidance_is_printed_once(self):
        noted = {"ok": True, "output": "x", "condebug": False, "note": "no -condebug: pty capture"}
        self.assertIn("(note: no -condebug", self.sent("tool_console", noted, command="status")[1])
        self.assertNotIn("(note:", self.sent("tool_console", noted, command="status")[1])
        self.write("lua/a.lua", "x")
        first, _ = self.h.call("tool_fetch", what="hash", path="lua")
        again, _ = self.h.call("tool_fetch", what="hash", path="lua")
        self.assertIn("TIP:", first)
        self.assertNotIn("TIP:", again)
        acked = {"ok": True, "result": {
            "started": True, "ended": True, "ret": "1", "sum": "p=1 f=0", "fails": [],
            "out": "clientlua ack: sent=1 ready=1 acked=1 ok=1 compile_error=0 runtime_error=0 transfer_error=0 timeout=0"}}
        args = dict(code="print(1)", target="76561198000000000", confirm=True)
        self.assertIn("visual/player acceptance remains separate", self.sent("tool_clientlua", acked, **args)[1])
        self.assertNotIn("visual/player", self.sent("tool_clientlua", acked, **args)[1])

    def test_listings_have_no_fixed_width_padding(self):
        self.write("lua/a.lua", "x")
        (self.f.gm / "lua/sub").mkdir()
        text, _ = self.h.call("tool_fetch", what="dir", path="lua")
        self.assertRegex(text, r"\na\.lua  1  \d{4}-\d\d-\d\d \d\d:\d\d\nsub/$")
        hashed, _ = self.h.call("tool_fetch", what="hash", path="lua")
        self.assertIn("\n%s 1 a.lua" % sha(b"x"), hashed)


if __name__ == "__main__":
    unittest.main()
