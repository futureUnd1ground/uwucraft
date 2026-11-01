import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from . import backups, content, installers, network, server
from .config import Error, Paths
from .platforms import windows

app = typer.Typer(
    no_args_is_help=False, help="uwucraft · A quiet home for your Minecraft server."
)
mod = typer.Typer(help="Совместимый серверный контент Modrinth.")
profile_app = typer.Typer(help="Отдельные серверы, миры и версии Minecraft.")
app.add_typer(profile_app, name="profile")
net = typer.Typer(help="Адреса и честная диагностика сети.")
app.add_typer(mod, name="mod")
app.add_typer(net, name="network")
console = Console(no_color="NO_COLOR" in os.environ)


@app.callback(invoke_without_command=True)
def entry(
    ctx: typer.Context,
    profile: Annotated[
        str,
        typer.Option(
            "--profile", "-p", help="ID сервера; по умолчанию выбранный профиль."
        ),
    ] = "",
):
    if profile:
        Paths(profile=profile)  # validate before any mutation
        previous = os.environ.get("UWUCRAFT_PROFILE")
        os.environ["UWUCRAFT_PROFILE"] = profile

        def restore():
            if previous is None:
                os.environ.pop("UWUCRAFT_PROFILE", None)
            else:
                os.environ["UWUCRAFT_PROFILE"] = previous

        ctx.call_on_close(restore)
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
            address if report.get("verified") else "Адрес для игроков пока недоступен",
            title="Публичный адрес для подключения"
            if report.get("verified")
            else "Публичное подключение НЕ настроено",
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
    port: int | None = None,
    max_players: int = 20,
    accept_eula: bool = False,
    start: bool = True,
    dry_run: bool = False,
    offline_mode: bool = False,
    whitelist: bool = False,
):
    """Создать сервер: краткий мастер или --quick без вопросов."""
    from .creation import CreateOptions, create, suggested_memory

    # Fail before any prompt: the wizard must not ask questions about a
    # profile that already holds a server. A dry-run only prints parameters
    # and never touches the installation, so it stays allowed.
    existing = Paths()
    if not dry_run and (
        existing.file.exists()
        or (existing.server.exists() and any(existing.server.iterdir()))
    ):
        raise Error(
            "Сервер уже существует и сохранён. Для повторной настройки публичного "
            "доступа: uwucraft network configure. Для другого сервера: uwucraft "
            "--profile ИМЯ create. Для удаления этого сервера: uwucraft delete."
        )
    if port is None:
        from .profiles import suggested_port

        port = suggested_port()
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
    mode = mode or ("pirate" if offline_mode else "license")
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
        whitelist=whitelist,
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
    server.manage(action)


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
    address = network.connection_address(refresh=True)
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


@app.command("delete")
def delete_command(
    identifier: Annotated[str, typer.Argument()] = "",
    yes: bool = False,
    purge_files: bool = False,
):
    """Удалить профиль сервера: службы, метаданные и файлы uwucraft.

    Без --purge-files файлы импортированного «на месте» сервера в его
    каталоге сохраняются; мир сервера, созданного uwucraft, удаляется вместе
    с профилем. Действие необратимо.
    """
    from .profiles import delete

    identifier = identifier or Paths().profile
    paths = Paths(profile=identifier)  # validate the ID early
    if not paths.file.exists():
        raise Error(
            "Профиль не установлен: "
            + identifier
            + ". Установленные: uwucraft profile list."
        )
    config = paths.load()
    marker = paths.data / "server-location"
    imported = marker.exists() and not marker.is_symlink()
    note = (
        "Файлы сервера в "
        + str(paths.server)
        + (" будут удалены." if purge_files else " сохранятся (управление на месте).")
        if imported
        else "Мир сервера в " + str(paths.server) + " будет удалён."
    )
    if not yes and not typer.confirm(
        f"Удалить сервер «{config.name}» (профиль {identifier}, "
        f"{config.loader} {config.version or '—'})? {note} Продолжить?",
        default=False,
    ):
        raise typer.Abort()
    warnings = delete(identifier, purge_imported=purge_files)
    console.print("Удалён профиль: " + identifier, markup=False)
    for message in warnings:
        console.print("Примечание: " + message, markup=False)


