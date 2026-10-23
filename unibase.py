"""MeterMesh app-owned usage index, source registry, and query projection."""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator


SCHEMA_VERSION = 11
DEFAULT_UNIBASE_DB = Path.home() / ".metermesh" / "unibase.sqlite3"
PROVIDERS = ("codex", "claude", "opencode")
SOURCE_PRIORITIES = {"live": 1000, "normalized_backup": 500, "legacy_backup": 400}
SNAPSHOT_FORMAT = "metermesh-provider-snapshot"
MAX_SAFE_ERROR_LENGTH = 240
DEFAULT_NON_WORKING_WEEKDAYS = [5, 6]


def fnv1a32_utf8(value: str) -> int:
    result = 2166136261
    for byte in value.encode("utf-8"):
        result = ((result ^ byte) * 16777619) & 0xFFFFFFFF
    return result


def normalize_non_working_weekdays(value: object) -> list[int]:
    if not isinstance(value, list) or any(type(day) is not int for day in value):
        raise ValueError("Invalid non-working weekdays")
    days = sorted(value)
    if len(days) != len(set(days)) or any(day < 0 or day > 6 for day in days) or len(days) == 7:
        raise ValueError("Invalid non-working weekdays")
    return days


def load_non_working_weekdays(value: object) -> list[int]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return normalize_non_working_weekdays(parsed)
    except (json.JSONDecodeError, TypeError, ValueError):
        return list(DEFAULT_NON_WORKING_WEEKDAYS)


def _assign_model_color_slot_conn(conn: sqlite3.Connection, model: str) -> int:
    row = conn.execute("select color_slot from known_models where model = ?", (model,)).fetchone()
    if row is None:
        raise ValueError("Unknown model")
    if row[0] is not None:
        return int(row[0])
    slot = int(conn.execute(
        "select coalesce(max(color_slot), -1) + 1 from known_models"
    ).fetchone()[0])
    conn.execute("update known_models set color_slot = ? where model = ?", (slot, model))
    return slot


def _assign_missing_model_color_slots_conn(conn: sqlite3.Connection) -> None:
    models = [
        str(row[0])
        for row in conn.execute("select model from known_models where color_slot is null").fetchall()
    ]
    for model in sorted(models, key=lambda value: (fnv1a32_utf8(value), value)):
        _assign_model_color_slot_conn(conn, model)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class BorrowedConnection:
    """Keep nested repository helpers inside one outer source transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql: str, parameters=()):
        if sql.strip().lower().startswith("begin"):
            return self._connection.execute("select 1")
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters):
        return self._connection.executemany(sql, parameters)

    def executescript(self, sql: str):
        return self._connection.executescript(sql)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def resolve_unibase_path(cli_value: Path | str | None = None, environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    value = cli_value or env.get("METERMESH_UNIBASE_DB") or DEFAULT_UNIBASE_DB
    return Path(value).expanduser()


def default_opencode_data_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    xdg_data_home = env.get("XDG_DATA_HOME")
    return (Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share") / "opencode"


def sanitize_error(error: BaseException | str | None) -> str | None:
    if error is None:
        return None
    text = str(error).replace("\r", " ").replace("\n", " ")
    for prefix in (str(Path.home()), os.getcwd()):
        if prefix:
            text = text.replace(prefix, "~")
    text = re.sub(r"(?<![\w.-])(?:[A-Za-z]:[\\/]|/)[^,;\n]+", "<path>", text)
    return text[:MAX_SAFE_ERROR_LENGTH]


def stable_id(*parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return digest[:24]


def safe_display_label(value: object, fallback: str) -> str:
    text = str(value or fallback).replace("\r", " ").replace("\n", " ").strip()
    if "/" in text or "\\" in text:
        text = text.replace("\\", "/").rstrip("/").split("/")[-1]
    return (text or fallback)[:120]


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False, factory=ClosingConnection
        )
        conn.execute("pragma query_only = on")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False, factory=ClosingConnection)
        conn.execute("pragma journal_mode = wal")
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 30000")
    return conn


MIGRATION_1 = """
create table app_settings (
    id integer primary key check (id = 1),
    revision integer not null default 1,
    merge_models_across_providers integer not null default 1,
    ignore_codex_auto_review integer not null default 0,
    experimental_codex_deduplication integer not null default 0,
    ignore_failed_requests integer not null default 0,
    legacy_preference_migrated integer not null default 0,
    non_working_weekdays text not null default '[5,6]',
    generation integer not null default 0,
    state text not null default 'ready',
    created_at text not null,
    updated_at text not null
);

create table disabled_models (
    model text primary key,
    created_at text not null
);

create table known_models (
    model text primary key,
    first_seen_at text not null,
    last_seen_at text not null,
    color_slot integer
);

create table sources (
    source_id text primary key,
    provider text not null check (provider in ('codex', 'claude', 'opencode')),
    kind text not null check (kind in ('live', 'normalized_backup', 'legacy_backup')),
    root_path text not null,
    relative_name text not null,
    label text not null,
    enabled integer not null,
    priority integer not null,
    snapshot_id text,
    snapshot_date text,
    discovery_status text not null default 'not_indexed',
    stale integer not null default 0,
    last_successful_generation integer,
    file_count integer not null default 0,
    event_count integer not null default 0,
    last_successful_scan text,
    error text,
    inventory_signature text,
    stable_inventory_count integer not null default 0,
    created_at text not null,
    updated_at text not null,
    unique(provider, kind, relative_name)
);

create table source_files (
    source_file_id integer primary key,
    source_id text not null references sources(source_id) on delete cascade,
    relative_path text not null,
    file_kind text not null,
    size integer not null default 0,
    mtime_ns integer not null default 0,
    file_generation integer not null default 1,
    complete_offset integer not null default 0,
    content_hash text,
    scan_generation integer not null default 0,
    parser_version integer not null default 1,
    schema_fingerprint text,
    change_cursor text,
    unique(source_id, relative_path)
);

create table content_blobs (
    blob_id integer primary key,
    sha256 text not null,
    size integer not null,
    parsed_provider text,
    parser_version integer,
    created_at text not null,
    unique(sha256, size)
);

create table logical_streams (
    stream_id integer primary key,
    provider text not null,
    stream_key text not null,
    model text,
    native_provider_id text,
    conflict_state text not null default 'clean',
    unique(provider, stream_key)
);

