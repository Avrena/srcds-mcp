"""Regression cases for file integrity and truthful deployment receipts."""
from unittest import mock

from test_compact_output import ToolTestCase
from test_deploy_guard import MCP, sha


class ReviewRegressionTests(ToolTestCase):
    def test_grep_finds_a_match_in_the_middle_of_a_large_line(self):
        body = b"x" * (5 << 20) + b"NEEDLE" + b"y" * (2 << 20) + b"\nlast\n"
        self.write("data/large.txt", body)
        text, error = self.h.call("tool_fetch", what="file", path="data/large.txt", grep="NEEDLE")
        self.assertFalse(error, text)
        self.assertIn("matched 1 of 2 lines; showing 1", text)
        self.assertIn("full_file_sha256=" + sha(body), text)
        self.assertIn("...<+%d bytes>" % (len(body.split(b"\n")[0]) - 2000), text)

    def test_grep_matches_across_read_blocks(self):
        body = b"x" * ((1 << 20) - 3) + b"NEEDLE" + b"\n"
        self.write("data/boundary.txt", body)
        text, error = self.h.call("tool_fetch", what="file", path="data/boundary.txt", grep="NEEDLE")
        self.assertFalse(error, text)
        self.assertIn("matched 1 of 1 lines; showing 1", text)

    def test_file_payload_budget_counts_utf8_bytes(self):
        self.write("data/unicode.txt", "\u00e9\u00e9\u00e9\n" * 10)
        result = self.f.ns["op_fetch"]({"uuid": "unit", "what": "file",
                                         "path": "data/unicode.txt", "maxbytes": 12})
        self.assertTrue(result["ok"], result)
        self.assertLessEqual(len(result["content"].encode("utf-8")), 12)
        self.assertEqual(result["last"], 1)

    def test_tiny_file_budget_does_not_return_an_oversized_first_line(self):
        self.write("data/line.txt", "a" * 2000)
        text, error = self.h.call("tool_fetch", what="file", path="data/line.txt", maxbytes=1)
        self.assertTrue(error)
        self.assertIn("maxbytes", text)
        self.assertLess(len(text), 250)

    def test_prepared_receipts_do_not_confirm_writes_or_backups(self):
        self.write("data/value", b"old")
        result = self.f.deploy(self.f.entry("data/value", b"new", sha(b"old")))
        ident = result["deployment_id"]
        (self.f.history / (ident + ".result.json")).unlink()
        result = self.f.ns["_deployment_history"]({"uuid": "unit", "deployment_id": ident})
        self.assertEqual(result["summary"]["counts"].get("replaced"), 0)
        self.assertEqual(result["summary"]["bytes"], 0)
        text, error = self.h.call("tool_fetch", what="history", deployment_id=ident)
        self.assertFalse(error, text)
        self.assertIn("UNCONFIRMED", text)
        self.assertIn("inspect live hashes", text)
        self.assertNotIn("restorable with backup_id", text)

    def test_inline_server_lua_uses_the_same_byte_limit_as_local_files(self):
        with mock.patch.object(MCP, "resolve") as resolve, mock.patch.object(MCP, "run_driver") as run:
            text, error = MCP.tool_lua({"server": MCP.SERVER_NAMES[0], "confirm": True,
                                        "code": "--" + "\u00e9" * 32768})
        self.assertTrue(error, text)
        self.assertIn("UTF-8 bytes", text)
        resolve.assert_not_called()
        run.assert_not_called()
