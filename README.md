# hydro-script

A Python CLI for automating a spa and pool via a Jandy iAquaLink controller. Handles the proper sequencing for startup (spa mode → valve settle → heater on) and shutdown (heater off → purge → spa mode off), and includes a safety command designed to run as a nightly cron job.

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

Create a `.env` file in the project root with your credentials:

```
IAQUALINK_USER=you@example.com
IAQUALINK_PASS=yourpassword
```

## Usage

With `just` installed:

```bash
just spa-on     # enable spa mode, set temp, wait for valves, turn on heater
just spa-off    # turn off heater, purge heat exchanger, disable spa mode
just pool-on    # turn on pool heater
just pool-off   # turn off pool heater
just status     # print current state of spa pump, heater, and set point
just safety     # shut down only if spa is currently on (for cron)
```

Or directly with Python:

```bash
python server/app/controls.py spa-on
python server/app/controls.py spa-off
python server/app/controls.py pool-on
python server/app/controls.py pool-off
python server/app/controls.py status
python server/app/controls.py safety
```

Install `just` on Windows: `winget install Casey.Just`

### `safety` command — nightly cron job

The `safety` command is a no-op if the spa and pool are already off, and runs a full shutdown if either one was left on. Add it to cron to ensure the spa doesn't run overnight:

```cron
0 2 * * * /path/to/.venv/bin/python /path/to/server/app/controls.py safety >> /var/log/hydro-safety.log 2>&1
```

## Configuration

Edit the constants at the top of [server/app/controls.py](server/app/controls.py):

- `VALVE_DELAY`: Seconds to wait for valves and flow to establish before firing the heater on startup.
- `COOLDOWN_DELAY`: Seconds to keep the pump running after the heater turns off, to purge the heat exchanger before stopping flow.
- `SPA_SET_POINT`: Target spa temperature in °F. Set to `None` to skip.
- `SPA_DEVICE`: Device key for the spa mode toggle.
- `SPA_HEATER`: Device key for the spa heater.
- `SPA_SETPOINT_DEV`: Device key for the temperature set point.
- `POOL_SET_POINT`: Target pool temperature in °F. Set to `None` to skip.
- `POOL_HEATER`: Device key for the pool heater.
- `POOL_SETPOINT_DEV`: Device key for the pool temperature set point.

If you're unsure of the device key names on your system, run `status` first — it prints all available device keys.
