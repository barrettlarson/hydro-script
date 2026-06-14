set dotenv-load
set shell := ["powershell.exe", "-NoLogo", "-Command"]

python := if os() == "windows" { ".venv\\Scripts\\python" } else { ".venv/bin/python" }

export PYTHONPATH := "server"

default:
    @just --list

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
