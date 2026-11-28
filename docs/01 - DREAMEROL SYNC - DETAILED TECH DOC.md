# DREAMEROL SYNC — Detailed Technical Documentation

| Document information | Details |
|---|---|
| **Status** | Current implementation reference |
| **Canonical path** | `docs/DREAMEROL SYNC - DETAILED TECH DOC.md` |
| **Scope** | CLI, GitHub client, synchronization engine, Auth Broker, web API, dashboard frontend, persistence, testing, security and deployment |

---

This document describes the DREAMEROL SYNC project structure, runtime behavior, configuration, security controls, API contracts, testing and deployment considerations.

## Contents

1. [System orientation](#1-system-orientation)
2. [Component map](#2-component-map)
3. [Installation, configuration and startup](#3-installation-configuration-and-startup)
4. [CLI implementation](#4-cli-implementation)
5. [Synchronization engine](#5-synchronization-engine)
6. [Web API implementation](#6-web-api-implementation)
7. [Auth Broker implementation](#7-auth-broker-implementation)
8. [Dashboard frontend implementation](#8-dashboard-frontend-implementation)
9. [Persistence](#9-persistence)
10. [Testing](#10-testing)
11. [Release status and known limitations](#11-release-status-and-known-limitations)
12. [Reference documents](#12-reference-documents)

---

## 1. System orientation

DREAMEROL SYNC keeps GitHub forks synchronized with their upstream repositories through the official GitHub API. It is one synchronization engine exposed through three runtimes:

| Runtime | Entry point | Responsibility |
|---|---|---|
| CLI | `main.py:244-318` | Account management, Broker login, preview, explicitly confirmed live sync |
| Auth Broker | `auth_broker.app:create_app` (`auth_broker/app.py:74-150`) | GitHub App OAuth, PKCE, transaction state, credential delivery |
| Web application | `api.app:app` (`api/app.py:19-24`) | Server-side sessions, dashboard API, static pages, sync history |

Core invariant: **discovery and comparison are read-only; a merge-upstream write requires a fresh comparison, an `ahead_by == 0 / behind_by > 0` state, and explicit user confirmation in interactive modes.**

```
Browser ──► /api/* (8001) ──► WebAuthManager ──► Broker /auth/* (8000)
                 │                                     │
                 └────► ForkSynchronizer ◄──────────────┘
                              │
                              ▼
                     GitHub REST API
                 /user/repos · /compare · /merge-upstream
```

---

## 2. Component map

| Path | Key symbols / files | Role |
|---|---|---|
| `main.py` | `build_parser()`, `filter_repositories()`, `confirm_synchronization()`, `_run_broker_login()`, `main()` | CLI orchestration, account selection, typed `SYNC` gate, exit codes |
| `fork_sync/github.py` | `GitHubClient` | REST calls, pagination, bounded retries, compare, merge-upstream |
| `fork_sync/synchronizer.py` | `ForkSynchronizer` (`get_forks`, `compare_fork_diagnostic`, `_compare_fork`, `preview_fork`, `preview_all`, `sync_fork`, `sync_all`) | Discovery, validation, status derivation, guarded sync |
| `fork_sync/accounts.py` | `AccountManager`, `SERVICE_NAME = "DreamerolForkSync"`, `ACCOUNT_INDEX = "__account_index__"` | OS keyring account registry |
| `auth_broker/routes.py` | `/auth/start`, `/auth/callback`, `/auth/poll`, `/auth/ack` | Broker HTTP contract |
| `auth_broker/transactions.py` | `TransactionState`, `TransactionStore`, `PollResult` | In-memory transaction state machine |
| `auth_broker/oauth.py` | `GitHubOAuthClient`, `AuthorizationRequest` | GitHub authorization URL and PKCE code exchange |
| `auth_broker/token_policy.py` | `validate_token_response()`, `ValidatedToken`, `FORBIDDEN_TOKEN_FIELDS` | Token response validation without secret exposure |
| `auth_broker/security.py` | `generate_polling_secret()`, `hash_secret()`, `verify_secret()`, `pkce_s256_challenge()` | 256-bit secret, SHA-256 hashing, PKCE S256 |
| `auth_broker/config.py` / `rate_limit.py` | `BrokerConfig`, `RateLimitConfig`, `InMemoryRateLimiter`, `get_client_ip()` | Configuration validation, trusted proxies, per-IP/per-transaction limits |
| `api/routes.py` | `/api/auth/*`, `/api/sync`, `/api/history`, `/api/debug/compare-forks` | Web API contract and server-side revalidation |
| `api/auth.py` | `WebAuthManager`, `SESSION_COOKIE` | Server-side sessions, cookie, credential lifetime |
| `api/sync_history.py` | `SyncHistoryStore` | Atomic JSON persistence of last sync and operations |
| `web/` | `index.html`, `style.css`, `app.js`, `theme_guard.js` integration | Welcome/sign-in page |
| `DASHBOARD/` | `index.html`, `app.js`, `utils.js`, `dashboard.css`, `theme_guard.js` | Fork dashboard, counters, selection, preflight, sync actions |
| `backend/github_client.py` | legacy client | Kept for compatibility tests |
| `scripts/` | `check.ps1`, `start-smoke.ps1`, `stop-smoke.ps1`, `watch-smoke.ps1` | Local checks and smoke-stack lifecycle |
| `tests/` | 19 modules + `conftest.py` | Full automated suite |

---

## 3. Installation, configuration and startup

### 3.1 Requirements

- Python **3.11+** (CI matrix: 3.11 and 3.12).
- `pip install -r requirements.txt`.

Pinned direct dependencies: `requests==2.32.3`, `python-dotenv==1.0.1`, `keyring==25.5.0`, `pytest==8.3.3`, `fastapi==0.162.5`, `uvicorn[standard]==0.54.0`, `httpx2==2.16.0`. This is not a transitive lockfile.

### 3.2 Environment variables

Copy `.env.example` to `.env` in the project root. `.env` is Git-ignored; never commit real values.

| Variable | Default | Consumer / purpose |
|---|---|---|
| `GITHUB_TOKEN` | none | CLI / legacy client; used unless `--account` is supplied |
| `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` | required | Broker OAuth client |
| `BROKER_CALLBACK_URL` | `http://127.0.0.1:8000/auth/callback` | Fixed callback; remote deployments require HTTPS |
| `BROKER_REQUEST_TIMEOUT` | `30` | GitHub token-exchange timeout |
| `BROKER_URL` | `http://127.0.0.1:8000` | CLI and web Broker base URL |
| `BROKER_DASHBOARD_URL` | none | Optional redirect after callback |
| `COOKIE_SECURE` | `auto` | `auto` (scheme-based), `true` or `false` |
| `BROKER_RATE_LIMIT_ENABLED` | `true` | Enable in-memory rate limits |
| `BROKER_RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding window |
| `BROKER_RATE_LIMIT_START_PER_IP` / `_CALLBACK_PER_IP` / `_POLL_PER_IP` / `_ACK_PER_IP` | `60` / `120` / `240` / `60` | Per-IP limits |
| `BROKER_RATE_LIMIT_POLL_PER_TRANSACTION` / `_ACK_PER_TRANSACTION` / `_FAILURES_PER_IP` | `60` / `10` / `30` | Per-transaction and failure limits |
| `BROKER_TRUSTED_PROXIES` | empty | Comma-separated proxy IPs/CIDRs allowed to set `X-Forwarded-For` |

Rate-limit counters are per process. Blocked requests return `429` with `Retry-After`; the browser callback returns an HTML `429` page.

### 3.3 GitHub App prerequisites

1. Fixed callback URL; no wildcard callback matching.
2. Opt out of user-to-server token expiration — `token_policy.py:16-20` rejects `expires_in`, `refresh_token` and `refresh_token_expires_in`.
3. Contents: read and write; Metadata: read-only.
4. App installed on the account/organization owning the forks; signed-in user must have write access.
5. Leave `BROKER_TRUSTED_PROXIES` empty for direct loopback access.

### 3.4 Process startup

```bash
uvicorn auth_broker.app:create_app --factory --host 127.0.0.1 --port 8000
uvicorn api.app:app --host 127.0.0.1 --port 8001
```

Static mounts: `/static` → `web/`, `/dashboard/static` → `DASHBOARD/` (`api/app.py:106-120`). Pages: `/`, `/dashboard`, `/healthz` (`api/app.py:123-144`). The Broker also exposes `/healthz`.

Windows smoke stack:

```powershell
.\scripts\start-smoke.ps1 -Watchdog
.\scripts\stop-smoke.ps1
```

`start-smoke.ps1` checks `.env` and free ports, starts both processes, waits for health endpoints, stores PIDs in `%TEMP%\dreamerol-smoke-state.json`, and supports `-Force` and `-DryRun`. The watchdog polls every 5 seconds and restarts only local processes.

---

## 4. CLI implementation

`main.py:27-69` defines the argument surface:

| Argument | Effect |
|---|---|
| `--add-account` | Hidden token prompt; stores credential in OS keyring |
| `--list-accounts` | Lists saved GitHub usernames |
| `--login` | Runs the Broker OAuth flow |
| `--account USERNAME` | Selects a saved account explicitly |
| `--broker-url URL` | Overrides `BROKER_URL` |
| `--dry-run` | Preview only; no confirmation and no writes |
| `--repo OWNER/REPO` | Repeatable exact fork filter |

Credential resolution (`main.py:111-131`): explicit `--account` → keyring; else `GITHUB_TOKEN`; else a single saved account; more than one saved account without `--account` is an error. `--repo` filters are exact, and unknown names raise `ValueError` rather than being silently ignored (`main.py:72-92`).

Live sync requires the exact string `SYNC` (`main.py:95-108`, `295-298`). There is no bypass flag. Exit code is `1` if any result has status `error` or `conflict`, otherwise `0` (`main.py:304`).

Broker login (`main.py:134-241`) polls every 2 seconds for at most 600 seconds. It stores the credential in the keyring before sending `/auth/ack`; if storage fails, it does not acknowledge and offers a retry while the transaction is still valid.

---

## 5. Synchronization engine

### 5.1 GitHub client (`fork_sync/github.py`)

- Paginates `GET /user/repos?type=owner`.
- Retries transient GET failures up to three attempts with bounded backoff.
- Compares with `basehead = UPSTREAM_OWNER:BRANCH...FORK_OWNER:BRANCH`.
- Performs writes only through `POST /repos/{owner}/{repo}/merge-upstream` with `{"branch": ...}`.

### 5.2 Fork discovery (`fork_sync/synchronizer.py:18-79`)

Only records with `fork is True` are candidates. A fork is valid when both `parent.full_name` and `parent.default_branch` are non-empty strings. If the list response omits parent metadata, `get_repository()` is called; HTTP 404 increments `unavailable_forks`, other errors propagate.

`last_diagnostics` / `get_last_diagnostics()` expose read-only counters:

| Counter | Meaning |
|---|---|
| `owned_repositories` | Repositories returned by discovery |
| `fork_candidates` | Records with `fork is True` |
| `valid_forks` | Forks with usable upstream metadata |
| `unavailable_forks` | Forks whose detail lookup returned 404 |

### 5.3 Comparison and status derivation

`_compare_fork()` (`:239-288`) requires valid `full_name`, `default_branch`, upstream name and upstream branch, then strictly validates `ahead_by`/`behind_by` as non-negative integers (booleans rejected). `compare_fork_diagnostic()` (`:85-237`) produces a richer read-only record with endpoint, `basehead`, timings, HTTP status, raw GitHub status and `result_state` (`VERIFIED_FRESH`, `UNVERIFIED`, `fresh_api_error`, `fresh_api_invalid`, `error`, `not_found_in_discovery`).

| `ahead_by` | `behind_by` | Derived status |
|---:|---:|---|
| 0 | 0 | `up-to-date` |
| 0 | > 0 | `behind` |
| > 0 | 0 | `ahead` |
| > 0 | > 0 | `diverged` |

### 5.4 Preview and live sync

- `preview_fork()` (`:352-393`) returns `repository`, `status`, `message`, `ahead_by`, `behind_by` and performs no write.
- `preview_all()` (`:395-410`) processes all discovered or supplied forks.
- `sync_fork()` (`:412-484`) re-compares immediately before any write and merges **only** when `behind_by > 0` and `ahead_by == 0`.
- `sync_all()` (`:486-518`) isolates each repository so one failure cannot stop the others; `dry_run=True` returns `status: "dry-run"` without writes.

| Result status | Meaning |
|---|---|
| `synced` | GitHub accepted the upstream synchronization request |
| `up-to-date` | No refresh required |
| `behind` / `dry-run` | Preview state; no write performed |
| `ahead` | Fork has local commits; automatic merge refused |
| `diverged` | Both sides changed; automatic merge refused |
| `ambiguous` | Merge outcome unknown after timeout or HTTP ≥ 500; no automatic retry |
| `conflict` | GitHub returned HTTP 409 |
| `error` | Invalid data or failed API request |

Error messages include HTTP status, GitHub `message`, `errors` and `documentation_url` where available (`_github_error_details()`, `:290-320`).

---

## 6. Web API implementation

### 6.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/start` | Start Broker login; set session cookie |
| GET | `/api/auth/status` | Poll session state; clear cookie on terminal state |
| POST | `/api/auth/logout` | Discard session |
| POST | `/api/auth/switch` | Logout for a different GitHub account |
| POST | `/api/sync` | Preview or confirmed live sync |
| GET | `/api/history` | Persisted per-account results |
| POST | `/api/debug/compare-forks` | Read-only compare diagnostics, max 20 repositories |

### 6.2 `/api/sync` contract and safety gates

Request fields include `dry_run`, `mode` (`"list"` for fast discovery), `repositories`, `confirmation`, `account` and optional `authorization` header (`api/routes.py`).

- Live sync requires `confirmation == "SYNC"` and at least one repository; more than 5,000 or duplicates are rejected (`:283-297`).
- `mode: "list"` returns `status: "pending"` rows with the real GitHub `fork` boolean and no comparison (`:343-365`).
- Targeted preview re-runs discovery and rejects any repository that is no longer an available fork (`:366-389`).
- Live sync re-runs discovery, verifies every selected name, re-compares every selected fork server-side, and passes only `behind` forks to `sync_all()` (`:393-439`). This prevents a stale or edited browser request from sending an up-to-date fork to `merge-upstream`.
- History is recorded only for `synced` results; every result is enriched with `last_successful_sync` (`:441-462`).
- Response `SyncResponse` contains `account`, `total`, `results`, `diagnostics` (preview only) and `operation_id` (live only) (`:464-470`).
- GitHub `401` clears the server session and cookie and returns `{"state": "REAUTH_REQUIRED", ...}`; `403` maps to an explicit permissions message; discovery failures map to `502`.

### 6.3 Debug diagnostics

`/api/debug/compare-forks` (`:515-614`) accepts 1–20 repository names, performs fresh discovery plus `compare_fork_diagnostic()` for each, and returns `account`, `discovery_diagnostics`, `total` and `results`. It never writes and never updates counters.

### 6.4 Session implementation (`api/auth.py`)

`WebAuthManager` uses random session IDs and a 600-second TTL, extends sessions on activity, and keeps credentials server-side. The browser receives only the `dreamerol_session` cookie (`HttpOnly`, `SameSite=Lax`, `COOKIE_SECURE` controlling `Secure`). All `/api/` responses use `Cache-Control: no-store`; state-changing `/api/` POSTs are checked against `Origin`/`Referer` (`api/app.py`). There is no manual PAT form on the welcome page; browser users sign in through the Broker.

---

## 7. Auth Broker implementation

### 7.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/start` | Create transaction from polling-secret hash; return authorization URL |
| GET | `/auth/callback` | Validate `state`/`code`, exchange with PKCE, move to token-ready or safe failure |
| POST | `/auth/poll` | Require `X-Polling-Secret`; deliver credential |
| POST | `/auth/ack` | Confirm local storage; clear credential |

### 7.2 Transaction state machine (`auth_broker/transactions.py`)

`PENDING → PROCESSING → TOKEN_READY → DELIVERY_AUTHORIZED → ACKNOWLEDGED`, with terminal `FAILED`, `CANCELLED`, `EXPIRED`. TTL is 600 seconds; cleanup runs every 60 seconds. Callback `state` is single-use (`claim_callback()`), polling secrets are compared in constant time, and `access_denied` maps to `CANCELLED`.

### 7.3 Token policy

`auth_broker/token_policy.py` validates HTTP 2xx, `application/json`, non-empty string `access_token`, `token_type: bearer`, and absence of all forbidden expiration fields. `ValidatedToken.access_token` has `repr=False`; OAuth `code`/`state` are redacted from uvicorn access logs.

### 7.4 Security controls

- 256-bit polling secret; only its SHA-256 hash is sent to `/auth/start`.
- PKCE `S256`; authorization URL generated only for `github.com` with `prompt=select_account`.
- Client rejects authorization URLs outside the Broker/GitHub contract.
- Fixed loopback callback and dashboard URL validation.
- Per-IP and per-transaction rate limits; trusted-proxy IP parsing.
- Security headers, `no-store` on auth responses, `openapi_url=None`.
- In-memory transactions, sessions and rate limits imply a single Broker/API worker.

---

## 8. Dashboard frontend implementation

| Concern | Implementation |
|---|---|
| Fork-only counters | `computeDashboardCounts()` uses `fork === true`, excludes `fork === false`, treats missing/invalid flags as data errors, deduplicates by repository name; four counters sum to `TOTAL FORKS` |
| Classification | `0/0` → `UP TO DATE`; `0/>0` → `TO BE SYNCED`; `>0/>0` and `>0/0` → `UNSYNCABLE`; failed/unverified checks → `CHECK FAILED / UNVERIFIED` |
| Selection | Fast list mode enables checkboxes immediately; selection kept in `sessionStorage` per account; checkboxes disabled only while checks run |
| Background checks | `runBackgroundChecks()` sends batches of 10 with `{dry_run:true, repositories:[...]}`; results mapped by `repository` |
| Timeouts | `PREFLIGHT_TIMEOUT_MS = 30000`; `CHECK_TIMEOUT_MS = 8000` |
| BFCache | `handlePageShowRestore()` preserves counters/results and prevents duplicate batch runs |
| Theme isolation | `DASHBOARD/theme_guard.js` with `@layer dreamerol-guard` neutralizes injected third-party dark-theme CSS |
| Session expiry | 401 during checks shows `SESSION EXPIRED — PLEASE SIGN IN` without redirect; sync buttons remain disabled until a valid session and completed checks |
| Sync actions | Green `SYNC CHOSEN` and red `SYNC ALL`, separate confirmations, empty-selection guards, busy-state blocking |
| Cache control | Versioned asset URLs such as `?v=20261122s` and `?v=20261121d` to defeat stale browser CSS/JS |

---

## 9. Persistence

Implementation: `api/sync_history.py`.

Default store: `data/sync_history.json` (Git-ignored). Schema:

```json
{
  "accounts": {
    "OWNER": {
      "repos": {"owner/repo": {"last_successful_sync": "<iso-8601>"}},
      "operations": [
        {"operation_id": "<uuid>", "created_at": "<iso>", "targets": [], "results": [], "summary": {}}
      ]
    }
  }
}
```

Writes use a temporary file, `fsync` and `os.replace` for atomicity. Account and repository keys are normalized case-insensitively. Only the most recent 20 operations are retained.

---

## 10. Testing

### 10.1 Verified execution

```
python -m pytest -q -p no:cacheprovider
```

**2026-11-23:** `506 passed in 8.09s` (no warnings). Tests use fakes for GitHub and the keyring; no real token is required and no real repository is synchronized. `tests/conftest.py` isolates the history store in a temporary directory and blocks real GitHub write requests except the OAuth token endpoint.

### 10.2 Complete test inventory

19 modules, 349 `test_*` declarations, expanded by parametrization to 506 collected cases.

| Test module | Declarations | Coverage |
|---|---:|---|
| `tests/test_accounts.py` | 6 | Keyring account storage and selection |
| `tests/test_api.py` | 23 | Dashboard API, preview/sync contract, fork serialization, diagnostics |
| `tests/test_auth_broker_api.py` | 20 | Broker routes, callback, polling, acknowledgement |
| `tests/test_auth_cli.py` | 16 | CLI Broker login, keyring storage, ACK |
| `tests/test_auth_client.py` | 18 | Broker client, trusted URL checks, secret handling |
| `tests/test_auth_e2e.py` | 1 | CLI ↔ Broker end-to-end flow |
| `tests/test_auth_mock_oauth_e2e.py` | 3 | Mocked GitHub OAuth exchange |
| `tests/test_auth_oauth.py` | 31 | Authorization URL, `state`, PKCE, token policy |
| `tests/test_auth_security.py` | 5 | Secret hashing, redaction, security helpers |
| `tests/test_auth_transactions.py` | 38 | Transaction state machine, TTL, single-use state |
| `tests/test_cli.py` | 6 | CLI arguments, credential resolution, `SYNC` confirmation |
| `tests/test_dashboard_progress.py` | 23 | Sync action group, progress, counters after sync/reload |
| `tests/test_dashboard_strict_counts.py` | 18 | Strict fork counting and classification |
| `tests/test_dashboard_workflow.py` | 41 | Welcome/dashboard DOM workflow, theme guard, cache-bust, simulated profiles |
| `tests/test_github_client.py` | 17 | REST pagination, retries, merge-upstream request |
| `tests/test_rate_limit.py` | 23 | Per-IP/per-transaction limits, trusted-proxy parsing |
| `tests/test_sync_history.py` | 6 | Atomic JSON persistence, per-account history |
| `tests/test_synchronizer.py` | 19 | Discovery, comparison, status derivation, merge safety |
| `tests/test_web_auth.py` | 35 | Sessions, cookie, 401/403 handling, CSRF and headers |

### 10.3 CI and local checks

- `.github/workflows/tests.yml`: push/PR on `main` and `feature/**`, plus `workflow_dispatch`; Ubuntu/Windows × Python 3.11/3.12; `compileall`, three `node --check`, PowerShell parse, `pip check`, pytest. Manual run `#50` on `Dreamerol/main` → `success` (4/4 jobs).
- `scripts/check.ps1`: compile, JavaScript syntax, PowerShell parse, `pip check`, optional pytest.
- Browser/CDP checks are manual validations, not committed browser E2E automation.

---

## 11. Release status and known limitations

**Project status:** Python 3.11+ support; automated test suite; CI workflow; MIT license; no monetization.

**Requires your own setup:** GitHub credentials (PAT or GitHub App); `.env` never committed; fixed loopback callback and correct installation permissions.

**Not covered by automated CI:** real GitHub App sign-in/callback and installation permissions; mutating sync against live repositories; remote HTTPS/TLS proxy deployment (`COOKIE_SECURE=true`, `--proxy-headers`); multi-worker sessions; a separate CSRF token is not implemented; token refresh/rotation is not implemented. Sessions, credentials and rate-limit counters are in-memory and per-process — run a single API/Broker worker. Browser E2E and visual validation remain manual.

**Operational rule:** use dry-run preview first; no live sync occurs without explicit confirmation.

---

## 12. Reference documents

- `README.md` — project overview and usage.
- `docs/GITHUB_APP_AUTH_SPEC.md` — GitHub App authentication contract.



