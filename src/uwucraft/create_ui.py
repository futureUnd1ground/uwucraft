"""The TUI collects the same CreateOptions consumed by the CLI."""

import asyncio

from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from . import network
from .config import Error
from .creation import CreateOptions, create, suggested_memory
from .installers import LOADERS
from .platforms import windows


class CreateServer(ModalScreen):
    DEFAULT_CSS = """
    CreateServer { align: center middle; background: $background 85%; }
    #create-dialog { width: 86; max-width: 98%; height: auto; max-height: 95%; padding: 1 2; border: round $accent; background: $surface; }
    #create-fields { layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 0 2; height: auto; }
    .create-field { height: 5; }
    .create-field Label { height: 1; }
    .create-field Input, .create-field Select { margin: 0; }
    #create-eula { height: auto; width: 100%; margin: 1 0; }
    #create-progress, #create-errors { height: auto; margin: 1 0; }
    #create-address { text-style: bold; color: $accent; height: auto; padding: 1; border: round $accent; }
    #create-copy { width: 100%; height: 5; margin: 1 0; }
    #create-actions { height: 3; }
    """

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.creating = False
        self.report = None
        from .profiles import suggested_port

        try:
            self.port = suggested_port()
        except Error:
            self.port = 25565

    def compose(self):
        try:
            memory = suggested_memory()
        except Error:
            memory = ""
        with VerticalScroll(id="create-dialog"):
            yield Static("СОЗДАТЬ СЕРВЕР", classes="heading")
            with Vertical(id="create-fields"):
                for label, control in [
                    ("Название", Input(value="My Minecraft server", id="create-name")),
                    (
                        "Minecraft",
                        Input(
                            placeholder="Авто: последняя стабильная совместимая версия",
                            id="create-version",
                        ),
                    ),
                    (
                        "Ядро",
                        Select(
                            [(name.title(), name) for name in LOADERS],
                            value="paper",
                            allow_blank=False,
                            id="create-loader",
                        ),
                    ),
                    (
                        "Режим аккаунтов",
                        Select(
                            [("Лицензия", "license"), ("Пиратка", "pirate")],
                            value="license",
                            allow_blank=False,
                            id="create-mode",
                        ),
                    ),
                    (
                        "RAM",
                        Input(
                            value=memory,
                            placeholder="Автоматически",
                            id="create-memory",
                        ),
                    ),
                    (
                        "TCP-порт",
                        Input(value=str(self.port), type="integer", id="create-port"),
                    ),
                    (
                        "Количество игроков",
                        Input(value="20", type="integer", id="create-players"),
                    ),
                ]:
                    with Vertical(classes="create-field"):
                        yield Label(label)
                        yield control
            yield Checkbox(
                "Принимаю Minecraft EULA: https://aka.ms/MinecraftEULA",
                id="create-eula",
            )
            yield Static(
                (
                    "Windows: брандмауэр запросит UAC; автозапуск — после входа пользователя.\n"
                    if windows()
                    else "Для изменения nftables нужны права администратора (sudo -v перед TUI).\n"
                )
                + "Режим «пиратка» не проверяет владение аккаунтом; на Paper/Purpur устанавливается AuthMe.",
                id="create-note",
            )
            yield Static("", id="create-progress", markup=False)
            yield Static("", id="create-address", markup=False)
            yield Static("", id="create-errors", markup=False)
            yield Button(
                "Скопировать адрес", id="create-copy", variant="primary", disabled=True
            )
            with Horizontal(id="create-actions"):
                yield Button("Создать сервер", id="create-submit", variant="primary")
                yield Button("Версии", id="create-versions")
                yield Button("Закрыть", id="create-close")

    def on_mount(self):
        self.query_one("#create-address").display = False
        self.query_one("#create-copy").display = False

    def progress(self, message):
        self.app.call_from_thread(
            lambda: self.query_one("#create-progress", Static).update(message)
        )

    @on(Button.Pressed, "#create-submit")
    def submit(self):
        if self.creating:
            return
        try:
            options = CreateOptions(
                name=self.query_one("#create-name", Input).value,
                version=self.query_one("#create-version", Input).value.strip(),
                loader=self.query_one("#create-loader", Select).value,
                online_mode=self.query_one("#create-mode", Select).value == "license",
                memory=self.query_one("#create-memory", Input).value.strip(),
                port=int(self.query_one("#create-port", Input).value),
                max_players=int(self.query_one("#create-players", Input).value),
                accept_eula=self.query_one("#create-eula", Checkbox).value,
                start=True,
            )
            options.validate()
        except (Error, ValueError) as exc:
            self.query_one("#create-errors", Static).update(str(exc))
            return
        self.creating = True
        self.query_one("#create-submit", Button).disabled = True
        self.query_one("#create-close", Button).disabled = True
        self.query_one("#create-versions", Button).disabled = True
        for widget in self.query("Input, Select, Checkbox"):
            widget.disabled = True
        self.run_create(options)

    @work(group="create")
    async def run_create(self, options):
        try:
            self.report = await asyncio.to_thread(
                create, options, self.paths, self.progress
            )
            await self.query_one("#create-fields").remove()
            self.query_one("#create-note").display = False
            self.query_one("#create-eula").display = False
            self.query_one("#create-submit").display = False
            self.query_one("#create-versions").display = False
            if hasattr(self.app, "select_profile") and self.paths.file.exists():
                await self.app.select_profile(self.paths.profile)
            address = self.report["address"]
            self.query_one("#create-progress", Static).update(
                "Внешний вход Minecraft подтверждён."
                if self.report["verified"]
                else "Доступность извне НЕ подтверждена."
            )
            self.query_one("#create-address", Static).update(
                address or "Публичный IPv4 не определён"
            )
            self.query_one("#create-address").display = True
            self.query_one("#create-errors", Static).update(
                "\n".join(self.report["errors"] + self.report["warnings"])
            )
            button = self.query_one("#create-copy", Button)
            button.display = True
            button.disabled = not bool(address)
        except Exception as exc:
            self.query_one("#create-errors", Static).update(
                str(exc)
                if isinstance(exc, Error)
                else f"{type(exc).__name__}: создание не завершено. Проверьте права и журнал сервиса."
            )
            for widget in self.query("Input, Select, Checkbox"):
                widget.disabled = False
            self.query_one("#create-submit", Button).disabled = False
            self.query_one("#create-versions", Button).disabled = False
        finally:
            self.creating = False
            self.query_one("#create-close", Button).disabled = False

    @on(Button.Pressed, "#create-copy")
    def copy_address(self):
        if self.report and self.report["address"]:
            self.copy_result(self.report["address"])

    @work(group="copy")
    async def copy_result(self, value):
        try:
            self.notify(await asyncio.to_thread(network.copy, value))
        except Error as exc:
            self.notify(str(exc), severity="warning")

    @on(Button.Pressed, "#create-close")
    def close(self):
        if not self.creating:
            self.dismiss()

    @on(Button.Pressed, "#create-versions")
    def versions(self):
        from .version_ui import VersionPicker

        def selected(version):
            if version:
                self.query_one("#create-version", Input).value = version

        self.app.push_screen(
            VersionPicker(self.query_one("#create-loader", Select).value), selected
        )
