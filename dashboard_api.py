#!/usr/bin/env python3
"""MeterMesh local multi-provider usage dashboard backed by Unibase."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import ipaddress
import json
import os
import sqlite3
import threading
import time
import warnings
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from claude_usage import DEFAULT_CLAUDE_DB, DEFAULT_CLAUDE_PROJECTS, PARSER_VERSION as CLAUDE_PARSER_VERSION
from claude_usage import index_claude_usage, load_claude_events
from claude_usage import import_claude_source
from codex_usage import PARSER_VERSION as CODEX_PARSER_VERSION
from codex_usage import import_codex_source
from codex_usage import scan_rollout_deduplicated_usage as scan_codex_rollout
from opencode_usage import PARSER_VERSION as OPENCODE_PARSER_VERSION
from opencode_usage import import_opencode_source, resolve_opencode_db
from unibase import (
    OPERATION_LOCKS,
    ClosingConnection,
    OperationConflict,
    RevisionConflict,
    Unibase,
    load_non_working_weekdays,
    normalize_non_working_weekdays,
    register_default_sources,
    resolve_unibase_path,
    sanitize_error,
)


DEFAULT_DB = Path.home() / ".codex" / "state_5.sqlite"
PROVIDERS = {"all", "codex", "claude", "opencode"}
RANGES = {"all", "30d", "7d", "1d", "custom"}
CHART_RANGES = {"all", "1y", "6m", "90d", "30d", "21d", "14d", "7d", "3d", "1d", "custom"}
REQUEST_GROUPS = {"none", "1m", "15m", "30m", "1h", "6h", "12h", "24h"}
REQUEST_PAGE_SIZES = {10, 25, 50, 100}
ACTIVITY_IDLE_TIMEOUT_SECONDS = 10 * 60
AUTO_REVIEW_MODEL = "codex-auto-review"
PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICING_CACHE_SECONDS = 600
PRICING_CACHE: dict[str, object] = {"loaded_at": 0.0, "pricing": None}
USAGE_RESPONSE_CACHE_SECONDS = 30.0
USAGE_RESPONSE_CACHE_MAX_ENTRIES = 32
USAGE_RESPONSE_CACHE_LOCK = threading.Lock()
USAGE_RESPONSE_CACHE: OrderedDict[tuple[object, ...], tuple[float, dict]] = OrderedDict()
SOURCE_REFRESH_STATE_LOCK = threading.Lock()
SOURCE_REFRESH_RUNNING = False
SOURCE_REFRESH_COMPLETED_MONOTONIC = 0.0
SOURCE_REFRESH_STARTED_AT: str | None = None
SOURCE_REFRESH_COMPLETED_AT: str | None = None
SOURCE_REFRESH_REASON: str | None = None
SOURCE_REFRESH_ERROR: str | None = None
SOURCE_REFRESH_SCHEDULER_POLL_SECONDS = 30.0
SOURCE_REFRESH_SCHEDULER_COOLDOWN_SECONDS = 60.0
FALLBACK_PRICING = {
    "gpt-5.5": {
        "input_cost_per_token": 0.000005,
        "cache_read_input_token_cost": 0.0000005,
        "output_cost_per_token": 0.00003,
    },
    "gpt-5.4": {
        "input_cost_per_token": 0.0000025,
        "cache_read_input_token_cost": 0.00000025,
        "output_cost_per_token": 0.000015,
    },
    "gpt-5.3-codex": {
        "input_cost_per_token": 0.00000175,
        "cache_read_input_token_cost": 0.000000175,
        "output_cost_per_token": 0.000014,
    },
    "gpt-5.2-codex": {
        "input_cost_per_token": 0.00000175,
        "cache_read_input_token_cost": 0.000000175,
        "output_cost_per_token": 0.000014,
    },
    "gpt-5.6-sol": {
        "input_cost_per_token": 0.000005,
        "cache_creation_input_token_cost": 0.00000625,
        "cache_read_input_token_cost": 0.0000005,
        "output_cost_per_token": 0.00003,
    },
    "claude-sonnet-5": {
        "input_cost_per_token": 0.000002,
        "cache_creation_input_token_cost": 0.0000025,
        "cache_read_input_token_cost": 0.0000002,
        "output_cost_per_token": 0.00001,
    },
    "claude-fable-5": {
        "input_cost_per_token": 0.00001,
        "cache_creation_input_token_cost": 0.0000125,
        "cache_read_input_token_cost": 0.000001,
        "output_cost_per_token": 0.00005,
    },
    "claude-opus-4-8": {
        "input_cost_per_token": 0.000005,
        "cache_creation_input_token_cost": 0.00000625,
        "cache_read_input_token_cost": 0.0000005,
        "output_cost_per_token": 0.000025,
    },
    "claude-sonnet-4-6": {
        "input_cost_per_token": 0.000003,
        "cache_creation_input_token_cost": 0.00000375,
        "cache_read_input_token_cost": 0.0000003,
        "output_cost_per_token": 0.000015,
    },
}


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Codex state database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_int(value: int | float | None) -> str:
    return f"{int(value or 0):,}"


def fmt_short(value: int | float | None) -> str:
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def fmt_usd(value: int | float | None) -> str:
    return f"${float(value or 0):,.2f}"


def iso_from_unix(value: int | None) -> str:
    if value is None:
        return "-"
    return dt.datetime.fromtimestamp(value).date().isoformat()


def day_from_iso_timestamp(value: str) -> str:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().date().isoformat()


def normalize_provider(provider: str | None) -> str:
    return provider if provider in PROVIDERS else "all"


def normalize_range(range_name: str | None) -> str:
    return range_name if range_name in RANGES else "all"


def normalize_chart_range(range_name: str | None) -> str:
    return range_name if range_name in CHART_RANGES else "30d"


def parse_iso_day(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def day_start_timestamp(value: dt.date) -> int:
    return int(dt.datetime.combine(value, dt.time.min).timestamp())


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    return (next_month - dt.timedelta(days=1)).day


def add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, days_in_month(year, month))
    return dt.date(year, month, day)


def parse_bool_flag(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_pricing() -> dict:
    now = dt.datetime.now().timestamp()
    cached = PRICING_CACHE.get("pricing")
    loaded_at = float(PRICING_CACHE.get("loaded_at") or 0)
    if isinstance(cached, dict) and now - loaded_at < PRICING_CACHE_SECONDS:
        return cached

    started_at = time.perf_counter()
    try:
        request = Request(PRICING_URL, headers={"User-Agent": "MeterMesh/2.1"})
        with urlopen(request, timeout=2.5) as response:
            live_pricing = json.loads(response.read().decode("utf-8"))
        pricing = {
            "source": "LiteLLM live",
            "url": PRICING_URL,
            "loaded_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "models": live_pricing,
            "fallback": FALLBACK_PRICING,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        pricing = {
            "source": "bundled fallback",
            "url": PRICING_URL,
            "loaded_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "models": FALLBACK_PRICING,
            "fallback": FALLBACK_PRICING,
            "error": exc.__class__.__name__,
        }

    PRICING_CACHE["loaded_at"] = now
    PRICING_CACHE["pricing"] = pricing
    print(
        f"[MeterMesh timing] pricing refresh: {(time.perf_counter() - started_at) * 1000:.0f} ms; "
        f'source={pricing["source"]}',
        flush=True,
    )
    return pricing


def model_price_key(model: str, pricing_models: dict) -> str | None:
    base_model = model
    for prefix in ("openai/", "anthropic/"):
        if base_model.startswith(prefix):
            base_model = base_model.removeprefix(prefix)
            break
    candidates = [model, base_model, f"openai/{base_model}", f"anthropic/{base_model}"]
    for candidate in candidates:
        if candidate in pricing_models:
            return candidate
    return None


def token_cost_usd(event: dict, pricing: dict) -> tuple[float, str | None]:
    models = pricing["models"]
    fallback = pricing["fallback"]
    price_key = model_price_key(event["model"], models)
    price_source = models
    if price_key is None:
        price_key = model_price_key(event["model"], fallback)
        price_source = fallback
    if price_key is None:
        return 0.0, event["model"]

    model_price = price_source[price_key]
    input_price = float(model_price.get("input_cost_per_token") or 0)
    cache_creation_price = model_price.get("cache_creation_input_token_cost")
    if cache_creation_price is None:
        cache_creation_price = input_price
    cache_read_price = model_price.get("cache_read_input_token_cost")
    if cache_read_price is None:
        cache_read_price = input_price
    output_price = float(model_price.get("output_cost_per_token") or 0)
    cache_creation_tokens = int(event.get("cache_creation_input_tokens") or 0)
    cache_read_tokens = event.get("cache_read_input_tokens")
    if cache_read_tokens is None:
        cache_read_tokens = max(int(event["cached_input_tokens"]) - cache_creation_tokens, 0)
    cost = (
        event["input_tokens"] * input_price
        + cache_creation_tokens * float(cache_creation_price)
        + int(cache_read_tokens) * float(cache_read_price)
        + event["output_tokens"] * output_price
    )
    return cost, None


def resolve_range(
    range_name: str | None,
    start_day: str | None = None,
    end_day: str | None = None,
    ignore_auto_review: bool = False,
    today: dt.date | None = None,
) -> dict[str, str | int | bool | None]:
    range_name = normalize_range(range_name)
    today = today or dt.date.today()
    start_date: dt.date | None = None
    end_date: dt.date | None = None

    if range_name == "1d":
        start_date = today
        end_date = today
    elif range_name == "7d":
        start_date = today - dt.timedelta(days=6)
        end_date = today
    elif range_name == "30d":
        start_date = today - dt.timedelta(days=29)
        end_date = today
    elif range_name == "custom":
        start_date = parse_iso_day(start_day) or today
        end_date = parse_iso_day(end_day) or start_date
        if start_date > end_date:
            start_date, end_date = end_date, start_date

    return {
        "range": range_name,
        "start_day": start_date.isoformat() if start_date else None,
        "end_day": end_date.isoformat() if end_date else None,
        "start_ts": day_start_timestamp(start_date) if start_date else None,
        "ignore_auto_review": ignore_auto_review,
    }


def resolve_chart_range(
    range_name: str | None,
    start_day: str | None = None,
    end_day: str | None = None,
    ignore_auto_review: bool = False,
    today: dt.date | None = None,
) -> dict[str, str | int | bool | None]:
    range_name = normalize_chart_range(range_name)
    today = today or dt.date.today()
    start_date: dt.date | None = None
    end_date: dt.date | None = None

    if range_name == "1d":
        start_date = today
        end_date = today
    elif range_name == "3d":
        start_date = today - dt.timedelta(days=2)
        end_date = today
    elif range_name == "7d":
        start_date = today - dt.timedelta(days=6)
        end_date = today
    elif range_name == "14d":
        start_date = today - dt.timedelta(days=13)
        end_date = today
    elif range_name == "21d":
        start_date = today - dt.timedelta(days=20)
        end_date = today
    elif range_name == "30d":
        start_date = today - dt.timedelta(days=29)
        end_date = today
    elif range_name == "90d":
        start_date = today - dt.timedelta(days=89)
        end_date = today
    elif range_name == "6m":
        start_date = add_months(today, -6) + dt.timedelta(days=1)
        end_date = today
    elif range_name == "1y":
        start_date = today - dt.timedelta(days=364)
        end_date = today
    elif range_name == "custom":
        start_date = parse_iso_day(start_day) or (today - dt.timedelta(days=29))
        end_date = parse_iso_day(end_day) or today
        if start_date > end_date:
            start_date, end_date = end_date, start_date

    return {
        "range": range_name,
        "start_day": start_date.isoformat() if start_date else None,
        "end_day": end_date.isoformat() if end_date else None,
        "start_ts": day_start_timestamp(start_date) if start_date else None,
        "today_day": today.isoformat(),
        "ignore_auto_review": ignore_auto_review,
    }


def longest_streak(days: set[str]) -> int:
    if not days:
        return 0
    parsed = sorted(dt.date.fromisoformat(day) for day in days)
    best = current = 1
    for prev, day in zip(parsed, parsed[1:]):
        if day == prev + dt.timedelta(days=1):
            current += 1
        else:
            current = 1
        best = max(best, current)
    return best


def current_streak(days: set[str]) -> int:
    today = dt.date.today()
    current = 0
    cursor = today
    while cursor.isoformat() in days:
        current += 1
        cursor -= dt.timedelta(days=1)
    return current


def local_hour_from_iso_timestamp(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    return parsed.strftime("%Y-%m-%d %H:00")


def usage_event(
    thread_id: str,
    model: str,
    timestamp: str,
    usage: dict[str, int],
    source: str,
    classification: str,
    response_id: str | None = None,
) -> dict:
    raw_input_tokens = usage["input_tokens"]
    cached_input_tokens = usage["cached_input_tokens"]
    output_tokens = usage["output_tokens"]
    billable_input_tokens = max(raw_input_tokens - cached_input_tokens, 0)
    return {
        "thread_id": thread_id,
        "timestamp": timestamp,
        "hour": local_hour_from_iso_timestamp(timestamp),
        "day": day_from_iso_timestamp(timestamp),
        "model": model,
        "source": source,
        "classification": classification,
        "response_id": response_id,
        "input_tokens": billable_input_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "raw_input_tokens": raw_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": usage["reasoning_output_tokens"],
        "total_tokens": billable_input_tokens + output_tokens,
        "total_with_cached_tokens": raw_input_tokens + output_tokens,
    }


def scan_rollout_telemetry(rollout_path: Path, thread_id: str, model: str) -> dict[str, list[dict]]:
    scanned = scan_codex_rollout(rollout_path, thread_id, model)
    usage_events = []
    for item in scanned["usage_events"]:
        event = usage_event(
            thread_id,
            model,
            item["timestamp_utc"],
            {
                "input_tokens": item["input_tokens"] + item["cache_read_tokens"],
                "cached_input_tokens": item["cache_read_tokens"],
                "output_tokens": item["output_tokens"],
                "reasoning_output_tokens": item["reasoning_tokens"],
            },
            "deduplicated",
            "usage_update",
        )
        event["dedup_key"] = item["dedup_key"]
        usage_events.append(event)
    return {"usage_events": usage_events, "token_events": list(usage_events)}


def filter_telemetry_events(events: list[dict], filters: dict[str, str | int | bool | None]) -> list[dict]:
    start_day = filters.get("start_day")
    end_day = filters.get("end_day")
    return [
        event
        for event in events
        if (not start_day or event["day"] >= start_day) and (not end_day or event["day"] <= end_day)
    ]


def scan_token_telemetry(db_path: Path, start_ts: int | None, ignore_auto_review: bool) -> dict[str, list[dict]]:
    with connect(db_path) as conn:
        where_sql = "where rollout_path != ''"
        params: list[int] = []
        if start_ts is not None:
            where_sql += " and updated_at >= ?"
            params.append(start_ts)
        threads = conn.execute(
            f"""
            select id, rollout_path, coalesce(model, '(unknown)') model
            from threads
            {where_sql}
            """,
            params,
        ).fetchall()

    usage_events_by_key: dict[str, dict] = {}
    token_records: list[dict] = []
    for thread in threads:
        if ignore_auto_review and thread["model"] == AUTO_REVIEW_MODEL:
            continue
        rollout_path = Path(thread["rollout_path"])
        if not rollout_path.exists():
            continue
        result = scan_rollout_telemetry(rollout_path, thread["id"], thread["model"])
        token_records.extend(result["token_events"])
        for event in result["usage_events"]:
            previous = usage_events_by_key.get(event["dedup_key"])
            if previous is None or event["timestamp"] < previous["timestamp"]:
                usage_events_by_key[event["dedup_key"]] = event
    usage_events = sorted(usage_events_by_key.values(), key=lambda event: event["timestamp"])
    return {"usage_events": usage_events, "token_events": token_records}


def token_events(db_path: Path, filters: dict[str, str | int | bool | None]) -> list[dict]:
    telemetry = scan_token_telemetry(db_path, filters.get("start_ts"), bool(filters.get("ignore_auto_review")))
    return filter_telemetry_events(telemetry["usage_events"], filters)


def chart_granularity(start_day: dt.date, end_day: dt.date) -> str:
    span_days = (end_day - start_day).days + 1
    if span_days <= 90:
        return "day"
    if span_days <= 190:
        return "week"
    return "month"


def chart_bucket_for_day(day: dt.date, granularity: str) -> tuple[dt.date, dt.date, str]:
    if granularity == "week":
        start = day - dt.timedelta(days=day.weekday())
        end = start + dt.timedelta(days=6)
        label = f"{start.strftime('%b')} {start.day}"
        return start, end, label
    if granularity == "month":
        start = day.replace(day=1)
        end = dt.date(day.year, day.month, days_in_month(day.year, day.month))
        label = start.strftime("%b %Y")
        return start, end, label
    return day, day, f"{day.strftime('%b')} {day.day}"


def activity_from_timestamps(
    timestamps: list[int],
    filters: dict[str, str | int | bool | None],
    timezone: ZoneInfo,
    idle_timeout_seconds: int = ACTIVITY_IDLE_TIMEOUT_SECONDS,
    non_working_weekdays: tuple[int, ...] | list[int] = (5, 6),
    workdays_only: bool = False,
) -> dict:
    ordered = sorted(int(value) for value in timestamps)
    start_day = parse_iso_day(str(filters["start_day"])) if filters.get("start_day") else None
    end_day = parse_iso_day(str(filters["end_day"])) if filters.get("end_day") else None
    today = parse_iso_day(str(filters.get("today_day") or "")) or dt.datetime.now(timezone).date()
    if start_day is None and ordered:
        start_day = dt.datetime.fromtimestamp(ordered[0], timezone).date()
    if end_day is None:
        last_day = dt.datetime.fromtimestamp(ordered[-1], timezone).date() if ordered else today
        end_day = max(today, last_day)
    if start_day is None:
        start_day = end_day

    range_start = int(dt.datetime.combine(start_day, dt.time.min, timezone).timestamp())
    range_end = int(
        dt.datetime.combine(end_day + dt.timedelta(days=1), dt.time.min, timezone).timestamp()
    )
    intervals: list[list[int]] = []
    excluded_weekdays = set(non_working_weekdays)
    included_request_days: dict[str, int] = {}
    excluded_request_days: dict[str, int] = {}
    for timestamp in ordered:
        if range_start <= timestamp < range_end:
            request_day = dt.datetime.fromtimestamp(timestamp, timezone).date()
            day_key = request_day.isoformat()
            target = (
                excluded_request_days
                if workdays_only and request_day.weekday() in excluded_weekdays
                else included_request_days
            )
            target[day_key] = target.get(day_key, 0) + 1
        interval_start = max(timestamp, range_start)
        interval_end = min(timestamp + idle_timeout_seconds, range_end)
        if interval_end <= interval_start:
            continue
        if intervals and interval_start <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], interval_end)
        else:
            intervals.append([interval_start, interval_end])

    included_daily_seconds: dict[str, int] = {}
    excluded_daily_seconds: dict[str, int] = {}
    included_focus_blocks = 0
    for interval_start, interval_end in intervals:
        cursor = interval_start
        interval_has_included_segment = False
        while cursor < interval_end:
            local_day = dt.datetime.fromtimestamp(cursor, timezone).date()
            next_day = local_day + dt.timedelta(days=1)
            next_day_start = int(dt.datetime.combine(next_day, dt.time.min, timezone).timestamp())
            segment_end = min(interval_end, next_day_start)
            day_key = local_day.isoformat()
            if workdays_only and local_day.weekday() in excluded_weekdays:
                excluded_daily_seconds[day_key] = (
                    excluded_daily_seconds.get(day_key, 0) + segment_end - cursor
                )
            else:
                included_daily_seconds[day_key] = (
                    included_daily_seconds.get(day_key, 0) + segment_end - cursor
                )
                interval_has_included_segment = True
            cursor = segment_end
        if interval_has_included_segment:
            included_focus_blocks += 1

    granularity = chart_granularity(start_day, end_day)
    days = []
    cursor, _, _ = chart_bucket_for_day(start_day, granularity)
    while cursor <= end_day:
        bucket_start, bucket_end, label = chart_bucket_for_day(cursor, granularity)
        visible_start = max(bucket_start, start_day)
        visible_end = min(bucket_end, end_day)
        bucket_seconds = 0
        bucket_excluded_seconds = 0
        bucket_requests = 0
        bucket_excluded_requests = 0
        bucket_all_excluded = workdays_only
        day_cursor = visible_start
        while day_cursor <= visible_end:
            day_key = day_cursor.isoformat()
            bucket_seconds += included_daily_seconds.get(day_key, 0)
            bucket_excluded_seconds += excluded_daily_seconds.get(day_key, 0)
            bucket_requests += included_request_days.get(day_key, 0)
            bucket_excluded_requests += excluded_request_days.get(day_key, 0)
            bucket_all_excluded = bucket_all_excluded and day_cursor.weekday() in excluded_weekdays
            day_cursor += dt.timedelta(days=1)
        days.append(
            {
                "day": bucket_start.isoformat(),
                "bucket_start": visible_start.isoformat(),
                "bucket_end": visible_end.isoformat(),
                "label": label,
                "active_seconds": bucket_seconds,
                "excluded_active_seconds": bucket_excluded_seconds,
                "raw_active_seconds": bucket_seconds + bucket_excluded_seconds,
                "request_count": bucket_requests,
                "excluded_request_count": bucket_excluded_requests,
                "raw_request_count": bucket_requests + bucket_excluded_requests,
                "fully_excluded": bucket_all_excluded,
            }
        )
        if granularity == "month":
            cursor = add_months(bucket_start, 1)
        elif granularity == "week":
            cursor = bucket_start + dt.timedelta(days=7)
        else:
            cursor = bucket_start + dt.timedelta(days=1)

    total_seconds = sum(included_daily_seconds.values())
    active_days = len(included_daily_seconds)
    calendar_period_days = (end_day - start_day).days + 1
    eligible_period_days = sum(
        not workdays_only or day.weekday() not in excluded_weekdays
        for day in (start_day + dt.timedelta(days=offset) for offset in range(calendar_period_days))
    )
    peak_day, peak_day_seconds = max(
        included_daily_seconds.items(), key=lambda item: item[1], default=("-", 0)
    )
    return {
        "range": filters["range"],
        "granularity": granularity,
        "range_start": start_day.isoformat(),
        "range_end": end_day.isoformat(),
        "idle_timeout_minutes": idle_timeout_seconds // 60,
        "workdays_only": workdays_only,
        "non_working_weekdays": sorted(excluded_weekdays),
        "total_seconds": total_seconds,
        "average_seconds_per_day": total_seconds / max(eligible_period_days, 1),
        "average_seconds_per_active_day": total_seconds / max(active_days, 1),
        "period_days": eligible_period_days,
        "calendar_period_days": calendar_period_days,
        "eligible_period_days": eligible_period_days,
        "active_days": active_days,
        "focus_blocks": included_focus_blocks,
        "request_count": sum(included_request_days.values()),
        "peak_day": peak_day,
        "peak_day_seconds": peak_day_seconds,
        "days": days,
    }


def chart_days_from_events(events: list[dict], filters: dict[str, str | int | bool | None]) -> dict:
    metric_keys = ("total_tokens", "total_with_cached_tokens")
    bucket_model_map: dict[str, dict[str, dict[str, int]]] = {}
    model_totals: dict[str, dict[str, int]] = {}
    daily_map: dict[str, dict] = {}
    start_day = parse_iso_day(str(filters["start_day"])) if filters.get("start_day") else None
    end_day = parse_iso_day(str(filters["end_day"])) if filters.get("end_day") else None

    event_days = [dt.date.fromisoformat(event["day"]) for event in events]
    if start_day is None and event_days:
        start_day = min(event_days)
    today = parse_iso_day(str(filters.get("today_day") or "")) or dt.date.today()
    if end_day is None:
        if event_days:
            end_day = max(today, max(event_days))
        else:
            end_day = today
    if start_day is None:
        start_day = end_day

    granularity = chart_granularity(start_day, end_day)
    for event in events:
        event_day = dt.date.fromisoformat(event["day"])
        daily = daily_map.setdefault(event["day"], {"day": event["day"], **{key: 0 for key in metric_keys}})
        for key in metric_keys:
            daily[key] += int(event[key])
        bucket_start, _, _ = chart_bucket_for_day(event_day, granularity)
        bucket_key = bucket_start.isoformat()
        model = event["model"]
        bucket_models = bucket_model_map.setdefault(bucket_key, {})
        bucket_totals = bucket_models.setdefault(model, {key: 0 for key in metric_keys})
        overall_totals = model_totals.setdefault(model, {key: 0 for key in metric_keys})
        for key in metric_keys:
            tokens = int(event[key])
            bucket_totals[key] += tokens
            overall_totals[key] += tokens

    days = []
    cursor, _, _ = chart_bucket_for_day(start_day, granularity)
    while cursor <= end_day:
        bucket_start, bucket_end, label = chart_bucket_for_day(cursor, granularity)
        day_key = bucket_start.isoformat()
        models = [
            {"model": model, **totals}
            for model, totals in sorted(
                bucket_model_map.get(day_key, {}).items(), key=lambda item: item[1]["total_tokens"], reverse=True
            )
            if totals["total_tokens"] or totals["total_with_cached_tokens"]
        ]
        days.append(
            {
                "day": day_key,
                "bucket_start": bucket_start.isoformat(),
                "bucket_end": bucket_end.isoformat(),
                "label": label,
                "total_tokens": sum(item["total_tokens"] for item in models),
                "total_with_cached_tokens": sum(item["total_with_cached_tokens"] for item in models),
                "models": models,
            }
        )
        if granularity == "month":
            cursor = add_months(bucket_start, 1)
        elif granularity == "week":
            cursor = bucket_start + dt.timedelta(days=7)
        else:
            cursor = bucket_start + dt.timedelta(days=1)

    models = [
        {"model": model, **totals}
        for model, totals in sorted(model_totals.items(), key=lambda item: item[1]["total_tokens"], reverse=True)
        if totals["total_tokens"] or totals["total_with_cached_tokens"]
    ]
    return {
        "range": filters["range"],
        "granularity": granularity,
        "range_start": start_day.isoformat() if start_day else None,
        "range_end": end_day.isoformat() if end_day else None,
        "days": days,
        "daily": sorted(daily_map.values(), key=lambda item: item["day"]),
        "models": models,
    }


def diagnostics_from_events(usage_events: list[dict], token_records: list[dict]) -> dict:
    rows: dict[tuple[str, str], dict] = {}

    def row_for(event: dict) -> dict:
        key = (event["hour"], event["model"])
        return rows.setdefault(
            key,
            {
                "hour": event["hour"],
                "model": event["model"],
                "raw_token_events": 0,
                "deduplicated_usage_updates": 0,
                "replayed_events": 0,
                "baseline_events": 0,
                "counter_resets": 0,
                "unverifiable_events": 0,
                "exact_usage_events": 0,
                "fallback_usage_events": 0,
                "deduplicated_usage_events": 0,
                "reported_tokens": 0,
                "deduplicated_tokens": 0,
            },
        )

    classification_keys = {
        "replayed_event": "replayed_events",
        "baseline_event": "baseline_events",
        "counter_reset": "counter_resets",
        "unverifiable_event": "unverifiable_events",
    }
    for event in token_records:
        row = row_for(event)
        row["raw_token_events"] += 1
        classification_key = classification_keys.get(event["classification"])
        if classification_key:
            row[classification_key] += 1
        row["reported_tokens"] += event["total_with_cached_tokens"]

    for event in usage_events:
        row = row_for(event)
        row["deduplicated_usage_updates"] += 1
        row[f'{event["source"]}_usage_events'] += 1
        row["deduplicated_tokens"] += event["total_with_cached_tokens"]

    result_rows = sorted(rows.values(), key=lambda row: (row["hour"], row["model"]), reverse=True)
    for row in result_rows:
        row["replay_rate"] = row["replayed_events"] / max(row["raw_token_events"], 1)
        row["estimated_local_overcount_tokens"] = max(row["reported_tokens"] - row["deduplicated_tokens"], 0)

    summary_keys = (
        "raw_token_events",
        "deduplicated_usage_updates",
        "replayed_events",
        "baseline_events",
        "counter_resets",
        "unverifiable_events",
        "exact_usage_events",
        "fallback_usage_events",
        "deduplicated_usage_events",
        "reported_tokens",
        "deduplicated_tokens",
    )
    summary = {key: sum(row[key] for row in result_rows) for key in summary_keys}
    summary["replay_rate"] = summary["replayed_events"] / max(summary["raw_token_events"], 1)
    summary["estimated_local_overcount_tokens"] = max(
        summary["reported_tokens"] - summary["deduplicated_tokens"], 0
    )
    return {"summary": summary, "rows": result_rows}


def load_usage(
    db_path: Path,
    range_name: str,
    start_day: str | None = None,
    end_day: str | None = None,
    ignore_auto_review: bool = False,
    chart_range: str | None = "30d",
    chart_start_day: str | None = None,
    chart_end_day: str | None = None,
    include_diagnostics: bool = False,
    provider: str = "codex",
    claude_projects_path: Path = DEFAULT_CLAUDE_PROJECTS,
    claude_db_path: Path = DEFAULT_CLAUDE_DB,
    today: dt.date | None = None,
) -> dict:
    provider = normalize_provider(provider)
    if provider == "claude":
        ignore_auto_review = False
        include_diagnostics = False
    filters = resolve_range(range_name, start_day, end_day, ignore_auto_review, today=today)
    chart_filters = resolve_chart_range(
        chart_range, chart_start_day, chart_end_day, ignore_auto_review, today=today
    )
    start_timestamps = [filters["start_ts"], chart_filters["start_ts"]]
    scan_start_ts = None if any(value is None for value in start_timestamps) else min(int(value) for value in start_timestamps)
    indexing = None
    if provider == "claude":
        indexing = index_claude_usage(claude_projects_path, claude_db_path)
        telemetry = {"usage_events": load_claude_events(claude_db_path, scan_start_ts), "token_events": []}
    else:
        telemetry = scan_token_telemetry(db_path, scan_start_ts, ignore_auto_review)
    events = filter_telemetry_events(telemetry["usage_events"], filters)
    chart_events = filter_telemetry_events(telemetry["usage_events"], chart_filters)
    diagnostic_token_records = filter_telemetry_events(telemetry["token_events"], filters)
    pricing = load_pricing()
    missing_price_models = set()

    daily_map: dict[str, dict] = {}
    model_map: dict[str, dict] = {}
    thread_ids = set()
    for event in events:
        event_cost, missing_model = token_cost_usd(event, pricing)
        if missing_model:
            missing_price_models.add(missing_model)
        event["cost_usd"] = event_cost
        thread_ids.add(event["thread_id"])
        day = event["day"]
        model = event["model"]
        daily = daily_map.setdefault(
            day,
            {
                "day": day,
                "sessions": set(),
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cached_input_tokens": 0,
                "raw_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
                "total_with_cached_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        daily["sessions"].add(event["thread_id"])
        model_row = model_map.setdefault(
            model,
            {
                "model": model,
                "sessions": set(),
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cached_input_tokens": 0,
                "raw_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
                "total_with_cached_tokens": 0,
                "cost_usd": 0.0,
                "daily_map": {},
            },
        )
        model_row["sessions"].add(event["thread_id"])
        model_daily = model_row["daily_map"].setdefault(
            day,
            {
                "day": day,
                "sessions": set(),
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cached_input_tokens": 0,
                "raw_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
                "total_with_cached_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        model_daily["sessions"].add(event["thread_id"])
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
            "raw_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
            "total_with_cached_tokens",
            "cost_usd",
        ):
            daily[key] += event[key]
            model_row[key] += event[key]
            model_daily[key] += event[key]

    day_rows = sorted(daily_map.values(), key=lambda row: row["day"])
    for row in day_rows:
        row["sessions"] = len(row["sessions"])

    model_rows = sorted(model_map.values(), key=lambda row: (row["total_tokens"], len(row["sessions"])), reverse=True)
    for row in model_rows:
        row["sessions"] = len(row["sessions"])
        daily_rows = sorted(row.pop("daily_map").values(), key=lambda item: item["day"], reverse=True)
        for item in daily_rows:
            item["sessions"] = len(item["sessions"])
        row["active_days"] = len(daily_rows)
        row["daily"] = daily_rows

    days = {row["day"] for row in day_rows}
    favorite = model_rows[0]["model"] if model_rows else "-"
    peak = max(day_rows, key=lambda row: row["total_tokens"], default=None)
    total_input = sum(row["input_tokens"] for row in day_rows)
    total_cache_creation = sum(row["cache_creation_input_tokens"] for row in day_rows)
    total_cache_read = sum(row["cache_read_input_tokens"] for row in day_rows)
    total_cached = sum(row["cached_input_tokens"] for row in day_rows)
    total_output = sum(row["output_tokens"] for row in day_rows)
    total_reasoning = sum(row["reasoning_output_tokens"] for row in day_rows)
    total_tokens = sum(row["total_tokens"] for row in day_rows)
    total_with_cached = sum(row["total_with_cached_tokens"] for row in day_rows)
    total_cost = sum(row["cost_usd"] for row in day_rows)

    provider_label = "Claude" if provider == "claude" else "Codex"
    result = {
        "provider": provider,
        "provider_label": provider_label,
        "data_source": "Claude JSONL → SQLite" if provider == "claude" else "SQLite + JSONL",
        "supports_diagnostics": provider == "codex",
        "range": filters["range"],
        "range_start": filters["start_day"],
        "range_end": filters["end_day"],
        "ignore_auto_review": bool(filters["ignore_auto_review"]),
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "sessions": len(thread_ids),
            "active_days": len(days),
            "input_tokens": total_input,
            "cache_creation_input_tokens": total_cache_creation,
            "cache_read_input_tokens": total_cache_read,
            "cached_input_tokens": total_cached,
            "output_tokens": total_output,
            "reasoning_output_tokens": total_reasoning,
            "total_tokens": total_tokens,
            "total_with_cached_tokens": total_with_cached,
            "cost_usd": total_cost,
        },
        "daily": day_rows,
        "models": model_rows,
        "chart": chart_days_from_events(chart_events, chart_filters),
        "pricing": {
            "source": pricing["source"],
            "url": pricing["url"],
            "loaded_at": pricing["loaded_at"],
            "error": pricing["error"],
            "missing_models": sorted(missing_price_models),
        },
        "favorite_model": favorite,
        "current_streak": current_streak(days),
        "longest_streak": longest_streak(days),
        "peak_day": peak["day"] if peak else "-",
        "peak_day_tokens": peak["total_tokens"] if peak else 0,
    }
    if indexing is not None:
        result["indexing"] = indexing
    if include_diagnostics:
        result["diagnostics"] = diagnostics_from_events(events, diagnostic_token_records)
    return result


PROVIDER_LABELS = {"all": "All", "codex": "Codex", "claude": "Claude", "opencode": "OpenCode"}
AGGREGATE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cached_input_tokens",
    "raw_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "total_with_cached_tokens",
    "cost_usd",
)


def resolve_timezone(value: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def event_from_active_row(row: dict, timezone: ZoneInfo, all_scope: bool, pricing: dict) -> tuple[dict, str | None]:
    timestamp = dt.datetime.fromisoformat(str(row["timestamp_utc"]).replace("Z", "+00:00"))
    local = timestamp.astimezone(timezone)
    provider = str(row["provider"])
    model = str(row["model"])
    model_label = f"{PROVIDER_LABELS[provider]} · {model}" if all_scope else model
    cache_read = int(row["cache_read_tokens"] or 0)
    cache_write = int(row["cache_write_tokens"] or 0)
    input_tokens = int(row["input_tokens"] or 0)
    output_tokens = int(row["output_tokens"] or 0)
    event = {
        "provider": provider,
        "thread_id": f'{provider}:{row["stream_key"]}',
        "timestamp": row["timestamp_utc"],
        "hour": local.strftime("%Y-%m-%d %H:00"),
        "day": local.date().isoformat(),
        "model": model_label,
        "model_key": f"{provider}:{model}",
        "source": row["semantics"],
        "classification": row["classification"],
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "cached_input_tokens": cache_read + cache_write,
        "raw_input_tokens": input_tokens + cache_read + cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": int(row["reasoning_tokens"] or 0),
        "total_tokens": input_tokens + output_tokens,
        "total_with_cached_tokens": input_tokens + output_tokens + cache_read + cache_write,
        "cost_kind": row["cost_kind"],
        "event_label": {
            "exact": "Exact response usage",
            "cumulative_fallback": "Cumulative fallback",
            "claude_metadata": "Claude metadata",
            "opencode_recorded": "Recorded by OpenCode",
        }.get(row["semantics"], str(row["semantics"])),
    }
    missing_model = None
    if row["cost_kind"] == "recorded" and row["cost_usd"] is not None:
        event["cost_usd"] = float(row["cost_usd"])
    else:
        event["cost_usd"], missing_model = token_cost_usd({**event, "model": model}, pricing)
        event["cost_kind"] = "unavailable" if missing_model else "estimated"
    return event, missing_model


def _aggregate_rows(events: list[dict]) -> tuple[list[dict], list[dict]]:
    daily_map: dict[str, dict] = {}
    model_map: dict[str, dict] = {}
    for event in events:
        day = event["day"]
        model = event["model"]
        daily = daily_map.setdefault(day, {"day": day, "sessions": set(), **{key: 0 for key in AGGREGATE_KEYS}})
        model_row = model_map.setdefault(
            event["model_key"],
            {
                "model": model,
                "model_key": event["model_key"],
                "provider": event["provider"],
                "sessions": set(),
                "daily_map": {},
                **{key: 0 for key in AGGREGATE_KEYS},
            },
        )
        model_daily = model_row["daily_map"].setdefault(
            day, {"day": day, "sessions": set(), **{key: 0 for key in AGGREGATE_KEYS}}
        )
        for target in (daily, model_row, model_daily):
            target["sessions"].add(event["thread_id"])
            for key in AGGREGATE_KEYS:
                target[key] += event[key]
    daily_rows = sorted(daily_map.values(), key=lambda row: row["day"])
    for row in daily_rows:
        row["sessions"] = len(row["sessions"])
    model_rows = sorted(model_map.values(), key=lambda row: (row["total_tokens"], len(row["sessions"])), reverse=True)
    for row in model_rows:
        row["sessions"] = len(row["sessions"])
        model_days = sorted(row.pop("daily_map").values(), key=lambda item: item["day"], reverse=True)
        for item in model_days:
            item["sessions"] = len(item["sessions"])
        row["active_days"] = len(model_days)
        row["daily"] = model_days
    return daily_rows, model_rows


def _usage_where(
    provider: str,
    start_ts: int | None,
    end_ts: int | None,
    ignore_auto_review: bool,
) -> tuple[str, list[object]]:
    clauses = ["model not in (select model from disabled_models)"]
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
    if ignore_auto_review:
        clauses.append("not (provider = 'codex' and model = ?)")
        params.append(AUTO_REVIEW_MODEL)
    return (" where " + " and ".join(clauses) if clauses else ""), params


def _register_local_day(conn: sqlite3.Connection, timezone: ZoneInfo) -> None:
    def local_day(occurred_at: int | float | None) -> str | None:
        if occurred_at is None:
            return None
        return dt.datetime.fromtimestamp(int(occurred_at), timezone).date().isoformat()

    conn.create_function("mm_local_day", 1, local_day, deterministic=True)


def _local_day_sql(timezone: ZoneInfo, start_ts: int | None, end_ts: int | None) -> str:
    if start_ts is None or end_ts is None or end_ts <= start_ts:
        return "mm_local_day(occurred_at)"
    offsets = set()
    cursor = start_ts
    last_ts = end_ts - 1
    while cursor <= last_ts:
        offset = dt.datetime.fromtimestamp(cursor, timezone).utcoffset()
        offsets.add(int(offset.total_seconds()) if offset else 0)
        if len(offsets) > 1:
            return "mm_local_day(occurred_at)"
        cursor += 24 * 60 * 60
    offset = dt.datetime.fromtimestamp(last_ts, timezone).utcoffset()
    offsets.add(int(offset.total_seconds()) if offset else 0)
    if len(offsets) != 1:
        return "mm_local_day(occurred_at)"
    return f"date(occurred_at + {offsets.pop()}, 'unixepoch')"


def _usage_time_bounds(
    conn: sqlite3.Connection,
    provider: str,
    start_ts: int | None,
    end_ts: int | None,
    ignore_auto_review: bool,
) -> tuple[int | None, int | None]:
    where, params = _usage_where(provider, start_ts, end_ts, ignore_auto_review)
    row = conn.execute(
        "select min(occurred_at) first_event, max(occurred_at) last_event from active_events" + where,
        params,
    ).fetchone()
    if row["first_event"] is None:
        return start_ts, end_ts
    return int(row["first_event"]), int(row["last_event"]) + 1


def _usage_group_rows(
    conn: sqlite3.Connection,
    provider: str,
    start_ts: int | None,
    end_ts: int | None,
    ignore_auto_review: bool,
    local_day_sql: str,
) -> list[dict]:
    where, params = _usage_where(provider, start_ts, end_ts, ignore_auto_review)
    rows = conn.execute(
        """
        select """ + local_day_sql + """ day,
               provider,
               model,
               coalesce(nullif(native_provider_id, ''), provider) native_provider_id,
               count(*) event_count,
               sum(input_tokens) input_tokens,
               sum(cache_read_tokens) cache_read_tokens,
               sum(cache_write_tokens) cache_write_tokens,
               sum(output_tokens) output_tokens,
               sum(reasoning_tokens) reasoning_tokens,
               sum(case when cost_kind = 'recorded' and cost_usd is not null then cost_usd else 0.0 end) recorded_cost,
               sum(case when cost_kind = 'recorded' and cost_usd is not null then 0 else input_tokens end) repriced_input_tokens,
               sum(case when cost_kind = 'recorded' and cost_usd is not null then 0 else cache_read_tokens end) repriced_cache_read_tokens,
               sum(case when cost_kind = 'recorded' and cost_usd is not null then 0 else cache_write_tokens end) repriced_cache_write_tokens,
               sum(case when cost_kind = 'recorded' and cost_usd is not null then 0 else output_tokens end) repriced_output_tokens,
               sum(case when cost_kind = 'recorded' and cost_usd is not null then 0 else 1 end) repriced_events,
               sum(classification = 'counter_reset') counter_resets,
               sum(classification = 'unverifiable_event') unverifiable_events,
               sum(semantics = 'exact') exact_usage_events,
               sum(semantics = 'cumulative_fallback') fallback_usage_events,
               min(occurred_at) first_occurred_at,
               min(canonical_event_id) first_event_id
        from active_events
        """ + where + """
        group by day, provider, model, native_provider_id
        order by first_occurred_at, first_event_id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _usage_session_rows(
    conn: sqlite3.Connection,
    provider: str,
    start_ts: int | None,
    end_ts: int | None,
    ignore_auto_review: bool,
    local_day_sql: str,
) -> list[dict]:
    where, params = _usage_where(provider, start_ts, end_ts, ignore_auto_review)
    rows = conn.execute(
        """
        select distinct """ + local_day_sql + """ day,
               provider,
               stream_key,
               model,
               coalesce(nullif(native_provider_id, ''), provider) native_provider_id
        from active_events
        """ + where,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _chart_group_rows(
    conn: sqlite3.Connection,
    provider: str,
    start_ts: int | None,
    end_ts: int | None,
    ignore_auto_review: bool,
    local_day_sql: str,
) -> list[dict]:
    where, params = _usage_where(provider, start_ts, end_ts, ignore_auto_review)
    rows = conn.execute(
        """
        select """ + local_day_sql + """ day,
               provider,
               model,
               sum(input_tokens + output_tokens) total_tokens,
               sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) total_with_cached_tokens,
               min(occurred_at) first_occurred_at,
               min(canonical_event_id) first_event_id
        from active_events
        """ + where + """
        group by day, provider, model
        order by first_occurred_at, first_event_id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _activity_timestamps(
    conn: sqlite3.Connection,
    provider: str,
    start_ts: int | None,
    end_ts: int | None,
    ignore_auto_review: bool,
) -> list[int]:
    query_start = start_ts - ACTIVITY_IDLE_TIMEOUT_SECONDS if start_ts is not None else None
    where, params = _usage_where(provider, query_start, end_ts, ignore_auto_review)
    return [
        int(row["occurred_at"])
        for row in conn.execute(
            "select occurred_at from active_events" + where
            + " order by occurred_at, canonical_event_id",
            params,
        ).fetchall()
    ]


def _repriced_models(
    conn: sqlite3.Connection,
    provider: str,
    start_ts: int | None,
    end_ts: int | None,
    ignore_auto_review: bool,
) -> list[str]:
    where, params = _usage_where(provider, start_ts, end_ts, ignore_auto_review)
    cost_clause = "(cost_kind != 'recorded' or cost_usd is null)"
    where += (" and " if where else " where ") + cost_clause
    return [
        str(row["model"])
        for row in conn.execute("select distinct model from active_events" + where, params).fetchall()
    ]


def _price_usage_group(row: dict, pricing: dict, all_scope: bool) -> dict:
    provider = str(row["provider"])
    model = str(row["model"])
    model_label = f"{PROVIDER_LABELS[provider]} · {model}" if all_scope else model
    input_tokens = int(row["input_tokens"] or 0)
    cache_read = int(row["cache_read_tokens"] or 0)
    cache_write = int(row["cache_write_tokens"] or 0)
    output_tokens = int(row["output_tokens"] or 0)
    recorded_cost = float(row["recorded_cost"] or 0)
    repriced_events = int(row["repriced_events"] or 0)
    estimated_cost = 0.0
    missing_model = None
    if repriced_events:
        estimated_cost, missing_model = token_cost_usd(
            {
                "model": model,
                "input_tokens": int(row["repriced_input_tokens"] or 0),
                "cache_creation_input_tokens": int(row["repriced_cache_write_tokens"] or 0),
                "cache_read_input_tokens": int(row["repriced_cache_read_tokens"] or 0),
                "cached_input_tokens": int(row["repriced_cache_read_tokens"] or 0)
                + int(row["repriced_cache_write_tokens"] or 0),
                "output_tokens": int(row["repriced_output_tokens"] or 0),
            },
            pricing,
        )
    return {
        **row,
        "model": model_label,
        "raw_model": model,
        "model_key": f"{provider}:{model}",
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "cached_input_tokens": cache_read + cache_write,
        "raw_input_tokens": input_tokens + cache_read + cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": int(row["reasoning_tokens"] or 0),
        "total_tokens": input_tokens + output_tokens,
        "total_with_cached_tokens": input_tokens + output_tokens + cache_read + cache_write,
        "cost_usd": recorded_cost + estimated_cost,
        "recorded_cost": recorded_cost,
        "estimated_cost": estimated_cost,
        "unavailable_cost_events": repriced_events if missing_model else 0,
        "missing_model": missing_model,
    }


def _chart_days_from_aggregate_rows(rows: list[dict], filters: dict[str, str | int | bool | None]) -> dict:
    metric_keys = ("total_tokens", "total_with_cached_tokens")
    bucket_model_map: dict[str, dict[str, dict[str, object]]] = {}
    model_totals: dict[str, dict[str, object]] = {}
    daily_map: dict[str, dict] = {}
    start_day = parse_iso_day(str(filters["start_day"])) if filters.get("start_day") else None
    end_day = parse_iso_day(str(filters["end_day"])) if filters.get("end_day") else None
    event_days = [dt.date.fromisoformat(str(row["day"])) for row in rows]
    if start_day is None and event_days:
        start_day = min(event_days)
    today = parse_iso_day(str(filters.get("today_day") or "")) or dt.date.today()
    if end_day is None:
        end_day = max(today, max(event_days)) if event_days else today
    if start_day is None:
        start_day = end_day

    granularity = chart_granularity(start_day, end_day)
    for row in rows:
        event_day = dt.date.fromisoformat(str(row["day"]))
        day_key = str(row["day"])
        daily = daily_map.setdefault(day_key, {"day": day_key, **{key: 0 for key in metric_keys}})
        for key in metric_keys:
            daily[key] += int(row[key])
        bucket_start, _, _ = chart_bucket_for_day(event_day, granularity)
        bucket_key = bucket_start.isoformat()
        model = str(row["model"])
        bucket_models = bucket_model_map.setdefault(bucket_key, {})
        metadata = {key: 0 for key in metric_keys}
        bucket_totals = bucket_models.setdefault(model, dict(metadata))
        overall_totals = model_totals.setdefault(model, dict(metadata))
        for key in metric_keys:
            tokens = int(row[key])
            bucket_totals[key] = int(bucket_totals[key]) + tokens
            overall_totals[key] = int(overall_totals[key]) + tokens

    days = []
    cursor, _, _ = chart_bucket_for_day(start_day, granularity)
    while cursor <= end_day:
        bucket_start, bucket_end, label = chart_bucket_for_day(cursor, granularity)
        day_key = bucket_start.isoformat()
        models = [
            {"model": model, **totals}
            for model, totals in sorted(
                bucket_model_map.get(day_key, {}).items(), key=lambda item: int(item[1]["total_tokens"]), reverse=True
            )
            if totals["total_tokens"] or totals["total_with_cached_tokens"]
        ]
        days.append({
            "day": day_key,
            "bucket_start": bucket_start.isoformat(),
            "bucket_end": bucket_end.isoformat(),
            "label": label,
            "total_tokens": sum(item["total_tokens"] for item in models),
            "total_with_cached_tokens": sum(item["total_with_cached_tokens"] for item in models),
            "models": models,
        })
        if granularity == "month":
            cursor = add_months(bucket_start, 1)
        elif granularity == "week":
            cursor = bucket_start + dt.timedelta(days=7)
        else:
            cursor = bucket_start + dt.timedelta(days=1)

    models = [
        {"model": model, **totals}
        for model, totals in sorted(model_totals.items(), key=lambda item: int(item[1]["total_tokens"]), reverse=True)
        if totals["total_tokens"] or totals["total_with_cached_tokens"]
    ]
    return {
        "range": filters["range"],
        "granularity": granularity,
        "range_start": start_day.isoformat(),
        "range_end": end_day.isoformat(),
        "days": days,
        "daily": sorted(daily_map.values(), key=lambda item: item["day"]),
        "models": models,
    }


def _diagnostics_from_unibase(unibase: Unibase, provider: str, groups: list[dict]) -> dict:
    providers = ("codex", "claude", "opencode") if provider == "all" else (provider,)
    source_rows = [row for row in unibase.sources() if row["provider"] in providers]
    provider_breakdown = {}
    for name in providers:
        provider_groups = [row for row in groups if row["provider"] == name]
        provider_sources = [row for row in source_rows if row["provider"] == name]
        provider_breakdown[name] = {
            "events": sum(int(row["event_count"]) for row in provider_groups),
            "sources": len(provider_sources),
            "stale_sources": sum(int(row["stale"]) for row in provider_sources),
            "error_sources": sum(row["discovery_status"] == "error" for row in provider_sources),
        }
    with unibase.connect(readonly=True) as conn:
        conflicts = int(
            conn.execute(
                """
                select count(*)
                from canonical_events canonical
                join active_events active
                  on active.provider = canonical.provider and active.event_key = canonical.event_key
                where canonical.conflict_state = 'conflict'
                  and active.model not in (select model from disabled_models)
                  and (? = 'all' or canonical.provider = ?)
                """,
                (provider, provider),
            ).fetchone()[0]
        )
    summary = {
        "raw_token_events": 0,
        "deduplicated_usage_updates": sum(int(row["event_count"]) for row in groups),
        "replayed_events": 0,
        "baseline_events": 0,
        "counter_resets": sum(int(row["counter_resets"] or 0) for row in groups),
        "unverifiable_events": sum(int(row["unverifiable_events"] or 0) for row in groups),
        "exact_usage_events": sum(int(row["exact_usage_events"] or 0) for row in groups),
        "fallback_usage_events": sum(int(row["fallback_usage_events"] or 0) for row in groups),
        "reported_tokens": sum(int(row["total_with_cached_tokens"]) for row in groups),
        "deduplicated_tokens": sum(int(row["total_with_cached_tokens"]) for row in groups),
        "replay_rate": 0,
        "estimated_local_overcount_tokens": 0,
        "conflicts": conflicts,
    }
    safe_sources = [
        {
            "source_id": row["source_id"],
            "provider": row["provider"],
            "kind": row["kind"],
            "label": row["label"],
            "relative_name": row["relative_name"],
            "enabled": bool(row["enabled"]),
            "status": row["discovery_status"],
            "stale": bool(row["stale"]),
            "file_count": row["file_count"],
            "event_count": row["event_count"],
            "last_successful_scan": row["last_successful_scan"],
            "error": row["error"],
        }
        for row in source_rows
    ]
    return {"summary": summary, "rows": [], "provider_breakdown": provider_breakdown, "sources": safe_sources}


def load_unibase_usage(
    unibase_path: Path,
    range_name: str = "all",
    start_day: str | None = None,
    end_day: str | None = None,
    ignore_auto_review: bool = False,
    chart_range: str | None = "30d",
    chart_start_day: str | None = None,
    chart_end_day: str | None = None,
    include_diagnostics: bool = False,
    provider: str = "all",
    timezone_name: str = "UTC",
    workdays_only: bool = False,
) -> dict:
    started_at = time.perf_counter()
    provider = normalize_provider(provider)
    unibase = Unibase(unibase_path, migrate=False)
    settings = unibase.settings()
    merge_models = provider == "all" and bool(settings["merge_models_across_providers"])
    timezone = resolve_timezone(timezone_name)
    today = dt.datetime.now(timezone).date()
    filters = resolve_range(range_name, start_day, end_day, bool(ignore_auto_review), today=today)
    chart_filters = resolve_chart_range(
        chart_range, chart_start_day, chart_end_day, bool(ignore_auto_review), today=today
    )
    usage_start_ts, usage_end_ts = _local_range_timestamps(filters, timezone)
    chart_start_ts, chart_end_ts = _local_range_timestamps(chart_filters, timezone)
    pricing = load_pricing()
    sql_started_at = time.perf_counter()
    envelope_start_ts = (
        None if usage_start_ts is None or chart_start_ts is None else min(usage_start_ts, chart_start_ts)
    )
    envelope_end_ts = None if usage_end_ts is None or chart_end_ts is None else max(usage_end_ts, chart_end_ts)
    with unibase.connect(readonly=True) as conn:
        conn.execute("begin")
        _register_local_day(conn, timezone)
        actual_start_ts, actual_end_ts = _usage_time_bounds(
            conn, provider, envelope_start_ts, envelope_end_ts, bool(ignore_auto_review)
        )
        local_day_sql = _local_day_sql(timezone, actual_start_ts, actual_end_ts)
        raw_groups = _usage_group_rows(
            conn, provider, usage_start_ts, usage_end_ts, bool(ignore_auto_review), local_day_sql
        )
        session_rows = _usage_session_rows(
            conn, provider, usage_start_ts, usage_end_ts, bool(ignore_auto_review), local_day_sql
        )
        same_chart_range = (usage_start_ts, usage_end_ts) == (chart_start_ts, chart_end_ts)
        raw_chart_rows = raw_groups if same_chart_range else _chart_group_rows(
            conn, provider, chart_start_ts, chart_end_ts, bool(ignore_auto_review), local_day_sql
        )
        activity_timestamps = _activity_timestamps(
            conn, provider, chart_start_ts, chart_end_ts, bool(ignore_auto_review)
        )
        repriced_models = _repriced_models(
            conn, provider, envelope_start_ts, envelope_end_ts, bool(ignore_auto_review)
        )
        conn.commit()
    groups = [_price_usage_group(row, pricing, provider == "all" and not merge_models) for row in raw_groups]
    print(
        f"[MeterMesh timing] usage SQL aggregates: {(time.perf_counter() - sql_started_at) * 1000:.0f} ms; "
        f"groups={len(groups)}; sessions={len(session_rows)}; provider={provider}",
        flush=True,
    )

    total_sessions: set[tuple[str, str]] = set()
    daily_sessions: dict[str, set[tuple[str, str]]] = {}
    model_sessions: dict[str, set[tuple[str, str]]] = {}
    model_day_sessions: dict[tuple[str, str], set[tuple[str, str]]] = {}
    provider_sessions: dict[str, set[str]] = {name: set() for name in ("codex", "claude", "opencode")}
    for row in session_rows:
        name = str(row["provider"])
        session = (name, str(row["stream_key"]))
        model_key = str(row["model"]) if merge_models else f'{name}:{row["model"]}'
        day = str(row["day"])
        total_sessions.add(session)
        daily_sessions.setdefault(day, set()).add(session)
        model_sessions.setdefault(model_key, set()).add(session)
        model_day_sessions.setdefault((model_key, day), set()).add(session)
        provider_sessions[name].add(str(row["stream_key"]))

    daily_map: dict[str, dict] = {}
    model_map: dict[str, dict] = {}
    provider_breakdown = {
        name: {
            "events": 0,
            "sessions": 0,
            "total_tokens": 0,
            "total_with_cached_tokens": 0,
            "cost": {"recorded": 0.0, "estimated": 0.0, "unavailable": 0},
        }
        for name in ("codex", "claude", "opencode")
    }
    missing_models = set()
    for model in repriced_models:
        if model_price_key(model, pricing["models"]) is None and model_price_key(model, pricing["fallback"]) is None:
            missing_models.add(model)
    cost_breakdown = {"recorded": 0.0, "estimated": 0.0, "unavailable": 0}
    for row in groups:
        day = str(row["day"])
        model_key = str(row["raw_model"]) if merge_models else str(row["model_key"])
        daily = daily_map.setdefault(day, {"day": day, **{key: 0 for key in AGGREGATE_KEYS}})
        model_row = model_map.setdefault(
            model_key,
            {
                "model": row["raw_model"] if merge_models else row["model"],
                "model_key": model_key,
                "provider": "all" if merge_models else row["provider"],
                "daily_map": {},
                "first_occurred_at": row["first_occurred_at"],
                "first_event_id": row["first_event_id"],
                **{key: 0 for key in AGGREGATE_KEYS},
            },
        )
        model_daily = model_row["daily_map"].setdefault(
            day, {"day": day, **{key: 0 for key in AGGREGATE_KEYS}}
        )
        for target in (daily, model_row, model_daily):
            for key in AGGREGATE_KEYS:
                target[key] += row[key]
        name = str(row["provider"])
        provider_row = provider_breakdown[name]
        provider_row["events"] += int(row["event_count"])
        provider_row["total_tokens"] += int(row["total_tokens"])
        provider_row["total_with_cached_tokens"] += int(row["total_with_cached_tokens"])
        provider_row["cost"]["recorded"] += float(row["recorded_cost"])
        provider_row["cost"]["estimated"] += float(row["estimated_cost"])
        provider_row["cost"]["unavailable"] += int(row["unavailable_cost_events"])
        cost_breakdown["recorded"] += float(row["recorded_cost"])
        cost_breakdown["estimated"] += float(row["estimated_cost"])
        cost_breakdown["unavailable"] += int(row["unavailable_cost_events"])
        if row["missing_model"]:
            missing_models.add(str(row["missing_model"]))

    daily_rows = sorted(daily_map.values(), key=lambda row: row["day"])
    for row in daily_rows:
        row["sessions"] = len(daily_sessions.get(str(row["day"]), set()))
    model_rows = list(model_map.values())
    for row in model_rows:
        model_key = str(row["model_key"])
        row["sessions"] = len(model_sessions.get(model_key, set()))
        model_days = sorted(row.pop("daily_map").values(), key=lambda item: item["day"], reverse=True)
        for item in model_days:
            item["sessions"] = len(model_day_sessions.get((model_key, str(item["day"])), set()))
        row["active_days"] = len(model_days)
        row["daily"] = model_days
    model_rows.sort(
        key=lambda row: (
            -int(row["total_tokens"]),
            -int(row["sessions"]),
            int(row["first_occurred_at"]),
            int(row["first_event_id"]),
        )
    )
    for row in model_rows:
        row.pop("first_occurred_at")
        row.pop("first_event_id")
    for name, row in provider_breakdown.items():
        row["sessions"] = len(provider_sessions[name])

    days = {row["day"] for row in daily_rows}
    peak = max(daily_rows, key=lambda row: row["total_tokens"], default=None)
    totals = {key: sum(row[key] for row in daily_rows) for key in AGGREGATE_KEYS}
    totals["sessions"] = len(total_sessions)
    totals["active_days"] = len(days)
    if same_chart_range:
        chart_rows = [
            {
                "day": row["day"],
                "model": row["model"],
                "total_tokens": row["total_tokens"],
                "total_with_cached_tokens": row["total_with_cached_tokens"],
            }
            for row in groups
        ]
    else:
        chart_rows = [
            {
                **row,
                "model": str(row["model"])
                if merge_models or provider != "all"
                else f'{PROVIDER_LABELS[str(row["provider"])]} · {row["model"]}',
            }
            for row in raw_chart_rows
        ]
    chart = _chart_days_from_aggregate_rows(chart_rows, chart_filters)
    favorite_model = model_rows[0]["model"] if model_rows else "-"
    if provider == "all" and favorite_model != "-":
        favorite_model = favorite_model.split(" · ", 1)[-1]
    result = {
        "provider": provider,
        "provider_label": PROVIDER_LABELS[provider],
        "data_source": "Unibase",
        "supports_diagnostics": True,
        "generation": settings["generation"],
        "range": filters["range"],
        "range_start": filters["start_day"],
        "range_end": filters["end_day"],
        "timezone": getattr(timezone, "key", "UTC"),
        "merge_models_across_providers": bool(settings["merge_models_across_providers"]),
        "ignore_auto_review": bool(ignore_auto_review),
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": totals,
        "cost": cost_breakdown,
        "provider_breakdown": provider_breakdown,
        "daily": daily_rows,
        "models": model_rows,
        "chart": chart,
        "activity": activity_from_timestamps(
            activity_timestamps,
            chart_filters,
            timezone,
            non_working_weekdays=load_non_working_weekdays(settings["non_working_weekdays"]),
            workdays_only=workdays_only,
        ),
        "pricing": {
            "source": pricing["source"], "url": pricing["url"], "loaded_at": pricing["loaded_at"],
            "error": pricing["error"], "missing_models": sorted(missing_models),
        },
        "favorite_model": favorite_model,
        "current_streak": current_streak(days),
        "longest_streak": longest_streak(days),
        "peak_day": peak["day"] if peak else "-",
        "peak_day_tokens": peak["total_tokens"] if peak else 0,
    }
    if include_diagnostics:
        result["diagnostics"] = _diagnostics_from_unibase(unibase, provider, groups)
    print(
        f"[MeterMesh timing] usage aggregation total: {(time.perf_counter() - started_at) * 1000:.0f} ms; "
        f"groups={len(groups)}; chart_groups={len(chart_rows)}",
        flush=True,
    )
    return result


def _local_range_timestamps(filters: dict, timezone: ZoneInfo) -> tuple[int | None, int | None]:
    start_day = parse_iso_day(str(filters["start_day"])) if filters.get("start_day") else None
    end_day = parse_iso_day(str(filters["end_day"])) if filters.get("end_day") else None
    start_ts = None
    end_ts = None
    if start_day:
        start_ts = int(dt.datetime.combine(start_day, dt.time.min, timezone).timestamp())
    if end_day:
        end_ts = int(dt.datetime.combine(end_day + dt.timedelta(days=1), dt.time.min, timezone).timestamp())
    return start_ts, end_ts


def _request_item(row: dict, timezone: ZoneInfo, all_scope: bool, pricing: dict) -> dict:
    event, _ = event_from_active_row(row, timezone, all_scope, pricing)
    return {
        "provider": event["provider"],
        "timestamp": event["timestamp"],
        "local_timestamp": dt.datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00")).astimezone(timezone).isoformat(),
        "model": event["model"],
        "input": event["input_tokens"],
        "output": event["output_tokens"],
        "reasoning": event["reasoning_output_tokens"],
        "cache_read": event["cache_read_input_tokens"],
        "cache_write": event["cache_creation_input_tokens"],
        "cached": event["cache_read_input_tokens"],
        "total": event["total_tokens"],
        "total_with_cache": event["total_with_cached_tokens"],
        "event_label": event["event_label"],
        "classification": event["classification"],
        "cost": event["cost_usd"] if event["cost_kind"] != "unavailable" else None,
        "cost_kind": event["cost_kind"],
        "cost_label": {
            "recorded": "Recorded by OpenCode",
            "estimated": "Estimated",
            "unavailable": "Unavailable",
        }[event["cost_kind"]],
        "_internal_id": int(row["canonical_event_id"]),
        "_occurred_at": int(row["occurred_at"]),
    }


def _bucket_start(timestamp: str, group: str, timezone: ZoneInfo) -> dt.datetime:
    local = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone)
    if group == "24h":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = {"1m": 1, "15m": 15, "30m": 30, "1h": 60, "6h": 360, "12h": 720}[group]
    minute_of_day = local.hour * 60 + local.minute
    floored = minute_of_day - minute_of_day % minutes
    return local.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def _bucket_end(bucket_start: str, group: str, timezone: ZoneInfo) -> dt.datetime:
    start = dt.datetime.fromisoformat(bucket_start).astimezone(timezone)
    start_key = start.isoformat()
    start_utc = start.astimezone(dt.timezone.utc)
    for minute in range(1, 26 * 60 + 1):
        candidate_utc = start_utc + dt.timedelta(minutes=minute)
        if _bucket_start(candidate_utc.isoformat(), group, timezone).isoformat() != start_key:
            return candidate_utc.astimezone(timezone)
    raise RuntimeError("Could not resolve request bucket end")


def _public_request_item(item: dict) -> dict:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _requests_snapshot_digest(rows) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(dict(row), sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()[:20]


class SnapshotConflict(RuntimeError):
    pass


def load_requests(
    unibase_path: Path,
    *,
    provider: str = "all",
    range_name: str = "all",
    start_day: str | None = None,
    end_day: str | None = None,
    timezone_name: str = "UTC",
    ignore_auto_review: bool = False,
    group: str = "none",
    page: int = 1,
    page_size: int = 25,
    snapshot: str | None = None,
    bucket_start: str | None = None,
    child_page: int = 1,
) -> dict:
    started_at = time.perf_counter()
    provider = normalize_provider(provider)
    if group not in REQUEST_GROUPS:
        raise ValueError("Invalid request grouping")
    if page < 1:
        raise ValueError("Page must be at least 1")
    if page_size not in REQUEST_PAGE_SIZES:
        raise ValueError("Invalid page size")
    if child_page < 1:
        raise ValueError("Child page must be at least 1")
    if bucket_start and group == "none":
        raise ValueError("Bucket pagination requires grouping")
    timezone = resolve_timezone(timezone_name)
    unibase = Unibase(unibase_path, migrate=False)
    with unibase.connect(readonly=True) as conn:
        conn.execute("begin")
        settings = conn.execute("select generation from app_settings where id = 1").fetchone()
        filters = resolve_range(
            range_name,
            start_day,
            end_day,
            bool(ignore_auto_review),
            today=dt.datetime.now(timezone).date(),
        )
        start_ts, end_ts = _local_range_timestamps(filters, timezone)
        snapshot_generation = int(settings["generation"])
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
        if ignore_auto_review:
            clauses.append("not (provider = 'codex' and model = ?)")
            params.append(AUTO_REVIEW_MODEL)
        clauses.append("model not in (select model from disabled_models)")
        base_where = " where " + " and ".join(clauses) if clauses else ""
        max_event_id = int(conn.execute(
            "select coalesce(max(canonical_event_id), 0) from active_events" + base_where,
            params,
        ).fetchone()[0])
        if snapshot:
            try:
                snapshot_generation_text, max_event_text, expected_digest = snapshot.split(":", 2)
                snapshot_generation = int(snapshot_generation_text)
                max_event_id = int(max_event_text)
            except (ValueError, AttributeError) as exc:
                raise ValueError("Invalid snapshot") from exc
        snapshot_clauses = [*clauses, "canonical_event_id <= ?"]
        snapshot_params = [*params, max_event_id]
        snapshot_where = " where " + " and ".join(snapshot_clauses)
        digest_sql = """
            select count(*) row_count,
                   coalesce(sum(canonical_event_id), 0) canonical_id_sum,
                   coalesce(sum(event_variant_id), 0) variant_id_sum,
                   coalesce(sum(occurred_at), 0) occurred_at_sum,
                   coalesce(sum(input_tokens), 0) input_sum,
                   coalesce(sum(cache_read_tokens), 0) cache_read_sum,
                   coalesce(sum(cache_write_tokens), 0) cache_write_sum,
                   coalesce(sum(output_tokens), 0) output_sum,
                   coalesce(sum(reasoning_tokens), 0) reasoning_sum,
                   coalesce(sum(cost_usd), 0) cost_sum,
                   coalesce(sum(length(model)), 0) model_length_sum,
                   coalesce(sum(length(stream_key)), 0) stream_length_sum,
                    sum(cost_kind = 'recorded') recorded_count,
                    sum(cost_kind = 'estimated') estimated_count,
                     sum(cost_kind = 'unavailable') unavailable_count
            from active_events
        """ + snapshot_where
        digest_started_at = time.perf_counter()
        snapshot_digest = _requests_snapshot_digest(conn.execute(digest_sql, snapshot_params))
        print(
            f"[MeterMesh timing] requests snapshot digest: "
            f"{(time.perf_counter() - digest_started_at) * 1000:.0f} ms",
            flush=True,
        )
        if snapshot and snapshot_digest != expected_digest:
            raise SnapshotConflict("Requests snapshot changed; reload the first page")
        if group == "none":
            total_rows = int(conn.execute(
                "select count(*) from active_events" + snapshot_where,
                snapshot_params,
            ).fetchone()[0])
            total_pages = max((total_rows + page_size - 1) // page_size, 1)
            page_rows = [dict(row) for row in conn.execute(
                "select * from active_events" + snapshot_where
                + " order by occurred_at desc, canonical_event_id desc limit ? offset ?",
                [*snapshot_params, page_size, (page - 1) * page_size],
            )]
            ordered_keys = []
            page_keys = []
            branch_data = {}
        else:
            ordered_keys = []
            seen_keys = set()
            bucket_rows = conn.execute(
                "select timestamp_utc from active_events" + snapshot_where
                + " order by occurred_at desc, canonical_event_id desc",
                snapshot_params,
            )
            for row in bucket_rows:
                key = _bucket_start(row["timestamp_utc"], group, timezone).isoformat()
                if key not in seen_keys:
                    seen_keys.add(key)
                    ordered_keys.append(key)
            total_rows = len(ordered_keys)
            total_pages = max((total_rows + page_size - 1) // page_size, 1)
            start = (page - 1) * page_size
            page_keys = [bucket_start] if bucket_start in seen_keys else []
            if not bucket_start:
                page_keys = ordered_keys[start : start + page_size]
            selected_keys = set(page_keys)
            branch_data = {
                key: {
                    "count": 0,
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "children_rows": [],
                }
                for key in page_keys
            }
            child_offset = (child_page - 1) * page_size
            event_rows = conn.execute(
                "select * from active_events" + snapshot_where
                + " order by occurred_at desc, canonical_event_id desc",
                snapshot_params,
            )
            for row in event_rows:
                key = _bucket_start(row["timestamp_utc"], group, timezone).isoformat()
                if key in selected_keys:
                    branch = branch_data[key]
                    child_index = branch["count"]
                    branch["count"] += 1
                    branch["input"] += int(row["input_tokens"] or 0)
                    branch["output"] += int(row["output_tokens"] or 0)
                    branch["reasoning"] += int(row["reasoning_tokens"] or 0)
                    branch["cache_read"] += int(row["cache_read_tokens"] or 0)
                    branch["cache_write"] += int(row["cache_write_tokens"] or 0)
                    if bucket_start and child_offset <= child_index < child_offset + page_size:
                        branch["children_rows"].append(dict(row))
        conn.commit()

    snapshot_value = f"{snapshot_generation}:{max_event_id}:{snapshot_digest}"
    pricing = load_pricing() if group == "none" or bucket_start else None
    if group == "none":
        page_items = [
            _public_request_item(_request_item(row, timezone, provider == "all", pricing))
            for row in page_rows
        ]
    else:
        page_items = []
        for key in page_keys:
            branch = branch_data[key]
            children = [
                _public_request_item(_request_item(row, timezone, provider == "all", pricing))
                for row in branch["children_rows"]
            ]
            child_total_pages = max((branch["count"] + page_size - 1) // page_size, 1)
            page_items.append({
                "bucket_start": key,
                "bucket_end": _bucket_end(key, group, timezone).isoformat(),
                "count": branch["count"],
                "input": branch["input"],
                "output": branch["output"],
                "reasoning": branch["reasoning"],
                "cache_read": branch["cache_read"],
                "cache_write": branch["cache_write"],
                "total": branch["input"] + branch["output"],
                "total_with_cache": (
                    branch["input"] + branch["output"]
                    + branch["cache_read"] + branch["cache_write"]
                ),
                "children": children,
                "child_page": child_page if bucket_start else 0,
                "child_page_size": page_size,
                "child_total_pages": child_total_pages,
                "child_has_previous": bool(bucket_start and child_page > 1),
                "child_has_next": bool(bucket_start and child_page < child_total_pages),
            })
    result = {
        "provider": provider,
        "group": group,
        "timezone": getattr(timezone, "key", "UTC"),
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "total_top_level_rows": total_rows,
        "has_previous": page > 1 and page <= total_pages,
        "has_next": page < total_pages,
        "snapshot": snapshot_value,
        "items": page_items,
    }
    print(
        f"[MeterMesh timing] requests load total: {(time.perf_counter() - started_at) * 1000:.0f} ms; "
        f"group={group}; page_items={len(page_items)}",
        flush=True,
    )
    return result


def display_path(path: Path) -> str:
    try:
        return "~/" + path.expanduser().relative_to(Path.home()).as_posix()
    except ValueError:
        return path.name


def unibase_freshness(database: Unibase, provider: str = "all") -> str | None:
    with database.connect(readonly=True) as conn:
        clauses = ["enabled = 1", "discovery_status != 'not_installed'"]
        params: list[object] = []
        if provider != "all":
            clauses.append("provider = ?")
            params.append(provider)
        row = conn.execute(
            """
            select count(*) enabled_count,
                   sum(last_successful_scan is not null and stale = 0 and discovery_status = 'ready') ready_count,
                   coalesce(
                       min(case when kind = 'live' then last_successful_scan end),
                       min(last_successful_scan)
                   ) fresh_at
            from sources where """ + " and ".join(clauses),
            params,
        ).fetchone()
        if not row["enabled_count"] or row["ready_count"] != row["enabled_count"]:
            return None
        return row["fresh_at"]


def source_parser_outdated(database: Unibase, source: dict) -> bool:
    expected = {
        "codex": CODEX_PARSER_VERSION,
        "claude": CLAUDE_PARSER_VERSION,
        "opencode": OPENCODE_PARSER_VERSION,
    }[source["provider"]]
    with database.connect(readonly=True) as conn:
        return conn.execute(
            "select 1 from source_files where source_id = ? and parser_version != ? limit 1",
            (source["source_id"], expected),
        ).fetchone() is not None


def settings_payload(unibase_path: Path) -> dict:
    database = Unibase(unibase_path, migrate=False)
    settings = database.settings()
    groups = {provider: [] for provider in ("codex", "claude", "opencode")}
    model_groups = {group: [] for group in ("gpt", "claude", "others")}
    with database.connect(readonly=True) as conn:
        rows = conn.execute(
            """
            select s.*, coalesce(sum(sf.size), 0) size_bytes
            from sources s
            left join source_files sf on sf.source_id = s.source_id
            group by s.source_id
            order by s.provider, s.priority desc, coalesce(s.snapshot_date, '') desc, s.source_id
            """
        ).fetchall()
        model_rows = conn.execute(
            """
            select known.model, disabled.model is null enabled
            from known_models known
            left join disabled_models disabled on disabled.model = known.model
            order by lower(known.model), known.model
            """
        ).fetchall()
    for row in rows:
        groups[row["provider"]].append({
            "source_id": row["source_id"],
            "label": row["label"],
            "relative_name": row["relative_name"],
            "path": display_path(Path(row["root_path"])) if row["kind"] == "live" else row["relative_name"],
            "kind": row["kind"],
            "original": row["kind"] == "live",
            "enabled": bool(row["enabled"]),
            "layout": "Original source" if row["kind"] == "live" else "Snapshot" if row["kind"] == "normalized_backup" else "Imported source",
            "snapshot_date": row["snapshot_date"],
            "status": row["discovery_status"],
            "stale": bool(row["stale"]),
            "file_count": row["file_count"],
            "event_count": row["event_count"],
            "size_bytes": int(row["size_bytes"]),
            "last_successful_scan": row["last_successful_scan"],
            "error": row["error"],
        })
    for row in model_rows:
        model = str(row["model"])
        normalized = model.upper()
        group = "gpt" if "GPT" in normalized else "claude" if "CLAUDE" in normalized else "others"
        model_groups[group].append({"model": model, "enabled": bool(row["enabled"])})
    with database.connect(readonly=True) as conn:
        counts = {
            "active_events": int(conn.execute("select count(*) from active_events").fetchone()[0]),
            "retained_variants": int(conn.execute("select count(*) from event_variants").fetchone()[0]),
            "sources": int(conn.execute("select count(*) from sources").fetchone()[0]),
        }
    return {
        "revision": settings["revision"],
        "merge_models_across_providers": bool(settings["merge_models_across_providers"]),
        "non_working_weekdays": load_non_working_weekdays(settings["non_working_weekdays"]),
        "sources": groups,
        "models": model_groups,
        "unibase": {
            "path": display_path(unibase_path),
            "generation": settings["generation"],
            "state": settings["state"],
            "counts": counts,
            "fresh_at": unibase_freshness(database),
            "current_operation": database.active_operation(),
        },
    }


def import_registered_source(
    database: Unibase,
    source: dict,
    opencode_live_db: Path | None = None,
    codex_state_db: Path | None = None,
    require_codex_state_inventory: bool = False,
    force_full_scan: bool = False,
    non_destructive: bool = False,
) -> None:
    with OPERATION_LOCKS.acquire("maintenance"):
        if database.settings()["state"] == "reset_empty":
            return
        try:
            with database.source_transaction():
                if source["provider"] == "codex":
                    state_path = codex_state_db if source["kind"] == "live" else None
                    import_codex_source(
                        database,
                        source,
                        state_path=state_path,
                        require_state_inventory=require_codex_state_inventory and source["kind"] == "live",
                        force_full_scan=force_full_scan,
                        non_destructive=non_destructive,
                    )
                elif source["provider"] == "claude":
                    import_claude_source(
                        database,
                        source,
                        force_full_scan=force_full_scan,
                        non_destructive=non_destructive,
                    )
                else:
                    override = opencode_live_db if source["kind"] == "live" else None
                    import_opencode_source(
                        database,
                        source,
                        db_override=override,
                        force_full_scan=force_full_scan,
                        non_destructive=non_destructive,
                    )
        except Exception as exc:
            database.mark_source_error(str(source["source_id"]), exc)
            raise


def enabled_sources_for_import(
    database: Unibase,
    opencode_live_db: Path | None = None,
    codex_state_db: Path | None = None,
) -> list[dict]:
    sources = []
    for source in database.sources():
        if not source["enabled"]:
            continue
        if source["kind"] == "live":
            root = Path(source["root_path"])
            if source["provider"] == "codex":
                markers = [codex_state_db or root / "state_5.sqlite", root / "sessions", root / "archived_sessions"]
            elif source["provider"] == "claude":
                markers = [root if root.name == "projects" else root / "projects"]
            else:
                markers = [opencode_live_db or root / "opencode.db"]
            # An absent installation is optional; a disappearing indexed source
            # is an error. Check before a staging reset erases file checkpoints.
            if (
                not any(path.exists() for path in markers)
                and not source["file_count"]
                and not source["event_count"]
                and not database.source_file_keys(source["source_id"])
            ):
                with database.connect() as conn:
                    conn.execute(
                        "update sources set discovery_status = 'not_installed', stale = 0, error = null where source_id = ?",
                        (source["source_id"],),
                    )
                continue
        sources.append(source)
    return sources


def refresh_enabled_sources(
    unibase_path: Path,
    opencode_live_db: Path | None = None,
    *,
    codex_root: Path | None = None,
    codex_state_db: Path | None = None,
    claude_root: Path | None = None,
    force_full_scan: bool = False,
) -> None:
    started_at = time.perf_counter()
    database = Unibase(unibase_path)
    errors = []
    with OPERATION_LOCKS.acquire("maintenance"):
        if database.settings()["state"] == "reset_empty":
            raise OperationConflict("Unibase is empty; use Reset to rebuild it")
        if database.active_operation():
            raise OperationConflict("A Unibase operation is already running")
        discovery_started_at = time.perf_counter()
        register_default_sources(
            database,
            codex_root=codex_root,
            claude_root=claude_root,
            opencode_root=opencode_live_db.parent if opencode_live_db else None,
        )
        sources = enabled_sources_for_import(database, opencode_live_db, codex_state_db)
        print(
            f"[MeterMesh timing] source discovery: {(time.perf_counter() - discovery_started_at) * 1000:.0f} ms; "
            f"registered={len(sources)}",
            flush=True,
        )
        for source in sources:
            if not source["enabled"] or source["discovery_status"] in {"incomplete", "ambiguous", "unavailable"}:
                continue
            if (
                not force_full_scan
                and not source["stale"]
                and source["kind"] != "live"
                and source["discovery_status"] == "ready"
                and source["last_successful_scan"]
                and not source_parser_outdated(database, source)
            ):
                continue
            source_force_full_scan = force_full_scan or bool(source["stale"])
            source_started_at = time.perf_counter()
            source_name = f'{source["provider"]}/{source["relative_name"]}'
            try:
                import_registered_source(
                    database,
                    source,
                    opencode_live_db,
                    codex_state_db,
                    force_full_scan=source_force_full_scan,
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[MeterMesh timing] source import {source_name}: "
                    f"{(time.perf_counter() - source_started_at) * 1000:.0f} ms; "
                    f"error={exc.__class__.__name__}: {exc}",
                    flush=True,
                )
                errors.append(f'{source["provider"]}: {sanitize_error(exc)}')
                continue
            print(
                f"[MeterMesh timing] source import {source_name}: "
                f"{(time.perf_counter() - source_started_at) * 1000:.0f} ms; status=ok",
                flush=True,
            )
    print(
        f"[MeterMesh timing] source refresh total: {(time.perf_counter() - started_at) * 1000:.0f} ms",
        flush=True,
    )
    if errors:
        raise RuntimeError("; ".join(errors))


def source_refresh_running() -> bool:
    with SOURCE_REFRESH_STATE_LOCK:
        return SOURCE_REFRESH_RUNNING


def source_refresh_status() -> dict[str, object]:
    with SOURCE_REFRESH_STATE_LOCK:
        return {
            "state": "running" if SOURCE_REFRESH_RUNNING else "idle",
            "reason": SOURCE_REFRESH_REASON,
            "started_at": SOURCE_REFRESH_STARTED_AT,
            "completed_at": SOURCE_REFRESH_COMPLETED_AT,
            "error": SOURCE_REFRESH_ERROR,
        }


def wait_for_source_refresh() -> None:
    while source_refresh_running():
        time.sleep(0.1)


def source_refresh_cooldown_remaining(now: float | None = None) -> float:
    with SOURCE_REFRESH_STATE_LOCK:
        completed_at = SOURCE_REFRESH_COMPLETED_MONOTONIC
    if not completed_at:
        return 0.0
    current = time.monotonic() if now is None else now
    return max(SOURCE_REFRESH_SCHEDULER_COOLDOWN_SECONDS - (current - completed_at), 0.0)


def wait_for_source_refresh_window() -> None:
    while True:
        if source_refresh_running():
            wait_for_source_refresh()
            continue
        cooldown = source_refresh_cooldown_remaining()
        if cooldown:
            time.sleep(cooldown)
            continue
        return


def schedule_enabled_sources_refresh(
    unibase_path: Path,
    opencode_live_db: Path | None = None,
    *,
    codex_root: Path | None = None,
    codex_state_db: Path | None = None,
    claude_root: Path | None = None,
    reason: str,
    force_full_scan: bool = False,
    respect_cooldown: bool = False,
) -> bool:
    global SOURCE_REFRESH_ERROR, SOURCE_REFRESH_REASON
    global SOURCE_REFRESH_RUNNING, SOURCE_REFRESH_STARTED_AT

    if Unibase(unibase_path, migrate=False).settings()["state"] == "reset_empty":
        print(f"[MeterMesh timing] source refresh not scheduled: Unibase is empty; reason={reason}", flush=True)
        return False
    with SOURCE_REFRESH_STATE_LOCK:
        if SOURCE_REFRESH_RUNNING:
            print(f"[MeterMesh timing] source refresh not scheduled: already running; reason={reason}", flush=True)
            return False
        if respect_cooldown and SOURCE_REFRESH_COMPLETED_MONOTONIC:
            elapsed = time.monotonic() - SOURCE_REFRESH_COMPLETED_MONOTONIC
            if elapsed < SOURCE_REFRESH_SCHEDULER_COOLDOWN_SECONDS:
                print(f"[MeterMesh timing] source refresh not scheduled: cooldown; reason={reason}", flush=True)
                return False
        SOURCE_REFRESH_RUNNING = True
        SOURCE_REFRESH_STARTED_AT = dt.datetime.now(dt.timezone.utc).isoformat()
        SOURCE_REFRESH_REASON = reason
        SOURCE_REFRESH_ERROR = None

    def worker() -> None:
        global SOURCE_REFRESH_COMPLETED_AT, SOURCE_REFRESH_COMPLETED_MONOTONIC, SOURCE_REFRESH_ERROR
        global SOURCE_REFRESH_RUNNING
        try:
            print(f"[MeterMesh timing] source refresh started; reason={reason}", flush=True)
            refresh_enabled_sources(
                unibase_path,
                opencode_live_db,
                codex_root=codex_root,
                codex_state_db=codex_state_db,
                claude_root=claude_root,
                force_full_scan=force_full_scan,
            )
        except Exception as exc:  # noqa: BLE001
            with SOURCE_REFRESH_STATE_LOCK:
                SOURCE_REFRESH_ERROR = sanitize_error(exc)
            print(f"[MeterMesh timing] source refresh failed: {exc.__class__.__name__}: {exc}", flush=True)
        finally:
            with SOURCE_REFRESH_STATE_LOCK:
                SOURCE_REFRESH_RUNNING = False
                SOURCE_REFRESH_COMPLETED_MONOTONIC = time.monotonic()
                SOURCE_REFRESH_COMPLETED_AT = dt.datetime.now(dt.timezone.utc).isoformat()

    try:
        threading.Thread(target=worker, name="metermesh-source-refresh", daemon=True).start()
    except Exception as exc:
        with SOURCE_REFRESH_STATE_LOCK:
            SOURCE_REFRESH_RUNNING = False
            SOURCE_REFRESH_ERROR = sanitize_error(exc)
        raise
    return True


def start_source_refresh_scheduler(
    unibase_path: Path,
    opencode_live_db: Path | None = None,
    *,
    codex_root: Path | None = None,
    codex_state_db: Path | None = None,
    claude_root: Path | None = None,
) -> None:
    def worker() -> None:
        time.sleep(SOURCE_REFRESH_SCHEDULER_POLL_SECONDS)
        while True:
            wait_for_source_refresh_window()
            try:
                scheduled = schedule_enabled_sources_refresh(
                    unibase_path,
                    opencode_live_db,
                    codex_root=codex_root,
                    codex_state_db=codex_state_db,
                    claude_root=claude_root,
                    reason="periodic",
                    respect_cooldown=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[MeterMesh timing] periodic source refresh could not start: {exc}", flush=True)
                scheduled = False
            if scheduled:
                wait_for_source_refresh()
            else:
                time.sleep(SOURCE_REFRESH_SCHEDULER_POLL_SECONDS)

    threading.Thread(target=worker, name="metermesh-source-scheduler", daemon=True).start()


def _checkpoint_database(path: Path) -> None:
    with Unibase(path).connect() as conn:
        conn.execute("pragma wal_checkpoint(truncate)")


def resync_worker(
    main_path: Path,
    operation_id: str,
    opencode_live_db: Path | None = None,
    codex_state_db: Path | None = None,
) -> None:
    database = Unibase(main_path)
    errors = []
    try:
        if database.settings()["state"] == "reset_empty":
            raise RuntimeError("Cannot resync an empty Unibase; use Reset to rebuild it")
        enabled_sources = enabled_sources_for_import(database, opencode_live_db, codex_state_db)
        total = len(enabled_sources)
        database.update_operation(operation_id, state="running", current=0, total=total)
        for index, source in enumerate(enabled_sources, 1):
            try:
                import_registered_source(
                    database,
                    source,
                    opencode_live_db,
                    codex_state_db,
                    force_full_scan=True,
                    non_destructive=True,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{source["provider"]}: {sanitize_error(exc)}')
            database.update_operation(operation_id, state="running", current=index, total=total)
        if errors:
            raise RuntimeError("; ".join(errors))
        database.update_operation(
            operation_id,
            state="succeeded",
            current=total,
            total=total,
            generation=database.settings()["generation"],
        )
    except Exception as exc:  # noqa: BLE001
        database.update_operation(operation_id, state="failed", error=exc)


def reset_worker(
    main_path: Path,
    operation_id: str,
    opencode_live_db: Path | None = None,
    codex_state_db: Path | None = None,
) -> None:
    main = Unibase(main_path)
    staging_path = main_path.with_name(f".{main_path.name}.{operation_id}.staging")
    try:
        main.update_operation(operation_id, state="running", current=0, total=0)
        codex_live_requires_state = codex_state_db is not None
        enabled_sources = enabled_sources_for_import(main, opencode_live_db, codex_state_db)
        if staging_path.exists():
            staging_path.unlink()
        with main.connect(readonly=True) as source_conn:
            destination = sqlite3.connect(staging_path)
            try:
                source_conn.backup(destination)
            finally:
                destination.close()
        staging = Unibase(staging_path)
        staging.reset(allow_active_operation=True)
        with staging.connect() as conn:
            conn.execute("update app_settings set state = 'ready' where id = 1")
        total = len(enabled_sources)
        main.update_operation(operation_id, state="running", current=0, total=total)
        for index, source in enumerate(enabled_sources, 1):
            import_registered_source(
                staging,
                source,
                opencode_live_db,
                codex_state_db,
                require_codex_state_inventory=codex_live_requires_state,
                force_full_scan=True,
            )
            main.update_operation(operation_id, state="running", current=index, total=total)
        if not staging.integrity_check():
            raise RuntimeError("Staging Unibase integrity check failed")
        generation = staging.settings()["generation"]
        staging.update_operation(
            operation_id, state="succeeded", current=total, total=total, generation=generation
        )
        _checkpoint_database(staging_path)
        with OPERATION_LOCKS.acquire("maintenance"):
            with sqlite3.connect(staging_path) as source_conn, sqlite3.connect(main_path) as destination:
                source_conn.backup(destination)
        staging_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(staging_path) + suffix).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        try:
            main.update_operation(operation_id, state="failed", error=exc)
        finally:
            if staging_path.exists():
                staging_path.unlink()


def heatmap_days(
    daily: list[dict],
    range_name: str,
    start_day: str | None = None,
    end_day: str | None = None,
) -> list[dict]:
    by_day = {row["day"]: row for row in daily}
    selected_start = parse_iso_day(start_day)
    selected_end = parse_iso_day(end_day)
    if range_name == "all":
        if daily:
            first = dt.date.fromisoformat(daily[0]["day"])
            last = max(dt.date.today(), dt.date.fromisoformat(daily[-1]["day"]))
        else:
            first = dt.date.today()
            last = dt.date.today()
    else:
        first = selected_start or (dt.date.fromisoformat(daily[0]["day"]) if daily else dt.date.today())
        last = selected_end or first

    # Align to Monday for a stable contribution-grid shape.
    first -= dt.timedelta(days=first.weekday())
    max_tokens = max((row["total_tokens"] for row in daily), default=0)
    cells = []
    cursor = first
    while cursor <= last:
        key = cursor.isoformat()
        row = by_day.get(key, {"sessions": 0, "total_tokens": 0})
        tokens = row["total_tokens"]
        if tokens == 0 or max_tokens == 0:
            level = 0
        elif tokens < max_tokens * 0.2:
            level = 1
        elif tokens < max_tokens * 0.45:
            level = 2
        elif tokens < max_tokens * 0.7:
            level = 3
        else:
            level = 4
        cells.append({"day": key, "sessions": row["sessions"], "tokens": tokens, "level": level})
        cursor += dt.timedelta(days=1)
    return cells


def render_dashboard(data: dict) -> str:
    provider = data.get("provider", "codex")
    provider_label = data.get("provider_label", "Codex")
    totals = data["totals"]
    daily_desc = list(reversed(data["daily"]))
    heat_cells = heatmap_days(data["daily"], data["range"], data.get("range_start"), data.get("range_end"))
    heat_columns = max(1, (len(heat_cells) + 6) // 7)
    favorite_model = str(data["favorite_model"])
    favorite_model = f"{favorite_model[:20]}..." if len(favorite_model) > 20 else favorite_model

    if data["range"] == "custom" and data.get("range_start") and data.get("range_end"):
        range_summary = f'{data["range_start"]} to {data["range_end"]}'
    elif data["range"] == "1d" and data.get("range_start"):
        range_summary = data["range_start"]
    elif data["range"] == "7d":
        range_summary = "Last 7 days"
    elif data["range"] == "30d":
        range_summary = "Last 30 days"
    else:
        range_summary = "All time"

    def range_link(label: str, value: str) -> str:
        active = " active" if data["range"] == value else ""
        return f'<a class="seg{active}" href="/?provider={provider}&range={value}">{label}</a>'

    day_rows = "\n".join(
        f"""
        <tr>
          <td>{html.escape(row["day"])}</td>
          <td>All</td>
          <td>{html.escape(str(provider_label))}</td>
          <td class="num">{fmt_int(row["input_tokens"])}</td>
          <td class="num">{fmt_int(row["output_tokens"])}</td>
          <td class="num">{fmt_int(row["total_tokens"])}</td>
          <td class="num">{fmt_int(row["cached_input_tokens"])}</td>
          <td class="num">{fmt_int(row["total_with_cached_tokens"])}</td>
          <td class="num">{fmt_usd(row["cost_usd"])}</td>
          <td class="num">{fmt_int(row["sessions"])}</td>
        </tr>
        """
        for row in daily_desc
    ) or '<tr><td colspan="10" class="empty">No usage in this range.</td></tr>'

    model_rows = "\n".join(
        f"""
        <tr>
          <td>{html.escape(row["model"])}</td>
          <td class="num">{fmt_int(row["sessions"])}</td>
          <td class="num">{fmt_int(row["input_tokens"])}</td>
          <td class="num">{fmt_int(row["output_tokens"])}</td>
          <td class="num">{fmt_int(row["total_tokens"])}</td>
          <td class="num">{fmt_int(row["cached_input_tokens"])}</td>
          <td class="num">{fmt_int(row["total_with_cached_tokens"])}</td>
          <td class="num">{fmt_usd(row["cost_usd"])}</td>
          <td class="num">{(row["total_tokens"] / max(totals["total_tokens"], 1) * 100):.1f}%</td>
        </tr>
        """
        for row in data["models"]
    ) or '<tr><td colspan="9" class="empty">No models in this range.</td></tr>'

    heatmap = "\n".join(
        f"""
        <div class="heat-cell level-{cell["level"]}" title="{html.escape(cell["day"])}: {fmt_int(cell["tokens"])} tokens, {fmt_int(cell["sessions"])} sessions"></div>
        """
        for cell in heat_cells
    )

    peak_value = data["peak_day"]
    if data["peak_day_tokens"]:
        peak_value = f'{peak_value}<span class="metric-note">{fmt_short(data["peak_day_tokens"])}</span>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MeterMesh · {html.escape(str(provider_label))}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #151515;
      --panel: #232323;
      --panel-2: #303030;
      --line: #444;
      --text: #f2f2f2;
      --muted: #a8a8a8;
      --accent: #84aef2;
      --accent-2: #d7df3f;
      --green-0: #303030;
      --green-1: #294761;
      --green-2: #2f67a2;
      --green-3: #3e87d6;
      --green-4: #7fb0f2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      font-size: 16px;
      line-height: 1.45;
    }}
    main {{
      width: min(1534px, calc(100vw - 32px));
      margin: 28px auto 56px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: 30px;
      font-weight: 760;
      letter-spacing: 0;
    }}
    .subtle {{ color: var(--muted); }}
    .segments {{
      display: flex;
      gap: 8px;
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 6px;
      border-radius: 8px;
      white-space: nowrap;
    }}
    .seg {{
      color: var(--muted);
      text-decoration: none;
      padding: 7px 13px;
      border-radius: 6px;
      min-height: 38px;
      display: inline-flex;
      align-items: center;
    }}
    .seg.active {{
      background: var(--panel-2);
      color: var(--text);
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      margin: 18px 0;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
      min-height: 92px;
    }}
    .label {{
      color: var(--muted);
      font-size: 15px;
      margin-bottom: 6px;
    }}
    .value {{
      font-size: 27px;
      font-weight: 760;
      overflow-wrap: anywhere;
    }}
    .metric-note {{
      display: block;
      color: var(--muted);
      font-size: 14px;
      font-weight: 500;
      margin-top: 4px;
    }}
    section {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 18px;
      letter-spacing: 0;
    }}
    .heat-wrap {{
      overflow-x: auto;
      padding-bottom: 4px;
    }}
    .heatmap {{
      display: grid;
      grid-auto-flow: column;
      grid-template-rows: repeat(7, 16px);
      grid-template-columns: repeat({heat_columns}, 16px);
      gap: 5px;
      width: max-content;
    }}
    .heat-cell {{
      width: 16px;
      height: 16px;
      border-radius: 4px;
      background: var(--green-0);
      border: 1px solid rgba(255,255,255,.04);
    }}
    .level-1 {{ background: var(--green-1); }}
    .level-2 {{ background: var(--green-2); }}
    .level-3 {{ background: var(--green-3); }}
    .level-4 {{ background: var(--green-4); }}
    .tables {{
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(360px, .9fr);
      gap: 18px;
      align-items: start;
    }}
    .table-scroll {{ overflow-x: auto; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 936px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 14px;
      font-weight: 650;
    }}
    td.num, th.num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    td.empty {{
      color: var(--muted);
      text-align: center;
      padding: 26px 12px;
    }}
    tfoot td {{
      color: var(--accent-2);
      font-weight: 760;
      border-bottom: 0;
    }}
    details {{
      margin-top: 18px;
      color: var(--muted);
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #101010;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      max-height: 420px;
      overflow: auto;
    }}
    @media (max-width: 880px) {{
      header {{ flex-direction: column; }}
      .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .tables {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 520px) {{
      main {{ width: min(100vw - 20px, 1534px); margin-top: 18px; }}
      .cards {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 25px; }}
      .value {{ font-size: 23px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>MeterMesh · {html.escape(str(provider_label))}</h1>
        <div class="subtle">Generated {html.escape(data["generated_at"])} from {html.escape(str(data.get("data_source", "local logs")))} · <a href="/data.json?provider={provider}&range={html.escape(data["range"])}">aggregate JSON</a></div>
        <div class="subtle">Showing {html.escape(range_summary)}</div>
      </div>
      <nav class="segments" aria-label="Range">
        {range_link("All", "all")}
        {range_link("30d", "30d")}
        {range_link("7d", "7d")}
        {range_link("1d", "1d")}
        {range_link("Custom", "custom")}
      </nav>
    </header>

    <div class="cards">
      <div class="card"><div class="label">Sessions</div><div class="value">{fmt_int(totals["sessions"])}</div></div>
      <div class="card"><div class="label">Input tokens</div><div class="value">{fmt_short(totals["input_tokens"])}</div></div>
      <div class="card"><div class="label">Output tokens</div><div class="value">{fmt_short(totals["output_tokens"])}</div></div>
      <div class="card"><div class="label">Total w/o cached</div><div class="value">{fmt_short(totals["total_tokens"])}</div></div>
      <div class="card"><div class="label">Cached input</div><div class="value">{fmt_short(totals["cached_input_tokens"])}</div></div>
      <div class="card"><div class="label">Total tokens</div><div class="value">{fmt_short(totals["total_with_cached_tokens"])}</div></div>
      <div class="card"><div class="label">Active days</div><div class="value">{fmt_int(totals["active_days"])}</div></div>
      <div class="card"><div class="label">API estimate</div><div class="value">{fmt_usd(totals["cost_usd"])}<span class="metric-note">{html.escape(data["pricing"]["source"])}</span></div></div>
      <div class="card"><div class="label">Favorite model</div><div class="value">{html.escape(favorite_model)}</div></div>
      <div class="card"><div class="label">Current streak</div><div class="value">{fmt_int(data["current_streak"])}d</div></div>
      <div class="card"><div class="label">Longest streak</div><div class="value">{fmt_int(data["longest_streak"])}d</div></div>
      <div class="card"><div class="label">Peak day</div><div class="value">{peak_value}</div></div>
      <div class="card"><div class="label">Data source</div><div class="value">SQLite</div></div>
    </div>

    <section>
      <h2>Daily Heatmap</h2>
      <div class="heat-wrap"><div class="heatmap">{heatmap}</div></div>
    </section>

    <div class="tables">
      <section>
        <h2>Daily Usage</h2>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Date</th><th>Scope</th><th>App</th><th class="num">Input</th><th class="num">Output</th><th class="num">Total w/o cached</th><th class="num">Cached</th><th class="num">Total</th><th class="num">Cost</th><th class="num">Sessions</th></tr></thead>
            <tbody>{day_rows}</tbody>
            <tfoot><tr><td>Total</td><td></td><td></td><td class="num">{fmt_int(totals["input_tokens"])}</td><td class="num">{fmt_int(totals["output_tokens"])}</td><td class="num">{fmt_int(totals["total_tokens"])}</td><td class="num">{fmt_int(totals["cached_input_tokens"])}</td><td class="num">{fmt_int(totals["total_with_cached_tokens"])}</td><td class="num">{fmt_usd(totals["cost_usd"])}</td><td class="num">{fmt_int(totals["sessions"])}</td></tr></tfoot>
          </table>
        </div>
      </section>

      <section>
        <h2>Models</h2>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Model</th><th class="num">Sessions</th><th class="num">Input</th><th class="num">Output</th><th class="num">Total w/o cached</th><th class="num">Cached</th><th class="num">Total</th><th class="num">Cost</th><th class="num">Share</th></tr></thead>
            <tbody>{model_rows}</tbody>
          </table>
        </div>
      </section>
    </div>

  </main>
</body>
</html>"""


def render_error_page(message: str, db_path: Path) -> str:
    safe_message = html.escape("Unibase or provider source is temporarily unavailable.")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MeterMesh</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{
      margin: 0;
      background: #151515;
      color: #f2f2f2;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      line-height: 1.45;
    }}
    main {{
      width: min(760px, calc(100vw - 32px));
      margin: 48px auto;
      background: #232323;
      border: 1px solid #444;
      border-radius: 8px;
      padding: 20px;
    }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    code {{
      display: block;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #101010;
      border: 1px solid #444;
      border-radius: 8px;
      padding: 12px;
      margin-top: 12px;
    }}
    .muted {{ color: #a8a8a8; }}
  </style>
</head>
<body>
  <main>
    <h1>MeterMesh</h1>
    <p>Could not read committed Unibase data.</p>
    <code>{safe_message}</code>
    <p class="muted">Provider source files were not modified.</p>
  </main>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB
    claude_projects_path: Path = DEFAULT_CLAUDE_PROJECTS
    claude_db_path: Path = DEFAULT_CLAUDE_DB
    opencode_db_path: Path = resolve_opencode_db()
    unibase_path: Path | None = None

    def do_GET(self) -> None:
        started_at = time.perf_counter()
        parsed = urlparse(self.path)
        print(f"[MeterMesh timing] request started: GET {parsed.path}", flush=True)
        try:
            if parsed.path in {"/", "/index.html"}:
                self.serve_dashboard(parsed.query)
                return
            if parsed.path in {"/data.json", "/api/usage"}:
                self.serve_json(parsed.query)
                return
            if parsed.path == "/api/requests":
                self.serve_requests(parsed.query)
                return
            if parsed.path == "/api/settings":
                self.serve_settings()
                return
            if parsed.path == "/api/unibase/status":
                self.serve_unibase_status(parsed.query)
                return
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self.send_error(404)
        finally:
            print(
                f"[MeterMesh timing] request finished: GET {parsed.path}; "
                f"{(time.perf_counter() - started_at) * 1000:.0f} ms",
                flush=True,
            )

    def do_POST(self) -> None:
        started_at = time.perf_counter()
        parsed = urlparse(self.path)
        print(f"[MeterMesh timing] request started: POST {parsed.path}", flush=True)
        try:
            if parsed.path == "/api/settings":
                self.apply_settings_request()
                return
            if parsed.path == "/api/sources/refresh":
                self.refresh_sources_request()
                return
            if parsed.path == "/api/unibase/reset":
                self.reset_unibase_request()
                return
            if parsed.path == "/api/unibase/resync":
                self.resync_unibase_request()
                return
            self.send_error(404)
        finally:
            print(
                f"[MeterMesh timing] request finished: POST {parsed.path}; "
                f"{(time.perf_counter() - started_at) * 1000:.0f} ms",
                flush=True,
            )

    def filters_from_query(self, query_string: str) -> dict[str, str | int | bool | None]:
        query = parse_qs(query_string)
        default_provider = "all" if self.unibase_path else "codex"
        provider = normalize_provider(query.get("provider", [default_provider])[0])
        timezone_name = query.get("timezone", ["UTC"])[0]
        today = dt.datetime.now(resolve_timezone(timezone_name)).date()
        ignore_auto_review = False if self.unibase_path else parse_bool_flag(
            query.get("ignore_auto_review", [None])[0], default=False
        )
        filters = resolve_range(
            query.get("range", ["all"])[0],
            query.get("start", [None])[0],
            query.get("end", [None])[0],
            ignore_auto_review if provider in {"all", "codex"} else False,
            today=today,
        )
        filters["provider"] = provider
        filters["timezone"] = timezone_name
        chart_filters = resolve_chart_range(
            query.get("chart_range", ["30d"])[0],
            query.get("chart_start", [None])[0],
            query.get("chart_end", [None])[0],
            bool(filters["ignore_auto_review"]),
            today=today,
        )
        filters["chart_range"] = chart_filters["range"]
        filters["chart_start_day"] = chart_filters["start_day"]
        filters["chart_end_day"] = chart_filters["end_day"]
        requested_diagnostics = parse_bool_flag(query.get("include_diagnostics", [None])[0], default=False)
        filters["include_diagnostics"] = requested_diagnostics if self.unibase_path else provider == "codex" and requested_diagnostics
        filters["workdays_only"] = parse_bool_flag(query.get("workdays", [None])[0], default=False)
        return filters

    def usage_payload(self, filters: dict) -> dict:
        if self.unibase_path:
            settings = Unibase(self.unibase_path, migrate=False).settings()
            generation = int(settings["generation"])
            settings_revision = int(settings["revision"])
            cache_key = (
                str(self.unibase_path), generation, settings_revision, filters["range"], filters["start_day"], filters["end_day"],
                bool(filters["ignore_auto_review"]), filters["chart_range"], filters["chart_start_day"],
                filters["chart_end_day"], bool(filters["include_diagnostics"]), filters["provider"], filters["timezone"],
                bool(filters["workdays_only"]),
            )
            now = time.monotonic()
            with USAGE_RESPONSE_CACHE_LOCK:
                cached = USAGE_RESPONSE_CACHE.get(cache_key)
                if cached and now - cached[0] <= USAGE_RESPONSE_CACHE_SECONDS:
                    USAGE_RESPONSE_CACHE.move_to_end(cache_key)
                    payload = dict(cached[1])
                else:
                    if cached:
                        del USAGE_RESPONSE_CACHE[cache_key]
                    payload = None
            if payload is None:
                payload = load_unibase_usage(
                    self.unibase_path,
                    str(filters["range"]),
                    filters["start_day"],
                    filters["end_day"],
                    bool(filters["ignore_auto_review"]),
                    str(filters["chart_range"]),
                    filters["chart_start_day"],
                    filters["chart_end_day"],
                    bool(filters["include_diagnostics"]),
                    provider=str(filters["provider"]),
                    timezone_name=str(filters["timezone"]),
                    workdays_only=bool(filters["workdays_only"]),
                )
                with USAGE_RESPONSE_CACHE_LOCK:
                    USAGE_RESPONSE_CACHE[cache_key] = (now, dict(payload))
                    USAGE_RESPONSE_CACHE.move_to_end(cache_key)
                    while len(USAGE_RESPONSE_CACHE) > USAGE_RESPONSE_CACHE_MAX_ENTRIES:
                        USAGE_RESPONSE_CACHE.popitem(last=False)
            payload["sync"] = source_refresh_status()
            payload["fresh_at"] = unibase_freshness(Unibase(self.unibase_path, migrate=False), str(filters["provider"]))
            return payload
        return load_usage(
            self.db_path,
            str(filters["range"]),
            filters["start_day"],
            filters["end_day"],
            bool(filters["ignore_auto_review"]),
            str(filters["chart_range"]),
            filters["chart_start_day"],
            filters["chart_end_day"],
            bool(filters["include_diagnostics"]),
            provider=str(filters["provider"]),
            claude_projects_path=self.claude_projects_path,
            claude_db_path=self.claude_db_path,
            today=dt.datetime.now(resolve_timezone(str(filters["timezone"]))).date(),
        )

    def send_body(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        self.send_body(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def read_json_body(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("JSON content type required")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid content length") from exc
        if content_length <= 0 or content_length > 64 * 1024:
            raise ValueError("Invalid body size")
        payload = json.loads(self.rfile.read(content_length))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def serve_dashboard(self, query_string: str) -> None:
        filters = self.filters_from_query(query_string)
        try:
            data = self.usage_payload(filters)
            body = render_dashboard(data).encode("utf-8")
            self.send_body(200, "text/html; charset=utf-8", body)
        except Exception as exc:  # noqa: BLE001
            source_path = self.claude_projects_path if filters["provider"] == "claude" else self.db_path
            body = render_error_page(str(exc), source_path).encode("utf-8")
            self.send_body(503, "text/html; charset=utf-8", body)

    def serve_json(self, query_string: str) -> None:
        filters = self.filters_from_query(query_string)
        try:
            payload = self.usage_payload(filters)
            status = 200
        except Exception as exc:  # noqa: BLE001
            provider_label = PROVIDER_LABELS[str(filters["provider"])]
            payload = {
                "error": f"Could not read {provider_label} usage data.",
                "provider": filters["provider"],
                "provider_label": provider_label,
                "range": filters["range"],
                "range_start": filters["start_day"],
                "range_end": filters["end_day"],
                "ignore_auto_review": bool(filters["ignore_auto_review"]),
                "chart_range": filters["chart_range"],
                "chart_start": filters["chart_start_day"],
                "chart_end": filters["chart_end_day"],
                "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            status = 503
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_body(status, "application/json; charset=utf-8", body)

    def serve_requests(self, query_string: str) -> None:
        if not self.unibase_path:
            self.send_body(503, "application/json; charset=utf-8", b'{"error":"Unibase is not configured"}')
            return
        query = parse_qs(query_string)
        try:
            database = Unibase(self.unibase_path, migrate=False)
            payload = load_requests(
                self.unibase_path,
                provider=query.get("provider", ["all"])[0],
                range_name=query.get("range", ["all"])[0],
                start_day=query.get("start", [None])[0],
                end_day=query.get("end", [None])[0],
                timezone_name=query.get("timezone", ["UTC"])[0],
                group=query.get("group", ["none"])[0],
                page=int(query.get("page", ["1"])[0]),
                page_size=int(query.get("page_size", ["25"])[0]),
                snapshot=query.get("snapshot", [None])[0],
                bucket_start=query.get("bucket_start", [None])[0],
                child_page=int(query.get("child_page", ["1"])[0]),
            )
            payload["sync"] = source_refresh_status()
            status = 200
        except SnapshotConflict as exc:
            payload = {"error": str(exc)}
            status = 409
        except (ValueError, TypeError):
            payload = {"error": "Invalid Requests parameters."}
            status = 400
        except Exception:  # noqa: BLE001
            payload = {"error": "Could not load Requests from Unibase."}
            status = 503
        self.send_body(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def serve_settings(self) -> None:
        if not self.unibase_path:
            self.send_json(503, {"error": "Unibase is not configured."})
            return
        try:
            refresh_running = source_refresh_running()
            database = Unibase(self.unibase_path, migrate=not refresh_running)
            if not refresh_running:
                discovery_started_at = time.perf_counter()
                register_default_sources(
                    database,
                    codex_root=self.db_path.parent,
                    claude_root=self.claude_projects_path.parent
                    if self.claude_projects_path.name == "projects" else self.claude_projects_path,
                    opencode_root=self.opencode_db_path.parent,
                )
                print(
                    f"[MeterMesh timing] settings source discovery: "
                    f"{(time.perf_counter() - discovery_started_at) * 1000:.0f} ms",
                    flush=True,
                )
            else:
                print("[MeterMesh timing] settings source discovery skipped: source refresh is running", flush=True)
            self.send_json(200, settings_payload(self.unibase_path))
        except Exception as exc:  # noqa: BLE001
            print(f"[MeterMesh timing] settings failed: {exc.__class__.__name__}: {exc}", flush=True)
            self.send_json(503, {"error": "Could not load MeterMesh settings."})

    def apply_settings_request(self) -> None:
        if not self.unibase_path:
            self.send_json(503, {"error": "Unibase is not configured."})
            return
        try:
            payload = self.read_json_body()
            if set(payload) != {
                "revision",
                "merge_models_across_providers",
                "non_working_weekdays",
                "sources",
                "models",
            }:
                raise ValueError("Unexpected settings fields")
            if (
                not isinstance(payload["revision"], int)
                or not isinstance(payload["merge_models_across_providers"], bool)
                or not isinstance(payload["non_working_weekdays"], list)
                or not isinstance(payload["sources"], list)
                or not isinstance(payload["models"], list)
                or not all(
                    isinstance(item, dict) and set(item) == {"source_id", "enabled"}
                    for item in payload["sources"]
                )
                or not all(
                    isinstance(item, dict) and set(item) == {"model", "enabled"}
                    for item in payload["models"]
                )
            ):
                raise ValueError("Invalid settings fields")
            database = Unibase(self.unibase_path)
            applied = database.apply_settings(
                payload["revision"],
                payload["merge_models_across_providers"],
                payload["sources"],
                payload["models"],
                payload["non_working_weekdays"],
            )
            maintenance_required = bool(applied.pop("_maintenance_required"))
            with USAGE_RESPONSE_CACHE_LOCK:
                USAGE_RESPONSE_CACHE.clear()
            if maintenance_required:
                schedule_enabled_sources_refresh(
                    self.unibase_path,
                    self.opencode_db_path,
                    codex_root=self.db_path.parent,
                    codex_state_db=self.db_path,
                    claude_root=self.claude_projects_path.parent
                    if self.claude_projects_path.name == "projects" else self.claude_projects_path,
                    reason="settings update",
                )
            self.send_json(200, settings_payload(self.unibase_path))
        except (RevisionConflict, OperationConflict) as exc:
            self.send_json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid settings payload."})
        except Exception:
            self.send_json(503, {"error": "Could not apply MeterMesh settings."})

    def refresh_sources_request(self) -> None:
        if not self.unibase_path:
            self.send_json(503, {"error": "Unibase is not configured."})
            return
        try:
            payload = self.read_json_body()
            if payload:
                raise ValueError("Refresh body must be empty")
            database = Unibase(self.unibase_path)
            if database.settings()["state"] == "reset_empty":
                raise OperationConflict("Unibase is empty; use Reset to rebuild it")
            if database.active_operation():
                raise OperationConflict("A Unibase operation is already running")
            scheduled = schedule_enabled_sources_refresh(
                self.unibase_path,
                self.opencode_db_path,
                codex_root=self.db_path.parent,
                codex_state_db=self.db_path,
                claude_root=self.claude_projects_path.parent
                if self.claude_projects_path.name == "projects" else self.claude_projects_path,
                reason="manual incremental refresh",
            )
            if not scheduled:
                self.send_json(409, {
                    "error": "A source refresh is already running",
                    "source_sync": source_refresh_status(),
                })
                return
            self.send_json(202, source_refresh_status())
        except OperationConflict as exc:
            self.send_json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid refresh payload."})
        except Exception:
            self.send_json(503, {"error": "Could not refresh sources."})

    def reset_unibase_request(self) -> None:
        if not self.unibase_path:
            self.send_json(503, {"error": "Unibase is not configured."})
            return
        try:
            payload = self.read_json_body()
            if payload != {"confirmation": "RESET UNIBASE"}:
                raise ValueError("Invalid reset confirmation")
            operation_id = Unibase(self.unibase_path).create_operation("reset")
            threading.Thread(
                target=reset_worker,
                args=(self.unibase_path, operation_id, self.opencode_db_path, self.db_path),
                name=f"metermesh-reset-{operation_id}",
                daemon=True,
            ).start()
            self.send_json(202, {"operation_id": operation_id})
        except OperationConflict as exc:
            self.send_json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Type RESET UNIBASE to confirm."})
        except Exception:
            self.send_json(503, {"error": "Could not reset Unibase."})

    def resync_unibase_request(self) -> None:
        if not self.unibase_path:
            self.send_json(503, {"error": "Unibase is not configured."})
            return
        try:
            payload = self.read_json_body()
            if payload:
                raise ValueError("Resync body must be empty")
            database = Unibase(self.unibase_path)
            if database.settings()["state"] == "reset_empty":
                raise OperationConflict("Unibase is empty; use Reset to rebuild it")
            operation_id = database.create_operation("resync")
            threading.Thread(
                target=resync_worker,
                args=(self.unibase_path, operation_id, self.opencode_db_path, self.db_path),
                name=f"metermesh-resync-{operation_id}",
                daemon=True,
            ).start()
            self.send_json(202, {"operation_id": operation_id})
        except OperationConflict as exc:
            self.send_json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid resync payload."})
        except Exception:
            self.send_json(503, {"error": "Could not start Resync."})

    def serve_unibase_status(self, query_string: str) -> None:
        if not self.unibase_path:
            self.send_json(503, {"error": "Unibase is not configured."})
            return
        query = parse_qs(query_string)
        try:
            database = Unibase(self.unibase_path, migrate=False)
            status = database.operation_status(query.get("operation_id", [None])[0])
            status["source_sync"] = source_refresh_status()
            status["fresh_at"] = unibase_freshness(database, normalize_provider(query.get("provider", ["all"])[0]))
            self.send_json(200, status)
        except Exception:
            self.send_json(503, {"error": "Could not read Unibase operation status."})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def run_check(db_path: Path) -> None:
    sample_cost, missing_model = token_cost_usd(
        {
            "model": "gpt-5.5",
            "input_tokens": 2_429_884,
            "cached_input_tokens": 40_193_280,
            "output_tokens": 195_249,
        },
        {"models": FALLBACK_PRICING, "fallback": FALLBACK_PRICING},
    )
    if missing_model or round(sample_cost, 5) != 38.10353:
        raise RuntimeError("Cost calculation check failed")

    checks = [
        ("all", None, None, False, "30d", None, None, "day"),
        ("30d", None, None, False, "90d", None, None, "day"),
        ("30d", None, None, False, "6m", None, None, "week"),
        ("30d", None, None, False, "1y", None, None, "month"),
        ("7d", None, None, False, "all", None, None, None),
        ("1d", None, None, False, "custom", "2026-01-01", "2026-01-03", "day"),
        ("1d", None, None, False, "custom", "2026-01-01", "2026-03-01", "day"),
        ("1d", None, None, False, "custom", "2026-01-01", "2026-03-31", "day"),
        ("1d", None, None, False, "custom", "2026-01-01", "2026-04-01", "week"),
        ("1d", None, None, False, "custom", "2026-01-01", "2026-05-01", "week"),
        ("1d", None, None, False, "custom", "2026-01-01", "2026-12-31", "month"),
        ("custom", "2026-01-01", "2026-01-03", False, "30d", None, None, "day"),
        ("bad-range", None, None, False, "bad-range", None, None, "day"),
        ("all", None, None, True, "30d", None, None, "day"),
    ]
    for range_name, start_day, end_day, ignore_auto_review, chart_range, chart_start, chart_end, expected_granularity in checks:
        data = load_usage(db_path, range_name, start_day, end_day, ignore_auto_review, chart_range, chart_start, chart_end)
        html_body = render_dashboard(data)
        json.dumps(data, ensure_ascii=False)
        chart = data["chart"]
        if chart["range"] not in {"all", "1y", "6m", "90d", "30d", "custom"}:
            raise RuntimeError(f"Chart range failed for range={range_name}")
        if chart["granularity"] not in {"day", "week", "month"}:
            raise RuntimeError(f"Chart granularity failed for range={range_name}")
        if expected_granularity and chart["granularity"] != expected_granularity:
            raise RuntimeError(
                f"Expected {expected_granularity} chart granularity for chart_range={chart_range}, got {chart['granularity']}"
            )
        for day_row in chart["days"]:
            model_total = sum(item["total_tokens"] for item in day_row["models"])
            if day_row["total_tokens"] != model_total:
                raise RuntimeError(f"Chart day model sum failed for range={range_name}")
        chart_model_totals = {}
        for day_row in chart["days"]:
            for item in day_row["models"]:
                chart_model_totals[item["model"]] = chart_model_totals.get(item["model"], 0) + item["total_tokens"]
        for model_row in chart["models"]:
            if model_row["total_tokens"] != chart_model_totals.get(model_row["model"], 0):
                raise RuntimeError(f"Chart model sum failed for range={range_name}")
        totals = data["totals"]
        if totals["total_tokens"] != totals["input_tokens"] + totals["output_tokens"]:
            raise RuntimeError(f"Total without cached invariant failed for range={range_name}")
        if totals["total_with_cached_tokens"] != totals["total_tokens"] + totals["cached_input_tokens"]:
            raise RuntimeError(f"Total with cached invariant failed for range={range_name}")
        for row in data["daily"]:
            if row["total_tokens"] != row["input_tokens"] + row["output_tokens"]:
                raise RuntimeError(f"Daily total without cached invariant failed for range={range_name}")
            if row["total_with_cached_tokens"] != row["total_tokens"] + row["cached_input_tokens"]:
                raise RuntimeError(f"Daily total with cached invariant failed for range={range_name}")
        for row in data["models"]:
            if row["total_tokens"] != row["input_tokens"] + row["output_tokens"]:
                raise RuntimeError(f"Model total without cached invariant failed for range={range_name}")
            if row["total_with_cached_tokens"] != row["total_tokens"] + row["cached_input_tokens"]:
                raise RuntimeError(f"Model total with cached invariant failed for range={range_name}")
        if "<!doctype html>" not in html_body:
            raise RuntimeError(f"Dashboard render failed for range={range_name}")
    print(f"Smoke check passed for {db_path}")


def bootstrap_unibase(
    unibase_path: Path,
    codex_db_path: Path,
    claude_projects_path: Path,
    opencode_db_path: Path,
) -> Unibase:
    started_at = time.perf_counter()
    database = Unibase(unibase_path)
    database.recover_interrupted_operations()
    claude_root = claude_projects_path.parent if claude_projects_path.name == "projects" else claude_projects_path
    register_default_sources(
        database,
        codex_root=codex_db_path.parent,
        claude_root=claude_root,
        opencode_root=opencode_db_path.parent,
    )
    if database.settings()["state"] == "reset_empty":
        print(
            f"[MeterMesh timing] Unibase bootstrap: {(time.perf_counter() - started_at) * 1000:.0f} ms; "
            "state=reset_empty",
            flush=True,
        )
        return database
    print(
        f"[MeterMesh timing] Unibase bootstrap: {(time.perf_counter() - started_at) * 1000:.0f} ms; "
        "imports=deferred",
        flush=True,
    )
    return database


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the local MeterMesh usage dashboard.")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("CODEX_USAGE_DB", DEFAULT_DB)))
    parser.add_argument(
        "--claude-projects",
        type=Path,
        default=Path(os.environ.get("CLAUDE_PROJECTS_DIR", DEFAULT_CLAUDE_PROJECTS)),
    )
    parser.add_argument(
        "--claude-db",
        type=Path,
        default=Path(os.environ.get("CLAUDE_USAGE_DB", DEFAULT_CLAUDE_DB)),
    )
    parser.add_argument("--opencode-db", type=Path, default=resolve_opencode_db())
    parser.add_argument("--unibase-db", type=Path, default=resolve_unibase_path())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--check", action="store_true", help="Render all ranges once and exit.")
    args = parser.parse_args()
    try:
        loopback_host = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback_host = False
    if not loopback_host and not parse_bool_flag(os.environ.get("METERMESH_ALLOW_REMOTE")):
        parser.error("MeterMesh only binds to loopback by default; set METERMESH_ALLOW_REMOTE=1 to allow remote access")

    DashboardHandler.db_path = args.db.expanduser()
    DashboardHandler.claude_projects_path = args.claude_projects.expanduser()
    DashboardHandler.claude_db_path = args.claude_db.expanduser()
    DashboardHandler.opencode_db_path = args.opencode_db.expanduser()
    DashboardHandler.unibase_path = args.unibase_db.expanduser()
    if "--claude-db" in os.sys.argv or os.environ.get("CLAUDE_USAGE_DB"):
        warnings.warn("--claude-db is retained for compatibility but MeterMesh stores Claude usage in Unibase", stacklevel=1)
    if args.check:
        run_check(DashboardHandler.db_path)
        return

    bootstrap_unibase(
        DashboardHandler.unibase_path,
        DashboardHandler.db_path,
        DashboardHandler.claude_projects_path,
        DashboardHandler.opencode_db_path,
    )
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"MeterMesh: http://{args.host}:{args.port}")
    print(f"Unibase: {DashboardHandler.unibase_path}")
    print(f"Codex source: {DashboardHandler.db_path}")
    print(f"Claude source: {DashboardHandler.claude_projects_path}")
    print(f"OpenCode source: {DashboardHandler.opencode_db_path}")
    schedule_enabled_sources_refresh(
        DashboardHandler.unibase_path,
        DashboardHandler.opencode_db_path,
        codex_root=DashboardHandler.db_path.parent,
        codex_state_db=DashboardHandler.db_path,
        claude_root=(
            DashboardHandler.claude_projects_path.parent
            if DashboardHandler.claude_projects_path.name == "projects"
            else DashboardHandler.claude_projects_path
        ),
        reason="startup",
    )
    start_source_refresh_scheduler(
        DashboardHandler.unibase_path,
        DashboardHandler.opencode_db_path,
        codex_root=DashboardHandler.db_path.parent,
        codex_state_db=DashboardHandler.db_path,
        claude_root=(
            DashboardHandler.claude_projects_path.parent
            if DashboardHandler.claude_projects_path.name == "projects"
            else DashboardHandler.claude_projects_path
        ),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMeterMesh stopped")


if __name__ == "__main__":
    main()