@app.command("discover")
def discover_command():
    """Найти существующие серверы в типичных каталогах (без полного сканирования диска)."""
    from .importer import discover, summarize

    found = discover()
    if not found:
        console.print(
            "Готовые серверы не найдены. Проверены домашний каталог (глубина 2), "
            "Desktop, Downloads, Documents, Games, /opt и /srv."
        )
        return
    for index, detected in enumerate(found, 1):
        console.print(f"── Вариант {index} " + "─" * 40, markup=False)
        console.print(summarize(detected), markup=False)
    console.print("Импорт: uwucraft import ПУТЬ [--quick]", markup=False)


@app.command("import")
def import_command(
    path: Annotated[str, typer.Argument(help="Каталог существующего сервера.")],
    profile: str = "",
    copy: bool = False,
    loader: str = "",
    version: str = "",
    name: str = "",
    memory: str = "",
    quick: bool = False,
    no_network: bool = False,
):
    """Подключить уже скачанный сервер к uwucraft без повторного скачивания.

    По умолчанию файлы не перемещаются: uwucraft управляет сервером в его
    каталоге. --copy дублирует сервер в каталог uwucraft. Миры, конфиги,
    моды и server.properties не изменяются.
    """
    from .importer import register, scan, summarize

    detected = scan(path)
    console.print(Panel(summarize(detected), title="Распознанный сервер"))
    if not quick:
        if loader or detected.loader == "unknown" or not detected.version:
            console.print(
                "Проверьте ядро и версию; Enter оставляет распознанное значение.",
                markup=False,
            )
        loader = typer.prompt(
            "Ядро",
            default=loader or detected.loader
            if (loader or detected.loader) in installers.LOADERS
            else "paper",
        )
        version = typer.prompt(
            "Версия Minecraft (пусто — не задана)",
            default=version or detected.version,
        )
        name = name or typer.prompt("Название", default=detected.directory.name)
        memory = memory or typer.prompt("RAM", default=detected.memory or "2G")
    with console.status("Импорт сервера…") as status:
        report = register(
            detected,
            profile=profile or None,
            copy=copy,
            name=name or None,
            loader=loader or None,
            version=version or None,
            memory=memory or None,
            configure_network=not no_network,
            progress=status.update,
        )
    console.print(
        f"Профиль: {report['profile']} · сервер управляется в каталоге: "
        + (str(Paths(profile=report["profile"]).server))
    )
    show_creation_report(report)
    if report.get("backup"):
        console.print("Первая резервная копия: " + report["backup"], markup=False)
    if report.get("errors"):
        raise typer.Exit(1)


@app.command("whitelist")
def whitelist(action: str, player: Annotated[str, typer.Argument()] = ""):
    _player_command("whitelist", action, player, {"add", "remove", "list", "on", "off"})


@app.command("ops")
def ops(action: str, player: str):
    if action not in {"add", "remove"}:
        raise Error("Действия: add, remove.")
    _player_command("op" if action == "add" else "deop", "", player, {""})


def _player_command(prefix, action, player, allowed, paths=None):
    import re

    if action not in allowed or (
        player and not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player)
    ):
        raise Error("Недопустимое действие или имя Minecraft.")
    if action in {"add", "remove", ""} and not player:
        raise Error("Укажите имя игрока.")
    server.command(" ".join(x for x in [prefix, action, player] if x), paths)


@app.command("service")
def service_install(enable: bool = False):
    console.print(str(server.unit()))
    if enable and not windows():
        subprocess.run(["systemctl", "--user", "enable", Paths().unit_name], check=True)


