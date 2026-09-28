"""Diagnostic protocol, deployment integration, and crash-log confinement."""
import base64
import json
import os
from pathlib import Path
from unittest import mock

from test_compact_output import ToolTestCase
from test_deploy_guard import MCP, sha


class DiagnosticTests(ToolTestCase):
    def test_state_changes_require_confirmation_before_discovery(self):
        with mock.patch.object(MCP, "resolve") as resolve:
            for action in ("profile", "errors_start", "errors_stop", "refresh"):
                text, error = MCP.tool_diagnostics({"server": MCP.SERVER_NAMES[0], "action": action})
                self.assertTrue(error, text)
                self.assertIn("confirm=true", text)
        resolve.assert_not_called()

    def test_untrusted_parameters_are_rejected_before_discovery(self):
        bad = [{"action": "run"}, {"seconds": float("nan")}, {"seconds": float("inf")},
               {"seconds": True}, {"limit": 0}, {"ttl": 3601}, {"offset": -1},
               {"maxbytes": 1}, {"after": "invalid"}, {"after": "a" * 32 + ":" + "9" * 16}]
        with mock.patch.object(MCP, "resolve") as resolve:
            for args in bad:
                text, error = MCP.tool_diagnostics(dict(args, server=MCP.SERVER_NAMES[0]))
                self.assertTrue(error, (args, text))
        resolve.assert_not_called()

    def test_runtime_absence_and_missing_adapter_are_explicit(self):
        self.assertIn("down", MCP._diagnostics_call({"running": False}, {"action": "capabilities"})["error"])
        with mock.patch.object(MCP, "_HERE", str(self.f.root / "missing")):
            result = MCP._diagnostics_call({"running": True}, {"action": "capabilities"})
        self.assertIn("install", result["error"])

    def test_adapter_requires_complete_valid_framed_json(self):
        srv = {"uuid": "unit", "running": True}
        for framed in ({"started": True, "ended": False},
                       {"started": True, "ended": True, "out": '{"ok":true'},
                       {"started": True, "ended": True, "out": '[]'},
                       {"started": True, "ended": True, "out": '{"ok":true}', "err": "failure"}):
            with mock.patch.object(MCP, "run_driver", return_value={"ok": True, "result": framed}), \
                 mock.patch.object(MCP, "log_event"):
                self.assertFalse(MCP._diagnostics_call(srv, {"action": "capabilities"})["ok"])

    def test_profile_uses_async_runner_and_never_retries(self):
        result = {"ok": True, "result": {"started": True, "ended": True,
            "out": base64.b64encode(b'{"ok":true,"nodes":[]}').decode()}}
        with mock.patch.object(MCP, "run_driver", return_value=result) as run, mock.patch.object(MCP, "log_event"):
            actual = MCP._diagnostics_call({"uuid": "unit", "running": True}, {"action": "profile", "seconds": 2.5})
        self.assertTrue(actual["ok"])
        run.assert_called_once()
        self.assertTrue(run.call_args.args[0]["async"])
        self.assertGreater(run.call_args.args[0]["async_timeout"], 2.5)

    def test_chunked_payload_preserves_long_unicode_records(self):
        data = {"ok": True, "message": "界" * 2000}
        encoded = base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
        chunks = "\n".join(encoded[i:i+1000] for i in range(0, len(encoded), 1000))
        response = {"ok": True, "result": {"started": True, "ended": True, "out": chunks}}
        with mock.patch.object(MCP, "run_driver", return_value=response), mock.patch.object(MCP, "log_event"):
            result = MCP._diagnostics_call({"uuid": "unit", "running": True}, {"action": "errors"})
        self.assertEqual(result, data)

    def test_refresh_preserves_full_source_paths_and_rejects_aliases(self):
        paths = ["addons/test/lua/shared.lua", "lua/autorun/test.lua", "gamemodes/test/gamemode/cl_init.lua"]
        self.assertEqual(MCP._refresh_paths(paths + paths)[0], paths)
        for path in ("../a.lua", "lua/../a.lua", "lua//a.lua", "C:/a.lua", "lua\\a.lua", "data/a.lua", "/lua/a.lua"):
            self.assertIsNotNone(MCP._refresh_paths([path])[1], path)

    def test_deploy_refresh_only_follows_successful_changed_lua_writes(self):
        self.write("lua/old.lua", b"old")
        self.write("data/note.txt", b"old")
        with mock.patch.object(MCP, "_diagnostics_call", return_value={"ok": True, "files": []}) as call:
            text, error = self.h.call("tool_deploy", confirm=True, refresh_lua=True, files=[
                {"to": "lua/old.lua", "content": "new", "expected_sha256": sha(b"old")},
                {"to": "data/note.txt", "content": "new", "expected_sha256": sha(b"old")}])
        self.assertFalse(error, text)
        self.assertEqual(call.call_args.args[1]["paths"], ["lua/old.lua"])
        self.assertIn("client execution unverified", text)

    def test_conflict_and_noop_never_refresh(self):
        self.write("lua/old.lua", b"old")
        with mock.patch.object(MCP, "_diagnostics_call") as call:
            _, conflict = self.h.call("tool_deploy", confirm=True, refresh_lua=True,
                to="lua/old.lua", content="new", expected_sha256=sha(b"stale"))
            _, noop = self.h.call("tool_deploy", confirm=True, refresh_lua=True,
                to="lua/old.lua", content="old", expected_sha256=sha(b"old"))
        self.assertTrue(conflict)
        self.assertFalse(noop)
        call.assert_not_called()

    def test_refresh_failure_does_not_misreport_completed_write(self):
        self.write("lua/old.lua", b"old")
        with mock.patch.object(MCP, "_diagnostics_call", return_value={"ok": False, "error": "server down"}):
            text, error = self.h.call("tool_deploy", confirm=True, refresh_lua=True,
                to="lua/old.lua", content="new", expected_sha256=sha(b"old"))
        self.assertFalse(error, text)
        self.assertIn("File deployment completed", text)
        self.assertEqual((self.f.gm / "lua/old.lua").read_bytes(), b"new")

    def test_crash_logs_are_newest_first_and_readable_while_down(self):
        self.h.srv["running"] = False
        self.write("holylib/crashes/older.txt", b"old\n")
        self.write("holylib/crashes/newer.txt", b"new\nsecond\n")
        os.utime(self.f.gm / "holylib/crashes/older.txt", (100, 100))
        os.utime(self.f.gm / "holylib/crashes/newer.txt", (200, 200))
        text, error = self.h.call("tool_fetch", what="crashes", lines=1)
        self.assertFalse(error, text)
        self.assertIn("newer.txt", text)
        self.assertNotIn("older.txt", text)
        text, error = self.h.call("tool_fetch", what="crashes", path="newer.txt", lines=1)
        self.assertFalse(error, text)
        self.assertIn("1\tnew", text)
        self.assertIn(sha(b"new\nsecond\n"), text)

    def test_crash_log_paths_cannot_escape(self):
        for path in ("../private", "sub/log", "sub\\log", "..", "C:secret"):
            text, error = self.h.call("tool_fetch", what="crashes", path=path)
            self.assertTrue(error, (path, text))

    def test_crash_log_symlinks_are_rejected(self):
        self.write("data/private.txt", b"private")
        (self.f.gm / "holylib/crashes").mkdir(parents=True)
        try:
            (self.f.gm / "holylib/crashes/link").symlink_to(self.f.gm / "data/private.txt")
        except OSError:
            self.skipTest("OS does not permit symlink creation")
        text, error = self.h.call("tool_fetch", what="crashes", path="link")
        self.assertTrue(error, text)
        self.assertNotIn("private", text)

    def test_schema_matches_dispatch_and_offers_diagnostic_options(self):
        tools = {t["name"]: t["inputSchema"]["properties"] for t in MCP.TOOLS}
        self.assertEqual(set(tools), set(MCP.DISPATCH))
        self.assertIn("crashes", tools["srcds_fetch"]["what"]["enum"])
        self.assertIn("refresh_lua", tools["srcds_deploy"])
        self.assertIn("diagnostics", tools["srcds_status"])
