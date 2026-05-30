set dotenv-load
set shell := ["powershell.exe", "-NoLogo", "-Command"]

python := if os() == "windows" { ".venv\\Scripts\\python" } else { ".venv/bin/python" }

default:
    @just --list

on:
    {{python}} controls.py on

off:
    {{python}} controls.py off

status:
    {{python}} controls.py status

safety:
    {{python}} controls.py safety