@app.command("backup-schedule")
def backup_schedule(calendar: str = "daily"):
    """systemd timer: остановка → backup → восстановление предыдущего состояния."""
    import sys

    from .config import atomic

    if windows():
        if calendar != "daily":
            raise Error(
                "Windows: поддерживается daily (ежедневно в 04:00 после входа пользователя)."
            )
        from .windows import task

        console.print(task(Paths(), "backup", daily=True))
        return
    if "\n" in calendar or "\r" in calendar:
        raise Error("Недопустимый календарь.")
    home = Path.home() / ".config/systemd/user"
    python = sys.executable.replace("%", "%%")
    paths = Paths()
    unit_name = (
        "uwucraft-backup"
        if paths.profile == "default"
        else "uwucraft-backup@" + paths.profile
    )
    environment = "".join(
        f'Environment="{key}={value}"\n' for key, value in paths.environment().items()
    )
    atomic(
        home / (unit_name + ".service"),
        f'[Unit]\nDescription=uwucraft consistent backup\n[Service]\nType=oneshot\nExecStart="{python}" -m uwucraft.scheduled\n{environment}',
    )
    atomic(
        home / (unit_name + ".timer"),
        f"[Unit]\nDescription=uwucraft backup timer\n[Timer]\nOnCalendar={calendar}\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", unit_name + ".timer"], check=True
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


@net.command("relay")
def network_relay(stop: bool = False):
    """Включить публичный TCP relay (сторонний сервер bore.pub) или отключить его."""
    from . import relay

    paths = Paths()
    if stop:
        relay.stop(paths)
        console.print("TCP relay отключён.")
        return
    with console.status("Запуск публичного TCP relay…"):
        value = relay.setup(paths)
        result = network.probe(value["address"], paths)
    if not result["online"] or not result["identity_verified"]:
        raise Error(
            "Relay запущен, но независимая проверка Minecraft ещё не подтвердила доступ. Повторите network check через минуту."
        )
    console.print(network.connection_address(paths), markup=False)


@net.command("configure")
def network_configure(wan_ip: str = ""):
    """Автоматически настроить nftables, прямой проброс и проверить внешний порт."""
    from .deployment import configure, disable_legacy_service

    paths = Paths()
    if not paths.file.exists():
        raise Error("Сначала создайте сервер: uwucraft create.")
    from . import privilege

    problem = privilege.ensure_interactive("настройка firewall nftables")
    if problem:
        console.print(problem, markup=False)
    disable_legacy_service()
    with console.status("Настройка прямого публичного доступа…") as status:
        report = configure(paths, status.update, wan_ip=wan_ip)
    show_creation_report(report)
    if report["errors"] or not report["verified"]:
        raise typer.Exit(1)


@mod.command("search")
def mod_search(query: str, kind: str = "mod"):
    console.print(content.search(query, kind), markup=False)


@app.command("compat-setup")
def compat_setup(restart: bool = False, legacy_clients: bool = False):
    """Установить ViaVersion + ViaBackwards из официальных релизов с SHA-256."""
    from .compat import setup

    console.print(setup(restart=restart, legacy=legacy_clients))


@profile_app.command("list")
def profile_list():
    from .profiles import listing

    active = Paths().profile
    for paths in listing():
        c = paths.load()
        console.print(
            f"{'*' if paths.profile == active else ' '} {paths.profile} | {c.name} | {c.loader} {c.version or 'не установлен'} | TCP {c.port} | {server.status(paths)['status']}",
            markup=False,
        )


@profile_app.command("use")
def profile_use(identifier: Annotated[str, typer.Argument()]):
    from .profiles import select

    console.print("Выбран профиль: " + select(identifier).profile)


@app.command("versions")
def versions_command(loader: str = "paper", limit: int = 30, snapshots: bool = False):
    from .sources import available_versions

    if not 1 <= limit <= 500:
        raise Error("limit: 1–500.")
    console.print(
        "\n".join(available_versions(loader, snapshots=snapshots)[:limit]), markup=False
    )
