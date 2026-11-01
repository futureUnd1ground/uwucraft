"""Minimal root helper; only adds one TCP exception to existing IPv4 input hooks.

This file is copied root-owned before a system service executes it. It must
remain stdlib-only and must never import a user-writable application package.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class FirewallError(Exception):
    pass


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=30, **kwargs)


def exception_plan(ruleset, port):
    """A separate accept chain cannot override a later input-chain drop."""
    entries = ruleset.get("nftables", [])
    chains = [
        entry["chain"]
        for entry in entries
        if "chain" in entry
        and entry["chain"].get("family") in {"ip", "inet"}
        and entry["chain"].get("hook") == "input"
    ]
    commands = []
    for chain in chains:
        identity = {key: chain[key] for key in ("family", "table", "name")}
        rules = [
            entry["rule"]
            for entry in entries
            if "rule" in entry
            and all(
                entry["rule"].get(key if key != "name" else "chain") == value
                for key, value in identity.items()
            )
        ]
        tag = f"uwucraft-tcp-{port}"
        existing = next((rule for rule in rules if rule.get("comment") == tag), None)
        if existing:
            # Reinsert at the head if rules were subsequently inserted before it.
            if rules and rules[0].get("handle") == existing.get("handle"):
                continue
            commands.append(
                {
                    "delete": {
                        "rule": {
                            "family": chain["family"],
                            "table": chain["table"],
                            "chain": chain["name"],
                            "handle": existing["handle"],
                        }
                    }
                }
            )
        commands.append(
            {
                "insert": {
                    "rule": {
                        "family": chain["family"],
                        "table": chain["table"],
                        "chain": chain["name"],
                        "comment": tag,
                        "expr": [
                            {
                                "match": {
                                    "op": "==",
                                    "left": {
                                        "payload": {"protocol": "tcp", "field": "dport"}
                                    },
                                    "right": port,
                                }
                            },
                            {"accept": None},
                        ],
                    }
                }
            }
        )
    for command in commands:
        if "insert" in command and command["insert"]["rule"]["family"] == "inet":
            command["insert"]["rule"]["expr"].insert(
                0,
                {
                    "match": {
                        "op": "==",
                        "left": {"meta": {"key": "nfproto"}},
                        "right": "ipv4",
                    }
                },
            )
    return {"nftables": commands}, len(chains)


def apply(port):
    if not shutil.which("nft"):
        active = run(["systemctl", "is-active", "nftables.service"])
        if active.returncode == 0:
            raise FirewallError(
                "nftables активен, но команда nft отсутствует: установите nftables."
            )
        return {"state": "inactive", "chains": 0}
    result = run(["nft", "-j", "-a", "list", "ruleset"])
    if result.returncode:
        raise FirewallError(
            "Не удалось прочитать ruleset nftables. Нужны права root/CAP_NET_ADMIN."
        )
    document, count = exception_plan(json.loads(result.stdout), port)
    if document["nftables"]:
        result = run(["nft", "-j", "-f", "-"], input=json.dumps(document))
        if result.returncode:
            raise FirewallError(
                "nftables отклонил TCP-исключение: " + result.stderr.strip()[:500]
            )
    return {"state": "configured" if count else "inactive", "chains": count}


def install(port):
    if os.geteuid() != 0:
        raise FirewallError(
            "Для nftables нужны права администратора. Выполните sudo -v перед созданием сервера."
        )
    report = apply(port)
    if report["state"] == "inactive":
        return report
    # Copy only these audited helper bytes into root-owned storage, never point
    # a root unit at a virtualenv or code under the user's home directory.
    folder = Path("/etc/uwucraft")
    folder.mkdir(mode=0o755, exist_ok=True)
    if (
        folder.is_symlink()
        or folder.stat().st_uid != 0
        or folder.stat().st_mode & 0o022
    ):
        raise FirewallError(
            "/etc/uwucraft должен принадлежать root и не быть доступным для записи другим пользователям."
        )
    target = folder / f"firewall-{port}.py"
    if target.is_symlink():
        raise FirewallError("Отклонена ссылка вместо root helper.")
    target.write_bytes(Path(__file__).read_bytes())
    target.chmod(0o644)
    unit = Path(f"/etc/systemd/system/uwucraft-firewall-{port}.service")
    if unit.is_symlink():
        raise FirewallError("Отклонена ссылка вместо firewall unit.")
    unit.write_text(f"""[Unit]
Description=uwucraft TCP {port} firewall exception
After=nftables.service firewalld.service
Before=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /etc/uwucraft/firewall-{port}.py --apply --port {port}
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
""")
    unit.chmod(0o644)
    for command in (["systemctl", "daemon-reload"], ["systemctl", "enable", unit.name]):
        if run(command).returncode:
            raise FirewallError(
                "TCP-правило добавлено, но не удалось включить его восстановление после перезагрузки."
            )
    report["persistent"] = True
    return report


def configure(port):
    """Called unprivileged. No hidden password prompt inside a running TUI."""
    if sys.platform == "win32":
        from uwucraft.windows import firewall

        return firewall(port)
    if not shutil.which("nft"):
        active = run(["systemctl", "is-active", "nftables.service"])
        if active.returncode != 0:
            return {"state": "inactive", "chains": 0}
    command = [sys.executable, str(Path(__file__).resolve()), "--port", str(port)]
    attempts = []
    if os.geteuid() != 0:
        # sudo -n first (no hidden prompt); pkexec falls back to the system
        # authentication agent when sudo has no cached credentials.
        attempts.append(["sudo", "-n", "--"] + command)
        if shutil.which("pkexec"):
            attempts.append(["pkexec"] + command)
    else:
        attempts.append(command)
    result = None
    for candidate in attempts:
        result = run(candidate)
        if not result.returncode:
            return json.loads(result.stdout)
    raise FirewallError(
        "Не удалось автоматически настроить nftables. Выполните sudo -v и повторите uwucraft network configure "
        "(пароль запрашивает sudo/pkexec). "
        + (result.stderr.strip()[:500] if result else "")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args()
    try:
        if not 1024 <= options.port <= 65535:
            raise FirewallError("Порт должен быть 1024–65535.")
        print(
            json.dumps(apply(options.port) if options.apply else install(options.port))
        )
    except (FirewallError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
