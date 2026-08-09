"""Codex rollout discovery, telemetry reconstruction, and Unibase ingestion."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from unibase import Unibase, open_source_sqlite_readonly, sanitize_error, stable_id


PARSER_VERSION = 3
DEDUP_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
ROLLOUT_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ROLLOUT_FILENAME = re.compile(r"^rollout-.*\.jsonl$")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def normalize_dedup_usage(value: object) -> tuple[int, int, int, int, int]:
    usage = value if isinstance(value, dict) else {}
    return tuple(int(usage.get(name) or 0) for name in DEDUP_USAGE_FIELDS)


def make_dedup_key(usage: object, rate_limits: object) -> str:
    canonical = canonical_json({"usage": normalize_dedup_usage(usage), "rate_limits": rate_limits})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def usage_event(
    stream_key: str,
    model: str,
    timestamp: str,
    usage: dict[str, int],
    source: str,
    classification: str,
    response_id: str | None = None,
) -> dict:
    raw_input = usage["input_tokens"]
    cache_read = usage["cached_input_tokens"]
    occurred_at = int(dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    return {
        "stream_key": stream_key,
        "timestamp_utc": dt.datetime.fromtimestamp(occurred_at, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "occurred_at": occurred_at,
        "model": model,
        "source": source,
        "classification": classification,
        "response_id": response_id,
        "input_tokens": max(raw_input - cache_read, 0),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": 0,
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_output_tokens"],
    }


def scan_rollout_deduplicated_usage(rollout_path: Path, stream_key: str, model: str) -> dict:
    usage_events = []
    diagnostics = {
        "processed_lines": 0,
        "malformed_lines": 0,
        "token_count_events": 0,
        "minimum_timestamp": None,
        "maximum_timestamp": None,
    }
    with rollout_path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            diagnostics["processed_lines"] += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                diagnostics["malformed_lines"] += 1
                continue
            if item.get("type") != "event_msg":
                continue
            payload = item.get("payload") or {}
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            diagnostics["token_count_events"] += 1
            info = payload.get("info") or {}
            usage_value = info.get("last_token_usage") if isinstance(info, dict) else None
            try:
                normalized = normalize_dedup_usage(usage_value)
            except (TypeError, ValueError):
                continue
            timestamp = item.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp:
                continue
            usage = dict(zip(DEDUP_USAGE_FIELDS, normalized))
            try:
                event = usage_event(stream_key, model, timestamp, usage, "deduplicated", "usage_update")
            except ValueError:
                continue
            event["input_tokens"] = max(normalized[0] - normalized[1], 0)
            event["dedup_key"] = make_dedup_key(usage_value, payload.get("rate_limits"))
            event["source_line"] = line_number
            usage_events.append(event)
            normalized_timestamp = event["timestamp_utc"]
            if diagnostics["minimum_timestamp"] is None or normalized_timestamp < diagnostics["minimum_timestamp"]:
                diagnostics["minimum_timestamp"] = normalized_timestamp
            if diagnostics["maximum_timestamp"] is None or normalized_timestamp > diagnostics["maximum_timestamp"]:
                diagnostics["maximum_timestamp"] = normalized_timestamp
    return {"usage_events": usage_events, "diagnostics": diagnostics}


def codex_usage_files(root: Path) -> list[Path]:
    sessions = root / "sessions"
    return sorted(path for path in sessions.glob("**/rollout-*.jsonl") if path.is_file()) if sessions.is_dir() else []


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rollout_metadata(path: Path, content_hash: str) -> tuple[str, str]:
    session_id = None
    model = None
    with path.open(encoding="utf-8", errors="ignore") as handle:
        for _ in range(80):
            line = handle.readline()
            if not line:
                break
            if "session_meta" not in line and '"model"' not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            if payload.get("type") == "session_meta" or item.get("type") == "session_meta":
                session_id = payload.get("id") or payload.get("session_id")
                model = payload.get("model") or model
            model = payload.get("model") or model
            if session_id and model:
                break
    if not session_id:
        match = ROLLOUT_UUID.search(str(path).replace("\\", "/"))
        session_id = match.group(0) if match else content_hash
    return stable_id("codex", "stream", session_id), str(model or "(unknown)")


def _validated_state_rollout(value: object) -> tuple[Path, Path, str] | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or not ROLLOUT_FILENAME.fullmatch(path.name):
        return None
    session_indexes = [
        index for index, part in enumerate(path.parts)
        if part in {"sessions", "archived_sessions"}
    ]
    if not session_indexes:
        return None
    try:
        sessions_root = Path(*path.parts[: session_indexes[-1] + 1]).resolve()
        resolved = path.resolve()
        relative = resolved.relative_to(sessions_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved, sessions_root, relative


def _state_rollouts(state_path: Path) -> tuple[dict[Path, str], bool]:
    if not state_path.is_file():
        return {}, False
    try:
        with open_source_sqlite_readonly(state_path) as conn:
            columns = {row[1] for row in conn.execute("pragma table_info(threads)")}
            if "rollout_path" not in columns:
                return {}, False
            model_sql = "model" if "model" in columns else "null"
            rows = conn.execute(
                f"select rollout_path, {model_sql} model from threads where rollout_path != ''"
            ).fetchall()
    except Exception:
        return {}, False

    rollouts: dict[Path, str] = {}
    complete = True
    for row in rows:
        validated = _validated_state_rollout(row["rollout_path"])
        if validated is None:
            complete = False
            continue
        path, _, _ = validated
        rollouts[path] = str(row["model"] or "(unknown)")
    return rollouts, complete


def import_codex_source(
    unibase: Unibase,
    source: dict,
    *,
    state_path: Path | None = None,
    require_state_inventory: bool = False,
    force_full_scan: bool = False,
    non_destructive: bool = False,
) -> dict[str, object]:
    source_id = str(source["source_id"])
    root = Path(source["root_path"])
    if not root.exists():
        error = FileNotFoundError("Codex source is unavailable")
        unibase.mark_source_error(source_id, error)
        raise error
    default_files = codex_usage_files(root)
    is_live = source.get("kind") == "live"
    state_rollouts, state_inventory_complete = _state_rollouts(
        state_path or root / "state_5.sqlite"
    )
    backup_models = {path.name: model for path, model in state_rollouts.items()} if not is_live else {}

    inventory: list[tuple[Path, str]] = []
    default_paths = set()
    for path in default_files:
        resolved = path.resolve()
        default_paths.add(resolved)
        relative = path.relative_to(root).as_posix()
        inventory.append((path, f'file:{stable_id("codex", "source-file", relative)}'))
    for path in (sorted(state_rollouts) if is_live else []):
        if path in default_paths:
            continue
        validated = _validated_state_rollout(str(path))
        if validated is None:
            state_inventory_complete = False
            continue
        resolved, sessions_root, relative = validated
        root_key = stable_id("codex", "rollout-root", str(sessions_root))
        file_key = stable_id("codex", "source-file", relative)
        external_key = f"external:{root_key}:{file_key}"
        if not resolved.is_file():
            state_inventory_complete = False
            continue
        inventory.append((resolved, external_key))

    if require_state_inventory and not state_inventory_complete:
        raise RuntimeError("Codex state inventory is unavailable or incomplete")

    scan_generation = unibase.begin_source_scan(source_id)
    parser_version = PARSER_VERSION
    seen_paths = []
    parsed_files = 0
    imported = 0
    processed_lines = 0
    malformed_lines = 0
    token_count_events = 0
    minimum_timestamp = None
    maximum_timestamp = None
    dirty_event_keys: set[tuple[str, str]] = set()
    try:
        for path, file_key in inventory:
            seen_paths.append(file_key)
            stat = path.stat()
            previous = unibase.file_checkpoint(source_id, file_key)
            if (
                previous
                and not force_full_scan
                and int(previous.get("parser_version") or 0) == parser_version
                and int(previous["size"]) == stat.st_size
                and int(previous["mtime_ns"]) == stat.st_mtime_ns
            ):
                continue
            content_hash = _file_hash(path)
            unchanged = bool(
                previous
                and not force_full_scan
                and int(previous.get("parser_version") or 0) == parser_version
                and previous.get("content_hash") == content_hash
                and int(previous["size"]) == stat.st_size
            )
            if unchanged:
                unibase.upsert_source_file(
                    source_id, file_key, "codex_rollout", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    complete_offset=stat.st_size, content_hash=content_hash, scan_generation=scan_generation,
                    parser_version=parser_version,
                )
                continue
            unibase.register_content_blob(content_hash, stat.st_size, "codex", parser_version)
            source_file_id = int(previous["source_file_id"]) if previous else unibase.upsert_source_file(
                source_id, file_key, "codex_rollout", size=0, mtime_ns=0, content_hash=None,
                scan_generation=scan_generation, parser_version=parser_version,
            )
            stream_key, metadata_model = rollout_metadata(path, content_hash)
            model = state_rollouts.get(path.resolve(), backup_models.get(path.name, metadata_model))
            telemetry = scan_rollout_deduplicated_usage(path, stream_key, model)
            diagnostics = telemetry["diagnostics"]
            processed_lines += int(diagnostics["processed_lines"])
            malformed_lines += int(diagnostics["malformed_lines"])
            token_count_events += int(diagnostics["token_count_events"])
            file_minimum = diagnostics["minimum_timestamp"]
            file_maximum = diagnostics["maximum_timestamp"]
            if file_minimum and (minimum_timestamp is None or file_minimum < minimum_timestamp):
                minimum_timestamp = file_minimum
            if file_maximum and (maximum_timestamp is None or file_maximum > maximum_timestamp):
                maximum_timestamp = file_maximum
            parsed_events = []
            for item in telemetry["usage_events"]:
                event_key = item["dedup_key"]
                parsed_events.append({
                    "provider": "codex",
                    "event_key": event_key,
                    "stream_key": stream_key,
                    "timestamp_utc": item["timestamp_utc"],
                    "occurred_at": item["occurred_at"],
                    "model": model,
                    "native_provider_id": "openai",
                    "semantics": "codex_global_dedup",
                    "classification": item["classification"],
                    "input_tokens": item["input_tokens"],
                    "cache_read_tokens": item["cache_read_tokens"],
                    "cache_write_tokens": 0,
                    "output_tokens": item["output_tokens"],
                    "reasoning_tokens": item["reasoning_tokens"],
                    "cost_usd": None,
                    "cost_kind": "unavailable",
                })
                imported += 1
            if non_destructive:
                dirty_event_keys.update(
                    unibase.add_events(source_id, source_file_id, parsed_events, scan_generation)
                )
            else:
                dirty_event_keys.update(
                    unibase.replace_source_file_events(source_id, source_file_id, parsed_events, scan_generation)
                )
            unibase.upsert_source_file(
                source_id, file_key, "codex_rollout", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                complete_offset=stat.st_size, content_hash=content_hash, scan_generation=scan_generation,
                parser_version=parser_version,
            )
            parsed_files += 1
        if is_live and not state_inventory_complete:
            seen_paths.extend(
                key for key in unibase.source_file_keys(source_id, file_kind="codex_rollout")
                if key.startswith("external:")
            )
        if non_destructive:
            seen_paths.extend(key for key in unibase.source_file_keys(source_id) if key not in seen_paths)
        unibase.reconcile_source_files(
            source_id,
            scan_generation,
            seen_paths,
            rebuild_active=False,
            dirty_event_keys=dirty_event_keys,
            complete=not non_destructive,
        )
        if is_live and not state_inventory_complete:
            unibase.mark_source_error(source_id, "Codex state inventory is incomplete; retained committed data")
        active_events = unibase.active_event_rows("codex")
        unique_usage_records = len(active_events)
        return {
            "files": len(set(seen_paths)),
            "scanned_files": parsed_files,
            "processed_files": parsed_files,
            "processed_lines": processed_lines,
            "malformed_lines": malformed_lines,
            "token_count_events": token_count_events,
            "unique_usage_records": unique_usage_records,
            "duplicate_usage_events": max(token_count_events - unique_usage_records, 0),
            "minimum_timestamp": minimum_timestamp,
            "maximum_timestamp": maximum_timestamp,
            "processed_records": imported,
            "new_events": imported,
            "events": len(active_events),
        }
    except Exception as exc:
        unibase.mark_source_error(source_id, sanitize_error(exc) or "Codex import failed")
        raise
