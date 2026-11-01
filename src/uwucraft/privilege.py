"""Privilege escalation is delegated to system tools.

uwucraft never reads, requests or stores a user password: sudo and pkexec
authenticate the operator themselves. Elevated rights are used only for
package installation, the nftables exception and the linger setting — never
to run Minecraft or the TUI as root.
"""

import os
import shutil
import subprocess
import sys

from .config import Error

# The textual TUI sets this while it owns the terminal, so no interactive
# sudo prompt is ever drawn over a running TUI screen.
ui_active = False


def _root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def sudo_cached():
    """True when a following `sudo -n` would run without any prompt."""
    if _root() or not shutil.which("sudo"):
        return False
    try:
        result = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ensure_interactive(reason=""):
    """Make a later `sudo -n` succeed, letting sudo itself ask for the password.

    Returns an empty string on success (or when root rights are already
    available) and an actionable message otherwise. Never blocks the TUI:
    callers from the TUI suspend the app first and only invoke this when
    ui_active is set; without a usable terminal it returns guidance instead
    of prompting.
    """
    if _root() or sudo_cached():
        return ""
    if ui_active or not sys.stdin.isatty() or not shutil.which("sudo"):
        return (
            "Нужны права администратора. Выполните sudo -v в этом терминале "
            "и повторите операцию; аутентификацией занимается sudo."
        )
    label = "Требуются права администратора"
    if reason:
        label += " (" + reason + ")"
    print(label + ".\nВведите пароль пользователя в запросе sudo.", flush=True)
    try:
        result = subprocess.run(["sudo", "-v"], timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        return "Не удалось запросить права через sudo. Повторите операцию."
    if result.returncode == 0:
        return ""
    return "sudo не подтвердил права. Повторите операцию."


def ensure_via_system(app, reason=""):
    """Request elevated rights while a TUI is running.

    Suspends the textual app so sudo/pkexec can draw their own prompt on the
    real terminal; uwucraft never reads or stores the password. Falls back to
    actionable guidance when suspension is unavailable (headless driver,
    tests, unsupported terminal).
    """
    global ui_active
    try:
        with app.suspend():
            previous, ui_active = ui_active, False
            try:
                return ensure_interactive(reason)
            finally:
                ui_active = previous
    except Exception:
        return ensure_interactive(reason)


def elevated(command):
    """Prefix a one-shot root command: direct when root, sudo -n when cached,
    otherwise pkexec (its system agent authenticates). Raises Error otherwise."""
    if _root():
        return list(command)
    if sudo_cached():
        return ["sudo", "-n", "--", *command]
    if shutil.which("pkexec"):
        return ["pkexec", *command]
    raise Error(
        "Нужны права администратора. Выполните sudo -v и повторите операцию; "
        "пароль запрашивает системный инструмент, uwucraft его не хранит."
    )
