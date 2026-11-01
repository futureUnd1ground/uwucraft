"""An official-version catalog, shared with the versions CLI."""

import asyncio

from textual import on, work
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from .config import Error
from .sources import available_versions


class VersionPicker(ModalScreen):
    DEFAULT_CSS = """
    VersionPicker {align: center middle; background: $background 85%;}
    #version-dialog {width: 65; max-width: 95%; height: auto; max-height: 95%; padding: 1 2; border: round $accent; background: $surface;}
    #version-list {height: 18;}
    #version-message {height: auto;}
    """

    def __init__(self, loader):
        super().__init__()
        self.loader = loader
        self.versions = []

    def compose(self):
        with Vertical(id="version-dialog"):
            yield Static("Версии Minecraft · " + self.loader.title())
            yield Input(placeholder="Фильтр: например 1.20", id="version-search")
            yield Static(
                "Загрузка официального каталога…", id="version-message", markup=False
            )
            yield ListView(id="version-list")
            yield Button("Закрыть", id="version-cancel")

    def on_mount(self):
        self.load_versions()

    @work
    async def load_versions(self):
        try:
            self.versions = await asyncio.to_thread(available_versions, self.loader)
            await self.filter_versions()
        except Error as exc:
            self.query_one("#version-message", Static).update(str(exc))

    async def filter_versions(self):
        query = self.query_one("#version-search", Input).value.strip()
        values = [v for v in self.versions if query in v]
        view = self.query_one("#version-list", ListView)
        await view.clear()
        await view.extend([ListItem(Label(v), name=v) for v in values[:200]])
        self.query_one("#version-message", Static).update(
            f"Найдено: {len(values)}. Список опубликованных версий; стабильный build проверяется при установке."
        )

    @on(Input.Changed, "#version-search")
    async def search(self):
        if self.versions:
            await self.filter_versions()

    @on(ListView.Selected, "#version-list")
    def choose(self, event):
        self.dismiss(event.item.name)

    @on(Button.Pressed, "#version-cancel")
    def close(self):
        self.dismiss(None)
