import tomllib

from textual.theme import Theme

PALETTES = {
    "uwu-dark": (
        "#c4a7e7",
        "#0f111a",
        "#191c28",
        "#a6da95",
        "#eed49f",
        "#ed8796",
        "#91d7e3",
        "#a5adcb",
        True,
    ),
    "opencode-dark": (
        "#fab283",
        "#141414",
        "#1e1e1e",
        "#7fd88f",
        "#e5c890",
        "#e78284",
        "#8caaee",
        "#a0a0a0",
        True,
    ),
    "catppuccin-mocha": (
        "#cba6f7",
        "#1e1e2e",
        "#313244",
        "#a6e3a1",
        "#f9e2af",
        "#f38ba8",
        "#89b4fa",
        "#a6adc8",
        True,
    ),
    "tokyo-night": (
        "#bb9af7",
        "#1a1b26",
        "#24283b",
        "#9ece6a",
        "#e0af68",
        "#f7768e",
        "#7aa2f7",
        "#a9b1d6",
        True,
    ),
    "gruvbox-dark": (
        "#d79921",
        "#282828",
        "#3c3836",
        "#b8bb26",
        "#fabd2f",
        "#fb4934",
        "#83a598",
        "#a89984",
        True,
    ),
    "nord": (
        "#88c0d0",
        "#2e3440",
        "#3b4252",
        "#a3be8c",
        "#ebcb8b",
        "#bf616a",
        "#81a1c1",
        "#d8dee9",
        True,
    ),
    "dracula": (
        "#bd93f9",
        "#282a36",
        "#44475a",
        "#50fa7b",
        "#f1fa8c",
        "#ff5555",
        "#8be9fd",
        "#b0b3c5",
        True,
    ),
    "solarized-dark": (
        "#b58900",
        "#002b36",
        "#073642",
        "#859900",
        "#cb4b16",
        "#dc322f",
        "#268bd2",
        "#93a1a1",
        True,
    ),
    "solarized-light": (
        "#6c71c4",
        "#fdf6e3",
        "#eee8d5",
        "#657b00",
        "#b58900",
        "#dc322f",
        "#268bd2",
        "#586e75",
        False,
    ),
    "monochrome": (
        "#ffffff",
        "#111111",
        "#252525",
        "#eeeeee",
        "#cccccc",
        "#ffffff",
        "#dddddd",
        "#aaaaaa",
        True,
    ),
}


def theme(name, values):
    accent, background, surface, success, warning, error, info, muted, dark = values
    return Theme(
        name=name,
        primary=accent,
        secondary=info,
        accent=accent,
        foreground=muted,
        background=background,
        surface=surface,
        panel=surface,
        success=success,
        warning=warning,
        error=error,
        dark=dark,
        variables={
            "border": surface,
            "muted": muted,
            "info": info,
            "footer-key-foreground": accent,
        },
    )


def load_custom(directory):
    result = []
    for file in directory.glob("*.toml"):
        data = tomllib.loads(file.read_text(encoding="utf-8"))
        colors = data["colors"]
        result.append(
            theme(
                file.stem,
                tuple(
                    colors[key]
                    for key in [
                        "accent",
                        "background",
                        "surface",
                        "success",
                        "warning",
                        "error",
                        "info",
                        "muted",
                    ]
                )
                + (data.get("dark", True),),
            )
        )
    return result
