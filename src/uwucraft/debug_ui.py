import asyncio
import json

from textual import on, work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, TextArea

from . import network
from .diagnostics import debug_report


class DebugScreen(ModalScreen):
    DEFAULT_CSS = """
    DebugScreen { align: center middle; }
    #debug-dialog { width: 92%; height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    #debug-report { height: 1fr; margin: 1 0; }
    #debug-actions { height: 3; }
    """
    BINDINGS = [("escape", "close", "Закрыть")]

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.report = ""

    def compose(self):
        with Vertical(id="debug-dialog"):
            yield Input(
                placeholder="Необязательно: папка Minecraft клиента для сравнения модов",
                id="debug-client",
            )
            yield Checkbox(
                "Независимая внешняя проверка Minecraft", id="debug-external"
            )
            yield TextArea(read_only=True, id="debug-report")
            with Horizontal(id="debug-actions"):
                yield Button("Проверить", id="debug-refresh", variant="primary")
                yield Button("Скопировать отчёт", id="debug-copy")
                yield Button("Закрыть", id="debug-close")

    def on_mount(self):
        self.collect()

    def action_close(self):
        self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == "debug-close":
            self.action_close()
        elif event.button.id == "debug-refresh":
            self.collect()
        elif event.button.id == "debug-copy":
            if self.report:
                try:
                    self.notify(network.copy(self.report))
                except Exception as exc:
                    self.notify(str(exc), severity="error")

    @work(exclusive=True)
    async def collect(self):
        field = self.query_one("#debug-report", TextArea)
        self.query_one("#debug-refresh", Button).disabled = True
        field.text = "Проверка модов, JVM и relay…"
        try:
            report = await asyncio.to_thread(
                debug_report,
                self.paths,
                self.query_one("#debug-client", Input).value.strip() or None,
                self.query_one("#debug-external", Checkbox).value,
            )
            self.report = json.dumps(report, ensure_ascii=False, indent=2)
            field.text = self.report
        except Exception as exc:
            field.text = "Диагностика не завершена: " + str(exc)
        finally:
            if self.is_mounted:
                self.query_one("#debug-refresh", Button).disabled = False