create table record_variants (
    record_variant_id integer primary key,
    stream_id integer not null references logical_streams(stream_id) on delete cascade,
    record_key text not null,
    record_type text not null,
    timestamp_utc text,
    sequence_no integer,
    metadata_json text not null,
    normalized_hash text not null,
    unique(stream_id, record_key, normalized_hash)
);

create table record_occurrences (
    record_variant_id integer not null references record_variants(record_variant_id) on delete cascade,
    source_file_id integer not null references source_files(source_file_id) on delete cascade,
    byte_offset integer,
    scan_generation integer not null,
    primary key(record_variant_id, source_file_id, byte_offset)
);

create table event_variants (
    event_variant_id integer primary key,
    provider text not null,
    event_key text not null,
    stream_key text not null,
    timestamp_utc text not null,
    occurred_at integer not null,
    model text not null,
    native_provider_id text,
    semantics text not null,
    classification text not null,
    input_tokens integer not null default 0,
    cache_read_tokens integer not null default 0,
    cache_write_tokens integer not null default 0,
    output_tokens integer not null default 0,
    reasoning_tokens integer not null default 0,
    cost_usd real,
    cost_kind text not null default 'unavailable',
    failed integer not null default 0,
    payload_hash text not null,
    created_at text not null,
    unique(provider, event_key, payload_hash)
);

create table event_occurrences (
    event_variant_id integer not null references event_variants(event_variant_id) on delete cascade,
    source_id text not null references sources(source_id) on delete cascade,
    source_file_id integer references source_files(source_file_id) on delete cascade,
    scan_generation integer not null,
    primary key(event_variant_id, source_id, source_file_id)
);

create table canonical_events (
    canonical_event_id integer primary key,
    provider text not null,
    event_key text not null,
    conflict_state text not null default 'clean',
    selected_variant_id integer references event_variants(event_variant_id),
    unique(provider, event_key)
);

create index canonical_events_conflicts_provider
on canonical_events(provider) where conflict_state = 'conflict';

create table active_events (
    canonical_event_id integer primary key references canonical_events(canonical_event_id) on delete cascade,
    event_variant_id integer not null references event_variants(event_variant_id),
    generation integer not null,
    provider text not null,
    event_key text not null,
    stream_key text not null,
    timestamp_utc text not null,
    occurred_at integer not null,
    model text not null,
    native_provider_id text,
    semantics text not null,
    classification text not null,
    input_tokens integer not null,
    cache_read_tokens integer not null,
    cache_write_tokens integer not null,
    output_tokens integer not null,
    reasoning_tokens integer not null,
    cost_usd real,
    cost_kind text not null,
    failed integer not null default 0
);

create index active_events_provider_time on active_events(provider, occurred_at desc, canonical_event_id desc);
create index active_events_provider_model_time on active_events(provider, model, occurred_at);
create index active_events_stream on active_events(provider, stream_key);
create index active_events_time on active_events(occurred_at desc, canonical_event_id desc);

create table diagnostic_events (
    diagnostic_id integer primary key,
    provider text not null,
    source_id text,
    kind text not null,
    count integer not null default 1,
    details text,
    generation integer not null,
    created_at text not null
);

create table operations (
    operation_id text primary key,
    kind text not null,
    state text not null,
    progress_current integer not null default 0,
    progress_total integer not null default 0,
    generation integer,
    error text,
    created_at text not null,
    updated_at text not null
);

