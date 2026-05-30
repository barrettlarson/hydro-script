set dotenv-load
set shell := ["powershell.exe", "-NoLogo", "-Command"]

python := if os() == "windows" { ".venv\\Scripts\\python" } else { ".venv/bin/python" }

default:
    @just --list

spa-on:
    {{python}} controls.py spa-on

spa-off:
    {{python}} controls.py spa-off

pool-on:
    {{python}} controls.py pool-on

pool-off:
    {{python}} controls.py pool-off

status:
    {{python}} controls.py status

safety:
    {{python}} controls.py safety
