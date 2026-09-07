import importlib.util
import ast
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock


MODULE_PATH = os.path.join(os.path.dirname(__file__), "srcds_mcp.py")
SPEC = importlib.util.spec_from_file_location("srcds_mcp_under_test", MODULE_PATH)
MCP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MCP)


class SrcdsMcpHardeningTests(unittest.TestCase):
    def setUp(self):
        self.server = MCP.SERVER_NAMES[0]
        self.srv = {
            "logical": self.server,
            "uuid": "test-uuid",
            "running": True,
            "port": 27015,
        }

    def common_patches(self, driver_result):
        return (
            mock.patch.object(MCP, "resolve", return_value=self.srv),
            mock.patch.object(MCP, "run_driver", return_value=driver_result),
            mock.patch.object(MCP, "log_event"),
        )

    def test_console_multiline_lua_is_not_read_only(self):
        reason = MCP.classify_console(
            "status\nlua_run local p=Entity(1); p:SetHealth(1)"
        )
        self.assertIsNotNone(reason)

    def test_lua_dot_call_is_detected(self):
        reason, band = MCP.classify_lua(
            "local p=Entity(1); p.SetHealth(p, 1)"
        )
        self.assertEqual(band, "mutate")
        self.assertIn("SetHealth", reason)

    def test_sql_executable_comment_is_not_read_only(self):
        reason = MCP.classify_db(
            "/*M! DELETE FROM audit_target WHERE 1=0 */;"
        )
        self.assertIsNotNone(reason)

    def test_arbitrary_lua_always_requires_confirmation(self):
        with mock.patch.object(MCP, "resolve", return_value=self.srv), \
             mock.patch.object(MCP, "live_info", return_value=(0, 100, False)), \
             mock.patch.object(MCP, "run_driver") as run:
            text, is_error = MCP.tool_lua(
                {"server": self.server, "code": "return player.GetCount()"}
            )
        self.assertTrue(is_error)
        self.assertIn("confirm=true", text)
        run.assert_not_called()

    def test_arbitrary_mongo_always_requires_confirmation(self):
        with mock.patch.object(MCP, "run_driver") as run:
            text, is_error = MCP.tool_mongo_query(
                {
                    "database": "test",
                    "script": "db['dropDatabase']()",
                }
            )
        self.assertTrue(is_error)
        self.assertIn("confirm=true", text)
        run.assert_not_called()

    def test_read_only_sql_is_wrapped_by_driver_request(self):
        with mock.patch.object(
            MCP,
            "run_driver",
            return_value={"ok": True, "output": "1", "error_out": ""},
        ) as run, mock.patch.object(MCP, "log_event"):
            text, is_error = MCP.tool_db_query({"sql": "SELECT 1"})
        self.assertFalse(is_error, text)
        self.assertTrue(run.call_args.args[0]["read_only"])

    def test_clientlua_requires_explicit_target(self):
        text, is_error = MCP.tool_clientlua(
            {"server": self.server, "code": "print(1)", "confirm": True}
        )
        self.assertTrue(is_error)
        self.assertIn("target", text.lower())

    def test_clientlua_broadcast_requires_explicit_force(self):
        with mock.patch.object(MCP, "resolve", return_value=self.srv):
            text, is_error = MCP.tool_clientlua(
                {
                    "server": self.server,
                    "code": "print(1)",
                    "target": "all",
                    "confirm": True,
                    "broadcast": True,
                }
            )
        self.assertTrue(is_error)
        self.assertIn("force=true", text)

    def test_clientlua_caps_utf8_bytes(self):
        text, is_error = MCP.tool_clientlua(
            {
                "server": self.server,
                "code": "界" * (MCP.CLIENTLUA_MAX_BYTES // 3 + 1),
                "target": "76561198000000000",
                "confirm": True,
            }
        )
        self.assertTrue(is_error)
        self.assertIn("too large", text.lower())

    def test_clientlua_no_server_output_is_error(self):
        patches = self.common_patches(
            {
                "ok": True,
                "result": {
                    "started": False,
                    "ended": False,
                    "note": "no output file produced",
                },
            }
        )
        with patches[0], patches[1], patches[2]:
            text, is_error = MCP.tool_clientlua(
                {
                    "server": self.server,
                    "code": "print(1)",
                    "target": "76561198000000000",
                    "confirm": True,
                }
            )
        self.assertTrue(is_error)
        self.assertIn("no output", text.lower())

    def test_clientlua_zero_targets_is_error(self):
        patches = self.common_patches(
            {
                "ok": True,
                "result": {
                    "started": True,
                    "ended": True,
                    "ret": "0",
                    "sum": None,
                },
            }
        )
        with patches[0], patches[1], patches[2]:
            text, is_error = MCP.tool_clientlua(
                {
                    "server": self.server,
                    "code": "print(1)",
                    "target": "76561198000000000",
                    "confirm": True,
                }
            )
        self.assertTrue(is_error)
        self.assertIn("zero", text.lower())

    def test_clientlua_uses_async_ack_path(self):
        patches = self.common_patches(
            {
                "ok": True,
                "result": {
                    "started": True,
                    "ended": True,
                    "ret": "1",
                    "out": "clientlua ack: sent=1 ready=1 acked=1 ok=1 compile_error=0 runtime_error=0 transfer_error=0 timeout=0",
                    "sum": "p=1 f=0",
                    "fails": [],
                },
            }
        )
        with patches[0], patches[1] as run, patches[2]:
            text, is_error = MCP.tool_clientlua(
                {
                    "server": self.server,
                    "code": "print(1)",
                    "target": "76561198000000000",
                    "confirm": True,
                }
            )
        self.assertFalse(is_error, text)
        req = run.call_args.args[0]
        self.assertTrue(req["async"])
        self.assertIn("clientlua ack", text)
        for marker in ("@TOKEN@", "@TARGET_LUA@", "@B64@", "@BOOT_B64@",
                       "@ACK_TIMEOUT@", "@MAX_RECIPIENTS@"):
            self.assertNotIn(marker, req["body"])
        token = MCP.re.search(r'local _tok = "([a-f0-9]{16})"', req["body"]).group(1)
        boot_b64 = MCP.re.search(
            r'local _boot = util\.Base64Decode\("([A-Za-z0-9+/=]+)"\)', req["body"]
        ).group(1)
        bootstrap = MCP.base64.b64decode(boot_b64, validate=True).decode("utf-8")
        self.assertIn('local READY="%s"' % token, bootstrap)

    def test_clientlua_ack_failure_is_tool_error(self):
        patches = self.common_patches(
            {
                "ok": True,
                "result": {
                    "started": True,
                    "ended": True,
                    "ret": "1",
                    "out": "clientlua ack: sent=1 ready=1 acked=1 ok=0 compile_error=1 runtime_error=0 transfer_error=0 timeout=0",
                    "sum": "p=0 f=1",
                    "fails": ["client execution acknowledgements incomplete/failed"],
                },
            }
        )
        with patches[0], patches[1], patches[2]:
            text, is_error = MCP.tool_clientlua(
                {
                    "server": self.server,
                    "code": "this is not lua",
                    "target": "76561198000000000",
                    "confirm": True,
                }
            )
        self.assertTrue(is_error)
        self.assertIn("failed or timed out", text)

    def test_clientlua_missing_ack_summary_is_not_success(self):
        patches = self.common_patches(
            {
                "ok": True,
                "result": {
                    "started": True,
                    "ended": True,
                    "ret": "1",
                    "out": "clientlua ack: sent=1 ready=1 acked=1 ok=1 compile_error=0 runtime_error=0 transfer_error=0 timeout=0",
                    "sum": None,
                },
            }
        )
        with patches[0], patches[1], patches[2]:
            text, is_error = MCP.tool_clientlua(
                {
                    "server": self.server,
                    "code": "print(1)",
                    "target": "76561198000000000",
                    "confirm": True,
                }
            )
        self.assertTrue(is_error)
        self.assertIn("acknowledgement summary", text)

    def test_clientlua_inconsistent_ack_counters_are_not_success(self):
        patches = self.common_patches(
            {
                "ok": True,
                "result": {
                    "started": True,
                    "ended": True,
                    "ret": "2",
                    "out": "clientlua ack: sent=1 ready=1 acked=1 ok=1 compile_error=0 runtime_error=0 transfer_error=0 timeout=0",
                    "sum": "p=1 f=0",
                },
            }
        )
        with patches[0], patches[1], patches[2]:
            text, is_error = MCP.tool_clientlua(
                {
                    "server": self.server,
                    "code": "print(1)",
                    "target": "76561198000000000",
                    "confirm": True,
                }
            )
        self.assertTrue(is_error)
        self.assertIn("inconsistent", text)

    def test_power_unknown_population_requires_force(self):
        with mock.patch.object(MCP, "resolve", return_value=self.srv), \
             mock.patch.object(MCP, "live_info", return_value=(None, None, None)), \
             mock.patch.object(MCP, "run_driver") as run:
            text, is_error = MCP.tool_power(
                {"server": self.server, "action": "stop", "confirm": True}
            )
        self.assertTrue(is_error)
        self.assertIn("force=true", text)
        run.assert_not_called()

    def test_fetch_save_to_requires_confirmation_before_remote_read(self):
        with mock.patch.object(MCP, "resolve", return_value=self.srv), \
             mock.patch.object(MCP, "run_driver") as run:
            text, is_error = MCP.tool_fetch(
                {
                    "server": self.server,
                    "what": "file",
                    "path": "data/test.bin",
                    "save_to": os.path.join(tempfile.gettempdir(), "mcp-test.bin"),
                }
            )
        self.assertTrue(is_error)
        self.assertIn("confirm=true", text)
        run.assert_not_called()

    def test_grep_accepts_multiple_filters_in_one_driver_call(self):
        with mock.patch.object(MCP, "resolve", return_value=self.srv), \
             mock.patch.object(
                 MCP,
                 "run_driver",
                 return_value={"ok": True, "matches": [], "total": 0, "shown": 0},
             ) as run, mock.patch.object(MCP, "log_event"):
            text, is_error = MCP.tool_grep(
                {
                    "server": self.server,
                    "patterns": ["foo", "bar"],
                    "globs": ["*.lua", "*.txt"],
                    "exclude_globs": ["*_test.lua"],
                    "paths": ["addons/a", "gamemodes/b"],
                }
            )
        self.assertFalse(is_error, text)
        req = run.call_args.args[0]
        self.assertEqual(req["patterns"], ["foo", "bar"])
        self.assertEqual(req["globs"], ["*.lua", "*.txt"])
        self.assertEqual(req["exclude_globs"], ["*_test.lua"])
        self.assertEqual(req["paths"], ["addons/a", "gamemodes/b"])

    def test_diff_local_confirmation_happens_before_file_read(self):
        with mock.patch.object(MCP, "resolve", return_value=self.srv), \
             mock.patch.object(MCP, "run_driver") as run:
            text, is_error = MCP.tool_diff(
                {
                    "server": self.server,
                    "path": "addons/example/lua/test.lua",
                    "local": os.path.join(tempfile.gettempdir(), "does-not-exist.lua"),
                }
            )
        self.assertTrue(is_error)
        self.assertIn("confirm=true", text)
        run.assert_not_called()

    def test_diff_local_file_size_cap(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"12345")
            local = f.name
        try:
            with mock.patch.object(MCP, "DIFF_FILE_MAX_BYTES", 4):
                entry, error = MCP._diff_local_entry(
                    self.server, self.srv, "addons/example/lua/test.lua", local, 3
                )
            self.assertIsNone(entry)
            self.assertIn("diff cap", error)
        finally:
            os.unlink(local)

    def test_host_driver_compiles_and_confines_prefix_escape(self):
        source = MCP.HOST_DRIVER
        compile(source, "<host-driver>", "exec")
        definitions = source.rsplit("\nmain()", 1)[0]
        tree = ast.parse(definitions, "<host-driver-test>")
        safe_under = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_safe_under"
        )
        namespace = {"os": os}
        module = ast.fix_missing_locations(ast.Module(body=[safe_under], type_ignores=[]))
        exec(compile(module, "<safe-under-test>", "exec"), namespace)
        with tempfile.TemporaryDirectory() as root:
            inside = namespace["_safe_under"](root, "nested/file.lua")
            sibling = os.path.join("..", os.path.basename(root) + "-sibling", "file.lua")
            escaped = namespace["_safe_under"](root, sibling)
        self.assertIsNotNone(inside)
        self.assertIsNone(escaped)

    def test_host_deploy_is_atomic_and_batch_preflights_all_base64(self):
        from test_deploy_guard import Fixture, sha
        with Fixture() as f:
            target = f.gm / "data/value.txt"
            target.write_bytes(b"old")
            result = f.deploy(f.entry("data/value.txt", b"new", sha(b"old")))
            self.assertTrue(result["ok"], result)
            self.assertEqual(target.read_bytes(), b"new")
            restored = f.deploy({"to": "data/value.txt", "expected_sha256": sha(b"new"),
                                 "backup_id": result["deployment_id"]}, restore=True)
            self.assertTrue(restored["ok"], restored)
            self.assertEqual(target.read_bytes(), b"old")
            rejected = f.deploy(f.entry("data/value.txt", b"newer", sha(b"old")),
                                {"to": "data/other.txt", "expected_sha256": "missing", "content_b64": "not base64!"})
            self.assertFalse(rejected["ok"])
            self.assertEqual(target.read_bytes(), b"old")

    def test_tool_schemas_publish_batch_diff_multifilter_grep_and_targeted_clientlua(self):
        schemas = {tool["name"]: tool["inputSchema"] for tool in MCP.TOOLS}
        self.assertIn("files", schemas["srcds_diff"]["properties"])
        self.assertIn("patterns", schemas["srcds_grep"]["properties"])
        self.assertIn("globs", schemas["srcds_grep"]["properties"])
        self.assertIn("paths", schemas["srcds_grep"]["properties"])
        self.assertIn("target", schemas["srcds_clientlua"]["required"])

    def test_audit_record_redacts_payload_and_identity_text(self):
        record = MCP._redact_log_record(
            {
                "code": "print('secret')",
                "target": "76561198000000000",
                "path": "private/path.lua",
            }
        )
        self.assertNotIn("code", record)
        self.assertNotIn("target", record)
        self.assertNotIn("path", record)
        self.assertEqual(record["target_mode"], "specific")

    def test_async_runner_finalizes_synchronous_setup_errors(self):
        self.assertIn("if _ASYNC and _ok and not _finalized then", MCP.RUNNER_TEMPLATE)
        self.assertIn("debug.gethook()", MCP.RUNNER_TEMPLATE)
        self.assertIn("debug.gethook()", MCP.CLIENTLUA_BOOTSTRAP)
        self.assertIn("_scratch.MCP_DONE", MCP.RUNNER_TEMPLATE)
        self.assertIn("_scratch.LOG", MCP.RUNNER_TEMPLATE)
        self.assertNotIn("_G[k] = _saved[k]", MCP.RUNNER_TEMPLATE)

    def test_clientlua_bootstrap_bounds_untrusted_net_metadata(self):
        self.assertIn("#tok~=16", MCP.CLIENTLUA_BOOTSTRAP)
        self.assertIn("rawlen>65536", MCP.CLIENTLUA_BOOTSTRAP)
        self.assertIn("total>2", MCP.CLIENTLUA_BOOTSTRAP)
        self.assertIn("bits>524288", MCP.CLIENTLUA_BOOTSTRAP)
        self.assertIn("bits > 32768", MCP.CLIENTLUA_BODY)
        self.assertIn('net.WriteString("ready")', MCP.CLIENTLUA_BOOTSTRAP)
        self.assertIn("pending.send(ply)", MCP.CLIENTLUA_BODY)
        self.assertIn("announce receiver readiness", MCP.CLIENTLUA_BODY)
        self.assertIn("p:IsFullyAuthenticated()", MCP.CLIENTLUA_BODY)

    def test_example_configuration_is_valid_json(self):
        path = os.path.join(os.path.dirname(__file__), "config.example.json")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        self.assertIn("ssh", config)
        self.assertIn("servers", config)

    def test_initialize_does_not_echo_an_unknown_version(self):
        sent = []
        with mock.patch.object(MCP, "send", side_effect=sent.append):
            MCP.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "1900-01-01"},
                }
            )
        self.assertEqual(sent[0]["result"]["protocolVersion"], MCP.MCP_PROTOCOL_VERSION)


if __name__ == "__main__":
    unittest.main()
