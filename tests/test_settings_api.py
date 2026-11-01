import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import dashboard_api
import unibase


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "metermesh" / "unibase.sqlite3"
        self.db = unibase.Unibase(self.path)
        self.codex_root = self.root / ".codex"
        (self.codex_root / "sessions").mkdir(parents=True)
        with sqlite3.connect(self.codex_root / "state_5.sqlite") as conn:
            conn.execute("create table threads (id text primary key, rollout_path text, model text)")
        self.db.register_source(unibase.DiscoveredSource(
            "codex-live", "codex", "live", self.codex_root, "live", "Live Codex",
            True, 1000, None, None, "ready",
        ))
        backup_root = self.root / "backup"
        (backup_root / "sessions").mkdir(parents=True)
        self.db.register_source(unibase.DiscoveredSource(
            "codex-backup", "codex", "normalized_backup", backup_root, "backup-one", "Backup one",
            False, 500, "snapshot-1", "2026-07-16T00:00:00Z", "ready",
        ))

        class Handler(dashboard_api.DashboardHandler):
            pass

        Handler.unibase_path = self.path
        Handler.db_path = self.codex_root / "state_5.sqlite"
        Handler.claude_projects_path = self.root / ".claude" / "projects"
        Handler.opencode_db_path = self.root / "opencode" / "opencode.db"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, method="GET", payload=None, content_type="application/json", cookie=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_get_and_apply_settings_are_safe_and_revisioned(self):
        self.db.upsert_source_file("codex-live", "file:safe", "codex_rollout", size=2 * 1024 * 1024, mtime_ns=1)
        status, payload = self.request("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(payload["revision"], 1)
        self.assertTrue(payload["merge_models_across_providers"])
        self.assertEqual(payload["non_working_weekdays"], [5, 6])
        self.assertEqual(len(payload["sources"]["codex"]), 2)
        self.assertTrue(payload["sources"]["codex"][0]["original"])
        self.assertEqual(payload["sources"]["codex"][0]["size_bytes"], 2 * 1024 * 1024)
        self.assertIsNone(payload["unibase"]["fresh_at"])
        self.assertNotIn("ignore_failed_requests", payload)
        self.assertNotIn("ignore_codex_auto_review", payload)
        self.assertNotIn("experimental_codex_deduplication", payload)
        encoded = json.dumps(payload)
        self.assertNotIn(str(self.root), encoded)

        sources = [
            {"source_id": source["source_id"], "enabled": source["enabled"]}
            for provider_sources in payload["sources"].values()
            for source in provider_sources
        ]
        next(source for source in sources if source["source_id"] == "codex-backup")["enabled"] = True
        body = {
            "revision": payload["revision"],
            "merge_models_across_providers": True,
            "non_working_weekdays": [5, 6],
            "sources": sources,
            "models": [],
        }
        with patch.object(dashboard_api, "schedule_enabled_sources_refresh", return_value=True):
            status, applied = self.request("/api/settings", "POST", body)
            self.assertEqual(status, 200)
            self.assertTrue(applied["merge_models_across_providers"])
            self.assertTrue(applied["sources"]["codex"][1]["enabled"])

            status, conflict = self.request("/api/settings", "POST", body)
        self.assertEqual(status, 409)
        self.assertIn("error", conflict)

    def test_models_are_grouped_globally_and_disabled_across_stats_and_requests(self):
        for provider in ("claude", "opencode"):
            self.db.register_source(unibase.DiscoveredSource(
                f"{provider}-live", provider, "live", self.root / f".{provider}", "live",
                f"Live {provider}", True, 1000, None, None, "ready",
            ))

        def add_event(provider, event_key, model, occurred_at):
            self.db.add_event(f"{provider}-live", None, {
                "provider": provider,
                "event_key": event_key,
                "stream_key": f"stream-{event_key}",
                "timestamp_utc": "2026-07-16T12:00:00Z",
                "occurred_at": occurred_at,
                "model": model,
                "native_provider_id": provider,
                "semantics": "exact",
                "classification": "usage_update",
                "input_tokens": 10,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 1,
                "reasoning_tokens": 0,
                "cost_usd": None,
                "cost_kind": "unavailable",
            }, 1)

        add_event("codex", "gpt-codex", "GPT-TEST", 1784203200)
        add_event("opencode", "gpt-opencode", "GPT-TEST", 1784203260)
        add_event("claude", "claude", "claude-test", 1784203320)
        add_event("codex", "other", "custom-model", 1784203380)
        self.db.rebuild_active_events()

        status, payload = self.request("/api/settings")

        self.assertEqual(status, 200)
        self.assertEqual([item["model"] for item in payload["models"]["gpt"]], ["GPT-TEST"])
        self.assertEqual([item["model"] for item in payload["models"]["claude"]], ["claude-test"])
        self.assertEqual([item["model"] for item in payload["models"]["others"]], ["custom-model"])

        sources = [
            {"source_id": source["source_id"], "enabled": source["enabled"]}
            for provider_sources in payload["sources"].values()
            for source in provider_sources
        ]
        models = [
            {"model": model["model"], "enabled": model["model"] != "GPT-TEST"}
            for group in payload["models"].values()
            for model in group
        ]
        with patch.object(dashboard_api, "schedule_enabled_sources_refresh", return_value=True):
            status, applied = self.request("/api/settings", "POST", {
                "revision": payload["revision"],
                "merge_models_across_providers": False,
                "non_working_weekdays": [5, 6],
                "sources": sources,
                "models": models,
            })

        self.assertEqual(status, 200)
        self.assertFalse(applied["models"]["gpt"][0]["enabled"])
        pricing = {
            "source": "test", "url": "", "loaded_at": "now", "models": {},
            "fallback": dashboard_api.FALLBACK_PRICING, "error": None,
        }
        with patch.object(dashboard_api, "load_pricing", return_value=pricing):
            stats = dashboard_api.load_unibase_usage(self.path, "all", chart_range="all", provider="all")
            requests = dashboard_api.load_requests(self.path, provider="all")
        self.assertNotIn("GPT-TEST", {row["model"].split(" · ")[-1] for row in stats["models"]})
        self.assertNotIn("GPT-TEST", {item["model"].split(" · ")[-1] for item in requests["items"]})
        self.assertEqual(stats["totals"]["total_tokens"], 22)
        self.assertEqual(stats["favorite_model"], "claude-test")

    def test_post_requires_json_and_strict_fields(self):
        status, _ = self.request("/api/settings", "POST", {"revision": 1}, content_type="text/plain")
        self.assertEqual(status, 400)
        status, _ = self.request("/api/settings", "POST", {"revision": 1})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/settings", "POST", {
            "revision": 1,
            "merge_models_across_providers": False,
            "non_working_weekdays": [5, 6],
            "sources": [None],
            "models": [],
        })
        self.assertEqual(status, 400)

    def test_calendar_only_settings_change_skips_index_maintenance(self):
        status, payload = self.request("/api/settings")
        self.assertEqual(status, 200)
        sources = [
            {"source_id": source["source_id"], "enabled": source["enabled"]}
            for provider_sources in payload["sources"].values()
            for source in provider_sources
        ]
        models = [
            {"model": model["model"], "enabled": model["enabled"]}
            for group in payload["models"].values()
            for model in group
        ]
        body = {
            "revision": payload["revision"],
            "merge_models_across_providers": payload["merge_models_across_providers"],
            "non_working_weekdays": [2, 6],
            "sources": sources,
            "models": models,
        }

        with patch.object(dashboard_api, "schedule_enabled_sources_refresh") as schedule, patch.object(
            unibase.Unibase, "rebuild_active_events"
        ) as rebuild:
            status, applied = self.request("/api/settings", "POST", body)

        self.assertEqual(status, 200)
        self.assertEqual(applied["non_working_weekdays"], [2, 6])
        schedule.assert_not_called()
        rebuild.assert_not_called()

    def test_settings_reject_invalid_non_working_weekdays(self):
        status, payload = self.request("/api/settings")
        self.assertEqual(status, 200)
        sources = [
            {"source_id": source["source_id"], "enabled": source["enabled"]}
            for provider_sources in payload["sources"].values()
            for source in provider_sources
        ]
        for weekdays in ([1, 1], [-1], [7], list(range(7))):
            with self.subTest(weekdays=weekdays):
                status, _ = self.request("/api/settings", "POST", {
                    "revision": payload["revision"],
                    "merge_models_across_providers": False,
                    "non_working_weekdays": weekdays,
                    "sources": sources,
                    "models": [],
                })
                self.assertEqual(status, 400)

    def test_model_inventory_excludes_unknown_and_survives_event_reset(self):
        for event_key, model in (("known", "remembered-model"), ("unknown", "(unknown)")):
            self.db.add_event("codex-live", None, {
                "provider": "codex", "event_key": event_key, "stream_key": event_key,
                "timestamp_utc": "2026-07-16T12:00:00Z", "occurred_at": 1784203200,
                "model": model, "native_provider_id": "openai", "semantics": "exact",
                "classification": "usage_update", "input_tokens": 1, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0,
                "cost_usd": None, "cost_kind": "unavailable",
            }, 1)
        self.db.rebuild_active_events()

        before = dashboard_api.settings_payload(self.path)
        visible = [item["model"] for group in before["models"].values() for item in group]
        self.assertEqual(visible, ["remembered-model"])

        sources = [
            {"source_id": source["source_id"], "enabled": source["enabled"]}
            for provider_sources in before["sources"].values()
            for source in provider_sources
        ]
        self.db.apply_settings(
            before["revision"],
            False,
            sources,
            [{"model": "remembered-model", "enabled": False}],
        )
        self.db.reset()

        after = dashboard_api.settings_payload(self.path)
        remembered = next(item for group in after["models"].values() for item in group)
        self.assertEqual(remembered, {"model": "remembered-model", "enabled": False})

    def test_manual_refresh_runs_incremental_source_scan(self):
        with patch.object(dashboard_api, "schedule_enabled_sources_refresh", return_value=True) as schedule:
            status, _ = self.request("/api/sources/refresh", "POST", {})

        self.assertEqual(status, 202)
        self.assertNotIn("force_full_scan", schedule.call_args.kwargs)
        self.assertEqual(schedule.call_args.kwargs["reason"], "manual incremental refresh")

    def test_manual_refresh_conflicts_when_refresh_is_already_running(self):
        running = {"state": "running", "reason": "periodic", "error": None}
        with patch.object(dashboard_api, "schedule_enabled_sources_refresh", return_value=False), patch.object(
            dashboard_api, "source_refresh_status", return_value=running
        ):
            status, payload = self.request("/api/sources/refresh", "POST", {})

        self.assertEqual(status, 409)
        self.assertIn("already running", payload["error"])
        self.assertEqual(payload["source_sync"], running)

    def test_source_refresh_is_rejected_while_unibase_is_empty(self):
        self.db.reset()

        status, payload = self.request("/api/sources/refresh", "POST", {})

        self.assertEqual(status, 409)
        self.assertIn("empty", payload["error"])
        self.assertFalse(dashboard_api.schedule_enabled_sources_refresh(self.path, reason="startup"))

    def test_scheduler_cooldown_is_measured_from_refresh_completion(self):
        with dashboard_api.SOURCE_REFRESH_STATE_LOCK:
            previous = dashboard_api.SOURCE_REFRESH_COMPLETED_MONOTONIC
            dashboard_api.SOURCE_REFRESH_COMPLETED_MONOTONIC = 100.0
        self.addCleanup(setattr, dashboard_api, "SOURCE_REFRESH_COMPLETED_MONOTONIC", previous)

        self.assertEqual(dashboard_api.source_refresh_cooldown_remaining(100.0), 60.0)
        self.assertEqual(dashboard_api.source_refresh_cooldown_remaining(130.0), 30.0)
        self.assertEqual(dashboard_api.source_refresh_cooldown_remaining(160.0), 0.0)

    def test_scheduler_rechecks_cooldown_after_each_wake_up(self):
        with patch.object(dashboard_api, "source_refresh_running", return_value=False), patch.object(
            dashboard_api, "source_refresh_cooldown_remaining", side_effect=[30.0, 59.0, 0.0]
        ), patch.object(dashboard_api.time, "sleep") as sleep:
            dashboard_api.wait_for_source_refresh_window()

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30.0, 59.0])

    def test_periodic_claim_rechecks_cooldown_atomically(self):
        with dashboard_api.SOURCE_REFRESH_STATE_LOCK:
            previous = dashboard_api.SOURCE_REFRESH_COMPLETED_MONOTONIC
            dashboard_api.SOURCE_REFRESH_COMPLETED_MONOTONIC = 100.0
        self.addCleanup(setattr, dashboard_api, "SOURCE_REFRESH_COMPLETED_MONOTONIC", previous)

        with patch.object(dashboard_api.time, "monotonic", return_value=101.0), patch.object(
            dashboard_api.threading, "Thread"
        ) as thread:
            scheduled = dashboard_api.schedule_enabled_sources_refresh(
                self.path,
                reason="periodic",
                respect_cooldown=True,
            )

        self.assertFalse(scheduled)
        thread.assert_not_called()

    def test_refresh_state_rolls_back_when_worker_thread_cannot_start(self):
        with dashboard_api.SOURCE_REFRESH_STATE_LOCK:
            previous_running = dashboard_api.SOURCE_REFRESH_RUNNING
            previous_error = dashboard_api.SOURCE_REFRESH_ERROR
            dashboard_api.SOURCE_REFRESH_RUNNING = False
            dashboard_api.SOURCE_REFRESH_ERROR = None
        self.addCleanup(setattr, dashboard_api, "SOURCE_REFRESH_RUNNING", previous_running)
        self.addCleanup(setattr, dashboard_api, "SOURCE_REFRESH_ERROR", previous_error)

        with patch.object(dashboard_api.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("thread unavailable")
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                dashboard_api.schedule_enabled_sources_refresh(self.path, reason="manual incremental refresh")

        self.assertFalse(dashboard_api.source_refresh_running())
        self.assertIn("thread unavailable", dashboard_api.source_refresh_status()["error"])

    def test_status_freshness_respects_provider_scope(self):
        self.db.register_source(unibase.DiscoveredSource(
            "claude-live", "claude", "live", self.root / ".claude", "live", "Live Claude",
            True, 1000, None, None, "ready",
        ))
        with self.db.connect() as conn:
            conn.execute(
                "update sources set last_successful_scan = '2026-07-16T12:00:00Z', stale = 0 where source_id = 'codex-live'"
            )
            conn.execute("update sources set stale = 1 where source_id = 'claude-live'")

        status, payload = self.request("/api/unibase/status?provider=codex")

        self.assertEqual(status, 200)
        self.assertEqual(payload["fresh_at"], "2026-07-16T12:00:00Z")

    def test_status_freshness_is_not_pinned_by_skipped_immutable_backup(self):
        with self.db.connect() as conn:
            conn.execute(
                "update sources set last_successful_scan = '2026-07-16T12:00:00Z', stale = 0 where source_id = 'codex-live'"
            )
            conn.execute(
                "update sources set enabled = 1, last_successful_scan = '2026-07-01T12:00:00Z', stale = 0 where source_id = 'codex-backup'"
            )

        for provider in ("codex", "all"):
            status, payload = self.request(f"/api/unibase/status?provider={provider}")
            self.assertEqual(status, 200)
            self.assertEqual(payload["fresh_at"], "2026-07-16T12:00:00Z")

    def test_source_refresh_reports_individual_source_failures(self):
        with patch.object(dashboard_api, "register_default_sources"), patch.object(
            dashboard_api, "import_registered_source", side_effect=RuntimeError("provider unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "codex"):
                dashboard_api.refresh_enabled_sources(self.path, force_full_scan=True)

    def test_periodic_refresh_reparses_sources_with_old_parser_versions(self):
        claude_root = self.root / ".claude-source"
        claude_root.mkdir()
        self.db.register_source(unibase.DiscoveredSource(
            "claude-source", "claude", "normalized_backup", claude_root, "snapshot", "Snapshot",
            True, 500, "snapshot", "2026-07-16T00:00:00Z", "ready",
        ))
        self.db.upsert_source_file(
            "claude-source", "file:safe", "claude_transcript", size=1, mtime_ns=1, parser_version=2
        )
        with self.db.connect() as conn:
            conn.execute(
                "update sources set last_successful_scan = '2026-07-16T00:00:00Z' where source_id = 'claude-source'"
            )

        with patch.object(dashboard_api, "register_default_sources"), patch.object(
            dashboard_api, "import_registered_source"
        ) as import_source:
            dashboard_api.refresh_enabled_sources(self.path)

        imported_ids = {call.args[1]["source_id"] for call in import_source.call_args_list}
        self.assertIn("claude-source", imported_ids)

    def test_stale_live_codex_keeps_checkpoints_while_stale_backups_force_full_scan(self):
        with self.db.connect() as conn:
            conn.execute(
                "update sources set enabled = 1, stale = 1, last_successful_scan = '2026-07-16T00:00:00Z' where provider = 'codex'"
            )
        with patch.object(dashboard_api, "register_default_sources"), patch.object(
            dashboard_api, "import_registered_source"
        ) as import_source:
            dashboard_api.refresh_enabled_sources(self.path)

        live_call = next(call for call in import_source.call_args_list if call.args[1]["source_id"] == "codex-live")
        self.assertFalse(live_call.kwargs["force_full_scan"])
        backup_call = next(call for call in import_source.call_args_list if call.args[1]["source_id"] == "codex-backup")
        self.assertTrue(backup_call.kwargs["force_full_scan"])

    def test_reset_confirmation_rebuilds_and_preserves_settings_and_registry(self):
        rollout = self.codex_root / "sessions" / "2026" / "07" / "16" / "rollout-test.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("\n".join(json.dumps(row) for row in (
            {
                "timestamp": "2026-07-16T11:59:00Z",
                "type": "session_meta",
                "payload": {"type": "session_meta", "id": "session-test", "model": "gpt-test"},
            },
            {
                "timestamp": "2026-07-16T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 5,
                            "cached_input_tokens": 1,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 7,
                        },
                    },
                    "rate_limits": {"used": 1},
                },
            },
        )) + "\n", encoding="utf-8")
        with sqlite3.connect(self.codex_root / "state_5.sqlite") as conn:
            conn.execute("insert into threads values (?, ?, ?)", ("session-test", str(rollout), "gpt-test"))
        self.db.update_settings(1, True)
        status, _ = self.request("/api/unibase/reset", "POST", {"confirmation": "wrong"})
        self.assertEqual(status, 400)
        status, payload = self.request("/api/unibase/reset", "POST", {"confirmation": "RESET UNIBASE"})
        self.assertEqual(status, 202)
        for _ in range(100):
            operation = self.db.operation_status(payload["operation_id"])["operation"]
            if operation["state"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        self.assertEqual(operation["state"], "succeeded")
        settings = dashboard_api.settings_payload(self.path)
        self.assertEqual(settings["unibase"]["state"], "ready")
        self.assertEqual(len(settings["sources"]["codex"]), 2)
        self.assertEqual(settings["unibase"]["counts"]["active_events"], 1)

    def test_staging_reindex_succeeds_and_status_is_pollable(self):
        operation_id = self.db.create_operation("reset")
        dashboard_api.reset_worker(self.path, operation_id)

        status = unibase.Unibase(self.path).operation_status(operation_id)
        self.assertEqual(status["operation"]["state"], "succeeded")
        self.assertTrue(unibase.Unibase(self.path).integrity_check())

    def test_reindex_publish_does_not_break_existing_reader(self):
        reader = self.db.connect(readonly=True)
        self.addCleanup(reader.close)
        reader.execute("begin")
        before = reader.execute("select generation from app_settings where id = 1").fetchone()[0]
        operation_id = self.db.create_operation("reset")

        dashboard_api.reset_worker(self.path, operation_id)

        self.assertEqual(reader.execute("select generation from app_settings where id = 1").fetchone()[0], before)
        self.assertEqual(unibase.Unibase(self.path).operation_status(operation_id)["operation"]["state"], "succeeded")

    def test_reindex_keeps_main_database_when_external_codex_inventory_is_unavailable(self):
        self.db.upsert_source_file(
            "codex-live",
            "external:root:file",
            "codex_rollout",
            size=1,
            mtime_ns=1,
        )
        operation_id = self.db.create_operation("reset")

        dashboard_api.reset_worker(self.path, operation_id, codex_state_db=self.codex_root / "missing.sqlite")

        database = unibase.Unibase(self.path)
        self.assertEqual(database.operation_status(operation_id)["operation"]["state"], "failed")
        self.assertIn("external:root:file", database.source_file_keys("codex-live"))

    def test_reindex_requires_provided_state_inventory_before_external_checkpoints_exist(self):
        operation_id = self.db.create_operation("reset")

        dashboard_api.reset_worker(self.path, operation_id, codex_state_db=self.codex_root / "missing.sqlite")

        database = unibase.Unibase(self.path)
        self.assertEqual(database.operation_status(operation_id)["operation"]["state"], "failed")
        self.assertEqual(len(database.sources("codex")), 2)

    def test_mutations_conflict_with_running_operation(self):
        self.db.create_operation("resync")
        settings = dashboard_api.settings_payload(self.path)
        body = {
            "revision": settings["revision"],
            "merge_models_across_providers": False,
            "non_working_weekdays": [5, 6],
            "sources": [
                {"source_id": "codex-live", "enabled": True},
                {"source_id": "codex-backup", "enabled": False},
            ],
            "models": [],
        }
        status, _ = self.request("/api/settings", "POST", body)
        self.assertEqual(status, 409)
        status, _ = self.request("/api/unibase/reset", "POST", {"confirmation": "RESET UNIBASE"})
        self.assertEqual(status, 409)

    def test_reset_waits_for_import_and_remains_reset_empty(self):
        entered = threading.Event()
        release = threading.Event()
        source = self.db.sources("codex")[0]

        def paused_import(*_args, **_kwargs):
            entered.set()
            release.wait(2)

        with patch.object(dashboard_api, "import_codex_source", side_effect=paused_import):
            import_thread = threading.Thread(target=dashboard_api.import_registered_source, args=(self.db, source))
            import_thread.start()
            self.assertTrue(entered.wait(1))
            reset_thread = threading.Thread(target=self.db.reset)
            reset_thread.start()
            self.assertTrue(reset_thread.is_alive())
            release.set()
            import_thread.join()
            reset_thread.join()

        self.assertEqual(self.db.settings()["state"], "reset_empty")

    def test_resync_is_non_destructive_and_forces_full_source_reads(self):
        self.db.add_event("codex-live", None, {
            "provider": "codex", "event_key": "event-1", "stream_key": "stream-1",
            "timestamp_utc": "2026-07-16T12:00:00Z", "occurred_at": 1784203200,
            "model": "gpt-test", "native_provider_id": "openai", "semantics": "exact",
            "classification": "usage_update", "input_tokens": 1, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 1, "reasoning_tokens": 0,
            "cost_usd": None, "cost_kind": "unavailable",
        }, 1)
        self.db.rebuild_active_events()
        operation_id = self.db.create_operation("resync")

        with patch.object(dashboard_api, "import_registered_source") as import_source:
            dashboard_api.resync_worker(self.path, operation_id)

        self.assertEqual(len(self.db.active_event_rows()), 1)
        self.assertTrue(import_source.call_args.kwargs["force_full_scan"])
        self.assertTrue(import_source.call_args.kwargs["non_destructive"])
        self.assertEqual(self.db.operation_status(operation_id)["operation"]["state"], "succeeded")

    def test_resync_rejects_reset_empty_unibase(self):
        self.db.reset()

        status, payload = self.request("/api/unibase/resync", "POST", {})

        self.assertEqual(status, 409)
        self.assertIn("empty", payload["error"])

    def test_legacy_preference_cookie_is_ignored(self):
        status, _ = self.request("/data.json?provider=codex", cookie="ignore_codex_auto_review_v2=1")

        self.assertEqual(status, 200)
        settings = self.db.settings()
        self.assertFalse(settings["ignore_codex_auto_review"])

    def test_unibase_queries_do_not_apply_legacy_auto_review_filter(self):
        self.db.update_settings(1, True)
        handler = object.__new__(dashboard_api.DashboardHandler)
        handler.unibase_path = self.path
        handler.headers = {}

        filters = handler.filters_from_query("provider=all&ignore_auto_review=0")

        self.assertFalse(filters["ignore_auto_review"])

    def test_settings_discovers_new_backup_without_restart(self):
        snapshot = self.codex_root / "add_stat" / "new-snapshot"
        rollout = snapshot / "root" / "sessions" / "2026" / "07" / "16" / "rollout-a.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n", encoding="utf-8")
        (snapshot / "snapshot.json").write_text(json.dumps({
            "format": unibase.SNAPSHOT_FORMAT,
            "version": 1,
            "id": "new-snapshot",
            "provider": "codex",
            "root": "root",
            "label": "New snapshot",
        }), encoding="utf-8")

        status, payload = self.request("/api/settings")

        self.assertEqual(status, 200)
        self.assertTrue(any(source["label"] == "New snapshot" for source in payload["sources"]["codex"]))


if __name__ == "__main__":
    unittest.main()
