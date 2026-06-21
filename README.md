# hydro-script

A full-stack pool/spa automation system for a Jandy **iAquaLink** controller.
It exposes the same control logic two ways — a thin **CLI** for scripting and
cron, and a **FastAPI** HTTP API for a (forthcoming) web/mobile frontend — and
puts engineering rigor (typed code, tests, an error taxonomy, and observability)
around the controller.

It handles the correct sequencing for startup (spa mode → valve settle →
heater on) and shutdown (heater off → purge → spa mode off), enforces spa/pool
mutual exclusion on the shared heater, and ships a safety command designed to
run as a nightly cron job.

## Architecture

iAquaLink has **no local API**. The controller is operated entirely through
Jandy's cloud service over HTTPS, authenticated with account credentials + the
system serial. The [`iaqualink`](https://github.com/flz/iaqualink-py) library
wraps that cloud API. Two consequences shape the design:

- The code can run **anywhere with internet** — a cloud server reaches Jandy
  exactly as well as a box on the home network. "Control it away from home" is
  free, not a feature to build.
- Everything is **cloud-polled** and water temps change slowly, so polling more
  than ~once/30s buys nothing and risks rate limits. The design is for a single
  background poller to cache upstream state so client count never multiplies
  upstream load. The `StateCache` exists today for observability (health,
  staleness, failure history); the poller that feeds it and the read-from-cache
  path are the **next build** — see the roadmap.

### Data flow (blueprint)

Solid boxes/arrows are built today; `┄┄` and `[planned]` mark what the roadmap
schedules next. Reads are served live today and move behind the cache once the
poller lands; write/command paths always go live to Jandy.

```
Legend:   ──►  built today          ┄►  planned (see roadmap)

                       ┌───────────────────────────────┐
                       │       Jandy iAquaLink cloud   │
                       │  HTTPS · no local API · rate- │
                       │            limited            │
                       └───────────────────────────────┘
                         ▲            ▲              ▲
                commands │   commands │       poll ┄┘ ~30s  [planned]
                         │            │              ┊
                   ┌─────┴────┐ ┌─────┴────┐ ┌───────┴───────┐
                   │  cli.py  │ │ main.py  │ │    poller     │  [planned]
                   │  cron /  │ │ FastAPI  │ │  background  │
                   │ scripts  │ │ actions  │ │     loop     │
                   └─────┬────┘ └─────┬────┘ └───────┬───────┘
                         │            │              ┊ writes snapshot
                         └─────┬──────┘              ▼
                               ▼              ┌───────────────┐
                        ┌────────────┐        │   StateCache  │
                        │ controls.py│        │  snapshot +   │
                        │ pure logic │        │  health       │
                        └────────────┘        └───────┬───────┘
                              ▲                       │ read
                              │ aqualink.py           │
                       (credentials, open_devices)    │
                                                      │
                          GET /api/status ┄┄┄┄┄┄┄┄┄┄┄┄┤ (live today;
                          GET /api/health ────────────┤  from cache once
                                                      │  poller lands)
                                                      ▼
                                             ┌──────────────────┐
                                             │  React client    │  [planned]
                                             │    (client/)     │
                                             └──────────────────┘
```

The codebase is organized around a strict **logic/interface separation**:

```
server/
  app/
    aqualink.py   # connection helper: credentials, open_devices, typed errors
    controls.py   # pure logic — spa/pool on-off, status, safety. No print/exit/argv.
    cli.py        # thin CLI wrapper (print/exit/argv) over controls
    main.py       # FastAPI app: action + status + health endpoints
    errors.py     # error taxonomy: classify(exc) -> FailureCategory, HTTP mapping
    cache.py      # StateCache: last snapshot, staleness, bounded failure history
  tests/          # fake-device tests; no hardware required
client/           # (future) React + TypeScript frontend
justfile          # task runner (cross-platform)
pyproject.toml    # deps + metadata, managed by uv
```

`controls.py` raises exceptions and returns values; both the CLI and the API
import it. That separation is what makes the control logic unit-testable
without hardware. See [CLAUDE.md](CLAUDE.md) for the full design rationale and
roadmap.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (dependency management)
- A Jandy iAquaLink system with an active account
- [just](https://github.com/casey/just) (optional, for shorthand commands)

## Setup

```bash
uv sync              # install runtime deps
uv sync --all-extras # include dev tools (pytest, mypy, ruff)
```

Create a `.env` file in the project root with your credentials (gitignored —
never commit):

```
IAQUALINK_USER=you@example.com
IAQUALINK_PASS=yourpassword
```

## CLI usage

With `just` installed:

```bash
just spa-on     # enable spa mode, set temp, wait for valves, turn on heater
just spa-off    # turn off heater, purge heat exchanger, disable spa mode
just pool-on    # turn on pool heater
just pool-off   # turn off pool heater
just status     # print current state + all discovered device keys
just safety     # shut down only if spa/pool is currently on (for cron)
```

Or directly via the module (the CLI lives in `app.cli`, with `PYTHONPATH=server`):

```bash
PYTHONPATH=server python -m app.cli spa-on
PYTHONPATH=server python -m app.cli status
# ... spa-off | pool-on | pool-off | safety
```

Install `just` on Windows: `winget install Casey.Just`

### `safety` command — nightly cron job

The `safety` command is a no-op if the spa and pool are already off, and runs a
full shutdown if either was left on. It tolerates a partially reachable system.
Add it to cron so the spa can't run overnight:

```cron
0 2 * * * PYTHONPATH=server /path/to/.venv/bin/python -m app.cli safety >> /var/log/hydro-safety.log 2>&1
```

## API server

Run the FastAPI dev server:

```bash
just dev
# or: PYTHONPATH=server uvicorn app.main:app --reload --app-dir server
```

Endpoints:

| Method | Path            | Description                                                |
| ------ | --------------- | ---------------------------------------------------------- |
| GET    | `/`             | Liveness check                                             |
| GET    | `/api/status`   | Current device state + all device keys                     |
| GET    | `/api/health`   | Observability: cache freshness, staleness, recent failures |
| POST   | `/api/spa/on`   | Spa startup sequence                                       |
| POST   | `/api/spa/off`  | Spa shutdown sequence                                      |
| POST   | `/api/pool/on`  | Pool heater on                                             |
| POST   | `/api/pool/off` | Pool heater off                                            |
| POST   | `/api/safety`   | Idempotent safety shutdown                                 |

Action endpoints currently run **synchronously** and block during the valve
delay; the background poller and a poll-for-result flow are planned (see the
roadmap). Failures are run through the error taxonomy in `errors.py`: each is
classified into a `FailureCategory` (auth / rate_limit / upstream_offline /
network / config / unknown), recorded in the `StateCache` with its real
message for debugging, and returned to the caller as a generic,
category-appropriate HTTP error.

## Development

```bash
just test          # pytest (fake devices, no hardware needed)
just lint          # ruff check
just format        # ruff format
just typecheck     # mypy
just check         # lint + format-check + typecheck + test
```

CI runs the same checks (ruff, mypy, pytest) on push — see
[.github/workflows/ci.yml](.github/workflows/ci.yml).

## Configuration

Edit the constants at the top of [server/app/controls.py](server/app/controls.py):

- `VALVE_DELAY`: Seconds to wait for valves and flow to establish before firing
  the heater on startup.
- `COOLDOWN_DELAY`: Seconds to keep the pump running after the heater turns off,
  to purge the heat exchanger before stopping flow.
- `SPA_SET_POINT`: Target spa temperature in °F. Set to `None` to skip.
- `SPA_DEVICE`: Device key for the spa mode toggle.
- `SPA_HEATER`: Device key for the spa heater.
- `SPA_SETPOINT_DEV`: Device key for the temperature set point.
- `POOL_SET_POINT`: Target pool temperature in °F. Set to `None` to skip.
- `POOL_HEATER`: Device key for the pool heater.
- `POOL_SETPOINT_DEV`: Device key for the pool temperature set point.

If you're unsure of the device key names on your system, run `status` first —
it prints all available device keys.
