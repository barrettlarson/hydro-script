# CLAUDE.md — hydro-script

Cross-session context for this project. Read this first.

A full-stack pool/spa automation system for a Jandy **iAquaLink** controller.
The bar is engineering rigor — tests, typed code, documented architecture,
thoughtful failure handling — not just "it works."

Owner: Barrett Larson. Repo: `barrettlarson/hydro-script`.

## The architecture fact that drives everything

iAquaLink has **no local API**. The controller is operated through Jandy's
cloud service (`iaqualink-api.realtime.io`) over HTTPS, authenticated with
account credentials + system serial. The `iaqualink` Python library
(`flz/iaqualink-py`) wraps this. Consequences:

- Code runs **anywhere with internet** — a cloud server reaches Jandy exactly
  as well as a box at the house. "Control it away from home" is free once the
  backend is reachable, not a feature to build.
- The library already handles 429 backoff (httpx-retries, Retry-After) and 401
  auth replay internally, so few raw rate-limit errors reach our layer. The
  error taxonomy is for observability, not retry.
- Everything is **cloud-polled**. Water temps change slowly; polling more than
  ~once/30s buys nothing and risks rate limits. Hence the cache.

## Design principles

1. **Logic/interface separation.** Pure logic in `controls.py` — raises,
   returns, never prints or reads argv. CLI and FastAPI both import it. This is
   what makes it testable.
2. **The cache decouples clients from upstream.** One poller reads Jandy;
   `/api/status` reads the cache. Client count never multiplies upstream load.
3. **Failures are categorized, not stringified.** `classify()` maps exceptions
   onto a small taxonomy; failures are timestamped into bounded history.
   Classify now, *react* later — don't over-fit exception mapping to guesses.
4. **Epoch floats internally; ISO strings only at the API edge.**
5. **Tests use fake devices** — no hardware, delays monkeypatched out, suite
   under 2s.
6. Small modules, type hints, docstrings that explain *why*.

## Project structure

```
server/
  app/
    aqualink.py   # connection helper (credentials, open_devices, require)
    auth.py       # session auth: shared login vs .env cred, require_auth
    controls.py   # pure logic: spa/pool on-off, status read, safety
    cli.py        # thin CLI wrapper (print/exit/argv)
    handlers.py   # Lambda entry points: http_handler (Mangum) + poll_handler
    config.py     # secret loading: SSM Parameter Store -> os.environ
    main.py       # FastAPI: actions, status, health, push; serves client/dist
    poller.py     # poll loop feeding the StateCache (on_snapshot + on_cycle)
    errors.py     # error taxonomy: classify(exc) -> FailureCategory
    cache.py      # StateCache: snapshot, staleness, failure history
    store.py      # persistence: DocumentStore (memory/file/DynamoDB)
    push.py       # Web Push: VAPID config, subscription store, sender
    watcher.py    # pure heat-watch logic: snapshot -> PushMessages
  tests/          # fake-device tests, one module per app module
client/           # React + TypeScript PWA (Vite), mobile-first
  src/
    api.ts             # typed API client (same-origin /api)
    usePolledState.ts  # polls status+health every 15s, pauses when hidden
    App.tsx            # temps, spa/pool cards, health, login gate, bell
    Login.tsx          # shared-credential login form (shown on 401)
    sw.ts              # service worker: precache + push display
    push.ts / usePush.ts  # subscribe/unsubscribe, permission state
scripts/              # Put-Secrets.ps1: mirror .env into SSM (SecureString)
template.yaml         # AWS SAM: two functions, state table, schedules
justfile              # task runner
pyproject.toml        # deps, managed by uv (+ uv.lock)
.env                  # IAQUALINK_USER/PASS, SESSION_SECRET, VAPID_PRIVATE_KEY
                      #  (gitignored, never commit)
.data/                # file-backend state: cache/watches/subscriptions JSON
                      #  (gitignored; DynamoDB replaces it on Lambda)
```

## Commands

```
just spa-on / spa-off / pool-on / pool-off / pump-on / pump-off / status / safety
just server                # FastAPI dev server (uvicorn --reload, :8000)
just client                # Vite dev server (:5173, proxies /api to :8000)
just client-build          # production client build into client/dist
just client-test           # vitest (jsdom)
just client-e2e            # Playwright (mocked /api, needs chromium)
just check                 # ruff + mypy + pytest
just vapid-keys            # generate a Web Push VAPID keypair
just up / just down        # docker compose (serves built client at :8000)
PYTHONPATH=server python -m app.cli [spa-on|...|status|safety]
```

