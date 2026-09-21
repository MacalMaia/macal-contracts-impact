# contracts-impact

Cross-service impact analysis for the macal platform. Answers the question:

> If I change this HTTP endpoint or pub/sub topic, **which other macal services break?**

It works by extracting a `.contracts.yaml` file in each repo that catalogues every HTTP route and pub/sub topic that service provides and consumes, then aggregating them into a single platform-wide map.

Currently indexes 8 macal services across 4 backends (FastAPI) and 4 frontends (Vue/Vite, Next.js, Nuxt).

## Why not just rely on GitNexus?

GitNexus is great for **within-repo** call graph analysis (rename safety, blast radius inside one service). It cannot answer cross-service questions for the macal stack because:

1. GCP Pub/Sub events are invisible to its tree-sitter rules
2. HTTP paths get mangled by API gateway prefixes
3. Custom HTTP client wrappers (`MacalUsersApiClient`, `usersApi.proxy(...)`) bypass its detection

`contracts-impact` is the cross-service complement. The two tools are complementary, not competing — and `contracts-impact` works equally well whether or not your team uses GitNexus.

## Install

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
uv tool install git+https://github.com/MacalMaia/macal-contracts-impact.git
```

The `contracts-impact` CLI is now available globally.

## Daily usage

All commands run from your terminal. Some assume your macal repos are cloned under a single parent directory. The default is `~/macal/`; override by setting `MACAL_ROOT` in your shell profile or passing `--macal-root`.

Only the direct children of that directory are indexed, one per service. `.contracts.yaml` is tracked in git, so a git worktree checked out *next to* its repo brings along its own copy of the index, usually stale. Those duplicates are ignored in favour of the directory named after the service, and every command says so. Keep worktrees under `<repo>-worktrees/` and they stay out of the index entirely.

### Find consumers of an endpoint

```bash
contracts-impact endpoint "POST /api/v1/bid"
```
```
POST /api/v1/bid
Provider: auction-engine
  handler: app/api/api_v1/endpoints/bids.py::place_bid:54
Consumers (1):
  • auctioneer-front
    src/composables/useRemateApi.ts::apiFetch:186
```

### Find publishers and subscribers of a pub/sub topic

```bash
contracts-impact topic auction.item.adjudicated
```
```
Topic: auction.item.adjudicated
Publishers (1):
  • auction-engine — app/core/pubsub.py::publish_adjudication_event:180
    schema: ItemAdjudicatedEventData
Subscribers (1):
  • macal-users-api — app/api/api_v1/endpoints/events.py::handle_item_adjudicated:445
```

### Status / inventory of every indexed service

```bash
contracts-impact status
```

### Detect orphans

```bash
contracts-impact orphans
```
Aggregates `topics_published` across all services and compares to `topics_subscribed`. Reports topics that are published but never subscribed, and topics that are subscribed but never published. Catches dead subscriptions before they bite you in production.

### Refresh a service's contracts after editing it

```bash
contracts-impact extract auction-engine
```

In CI or in a single-repo checkout (no `~/macal/` parent dir):

```bash
contracts-impact extract auction-engine --repo-path .
```

### Check whether an index still matches the code

```bash
contracts-impact validate --check macal-maia-front
```
```
  ✗ macal-maia-front index is behind the code:
      + consumes: GET /api/v4/operaciones/agendamiento/propiedades → macal-api
      + consumes: POST /api/v4/operaciones/agendamiento/aprobar-masivo → macal-api
    Run: contracts-impact extract macal-maia-front
```

Re-runs the extractors in memory and compares against the committed `.contracts.yaml`, exiting non-zero on any difference. It writes nothing, so it is safe to run on a dirty checkout before pushing. Without `--check`, `validate` still just schema-checks the index files as before.

## Stale indexes

`.contracts.yaml` is generated and committed, so it drifts behind the code between the moment you write a route and the moment CI regenerates it. An index that predates your change answers `0 consumers` for endpoints that *are* consumed, and that zero is indistinguishable from a real one.

When a query comes back empty, the CLI now says whether any index is behind its code:

```
Consumers: 0 found in indexed services
  ⚠ 8 of 8 indexes are behind their code (macal-new-web (116 committed),
    macal-maia-front (6 committed, 20 uncommitted), …). A 0 here may be a stale index.
    Confirm with: contracts-impact validate --check
