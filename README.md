# MeterMesh

MeterMesh is a local, privacy-conscious usage dashboard for Codex, Claude Code, and OpenCode. It incrementally imports usage metadata into one app-owned SQLite index named **Unibase**, deduplicates overlapping live and backup sources, and serves Usage, Requests, and Data Health from committed SQL projections.

![MeterMesh 2.2.0 dashboard showing the All provider scope and Data Health](docs/screenshot-v2.2.0.png)

## Highlights

- Provider selector: All, Codex, Claude, OpenCode.
- Usage charts and provider-qualified model totals, with matching model names merged by default in the All scope.
- Dynamic chart colors that assign the strongest base colors to the current leading models before tonal variants.
- Independent visualization ranges with daily, weekly, and monthly token buckets.
- Active-time workday filtering with visible, dimmed non-working-day activity.
- Provider-aware Data Health with index integrity, coverage, and source freshness.
- Privacy-safe Requests with numbered pagination and grouping from 1 minute to 24 hours.
- Settings for source and model filtering, non-working weekdays, All-scope aggregation, reset, and Full reindex.
- Retained provenance: disabling one duplicate backup does not remove an event still supported by another source.
- Recorded OpenCode costs remain distinct from pricing estimates and unavailable costs.

## Unibase

The default database is:

```text
~/.metermesh/unibase.sqlite3
```

Override it with `METERMESH_UNIBASE_DB` or `--unibase-db`. Unibase is a rebuildable index. Provider files remain the source of truth and are opened read-only.

MeterMesh never resets or migrates `~/.codex/state_5.sqlite` or OpenCode's `opencode.db`. The old Claude `~/.claude/usage-dashboard.sqlite` is left untouched and is not used by MeterMesh 2.x.

## Data Sources

Defaults:

```text
Codex:    ~/.codex/sessions/**/rollout-*.jsonl
Claude:   ~/.claude/projects/**/*.jsonl
OpenCode: $XDG_DATA_HOME/opencode/opencode.db
          or ~/.local/share/opencode/opencode.db
```

For the live Codex source, MeterMesh also imports valid absolute rollout paths registered in `state_5.sqlite`. Direct runs automatically include additional Codex profiles such as `~/.codex-work/sessions` without storing their raw paths in Unibase. Docker runs must mount each additional profile at the same absolute path, as described below.

Compatibility overrides:

```text
CODEX_USAGE_DB / --db
CLAUDE_PROJECTS_DIR / --claude-projects
OPENCODE_USAGE_DB / --opencode-db
```

`--claude-db` remains accepted for one transition release but does not select the Unibase path.

## Backup Snapshots

MeterMesh creates an `add_stat` directory for every provider:

```text
~/.codex/add_stat/
~/.claude/add_stat/
<opencode-data-dir>/add_stat/
```

Recommended immutable snapshot layout:

```text
20260701T120000Z--before-reset--8f31a2c4/
├── snapshot.json
└── root/
```

Example manifest:

```json
{
  "format": "metermesh-provider-snapshot",
  "version": 1,
  "id": "stable-snapshot-id",
  "provider": "codex",
  "created_at": "2026-07-01T12:00:00Z",
  "label": "Before reset",
  "root": "root"
}
```

Legacy full copies placed directly under `add_stat` are detected only at fixed safe roots. New backups are registered unchecked. Legacy copies require two stable inventory observations before import.

## Codex Accounting Policy

Codex usage is deduplicated globally across live files, copied subagent histories,
and backups by normalized `last_token_usage` plus canonicalized `rate_limits`.
The earliest available timestamp wins. Session IDs and timestamps deliberately
do not participate in the key: copied history can occur in another session.
Independent requests with identical usage and limits therefore count as one;
different limit snapshots count separately. This is an intentional accounting
tradeoff introduced in commits `c92078b` and `6f9a0db`, not an exact billing ledger.

An incomplete Codex state inventory retains previously indexed files, including
ordinary session files, while importing available new data. Full rebuild still
requires a complete inventory. Providers with no installation or indexed history
are skipped; a missing previously indexed source remains an error.

## Privacy

Unibase stores usage metadata, hashed stream/event identities, token components, model/provider identifiers, cost semantics, and source provenance. It does not store prompts, responses, tool output, cwd, attachments, project paths, session titles, credentials, auth tokens, or account identity.