Nightly safety shutoff, until EventBridge replaces it:

```
0 2 * * * PYTHONPATH=server <python> -m app.cli safety >> <log> 2>&1
```

---

# State of the project

**Built and working:** the control logic and CLI; the FastAPI app with action,
status, and health endpoints; the error taxonomy and `StateCache`; the
background poller and cache read path; the React PWA (temps, spa/pool cards,
target-temp sliders, filter-pump toggle, health indicator, installable);
shared-credential cookie auth; Web Push heat-up notifications; the
externalized state store; CI running ruff + mypy + pytest + vitest + Playwright.

**Not built:** aux controls (lights, bubbles/blower) — blocked on confirming
the `aux_N` mapping from real `status` output. Color/effects are deferred as
model-dependent. Everything in Phase 4 and Phase 5 below.

## Decisions that still bind

Things a reader can't recover from the code:

- **Actions are synchronous.** The valve delay was removed once we learned
  AquaLink stages valve actuation internally, so actions return fast and need
  no background-task + poll-for-result dance.
- **The poller was built before the web client**, deliberately, so the first
  multi-client load landed on a cache instead of multiplying upstream calls.
- **One shared login**, checked against `IAQUALINK_USER`/`PASS`, chosen over a
  separate app password because the household already shares the Jandy account.
  No user store; the Jandy credential stays server-side.
- **`/api/health` is auth-gated** because `recent_failures` carries raw
  exception text. Everything except login/logout lives on the `protected`
  router so new routes are gated and persisted by default.
- **Push is strictly per-device.** No subscription means no watch, so nobody
  gets pinged for someone else's button press. Device identity is an opaque
  `device_id` minted into the signed session cookie.
- **Progress notifications throttle to one per whole degree**, sharing a tag so
  they replace in place. Untamed, ~30s polls would blow through iOS's push
  budget.
- **`HEATER_OFF_GRACE` is 90s** because the cache briefly lags a just-issued
  spa-on, and without the grace period the watch would cancel itself instantly.
- **Explicit push TTLs** (ready 3600s, progress 120s): pywebpush defaults to
  `ttl=0`, which silently drops messages for a briefly-offline phone.
- **The VAPID public key is derived from the private key** at startup rather
  than stored, so the pair cannot drift out of sync.
- **Safety never touches the filter pump** — circulation is not a heat risk.
- The service worker **never caches `/api`**.
- **`api.ts` types declare what the client consumes, not the full payload.**
  They're hand-written, so a "mirror" would drift silently and look
  authoritative while being wrong — a narrow type is instead an honest
  statement of coupling, and TypeScript reads a wider response fine. `/docs`
  is the discoverable source of truth. E2E mocks deliberately return the whole
  server payload, so extra fields stay exercised. If real symmetry is ever
  wanted, generate the types from the OpenAPI schema rather than hand-copying.
- The footer's "Updated" time reads `last_snapshot_at`, not `last_success_at`:
  an action succeeds without producing data, so the latter would timestamp the
  button press while showing older temperatures.
- Playwright gotcha: headless Chromium reports `Notification.permission` as
  `denied`; stub it in an initScript or push specs fail confusingly.

---

# Phase 4 — Deploy to AWS Lambda [~] (active)

AWS is viable because iAquaLink is cloud-only (see the architecture fact).
Account `330555373901`, region **us-east-1**. Decisions made 2026-08-27:

- **Two Lambdas off one zip artifact** — an HTTP function (FastAPI via Mangum
  behind a Function URL) and an EventBridge-scheduled poll function. Chosen
  over always-on Fargate/Lightsail on cost: this lands in the perpetual free
  tier (~$0-1/mo vs ~$5-15).
- **Zip, not container.** The console-created `hydroScript` is Zip type and
  package type is immutable after creation. Deps are ~40-60 MB against a 250 MB
  limit. Cross-build Linux wheels from Windows with
  `uv pip install --python-platform x86_64-manylinux2014 --only-binary=:all:`.
- **AWS SAM** (`template.yaml` in-repo). SAM creates its own function, so the
  console-made one is throwaway once we cut over.
- The poller and heat watcher **cannot** run as a background task: Lambda
  freezes the execution environment between invocations. Hence the schedule.

