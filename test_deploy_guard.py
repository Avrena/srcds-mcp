"""Offline regression tests: actual host functions, isolated files, no SSH/game access."""
import ast
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("mcp_guard_test", Path(__file__).with_name("srcds_mcp.py"))
MCP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MCP)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_host():
    source = MCP.HOST_DRIVER.rsplit("\nmain()", 1)[0].replace(", pty,", ",")
    namespace = {}
    exec(compile(source, "<isolated-host-driver>", "exec"), namespace)
    return namespace


class Fixture:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="srcds-guard-test-")
        self.root = Path(self.temp.name)
        self.ns = load_host()
        self.ns.update(VOLROOT=str(self.root / "volumes"), BAKROOT=str(self.root / "backups"),
                       SERVERS=[{"logical": "game", "marker": "addons/test_marker"}])
        self.gm = self.root / "volumes/unit/garrysmod"
        (self.gm / "addons/test_marker").mkdir(parents=True)
        (self.gm / "data").mkdir()
        self.history = self.root / "backups/_guard_v2/unit/history"
        self.versions = self.root / "backups/_guard_v2/unit/versions"
        return self

    def __exit__(self, *args):
        self.temp.cleanup()

    def entry(self, path, data, expected):
        return {"to": path, "content_b64": base64.b64encode(data).decode(), "expected_sha256": expected}

    def deploy(self, *entries, **kwargs):
        req = dict(op="deploy", server="game", uuid="unit", files=list(entries),
                   origin={"client_instance": "test", "pid": 1, "tool_version": "2.0.0"})
        req.update(kwargs)
        return self.ns["op_deploy"](req)


