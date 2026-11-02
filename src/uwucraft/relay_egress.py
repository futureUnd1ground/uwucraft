"""Root-owned stdlib-only helper: one physical-interface relay TCP exception."""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def output_plan(document, address, device):
    ip = ipaddress.IPv4Address(address)
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,32}", device)
    ):
        raise ValueError("Invalid relay IP/interface")
    commands = []
    tag = f"uwucraft-relay-egress-{ip}"
    entries = document.get("nftables", [])
    for entry in entries:
        chain = entry.get("chain", {})
        if chain.get("family") not in {"ip", "inet"} or chain.get("hook") != "output":
            continue
        identity = {
            "family": chain["family"],
            "table": chain["table"],
            "chain": chain["name"],
        }
        rules = [
            e["rule"]
            for e in entries
            if "rule" in e and all(e["rule"].get(k) == v for k, v in identity.items())
        ]
        expression = [
            {
                "match": {
                    "op": "==",
                    "left": {"meta": {"key": "oifname"}},
                    "right": device,
                }
            },
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                    "right": str(ip),
                }
            },
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                    "right": 7835,
                }
            },
            {"accept": None},
        ]
        tagged = [r for r in rules if r.get("comment") == tag]
        if (
            len(tagged) == 1
            and rules[0] == tagged[0]
            and tagged[0]["expr"] == expression
        ):
            continue
        commands.extend(
            {"delete": {"rule": {**identity, "handle": r["handle"]}}} for r in tagged
        )
        commands.append(
            {"insert": {"rule": {**identity, "comment": tag, "expr": expression}}}
        )
    return {"nftables": commands}


def execute(args, **kwargs):
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=30, **kwargs
    )


def apply(address, device):
    if not shutil.which("nft"):
        return
    rules = json.loads(execute(["nft", "-j", "list", "ruleset"]).stdout)
    transaction = output_plan(rules, address, device)
    if transaction["nftables"]:
        execute(["nft", "-j", "-f", "-"], input=json.dumps(transaction))


def install(address, device):
    if os.geteuid() != 0:
        raise PermissionError("Root permissions required for relay firewall exception")
    output_plan({}, address, device)
    folder = Path("/etc/uwucraft")
    folder.mkdir(mode=0o755, exist_ok=True)
    if (
        folder.is_symlink()
        or folder.stat().st_uid != 0
        or folder.stat().st_mode & 0o022
    ):
        raise PermissionError("Unsafe root helper directory")
    target = folder / f"relay-egress-{address}.py"
    if target.is_symlink():
        raise PermissionError("Unsafe root helper path")
    target.write_bytes(Path(__file__).read_bytes())
    target.chmod(0o644)
    name = f"uwucraft-relay-egress-{address}"
    units = {
        name + ".service": f"""[Unit]
Description=uwucraft narrow physical relay egress exception
After=network-online.target nftables.service AmneziaVPN.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {target} --apply --ip {address} --device {device}
""",
        name + ".timer": f"""[Unit]
Description=uwucraft restore relay exception after VPN firewall changes
[Timer]
OnBootSec=15
OnUnitActiveSec=30
Unit={name}.service
[Install]
WantedBy=timers.target
""",
    }
    for filename, data in units.items():
        destination = Path("/etc/systemd/system") / filename
        if destination.is_symlink():
            raise PermissionError("Unsafe unit path")
        destination.write_text(data)
        destination.chmod(0o644)
    execute(["systemctl", "daemon-reload"])
    execute(["systemctl", "enable", "--now", name + ".timer"])
    apply(address, device)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--apply", action="store_true")
    options = parser.parse_args()
    if options.apply:
        apply(options.ip, options.device)
    else:
        install(options.ip, options.device)
