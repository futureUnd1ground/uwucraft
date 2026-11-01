import os
import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from . import backups, content, installers, network, server
from .config import Error, Paths

app = typer.Typer(
    no_args_is_help=False, help="uwucraft · A quiet home for your Minecraft server."
)
mod = typer.Typer(help="Совместимый серверный контент Modrinth.")
net = typer.Typer(help="Адреса и честная диагностика сети.")
app.add_typer(mod, name="mod")
app.add_typer(net, name="network")
console = Console(no_color="NO_COLOR" in os.environ)


@app.callback(invoke_without_command=True)
def entry(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        tui()


@app.command()
def tui(theme: str = "", hide_addresses: bool = False):
    """Открыть терминальную панель управления."""
    from .tui import Uwucraft

    Uwucraft(selected_theme=theme, hide_addresses=hide_addresses).run()


def show_creation_report(report):
    address = report.get("address") or "Публичный IPv4 не определён"
    console.print(
        Panel(
            address,
            title="Публичный адрес для подключения",
            border_style="green" if report.get("verified") else "yellow",
        )
    )
    console.print(
        "Внешний вход Minecraft подтверждён."
        if report.get("verified")
        else "Доступность извне НЕ подтверждена."
    )
    for message in report.get("errors", []):
        console.print("Ошибка: " + message, markup=False)
    for message in report.get("warnings", []):
        console.print("Примечание: " + message, markup=False)


@app.command("create")
def create_command(
    quick: bool = False,
    name: str = "",
    version: str = "",
    loader: str = "",
    mode: str = "",
    memory: str = "",
    port: int = 25565,
    max_players: int = 20,
    accept_eula: bool = False,
    start: bool = True,
    dry_run: bool = False,
):
    """Создать сервер: краткий мастер или --quick без вопросов."""
    from .creation import CreateOptions, create, suggested_memory

    if not quick:
        name = name or typer.prompt("Название", default="My Minecraft server")
        loader = loader or typer.prompt(
            "Ядро (vanilla/paper/purpur/fabric/forge/neoforge)", default="paper"
        )
        version = version or typer.prompt(
            "Minecraft (auto — последняя совместимая стабильная версия)", default="auto"
        )
        mode = mode or typer.prompt("Режим (лицензия/пиратка)", default="лицензия")
        memory = memory or typer.prompt("RAM", default=suggested_memory())
        port = typer.prompt("TCP-порт", default=port, type=int)
        max_players = typer.prompt("Количество игроков", default=max_players, type=int)
    modes = {"license": True, "лицензия": True, "pirate": False, "пиратка": False}
    mode = mode or "license"
    if mode not in modes:
        raise Error("Режим: license/лицензия или pirate/пиратка.")
    if not quick and not accept_eula and not dry_run:
        accept_eula = typer.confirm(
            "Принимаете Minecraft EULA? https://aka.ms/MinecraftEULA", default=False
        )
    options = CreateOptions(
        name=name or "My Minecraft server",
        version="" if version == "auto" else version,
        loader=loader or "paper",
        online_mode=modes[mode],
        memory=memory,
        port=port,
        max_players=max_players,
        accept_eula=accept_eula,
        start=start,
    )
    if dry_run:
        from dataclasses import asdict

        console.print(asdict(options))
        return
    with console.status("Создание сервера…") as status:
        report = create(options, progress=status.update)
    show_creation_report(report)
    if report.get("errors") or not report.get("verified"):
        raise typer.Exit(1)


@app.command("auto")
@app.command("install")
def auto(
    loader: str = "",
    memory: str = "",
    version: str = "",
    port: int = 25565,
    accept_eula: bool = False,
    start: bool = True,
    dry_run: bool = False,
):
    """Совместимый короткий вход в общий мастер create."""
    if Paths().file.exists():
        console.print(
            "Существующий сервер сохранён. Используйте network configure или start."
        )
        return
    create_command(
        quick=bool(loader),
        name="",
        version=version,
        loader=loader,
        mode="",
        memory=memory,
        port=port,
        max_players=20,
        accept_eula=accept_eula,
        start=start,
        dry_run=dry_run,
    )


@app.command()
def start():
    """Запустить сервер в фоне."""
    _lifecycle("start")


@app.command()
def stop():
    """Сохранить мир и корректно остановить сервер."""
    _lifecycle("stop")


def _lifecycle(action):
    unit = Path.home() / ".config/systemd/user/uwucraft.service"
    if unit.exists():
        subprocess.run(["systemctl", "--user", action, "uwucraft.service"], check=True)
        if action in {"start", "restart"}:
            for _ in range(100):
                if server.status()["status"] in {"Starting", "Running"}:
                    break
                time.sleep(0.1)
            else:
                raise Error(
                    "Supervisor не запустился. Проверьте journalctl --user -u uwucraft.service."
                )
    elif action == "restart":
        server.stop()
        server.start()
    else:
        getattr(server, action)()


@app.command()
def restart():
    """Корректно перезапустить сервер."""
    _lifecycle("restart")


@app.command()
def status():
    """Реальное состояние процесса и конфигурация."""
    paths = Paths()
    console.print(server.status(paths))
    if paths.file.exists():
        config = paths.load()
        console.print(
            f"{config.loader} {config.version} · {config.memory} · :{config.port}"
        )


@app.command()
def logs(follow: bool = False, lines: int = 100):
    """Последние строки лога; --follow для tail."""
    paths = Paths()
    if follow:
        file = paths.server / "logs/latest.log"
        if not file.exists():
            raise Error("Логов пока нет.")
        subprocess.run(["tail", "-n", str(lines), "-F", str(file)], check=False)
    else:
        console.print(server.logs(paths, lines), markup=False)


@app.command()
def command(text: Annotated[list[str], typer.Argument()]):
    """Передать одну команду Minecraft через приватную консоль."""
    server.command(" ".join(text))
    console.print("Команда передана; ответ доступен в logs.")


@app.command("console")
def console_command():
    """Интерактивная консоль с историей (выход: /exit)."""
    console.print(
        "Minecraft console · /exit для выхода · ответы: uwucraft logs --follow"
    )
    while True:
        try:
            text = input("uwu > ")
            if text == "/exit":
                break
            server.command(text)
        except (EOFError, KeyboardInterrupt):
            break


@app.command()
def backup(dry_run: bool = False):
    """Полная копия остановленного сервера и конфигурации."""
    if dry_run:
        console.print(str(Paths().backups))
    else:
        console.print(str(backups.create()))


@app.command("list-backups")
def list_backups():
    console.print(backups.listing())


@app.command()
def restore(name: str, yes: bool = False, dry_run: bool = False):
    """Восстановить копию, предварительно сохранив текущее состояние."""
    if dry_run:
        console.print("Будет восстановлена копия: " + name)
        return
    if not yes and not typer.confirm(
        f"Восстановить {name}? Текущее состояние будет сохранено."
    ):
        raise typer.Abort()
    console.print("Предыдущее состояние: " + str(backups.restore(name)))


@app.command()
def update(dry_run: bool = False):
    """Обновить сборку текущей версии, с предварительным бэкапом."""
    if dry_run:
        console.print(
            "Копия → новая сборка текущей версии Minecraft; мир и properties сохраняются."
        )
        return
    console.print("Копия для отката: " + str(installers.update()))


@app.command("copy-address")
def copy_address(player_info: bool = False):
    data = network.addresses(public=True)
    address = data["public"]
    if address == "Не определён":
        raise Error(
            "Публичный IP получить не удалось. Задайте public_address в config.toml."
        )
    text = address
    if player_info:
        config = Paths().load()
        text = f"Сервер: Minecraft {config.version}, {config.loader}\nАдрес: {address}"
    console.print(network.copy(text))


@net.command("check")
def network_check():
    data = network.addresses(public=True)
    if data["public"] == "Не определён":
        raise Error("Не определён публичный IPv4.")
    console.print(network.probe(data["public"]))


@net.command("address")
def network_address():
    console.print(network.addresses(public=True))


@mod.command("add")
def mod_add(source: str):
    console.print(content.install(source))


@mod.command("list")
def mod_list():
    console.print(content.inventory())


@mod.command("remove")
def mod_remove(slug: str, yes: bool = False, dry_run: bool = False):
    if dry_run:
        console.print("Удаление: " + slug)
    elif yes or typer.confirm("Удалить " + slug + "?"):
        content.remove(slug)


@mod.command("update")
def mod_update(slug: Annotated[str, typer.Argument()] = ""):
    records = content.inventory()
    for key in [slug] if slug else records:
        if key not in records:
            raise Error("Контент не найден.")
        if records[key].get("source") == "import":
            console.print("Локальный файл требует ручного обновления: " + key)
        elif records[key].get("source") == "curseforge":
            from .curseforge import install as cf_install

            console.print(cf_install(records[key]["project_id"], trust_server=True))
        else:
            console.print(content.install(key, records[key]["kind"]))


pack_app = typer.Typer()
plugin_app = typer.Typer()
datapack_app = typer.Typer()
app.add_typer(pack_app, name="pack")
app.add_typer(plugin_app, name="plugin")
app.add_typer(datapack_app, name="datapack")


@pack_app.command("install")
def pack_install(source: str):
    console.print(content.pack(source))


@plugin_app.command("add")
def plugin_add(source: str):
    console.print(content.install(source, "plugin"))


@datapack_app.command("add")
def datapack_add(source: str):
    console.print(content.install(source, "datapack"))


@app.command("import-file")
def import_file(source: str, kind: str = "mod", trust_file: bool = False):
    console.print(str(content.import_file(source, kind, trust_file)))


@app.command("whitelist")
def whitelist(action: str, player: Annotated[str, typer.Argument()] = ""):
    _player_command("whitelist", action, player, {"add", "remove", "list", "on", "off"})


@app.command("ops")
def ops(action: str, player: str):
    if action not in {"add", "remove"}:
        raise Error("Действия: add, remove.")
    _player_command("op" if action == "add" else "deop", "", player, {""})


def _player_command(prefix, action, player, allowed):
    import re

    if action not in allowed or (
        player and not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player)
    ):
        raise Error("Недопустимое действие или имя Minecraft.")
    if action in {"add", "remove", ""} and not player:
        raise Error("Укажите имя игрока.")
    server.command(" ".join(x for x in [prefix, action, player] if x))


