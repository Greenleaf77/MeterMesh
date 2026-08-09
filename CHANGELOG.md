# Changelog

## 2.2.3 - 2026-08-09

- Added Codex `archived_sessions` ingestion so archived rollout files remain indexed without leaving the live source in an error state.

## 2.2.2 - 2026-07-17

- Added a multi-stage Docker runtime that serves the built dashboard and API from one non-root container bound to `127.0.0.1`.
- Mounted provider sources read-only at their host-absolute paths while keeping Unibase in an isolated writable volume with Linux UID/GID support.
- Made source discovery tolerate absent `add_stat/` directories on read-only mounts without hiding unexpected filesystem errors.
- Added Docker smoke coverage for startup, SPA/API routing, absolute Codex rollout paths, provider imports, volume permissions, and host-write isolation.
- Documented Docker setup, additional Codex profile mounts, platform limits, updates, and troubleshooting alongside the MeterMesh 2.2.1 features.

## 2.2.1 - 2026-07-17

- Added configurable non-working weekdays and a URL-backed Workdays-only Active time view with excluded activity retained as dimmed chart segments.
- Enabled matching-model merging by default in the All scope while preserving the user's post-migration setting.
- Assigned chart colors dynamically from the current legend order using a scalable accessible palette instead of persisted model slots.
- Updated chart granularity to daily through 90 days, weekly through 190 days, and monthly for longer ranges, with calendar-aligned weeks and months.
- Improved Active time keyboard details and handled 25-hour daylight-saving fall-back days without clipping or incorrect axis labels.

## 2.2.0 - 2026-07-17

- Replaced the misleading raw telemetry Diagnostics view with a compact Data Health workspace for Unibase integrity, provider coverage, and source freshness.
- Reordered Usage Details to Models, Requests, and Data Health.
- Added readable Requests timestamps and exact grouped time windows with an explicit calculation timezone footer.
- Added short Active time presets and a fixed 24-hour daily scale.
- Added optional cross-provider model merging in the All scope and retained known model inventory across index resets.
- Improved Codex deduplication when overlapping sources disagree on an unknown model and kept freshness anchored to live sources.
- Updated the release screenshot at a 2560x1440 viewport using synthetic privacy-safe data.

## 2.1.0 - 2026-07-16

- Added automatic Codex discovery for valid rollout paths registered in `state_5.sqlite`, including sessions outside the default `~/.codex` root.
- Kept additional Codex paths privacy-safe by storing only opaque checkpoint identifiers in Unibase.
- Restored an independent visualization range for Daily Heatmap and Tokens over time without changing the Usage tables.
- Updated Tokens over time buckets to daily through 90 days, weekly through six calendar months, and monthly for longer ranges.
- Fixed Tokens over time to use a true linear height scale without inflating small bars or model segments.
- Fit every chart bucket into the visible panel so recent peaks are not hidden behind horizontal scrolling.
- Collapsed identical OpenCode model rows across native providers and endpoints while preserving provider-level separation.
- Kept Full reindex progress inside the Unibase settings section at desktop and mobile widths.
- Gave Daily Heatmap an All-time default while Tokens over time retains a separate 30-day default.
- Made the first Usage snapshot wait for the active startup import so the All scope is complete immediately.

## 2.0.0 - 2026-07-16

- Renamed the product to MeterMesh and introduced the app-owned Unibase SQLite index.
- Added provider-neutral source/provenance tables, deterministic conflict selection, retained occurrences, and active SQL projections.
- Added live and backup adapters for Codex, Claude, and OpenCode with read-only provider access and privacy allowlists.
- Added the default All scope, provider-qualified models and sessions, and recorded/estimated/unavailable cost breakdowns.
- Added privacy-safe Requests with numbered pagination, stable snapshots, timezone grouping, and complete grouped children.
- Added Settings for backup sources, Codex auto-review filtering, Reset Unibase, and atomic staging Full reindex.
- Moved production Usage and Diagnostics reads to committed Unibase SQL instead of synchronous provider scans.

## 1.3.0 - 2026-07-15

- Added one shared `With cache` / `Without cache` accounting control for Daily heatmap and Tokens over time.
- Made cache-inclusive visualization and included `codex-auto-review` data the defaults while preserving explicit URL and cookie preferences.
- Extended chart buckets and per-model stacks with additive cache-inclusive totals without changing existing token or cost semantics.
- Renamed the README screenshot asset to `screenshot-v1.3.0.png` to prevent stale GitHub/CDN image caching.

## 1.2.0 - 2026-07-15

- Redesigned the dark interface as a warm graphite telemetry console while preserving all existing dashboard behavior.
- Added responsive hero metrics, semantic accent colors, and compact daily sparklines derived from the existing usage payload.
- Added billion-scale compact values in metric cards and chart axes while keeping full token counts in detailed tables.
- Centered metric icons within their accent backplates across hero and secondary cards.
- Kept `All time` as the default dashboard range and updated the README screenshot to include the heatmap and table rows.

## 1.1.0 - 2026-07-14

- Added telemetry compatibility for Codex models in the `gpt-5.6-*` family.
- Fixed inflated token and cost estimates caused by replayed `token_count` snapshots whose cumulative usage had not changed.
- Preferred exact `raw_response_completed` token usage when available, with deduplicated cumulative-component deltas as the fallback.
- Added an optional, lazily loaded `Diagnostics` workspace for replay rates and estimated local overcount while keeping the main Usage tables unchanged.
- Added honest loading indicators with elapsed time for longer rollout scans and cached Diagnostics results for the active filter.
- Clarified that local rollout telemetry is not an OpenAI server billing ledger and cannot prove that a request was accepted or rejected.
- Added focused telemetry-classification tests for baselines, cumulative deltas, replays, counter resets, exact response deduplication, and range filtering.

## 1.0.4 - 2026-07-13

- Redesigned the dashboard with a compact graphite visual system.
- Added distinct multicolor inline SVG icons for every summary metric.
- Improved responsive controls, keyboard focus, and reduced-motion support without changing dashboard behavior.

## 1.0.3 - 2026-06-28

- Added visualization tabs for `Daily heatmap` and `Tokens over time`.
- Added a stacked tokens-over-time bar chart split by model.
- Added independent chart date filters: all time, 1 year, 6 months, 90 days, 30 days, and custom dates.
- Added adaptive chart buckets: daily through 60 days, weekly through 6 months, and monthly for longer ranges.
- Updated the README screenshot for the new chart view.

## 1.0.2 - 2026-06-07

- Added Windows-friendly Python command resolution for the launcher and smoke-check.
- Added cached input token visibility while preserving the original total-without-cached token accounting.
- Added a total-with-cached token metric for daily usage, model breakdowns, and summary cards.
- Added 1-day and custom date range filters.
- Added an option to ignore the `codex-auto-review` model.
- Added expandable daily details for each model.
- Thanks @nisaev for the pull request that contributed the Windows support and cached-token dashboard improvements.

## 1.0.1 - 2026-05-28

- Added API-equivalent USD cost estimates for totals, daily usage, and model breakdowns.
- Fetches current model pricing from LiteLLM at dashboard load time, with bundled fallback prices for known Codex models.
- Documents that historical cost estimates are recalculated with the current price table.

## 1.0.0 - 2026-05-27

- Initial open-source release.
