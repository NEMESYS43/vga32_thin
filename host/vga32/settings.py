"""Persistent desktop settings in ~/.vga32/settings.json (override the folder with VGA32_HOME).

Settings are a flat-ish dict merged over DEFAULTS, so a new key never breaks an old file.
"""
import copy
import json
import os

HOME = os.path.abspath(os.path.expanduser(os.environ.get("VGA32_HOME", "~/.vga32")))
APPS_DIR = os.path.join(HOME, "apps")
FONTS_DIR = os.path.join(HOME, "fonts")
PATH = os.path.join(HOME, "settings.json")

DEFAULTS = {
    "theme": "classic",                 # classic | platinum
    "desktop": {
        "background": None,             # None = the theme's own color
        "pattern": True,
        "caption": "Right-click the desktop for options",
        "wallpaper": "",                # image path or URL, dithered onto the palette
        "wallpaper_mode": "fill",       # fill | fit | center | tile
        "icons": True,                  # show desktop icons
    },
    "clock_24h": True,
    "adaptive_palette": True,           # 3D / media apps that ask for it get the best 16 of 64 colors while focused
    "icons": {},                        # app id -> [x, y] desktop icon position
    "hidden_apps": [],                  # app ids left out of menus and the desktop
    "dock": ["files", "web", "terminal", "notes", "assistant", "settings"],    # Aqua Dock, left to right
    "assistant": {
        "url": "http://127.0.0.1:11434/v1",     # any OpenAI-compatible endpoint (Ollama, llama.cpp, LM Studio)
        "model": "llama3.1",
        "api_key": "",
        "system": "",
    },
    # Scoped API tokens for agents, besides the HOST_KEY (which can do everything):
    # {"name": "hermes", "token": "...", "scopes": ["ui", "input", "terminal", "web", "admin"]}
    "tokens": [],
    "webhooks": [],                     # URLs that receive every desktop event as JSON (POST)
    "boards": [],                       # saved ESP32 VGA board hosts ("ip" or "ip:port") for multi-device management
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path=PATH):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as f:
                self.data = _merge(DEFAULTS, json.load(f))
        except (OSError, ValueError):
            pass

    def __getitem__(self, k):
        return self.data[k]

    def get(self, dotted, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted, value, save=True):
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        if save:
            self.save()

    def update(self, values, save=True):
        self.data = _merge(self.data, values)
        if save:
            self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def public(self):
        """Settings without secrets, for agents and the web remote."""
        d = copy.deepcopy(self.data)
        d["tokens"] = [{"name": t.get("name"), "scopes": t.get("scopes", [])} for t in d.get("tokens", [])]
        if d.get("assistant", {}).get("api_key"):
            d["assistant"]["api_key"] = "***"
        return d