- [x] Externalize process-local state (`store.py`): `DocumentStore` with
      memory/file/DynamoDB backends, `to_doc`/`load_doc` on `StateCache` and
      `HeatWatcher`, `SubscriptionStore` reading through the store. File
      backend keeps local dev unchanged; `STATE_TABLE` alone flips on DynamoDB.
      Documents are stored as JSON *strings* — DynamoDB has no float type and a
      marshalled map would hand back `Decimal` epoch timestamps.
- [x] `handlers.py`: `http_handler = Mangum(app, lifespan="off")` — lifespan
      MUST be off or the HTTP function starts a poller it cannot run — plus
      `poll_handler`, which routes on the event's `action` (`poll` | `safety`)
      so both schedules share one function.
- [x] Demand-driven refresh instead of a clock. `/api/status` calls
      `ensure_fresh_snapshot()`, polling inline when the snapshot has aged past
      `SNAPSHOT_MAX_AGE` (= the poller's 30s floor); the scheduled function
      only reaches upstream while a heat watch is active. Idle systems make no
      upstream calls; an open app drives ~2/min, same as the old loop.
      `request_refresh()` stays as-is — it's a harmless no-op without a loop,
      and the freshness check covers the Lambda case more cleanly.
- [x] `StateCache.last_snapshot_at` + `snapshot_age_seconds()`. Freshness could
      not be read off `last_success_at`, which actions bump without producing a
      snapshot — measuring from it would have served a pre-action snapshot
      forever. `is_stale()`/`age_seconds()` still answer the connectivity
      question for /api/health.
- [x] `sync_state` request dependency loads stored state before every request
      (a cold invocation's globals are empty) and saves after mutating ones.
- [x] Secrets in SSM Parameter Store (SecureString), not function env vars.
      `config.load_secrets()` copies every parameter under `SSM_PARAM_PATH`
      into `os.environ` at cold start, so the four existing env readers are
      untouched and local dev keeps using `.env` (no path set = no-op).
      Called from `handlers.py` *before* `import main` — main builds the VAPID
      config and the store at module scope, so a later fetch would leave push
      dead on every cold start. Upload with `just put-secrets`, which mirrors
      `.env` into SSM; deliberately a human action, never part of `sam deploy`,
      because regenerating `VAPID_PRIVATE_KEY` silently invalidates every push
      subscription and `SESSION_SECRET` logs everyone out. Note the template
      *cannot* do this: `{{resolve:ssm-secure:...}}` is unsupported on Lambda
      env vars, and the plaintext form puts the value back in the function
      config.
- [~] `template.yaml` written (SAM). Covers, but has **not yet been
      deployed**: timeout 30s / memory 512 MB, DynamoDB + SSM policies per
      function, reserved concurrency 5 (HTTP) and 1 (poll), both EventBridge
      schedules, 30-day log retention, and a `Retain` policy on the state
      table so a stack delete can't take every push subscription with it.
      Blocked on the build artifact: `CodeUri: build/` has nothing to point at
      yet, and `main._client_dist` resolves `parents[2]/client/dist`, which
      lands outside `/var/task` in a zip layout — decide the artifact layout
      before the first `sam deploy`. SAM CLI is not installed locally.
- [ ] Flip the session cookie to `https_only=True` behind TLS; add an
      unauthenticated `/api/ping` if a health check needs one
- [ ] Cost writeup in README

# Phase 5 — "Ready by time" with a learned heating model [ ]

Predict heating duration so the user sets "ready by 10am" and the backend
starts in time.

- [ ] Log heating sessions (start temp, target, air temp, time-to-target, zone)
      — needs real persistence; failure history is still bounded and in-memory
- [ ] Fixed-rate estimate first (degrees ÷ measured rate). Spa is small, fast,
      forgiving; pool is slow and high-stakes, and benefits most from
      correction.
- [ ] Polling-correction loop: estimate, then watch actual temp and adjust
- [ ] Self-calibrating rate from logged sessions; optionally factor air temp
- [ ] Session timer UX: "spa for N minutes" with extend; backend holds the
      shutoff timestamp. Nightly safety stays the backstop.
- [ ] Tests for the scheduling/estimation math

## Backlog

- Aux controls: lights + bubbles on/off (needs the `aux_N` mapping)
- Color/effect light control (model-dependent)
- Per-category backoff/alerting (deferred until real failures are observed)

## Open questions — resolve against real hardware

- Exact temp device key names (assumed `air_temp` / `pool_temp` / `spa_temp`)
- `aux_N` → light / bubble / waterfall mapping. Depends on how the installer
  wired and labeled the relays; discover by inspecting `status` output.
- What exception types the library actually raises, to tighten `classify()`
