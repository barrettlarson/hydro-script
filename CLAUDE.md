# CLAUDE.md — hydro-script

Context and roadmap for working on this project across sessions. Read this first.

## What this is

A full-stack pool/spa automation system for a Jandy **iAquaLink** controller.
The bar is engineering rigor (tests, typed code, documented architecture, thoughtful
failure handling), not just "it works."

Owner: Barrett Larson. Repo: `barrettlarson/hydro-script`.

## Critical architecture fact (drives everything)

iAquaLink has **no local API**. The controller is operated through Jandy's
cloud service (`iaqualink-api.realtime.io`) over HTTPS, authenticated with
account credentials + system serial. The `iaqualink` Python library
(`flz/iaqualink-py`) wraps this.

Consequences:

- Code can run **anywhere with internet** — a cloud server reaches Jandy
  exactly as well as a device at the house. There is no home-network
  dependency to design around.
- "Control it away from home" is therefore **free** once the backend is
  internet-reachable — not a feature to build.
- The library already handles **429 backoff (httpx-retries, Retry-After)** and
  **401 auth replay** internally. Our error taxonomy is still useful for
  observability, but expect fewer raw rate-limit errors to reach our layer.
- Everything is **cloud-polled** (~15s in Home Assistant's equivalent). Water
  temps change slowly; polling Jandy more than ~once/30s buys nothing and
  risks rate limits. This is why the backend caches (see below).

## Hardware / account specifics to confirm on real system

- Device keys are discovered via the `status` command. Confirmed working.
- **Aux circuits** (`aux_1`, `aux_2`, ...) map to lights / bubbles (blower) /
  waterfalls etc., but the number→device mapping depends on how the installer
  wired and labeled relays. Discover by inspecting `status` output. Library
  exposes `.turn_on()/.turn_off()/.toggle()`; color/effect control is
  model-dependent and more involved than on/off.
- Temp device keys assumed `air_temp` / `pool_temp` / `spa_temp` — verify
  against real `status` output and adjust constants if different.

## Design principles for this codebase

1. **Logic/interface separation.** Pure logic in `controls.py` (raises
   exceptions, returns values, no print/exit/argv). CLI and FastAPI both import
   it. This is what makes the logic testable.
2. **Cache decouples clients from upstream.** A single background poller
   (`poller.py`) polls Jandy ~once/30s; HTTP reads (`/api/status`) read an
   in-memory cache. Client count never multiplies upstream load.
3. **Failures are categorized, not stringified.** (target) Typed error
   taxonomy with classifier, timestamped records, bounded history. Track +
   classify now; build _reactions_ (backoff, alerts) later once real failures
   have been observed. Don't over-fit exception mapping to guesses.
4. **Time stored as epoch floats internally; ISO strings only at the API edge.**
5. **Tests use fake devices** — no hardware needed. Delays monkeypatched out
   so suite runs in <1s.
6. Keep responses/code reviewable: small modules, type hints, docstrings that
   explain _why_.

## Project structure

```
server/
  app/
    __init__.py
    aqualink.py   # connection helper (credentials, open_devices, require)
    controls.py   # pure logic: spa/pool on-off, status read (incl. temps), safety
    cli.py        # thin CLI wrapper (print/exit/argv)
    main.py       # FastAPI: action endpoints, status, health; serves client/dist
    poller.py     # single background poll loop feeding the StateCache
    errors.py     # error taxonomy: classify(exc) -> FailureCategory
    cache.py      # StateCache: snapshot, staleness, failure history
  tests/          # fake-device tests (controls, cache, errors, poller)
client/           # React + TypeScript PWA (Vite): mobile-first UI over the API
  src/
    api.ts             # typed API client (same-origin /api)
    usePolledState.ts  # polls status+health every 15s, pauses when hidden
    App.tsx            # temps, spa/pool cards, health indicator
CLAUDE.md
README.md
justfile              # task runner (cross-platform)
pyproject.toml        # project metadata + deps, managed by uv
uv.lock               # lockfile for reproducible installs
.env                  # IAQUALINK_USER / IAQUALINK_PASS (gitignored, never commit)
```

## Commands

```
just spa-on / spa-off / pool-on / pool-off / status / safety
just server                # FastAPI dev server (uvicorn --reload, :8000)
just client                # Vite dev server (:5173, proxies /api to :8000)
just client-build          # production client build into client/dist
just client-test           # vitest (jsdom)
just client-e2e            # Playwright (mocked /api, needs chromium installed)
just check                 # ruff + mypy + pytest
PYTHONPATH=server python -m app.cli [spa-on|spa-off|pool-on|pool-off|status|safety]
```

Cron (deploy target, Linux): nightly safety shutoff

```
0 2 * * * PYTHONPATH=server <python> -m app.cli safety >> <log> 2>&1
```

---

# Roadmap

Status legend: [x] done · [~] in progress · [ ] not started

## Phase 0 — Core scripting [x]

- [x] spa-on / spa-off with valve + cooldown sequencing
- [x] pool-on / pool-off with spa/pool mutual exclusion (shared heater)
- [x] status (device discovery + state read)
- [x] safety command (nightly, idempotent, tolerates partial system)

## Phase 1 — Rigor + API foundation [~]

- [x] Logic/interface refactor (controls.py pure logic, separate cli.py thin wrapper)
- [x] Connection helper module (aqualink.py — credentials, open_devices)
- [x] Unit tests for control logic (sequencing, exclusion, safety, partial)
- [x] FastAPI app with action endpoints (caching poll layer stubbed for later)
- [x] Error taxonomy + StateCache observability (health surface, history)
- [x] Tests for classifier + cache behavior
- [x] Switch to uv for dependency management (pyproject.toml + uv.lock)
- [x] Decide: action endpoints sync vs. background task + poll-for-result.
      Resolved: sync. The valve delay was removed (AquaLink stages valve
      actuation internally), so actions return quickly and sync is fine.
- [x] justfile recipes for new module paths (cli, uvicorn, pytest)
- [x] GitHub Actions CI: run pytest + ruff + mypy on push
- [x] README with architecture diagram (Jandy cloud → poller → cache → API → UI;
      drawn as a blueprint — built paths solid, poller/cache/client marked planned)

## Phase 1.5 — Background poller + cache read path [x]

Built the decoupling layer *before* the web client, so the
first real multi-client load lands on a cache instead of multiplying per-request
upstream calls. A single poller (`poller.py`) is now the only thing that polls
upstream; `/api/status` is served from the `StateCache`.

- [x] Background poller: one async loop started on FastAPI lifespan that polls
      Jandy every ~30s and writes snapshots into the shared `StateCache`
- [x] Serve reads from cache: `/api/status` returns the cached snapshot
      (503 "warming up" before the first successful poll) — no per-request conn
- [x] Route poller failures through the existing `classify()` /
      `record_failure()` path so health/staleness already surfaces them
- [x] Graceful start/stop of the poll task (lifespan startup + shutdown)
- [x] No overlapping polls (single sequential loop); ~30s floor between polls
- [x] Actions still go live (commands aren't cached); after an action, trigger a
      refresh poll so the cache reflects the change quickly
- [x] Tests: poller writes cache, reads served without an upstream call, failure
      classification, action-triggered refresh, lifecycle (fake fetch injected)

## Phase 2 — Web application [~]

- [x] React + TypeScript frontend (client/, Vite; `just client` to run)
- [x] Read path first: poll /api/status, render temps + on/off states + health
      (temps added to the status payload: `temps.air/pool/spa`, null when the
      sensor is absent or reads empty)
- [x] On/off controls for spa + pool wired to action endpoints
- [x] Handle action delay in UX: actions are sync but fast now (valve delay
      removed — AquaLink stages valves internally); button shows a spinner and
      the app re-polls after the action
- [x] Health/staleness indicator in UI (Live/Stale/Offline from /api/health)
- [x] No CORS anywhere: Vite dev proxy for /api; in prod FastAPI serves
      client/dist same-origin
- [x] Target-temp sliders for spa + pool: POST /api/spa/temp + /api/pool/temp,
      bounds-checked against system-specific SPA/POOL_SETPOINT_RANGE (served in
      /api/status so sliders and server agree). Wet-hands UX: slider commits
      once on release; +/− steppers debounce-coalesce taps into one request.
- [x] Filter pump (pool_pump) toggle: POST /api/pump/on|off, CLI pump-on/off,
      shown in status. Safety still never touches the pump.
- [ ] Aux controls: lights + bubbles (blower) on/off — confirm aux_N mapping
      from status first. Color/effects deferred (model-dependent).
- [x] PWA (add-to-home-screen) so family installs without an app store
      (vite-plugin-pwa; app shell precached, /api never cached by the SW)
- [x] Frontend tests: vitest + testing-library (client/src/test/) and
      Playwright e2e with /api mocked in-browser (client/e2e/, mobile +
      desktop projects). Backend endpoint tests in server/tests/test_main.py
      (fake connection layer). All wired into CI.

## Phase 2.5 — Auth / login (gate before exposing actions)

- [ ] Login UI in the frontend (Phase 2) + auth state handling
- [ ] Backend: session/token issuance; protect all action + state endpoints
- [ ] Credential provider returns the server-side `.env` cred for all sessions
- [ ] Keep the 2 AM safety cron working without an interactive login (it uses
      the server-side cred directly, independent of any user session)
- [ ] Tests for auth gating (unauthenticated request is rejected)

## Phase 3 — SMS notifications [ ]

Notify when pool/spa reaches target temperature.

- [ ] Background watcher: compare current temp vs. set point in the poll loop
- [ ] Hysteresis / one-shot latch so it doesn't re-fire while hovering at target
- [ ] SMS delivery (AWS SNS is the natural fit if deploying on AWS; evaluate
      cost + phone-number/verification requirements)
- [ ] Per-recipient opt-in / phone-number config (family members)
- [ ] Tests for the crossing-detection + latch logic (no real SMS)

## Phase 4 — Deploy v1 (AWS) [ ]

AWS is viable because iAquaLink is cloud-only (see architecture fact).

- [ ] Decide compute: small always-on (e.g. lightweight container/VM) vs.
      serverless. NOTE: the background poller + temp watcher need an
      always-running process or a scheduled invocation — pure request/response
      serverless doesn't poll on its own. Likely a small always-on container
      (e.g. ECS Fargate / Lightsail / a t-class instance) OR EventBridge-driven
      scheduled Lambda for polling + a separate path for actions. Evaluate cost.
- [ ] Secrets: credentials in AWS Secrets Manager / SSM, not env files in image
- [ ] Nightly safety: EventBridge schedule instead of cron
- [ ] HTTPS + enforce the Phase 2.5 auth gate on the deployed API (don't expose
      actions openly)
- [ ] Keep the 2 AM safety as an independent failsafe regardless of session logic
- [ ] Cost writeup in README

## Phase 5 — v2.0 "Ready-by-time" with learned heating model [ ]

Predict heating duration so user sets "ready by 10am" and backend starts in time.

- [ ] Log heating sessions: (start temp, target, air temp, time-to-target,
      pool vs spa, any other available signals) — needs persistence (e.g.
      SQLite/RDS); current error history is in-memory only.
- [ ] Fixed-rate estimate first (degrees ÷ measured rate). Spa is small/fast and
      forgiving; pool is slow/high-stakes and benefits most from correction.
- [ ] Polling-correction loop: estimate start, then watch actual temp and adjust.
- [ ] Self-calibrating rate from logged sessions; optionally factor air temp.
- [ ] Session timer UX: "spa for N minutes" with extend; backend holds shutoff
      timestamp, extend pushes it later. Keep cron safety as backstop.
- [ ] Tests for the scheduling/estimation math.

## Backlog / undecided

- Color/effect light control (model-dependent)
- Failure-trend persistence across restarts (currently in-memory)
- Per-category backoff/alerting (deferred until real failures observed)

## Open questions to resolve on real hardware

- Exact temp device key names (air/pool/spa)
- aux_N → light/bubble/waterfall mapping
- What real failure exception types the library raises (to tighten classify())
