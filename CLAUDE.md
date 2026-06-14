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

1. **Logic/interface separation.** (target, not yet done) Pure logic in
   `controls.py` (raises exceptions, returns values, no print/exit/argv). CLI
   and FastAPI both import it. This is what makes the logic testable. Currently
   `controls.py` still mixes CLI concerns — refactor is Phase 1 work.
2. **Cache decouples clients from upstream.** (target) A single background
   poller polls Jandy; HTTP requests read an in-memory cache. Client count
   never multiplies upstream load.
3. **Failures are categorized, not stringified.** (target) Typed error
   taxonomy with classifier, timestamped records, bounded history. Track +
   classify now; build _reactions_ (backoff, alerts) later once real failures
   have been observed. Don't over-fit exception mapping to guesses.
4. **Time stored as epoch floats internally; ISO strings only at the API edge.**
5. **Tests use fake devices** — no hardware needed. Delays monkeypatched out
   so suite runs in <1s. (not yet implemented)
6. Keep responses/code reviewable: small modules, type hints, docstrings that
   explain _why_.

## Project structure

```
server/
  app/
    __init__.py
    controls.py   # all control logic + CLI entry point (not yet refactored)
    main.py       # FastAPI scaffold (partially started, not functional yet)
  tests/
    test_controls.py  # (empty, tests not yet written)
client/               # (future) React + TypeScript frontend
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
python server/app/controls.py [spa-on|spa-off|pool-on|pool-off|status|safety]
uvicorn app.main:app --reload      # dev server (not functional yet)
pytest                             # test suite (no tests yet)
```

Cron (deploy target, Linux): nightly safety shutoff

```
0 2 * * * <python> server/app/controls.py safety >> <log> 2>&1
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

- [ ] Logic/interface refactor (controls.py pure logic, separate cli.py thin wrapper)
- [ ] Connection helper module (aqualink.py — credentials, open_devices)
- [ ] Unit tests for control logic (sequencing, exclusion, safety, partial)
- [~] FastAPI app with caching poll layer (main.py scaffolded, not functional)
- [ ] Error taxonomy + StateCache observability (health surface, history)
- [ ] Tests for classifier + cache behavior
- [x] Switch to uv for dependency management (pyproject.toml + uv.lock)
- [ ] Decide: action endpoints sync (block during VALVE_DELAY) vs. background
      task + poll-for-result. Currently sync. Revisit when building frontend UX.
- [ ] justfile recipes for new module paths (cli, uvicorn, pytest)
- [ ] GitHub Actions CI: run pytest + lint on push (high portfolio signal)
- [ ] Type checking (mypy) + lint (ruff) in CI
- [ ] README with architecture diagram (Jandy cloud → poller → cache → API → UI)

## Phase 2 — Web application [ ]

- [ ] React + TypeScript frontend (client/)
- [ ] Read path first: poll /api/state, render temps + on/off states + health
- [ ] On/off controls for spa + pool wired to action endpoints
- [ ] Handle the sync-action delay in UX (loading state or optimistic + poll)
- [ ] Health/staleness indicator in UI (uses the observability surface)
- [ ] Aux controls: lights + bubbles (blower) on/off — confirm aux_N mapping
      from status first. Color/effects deferred (model-dependent).
- [ ] PWA (add-to-home-screen) so family installs without an app store
- [ ] Frontend tests

## Phase 3 — SMS notifications [ ]

Notify when pool/spa reaches target temperature.

- [ ] Background watcher: compare current temp vs. set point in the poll loop
- [ ] Hysteresis / one-shot latch so it doesn't re-fire while hovering at target
- [ ] SMS delivery (AWS SNS is the natural fit if deploying on AWS; evaluate
      cost + phone-number/verification requirements)
- [ ] Per-user opt-in / recipient config
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
- [ ] HTTPS + auth on the API (family-only access; don't expose actions openly)
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
- Multi-user accounts / auth model beyond simple family access

## Open questions to resolve on real hardware

- Exact temp device key names (air/pool/spa)
- aux_N → light/bubble/waterfall mapping
- Whether VALVE_DELAY is reducible (does AquaLink stage spa-on internally?)
- What real failure exception types the library raises (to tighten classify())