@app.command("service")
def service_install(enable: bool = False):
    console.print(str(server.unit()))
    if enable:
        subprocess.run(
            ["systemctl", "--user", "enable", "uwucraft.service"], check=True
        )


@app.command("backup-schedule")
def backup_schedule(calendar: str = "daily"):
    """systemd timer: остановка → backup → восстановление предыдущего состояния."""
    import sys

    from .config import atomic

    if "\n" in calendar or "\r" in calendar:
        raise Error("Недопустимый календарь.")
    home = Path.home() / ".config/systemd/user"
    python = sys.executable.replace("%", "%%")
    paths = Paths()
    atomic(
        home / "uwucraft-backup.service",
        f'[Unit]\nDescription=uwucraft consistent backup\n[Service]\nType=oneshot\nExecStart="{python}" -m uwucraft.scheduled\nEnvironment="XDG_CONFIG_HOME={paths.config.parent}"\nEnvironment="XDG_DATA_HOME={paths.data.parent}"\n',
    )
    atomic(
        home / "uwucraft-backup.timer",
        f"[Unit]\nDescription=uwucraft backup timer\n[Timer]\nOnCalendar={calendar}\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "uwucraft-backup.timer"], check=True
    )
    console.print("Timer включён. Бэкап временно останавливает сервер.")


