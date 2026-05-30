# hydro-script

A Python CLI for automating a spa/hot tub via a Jandy iAquaLink controller. Handles the proper sequencing for startup (spa mode → valve settle → heater on) and shutdown (heater off → purge → spa mode off), and includes a safety command designed to run as a nightly cron job.

## Requirements

- Python 3.8+
- A Jandy iAquaLink system with an active account

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install iaqualink
```

Set your credentials as environment variables:

```bash
# Windows
set IAQUALINK_USER=you@example.com
set IAQUALINK_PASS=yourpassword

# macOS/Linux / .env file
export IAQUALINK_USER=you@example.com
export IAQUALINK_PASS=yourpassword
```

## Usage

```bash
python controls.py on        # enable spa mode, set temp, wait for valves, turn on heater
python controls.py off       # turn off heater, purge heat, disable spa mode
python controls.py status    # print current state of spa pump, heater, and set point
python controls.py safety    # shut down only if spa is currently on (for cron)
```

### `safety` command — nightly cron job

The `safety` command is a no-op if the spa is already off, and runs a full shutdown if it was left on. Add it to cron to ensure the spa doesn't run overnight:

```cron
0 2 * * * /path/to/.venv/bin/python /path/to/controls.py safety >> /var/log/hydro-safety.log 2>&1
```

Note: the cron job runs on the Raspberry Pi (Linux). Develop on Windows using `.venv\Scripts\activate`, then deploy to the Pi where the Linux path above applies.

## Configuration

Edit the constants at the top of [controls.py](controls.py):

`VALVE_DELAY`: Seconds to wait for valves and flow to establish before firing the heater on startup. Default 90s — **verify after first live test.** AquaLink may stage the spa-on sequence internally, in which case this can be reduced or set to 0.
`COOLDOWN_DELAY`: Seconds to keep the pump running after the heater turns off, to purge the heat exchanger before stopping flow. Default 15 minutes (900s) — the software equivalent of a fireman's switch. Do not reduce without live testing.
`SPA_SET_POINT`: Target temperature in °F. Set to `None` to skip.
`SPA_DEVICE`: Device key for the spa mode toggle
`SPA_HEATER`: Device key for the heater
`SPA_SETPOINT_DEV`: Device key for the temperature set point

If you're unsure of the device key names on your system, run `status` first — it prints all available device keys.
