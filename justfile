set dotenv-load
set shell := ["powershell.exe", "-NoLogo", "-Command"]

python := if os() == "windows" { ".venv\\Scripts\\python" } else { ".venv/bin/python" }

export PYTHONPATH := "server"

default:
    @just --list

# dev

test *args:
    {{python}} -m pytest server/tests {{args}}

lint:
    {{python}} -m ruff check server/

format:
    {{python}} -m ruff format server/

format-check:
    {{python}} -m ruff format --check server/

typecheck:
    {{python}} -m mypy server/app/

check: lint format-check typecheck test

dev:
    {{python}} -m uvicorn app.main:app --app-dir server --reload

# controls

spa-on:
    {{python}} -m app.cli spa-on

spa-off:
    {{python}} -m app.cli spa-off

pool-on:
    {{python}} -m app.cli pool-on

pool-off:
    {{python}} -m app.cli pool-off

status:
    {{python}} -m app.cli status

safety:
    {{python}} -m app.cli safety