```

The signal comes from git — the commit that last wrote the index, versus the source files that moved since — so it needs no change to the file format and works on indexes that already exist. It only counts files the extractors actually read (`.py` for backends, `.ts/.tsx/.js/.jsx/.vue/.mjs` for frontends), and stays silent outside a git checkout. Nothing is printed on a non-empty answer.

Changed sources are a cheap proxy, not proof: most edits never touch a route, so the warning over-reports on purpose. It points at `validate --check`, which re-extracts and says which index actually drifted. On the run above, 8 services had changed sources and exactly one had a contract that moved.

## CI integration

Each macal repo has a `.github/workflows/contracts-check.yml` that:
1. Installs `contracts-impact` via `uv tool install git+...`
2. Runs `contracts-impact extract <service> --repo-path .`
3. Fails the PR if `.contracts.yaml` drifted from what the code says

This keeps the contracts file honest without depending on developer discipline. If your edit broke the contract, the CI tells you.

Steps 2 and 3 can be collapsed into `contracts-impact validate --check <service> --repo-path .`, which reports the drift as endpoints gained and lost instead of a YAML text diff, and leaves the checkout clean rather than rewriting `.contracts.yaml` in order to diff it.

The workflow fires on pull requests and on pushes to `main`/`master`/`staging`, so it says nothing about a feature branch until the PR exists. That window — code written, PR not yet opened — is exactly when the index is behind and cross-service queries answer from it. That is what the stale-index warning above covers.

If the contracts repo is private, your CI needs a deploy key or a GitHub token with read access to `MacalMaia/macal-contracts-impact`.

## Local workflow per repo

Each macal repo has either a `make contracts` target (Python backends) or a `pnpm contracts:extract` script (TypeScript frontends). Run it before pushing to refresh `.contracts.yaml`:

```bash
# inside any backend repo
make contracts

# inside any frontend repo
pnpm contracts:extract
```

For richer cross-service queries, run from a directory that has all macal repos cloned alongside (typically `~/macal/`):

```bash
contracts-impact endpoint "GET /api/v1/admin/refunds/batches"
contracts-impact topic payment.capture-requested
```

## AI editor users (Claude Code, Cursor)

Each indexed repo includes a `macal-impact` skill at `.claude/skills/macal-impact/SKILL.md`. AI editors that support Claude skills (Claude Code, Cursor with the right config) will load it and automatically run `contracts-impact` before any HTTP endpoint or pub/sub edit. You can also invoke it manually with the regular CLI commands.

## What gets extracted, per service

The extractor walks the repo and produces a `.contracts.yaml` like this:

```yaml
service: auction-engine
provides:
  http:
    - { method: POST, path: /api/v1/bid, handler: app/api/api_v1/endpoints/bids.py::place_bid, line: 54 }
  topics_published:
    - { topic: auction.item.adjudicated, schema: ItemAdjudicatedEventData, publisher: app/core/pubsub.py::publish_adjudication_event, line: 180 }
consumes:
  http:
    - { target: macal-users-api, method: GET, path: /api/v1/internal/wallets/{user_id}/balance, caller: app/services/macal_users_api_client.py::MacalUsersApiClient.get_balance, line: 107 }
  topics_subscribed:
    - { topic: wallet.balance-changed, handler: app/api/api_v1/endpoints/events.py::handle_balance_changed, line: 30 }
```

The extractor handles:
- **FastAPI routes** in `app/api/api_v*/` with multi-version layouts (`api_v1`, `api_v3`, `api_v4`, `v1`)
- **HTTP client classes** with `self.base_url = settings.X` patterns
- **Generic proxy clients** like `users_api_client.proxy("GET", "/path", ...)`
- **Same-class wrapper methods** like `cls._post(path, body)`
- **Pub/Sub publishers** including wrappers like `_publish_defontana_event(topic=..., event_type=...)`
- **Pub/Sub subscribers** detected via `/events/<topic>` URL patterns — the URL last segment IS the canonical topic name
- **Frontend fetch patterns** in TypeScript/Vue/Next.js/Nuxt: composable wrappers, Next.js route handlers, Nuxt server route singletons
- **Settings-driven topic names** require a literal default in `app/core/config.py`. If neither a literal at the publish site nor a literal default in config is present, the tool emits an `unresolved_settings_topic` warning — no silent fallback.

## Limitations

- Cross-service queries require **all macal repos cloned in one parent directory**. CI per-repo can only verify drift, not answer cross-service questions.
- Frontend extraction is regex-based, not AST-based. ~95% accurate for standard patterns; new patterns may need extractor updates.
- The tool only detects calls to **other indexed macal services**. External integrations (Auth0, Algolia, GCP, Defontana ERP) are intentionally excluded.

## Development

```bash
git clone https://github.com/MacalMaia/macal-contracts-impact.git
cd contracts-impact
uv sync
uv run contracts-impact --help
```

Tests live under `tests/` and use golden fixture mini-repos at `tests/fixtures/`. Run with:

```bash
uv run pytest
```

The critical determinism test is `test_publisher_settings_no_default_emits_warning_not_silent_drop`: it locks in that the tool will never silently drop a publisher when settings indirection can't be resolved.

## License

Internal tooling — Macal Maia.
