set dotenv-load
set shell := ["powershell.exe", "-NoLogo", "-Command"]

python := if os() == "windows" { ".venv\\Scripts\\python" } else { ".venv/bin/python" }

default:
    @just --list

spa-on:
    {{python}} server/app/controls.py spa-on

spa-off:
    {{python}} server/app/controls.py spa-off

pool-on:
    {{python}} server/app/controls.py pool-on

pool-off:
    {{python}} server/app/controls.py pool-off

status:
    {{python}} server/app/controls.py status

safety:
    {{python}} server/app/controls.py safety
