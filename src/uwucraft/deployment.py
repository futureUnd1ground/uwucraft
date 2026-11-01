"""Direct IPv4 publication; configuration failures never masquerade as success."""

import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import firewall, network, portmap, server
from .config import Error, Paths, atomic


def disable_legacy_service(paths=None):
    # Migration of the previous release's optional networking unit. Never delete
    # world files or modify an unrelated system service.
    unit = Path.home() / ".config/systemd/user/uwucraft-tunnel.service"
    if unit.exists() and "Description=uwucraft" in unit.read_text():
        result = subprocess.run(
            ["systemctl", "--user", "disable", "--now", unit.name],
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise Error(
                "Не удалось отключить устаревший сетевой сервис uwucraft. Проверьте systemctl --user."
            )
        unit.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    paths = paths or Paths()
    for file in [
        paths.data / "tunnel.json",
        paths.config / "tunnel-host.txt",
        paths.data / "tools/bore",
        paths.data / "tools/playit",
        paths.data / "tools/playit-agent",
    ]:
        file.unlink(missing_ok=True)


def enable_renewal(paths):
    def q(value):
        return (
            '"'
            + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            + '"'
        )

    unit = Path.home() / ".config/systemd/user/uwucraft-network.service"
    atomic(
        unit,
        f"""[Unit]
Description=uwucraft direct port mapping renewal
After=network-online.target
Wants=network-online.target
[Service]
ExecStart={q(sys.executable)} -m uwucraft.portmap
Environment={q("XDG_CONFIG_HOME=" + str(paths.config.parent))}
Environment={q("XDG_DATA_HOME=" + str(paths.data.parent))}
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
[Install]
WantedBy=default.target
""",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", unit.name], check=True, timeout=15
    )


def wan_error(wan):
    import ipaddress

    try:
        address = ipaddress.IPv4Address(wan)
    except ValueError:
        return (
            "Маршрутизатор не сообщил WAN IPv4; проверьте его подключение к провайдеру."
        )
    if address in ipaddress.ip_network("100.64.0.0/10"):
        return "CGNAT у провайдера: запросите публичный (белый) IPv4 с входящими TCP-подключениями. Настройка ПК и домашнего роутера не устраняет CGNAT."
    if not address.is_global or address.is_multicast or address.is_reserved:
        return "WAN IPv4 маршрутизатора не публичный: устраните двойной NAT или запросите белый IPv4 у провайдера."
    return ""


def configure(paths=None, progress=None):
    paths = paths or Paths()
    progress = progress or (lambda message: None)
    report = {
        "address": "",
        "verified": False,
        "errors": [],
        "warnings": [],
        "checked_at": time.time(),
    }
    config = paths.load()

    def step(message):
        progress(message)

    step("Настройка действующего брандмауэра nftables")
    try:
        report["firewall"] = firewall.configure(config.port)
    except (
        firewall.FirewallError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        report["errors"].append(str(exc))
    step("Автопроброс TCP-порта через UPnP/NAT-PMP")
    mapping = None
    mapping_error = ""
    try:
        mapping = portmap.map_port(config.port)
        report["mapping"] = asdict(mapping)
        problem = wan_error(mapping.wan)
        if problem:
            report["errors"].append(problem)
        try:
            enable_renewal(paths)
        except (OSError, subprocess.SubprocessError) as exc:
            report["errors"].append(
                "Проброс выполнен, но автоматическое продление не включено: " + str(exc)
            )
    except (Error, OSError, ValueError) as exc:
        mapping_error = str(exc)
        wan = getattr(exc, "wan", "")
        if wan:
            report["errors"].append(wan_error(wan) or mapping_error)
    step("Определение публичного IPv4 через HTTPS")
    try:
        ip = network.external_ipv4(paths)
        report["address"] = f"{ip}:{config.port}"
        with paths.lock():
            config = paths.load()
            config.public_address = ip
            paths.save(config)
        if mapping and not wan_error(mapping.wan) and mapping.wan != ip:
            report["errors"].append(
                "WAN IPv4 маршрутизатора и адрес HTTPS-сервиса различаются. Проверьте двойной NAT и маршрут входящих/ответных пакетов."
            )
    except (Error, OSError, ValueError) as exc:
        report["errors"].append(str(exc))
    if report["address"] and server.status(paths)["status"] == "Running":
        step("Независимая внешняя проверка Minecraft-порта")
        try:
            result = network.probe(report["address"], paths)
            report["probe"] = result
            # API online alone can refer to a different Minecraft server.
            report["verified"] = result["online"] and result["identity_verified"]
            if not report["verified"]:
                report["errors"].append(
                    "Внешний Minecraft-порт не подтверждён. Проверьте белый IPv4, TCP port forwarding и правила провайдера. Результат внешнего сервиса может кэшироваться до 5 минут."
                )
        except (Error, OSError, ValueError, KeyError) as exc:
            report["errors"].append(
                "Внешнюю проверку выполнить не удалось: " + str(exc)
            )
    else:
        report["errors"].append(
            "Сервер не запущен или публичный IPv4 не определён; доступность извне не проверена."
        )
    if mapping_error:
        message = (
            mapping_error
            + " Настройте в маршрутизаторе TCP port forwarding выбранного порта на этот компьютер и закрепите его адрес в DHCP."
        )
        (report["warnings"] if report["verified"] else report["errors"]).append(message)
    atomic(
        paths.data / "deployment.json", json.dumps(report, ensure_ascii=False, indent=2)
    )
    return report


def enable_server(paths):
    server.unit(paths)
    subprocess.run(
        ["systemctl", "--user", "enable", "uwucraft.service"], check=True, timeout=15
    )
    import getpass

    result = subprocess.run(
        ["loginctl", "--no-ask-password", "enable-linger", getpass.getuser()],
        capture_output=True,
        timeout=15,
    )
    if result.returncode:
        result = subprocess.run(
            ["sudo", "-n", "--", "loginctl", "enable-linger", getpass.getuser()],
            capture_output=True,
            timeout=15,
        )
        if result.returncode:
            return 'Не удалось включить linger: выполните sudo loginctl enable-linger "$USER" для работы после выхода из системы.'
    return ""


def start_server(paths):
    subprocess.run(
        ["systemctl", "--user", "start", "uwucraft.service"], check=True, timeout=30
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.status(paths)["status"] in {"Starting", "Running"}:
            server.wait_ready(paths)
            return
        time.sleep(0.1)
    raise Error(
        "systemd не запустил supervisor. Проверьте journalctl --user -u uwucraft.service."
    )
