"""The app registry: everything that shows up in the Start/system menu and on the desktop.

Apps come from three places:
  * built-ins (Terminal, Web, Notes...), registered in desktop.py
  * Python plugins: ~/.vga32/apps/*.py. Any App subclass in the module with an `app_id`
    is registered; the module may also define register(registry) to add manifests itself.
  * manifests: ~/.vga32/apps/*.json, or register_app from an agent (persist: true writes one).

A manifest:
  {"id": "hermes.weather", "title": "Weather", "icon": "cloud", "category": "Agents",
   "kind": "panel" | "web" | "browser" | "command" | "python",
   "owner": "hermes", "size": [w, h], "single_instance": true, "desktop": true,
   "spec": {"background": "navy", "elements": [...]},      # panel: what shows before the owner draws
   "menu": [{"id": "refresh", "label": "Refresh"}],        # panel: right-click / menu-bar items
   "url": "...", "command": "...",                          # web/browser, command
   "webhook": "http://...",                                 # receives this app's events (POST JSON)
   "persist": true}
Launching a panel app publishes app.launched {app_id, window_id}; the owner then draws it with
the panel tool and listens with wait_events.
"""
import glob
import importlib.util
import json
import os
import re
import sys
import traceback

from .settings import APPS_DIR
from .ui import GLYPHS, App

KINDS = ("builtin", "python", "panel", "web", "browser", "command")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
MANIFEST_KEYS = {"id", "title", "icon", "category", "kind", "owner", "size", "single_instance", "desktop",
                 "spec", "menu", "url", "command", "webhook", "persist", "description", "hidden"}


def assets_dir(module_file):
    """The asset folder of an installed app: ~/.vga32/apps/<name>/ for ~/.vga32/apps/<name>.py.
    Use it in an app as  ASSETS = assets_dir(__file__)  then os.path.join(ASSETS, "wall.png")."""
    return os.path.splitext(os.path.abspath(module_file))[0]


