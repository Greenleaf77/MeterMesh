import sqlite3
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import codex_usage
import dashboard_api
import unibase


class SourceAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "index.sqlite3"
        self.db = unibase.Unibase(self.path)
        self.codex = self.root / "codex"
        (self.codex / "sessions").mkdir(parents=True)
        self.state = self.codex / "custom-state.sqlite"
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("create table threads(id text, rollout_path text, model text)")
        self.claude = self.root / "claude"
        self.opencode = self.root / "opencode" / "custom-usage.sqlite"

    def refresh(self, **kwargs):
        dashboard_api.refresh_enabled_sources(
            self.path, self.opencode, codex_root=self.codex,
            codex_state_db=self.state, claude_root=self.claude,
            **kwargs,
        )

    def test_incomplete_codex_inventory_keeps_incremental_checkpoints(self):
        def append_usage(path, amount):
            with path.open("a") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-11-01T12:00:00Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": amount},
                    }},
                }) + "\n")

        old = self.codex / "sessions" / "rollout-old.jsonl"
        growing = self.codex / "sessions" / "rollout-growing.jsonl"
        append_usage(old, 7)
        append_usage(growing, 11)
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("insert into threads values (?, ?, ?)", ("old", str(old), "gpt-test"))
        self.refresh()
        old.unlink()
        self.refresh()  # Inventory becomes incomplete; retained history makes it stale.

        with patch.object(codex_usage, "_file_hash", wraps=codex_usage._file_hash) as hashed:
            self.refresh()
            hashed.assert_not_called()

        new = self.codex / "sessions" / "rollout-new.jsonl"
        append_usage(growing, 13)
        append_usage(new, 17)
        with patch.object(codex_usage, "scan_rollout_deduplicated_usage", wraps=codex_usage.scan_rollout_deduplicated_usage) as parsed:
            self.refresh()
            self.assertEqual({call.args[0] for call in parsed.call_args_list}, {growing, new})
        self.assertEqual(sum(row["input_tokens"] for row in self.db.active_event_rows("codex")), 48)
        source = self.db.sources("codex")[0]
        self.assertTrue(source["stale"])
        self.assertIn("incomplete", source["error"])

        with patch.object(codex_usage, "scan_rollout_deduplicated_usage", wraps=codex_usage.scan_rollout_deduplicated_usage) as parsed:
            self.refresh(force_full_scan=True)
            self.assertEqual({call.args[0] for call in parsed.call_args_list}, {growing, new})
        with self.db.connect() as conn:
            conn.execute("update source_files set parser_version = 0 where source_id = ?", (source["source_id"],))
        with patch.object(codex_usage, "scan_rollout_deduplicated_usage", wraps=codex_usage.scan_rollout_deduplicated_usage) as parsed:
            self.refresh()
            self.assertEqual({call.args[0] for call in parsed.call_args_list}, {growing, new})
        self.assertEqual(sum(row["input_tokens"] for row in self.db.active_event_rows("codex")), 48)

    def install_opencode(self):
        self.opencode.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.opencode)) as conn, conn:
            conn.execute("create table message(id text, session_id text, time_created integer, time_updated integer, data text)")

    def test_uninstalled_providers_do_not_fail_refresh_or_block_freshness(self):
        self.refresh()

        sources = {s["provider"]: s for s in self.db.sources()}
        self.assertEqual(sources["codex"]["discovery_status"], "ready")
        self.assertEqual(sources["claude"]["discovery_status"], "not_installed")
        self.assertEqual(sources["opencode"]["discovery_status"], "not_installed")
        self.assertIsNotNone(dashboard_api.unibase_freshness(self.db))

    def test_rebuild_skips_uninstalled_providers(self):
        unibase.register_default_sources(
            self.db, codex_root=self.codex, claude_root=self.claude,
            opencode_root=self.opencode.parent,
        )
        operation = self.db.create_operation("reset")

        dashboard_api.reset_worker(self.path, operation, self.opencode, self.state)

        self.assertEqual(self.db.operation_status(operation)["operation"]["state"], "succeeded")
        self.assertIsNotNone(dashboard_api.unibase_freshness(self.db))

    def test_provider_installed_later_is_imported_at_custom_path(self):
        self.refresh()
        self.install_opencode()

        self.refresh()

        source = self.db.sources("opencode")[0]
        self.assertEqual(source["discovery_status"], "ready")
        self.assertEqual(source["file_count"], 1)

    def test_missing_previously_indexed_database_still_fails_refresh_and_rebuild(self):
        self.install_opencode()
        self.refresh()
        source_id = self.db.sources("opencode")[0]["source_id"]
        checkpoint_keys = self.db.source_file_keys(source_id)
        self.opencode.unlink()

        with self.assertRaisesRegex(RuntimeError, "opencode"):
            self.refresh()
        operation = self.db.create_operation("reset")
        dashboard_api.reset_worker(self.path, operation, self.opencode, self.state)

        self.assertEqual(self.db.operation_status(operation)["operation"]["state"], "failed")
        self.assertEqual(self.db.source_file_keys(source_id), checkpoint_keys)

    def test_codex_absence_does_not_block_claude_only_installation(self):
        self.state.unlink()
        (self.codex / "sessions").rmdir()
        (self.claude / "projects").mkdir(parents=True)

        self.refresh()
        operation = self.db.create_operation("reset")
        dashboard_api.reset_worker(self.path, operation, self.opencode, self.state)

        self.assertEqual(self.db.operation_status(operation)["operation"]["state"], "succeeded")
        self.assertEqual(self.db.sources("codex")[0]["discovery_status"], "not_installed")
