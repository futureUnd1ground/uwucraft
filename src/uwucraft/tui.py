import asyncio
import json
import os
from typing import ClassVar

import psutil
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
)

from . import backups, content, network, server
from .config import Error, Paths
from .platforms import windows
from .themes import PALETTES, load_custom, theme

NAV = [
    "Dashboard",
    "Console",
    "Players",
    "Mods",
    "Packs",
    "Plugins",
    "Worlds",
    "Backups",
    "Network",
    "Settings",
    "Themes",
]


class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = """Confirm { align: center middle; background: $background 80%; }
    Confirm > Vertical { width: 64; height: auto; padding: 2 3; border: round $accent; background: $surface; }
    Confirm Horizontal { height: 3; margin-top: 1; }
    Confirm Button { margin-right: 2; }"""

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with Vertical():
            yield Label(self.message)
            with Horizontal():
                yield Button("Подтвердить", id="confirm", variant="error")
                yield Button("Отмена", id="cancel")

    @on(Button.Pressed)
    def answer(self, event):
        self.dismiss(event.button.id == "confirm")


class Uwucraft(App):
    TITLE = "uwucraft"
    SUB_TITLE = "a quiet home for your server"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS: ClassVar = [
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("ctrl+k", "command_palette", show=False),
        Binding("f5", "refresh", "Refresh"),
        Binding("ctrl+s", "start", "Start"),
        Binding("ctrl+x", "stop", "Stop"),
        Binding("ctrl+r", "restart", "Restart"),
        Binding("ctrl+b", "backup", "Backup"),
        Binding("ctrl+y", "copy_address", "Copy address"),
        Binding("ctrl+q", "quit", "Quit"),
    ]
    CSS = """
    Screen { background: $background; }
    #top { height: 5; padding: 1 3; border-bottom: solid $accent; background: $surface; }
    #body { height: 1fr; }
    #sidebar { width: 21; padding: 1 1; background: $surface; border-right: solid $primary 25%; }
    ListView { height: auto; background: transparent; }
    ListItem { height: 3; padding: 1 1 0 1; }
    ListItem Label { width: 100%; }
    ListItem.-highlight { background: $accent 18%; color: $accent; }
    #main { padding: 1 3; }
    .page { height: auto; min-height: 100%; }
    .heading { text-style: bold; color: $accent; margin-bottom: 1; height: 2; }
    .subtitle { color: $text-muted; height: auto; margin-bottom: 1; }
    .card { border: round $primary 35%; padding: 1 2; height: auto; margin-bottom: 1; }
    .cardtitle { color: $accent; text-style: bold; height: 2; }
    .bodytext { height: auto; }
    .actions { height: auto; min-height: 3; margin-top: 1; }
    Button { margin-right: 1; min-width: 12; border: none; }
    Input { margin-bottom: 1; }
    Select { margin-bottom: 1; }
    #stats { height: 2; padding: 0 2; background: $surface; color: $text-muted; }
    #consolelog { height: 1fr; min-height: 14; border: round $primary 35%; }
    #consolepage { height: 100%; }
    #sidebar-footer { margin-top: 2; color: $text-muted; padding: 1; height: auto; }
    #loading { height: 1; color: $accent; }
    #theme-grid { layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 1 2; height: auto; }
    .theme-tile { height: 6; padding: 0 1; border: round $primary 25%; }
    .theme-tile Static { height: 1; }
    .theme-tile Button { height: 1; min-height: 1; padding: 0; background: transparent; color: $accent; }
    .theme-name { text-style: bold; }
    """

    def __init__(self, selected_theme="", hide_addresses=False):
        super().__init__()
        self.paths = Paths()
        self.hide_addresses = hide_addresses
        self.selected_theme = selected_theme
        self.page = "Dashboard"
        self.history = []
        self.history_index = 0
        self.paused = False
        self.log_snapshot = ""
        self.busy = False
        self.last_state = None
        self.last_error = ""
        self.minecraft_metrics = {}
        self.world_bytes = None
        self.custom_theme_names = set()

    def get_system_commands(self, screen):
        from textual.app import SystemCommand

        yield from super().get_system_commands(screen)
        for name, description, action in [
            ("Создать сервер", "Мастер создания Minecraft", self.action_create),
            (
                "Импортировать сервер",
                "Подключить существующий каталог сервера",
                lambda: self.action_import(False),
            ),
            (
                "Найти серверы",
                "Обнаружить серверы в типичных каталогах",
                lambda: self.action_import(True),
            ),
            (
                "Удалить сервер",
                "Удалить профиль текущего сервера",
                self.action_delete,
            ),
            ("Start server", "Запуск Minecraft", self.action_start),
            ("Stop server", "Сохранить мир и остановить", self.action_stop),
            ("Restart server", "Перезапуск сервера", self.action_restart),
            ("Backup", "Создать полную копию", self.action_backup),
            ("Copy Address", "Скопировать адрес", self.action_copy_address),
            ("Refresh", "Обновить состояние", self.action_refresh),
        ]:
            yield SystemCommand(name, description, action)

    def compose(self) -> ComposeResult:
        yield Static("", id="top")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                from .profiles import listing

                items = listing()
                if self.paths.profile not in {p.profile for p in items}:
                    items.append(self.paths)
                yield Select(
                    [(p.profile, p.profile) for p in items],
                    value=self.paths.profile,
                    allow_blank=False,
                    id="profile-select",
                )
                yield ListView(
                    *[
                        ListItem(Label(f"{index + 1:02}  {name}"), name=name)
                        for index, name in enumerate(NAV)
                    ],
                    id="nav",
                )
                yield Static(
                    "LOCAL FIRST\nYour files. Your world.\n\nCtrl+P  commands",
                    id="sidebar-footer",
                )
            with VerticalScroll(id="main"):
                yield Vertical(id="page")
        yield Static("", id="loading")
        yield Static("", id="stats")
        yield Footer()

    async def on_mount(self):
        from . import privilege

        # Never draw a sudo password prompt over a running TUI screen; the
        # wizard suspends the app first, then lets sudo itself authenticate.
        privilege.ui_active = True
        for name, colors in PALETTES.items():
            self.register_theme(theme(name, colors))
        try:
            for custom in load_custom(self.paths.config / "themes"):
                self.register_theme(custom)
                self.custom_theme_names.add(custom.name)
        except (ValueError, KeyError) as error:
            self.notify(
                "Некорректная пользовательская тема: " + str(error), severity="error"
            )
        self.theme = (
            "monochrome"
            if "NO_COLOR" in os.environ
            else self.selected_theme or self.paths.load().theme
        )
        await self.render_page()
        self.update_state()
        self.set_interval(2, self.update_state)
        self.set_interval(5, self.refresh_metrics)
        self.refresh_metrics()

    @on(ListView.Selected, "#nav")
    async def navigate(self, event):
        self.page = event.item.name
        await self.render_page()

    def card(self, title, body, id=None):
        return Vertical(
            Static(title, classes="cardtitle"),
            Static(body, classes="bodytext", markup=False),
            classes="card",
            id=id,
        )

    async def render_page(self):
        area = self.query_one("#page", Vertical)
        await area.remove_children()
        config = self.paths.load()
        title = Static(self.page.upper(), classes="heading")
        await area.mount(title)
        if self.page == "Dashboard":
            await area.mount(
                Static(
                    "Your world. Your rules. One quiet terminal.", classes="subtitle"
                )
            )
            await area.mount(
                self.card("SERVER / OVERVIEW", self.overview(), id="overview")
            )
            await area.mount(
                Horizontal(
                    Button("Создать сервер", id="create-server", variant="primary"),
                    Button("Импортировать сервер", id="import-server"),
                    Button("Найти серверы", id="discover-servers"),
                    Button("Start", id="start"),
                    Button("Stop", id="stop"),
                    Button("Restart", id="restart"),
                    Button("Backup", id="backup"),
                    Button("Удалить сервер", id="delete-server", variant="error"),
                    Button("Разные клиенты", id="compat-setup"),
                    classes="actions",
                )
            )
            await area.mount(
                self.card(
                    "CONNECT / ПРИГЛАСИТЬ ИГРОКОВ", self.connection(), id="connect"
                )
            )
            await area.mount(
                Horizontal(
                    Button("Скопировать адрес", id="copy-public", variant="primary"),
                    classes="actions",
                )
            )
            copies = backups.listing(self.paths)
            world = self.paths.server / "world"
            await area.mount(
                self.card(
                    "CARE / YOUR WORLD",
                    f"Последний бэкап: {copies[0]['name'] if copies else 'Ещё нет — Ctrl+B'}\nМоды: {len(content.inventory(self.paths))}\nМир: {'Создан' if world.exists() else 'Будет создан при первом запуске'}\nСписки игроков и операторов — во вкладке Players.",
                )
            )
            if not config.version:
                await area.mount(
                    self.card(
                        "GET STARTED",
                        "Сервер пока не установлен.\n«Создать сервер» — новый сервер из официальных источников.\n«Импортировать сервер» / «Найти серверы» — подключить уже скачанный сервер без потери данных.\nМастер проверит систему и запросит принятие EULA.",
                    )
                )
        elif self.page == "Console":
            await area.mount(
                Vertical(
                    Input(placeholder="Поиск в логах", id="log-search"),
                    Select(
                        [
                            ("Все уровни", "ALL"),
                            ("INFO", "INFO"),
                            ("WARN", "WARN"),
                            ("ERROR", "ERROR"),
                        ],
                        value="ALL",
                        id="log-level",
                    ),
                    Button("Pause / Resume", id="pause"),
                    Button("Copy last error", id="copy-error"),
                    RichLog(id="consolelog", wrap=True, markup=False),
                    Input(
                        placeholder="Команда Minecraft · Enter отправить · ↑↓ история",
                        id="command",
                    ),
                    id="consolepage",
                )
            )
            self.log_snapshot = ""
            self.update_logs()
        elif self.page == "Network":
            await area.mount(
                self.card("ADDRESSES", self.connection(), id="network-addresses")
            )
            await area.mount(
                Horizontal(
                    Button("Get public IP", id="fetch-ip"),
                    Button("Check port", id="check-port"),
                    Button("Настроить доступ", id="configure-network"),
                    Button("Включить TCP relay", id="enable-relay"),
                    classes="actions",
                )
            )
            await area.mount(
                self.card(
                    "PUBLIC ACCESS",
                    "Пробросьте TCP-порт сервера на маршрутизаторе и разрешите его в существующем firewall.\nЛокальный listen НЕ подтверждает доступ из интернета. Проверяйте с независимой внешней сети.\nПри CGNAT запросите белый IPv4 у провайдера. Автопроброс требует UPnP или NAT-PMP на маршрутизаторе.\nНе открывайте административные порты. RCON выключен.",
                )
            )
            report_file = self.paths.data / "deployment.json"
            if report_file.exists():
                try:
                    report = json.loads(report_file.read_text(encoding="utf-8"))
                    messages = report.get("errors", []) + report.get("warnings", [])
                    await area.mount(
                        self.card(
                            "ДИАГНОСТИКА",
                            "\n".join(messages) or "Ошибок настройки не сохранено.",
                        )
                    )
                except (OSError, ValueError):
                    await area.mount(
                        self.card(
                            "ДИАГНОСТИКА", "Не удалось прочитать отчёт настройки."
                        )
                    )
        elif self.page == "Themes":
            await area.mount(
                Static(
                    "Выбор применяет тему сразу и сохраняет её в TOML.",
                    classes="subtitle",
                )
            )
            await area.mount(
                Select(
                    [
                        (name.replace("-", " ").title(), name)
                        for name in self.available_themes
                        if name in PALETTES or name in self.custom_theme_names
                    ],
                    value=self.theme,
                    id="theme-select",
                )
            )
            await area.mount(
                self.card(
                    "LIVE PREVIEW",
                    "● Running  ◌ Starting  ■ Stopped  ! Crashed\nТекст, контраст, акцент и фон меняются без перезапуска.",
                )
            )
            await area.mount(
                Static(
                    "[green]✓ Success[/]   [yellow]! Warning[/]   [red]✕ Error[/]   [blue]i Info[/]",
                    classes="card",
                )
            )
            gallery = Vertical(id="theme-grid")
            await area.mount(gallery)
            for index, (name, palette) in enumerate(PALETTES.items(), 1):
                accent, background, surface, success, warning, error, info, *_ = palette
                await gallery.mount(
                    Vertical(
                        Static(
                            f"{index:02}  {name.replace('-', ' ').title()}",
                            classes="theme-name",
                        ),
                        Static(
                            f"[{accent}]━━━[/] [{success}]━━━[/] [{warning}]━━━[/] [{error}]━━━[/] [{info}]━━━[/]"
                        ),
                        Button("Apply →", id="apply-theme-" + name),
                        classes="theme-tile",
                    )
                )

        elif self.page in {"Mods", "Plugins", "Packs"}:
            await area.mount(
                Static(
                    "Установка и обновление требуют остановленного сервера. Modrinth проверяет версию и загрузчик.",
                    classes="subtitle",
                )
            )
            await area.mount(
                Input(
                    placeholder="Поиск по установленному контенту", id="content-search"
                )
            )
            await area.mount(
                Static(
                    self.content_text(), id="content-list", markup=False, classes="card"
                )
            )
            await area.mount(
                Input(
                    placeholder="Modrinth slug / .mrpack для Packs", id="content-source"
                )
            )
            await area.mount(
                Horizontal(
                    Button("Install", id="content-install", variant="primary"),
                    Button("Search Modrinth", id="content-remote-search"),
                    *(
                        [
                            Button("Update", id="content-update"),
                            Button("Remove", id="content-remove"),
                        ]
                        if self.page != "Packs"
                        else []
                    ),
                    classes="actions",
                )
            )
        elif self.page == "Players":
            await area.mount(
                self.card(
                    "WHITELIST / OPERATORS", self.player_text(), id="players-list"
                )
            )
            await area.mount(
                Input(placeholder="Точное имя Minecraft", id="player-name")
            )
            await area.mount(
                Horizontal(
                    Button("Whitelist add", id="whitelist-add"),
                    Button("Whitelist remove", id="whitelist-remove"),
                    Button("Op", id="op"),
                    Button("Deop", id="deop"),
                    classes="actions",
                )
            )
            await area.mount(
                Static(
                    "Списки читаются из whitelist.json / ops.json. Команды требуют запущенный сервер.",
                    classes="subtitle",
                )
            )
        elif self.page == "Backups":
            await area.mount(
                Input(placeholder="Поиск резервных копий", id="backup-search")
            )
            await area.mount(
                Static(
                    self.backup_text(), id="backup-list", markup=False, classes="card"
                )
            )
            await area.mount(Button("Create backup", id="backup", variant="primary"))
            await area.mount(
                Input(placeholder="Точное имя копии .tar.gz", id="restore-name")
            )
            await area.mount(Button("Restore…", id="restore", variant="warning"))
            await area.mount(
                Static(
                    "Для целостности мира нужен остановленный сервер. Restore сохраняет текущее состояние.",
                    classes="subtitle",
                )
            )
        elif self.page == "Worlds":
            await area.mount(
                self.card(
                    "WORLD STORAGE",
                    "Основной мир: "
                    + str(self.paths.server / "world")
                    + "\nNether / End управляются Minecraft или выбранным ядром.\nДля переноса/отката используйте полную резервную копию. Удаление миров из UI намеренно не предоставляется.",
                )
            )
            await area.mount(Button("Backup world", id="backup"))
        else:
            await area.mount(
                self.card(
                    "CONFIGURATION",
                    f"Config: {self.paths.file}\nData: {self.paths.data}\nserver.properties: {self.paths.server / 'server.properties'}\nJava: {config.java}\nEULA: https://aka.ms/MinecraftEULA\nПроверяйте online-mode и white-list в server.properties; режим offline требует auth-плагин.",
                )
            )
            await area.mount(
                Input(
                    value=config.memory,
                    placeholder="Память, например 4G",
                    id="memory-setting",
                )
            )
            await area.mount(Button("Save memory", id="save-memory", variant="primary"))
            await area.mount(
                Static(
                    "Остальные настройки меняются в TOML / server.properties при остановленном сервере.\nНе отключайте online-mode на публичном сервере.",
                    classes="subtitle",
                )
            )

    def overview(self):
        c = self.paths.load()
        state = server.status(self.paths)
        return f"{c.name}\nMinecraft {c.version or '—'}  /  {c.loader.upper()}  /  {c.memory}\nСостояние: {state['status']}   Порт: {c.port}\nПроцесс RAM: {state.get('ram_mb', 0)} MiB   Uptime: {state.get('uptime', 0)}s"

    def connection(self):
        try:
            address = network.connection_address(self.paths)
            return (
                "•••• (privacy mode)" if self.hide_addresses else address
            ) + "\nВнешний вход подтверждён."
        except Error:
            return "Публичный адрес для игроков пока недоступен.\nNetwork → Настроить доступ: проверьте маршрут, белый IPv4 и внешний порт."

    def player_text(self):
        result = []
        for name in ["whitelist", "ops"]:
            file = self.paths.server / (name + ".json")
            players = (
                json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
            )
            result.append(
                name + ": " + (", ".join(p["name"] for p in players) or "Пусто")
            )
        return "\n".join(result)

    def content_text(self, search=""):
        records = content.inventory(self.paths)
        rows = [
            f"{key}  ·  {value['kind']}  ·  {value['version_id']}"
            for key, value in records.items()
            if search.lower() in key.lower()
        ]
        return (
            "\n".join(rows)
            or "Контент не установлен. Укажите slug проекта Modrinth ниже."
        )

    def backup_text(self, search=""):
        return (
            "\n".join(
                f"{b['name']}  ·  {b['bytes'] / 1024**2:.1f} MiB"
                for b in backups.listing(self.paths)
                if search in b["name"]
            )
            or "Копий пока нет. Создайте первую при остановленном сервере."
        )

    @work(group="metrics", exclusive=True)
    async def refresh_metrics(self):
        if server.status(self.paths)["status"] != "Running":
            self.minecraft_metrics = {}
            return
        paths = self.paths
        try:
            metrics = await asyncio.to_thread(
                network.minecraft_status, "127.0.0.1", paths.load().port
            )
        except (Error, OSError, ValueError):
            metrics = {}
        if self.paths.profile == paths.profile:
            self.minecraft_metrics = metrics

    def update_state(self):
        c = self.paths.load()
        state = server.status(self.paths)
        icon = {
            "Running": "●",
            "Starting": "◌",
            "Stopping": "◌",
            "Stopped": "■",
            "Crashed": "✕",
        }[state["status"]]
        color = {
            "Running": "green",
            "Starting": "yellow",
            "Stopping": "yellow",
            "Stopped": "dim",
            "Crashed": "red",
        }[state["status"]]
        self.query_one("#top", Static).update(
            f"[bold $accent]uwucraft[/]  /  {c.name}\nMinecraft {c.version or 'not installed'} · {c.loader}     [{color}]{icon} {state['status']}[/]".replace(
                "[bold $accent]", "[bold]"
            )
        )
        self.query_one("#stats", Static).update(
            f"CPU {psutil.cpu_percent():.0f}%  ·  System RAM {psutil.virtual_memory().percent:.0f}%  ·  Java {state.get('ram_mb', 0)} MiB  ·  Uptime {state.get('uptime', 0)}s  ·  Players {self.minecraft_metrics.get('players', {}).get('online', '—')} / {self.minecraft_metrics.get('players', {}).get('max', '—')}  ·  TPS / MSPT: —"
        )
        if self.query("#overview"):
            self.query_one("#overview .bodytext", Static).update(self.overview())
        for card_id in ["connect", "network-addresses"]:
            if self.query("#" + card_id):
                self.query_one("#" + card_id + " .bodytext", Static).update(
                    self.connection()
                )
        self.update_logs()
        if state["status"] == "Crashed" and self.last_state != "Crashed":
            self.notify(
                "Сервер завершился с ошибкой. Откройте Console для логов.",
                severity="error",
                timeout=10,
            )
        self.last_state = state["status"]

    def update_logs(self):
        if self.page != "Console" or self.paused or not self.query("#consolelog"):
            return
        text = server.logs(self.paths, 400)
        search = self.query_one("#log-search", Input).value
        level = self.query_one("#log-level", Select).value
        text = "\n".join(
            line
            for line in text.splitlines()
            if search.lower() in line.lower() and (level == "ALL" or str(level) in line)
        )
        if text != self.log_snapshot:
            log = self.query_one("#consolelog", RichLog)
            log.clear()
            log.write(Text(text))
            self.log_snapshot = text

    @on(Input.Changed)
    def search_changed(self, event):
        if event.input.id == "content-search":
            self.query_one("#content-list", Static).update(
                self.content_text(event.value)
            )
        elif event.input.id == "backup-search":
            self.query_one("#backup-list", Static).update(self.backup_text(event.value))
        elif event.input.id == "log-search":
            self.update_logs()

    @on(Select.Changed)
    def select_changed(self, event):
        if event.select.id == "theme-select" and isinstance(event.value, str):
            if event.value == self.theme:
                return
            if "NO_COLOR" in os.environ and event.value != "monochrome":
                self.notify("NO_COLOR включён: используется Monochrome.")
                return
            self.theme = event.value
            if self.paths.file.exists():
                config = self.paths.load()
                config.theme = event.value
                self.paths.save(config)
            self.notify("Тема: " + event.value)
        elif event.select.id == "log-level":
            self.update_logs()

    @on(Input.Submitted, "#command")
    def send_command(self, event):
        value = event.value
        if not value:
            return
        self.history.append(value)
        self.history_index = len(self.history)
        self.perform(lambda: server.command(value, self.paths), "Команда передана")
        event.input.value = ""

    def on_key(self, event):
        if (
            self.page == "Console"
            and self.query("#command")
            and self.focused == self.query_one("#command")
            and event.key in {"up", "down"}
        ):
            self.history_index = max(
                0,
                min(
                    len(self.history),
                    self.history_index + (-1 if event.key == "up" else 1),
                ),
            )
            self.query_one("#command", Input).value = (
                self.history[self.history_index]
                if self.history_index < len(self.history)
                else ""
            )
            event.prevent_default()

    @work(group="mutation")
    async def perform(self, function, message="Готово"):
        if self.busy:
            self.notify("Дождитесь текущей операции.", severity="warning")
            return
        self.busy = True
        self.query_one("#loading", Static).update("◌ Выполняется операция…")
        try:
            result = await asyncio.to_thread(function)
            self.notify(message if result is None else str(result), timeout=6)
            if self.page != "Console":
                await self.render_page()
            self.update_state()
        except Exception as error:
            self.last_error = (
                str(error)
                if isinstance(error, Error)
                else f"{type(error).__name__}: операция не завершена. Проверьте файлы и настройки; повторите через CLI для диагностики."
            )
            self.notify(
                self.last_error,
                title="Операция не завершена",
                severity="error",
                timeout=10,
            )
        finally:
            self.busy = False
            self.query_one("#loading", Static).update("")

    async def confirm_action(self, message, function):
        if await self.push_screen_wait(Confirm(message)):
            self.perform(function)

    @work
    async def confirm(self, message, function):
        await self.confirm_action(message, function)

    def action_start(self):
        self.perform(lambda: server.manage("start", self.paths), "Запуск запрошен")

    def action_stop(self):
        self.confirm(
            "Сохранить мир и остановить сервер?",
            lambda: server.manage("stop", self.paths),
        )

    def action_restart(self):
        self.confirm(
            "Перезапустить сервер? Игроки будут отключены.",
            lambda: server.manage("restart", self.paths),
        )

    def action_backup(self):
        self.perform(lambda: backups.create(self.paths))

    def action_create(self):
        if self.busy:
            self.notify("Дождитесь текущей операции.", severity="warning")
            return
        from .create_ui import CreateServer
        from .profiles import next_profile

        target = self.paths if not self.paths.file.exists() else next_profile()
        self.push_screen(CreateServer(target))

    def action_import(self, discover):
        if self.busy:
            self.notify("Дождитесь текущей операции.", severity="warning")
            return
        from .import_ui import ImportServer

        self.push_screen(ImportServer(discover=discover))

    def action_delete(self):
        if self.busy:
            self.notify("Дождитесь текущей операции.", severity="warning")
            return
        if not self.paths.file.exists():
            self.notify("Сервер не установлен.", severity="warning")
            return
        from .profiles import delete

        config = self.paths.load()
        imported = (self.paths.data / "server-location").exists()
        note = (
            "Файлы в " + str(self.paths.server) + " сохранятся (управление на месте)."
            if imported
            else "Мир в " + str(self.paths.server) + " будет удалён безвозвратно."
        )
        self.delete_profile(
            f"Удалить сервер «{config.name}» (профиль {self.paths.profile})? " + note,
            lambda: delete(self.paths.profile),
        )

    @work(group="mutation")
    async def delete_profile(self, message, function):
        if not await self.push_screen_wait(Confirm(message)):
            return
        self.busy = True
        self.query_one("#loading", Static).update("◌ Выполняется операция…")
        try:
            warnings = await asyncio.to_thread(function)
            self.notify("Профиль удалён. " + " ".join(warnings or []), timeout=8)
        except Error as exc:
            self.notify(str(exc), severity="error", timeout=10)
            return
        finally:
            self.busy = False
            self.query_one("#loading", Static).update("")
        # The deleted profile may have been the active one: re-resolve.
        self.paths = Paths()
        self.minecraft_metrics = {}
        self.log_snapshot = ""
        self.last_error = ""
        self.last_state = None
        from .profiles import listing

        items = listing()
        picker = self.query_one("#profile-select", Select)
        with picker.prevent(Select.Changed):
            picker.set_options(
                [(p.profile, p.profile) for p in items] or [("default", "default")]
            )
            picker.value = (
                self.paths.profile
                if any(p.profile == self.paths.profile for p in items)
                else items[0].profile
            )
        await self.render_page()
        self.update_state()

    def action_copy_address(self):
        self.copy_address("public")

    def action_refresh(self):
        self.update_state()
        self.notify("Обновлено")

    def copy_address(self, kind):
        try:
            value = network.connection_address(self.paths)
        except Error as exc:
            self.notify(str(exc), severity="warning")
            return
        if kind == "info":
            c = self.paths.load()
            value = f"Сервер: Minecraft {c.version}, {c.loader}\nАдрес: {value}"
        self.perform(lambda: network.copy(value))

    @on(Button.Pressed)
    def button(self, event):
        action = event.button.id
        actions = {
            "start": self.action_start,
            "stop": self.action_stop,
            "restart": self.action_restart,
            "backup": self.action_backup,
        }
        if action and action.startswith("apply-theme-"):
            self.query_one("#theme-select", Select).value = action.removeprefix(
                "apply-theme-"
            )
        elif action in actions:
            actions[action]()
        elif action == "create-server":
            self.action_create()
        elif action in {"import-server", "discover-servers"}:
            self.action_import(action == "discover-servers")
        elif action == "delete-server":
            self.action_delete()
        elif action == "enable-relay":

            def enable_relay():
                from . import relay

                value = relay.setup(self.paths)
                network.probe(value["address"], self.paths)
                return network.connection_address(self.paths)

            self.perform(enable_relay)
        elif action == "configure-network":
            self.network_setup()
        elif action in {"copy-public", "copy-info"}:
            self.copy_address(action.split("-")[1])
        elif action == "pause":
            self.paused = not self.paused
            self.notify("Логи приостановлены" if self.paused else "Логи возобновлены")
        elif action == "copy-error":
            lines = [
                line
                for line in server.logs(self.paths).splitlines()
                if "ERROR" in line or "Exception" in line
            ]
            value = self.last_error or (lines[-1] if lines else "")
            if value:
                self.perform(lambda: network.copy(value))
            else:
                self.notify("Ошибок пока нет.")
        elif action == "content-remote-search":
            query = self.query_one("#content-source", Input).value
            kind = (
                "plugin"
                if self.page == "Plugins"
                else "pack"
                if self.page == "Packs"
                else "mod"
            )

            def find():
                return content.search(query, kind, self.paths)

            self.remote_search(find)
        elif action == "fetch-ip":

            def fetch():
                data = network.addresses(self.paths, public=True)
                if data["public"] == "Не определён":
                    raise Error("HTTPS-сервис не вернул публичный IPv4.")
                with self.paths.lock():
                    config = self.paths.load()
                    config.public_address = data["public"].rsplit(":", 1)[0]
                    self.paths.save(config)
                return "Публичный IPv4 определён; доступность порта ещё не проверена."

            self.perform(fetch)
        elif action == "check-port":
            self.perform(
                lambda: network.probe(
                    network.addresses(self.paths, public=True)["public"], self.paths
                )
            )
        elif action == "save-memory":
            value = self.query_one("#memory-setting", Input).value

            def save():
                backups.require_stopped(self.paths)
                config = self.paths.load()
                config.memory = value
                self.paths.save(config)

            self.perform(save)
        elif action == "restore":
            name = self.query_one("#restore-name", Input).value
            self.confirm(
                f"Восстановить {name}?\nТекущее состояние будет сохранено.",
                lambda: backups.restore(name, self.paths),
            )
        elif action in {"content-install", "content-remove", "content-update"}:
            source = self.query_one("#content-source", Input).value.strip()
            kind = "plugin" if self.page == "Plugins" else "mod"
            if action == "content-remove":
                self.confirm(
                    "Удалить " + source + "?",
                    lambda: content.remove(source, self.paths),
                )
            elif self.page == "Packs":
                self.perform(lambda: content.pack(source, self.paths))
            else:
                self.perform(lambda: content.install(source, kind, self.paths))
        elif action in {"whitelist-add", "whitelist-remove", "op", "deop"}:
            from .cli import _player_command

            name = self.query_one("#player-name", Input).value.strip()
            function = (
                (
                    lambda: _player_command(
                        "whitelist",
                        action.split("-")[1],
                        name,
                        {"add", "remove"},
                        self.paths,
                    )
                )
                if action.startswith("whitelist")
                else (lambda: _player_command(action, "", name, {""}, self.paths))
            )
            if action == "op":
                self.confirm("Выдать права оператора " + name + "?", function)
            else:
                self.perform(function)

    @work(group="mutation")
    async def network_setup(self):
        from . import privilege
        from .deployment import configure, disable_legacy_service

        if (
            self.paths.load().public_access == "direct"
            and not windows()
            and not privilege.sudo_cached()
        ):
            problem = privilege.ensure_via_system(self, "настройка firewall nftables")
            if problem:
                self.notify(problem, severity="warning", timeout=10)

        def configure_public():
            disable_legacy_service()
            report = configure(self.paths)
            return (
                "Внешний вход подтверждён."
                if report["verified"]
                else "Доступность извне не подтверждена. Диагностика — в Network."
            )

        await self.perform(configure_public)

    @work(group="search", exclusive=True)
    async def remote_search(self, function):
        try:
            text = await asyncio.to_thread(function)
            if self.query("#content-list"):
                self.query_one("#content-list", Static).update(text)
        except (Error, OSError, ValueError, KeyError) as error:
            self.notify(str(error), severity="error")

    async def select_profile(self, identifier):
        from .profiles import listing, select

        self.paths = select(identifier)
        self.minecraft_metrics = {}
        self.log_snapshot = ""
        self.last_error = ""
        self.last_state = None
        self.world_bytes = None
        self.history = []
        self.history_index = 0
        picker = self.query_one("#profile-select", Select)
        with picker.prevent(Select.Changed):
            picker.set_options([(p.profile, p.profile) for p in listing()])
            picker.value = identifier
        await self.render_page()
        self.update_state()

    @on(Select.Changed, "#profile-select")
    async def profile_changed(self, event):
        if event.value == Select.BLANK or event.value == self.paths.profile:
            return
        if self.busy:
            self.notify("Дождитесь текущей операции.", severity="warning")
            with event.select.prevent(Select.Changed):
                event.select.value = self.paths.profile
            return
        try:
            await self.select_profile(event.value)
        except Error as exc:
            self.notify(str(exc), severity="error")

    @on(Button.Pressed, "#compat-setup")
    def compatibility(self):
        from .compat import setup

        self.confirm(
            "Установить ViaVersion/ViaBackwards? Игроки будут отключены на время резервной копии и перезапуска. Моды не переводятся между версиями.",
            lambda: setup(self.paths, restart=True),
        )
