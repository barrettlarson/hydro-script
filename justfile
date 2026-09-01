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

server:
    {{python}} -m uvicorn app.main:app --app-dir server --reload

# generate a VAPID keypair for Web Push (paste the private key into .env)
vapid-keys:
    {{python}} -m app.push

# aws

# upload .env secrets to SSM as SecureStrings (pass -DryRun to preview)
put-secrets *args:
    powershell -NoProfile -File scripts/Put-Secrets.ps1 {{args}}

# names and metadata of what is stored — never the values
show-secrets:
    aws ssm get-parameters-by-path --path /hydro-script/prod --region us-east-1 --query "Parameters[].[Name,Type,Version,LastModifiedDate]" --output table

# docker (serves the production client bundle at :8000)

up:
    docker compose up -d --build
    Start-Process http://localhost:8000

down:
    docker compose down

# frontend (client/)

client:
    cd client; npm run dev

client-build:
    cd client; npm run build

client-lint:
    cd client; npm run lint

client-test:
    cd client; npm run test

client-e2e:
    cd client; npm run e2e

# controls

spa-on:
    {{python}} -m app.cli spa-on

spa-off:
    {{python}} -m app.cli spa-off

pool-on:
    {{python}} -m app.cli pool-on

pool-off:
    {{python}} -m app.cli pool-off

pump-on:
    {{python}} -m app.cli pump-on

pump-off:
    {{python}} -m app.cli pump-off

status:
    {{python}} -m app.cli status

safety:
    {{python}} -m app.cli safety