Requests and Settings APIs omit raw provider IDs and absolute source paths. Source errors are sanitized.

## Run

Requirements: Node.js 20+ and Python 3.10+.

```bash
npm install
npm run dev:all
```

Open <http://127.0.0.1:8765>.

On macOS, `Start MeterMesh.command` starts both the Python API and Vite frontend.

Direct API server:

```bash
python3 dashboard_api.py --host 127.0.0.1 --port 8766
```

### Run in Docker

`Dockerfile` + `docker-compose.yml` give you a sandboxed runtime: provider sources bind-mounted read-only, Unibase in an isolated volume, host port bound to `127.0.0.1`.

Requires Docker 24+ (or Docker Desktop) with Compose v2. The supplied Compose file supports Linux and macOS paths. On Windows, run it from WSL2 with provider data under the WSL home directory; native `C:\...` paths cannot be preserved inside the Linux container.

The default Compose file expects all three source directories to exist. Create empty user-owned directories for providers that are not installed; fail-fast bind mounts prevent Docker from silently creating them as `root`:

```bash
mkdir -p "$HOME/.codex" "$HOME/.claude" "$HOME/.local/share/opencode"
```

```bash
docker compose up -d                                                # macOS or WSL2
METERMESH_UID=$(id -u) METERMESH_GID=$(id -g) docker compose up -d  # Linux
```

Open <http://127.0.0.1:8765>.