class AppRegistry:
    def __init__(self):
        self.entries = {}          # id -> entry dict; "cls" for builtin/python
        self.errors = []           # plugin load problems, shown in get_state

    def add_builtin(self, app_id, cls, **kw):
        cls.app_id = app_id
        self.entries[app_id] = {"id": app_id, "title": cls.title, "icon": getattr(cls, "icon", "app"),
                                "category": getattr(cls, "category", "Accessories"), "kind": "builtin",
                                "cls": cls, "owner": "system", "desktop": False, **kw}

    def register(self, manifest, persist=None):
        m = {k: v for k, v in dict(manifest).items() if k in MANIFEST_KEYS}
        unknown = set(manifest) - MANIFEST_KEYS
        if unknown:
            raise ValueError(f"unknown manifest field(s): {', '.join(sorted(unknown))}")
        app_id = str(m.get("id", ""))
        if not ID_RE.match(app_id):
            raise ValueError("id must be 1-64 chars of letters, digits, '.', '_' or '-'")
        old = self.entries.get(app_id)
        if old and old["kind"] in ("builtin", "python") and old.get("owner") == "system":
            raise ValueError(f"{app_id!r} is a built-in app")
        kind = m.setdefault("kind", "panel")
        if kind not in ("panel", "web", "browser", "command"):
            raise ValueError("kind must be panel, web, browser or command (python apps are .py plugins)")
        if kind in ("web", "browser") and not m.get("url"):
            raise ValueError(f"a {kind} app needs url")
        if kind == "command" and not m.get("command"):
            raise ValueError("a command app needs command")
        m.setdefault("title", app_id)
        m.setdefault("category", "Agents")
        m.setdefault("icon", {"panel": "panel", "web": "web", "browser": "browser", "command": "terminal"}[kind])
        if m["icon"] not in GLYPHS:
            m["icon"] = "app"
        m.setdefault("desktop", False)
        if persist is not None:
            m["persist"] = bool(persist)
        self.entries[app_id] = m
        if m.get("persist"):
            self._save(m)
        elif old and old.get("persist"):
            self._delete_file(app_id)
        return self.public(m)

    def unregister(self, app_id):
        e = self.entries.get(app_id)
        if e is None:
            raise KeyError(f"no app {app_id!r}")
        if e["kind"] == "builtin":
            raise ValueError(f"{app_id!r} is a built-in app")
        if e["kind"] == "python":
            path = e.get("file", "")
            if os.path.dirname(os.path.abspath(path)) != os.path.abspath(APPS_DIR):
                raise ValueError(f"{app_id!r} comes from {path}; delete that file to remove it")
            for other in [k for k, v in self.entries.items() if v.get("file") == path]:
                del self.entries[other]
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            del self.entries[app_id]
            self._delete_file(app_id)
        self.forget_content(app_id)

    # ----- python plugins installed at runtime -----
    def install(self, name, source, assets=None):
        """Write ~/.vga32/apps/<name>.py (+ assets into ~/.vga32/apps/<name>/) and (re)load it.
        assets: {relative file name: base64 data, "text:..." or an http(s) URL to download}.
        Returns (app ids, error or None)."""
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", name):
            raise ValueError("name must be a Python identifier (letters, digits, _), e.g. snake_game")
        os.makedirs(APPS_DIR, exist_ok=True)
        path = os.path.join(APPS_DIR, name + ".py")
        compile(source, path, "exec")                  # syntax errors raise here, before anything is saved
        if assets:
            self._write_assets(os.path.join(APPS_DIR, name), assets)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        return self.reload_plugin(path)

    @staticmethod
    def _write_assets(folder, assets):
        import base64
        import urllib.request
        root = os.path.abspath(folder)
        for rel, data in assets.items():
            dest = os.path.abspath(os.path.join(root, rel))
            if not dest.startswith(root + os.sep):
                raise ValueError(f"asset path {rel!r} leaves the app folder")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if isinstance(data, str) and data.startswith(("http://", "https://")):
                req = urllib.request.Request(data, headers={"User-Agent": "vga32"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    blob = r.read(64 << 20)
            elif isinstance(data, str) and data.startswith("text:"):
                blob = data[5:].encode("utf-8")
            else:
                blob = base64.b64decode(data.split(",", 1)[1] if str(data).startswith("data:") else data)
            with open(dest, "wb") as f:
                f.write(blob)

    def reload_plugin(self, path):
        path = os.path.abspath(path)
        for k in [k for k, v in self.entries.items() if v.get("file") == path]:
            del self.entries[k]
        n = len(self.errors)
        self.load_plugin(path)
        err = self.errors[n] if len(self.errors) > n else None
        if err:
            del self.errors[n:]
        ids = [k for k, v in self.entries.items() if v.get("file") == path]
        return ids, err

    def source(self, app_id):
        e = self.get(app_id)
        if e["kind"] != "python" or not e.get("file"):
            raise ValueError(f"{app_id!r} is not a Python plugin")
        with open(e["file"], encoding="utf-8") as f:
            return e["file"], f.read()

    # ----- saved panel content, so a persistent app reopens as it was last drawn -----
    def _content_path(self, app_id):
        return os.path.join(APPS_DIR, "state", app_id + ".json")

    def save_content(self, app_id, spec):
        os.makedirs(os.path.dirname(self._content_path(app_id)), exist_ok=True)
        with open(self._content_path(app_id), "w", encoding="utf-8") as f:
            json.dump(strip_private(spec), f)

    def load_content(self, app_id):
        try:
            with open(self._content_path(app_id), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def forget_content(self, app_id):
        try:
            os.remove(self._content_path(app_id))
        except OSError:
            pass

    def _path(self, app_id):
        return os.path.join(APPS_DIR, app_id + ".json")

    def _save(self, m):
        os.makedirs(APPS_DIR, exist_ok=True)
        clean = {k: v for k, v in m.items() if k != "spec"} | \
            ({"spec": strip_private(m["spec"])} if m.get("spec") else {})
        with open(self._path(m["id"]), "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)

    def _delete_file(self, app_id):
        try:
            os.remove(self._path(app_id))
        except OSError:
            pass

    def load_dir(self, folder=APPS_DIR):
        for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    self.register(json.load(f), persist=None)
            except Exception as e:
                self.errors.append(f"{os.path.basename(path)}: {e}")
        for path in sorted(glob.glob(os.path.join(folder, "*.py"))):
            self.load_plugin(path)

    def load_plugin(self, path):
        name = "vga32_app_" + re.sub(r"\W", "_", os.path.splitext(os.path.basename(path))[0])
        try:
            path = os.path.abspath(path)
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            for obj in vars(mod).values():
                if isinstance(obj, type) and issubclass(obj, App) and obj is not App and \
                        getattr(obj, "app_id", None) and obj.__module__ == name:
                    self.entries[obj.app_id] = {"id": obj.app_id, "title": obj.title, "icon": obj.icon,
                                                "category": obj.category, "kind": "python", "cls": obj,
                                                "owner": "plugin", "desktop": getattr(obj, "desktop", False),
                                                "file": path}
            if hasattr(mod, "register"):
                mod.register(self)
        except Exception:
            self.errors.append(f"{os.path.basename(path)}: {traceback.format_exc(limit=2).strip()}")

    def get(self, app_id):
        e = self.entries.get(app_id) or self.entries.get(str(app_id).lower())
        if e is None:
            raise ValueError(f"unknown app {app_id!r}; choose from {', '.join(self.entries)}")
        return e

    def visible(self, hidden=()):
        return [e for e in self.entries.values() if not e.get("hidden") and e["id"] not in hidden]

    def categories(self, hidden=()):
        out = {}
        for e in self.visible(hidden):
            out.setdefault(e.get("category") or "Other", []).append(e)
        return out

    @staticmethod
    def public(e):
        out = {k: v for k, v in e.items() if k not in ("cls", "spec")}
        if e.get("spec"):
            out["spec"] = strip_private(e["spec"])
        return out


def strip_private(spec):
    """A panel spec without cached images (keys starting with _)."""
    if isinstance(spec, dict):
        return {k: strip_private(v) for k, v in spec.items() if not k.startswith("_")}
    if isinstance(spec, list):
        return [strip_private(v) for v in spec]
    return spec
