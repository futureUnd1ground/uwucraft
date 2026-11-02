"""Terminal file-drop area. No OS drag protocol is assumed or simulated."""

import asyncio

from textual import on, work
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Static, TextArea

from . import local_content, server
from .config import Error


class DropFiles(ModalScreen):
    DEFAULT_CSS = """
    DropFiles { align: center middle; }
    #drop-dialog { width: 88%; max-width: 110; height: 85%; border: round $accent; padding: 1 2; background: $surface; overflow-y: auto; }
    #drop-paths { height: 7; margin: 1 0; }
    #drop-result { height: 1fr; overflow-y: auto; }
    #drop-actions { height: 3; }
    """

    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.installing = False

    def compose(self):
        with Vertical(id="drop-dialog"):
            yield Label(
                f"Установить файлы: {self.paths.load().name} [{self.paths.profile}]"
            )
            yield Static(
                "Остановите сервер. Перетащите один или несколько файлов сюда либо вставьте пути. "
                "Терминал должен передавать перетаскиваемые файлы как текстовые пути.\n"
                "JAR: моды/плагины; ZIP: datapack, resourcepack, server pack или мир; MRPACK: модпак. "
                "Существующие файлы не перезаписываются."
            )
            yield Static(id="drop-server-status", markup=False)
            yield TextArea(
                id="drop-paths",
                placeholder="Пути файлов — в кавычках или по одному на строку",
            )
            yield Checkbox(
                "Доверяю этим файлам и проверил совместимость/зависимости",
                id="drop-trust",
            )
            yield Static(
                "Тип определяется по содержимому. JAR и скрипты могут выполнять код.\n"
                "Resourcepack копируется в resourcepacks: раздача игрокам требует HTTPS URL. "
                "Мир копируется в worlds/<имя>; текущий мир автоматически не переключается.",
                id="drop-result",
                markup=False,
            )
            with Horizontal(id="drop-actions"):
                yield Button("Остановить сервер", id="drop-stop", variant="warning")
                yield Button("Проверить список", id="drop-preview")
                yield Button("Установить всё", id="drop-install", variant="primary")
                yield Button("Закрыть", id="drop-close")

    def on_mount(self):
        self.refresh_server()
        self.set_interval(1, self.refresh_server)

    def refresh_server(self):
        state = server.status(self.paths)["status"]
        stopped = state in {"Stopped", "Crashed"}
        self.query_one("#drop-server-status", Static).update(
            f"Сервер: {state}. "
            + (
                "Можно устанавливать файлы."
                if stopped
                else "Сначала остановите JVM; мир будет сохранён."
            )
        )
        self.query_one("#drop-stop", Button).disabled = self.installing or stopped
        self.query_one("#drop-install", Button).disabled = (
            self.installing or not stopped
        )

    @work(exclusive=True)
    async def stop_server(self):
        self.installing = True
        for button in self.query(Button):
            button.disabled = True
        self.query_one("#drop-result", Static).update(
            "Остановка JVM и сохранение мира…"
        )
        try:
            await asyncio.to_thread(server.manage, "stop", self.paths)
            self.query_one("#drop-result", Static).update(
                "Сервер остановлен. Можно установить файлы."
            )
        except (Error, OSError, ValueError) as exc:
            self.query_one("#drop-result", Static).update(str(exc))
        finally:
            self.installing = False
            for button in self.query(Button):
                button.disabled = False
            self.refresh_server()

    BINDINGS = [("escape", "close", "Закрыть")]

    def action_close(self):
        if not self.installing:
            self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == "drop-close":
            if not self.installing:
                self.dismiss(None)
            return
        if self.installing:
            return
        if event.button.id == "drop-stop":
            self.stop_server()
            return
        if event.button.id in {"drop-preview", "drop-install"}:
            try:
                sources = local_content.dropped_paths(
                    self.query_one("#drop-paths", TextArea).text
                )
                if (
                    event.button.id == "drop-install"
                    and not self.query_one("#drop-trust", Checkbox).value
                ):
                    raise Error("Подтвердите доверие файлам и их совместимость.")
            except Error as exc:
                self.query_one("#drop-result", Static).update(str(exc))
                return
            self.process(sources, event.button.id == "drop-install")

    @work(exclusive=True)
    async def process(self, sources, install):
        self.installing = True
        for button in self.query(Button):
            button.disabled = True
        self.query_one("#drop-result", Static).update(
            "Проверка и установка…" if install else "Проверка файлов…"
        )
        try:
            if install:
                result = await asyncio.to_thread(
                    local_content.install_many, sources, True, self.paths
                )
                text = (
                    "Установлено:\n"
                    + "\n".join(result)
                    + "\nЗапустите сервер, чтобы применить изменения."
                )
            else:
                result = await asyncio.to_thread(
                    local_content.plan, sources, self.paths
                )
                text = "\n".join(f"{item['name']} → {item['kind']}" for item in result)
            if install:
                if any(line.endswith("→ resourcepack") for line in result):
                    text += "\nResourcepack не раздаётся автоматически: настройте публичный HTTPS URL."
                if any(line.endswith("→ world") for line in result):
                    text += "\nТекущий мир сохранён; для переключения измените level-name отдельно."
            self.query_one("#drop-result", Static).update(text)
        except (Error, OSError, ValueError) as exc:
            self.query_one("#drop-result", Static).update(str(exc))
        finally:
            self.installing = False
            for button in self.query(Button):
                button.disabled = False
            self.refresh_server()