Linux needs the extra ids because bind mounts there preserve host ownership — see [Linux: run as yourself](#linux-run-as-yourself).

#### Path mapping

Provider sources are bind-mounted **read-only** at the *same absolute path* they occupy on the host: `~/.codex` appears inside the container as `/home/you/.codex` (or `/Users/you/.codex` on macOS), not at a rewritten location.

| Host path | Container path | Env var | What it is |
|-----------|----------------|---------|------------|
| `~/.codex` | identical | `CODEX_USAGE_DB=$HOME/.codex/state_5.sqlite` | Codex live source + `add_stat/` backups |
| `~/.claude` | identical | `CLAUDE_PROJECTS_DIR=$HOME/.claude/projects` | Claude `projects/**/*.jsonl` + `add_stat/` backups |
| `~/.local/share/opencode` | identical | `OPENCODE_USAGE_DB=$HOME/.local/share/opencode/opencode.db` | OpenCode live source + `add_stat/` backups |
| — (named volume) | `/data/unibase` | `METERMESH_UNIBASE_DB=/data/unibase/unibase.sqlite3` | Rebuildable index and only persistent app data |

Mounting at identical paths is deliberate. Codex's `state_5.sqlite` records each session's rollout file as an **absolute** host path (`/home/you/.codex/sessions/…`). Mounted anywhere else, those paths would not resolve inside the container. Compose therefore reads `$HOME` from your shell and refuses to start if it is unset.

If a read-only source has no `add_stat/` directory, backup discovery receives `EROFS` and continues with the live source only. Existing snapshots remain visible through the read-only bind; MeterMesh never creates or writes `add_stat/` inside the container.

#### Linux: run as yourself

On macOS, Docker Desktop remaps ownership on bind mounts and the defaults work as-is. On Linux, bind mounts preserve host ownership and mode: `~/.codex` and `~/.claude` are commonly `0700` with `0600` files owned by you, so the container's default uid `10001` cannot even traverse them. Pass your own ids:

```bash
METERMESH_UID=$(id -u) METERMESH_GID=$(id -g) docker compose up -d
```

Persist it by putting the same two lines in a `.env` file next to `docker-compose.yml`:

```
METERMESH_UID=1000
METERMESH_GID=1000
```

The container still reaches the Unibase volume, which is owned by gid `10001` and group-writable — compose adds that gid as a supplementary group.

#### Custom source paths

If your provider data is not under the defaults, edit the `volumes` and `environment` blocks in `docker-compose.yml` together, keeping both sides of each mount identical:

```yaml
environment:
  CODEX_USAGE_DB: /opt/codex/state_5.sqlite   # ← must match the mount below
volumes:
  - type: bind
    source: /opt/codex
    target: /opt/codex                        # ← same absolute path
    read_only: true
    bind:
      create_host_path: false
```

Keep the host and container sides equal. Mapping a source to a different container path breaks Codex's absolute `rollout_path` lookups, as described above.

Additional Codex profiles referenced by `state_5.sqlite` need their own same-path read-only mounts. For example, add this alongside the default `~/.codex` mount:

```yaml
volumes:
  - type: bind
    source: ${HOME}/.codex-work
    target: ${HOME}/.codex-work
    read_only: true
    bind:
      create_host_path: false
```

#### Storing Unibase in a host directory

Unibase lives in a named volume by default. To inspect or back it up without `docker compose cp`, bind-mount it instead — but note this needs **both** steps on Linux, or the container cannot write its index:

```bash
mkdir -p ./unibase                                              # 1. create it yourself
METERMESH_UID=$(id -u) METERMESH_GID=$(id -g) docker compose up -d  # 2. run as its owner
```

```yaml
volumes:
  - type: bind
    source: ./unibase
    target: /data/unibase
    bind:
      create_host_path: false
  # …source mounts unchanged…
```

If Docker creates `./unibase` itself it makes it `root:root`, and the container — which never runs as root — cannot write there. `create_host_path: false` turns that silent failure into a startup error instead. Creating the directory without also passing `METERMESH_UID` fails the same way, since the default uid `10001` does not own it.

#### Sandbox guarantees

| Concern | Mitigation |
|---------|------------|
| Writes to provider sources | Blocked — bind-mounted read-only; absent `add_stat/` directories are treated as having no backups |
| Network exposure | Host port bound to `127.0.0.1:8765` only |
| Privilege escalation | `no-new-privileges`, `cap_drop: ALL`, non-root uid 10001 |
| Persistent app data | Only `/data/unibase` (group-writable to gid 10001, not world-writable) |

#### Updating to a new MeterMesh version

The version (`package.json`) and SPA (`dist/`) are baked in at build time; the container does no runtime git fetch.

```bash
git checkout main && git pull origin main          # latest, or:
git fetch && git checkout v2.2.1                  # pin a tag

docker compose build                               # rebuild image
docker compose up -d                               # recreate container
```

Unibase and the source bind mounts survive the rebuild. If a new version changes the schema, trigger **Full reindex** in Settings, or `docker compose down -v` to start fresh.

- Run `docker compose build --pull` periodically to refresh the pinned base images against security patches, independent of MeterMesh releases.

#### Smoke test

`tests/docker/smoke.sh` builds the image and runs it against throwaway fixtures — private `0700` sources, no pre-existing `add_stat/`, and a Codex `state_5.sqlite` holding absolute rollout paths — then asserts the indexed token totals actually come back:

```bash
tests/docker/smoke.sh
```

It uses its own compose project name and tears itself down afterwards, so it will not disturb a running `docker compose up -d` instance. A few assertions cover Linux-only bind-mount ownership behaviour and report `SKIP` on macOS.

## Settings And Maintenance

- **Apply** persists source, model, non-working weekday, and All-scope aggregation preferences with optimistic revision checking.
- **Reset Unibase** requires `RESET UNIBASE`, preserves settings and source registry, and blocks automatic indexing until Full reindex.
- **Full reindex** builds a staging Unibase from live sources and enabled backups, runs invariants and `PRAGMA integrity_check`, then atomically swaps the database.
- Usage, Requests, and Data Health continue reading the previous committed database while staging is built.

## API

```text
GET  /api/usage
GET  /data.json              compatibility alias
GET  /api/requests
GET  /api/settings
POST /api/settings
POST /api/unibase/reset
POST /api/unibase/reindex
GET  /api/unibase/status
```

The default provider scope is `all`. Explicit `provider=codex`, `provider=claude`, and `provider=opencode` links remain supported.

Active time accepts `workdays=1`. When enabled, configured non-working weekdays are excluded from totals and averages but remain visible as dimmed chart activity. The persistent **Workdays only** toggle switches the visualization back to all calendar days.

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/*.test.mjs
npm run build
npm run check
```

`npm run check` uses configured local provider data. Unit tests use synthetic privacy-safe fixtures.

## License

MIT. See [LICENSE](LICENSE).