class HostGuardTests(unittest.TestCase):
    def test_stale_writer_cannot_erase_other_writers_fix(self):
        with Fixture() as f:
            path = f.gm / "data/value.txt"
            path.write_bytes(b"A=off B=off")
            base = sha(path.read_bytes())
            first = f.deploy(f.entry("data/value.txt", b"A=on B=off", base))
            second = f.deploy(f.entry("data/value.txt", b"A=off B=on", base))
            self.assertTrue(first["ok"], first)
            self.assertFalse(second["ok"], second)
            self.assertIn("STALE_BASE", second["error"])
            self.assertEqual(path.read_bytes(), b"A=on B=off")
            self.assertEqual(len(list(f.versions.iterdir())), 1)

    def test_stale_member_rejects_whole_batch_before_backup_or_write(self):
        with Fixture() as f:
            (f.gm / "data/a").write_bytes(b"a")
            (f.gm / "data/b").write_bytes(b"changed by another writer")
            result = f.deploy(f.entry("data/a", b"new a", sha(b"a")), f.entry("data/b", b"new b", sha(b"b")))
            self.assertFalse(result["ok"])
            self.assertEqual((f.gm / "data/a").read_bytes(), b"a")
            self.assertFalse(f.versions.exists())
            self.assertEqual(len(list(f.history.glob("*.rejected.json"))), 1)

    def test_missing_hash_old_protocol_and_creation_conflict(self):
        with Fixture() as f:
            for expected in (None, "", "abc", 7, "f" * 63):
                result = f.deploy(f.entry("data/new", b"new", expected))
                self.assertFalse(result["ok"], expected)
                self.assertIn("STALE_BASE_REQUIRED", result["error"])
            self.assertFalse((f.gm / "data/new").exists())
            self.assertTrue(f.deploy(f.entry("data/new", b"one", "missing"))["ok"])
            self.assertFalse(f.deploy(f.entry("data/new", b"two", "missing"))["ok"])
            self.assertEqual((f.gm / "data/new").read_bytes(), b"one")

    def test_single_mode_cannot_bypass_guard(self):
        with Fixture() as f:
            req = dict(op="deploy", server="game", uuid="unit", **f.entry("data/x", b"new", "missing"))
            result = f.ns["op_deploy"](req)
            self.assertTrue(result["ok"], result)
            self.assertFalse(f.ns["op_deploy"](req)["ok"])
            self.assertEqual(result["after_sha256"], sha(b"new"))

    def test_all_payloads_decoded_before_any_target_write(self):
        with Fixture() as f:
            result = f.deploy(f.entry("data/a", b"a", "missing"),
                              {"to": "data/b", "expected_sha256": "missing", "content_b64": "not base64!"})
            self.assertFalse(result["ok"])
            self.assertFalse((f.gm / "data/a").exists())

    def test_path_aliases_escapes_and_duplicate_destinations(self):
        with Fixture() as f:
            for path in ("../escape", "/tmp/escape", "data/./x", "data//x", "data/../x", "C:/x", "data\\x", "data/x\x00"):
                with self.subTest(path=path):
                    self.assertFalse(f.deploy(f.entry(path, b"x", "missing"))["ok"])
            result = f.deploy(f.entry("data/x", b"a", "missing"), f.entry("data/x", b"b", "missing"))
            self.assertFalse(result["ok"])
            self.assertFalse((f.gm / "data/x").exists())

    def test_canonical_symlink_duplicates_rejected(self):
        with Fixture() as f:
            (f.gm / "data/x").write_bytes(b"old")
            try:
                (f.gm / "data/link").symlink_to(f.gm / "data/x")
            except OSError:
                self.skipTest("OS does not permit symlink creation")
            result = f.deploy(f.entry("data/x", b"new", sha(b"old")), f.entry("data/link", b"new", sha(b"old")))
            self.assertFalse(result["ok"])
            self.assertIn("duplicate canonical", result["error"])
            self.assertEqual((f.gm / "data/x").read_bytes(), b"old")

    def test_fresh_host_target_rejects_duplicate_changed_and_unknown_mapping(self):
        with Fixture() as f:
            entry = f.entry("data/x", b"new", "missing")
            self.assertFalse(f.deploy(entry, server="wrong")["ok"])
            self.assertFalse(f.deploy(entry, uuid="other")["ok"])
            other = f.root / "volumes/duplicate/garrysmod/addons/test_marker"
            other.mkdir(parents=True)
            result = f.deploy(entry)
            self.assertFalse(result["ok"])
            self.assertIn("AMBIGUOUS", result["error"])
            self.assertFalse((f.gm / "data/x").exists())

    def test_versions_retained_restore_preserves_displaced_version_and_noop(self):
        with Fixture() as f:
            path = f.gm / "data/x"
            path.write_bytes(b"zero")
            first = f.deploy(f.entry("data/x", b"one", sha(b"zero")))
            second = f.deploy(f.entry("data/x", b"two", sha(b"one")))
            for result, original in ((first, b"zero"), (second, b"one")):
                self.assertTrue(result["ok"], result)
                self.assertEqual(Path(result["results"][0]["backup"]).read_bytes(), original)
            entry = {"to": "data/x", "expected_sha256": sha(b"two"), "backup_id": first["deployment_id"]}
            restored = f.deploy(entry, restore=True)
            self.assertTrue(restored["ok"], restored)
            self.assertEqual(path.read_bytes(), b"zero")
            self.assertEqual(Path(restored["results"][0]["backup"]).read_bytes(), b"two")
            stale = f.deploy(entry, restore=True)
            self.assertFalse(stale["ok"])
            no_op = f.deploy(f.entry("data/x", b"zero", sha(b"zero")))
            self.assertTrue(no_op["ok"])
            self.assertTrue(no_op["results"][0]["noop"])
            self.assertEqual(len(list(f.versions.iterdir())), 3)

    def test_restore_requires_explicit_source_and_checks_backup_integrity(self):
        with Fixture() as f:
            path = f.gm / "data/x"
            path.write_bytes(b"old")
            result = f.deploy(f.entry("data/x", b"new", sha(b"old")))
            request = {"to": "data/x", "expected_sha256": sha(b"new")}
            self.assertFalse(f.deploy(request, restore=True)["ok"])
            request["backup_id"] = result["deployment_id"]
            Path(result["results"][0]["backup"]).write_bytes(b"corrupt")
            self.assertFalse(f.deploy(request, restore=True)["ok"])
            self.assertEqual(path.read_bytes(), b"new")

    def test_legacy_restore_preserves_original_and_streams_large_backup(self):
        with Fixture() as f:
            backup = f.root / "backups/unit/data/x"
            backup.parent.mkdir(parents=True)
            backup.write_bytes(b"large" * 1000)
            f.ns["DEPLOY_FILE_MAX_BYTES"] = 1
            result = f.deploy({"to": "data/x", "expected_sha256": "missing", "backup_id": "legacy"}, restore=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual((f.gm / "data/x").read_bytes(), backup.read_bytes())

    def test_backup_failure_prevents_every_target_write(self):
        with Fixture() as f:
            for name in ("a", "b"):
                (f.gm / "data" / name).write_bytes(name.encode())
            original = f.ns["_atomic_copy"]
            def fail_second(src, dst, owned=False):
                if str(src).endswith("b"):
                    raise OSError("simulated disk full")
                return original(src, dst, owned)
            f.ns["_atomic_copy"] = fail_second
            result = f.deploy(f.entry("data/a", b"new a", sha(b"a")), f.entry("data/b", b"new b", sha(b"b")))
            self.assertFalse(result["ok"])
            self.assertEqual((f.gm / "data/a").read_bytes(), b"a")
            self.assertEqual((f.gm / "data/b").read_bytes(), b"b")

    def test_audit_prepare_failure_is_closed_and_result_failure_is_uncertain(self):
        with Fixture() as f:
            original = f.ns["_audit_record"]
            def fail_prepare(root, ident, phase, record):
                raise OSError("simulated audit disk full")
            f.ns["_audit_record"] = fail_prepare
            result = f.deploy(f.entry("data/x", b"new", "missing"))
            self.assertFalse(result["ok"])
            self.assertFalse((f.gm / "data/x").exists())
            def fail_receipt(root, ident, phase, record):
                if phase == "result":
                    raise OSError("simulated receipt disk full")
                original(root, ident, phase, record)
            f.ns["_audit_record"] = fail_receipt
            result = f.deploy(f.entry("data/x", b"new", "missing"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["outcome"], "partial_or_uncertain")
            self.assertEqual((f.gm / "data/x").read_bytes(), b"new")
            self.assertEqual(len(list(f.history.glob("*.prepared.json"))), 1)
            self.assertFalse(list(f.history.glob("*.result.json")))

    def test_mid_batch_io_failure_reports_partial_and_keeps_all_backups(self):
        with Fixture() as f:
            for name in ("a", "b"):
                (f.gm / "data" / name).write_bytes(name.encode())
            original = f.ns["_atomic_write"]
            def fail_second(path, data):
                if str(path).endswith("b"):
                    raise OSError("simulated write failure")
                original(path, data)
            f.ns["_atomic_write"] = fail_second
            result = f.deploy(f.entry("data/a", b"new a", sha(b"a")), f.entry("data/b", b"new b", sha(b"b")))
            self.assertFalse(result["ok"])
            self.assertEqual(result["outcome"], "partial_or_uncertain")
            self.assertEqual(result["n_ok"], 1)
            self.assertEqual((f.gm / "data/a").read_bytes(), b"new a")
            self.assertEqual((f.gm / "data/b").read_bytes(), b"b")
            self.assertEqual(len(list(f.versions.rglob("*") )), 4)  # version, data, a, b

    def test_audit_contains_provenance_and_hashes_but_no_file_payload(self):
        with Fixture() as f:
            secret = b"payload-must-not-be-logged-123"
            entry = f.entry("data/x", secret, "missing")
            entry["source_path"] = "C:/work/source/x"
            result = f.deploy(entry)
            second = f.deploy(f.entry("data/x", b"second", sha(secret)))
            history = f.ns["op_fetch"]({"uuid": "unit", "what": "history", "path": "data/x", "lines": 1})
            self.assertEqual([h["deployment_id"] for h in history["history"]], [second["deployment_id"]])
            next_page = f.ns["op_fetch"]({"uuid": "unit", "what": "history", "path": "data/x", "before": history["before"]})
            self.assertEqual([h["deployment_id"] for h in next_page["history"]], [result["deployment_id"]])
            self.assertIsNone(next_page["before"])
            detail = f.ns["op_fetch"]({"uuid": "unit", "what": "history", "deployment_id": result["deployment_id"]})
            self.assertEqual(detail["files"][0]["source_path"], entry["source_path"])
            combined = "".join(p.read_text() for p in f.history.iterdir())
            self.assertNotIn(secret.decode(), combined)
            self.assertNotIn(base64.b64encode(secret).decode(), combined)
            self.assertIn(sha(secret), combined)
            self.assertIn(result["deployment_id"], combined)

    def test_full_hashes_download_diff_and_missing_file(self):
        with Fixture() as f:
            data = b"first\r\nsecond\n"
            (f.gm / "data/x").write_bytes(data)
            for request in ({"what": "file", "b64": True}, {"what": "file", "lines": 1}):
                result = f.ns["op_fetch"](dict(uuid="unit", path="data/x", **request))
                self.assertEqual(result["sha256"], sha(data))
            hashed = f.ns["op_fetch"]({"uuid": "unit", "path": "data/x", "what": "hash"})
            self.assertEqual(hashed["files"]["x"][0], sha(data))
            absent = f.ns["op_fetch"]({"uuid": "unit", "path": "data/missing", "what": "hash"})
            self.assertEqual(absent["files"]["missing"][0], "missing")
            diff = f.ns["_diff_one"]({"uuid_a": "unit", "path_a": "data/x", "content_b64": base64.b64encode(b"new").decode()})
            self.assertEqual(diff["sha256_a"], sha(data))
            self.assertEqual(diff["sha256_b"], sha(b"new"))

    def test_backup_listing_filters_before_cap_and_prioritizes_versions(self):
        with Fixture() as f:
            legacy = f.root / "backups/unit/old"
            legacy.mkdir(parents=True)
            for index in range(501):
                (legacy / str(index)).write_bytes(b"legacy")
            (f.gm / "data/x").write_bytes(b"old")
            deployed = f.deploy(f.entry("data/x", b"new", sha(b"old")))
            listing = f.ns["op_fetch"]({"uuid": "unit", "what": "backups", "path": "data/x"})
            self.assertEqual(len(listing["backups"]), 1)
            self.assertEqual(listing["backups"][0]["backup_id"], deployed["deployment_id"])
            self.assertFalse(listing["truncated"])
            all_backups = f.ns["op_fetch"]({"uuid": "unit", "what": "backups"})
            self.assertEqual(all_backups["backups"][0]["backup_id"], deployed["deployment_id"])
            self.assertTrue(all_backups["truncated"])

    def test_concurrent_processes_accept_only_one_writer(self):
        with Fixture() as f:
            path = f.gm / "data/x"
            path.write_bytes(b"base")
            worker = f.root / "worker.py"
            source = MCP.HOST_DRIVER.rsplit("\nmain()", 1)[0].replace(", pty,", ",")
            source += "\nVOLROOT = " + repr(f.ns["VOLROOT"])
            source += "\nBAKROOT = " + repr(f.ns["BAKROOT"])
            source += "\nSERVERS = " + repr(f.ns["SERVERS"])
            source += '''
_original_write = _atomic_write
def _atomic_write(path, data):
    time.sleep(0.25)
    _original_write(path, data)
req = json.loads(sys.stdin.read())
time.sleep(max(0, req.pop('start_at') - time.time()))
print(json.dumps(op_deploy(req)))
'''
            worker.write_text(source, encoding="utf-8")
            processes = []
            start_at = time.time() + 0.5
            for value in (b"writer A", b"writer B"):
                req = dict(server="game", uuid="unit", start_at=start_at, files=[f.entry("data/x", value, sha(b"base"))])
                process = subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                process.stdin.write(json.dumps(req).encode())
                process.stdin.close()
                processes.append(process)
            results = []
            for process in processes:
                process.wait(timeout=15)
                output = process.stdout.read()
                errors = process.stderr.read()
                process.stdout.close()
                process.stderr.close()
                self.assertEqual(process.returncode, 0, errors)
                results.append(json.loads(output))
            self.assertEqual(sum(r["ok"] for r in results), 1, results)
            self.assertIn(path.read_bytes(), (b"writer A", b"writer B"))
            self.assertEqual(len(list(f.versions.iterdir())), 1)


class ClientGuardTests(unittest.TestCase):
    def args(self, **overrides):
        args = dict(server=MCP.SERVER_NAMES[0], to="data/x", content="new", expected_sha256="missing", confirm=True)
        args.update(overrides)
        return args

    def test_missing_hash_is_rejected_before_discovery_or_transport(self):
        for request in (self.args(expected_sha256=None),
                        dict(server=MCP.SERVER_NAMES[0], files=[{"to": "data/x", "content": "new"}], confirm=True)):
            with mock.patch.object(MCP, "resolve") as resolve, mock.patch.object(MCP, "run_driver") as run:
                text, error = MCP.tool_deploy(request)
                self.assertTrue(error)
                self.assertIn("STALE_BASE_REQUIRED", text)
                resolve.assert_not_called()
                run.assert_not_called()

    def test_strict_discovery_cannot_fall_back_to_stale_or_ambiguous_target(self):
        server = MCP.SERVER_NAMES[0]
        stale = [{"logical": server, "uuid": "old"}]
        with mock.patch.dict(MCP._disc_cache, {"t": time.time(), "data": stale}), mock.patch.object(MCP, "run_driver", return_value={"error": "unreachable"}):
            self.assertIsNone(MCP.resolve(server, fresh=True))
        with mock.patch.object(MCP, "run_driver", return_value={"servers": stale + [{"logical": server, "uuid": "new"}]}):
            self.assertIsNone(MCP.resolve(server, fresh=True))

    def test_single_and_batch_transport_hashes_and_origin_without_loss(self):
        for request in (self.args(), dict(server=MCP.SERVER_NAMES[0], files=[{"to": "data/x", "content": "new", "expected_sha256": "missing"}], confirm=True)):
            with mock.patch.object(MCP, "resolve", return_value={"uuid": "unit"}) as resolve, \
                 mock.patch.object(MCP, "run_driver", return_value={"ok": False, "error": "test", "deployment_id": "test"}) as run, \
                 mock.patch.object(MCP, "log_event"):
                MCP.tool_deploy(request)
                resolve.assert_called_once_with(request["server"], fresh=True)
                sent = run.call_args.args[0]
                entry = sent["files"][0] if "files" in sent else sent
                self.assertEqual(entry["expected_sha256"], "missing")
                self.assertEqual(sent["server"], request["server"])
                self.assertEqual(sent["origin"]["tool_version"], "2.0.0")
                self.assertEqual(base64.b64decode(entry["content_b64"]), b"new")

    def test_unsafe_or_ambiguous_client_inputs_do_not_deploy(self):
        for request in (self.args(backup=False), self.args(local="both"), self.args(to="data/./x"),
                        self.args(restore=True, backup_id="legacy"), self.args(source_revision="not-a-commit"),
                        self.args(confirm=False)):
            with mock.patch.object(MCP, "run_driver") as run:
                self.assertTrue(MCP.tool_deploy(request)[1], request)
                run.assert_not_called()

    def test_schema_publishes_required_hash_and_history(self):
        schemas = {t["name"]: t["inputSchema"] for t in MCP.TOOLS}
        schema = schemas["srcds_deploy"]
        self.assertIn("expected_sha256", schema["properties"]["files"]["items"]["required"])
        self.assertIn("expected_sha256", schema["oneOf"][1]["required"])
        self.assertIn("history", schemas["srcds_fetch"]["properties"]["what"]["enum"])
        self.assertIn("reconcile", MCP.MCP_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