def main():
    import sqlite3
    import tarfile
    import zipfile

    try:
        app()
    except Error as error:
        console.print("Ошибка: " + str(error), markup=False)
        raise SystemExit(1) from None
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        sqlite3.Error,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        console.print(
            f"Операция не завершена ({type(error).__name__}). Проверьте конфигурацию, доступ к системе и источнику.",
            markup=False,
        )
        raise SystemExit(1) from None


@net.command("probe")
def network_probe(address: str):
    """Независимая внешняя проверка Minecraft; адрес отправляется mcsrvstat.us."""
    console.print(network.probe(address))


@net.command("ping")
def network_ping(host: str = "127.0.0.1", port: int = 25565):
    """Прямое подключение Minecraft SLP с проверкой ответа протокола."""
    console.print(network.minecraft_status(host, port))


@app.command("doctor")
def doctor():
    """Проверить систему без изменений."""
    from .diagnostics import doctor as inspect

    console.print(inspect())


@app.command("auth-setup")
def auth_setup():
    """Установить AuthMe из официального релиза GitHub с SHA-256."""
    from .auth import install_authme

    console.print(install_authme())


@app.command("config-set")
def config_set(key: str, value: str, allow_offline: bool = False):
    """Изменить проверяемое свойство остановленного сервера."""
    from .settings import set_property

    set_property(key, value, allow_offline)
    console.print("Сохранено. Применится при запуске сервера.")


cf_app = typer.Typer(help="Официальный CurseForge API; требуется API key.")
app.add_typer(cf_app, name="curseforge")


@cf_app.command("add")
def curseforge_add(project_id: int, trust_server: bool = False):
    from .curseforge import install

    console.print(install(project_id, trust_server))


@net.command("diagnose")
def network_diagnose(wan_ip: str = ""):
    """Проверить WAN-адрес роутера на частный NAT/CGNAT."""
    import ipaddress

    from .diagnostics import doctor

    console.print(doctor())
    if wan_ip:
        address = ipaddress.ip_address(wan_ip)
        cgnat = address.version == 4 and address in ipaddress.ip_network(
            "100.64.0.0/10"
        )
        console.print(
            {
                "wan_public": address.is_global,
                "cgnat": cgnat,
                "action": "Нужен публичный IPv4 от провайдера с входящими TCP-подключениями."
                if not address.is_global
                else "Настройте проброс TCP-порта и проверьте nftables и маршрутизацию.",
            }
        )


@app.command("auth-reserve-admin")
def auth_reserve_admin(name: str):
    """Зарегистрировать offline-администратора; сгенерированный пароль только в файле 0600."""
    from .auth import reserve_admin

    console.print(reserve_admin(name))


@net.command("configure")
def network_configure():
    """Автоматически настроить nftables, прямой проброс и проверить внешний порт."""
    from .deployment import configure, disable_legacy_service

    paths = Paths()
    if not paths.file.exists():
        raise Error("Сначала создайте сервер: uwucraft create.")
    disable_legacy_service()
    with console.status("Настройка прямого публичного доступа…") as status:
        report = configure(paths, status.update)
    show_creation_report(report)
    if report["errors"] or not report["verified"]:
        raise typer.Exit(1)


@mod.command("search")
def mod_search(query: str, kind: str = "mod"):
    console.print(content.search(query, kind), markup=False)


@app.command("compat-setup")
def compat_setup():
    """Установить ViaVersion + ViaBackwards из официальных релизов с SHA-256."""
    from .compat import install

    console.print(install())
