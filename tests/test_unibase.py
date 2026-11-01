import errno
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import unibase


class UnibaseFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "app" / "unibase.sqlite3"
        self.db = unibase.Unibase(self.db_path)

    def test_path_precedence_and_pragmas(self):
        self.assertEqual(unibase.resolve_unibase_path("/tmp/cli.sqlite", {"METERMESH_UNIBASE_DB": "/tmp/env.sqlite"}), Path("/tmp/cli.sqlite"))
        self.assertEqual(unibase.resolve_unibase_path(None, {"METERMESH_UNIBASE_DB": "/tmp/env.sqlite"}), Path("/tmp/env.sqlite"))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], unibase.SCHEMA_VERSION)
            self.assertEqual(conn.execute("pragma journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("pragma foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("pragma busy_timeout").fetchone()[0], 30000)

    def test_schema_eleven_defaults_to_merged_models(self):
        self.assertEqual(unibase.SCHEMA_VERSION, 11)
        self.assertEqual(json.loads(self.db.settings()["non_working_weekdays"]), [5, 6])
        self.assertEqual(self.db.settings()["merge_models_across_providers"], 1)
        with self.db.connect(readonly=True) as conn:
            self.assertIn("color_slot", {row[1] for row in conn.execute("pragma table_info(known_models)")})

    def test_fnv1a32_utf8_vectors(self):
        self.assertEqual(unibase.fnv1a32_utf8(""), 2166136261)
        self.assertEqual(unibase.fnv1a32_utf8("a"), 3826002220)
        self.assertEqual(unibase.fnv1a32_utf8("gpt-5.6-sol"), 2826131955)

    def test_non_working_weekdays_validation(self):
        self.assertEqual(unibase.normalize_non_working_weekdays([]), [])
        self.assertEqual(unibase.normalize_non_working_weekdays([6, 2]), [2, 6])
        for invalid in (None, [True], [1, 1], [-1], [7], list(range(7))):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                unibase.normalize_non_working_weekdays(invalid)

    def test_invalid_stored_weekdays_fall_back_and_are_repaired_on_apply(self):
        with self.db.connect() as conn:
            conn.execute("update app_settings set non_working_weekdays = 'broken' where id = 1")
        settings = self.db.settings()
        self.assertEqual(unibase.load_non_working_weekdays(settings["non_working_weekdays"]), [5, 6])

        self.db.apply_settings(
            settings["revision"],
            bool(settings["merge_models_across_providers"]),
            [],
            [],
            [5, 6],
        )
        self.assertEqual(self.db.settings()["non_working_weekdays"], "[5,6]")

    def test_version_eight_assigns_existing_model_color_slots(self):
        with self.db.connect() as conn:
            conn.executemany(
                "insert into known_models(model, first_seen_at, last_seen_at) values (?, 'now', 'now')",
                [("gpt-a",), ("gpt-b",), ("gpt-c",)],
            )
            conn.execute("alter table app_settings drop column non_working_weekdays")
            conn.execute("alter table known_models drop column color_slot")
            conn.execute("pragma user_version = 8")

        reopened = unibase.Unibase(self.db_path)

        self.assertEqual(json.loads(reopened.settings()["non_working_weekdays"]), [5, 6])
        with reopened.connect(readonly=True) as conn:
            slots = [row[0] for row in conn.execute("select color_slot from known_models order by model")]
        self.assertEqual(slots, [None, None, None])

    def test_version_nine_reassigns_existing_slots_by_first_seen_then_model(self):
        with self.db.connect() as conn:
            conn.execute("delete from known_models")
            conn.executemany(
                "insert into known_models(model, first_seen_at, last_seen_at, color_slot) values (?, ?, ?, ?)",
                [
                    ("model-c", "2026-07-03", "2026-07-03", 2),
                    ("model-b", "2026-07-01", "2026-07-01", 17),
                    ("model-a", "2026-07-01", "2026-07-01", 9),
                ],
            )
            conn.execute("pragma user_version = 9")

        reopened = unibase.Unibase(self.db_path)
        with reopened.connect(readonly=True) as conn:
            slots = {str(row[0]): row[1] for row in conn.execute("select model, color_slot from known_models")}
        self.assertEqual(slots, {"model-a": None, "model-b": None, "model-c": None})

    def test_version_ten_forces_merge_once_and_clears_legacy_color_slots(self):
        with self.db.connect() as conn:
            conn.execute(
                "insert into known_models(model, first_seen_at, last_seen_at, color_slot) values ('model-a', 'now', 'now', 7)"
            )
            conn.execute("update app_settings set merge_models_across_providers = 0")
            before_revision = int(conn.execute("select revision from app_settings where id = 1").fetchone()[0])
            conn.execute("pragma user_version = 10")

        reopened = unibase.Unibase(self.db_path)
        settings = reopened.settings()
        self.assertEqual(settings["merge_models_across_providers"], 1)
        self.assertEqual(settings["revision"], before_revision + 1)
        with reopened.connect(readonly=True) as conn:
            self.assertIsNone(conn.execute("select color_slot from known_models where model = 'model-a'").fetchone()[0])

    def test_existing_database_can_skip_migration_for_read_paths(self):
        with patch.object(unibase.Unibase, "migrate") as migrate:
            database = unibase.Unibase(self.db_path, migrate=False)

        migrate.assert_not_called()
        self.assertEqual(database.settings()["state"], "ready")

    def test_version_five_database_adds_failed_request_columns(self):
        with self.db.connect() as conn:
            conn.execute("alter table app_settings drop column ignore_failed_requests")
            conn.execute("alter table event_variants drop column failed")
            conn.execute("alter table active_events drop column failed")
            conn.execute("pragma user_version = 5")

        reopened = unibase.Unibase(self.db_path)

        self.assertFalse(reopened.settings()["ignore_failed_requests"])
        with reopened.connect(readonly=True) as conn:
            self.assertIn("failed", {row[1] for row in conn.execute("pragma table_info(event_variants)")})
            self.assertIn("failed", {row[1] for row in conn.execute("pragma table_info(active_events)")})

    def test_version_seven_adds_model_inventory_and_removes_unknown_sentinel(self):
        with self.db.connect() as conn:
            conn.execute("insert into disabled_models(model, created_at) values ('remembered-model', 'now')")
            conn.execute("insert into disabled_models(model, created_at) values ('(unknown)', 'now')")
            conn.execute("drop table known_models")
            conn.execute("alter table app_settings drop column merge_models_across_providers")
            conn.execute("pragma user_version = 7")

        reopened = unibase.Unibase(self.db_path)

        self.assertTrue(reopened.settings()["merge_models_across_providers"])
        with reopened.connect(readonly=True) as conn:
            self.assertEqual(
                [row[0] for row in conn.execute("select model from known_models order by model")],
                ["remembered-model"],
            )
            self.assertEqual(
                [row[0] for row in conn.execute("select model from disabled_models order by model")],
                ["remembered-model"],
            )

    def test_settings_revision_conflict_and_reset_preserves_registry(self):
        codex = self.root / ".codex"
        claude = self.root / ".claude"
        opencode = self.root / "opencode"
        unibase.register_default_sources(self.db, codex_root=codex, claude_root=claude, opencode_root=opencode)
        settings = self.db.update_settings(1, True)
        self.assertEqual(settings["revision"], 2)
        with self.assertRaises(unibase.RevisionConflict):
            self.db.update_settings(1, False)
        source_ids = [row["source_id"] for row in self.db.sources()]
        self.db.reset()
        self.assertEqual(self.db.settings()["state"], "reset_empty")
        self.assertTrue(self.db.settings()["ignore_codex_auto_review"])
        self.assertEqual([row["source_id"] for row in self.db.sources()], source_ids)

    def test_read_only_root_without_add_stat_registers_live_source(self):
        # A `:ro` container mount of a never-indexed source has no add_stat/, and
        # mkdir answers EROFS rather than EEXIST, so exist_ok cannot absorb it.
        # Discovery must degrade to "no backups" instead of taking the live
        # source down with it.
        codex = self.root / "ro" / ".codex"
        (codex / "sessions").mkdir(parents=True)
        original_mkdir = Path.mkdir

        def mkdir_unless_add_stat(path, *args, **kwargs):
            if path.name == "add_stat":
                raise OSError(errno.EROFS, "read-only file system")
            return original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", autospec=True, side_effect=mkdir_unless_add_stat):
            self.assertEqual(unibase.discover_backup_sources("codex", codex / "add_stat"), [])
            unibase.register_default_sources(
                self.db,
                codex_root=codex,
                claude_root=self.root / ".claude",
                opencode_root=self.root / "opencode",
            )
        codex_live = [
            row for row in self.db.sources()
            if row["provider"] == "codex" and row["kind"] == "live"
        ]
        self.assertEqual(len(codex_live), 1)

    def test_backup_discovery_does_not_hide_unexpected_filesystem_errors(self):
        with patch.object(Path, "mkdir", side_effect=OSError(errno.EIO, "input/output error")):
            with self.assertRaises(OSError) as raised:
                unibase.discover_backup_sources("codex", self.root / "add_stat")
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_default_source_registration_does_not_hide_root_errors(self):
        codex = self.root / "broken" / ".codex"
        original_mkdir = Path.mkdir

        def fail_codex_root(path, *args, **kwargs):
            if path == codex:
                raise OSError(errno.EIO, "input/output error")
            return original_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", autospec=True, side_effect=fail_codex_root), patch.object(
            unibase, "discover_backup_sources", return_value=[]
        ):
            with self.assertRaises(OSError) as raised:
                unibase.register_default_sources(
                    self.db,
                    codex_root=codex,
                    claude_root=self.root / ".claude",
                    opencode_root=self.root / "opencode",
                )
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_normalized_and_legacy_discovery(self):
        add_stat = self.root / "add_stat"
        normalized = add_stat / "normalized"
        rollout = normalized / "root" / "sessions" / "2026" / "07" / "16" / "rollout-a.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n", encoding="utf-8")
        (normalized / "snapshot.json").write_text(json.dumps({
            "format": unibase.SNAPSHOT_FORMAT,
            "version": 1,
            "id": "snapshot-1",
            "provider": "codex",
            "created_at": "2026-07-16T00:00:00Z",
            "label": "Before reset",
            "root": "root",
        }), encoding="utf-8")
        legacy = add_stat / "legacy" / ".codex" / "sessions" / "2026" / "07" / "16"
        legacy.mkdir(parents=True)
        (legacy / "rollout-b.jsonl").write_text("{}\n", encoding="utf-8")

        sources = unibase.discover_backup_sources("codex", add_stat)

        self.assertEqual([source.kind for source in sources], ["legacy_backup", "normalized_backup"])
        self.assertEqual(sources[0].status, "ready")
        self.assertEqual(sources[1].status, "ready")
        self.assertFalse(any(source.enabled for source in sources))

        # A migrated backup can contain only compressed rollouts.
        (legacy / "rollout-b.jsonl").rename(legacy / "rollout-b.jsonl.zst")
        compressed_sources = unibase.discover_backup_sources("codex", add_stat)
        self.assertEqual([source.kind for source in compressed_sources], ["legacy_backup", "normalized_backup"])
        self.assertEqual(compressed_sources[0].status, "ready")

    def test_rediscovery_preserves_existing_live_source_status(self):
        source = unibase.DiscoveredSource(
            "live", "codex", "live", self.root, "live", "Live", True, 1000, None, None, "not_indexed"
        )
        self.db.register_source(source)
        with self.db.connect() as conn:
            conn.execute(
                "update sources set discovery_status = 'ready', last_successful_scan = '2026-07-16T12:00:00Z' where source_id = 'live'"
            )

        self.db.register_source(source)

        self.assertEqual(self.db.sources("codex")[0]["discovery_status"], "ready")

    def test_manifest_path_traversal_is_incomplete(self):
        child = self.root / "add_stat" / "bad"
        child.mkdir(parents=True)
        (child / "snapshot.json").write_text(json.dumps({
            "format": unibase.SNAPSHOT_FORMAT,
            "version": 1,
            "id": "bad",
            "provider": "claude",
            "root": "../outside",
        }), encoding="utf-8")
        source = unibase.discover_backup_sources("claude", child.parent)[0]
        self.assertEqual(source.status, "incomplete")

    def test_source_sqlite_is_query_only(self):
        source = self.root / "source.sqlite"
        conn = sqlite3.connect(source)
        try:
            conn.execute("create table data(value text)")
            conn.commit()
        finally:
            conn.close()
        with unibase.open_source_sqlite_readonly(source) as conn:
            self.assertEqual(conn.execute("pragma query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("insert into data values ('no')")

    def test_provenance_survives_source_toggle(self):
        now = "2026-07-16T12:00:00Z"
        for name, priority in (("live", 1000), ("backup", 500)):
            self.db.register_source(unibase.DiscoveredSource(name, "claude", "live" if name == "live" else "normalized_backup", self.root, name, name, True, priority, None, None, "ready"))
            self.db.add_event(name, None, {
                "provider": "claude", "event_key": "event-1", "stream_key": "session-1",
                "timestamp_utc": now, "occurred_at": 1784203200, "model": "claude-test",
                "native_provider_id": "anthropic", "semantics": "metadata", "classification": "usage_update",
                "input_tokens": 10, "cache_read_tokens": 2, "cache_write_tokens": 3,
                "output_tokens": 4, "reasoning_tokens": 0, "cost_usd": None, "cost_kind": "estimated",
            }, 1)
        self.db.rebuild_active_events()
        self.assertEqual(len(self.db.active_event_rows()), 1)
        self.db.set_source_enabled("backup", False)
        self.assertEqual(len(self.db.active_event_rows()), 1)
        self.db.set_source_enabled("live", True)
        self.assertEqual(len(self.db.active_event_rows()), 1)

    def test_incremental_projection_updates_only_dirty_event_keys(self):
        self.db.register_source(unibase.DiscoveredSource(
            "live", "codex", "live", self.root, "live", "live",
            True, 1000, None, None, "ready",
        ))
        for index in (1, 2):
            self.db.add_event("live", None, {
                "provider": "codex", "event_key": f"event-{index}", "stream_key": f"stream-{index}",
                "timestamp_utc": "2026-07-16T12:00:00Z", "occurred_at": 1784203200 + index,
                "model": "gpt-test", "native_provider_id": "openai", "semantics": "exact",
                "classification": "usage_update", "input_tokens": index, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0,
                "cost_usd": None, "cost_kind": "unavailable",
            }, 1)
        first_generation = self.db.rebuild_active_events()

        second_generation = self.db.rebuild_active_events({("codex", "event-1")})
        rows = {row["event_key"]: row for row in self.db.active_event_rows("codex")}

        self.assertGreater(second_generation, first_generation)
        self.assertEqual(rows["event-1"]["generation"], second_generation)
        self.assertEqual(rows["event-2"]["generation"], first_generation)

    def test_non_destructive_resync_retains_variants_but_selects_latest_scan(self):
        self.db.register_source(unibase.DiscoveredSource(
            "live", "claude", "live", self.root, "live", "live",
            True, 1000, None, None, "ready",
        ))
        source_file_id = self.db.upsert_source_file("live", "file:safe", "transcript", size=1, mtime_ns=1)
        original = {
            "provider": "claude", "event_key": "event-1", "stream_key": "stream-1",
            "timestamp_utc": "2026-07-16T12:00:00Z", "occurred_at": 1784203200,
            "model": "claude-test", "native_provider_id": "anthropic", "semantics": "claude_metadata",
            "classification": "usage_update", "input_tokens": 1, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0,
            "cost_usd": None, "cost_kind": "unavailable",
        }
        self.db.add_event("live", source_file_id, original, 1)
        self.db.rebuild_active_events()

        self.db.add_events("live", source_file_id, [{**original, "input_tokens": 9}], 2)
        self.db.rebuild_active_events({("claude", "event-1")})

        self.assertEqual(self.db.active_event_rows("claude")[0]["input_tokens"], 9)
        with self.db.connect(readonly=True) as conn:
            self.assertEqual(conn.execute("select count(*) from event_variants").fetchone()[0], 2)

        self.db.add_events("live", source_file_id, [original], 3)
        self.db.rebuild_active_events({("claude", "event-1")})
        self.assertEqual(self.db.active_event_rows("claude")[0]["input_tokens"], 1)

    def test_latest_occurrence_generation_wins_for_duplicate_variant(self):
        self.db.register_source(unibase.DiscoveredSource(
            "live", "claude", "live", self.root, "live", "live",
            True, 1000, None, None, "ready",
        ))
        files = [
            self.db.upsert_source_file("live", f"file:{index}", "transcript", size=1, mtime_ns=index)
            for index in range(3)
        ]
        event = {
            "provider": "claude", "event_key": "event-1", "stream_key": "stream-1",
            "timestamp_utc": "2026-07-16T12:00:00Z", "occurred_at": 1784203200,
            "model": "claude-test", "native_provider_id": "anthropic", "semantics": "claude_metadata",
            "classification": "usage_update", "input_tokens": 1, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0,
            "cost_usd": None, "cost_kind": "unavailable",
        }
        self.db.add_events("live", files[0], [event], 1)
        self.db.add_events("live", files[1], [{**event, "input_tokens": 9}], 2)
        self.db.add_events("live", files[2], [event], 3)

        self.db.rebuild_active_events()

        self.assertEqual(self.db.active_event_rows("claude")[0]["input_tokens"], 1)

    def test_incomplete_reconciliation_marks_source_stale_without_advancing_freshness(self):
        self.db.register_source(unibase.DiscoveredSource(
            "live", "codex", "live", self.root, "live", "live",
            True, 1000, None, None, "ready",
        ))
        with self.db.connect() as conn:
            conn.execute(
                "update sources set last_successful_generation = 1, last_successful_scan = '2026-07-16T12:00:00Z' where source_id = 'live'"
            )

        self.db.reconcile_source_files("live", 2, [], complete=False)

        source = self.db.sources("codex")[0]
        self.assertTrue(source["stale"])
        self.assertEqual(source["last_successful_generation"], 1)
        self.assertEqual(source["last_successful_scan"], "2026-07-16T12:00:00Z")

    def test_failed_atomic_file_replacement_keeps_previous_occurrence(self):
        self.db.register_source(unibase.DiscoveredSource(
            "backup", "claude", "normalized_backup", self.root, "backup", "backup",
            True, 500, None, None, "ready",
        ))
        source_file_id = self.db.upsert_source_file("backup", "session.jsonl", "transcript", size=1, mtime_ns=1)
        valid = {
            "provider": "claude", "event_key": "event-1", "stream_key": "stream-1",
            "timestamp_utc": "2026-07-16T12:00:00Z", "occurred_at": 1784203200,
            "model": "claude-test", "native_provider_id": "anthropic", "semantics": "claude_metadata",
            "classification": "usage_update", "input_tokens": 10, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0,
            "cost_usd": None, "cost_kind": "unavailable",
        }
        self.db.add_event("backup", source_file_id, valid, 1)
        self.db.rebuild_active_events()

        with self.assertRaises(KeyError):
            self.db.replace_source_file_events("backup", source_file_id, [{"provider": "claude"}], 2)
        self.db.rebuild_active_events()

        self.assertEqual(len(self.db.active_event_rows("claude")), 1)

    def test_failed_source_transaction_rolls_back_partial_projection(self):
        self.db.register_source(unibase.DiscoveredSource(
            "backup", "claude", "normalized_backup", self.root, "backup", "backup",
            True, 500, None, None, "ready",
        ))
        original = {
            "provider": "claude", "event_key": "event-1", "stream_key": "stream-1",
            "timestamp_utc": "2026-07-16T12:00:00Z", "occurred_at": 1784203200,
            "model": "claude-test", "native_provider_id": "anthropic", "semantics": "metadata",
            "classification": "usage_update", "input_tokens": 1, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0,
            "cost_usd": None, "cost_kind": "unavailable",
        }
        self.db.add_event("backup", None, original, 1)
        self.db.rebuild_active_events()

        with self.assertRaises(RuntimeError):
            with self.db.source_transaction():
                self.db.add_event("backup", None, {**original, "input_tokens": 9}, 2)
                self.db.rebuild_active_events()
                raise RuntimeError("failed scan")

        self.assertEqual(self.db.active_event_rows("claude")[0]["input_tokens"], 1)

    def test_only_one_concurrent_operation_can_be_claimed(self):
        barrier = threading.Barrier(8)
        outcomes = []

        def claim():
            barrier.wait()
            try:
                outcomes.append(self.db.create_operation("full_reindex"))
            except unibase.OperationConflict:
                outcomes.append(None)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(value is not None for value in outcomes), 1)

    def test_corrected_normalized_manifest_reuses_registry_row(self):
        add_stat = self.root / "add_stat"
        child = add_stat / "snapshot"
        child.mkdir(parents=True)
        (child / "snapshot.json").write_text("{}", encoding="utf-8")
        first = unibase.discover_backup_sources("codex", add_stat)[0]
        self.db.register_source(first)
        rollout = child / "root" / "sessions" / "2026" / "07" / "16" / "rollout-a.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n", encoding="utf-8")
        (child / "snapshot.json").write_text(json.dumps({
            "format": unibase.SNAPSHOT_FORMAT, "version": 1, "id": "fixed",
            "provider": "codex", "root": "root",
        }), encoding="utf-8")
        corrected = unibase.discover_backup_sources("codex", add_stat)[0]

        self.db.register_source(corrected)

        sources = self.db.sources("codex")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["discovery_status"], "ready")

    def test_version_one_database_migrates_operation_constraint(self):
        with self.db.connect() as conn:
            conn.execute("drop index one_active_operation")
            conn.execute("pragma user_version = 1")
        reopened = unibase.Unibase(self.db_path)
        with reopened.connect(readonly=True) as conn:
            self.assertEqual(conn.execute("pragma user_version").fetchone()[0], unibase.SCHEMA_VERSION)
            indexes = {row[1] for row in conn.execute("pragma index_list(operations)")}
        self.assertIn("one_active_operation", indexes)

    def test_version_two_database_hashes_stored_source_paths(self):
        self.db.register_source(unibase.DiscoveredSource(
            "backup", "claude", "normalized_backup", self.root, "backup", "backup",
            True, 500, None, None, "ready",
        ))
        with self.db.connect() as conn:
            conn.execute(
                "insert into source_files(source_id, relative_path, file_kind) values (?, ?, ?)",
                ("backup", "private-project/private-session.jsonl", "transcript"),
            )
            conn.execute("pragma user_version = 2")

        reopened = unibase.Unibase(self.db_path)

        with reopened.connect(readonly=True) as conn:
            stored = conn.execute("select relative_path from source_files where source_id = 'backup'").fetchone()[0]
        self.assertTrue(stored.startswith("file:"))
        self.assertNotIn("private-project", stored)

    def test_version_three_database_hashes_opencode_cursor_ids(self):
        self.db.register_source(unibase.DiscoveredSource(
            "opencode-live", "opencode", "live", self.root, "live", "Live OpenCode",
            True, 1000, None, None, "ready",
        ))
        with self.db.connect() as conn:
            conn.execute(
                "insert into source_files(source_id, relative_path, file_kind, change_cursor) values (?, ?, ?, ?)",
                ("opencode-live", "file:abc", "opencode_sqlite", json.dumps((123, "raw-private-message-id"))),
            )
            conn.execute("pragma user_version = 3")

        reopened = unibase.Unibase(self.db_path)

        with reopened.connect(readonly=True) as conn:
            cursor = conn.execute("select change_cursor from source_files where source_id = 'opencode-live'").fetchone()[0]
        self.assertNotIn("raw-private-message-id", cursor)
        self.assertIn("cursor:", cursor)


if __name__ == "__main__":
    unittest.main()
