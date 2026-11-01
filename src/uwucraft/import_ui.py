"""Import wizard: recognize an existing server, correct the guess, register it.

The terminal UI cannot show a native directory picker, so the directory is
chosen either by typing its path into the text field or by picking one of
the candidates found by the bounded discover scan.
"""

import asyncio
import time

from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from . import importer, network
from .config import Error
from .installers import LOADERS
from .platforms import windows


class ImportServer(ModalScreen):
    DEFAULT_CSS = """
    ImportServer { align: center middle; background: $background 85%; }
    #import-dialog { width: 92; max-width: 98%; height: auto; max-height: 95%; padding: 1 2; border: round $accent; background: $surface; }
    #import-fields { layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 0 2; height: auto; }
    .import-field { height: 5; }
    .import-field Label { height: 1; }
    .import-field Input, .import-field Select { margin: 0; }
    #import-path { margin-bottom: 1; }
    #import-actions-top { height: 3; margin-bottom: 1; }
    #import-found { margin-bottom: 1; }
    #import-summary { height: auto; border: round $primary 35%; padding: 1 2; margin-bottom: 1; }
    #import-copy-flag { height: auto; margin: 1 0; }
    #import-note, #import-progress, #import-errors { height: auto; margin: 1 0; }
    #import-address { text-style: bold; color: $accent; height: auto; padding: 1; border: round $accent; }
    #import-copy { width: 100%; height: 5; margin: 1 0; }
    #import-actions { height: 3; }
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, discover=False):
        super().__init__()
        self.detected = None
        self.report = None
        self.working = False
        self.autodiscover = discover
        self._progress_text = ""
        self._pulse_timer = None
        self._started_at = 0.0
        self._pulse_frame = 0

    def compose(self):
        with VerticalScroll(id="import-dialog"):
            yield Static("ИМПОРТ СУЩЕСТВУЮЩЕГО СЕРВЕРА", classes="heading")
            yield Input(
                placeholder="Путь к каталогу сервера, например ~/minecraft-server",
                id="import-path",
            )
            with Horizontal(id="import-actions-top"):
                yield Button("Распознать", id="import-scan")
                yield Button("Найти серверы", id="import-discover")
            yield Select(
                [("Сначала нажмите «Найти серверы»", "")],
                value="",
                allow_blank=False,
                disabled=True,
                id="import-found",
            )
            yield Static(
                "Укажите каталог и нажмите «Распознать». Файлы сервера не изменяются.",
                id="import-summary",
                markup=False,
            )
            with Vertical(id="import-fields"):
                for label, control in [
                    ("Название", Input(id="import-name")),
                    (
                        "Ядро",
                        Select(
                            [(name.title(), name) for name in LOADERS],
                            value="paper",
                            allow_blank=False,
                            id="import-loader",
                        ),
                    ),
                    (
                        "Minecraft",
                        Input(
                            placeholder="Не определена — укажите вручную",
                            id="import-version",
                        ),
                    ),
                    (
                        "RAM",
                        Input(
                            placeholder="Авто: из скриптов запуска или 2G",
                            id="import-memory",
                        ),
                    ),
                ]:
                    with Vertical(
                        classes="import-field"
                        + (" version-field" if label == "Minecraft" else "")
                    ):
                        yield Label(label)
                        yield control
                        if label == "Minecraft":
                            yield Button("Выбрать версию", id="import-versions")
            yield Checkbox(
                "Скопировать сервер в каталог uwucraft (по умолчанию управление на месте, без копирования)",
                id="import-copy-flag",
            )
            yield Static(
                (
                    "Windows: сервер подключается как задача автозапуска после входа.\n"
                    if windows()
                    else "Миры, моды, конфиги и server.properties не перезаписываются. Для firewall sudo запросит пароль сам.\n"
                )
                + "Если ядро или версия не распознаны — исправьте их вручную до импорта.",
                id="import-note",
            )
            yield Static("", id="import-progress", markup=False)
            yield Static("", id="import-address", markup=False)
            yield Static("", id="import-errors", markup=False)
            yield Button(
                "Скопировать адрес", id="import-copy", variant="primary", disabled=True
            )
            with Horizontal(id="import-actions"):
                yield Button("Импортировать", id="import-submit", variant="primary")
                yield Button("Закрыть", id="import-close")

    def on_mount(self):
        self.query_one("#import-address").display = False
        self.query_one("#import-copy").display = False
        if self.autodiscover:
            self.query_one("#import-found", Select).disabled = False
            self.run_discover()

    def progress(self, message):
        self._progress_text = message
        self.app.call_from_thread(
            lambda: self.query_one("#import-progress", Static).update(message)
        )

    def _start_pulse(self):
        self._started_at = time.monotonic()
        self._pulse_timer = self.set_interval(0.15, self._pulse)

    def _stop_pulse(self):
        if self._pulse_timer is not None:
            self._pulse_timer.stop()
            self._pulse_timer = None

    def _pulse(self):
        # Tarring a large world can stay silent for minutes: keep a heartbeat
        # and elapsed time on screen so import never looks frozen.
        if not self.working:
            return
        self._pulse_frame = (self._pulse_frame + 1) % len(self.FRAMES)
        elapsed = int(time.monotonic() - self._started_at)
        text = f"{self._progress_text}  {self.FRAMES[self._pulse_frame]}  · {elapsed} с"
        try:
            self.query_one("#import-progress", Static).update(text)
        except Exception:
            self._stop_pulse()

    def _fill(self, detected):
        self.detected = detected
        self.query_one("#import-summary", Static).update(importer.summarize(detected))
        self.query_one("#import-name", Input).value = detected.directory.name
        if detected.loader in LOADERS:
            self.query_one("#import-loader", Select).value = detected.loader
        self.query_one("#import-version", Input).value = detected.version
        self.query_one("#import-memory", Input).value = detected.memory
        self.query_one("#import-errors", Static).update("")

    @work(group="import-scan", exclusive=True)
    async def run_scan(self):
        path = self.query_one("#import-path", Input).value.strip()
        try:
            detected = await asyncio.to_thread(importer.scan, path)
        except (Error, OSError) as exc:
            self.detected = None
            self.query_one("#import-errors", Static).update(str(exc))
            return
        self._fill(detected)

    @work(group="import-scan", exclusive=True)
    async def run_discover(self):
        select = self.query_one("#import-found", Select)
        select.disabled = False
        try:
            found = await asyncio.to_thread(importer.discover)
        except (Error, OSError) as exc:
            self.query_one("#import-errors", Static).update(str(exc))
            return
        if not found:
            select.set_options([("Серверы не найдены в типичных каталогах", "")])
            return
        select.set_options(
            [
                (
                    f"{item.directory} · {item.loader} {item.version or '?'}",
                    str(item.directory),
                )
                for item in found
            ]
        )

    @on(Button.Pressed, "#import-scan")
    def scan_button(self):
        if not self.working:
            self.run_scan()

    @on(Button.Pressed, "#import-discover")
    def discover_button(self):
        if not self.working:
            self.run_discover()

    @on(Select.Changed, "#import-found")
    def picked(self, event):
        if event.value and isinstance(event.value, str):
            self.query_one("#import-path", Input).value = event.value
            self.run_scan()

    @on(Button.Pressed, "#import-versions")
    def versions(self):
        from .version_ui import VersionPicker

        def selected(version):
            if version:
                self.query_one("#import-version", Input).value = version

        self.app.push_screen(
            VersionPicker(self.query_one("#import-loader", Select).value), selected
        )

    @on(Button.Pressed, "#import-submit")
    def submit(self):
        if self.working:
            return
        if self.detected is None:
            self.query_one("#import-errors", Static).update(
                "Сначала укажите каталог и нажмите «Распознать»."
            )
            return
        loader = self.query_one("#import-loader", Select).value
        if loader not in LOADERS:
            self.query_one("#import-errors", Static).update(
                "Выберите ядро из списка: " + ", ".join(LOADERS)
            )
            return
        self.working = True
        for widget in self.query("Input, Select, Checkbox, Button"):
            if widget.id not in {"import-close", "import-copy"}:
                widget.disabled = True
        self._start_pulse()
        self.run_import()

    @work(group="import")
    async def run_import(self):
        detected = self.detected
        kwargs = {
            "copy": self.query_one("#import-copy-flag", Checkbox).value,
            "name": self.query_one("#import-name", Input).value.strip() or None,
            "loader": self.query_one("#import-loader", Select).value,
            "version": self.query_one("#import-version", Input).value.strip() or None,
            "memory": self.query_one("#import-memory", Input).value.strip() or None,
        }
        try:
            if not windows():
                from . import privilege

                if not privilege.sudo_cached():
                    problem = privilege.ensure_via_system(
                        self.app, "настройка firewall при импорте"
                    )
                    if problem:
                        self.query_one("#import-errors", Static).update(problem)
            self.report = await asyncio.to_thread(
                lambda: importer.register(detected, progress=self.progress, **kwargs)
            )
        except Exception as exc:
            self._stop_pulse()
            self.query_one("#import-errors", Static).update(
                str(exc)
                if isinstance(exc, Error)
                else f"{type(exc).__name__}: импорт не завершён. Проверьте каталог и права."
            )
            for widget in self.query("Input, Select, Checkbox, Button"):
                if widget.id != "import-copy":
                    widget.disabled = False
            self.working = False
            return
        try:
            self.query_one("#import-fields").remove()
            self.query_one("#import-note").display = False
            self.query_one("#import-copy-flag").display = False
            self.query_one("#import-submit").display = False
            self.query_one("#import-found").display = False
            self.query_one("#import-actions-top").display = False
            self.query_one("#import-path").display = False
        except Exception:
            pass
        if hasattr(self.app, "select_profile"):
            from .profiles import select

            try:
                await self.app.select_profile(select(self.report["profile"]).profile)
            except Error:
                pass
        address = self.report.get("address", "")
        self.query_one("#import-progress", Static).update(
            "Внешний вход Minecraft подтверждён."
            if self.report.get("verified")
            else "Доступность извне НЕ подтверждена; подробности — в Network."
        )
        self.query_one("#import-address", Static).update(
            address or "Публичный IPv4 не определён"
        )
        self.query_one("#import-address").display = True
        self.query_one("#import-errors", Static).update(
            "\n".join(self.report.get("errors", []) + self.report.get("warnings", []))
        )
        button = self.query_one("#import-copy", Button)
        button.display = True
        button.disabled = not bool(address)
        self._stop_pulse()
        self.working = False

    @on(Button.Pressed, "#import-copy")
    def copy_address(self):
        if self.report and self.report.get("address"):
            self.copy_result(self.report["address"])

    @work(group="copy")
    async def copy_result(self, value):
        try:
            self.notify(await asyncio.to_thread(network.copy, value))
        except Error as exc:
            self.notify(str(exc), severity="warning")

    @on(Button.Pressed, "#import-close")
    def close(self):
        if not self.working:
            self.dismiss()
