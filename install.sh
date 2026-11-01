#!/usr/bin/env bash
set -euo pipefail
# Run from a clone. Only a user-owned virtualenv is written; no sudo required.
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$root"
else
    command -v python3 >/dev/null || { echo 'Установите Python: sudo pacman -S python'; exit 1; }
    target="${XDG_DATA_HOME:-$HOME/.local/share}/uwucraft-app"
    python3 -m venv "$target/venv"
    "$target/venv/bin/python" -m pip install "$root"
    mkdir -p "$HOME/.local/bin"
    ln -sfn "$target/venv/bin/uwucraft" "$HOME/.local/bin/uwucraft"
fi
printf '\nГотово: uwucraft auto\nДобавьте ~/.local/bin в PATH, если требуется.\n'