create unique index one_active_operation
on operations((1)) where state in ('queued', 'running');
"""


class OperationLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    @contextlib.contextmanager
    def acquire(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.RLock())
        with lock:
            yield


OPERATION_LOCKS = OperationLocks()


@dataclass(frozen=True)
class DiscoveredSource:
    source_id: str
    provider: str
    kind: str
    root_path: Path
    relative_name: str
    label: str
    enabled: bool
    priority: int
    snapshot_id: str | None
    snapshot_date: str | None
    status: str
    inventory_signature: str | None = None


class Unibase:
    def __init__(self, path: Path | str, *, migrate: bool = True) -> None:
        self.path = Path(path).expanduser()
        self._local = threading.local()
        if migrate:
            self.migrate()

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        transaction = getattr(self._local, "transaction", None)
        if transaction is not None:
            return BorrowedConnection(transaction)
        return _connect(self.path, readonly=readonly)

    @contextlib.contextmanager
    def source_transaction(self) -> Iterator[None]:
        if getattr(self._local, "transaction", None) is not None:
            yield
            return
        with OPERATION_LOCKS.acquire(f"source-transaction:{self.path}"):
            connection = _connect(self.path)
            connection.execute("begin immediate")
            self._local.transaction = connection
            try:
                yield
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                del self._local.transaction
                connection.close()

    def migrate(self) -> None:
        with OPERATION_LOCKS.acquire(f"db:{self.path}"):
            with _connect(self.path) as conn:
                version = int(conn.execute("pragma user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise RuntimeError(f"Unibase schema {version} is newer than supported schema {SCHEMA_VERSION}")
                if version == 0:
                    try:
                        conn.executescript("begin immediate;\n" + MIGRATION_1)
                        now = utc_now()
                        conn.execute(
                            "insert into app_settings(id, created_at, updated_at) values (1, ?, ?)",
                            (now, now),
                        )
                        conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                elif version == 1:
                    conn.executescript(
                        """
                        begin immediate;
                        update operations
                        set state = 'failed', error = 'Superseded during schema migration'
                        where state in ('queued', 'running')
                          and operation_id not in (
                              select operation_id from operations
                              where state in ('queued', 'running')
                              order by created_at desc limit 1
                          );
                        create unique index if not exists one_active_operation
                        on operations((1)) where state in ('queued', 'running');
                        pragma user_version = 2;
                        commit;
                        """
                    )
                    version = 2
                if version == 2:
                    conn.execute("begin immediate")
                    rows = conn.execute(
                        """
                        select sf.source_file_id, sf.relative_path, s.provider
                        from source_files sf
                        join sources s on s.source_id = sf.source_id
                        """
                    ).fetchall()
                    for row in rows:
                        relative_path = str(row["relative_path"])
                        if relative_path.startswith("file:"):
                            safe_path = relative_path
                        elif re.fullmatch(r"[0-9a-f]{24}", relative_path):
                            safe_path = f"file:{relative_path}"
                        else:
                            safe_path = f'file:{stable_id(row["provider"], "source-file", relative_path)}'
                        conn.execute(
                            "update source_files set relative_path = ? where source_file_id = ?",
                            (safe_path, row["source_file_id"]),
                        )
                    conn.execute("pragma user_version = 3")
                    conn.commit()
                    version = 3
                if version == 3:
                    conn.execute("begin immediate")
                    rows = conn.execute(
                        """
                        select sf.source_file_id, sf.change_cursor
                        from source_files sf
                        join sources s on s.source_id = sf.source_id
                        where s.provider = 'opencode' and sf.change_cursor is not null
                        """
                    ).fetchall()
                    for row in rows:
                        try:
                            timestamp, cursor_id = json.loads(row["change_cursor"])
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        cursor_text = str(cursor_id or "")
                        if cursor_text and not cursor_text.startswith("cursor:"):
                            cursor_text = f'cursor:{stable_id("opencode", "cursor", cursor_text)}'
                        conn.execute(
                            "update source_files set change_cursor = ? where source_file_id = ?",
                            (json.dumps((timestamp, cursor_text)), row["source_file_id"]),
                        )
                    conn.execute("pragma user_version = 4")
                    conn.commit()
                    version = 4
                if version == 4:
                    conn.executescript(
                        """
                        begin immediate;
                        create index if not exists active_events_time
                        on active_events(occurred_at desc, canonical_event_id desc);
                        create index if not exists canonical_events_conflicts_provider
                        on canonical_events(provider) where conflict_state = 'conflict';
                        pragma user_version = 5;
                        commit;
                        """
                    )
                    version = 5
                if version == 5:
                    conn.execute("begin immediate")
                    migrations = (
                        ("app_settings", "ignore_failed_requests", "integer not null default 0"),
                        ("event_variants", "failed", "integer not null default 0"),
                        ("active_events", "failed", "integer not null default 0"),
                    )
                    for table, column, declaration in migrations:
                        columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}
                        if column not in columns:
                            conn.execute(f"alter table {table} add column {column} {declaration}")
                    conn.execute("pragma user_version = 6")
                    conn.commit()
                    version = 6
                if version == 6:
                    conn.execute("begin immediate")
                    columns = {row[1] for row in conn.execute("pragma table_info(app_settings)")}
                    if "experimental_codex_deduplication" not in columns:
                        conn.execute(
                            "alter table app_settings add column experimental_codex_deduplication integer not null default 0"
                        )
                    conn.execute(
                        "create table if not exists disabled_models (model text primary key, created_at text not null)"
                    )
                    conn.execute("pragma user_version = 7")
                    conn.commit()
                    version = 7
                if version == 7:
                    conn.execute("begin immediate")
                    columns = {row[1] for row in conn.execute("pragma table_info(app_settings)")}
                    if "merge_models_across_providers" not in columns:
                        conn.execute(
                            "alter table app_settings add column merge_models_across_providers integer not null default 1"
                        )
                    conn.execute(
                        """
                        create table if not exists known_models (
                            model text primary key,
                            first_seen_at text not null,
                            last_seen_at text not null
                        )
                        """
                    )
                    now = utc_now()
                    conn.execute(
                        """
                        insert or ignore into known_models(model, first_seen_at, last_seen_at)
                        select model, ?, ? from active_events
                        where trim(model) != '' and lower(trim(model)) != '(unknown)'
                        union
                        select model, ?, ? from disabled_models
                        where trim(model) != '' and lower(trim(model)) != '(unknown)'
                        """,
                        (now, now, now, now),
                    )
                    conn.execute("delete from disabled_models where lower(trim(model)) = '(unknown)'")
                    conn.execute("pragma user_version = 8")
                    conn.commit()
                    version = 8
                if version == 8:
                    conn.execute("begin immediate")
                    settings_columns = {row[1] for row in conn.execute("pragma table_info(app_settings)")}
                    if "non_working_weekdays" not in settings_columns:
                        conn.execute(
                            "alter table app_settings add column non_working_weekdays text not null default '[5,6]'"
                        )
                    model_columns = {row[1] for row in conn.execute("pragma table_info(known_models)")}
                    if "color_slot" not in model_columns:
                        conn.execute("alter table known_models add column color_slot integer")
                    _assign_missing_model_color_slots_conn(conn)
                    conn.execute("pragma user_version = 9")
                    conn.commit()
                    version = 9
                if version == 9:
                    conn.execute("begin immediate")
                    models = conn.execute(
                        "select model from known_models order by first_seen_at, model"
                    ).fetchall()
                    conn.executemany(
                        "update known_models set color_slot = ? where model = ?",
                        [(slot, str(row[0])) for slot, row in enumerate(models)],
                    )
                    conn.execute("pragma user_version = 10")
                    conn.commit()
                    version = 10
                if version == 10:
                    conn.execute("begin immediate")
                    conn.execute(
                        "update app_settings set merge_models_across_providers = 1, revision = revision + 1, updated_at = ? where id = 1",
                        (utc_now(),),
                    )
                    conn.execute("update known_models set color_slot = null")
                    conn.execute("pragma user_version = 11")
                    conn.commit()

    def settings(self) -> dict:
        with self.connect(readonly=True) as conn:
            row = conn.execute("select * from app_settings where id = 1").fetchone()
        return dict(row)

    def update_settings(
        self,
        revision: int,
        ignore_codex_auto_review: bool,
    ) -> dict:
        with OPERATION_LOCKS.acquire("settings"):
            with self.connect() as conn:
                conn.execute("begin immediate")
                current = conn.execute("select revision from app_settings where id = 1").fetchone()
                if int(current[0]) != revision:
                    raise RevisionConflict("Settings changed since this draft was opened")
                conn.execute(
                    "update app_settings set revision = revision + 1, ignore_codex_auto_review = ?, ignore_failed_requests = 0, legacy_preference_migrated = 1, updated_at = ? where id = 1",
                    (int(ignore_codex_auto_review), utc_now()),
                )
            return self.settings()

    def seed_legacy_preference(self, value: bool) -> dict:
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select legacy_preference_migrated from app_settings where id = 1").fetchone()
            if not row[0]:
                conn.execute(
                    "update app_settings set ignore_codex_auto_review = ?, legacy_preference_migrated = 1, updated_at = ? where id = 1",
                    (int(value), utc_now()),
                )
        return self.settings()

    def active_operation(self) -> dict | None:
        with self.connect(readonly=True) as conn:
            row = conn.execute(
                "select * from operations where state in ('queued', 'running') order by created_at desc limit 1"
            ).fetchone()
        return dict(row) if row else None

    def apply_settings(
        self,
        revision: int,
        merge_models_across_providers: bool,
        sources: list[dict],
        models: list[dict],
        non_working_weekdays: list[int] | None = None,
    ) -> dict:
        with OPERATION_LOCKS.acquire("maintenance"):
            if self.active_operation():
                raise OperationConflict("A Unibase operation is already running")
            with self.connect() as conn:
                conn.execute("begin immediate")
                current = conn.execute("select * from app_settings where id = 1").fetchone()
                if int(current["revision"]) != revision:
                    raise RevisionConflict("Settings changed since this draft was opened")
                current_weekdays = load_non_working_weekdays(current["non_working_weekdays"])
                requested_weekdays = (
                    current_weekdays
                    if non_working_weekdays is None
                    else normalize_non_working_weekdays(non_working_weekdays)
                )
                registry = {
                    row["source_id"]: row["kind"]
                    for row in conn.execute("select source_id, kind from sources").fetchall()
                }
                current_sources = {
                    row["source_id"]: bool(row["enabled"])
                    for row in conn.execute("select source_id, enabled from sources").fetchall()
                }
                provided = {str(item.get("source_id")) for item in sources}
                if provided != set(registry) or len(provided) != len(sources):
                    raise ValueError("Source list does not match the current registry")
                for item in sources:
                    if not isinstance(item.get("enabled"), bool):
                        raise ValueError("Source enabled value must be boolean")
                    source_id = str(item["source_id"])
                    if registry[source_id] == "live" and not item["enabled"]:
                        raise ValueError("Original sources cannot be disabled")
                    conn.execute(
                        "update sources set enabled = ?, updated_at = ? where source_id = ? and kind != 'live'",
                        (int(item["enabled"]), utc_now(), source_id),
                    )
                model_names = [str(item.get("model") or "") for item in models]
                if (
                    len(set(model_names)) != len(model_names)
                    or any(not name or len(name) > 500 for name in model_names)
                    or not all(isinstance(item.get("enabled"), bool) for item in models)
                ):
                    raise ValueError("Invalid model settings")
                current_disabled = {
                    str(row[0]) for row in conn.execute("select model from disabled_models").fetchall()
                }
                requested_disabled = {
                    name for name, item in zip(model_names, models) if not item["enabled"]
                }
                requested_sources = {str(item["source_id"]): bool(item["enabled"]) for item in sources}
                maintenance_required = (
                    bool(current["merge_models_across_providers"]) != merge_models_across_providers
                    or current_sources != requested_sources
                    or current_disabled != requested_disabled
                )
                conn.execute("delete from disabled_models")
                conn.executemany(
                    "insert into disabled_models(model, created_at) values (?, ?)",
                    [(name, utc_now()) for name, item in zip(model_names, models) if not item["enabled"]],
                )
                conn.execute(
                    "update app_settings set revision = revision + 1, merge_models_across_providers = ?, non_working_weekdays = ?, ignore_codex_auto_review = 0, experimental_codex_deduplication = 1, ignore_failed_requests = 0, legacy_preference_migrated = 1, updated_at = ? where id = 1",
                    (int(merge_models_across_providers), json.dumps(requested_weekdays, separators=(",", ":")), utc_now()),
                )
            if maintenance_required:
                self.rebuild_active_events()
            result = self.settings()
            result["_maintenance_required"] = maintenance_required
            return result

    def register_source(self, source: DiscoveredSource) -> None:
        now = utc_now()
        with self.connect() as conn:
            previous = conn.execute(
                """
                select source_id, root_path, discovery_status, inventory_signature, stable_inventory_count, enabled
                from sources
                where source_id = ? or (provider = ? and kind = ? and relative_name = ?)
                order by source_id = ? desc
                limit 1
                """,
                (source.source_id, source.provider, source.kind, source.relative_name, source.source_id),
            ).fetchone()
            effective_source_id = previous["source_id"] if previous else source.source_id
            stable_count = 0
            status = source.status
            if (
                previous
                and source.kind == "live"
                and source.status == "not_indexed"
                and previous["root_path"] == str(source.root_path)
            ):
                status = previous["discovery_status"]
            if source.kind == "legacy_backup" and source.inventory_signature:
                stable_count = 1
                if previous and previous["inventory_signature"] == source.inventory_signature:
                    stable_count = int(previous["stable_inventory_count"]) + 1
                status = "ready" if stable_count >= 2 and source.status == "ready" else source.status
                if source.status == "ready" and stable_count < 2:
                    status = "incomplete"
            enabled = int(previous["enabled"]) if previous else int(source.enabled)
            conn.execute(
                """
                insert into sources(
                    source_id, provider, kind, root_path, relative_name, label, enabled, priority,
                    snapshot_id, snapshot_date, discovery_status, inventory_signature,
                    stable_inventory_count, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id) do update set
                    root_path = excluded.root_path,
                    relative_name = excluded.relative_name,
                    label = excluded.label,
                    priority = excluded.priority,
                    snapshot_id = excluded.snapshot_id,
                    snapshot_date = excluded.snapshot_date,
                    discovery_status = excluded.discovery_status,
                    inventory_signature = excluded.inventory_signature,
                    stable_inventory_count = excluded.stable_inventory_count,
                    updated_at = excluded.updated_at
                """,
                (
                    effective_source_id,
                    source.provider,
                    source.kind,
                    str(source.root_path),
                    source.relative_name,
                    source.label,
                    enabled,
                    source.priority,
                    source.snapshot_id,
                    source.snapshot_date,
                    status,
                    source.inventory_signature,
                    stable_count,
                    now,
                    now,
                ),
            )

    def sources(self, provider: str | None = None) -> list[dict]:
        with self.connect(readonly=True) as conn:
            sql = "select * from sources"
            params: tuple[str, ...] = ()
            if provider:
                sql += " where provider = ?"
                params = (provider,)
            sql += " order by provider, priority desc, coalesce(snapshot_date, '') desc, source_id"
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def set_source_enabled(self, source_id: str, enabled: bool) -> None:
        with OPERATION_LOCKS.acquire(f"source:{source_id}"):
            with self.connect() as conn:
                row = conn.execute("select kind from sources where source_id = ?", (source_id,)).fetchone()
                if row is None:
                    raise ValueError("Unknown source")
                if row["kind"] == "live" and not enabled:
                    raise ValueError("Live sources cannot be disabled")
                conn.execute("update sources set enabled = ?, updated_at = ? where source_id = ?", (int(enabled), utc_now(), source_id))
            self.rebuild_active_events()

    def begin_source_scan(self, source_id: str) -> int:
        with self.connect() as conn:
            generation = int(conn.execute("select generation from app_settings where id = 1").fetchone()[0]) + 1
            conn.execute("update sources set discovery_status = 'not_indexed', error = null where source_id = ?", (source_id,))
        return generation

    def upsert_source_file(
        self,
        source_id: str,
        relative_path: str,
        file_kind: str,
        *,
        size: int,
        mtime_ns: int,
        complete_offset: int = 0,
        content_hash: str | None = None,
        scan_generation: int = 0,
        parser_version: int = 1,
        schema_fingerprint: str | None = None,
        change_cursor: str | None = None,
    ) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                insert into source_files(
                    source_id, relative_path, file_kind, size, mtime_ns, complete_offset,
                    content_hash, scan_generation, parser_version, schema_fingerprint, change_cursor
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id, relative_path) do update set
                    file_kind = excluded.file_kind,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    complete_offset = excluded.complete_offset,
                    content_hash = excluded.content_hash,
                    scan_generation = excluded.scan_generation,
                    parser_version = excluded.parser_version,
                    schema_fingerprint = excluded.schema_fingerprint,
                    change_cursor = excluded.change_cursor
                """,
                (
                    source_id,
                    relative_path,
                    file_kind,
                    size,
                    mtime_ns,
                    complete_offset,
                    content_hash,
                    scan_generation,
                    parser_version,
                    schema_fingerprint,
                    change_cursor,
                ),
            )
            return int(
                conn.execute(
                    "select source_file_id from source_files where source_id = ? and relative_path = ?",
                    (source_id, relative_path),
                ).fetchone()[0]
            )

    def file_checkpoint(self, source_id: str, relative_path: str) -> dict | None:
        with self.connect(readonly=True) as conn:
            row = conn.execute(
                "select * from source_files where source_id = ? and relative_path = ?",
                (source_id, relative_path),
            ).fetchone()
        return dict(row) if row else None

    def source_file_keys(self, source_id: str, *, file_kind: str | None = None) -> set[str]:
        sql = "select relative_path from source_files where source_id = ?"
        params: list[object] = [source_id]
        if file_kind is not None:
            sql += " and file_kind = ?"
            params.append(file_kind)
        with self.connect(readonly=True) as conn:
            return {str(row["relative_path"]) for row in conn.execute(sql, params).fetchall()}

    def clear_source_file_occurrences(self, source_file_id: int) -> None:
        with self.connect() as conn:
            conn.execute("delete from event_occurrences where source_file_id = ?", (source_file_id,))

    def clear_source_event_occurrences(self, source_id: str, provider: str, event_key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                delete from event_occurrences
                where source_id = ? and event_variant_id in (
                    select event_variant_id from event_variants where provider = ? and event_key = ?
                )
                """,
                (source_id, provider, event_key),
            )

    def reconcile_source_event_keys(self, source_id: str, provider: str, active_event_keys: set[str]) -> None:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select distinct ev.event_key
                from event_occurrences eo
                join event_variants ev on ev.event_variant_id = eo.event_variant_id
                where eo.source_id = ? and ev.provider = ?
                """,
                (source_id, provider),
            ).fetchall()
            for row in rows:
                if row["event_key"] not in active_event_keys:
                    conn.execute(
                        """
                        delete from event_occurrences
                        where source_id = ? and event_variant_id in (
                            select event_variant_id from event_variants where provider = ? and event_key = ?
                        )
                        """,
                        (source_id, provider, row["event_key"]),
                    )

    def add_event(self, source_id: str, source_file_id: int | None, event: dict, scan_generation: int) -> int:
        with self.connect() as conn:
            return self._add_event_conn(conn, source_id, source_file_id, event, scan_generation)

    def _add_event_conn(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        source_file_id: int | None,
        event: dict,
        scan_generation: int,
    ) -> int:
        provider = str(event["provider"])
        event_key = str(event["event_key"])
        payload = {
            key: event.get(key)
            for key in (
                "stream_key",
                "timestamp_utc",
                "model",
                "native_provider_id",
                "semantics",
                "classification",
                "input_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cost_usd",
                "cost_kind",
            )
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        model = str(payload["model"] or "").strip()
        if model and model.lower() != "(unknown)":
            now = utc_now()
            conn.execute(
                """
                insert into known_models(model, first_seen_at, last_seen_at) values (?, ?, ?)
                on conflict(model) do update set last_seen_at = excluded.last_seen_at
                """,
                (model, now, now),
            )
        conn.execute(
                """
                insert into event_variants(
                    provider, event_key, stream_key, timestamp_utc, occurred_at, model,
                    native_provider_id, semantics, classification, input_tokens, cache_read_tokens,
                    cache_write_tokens, output_tokens, reasoning_tokens, cost_usd, cost_kind,
                    payload_hash, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(provider, event_key, payload_hash) do nothing
                """,
                (
                    provider,
                    event_key,
                    payload["stream_key"],
                    payload["timestamp_utc"],
                    int(event["occurred_at"]),
                    payload["model"],
                    payload["native_provider_id"],
                    payload["semantics"],
                    payload["classification"],
                    int(payload["input_tokens"] or 0),
                    int(payload["cache_read_tokens"] or 0),
                    int(payload["cache_write_tokens"] or 0),
                    int(payload["output_tokens"] or 0),
                    int(payload["reasoning_tokens"] or 0),
                    payload["cost_usd"],
                    payload["cost_kind"] or "unavailable",
                    payload_hash,
                    utc_now(),
                ),
        )
        variant_id = int(
            conn.execute(
                    "select event_variant_id from event_variants where provider = ? and event_key = ? and payload_hash = ?",
                    (provider, event_key, payload_hash),
            ).fetchone()[0]
        )
        conn.execute(
            """
            insert into event_occurrences(event_variant_id, source_id, source_file_id, scan_generation)
            values (?, ?, ?, ?)
            on conflict(event_variant_id, source_id, source_file_id)
            do update set scan_generation = excluded.scan_generation
            """,
            (variant_id, source_id, source_file_id, scan_generation),
        )
        conn.execute(
            "insert into canonical_events(provider, event_key) values (?, ?) on conflict(provider, event_key) do nothing",
            (provider, event_key),
        )
        return variant_id

    def add_events(
        self,
        source_id: str,
        source_file_id: int | None,
        events: Iterable[dict],
        scan_generation: int,
    ) -> set[tuple[str, str]]:
        event_list = list(events)
        with self.connect() as conn:
            conn.execute("begin immediate")
            for event in event_list:
                self._add_event_conn(conn, source_id, source_file_id, event, scan_generation)
        return {(str(event["provider"]), str(event["event_key"])) for event in event_list}

    def replace_source_file_events(
        self,
        source_id: str,
        source_file_id: int,
        events: Iterable[dict],
        scan_generation: int,
    ) -> set[tuple[str, str]]:
        event_list = list(events)
        with self.connect() as conn:
            conn.execute("begin immediate")
            previous = conn.execute(
                """
                select distinct ev.provider, ev.event_key
                from event_occurrences eo
                join event_variants ev on ev.event_variant_id = eo.event_variant_id
                where eo.source_id = ? and eo.source_file_id = ?
                """,
                (source_id, source_file_id),
            ).fetchall()
            conn.execute("delete from event_occurrences where source_id = ? and source_file_id = ?", (source_id, source_file_id))
            for event in event_list:
                self._add_event_conn(conn, source_id, source_file_id, event, scan_generation)
        dirty = {(str(row["provider"]), str(row["event_key"])) for row in previous}
        dirty.update((str(event["provider"]), str(event["event_key"])) for event in event_list)
        return dirty

    def replace_source_event_updates(
        self,
        source_id: str,
        source_file_id: int,
        provider: str,
        events: Iterable[dict],
        active_event_keys: set[str],
        scan_generation: int,
    ) -> set[tuple[str, str]]:
        event_list = list(events)
        updated_keys = {str(event["event_key"]) for event in event_list}
        with self.connect() as conn:
            conn.execute("begin immediate")
            existing = conn.execute(
                """
                select distinct ev.event_key
                from event_occurrences eo
                join event_variants ev on ev.event_variant_id = eo.event_variant_id
                where eo.source_id = ? and ev.provider = ?
                """,
                (source_id, provider),
            ).fetchall()
            keys_to_remove = updated_keys | {row["event_key"] for row in existing if row["event_key"] not in active_event_keys}
            for event_key in keys_to_remove:
                conn.execute(
                    """
                    delete from event_occurrences
                    where source_id = ? and event_variant_id in (
                        select event_variant_id from event_variants where provider = ? and event_key = ?
                    )
                    """,
                    (source_id, provider, event_key),
                )
            for event in event_list:
                self._add_event_conn(conn, source_id, source_file_id, event, scan_generation)
        return {(provider, str(event_key)) for event_key in keys_to_remove | updated_keys}

    def register_content_blob(self, sha256: str, size: int, provider: str, parser_version: int) -> tuple[int, bool]:
        with self.connect() as conn:
            row = conn.execute(
                "select blob_id, parsed_provider, parser_version from content_blobs where sha256 = ? and size = ?",
                (sha256, size),
            ).fetchone()
            if row:
                return int(row["blob_id"]), bool(row["parsed_provider"] == provider and row["parser_version"] == parser_version)
            conn.execute(
                "insert into content_blobs(sha256, size, parsed_provider, parser_version, created_at) values (?, ?, ?, ?, ?)",
                (sha256, size, provider, parser_version, utc_now()),
            )
            return int(conn.execute("select last_insert_rowid()").fetchone()[0]), False

    def reconcile_source_files(
        self,
        source_id: str,
        scan_generation: int,
        seen_paths: Iterable[str],
        *,
        rebuild_active: bool = True,
        dirty_event_keys: Iterable[tuple[str, str]] = (),
        complete: bool = True,
    ) -> bool:
        seen = set(seen_paths)
        removed = False
        dirty = set(dirty_event_keys)
        with self.connect() as conn:
            rows = conn.execute("select source_file_id, relative_path from source_files where source_id = ?", (source_id,)).fetchall()
            for row in rows:
                if row["relative_path"] not in seen:
                    removed = True
                    dirty.update(
                        (str(event["provider"]), str(event["event_key"]))
                        for event in conn.execute(
                            """
                            select distinct ev.provider, ev.event_key
                            from event_occurrences eo
                            join event_variants ev on ev.event_variant_id = eo.event_variant_id
                            where eo.source_id = ? and eo.source_file_id = ?
                            """,
                            (source_id, row["source_file_id"]),
                        ).fetchall()
                    )
                    conn.execute("delete from event_occurrences where source_id = ? and source_file_id = ?", (source_id, row["source_file_id"]))
                    conn.execute("delete from source_files where source_file_id = ?", (row["source_file_id"],))
            if complete:
                conn.execute(
                    "update sources set discovery_status = 'ready', stale = 0, error = null, last_successful_generation = ?, last_successful_scan = ?, file_count = ?, updated_at = ? where source_id = ?",
                    (scan_generation, utc_now(), len(seen), utc_now(), source_id),
                )
            else:
                conn.execute(
                    "update sources set discovery_status = 'ready', stale = 1, error = null, file_count = ?, updated_at = ? where source_id = ?",
                    (len(seen), utc_now(), source_id),
                )
        if dirty:
            self.rebuild_active_events(dirty)
        elif rebuild_active or removed:
            self.rebuild_active_events()
        return rebuild_active or removed

    def mark_source_error(self, source_id: str, error: BaseException | str) -> None:
        with self.connect() as conn:
            conn.execute(
                "update sources set discovery_status = 'error', stale = 1, error = ?, updated_at = ? where source_id = ?",
                (sanitize_error(error), utc_now(), source_id),
            )

    def rebuild_active_events(self, event_keys: Iterable[tuple[str, str]] | None = None) -> int:
        dirty = None if event_keys is None else set(event_keys)
        if dirty == set():
            return int(self.settings()["generation"])
        with OPERATION_LOCKS.acquire("active-events"):
            with self.connect() as conn:
                conn.execute("begin immediate")
                generation = int(conn.execute("select generation from app_settings where id = 1").fetchone()[0]) + 1
                conn.execute("drop table if exists temp.dirty_event_keys")
                if dirty is not None:
                    conn.execute(
                        "create temp table dirty_event_keys(provider text not null, event_key text not null, primary key(provider, event_key))"
                    )
                    conn.executemany(
                        "insert into dirty_event_keys(provider, event_key) values (?, ?)",
                        sorted(dirty),
                    )
                conn.execute("drop table if exists temp.active_event_winners")
                conn.execute(
                    f"""
                    create temp table active_event_winners as
                    with ranked_sources as (
                        select ev.*, s.priority, coalesce(s.snapshot_date, '') snapshot_date,
                               s.source_id stable_source_id, eo.scan_generation,
                                row_number() over (
                                    partition by ev.event_variant_id
                                    order by s.priority desc, coalesce(s.snapshot_date, '') desc, s.source_id asc,
                                             eo.scan_generation desc, coalesce(eo.source_file_id, 0) asc
                                ) source_rank
                        from event_variants ev
                        join event_occurrences eo on eo.event_variant_id = ev.event_variant_id
                        join sources s on s.source_id = eo.source_id and s.enabled = 1
                        {"join dirty_event_keys dirty on dirty.provider = ev.provider and dirty.event_key = ev.event_key" if dirty is not None else ""}
                    ), ranked_variants as (
                        select ranked_sources.*,
                                row_number() over (
                                    partition by provider, event_key
                                    order by case when semantics = 'codex_global_dedup' then occurred_at end asc,
                                             case when semantics = 'codex_global_dedup' and lower(trim(model)) = '(unknown)' then 1 else 0 end asc,
                                             priority desc, snapshot_date desc, stable_source_id asc,
                                             scan_generation desc, payload_hash asc
                                ) variant_rank,
                               count(*) over (partition by provider, event_key) variant_count
                        from ranked_sources
                        where source_rank = 1
                    )
                    select * from ranked_variants where variant_rank = 1
                    """
                )
                if dirty is None:
                    conn.execute("delete from active_events")
                    conn.execute("update canonical_events set selected_variant_id = null, conflict_state = 'clean'")
                else:
                    conn.execute(
                        """
                        delete from active_events
                        where exists (
                            select 1 from dirty_event_keys dirty
                            where dirty.provider = active_events.provider and dirty.event_key = active_events.event_key
                        )
                        """
                    )
                    conn.execute(
                        """
                        update canonical_events
                        set selected_variant_id = null, conflict_state = 'clean'
                        where exists (
                            select 1 from dirty_event_keys dirty
                            where dirty.provider = canonical_events.provider and dirty.event_key = canonical_events.event_key
                        )
                        """
                    )
                conn.execute(
                    """
                    update canonical_events as canonical
                    set selected_variant_id = winner.event_variant_id,
                        conflict_state = case
                            when winner.semantics = 'codex_global_dedup' then 'clean'
                            when winner.variant_count > 1 then 'conflict'
                            else 'clean'
                        end
                    from active_event_winners as winner
                    where canonical.provider = winner.provider and canonical.event_key = winner.event_key
                    """
                )
                conn.execute(
                    """
                    insert into active_events(
                        canonical_event_id, event_variant_id, generation, provider, event_key, stream_key,
                        timestamp_utc, occurred_at, model, native_provider_id, semantics, classification,
                        input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens,
                        cost_usd, cost_kind
                    )
                    select canonical.canonical_event_id, winner.event_variant_id, ?, winner.provider,
                           winner.event_key, winner.stream_key, winner.timestamp_utc, winner.occurred_at,
                           winner.model, winner.native_provider_id, winner.semantics, winner.classification,
                            winner.input_tokens, winner.cache_read_tokens, winner.cache_write_tokens,
                            winner.output_tokens, winner.reasoning_tokens, winner.cost_usd, winner.cost_kind
                    from active_event_winners winner
                    join canonical_events canonical
                      on canonical.provider = winner.provider and canonical.event_key = winner.event_key
                    """,
                    (generation,),
                )
                conn.execute("update app_settings set generation = ?, state = 'ready', updated_at = ? where id = 1", (generation, utc_now()))
                conn.execute(
                    "update sources set event_count = (select count(distinct eo.event_variant_id) from event_occurrences eo where eo.source_id = sources.source_id)"
                )
                conn.execute("drop table temp.active_event_winners")
                conn.execute("drop table if exists temp.dirty_event_keys")
                conn.commit()
                return generation

    def active_event_rows(self, provider: str = "all", start_ts: int | None = None, end_ts: int | None = None) -> list[dict]:
        clauses = []
        params: list[object] = []
        if provider != "all":
            clauses.append("provider = ?")
            params.append(provider)
        if start_ts is not None:
            clauses.append("occurred_at >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("occurred_at < ?")
            params.append(end_ts)
        sql = "select * from active_events"
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by occurred_at, canonical_event_id"
        with self.connect(readonly=True) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def source_event_keys(self, source_id: str, provider: str) -> set[str]:
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                """
                select distinct ev.event_key
                from event_occurrences eo
                join event_variants ev on ev.event_variant_id = eo.event_variant_id
                where eo.source_id = ? and ev.provider = ?
                """,
                (source_id, provider),
            ).fetchall()
        return {str(row["event_key"]) for row in rows}

    def reset(self, *, allow_active_operation: bool = False) -> None:
        with OPERATION_LOCKS.acquire("maintenance"):
            if not allow_active_operation and self.active_operation():
                raise OperationConflict("A Unibase operation is already running")
            with self.connect() as conn:
                conn.execute("begin immediate")
                for table in (
                    "active_events", "diagnostic_events", "record_occurrences", "record_variants",
                    "event_occurrences", "canonical_events", "event_variants", "logical_streams",
                    "content_blobs", "source_files",
                ):
                    conn.execute(f"delete from {table}")
                conn.execute("update sources set discovery_status = case when discovery_status = 'not_installed' then 'not_installed' else 'not_indexed' end, stale = 0, last_successful_generation = null, file_count = 0, event_count = 0, last_successful_scan = null, error = null")
                conn.execute("update app_settings set generation = generation + 1, state = 'reset_empty', updated_at = ? where id = 1", (utc_now(),))

    def integrity_check(self) -> bool:
        with self.connect(readonly=True) as conn:
            return conn.execute("pragma integrity_check").fetchone()[0] == "ok"

    def create_operation(self, kind: str) -> str:
        operation_id = stable_id(kind, utc_now(), threading.get_ident())
        now = utc_now()
        with OPERATION_LOCKS.acquire("maintenance"):
            with self.connect() as conn:
                conn.execute("begin immediate")
                if conn.execute("select 1 from operations where state in ('queued', 'running') limit 1").fetchone():
                    raise OperationConflict("A Unibase operation is already running")
                conn.execute(
                    "insert into operations(operation_id, kind, state, created_at, updated_at) values (?, ?, 'queued', ?, ?)",
                    (operation_id, kind, now, now),
                )
        return operation_id

    def recover_interrupted_operations(self) -> None:
        with self.connect() as conn:
            conn.execute(
                "update operations set state = 'failed', error = 'Interrupted by process restart', updated_at = ? where state in ('queued', 'running')",
                (utc_now(),),
            )

    def update_operation(
        self,
        operation_id: str,
        *,
        state: str,
        current: int | None = None,
        total: int | None = None,
        generation: int | None = None,
        error: BaseException | str | None = None,
    ) -> None:
        fields = ["state = ?", "updated_at = ?"]
        values: list[object] = [state, utc_now()]
        if current is not None:
            fields.append("progress_current = ?")
            values.append(current)
        if total is not None:
            fields.append("progress_total = ?")
            values.append(total)
        if generation is not None:
            fields.append("generation = ?")
            values.append(generation)
        fields.append("error = ?")
        values.append(sanitize_error(error))
        values.append(operation_id)
        with self.connect() as conn:
            conn.execute(f"update operations set {', '.join(fields)} where operation_id = ?", values)

    def operation_status(self, operation_id: str | None = None) -> dict:
        with self.connect(readonly=True) as conn:
            if operation_id:
                row = conn.execute("select * from operations where operation_id = ?", (operation_id,)).fetchone()
            else:
                row = conn.execute("select * from operations order by created_at desc limit 1").fetchone()
        operation = dict(row) if row else None
        return {"operation": operation, "generation": self.settings()["generation"], "state": self.settings()["state"]}


class RevisionConflict(RuntimeError):
    pass


class OperationConflict(RuntimeError):
    pass


def _safe_manifest_root(child: Path, root_value: object) -> Path | None:
    if not isinstance(root_value, str) or not root_value:
        return None
    pure = PurePosixPath(root_value.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        return None
    root = (child / Path(*pure.parts)).resolve()
    try:
        root.relative_to(child.resolve())
    except ValueError:
        return None
    return root


def _eligible_paths(provider: str, root: Path) -> list[Path]:
    if provider == "codex":
        return sorted(path for path in (root / "sessions").glob("**/rollout-*.jsonl") if path.is_file())
    if provider == "claude":
        projects = root / "projects"
        excluded = {"tool-results", "file-history"}
        return sorted(
            path for path in projects.glob("**/*.jsonl")
            if path.is_file() and not excluded.intersection(path.relative_to(projects).parts)
        )
    db = root / "opencode.db"
    return [db] if db.is_file() else []


def _inventory_signature(provider: str, root: Path) -> str | None:
    paths = _eligible_paths(provider, root)
    if not paths:
        return None
    inventory = []
    for path in paths:
        stat = path.stat()
        inventory.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(inventory, separators=(",", ":")).encode()).hexdigest()


def _legacy_roots(provider: str, child: Path) -> list[Path]:
    candidates = [child]
    if provider == "codex":
        candidates.append(child / ".codex")
    elif provider == "claude":
        candidates.append(child / ".claude")
    else:
        candidates.extend((child / "opencode", child / ".local" / "share" / "opencode"))
    return [candidate for candidate in candidates if _eligible_paths(provider, candidate)]


def discover_backup_sources(provider: str, add_stat_dir: Path) -> list[DiscoveredSource]:
    try:
        add_stat_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if exc.errno != errno.EROFS:
            raise
        # A read-only source root cannot gain an add_stat/ directory: mkdir
        # reports EROFS rather than EEXIST, so exist_ok never engages. Nothing
        # can have been snapshotted there either, so there is simply nothing to
        # discover. Read-only roots are normal (a `:ro` container mount, backups
        # on read-only media) and must not stop the live source registering.
        return []
    discovered: list[DiscoveredSource] = []
    for child in sorted(path for path in add_stat_dir.iterdir() if path.is_dir()):
        manifest_path = child / "snapshot.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                root = _safe_manifest_root(child, manifest.get("root"))
                valid = (
                    manifest.get("format") == SNAPSHOT_FORMAT
                    and manifest.get("version") == 1
                    and manifest.get("provider") == provider
                    and isinstance(manifest.get("id"), str)
                    and root is not None
                    and bool(_eligible_paths(provider, root))
                )
                if not valid:
                    raise ValueError("invalid snapshot manifest")
                snapshot_id = manifest["id"]
                label = safe_display_label(manifest.get("label"), child.name)
                discovered.append(
                    DiscoveredSource(
                        stable_id(provider, "snapshot", snapshot_id), provider, "normalized_backup", root,
                        child.name, label, False, SOURCE_PRIORITIES["normalized_backup"], snapshot_id,
                        str(manifest.get("created_at") or "") or None, "ready",
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError):
                discovered.append(
                    DiscoveredSource(
                        stable_id(provider, "incomplete", child.name), provider, "normalized_backup", child,
                        child.name, child.name, False, SOURCE_PRIORITIES["normalized_backup"], None, None, "incomplete",
                    )
                )
            continue

        roots = _legacy_roots(provider, child)
        status = "ambiguous" if len(roots) > 1 else "ready" if roots else "incomplete"
        root = roots[0] if len(roots) == 1 else child
        discovered.append(
            DiscoveredSource(
                stable_id(provider, "legacy", child.name), provider, "legacy_backup", root, child.name,
                child.name, False, SOURCE_PRIORITIES["legacy_backup"], None, None, status,
                _inventory_signature(provider, root) if len(roots) == 1 else None,
            )
        )
    return discovered


def register_default_sources(
    unibase: Unibase,
    *,
    codex_root: Path | None = None,
    claude_root: Path | None = None,
    opencode_root: Path | None = None,
) -> list[dict]:
    roots = {
        "codex": (codex_root or Path.home() / ".codex").expanduser(),
        "claude": (claude_root or Path.home() / ".claude").expanduser(),
        "opencode": (opencode_root or default_opencode_data_dir()).expanduser(),
    }
    for provider, root in roots.items():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if exc.errno != errno.EROFS:
                raise
            # Already-present read-only roots answer mkdir with EEXIST, which
            # exist_ok absorbs; only a genuinely missing root on read-only media
            # reaches here, and registration below still reports it accurately.
            pass
        add_stat = root / "add_stat"
        live_root = root
        source = DiscoveredSource(
            stable_id(provider, "live"), provider, "live", live_root, "live", f"Live {provider.title()}",
            True, SOURCE_PRIORITIES["live"], None, None, "not_indexed",
        )
        unibase.register_source(source)
        for backup in discover_backup_sources(provider, add_stat):
            unibase.register_source(backup)
    return unibase.sources()


def open_source_sqlite_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError("Usage source database is unavailable")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only = on")
    conn.execute("pragma busy_timeout = 30000")
    return conn
