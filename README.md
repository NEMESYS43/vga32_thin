# VGA32 Thin Client

The TTGO VGA32 board is a dumb terminal: a 640x400 16-color VGA screen, a PS/2 keyboard and a PS/2 mouse, connected over WiFi.
A Python program on your PC draws the screen and runs the apps.
The same program serves a web remote and an agent API (REST and MCP), so a browser tab or an AI agent such as Hermes can drive the desktop too.

## 1. Flash the board

1. Arduino IDE: install the **esp32** board package **2.0.17** and the **FabGL 1.0.9** library.
2. Copy `secrets.example.h` to `secrets.h` and set `WIFI_SSID`, `WIFI_PASS`, and `HOST_KEY` (a shared password between the PC and the board). `secrets.h` is not committed.
3. Board: **ESP32 Dev Module**. Upload, then open the Serial Monitor at 115200 baud.
4. Plug in the VGA monitor, the keyboard (port 0) and the mouse (port 1), then press reset.

The board shows a status screen with its IP address and keyboard/mouse status.
Hold the left mouse button to scribble, which confirms the mouse works.

The default resolution is 640x400 (`TC_RES_PROFILE 1`).
640x480 doesn't leave the ESP32 enough RAM for WiFi.
If the serial log ever says `only N of M lines allocated`, lower `TC_RES_PROFILE` or `WIFI_RESERVE`.

## 2. Run the host (PC on the same network)

```
cd host
pip install -r requirements.txt
python -m vga32 discover          # list boards on the LAN
python -m vga32 test              # test pattern + print key/mouse events
python -m vga32 desktop           # the desktop + web remote + agent API
python -m vga32 mirror --fps 8    # show this PC's screen on the VGA monitor (view only)
```

Discovery listens for UDP broadcasts on port 7778. If it finds nothing, allow Python through the Windows firewall or pass the IP from the status screen with `--host 192.168.x.y`.
If you changed `HOST_KEY`, pass `--key yourkey` or set the `VGA32_KEY` environment variable.

The desktop keeps running without a board and reattaches it whenever it appears.
Windows survive a board reboot, and the web remote and agents keep working in the meantime.
Use `--no-board` to run with only the web remote, for example to develop agent workflows.

## Desktop

| Action | How |
|---|---|
| App menu | **Start** (Classic) or the ◆ menu (Platinum), the Win key, or Ctrl+Esc. Apps are grouped by category |
| Context menus | Right-click the desktop, a title bar, a taskbar button, a desktop icon, or inside Files, Notes, Terminal, Assistant and agent panels |
| Move a window | Drag its title bar |
| Resize a window | Drag its bottom-right corner |
| Maximize / restore | The zoom button, or double-click the title bar (Classic) |
| Roll up (windowshade) | The shade button, or double-click the title bar (Platinum) |
| Always on top | Right-click the title bar |
| Close a window | The X / close box or Alt+F4 |
| Switch windows | Taskbar buttons, the Window menu, the app switcher (Platinum, top right) or Alt+Tab |
| Copy / paste | Edit menu, right-click, or Ctrl+Shift+C / Ctrl+Shift+V (one clipboard shared with the web remote and agents) |
| Desktop icons | Double-click to open, drag to move, right-click to remove; Settings > Apps chooses which apps get one |

### Themes and settings

**Settings** (or right-click the desktop > Desktop Settings) changes the following:

- **Theme**: Classic has a taskbar and Start menu. **Platinum** is in the style of Mac OS 9: a menu bar across the top (◆ system menu, File / Edit / app menus / Window, clock, app switcher), pinstriped title bars with a close box on the left and zoom and windowshade boxes on the right, grow box, black arrow cursor and a lavender highlight.
  - The board's DAC is RGB222, so Platinum's light grey is the 170 grey. Its palette and cursor are sent to the board when the theme is applied; no reflash is needed.
  - Drop a `.ttf` into `~/.vga32/fonts` to use it for Platinum title bars and menus (for example a Chicago-style bitmap font you have a license for). Otherwise DejaVu/Arial Bold is used.
- **Desktop**: background color, pattern, caption, desktop icons, and a **wallpaper** (image path or URL; fill, fit, center or tile, dithered onto the palette).
- **Apps**: which apps show in the menus and which get desktop icons.
- **Agents**: the Assistant's local model endpoint, webhooks and tokens.

Settings live in `~/.vga32/settings.json` (set `VGA32_HOME` to move the folder) and survive restarts.

Apps:

- **Terminal**: runs shell commands on the PC (`cd`, `cls`, Ctrl+C kills, PgUp/wheel scrolls).
- **Web**: a full browser (real Chromium: JavaScript, CSS, video, YouTube, maps) streamed to the screen (see below).
- **Browser**: a lightweight text-mode web browser with images, for reading (see below).
- **Images**: an image viewer for files on the PC, URLs, and images dropped onto the web remote. Left/Right steps through the folder, F toggles fit / 1:1 (drag or arrow keys to pan), D toggles dithering, and Ctrl+L opens a path or URL. It starts in your Pictures folder.
- **Notes**: text editor saved to `~/vga32_notes.txt` (Ctrl+S, or automatically on close).
- **Paint**: left button draws, right button erases, click the palette for a color, 1–9 or the wheel sets brush size, C clears.
- **Clock**
- **System**: CPU and RAM (needs psutil).
- **Files**: browse the PC's folders. Double-click opens folders, images (viewer), text files (Notes) and HTML (Web). Right-click: Copy Path, Use as Wallpaper, Open Terminal Here.
- **Assistant**: a chat window backed by a **local model** (Ollama, llama.cpp, LM Studio, or any OpenAI-compatible endpoint; set it in Settings > Agents). The model gets the same tools as external agents, so it can open apps, run commands and build panels on this desktop.
- **Settings**: see above.
- **Panel**: windows that agents design (dashboards, forms, menus, status boards). These are created through the API.
- Any app an agent registers, or that a plugin adds (see [Writing your own app](#writing-your-own-app)).

### Web (full browser)

The page is rendered by a real headless Chromium on the PC: your installed Edge or Chrome, driven by Playwright.
JavaScript, CSS, web apps, maps and video all work.
Its screencast frames (about 25 fps) are dithered onto the 16-color palette and streamed like any other window, and your mouse and keyboard go to the page.
There's no audio; the browser runs muted.

- Click the address bar, or press Ctrl+L or F6, to type a URL or search (DuckDuckGo).
- Back/forward: Alt+Left/Right or the < > buttons. Reload: F5 or R. Home: H.
- Ctrl+D cycles dithering:
  - `ordered` (default): a pattern dither that is stable frame to frame, so video only resends what changed.
  - `floyd`: the best quality for still pages.
  - `none`: flat nearest colors.
- Speed on the real board: a 320 px video plays at about 22 fps, and a 600 px one at about 14 fps. The limit is the board link (about 600 KB/s).
- Requires `pip install playwright`. It uses the installed Edge or Chrome; if you have neither, run `python -m playwright install chromium` once.

### Browser (text)

Pages are fetched and laid out as text on the PC, so they stay readable in 16 colors.
Images load in the background (up to 40 per page) and are dithered onto the palette.
Icons, SVGs and tiny images show their alt text instead. There's no JavaScript or CSS.
Pages open at the main content rather than the site menu; scroll up to see the menu.

- Click an image to follow its link, or to open it in the image viewer if it has none. Ctrl+I turns images on and off.
- Direct image URLs and local paths (`C:\Users\you\Pictures\cat.jpg`) open as an image page.

- Type a URL or a search in the address bar (click it, Ctrl+L or F6). Searches use DuckDuckGo Lite, falling back to Bing.
- Click links. Tab / Shift+Tab selects a link and Enter opens it.
- Scroll with Up/Down, PgUp/PgDn, Space and the wheel. Home and End jump to the top and bottom.
- Back: the < button, Alt+Left or Backspace. Forward: the > button or Alt+Right. Reload: R or F5. Find in page: Ctrl+F.

## Web remote

While `desktop` runs, it serves a page that mirrors the VGA screen and accepts mouse and keyboard input.
The URL is printed at startup: `http://<pc-ip>:8080/?key=<HOST_KEY>`.
The key is required because the Terminal app runs commands on your PC.

Click the screen to give it keyboard focus.
The OS keeps Win and Ctrl+Esc for itself, so the toolbar has **Apps menu**, **Alt+Tab**, **Alt+F4** and **Esc** buttons.
To show an image on the VGA screen, drop it onto the screen, paste it with Ctrl+V, or use **Image…**.
Photos are shrunk to 1280 px in the browser before they're sent.
Use `--web-port 0` to turn it off, or `--web-port N` to use another port.

## Agent control (Hermes and other MCP clients)

The desktop exposes one tool set two ways, both on the web port and protected by `HOST_KEY`:

- **MCP:** `POST http://<pc-ip>:8080/mcp` (Streamable HTTP, JSON responses), or `python -m vga32 mcp` as a stdio server.
- **REST:** `POST http://<pc-ip>:8080/api/<tool>` with JSON arguments, or `GET /api` for the tool list with schemas. Also `GET /api/state` and `GET /api/screenshot?scale=2`.

Send the key as `Authorization: Bearer <key>`, an `X-VGA32-Key: <key>` header, or `?key=<key>`.

**Scoped tokens.** The HOST_KEY can do everything, including running shell commands. To give an agent less, add a token to `settings.json`:

```json
"tokens": [{"name": "hermes", "token": "a-long-random-string", "scopes": ["ui", "input", "web"]}]
```

Scopes:

- `ui`: windows, panels, apps, dialogs, events.
- `input`: raw keys and mouse.
- `terminal`: shell commands.
- `web`: the Chromium browser, including JavaScript.
- `settings`: change settings.
- `admin`: change tokens and webhooks.

The web remote page still needs the HOST_KEY.

**Events.** Everything that happens is published with an increasing `seq`. Ways to receive events:

- `wait_events` (MCP/REST): long-polls, with filters.
- `GET /api/events?since=&types=panel.,menu.select`: a Server-Sent Events stream.
- `GET /mcp` with `Accept: text/event-stream`: the same events as MCP `notifications/message`.
- Webhooks: every event (or one app's events) POSTed to URLs you list in settings.

Event types:

- `app.launched`
- `window.opened`, `window.closed`, `window.focused`
- `panel.click`, `panel.submit`, `panel.change`, `panel.key`
- `menu.select`
- `dialog.result`
- `hotkey`
- `clipboard.changed`
- `settings.changed`, `theme.changed`
- `board.attached`, `board.detached`
- `assistant.reply`

**MCP resources:** `vga32://screen` (PNG), `vga32://state`, `vga32://apps`, `vga32://events`, `vga32://settings`, `vga32://clipboard`, `vga32://notes`.

### Hermes setup

Add one of these to `~/.hermes/config.yaml`, then restart Hermes.

Over HTTP (recommended; nothing to launch):

```yaml
mcp_servers:
  vga32:
    url: "http://192.168.1.110:8080/mcp"     # the "Agents:" line printed by `python -m vga32 desktop`
    headers:
      Authorization: "Bearer 12345678"       # your HOST_KEY
    timeout: 180
```

Or over stdio, if Hermes runs on the same PC and you prefer a subprocess:

```yaml
mcp_servers:
  vga32:
    command: "python"
    args: ["-m", "vga32", "mcp"]
    env:
      PYTHONPATH: "C:/Users/Nik/Documents/Arduino/vga32_thin/host"
      VGA32_URL: "http://127.0.0.1:8080"
      VGA32_KEY: "12345678"
```

If Hermes runs in WSL or on another machine, use the PC's LAN address, not `127.0.0.1`, and allow port 8080 through the Windows firewall.

### Tools

| Tool | What it does |
|---|---|
| `get_state` | Screen size, board status, mouse, every window (id, geometry, client area, focus, app details) |
| `screenshot` | PNG of what the monitor shows; optional `window_id` crop and `scale` 1–3 |
| `open_app` | Open any app by id (built-ins, plugins, registered apps) at an optional position and size, with options (`url`, `path`, `text`, `command`, `page`, panel `elements`) |
| `list_apps`, `register_app`, `unregister_app` | The app registry: declare apps that show up in the menus and on the desktop |
| `wait_events` | Wait for desktop events after a `seq`, filtered by type prefix, app or window |
| `ask_user` | Message, confirm, text input or choice dialog on the VGA screen; returns the answer |
| `show_menu` | Pop up a menu and return the item the user picked |
| `clipboard`, `hotkey` | The shared clipboard; global shortcuts that send `hotkey` events |
| `settings` | Read or change persistent settings |
| `local_ai` | Ask the local model (the Assistant's endpoint) a question and get text back |
| `window` | focus, close, move, resize, set_geometry, maximize, restore, shade, unshade, on_top, not_on_top |
| `layout` | cascade, tile, rows, grid, main-left, main-top, maximize, for all windows or a chosen list, with an optional gap |
| `type_text`, `press_key`, `mouse` | Raw input: text, key combos (`ctrl+s`, `alt+tab`, `win`), click / double-click / drag / scroll, optionally relative to a window |
| `terminal` | Run a command and get its output and exit code, read recent output, or kill a command |
| `web` | The full browser: navigate, back, forward, reload, read (text, links, form controls, videos), eval (run JavaScript), click (CSS selector or x/y), type, press, scroll, wait_for, screenshot (full-color PNG of the page), dither |
| `browser` | The text browser: navigate (URL or search words), back, forward, reload, read (text + numbered links and images), read_visible, click_link, scroll, find |
| `image` | Open an image in the viewer (URL, file or folder on the PC, data URI), step next/prev in its folder, set dither and fit |
| `notes` | Read, write or append the notes text |
| `panel` | Create or update a custom window from elements: text, rect, ellipse, line, bar, chart, list, table, button, image, input, checkbox, plus a right-click `menu` and `menubar` menus |
| `panel_events` | Button clicks and key presses on one panel (older polling API; `wait_events` covers every window) |
| `paint` | Draw the same elements onto a Paint canvas |
| `notify` | Toast message in the top-right corner |
| `desktop_style` | Theme (classic / platinum), background color, pattern, caption, wallpaper, desktop icons |

Example: a live dashboard with a button that the agent waits on.

```bash
curl -s -X POST http://localhost:8080/api/panel -H "Authorization: Bearer 12345678" -d '{
  "title": "Build", "x": 330, "y": 10, "w": 300, "h": 200, "background": "navy",
  "elements": [
    {"type": "text", "text": "Nightly build", "size": "medium", "bold": true},
    {"type": "bar", "value": 72, "color": "lime", "label": "tests 72%"},
    {"type": "chart", "values": [3, 5, 2, 8, 6, 9], "h": 50, "color": "yellow", "bg": "black"},
    {"type": "button", "id": "deploy", "label": "Deploy"}]}'
# use the "id" returned above
curl -s -X POST http://localhost:8080/api/panel_events -H "Authorization: Bearer 12345678" \
     -d '{"window_id": 2, "wait_seconds": 60}'
```

Colors are palette names (black darkgray gray navy blue sky teal cyan green lime maroon red orange yellow purple white), 0–15, or `#rrggbb`, which snaps to the nearest palette color.

## Writing your own app

There are three ways to put an app in the menus. All of them can also put it on the desktop.

### 1. Register it from an agent (no code on the PC)

```bash
curl -s -X POST http://localhost:8080/api/register_app -H "Authorization: Bearer 12345678" -d '{
  "id": "hermes.weather", "title": "Weather", "icon": "cloud", "category": "Agents",
  "desktop": true, "single_instance": true, "persist": true, "size": [300, 160],
  "menu": [{"id": "refresh", "label": "Refresh"}],
  "spec": {"background": "navy", "elements": [{"type": "text", "text": "Loading...", "color": "white"}]}}'
```

The app works like this:

1. When the user opens it, the desktop shows `spec` and publishes `app.launched {app_id, window_id}`.
2. The agent draws the window with `panel {window_id, elements}`.
3. The agent listens with `wait_events {"types": ["panel.", "menu.select"], "app_id": "hermes.weather", "since": <seq>}`.

Other options:

- `"kind": "web"` with a `url` opens a page, for example a local Open WebUI.
- `"kind": "command"` with a `command` runs it in a Terminal.
- `"webhook"` POSTs the app's events to a URL, so the agent doesn't have to stay connected.
- `persist: true` saves the manifest to `~/.vga32/apps/<id>.json`. You can also write that file by hand.

### 2. A Python plugin

Drop a `.py` file into `~/.vga32/apps/`. Every `App` subclass in it that has an `app_id` is registered at startup:

```python
from vga32.ui import App, FONT
from vga32.palette import C

class Hello(App):
    app_id = "demo.hello"
    title = "Hello"
    icon = "star"            # see vga32.ui.GLYPHS
    category = "Demos"       # the menu folder
    size = (200, 60)

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.yellow)
        d.text((x + 8, y + 8), "Hello", font=FONT, fill=C.black)

    def context_menu(self, x, y):           # right-click
        return [{"id": "wave", "label": "Wave"}]

    def on_menu(self, item_id):
        self.desk.notify("hi!")
```

Plugins that fail to load are listed in `get_state` under `plugin_errors` and in Settings > Apps.

### 3. A built-in

Subclass `App` (in `host/vga32/ui.py`) and add it to `BUILTINS` in `host/vga32/desktop.py`.

App hooks:

- `draw`, `key`, `mouse`, `tick`
- `state()`: details for agents
- `context_menu(x, y)` and `on_menu(id)`: right-click menus
- `menus()`: extra menu-bar menus
- `copy()` / `paste(text)`: the Edit menu

Draw with PIL using the colors in `vga32.palette.C`, or the theme's semantic colors (`self.desk.theme.colors["face"]`). Agents can use the same names (`face`, `text`, `accent`, `select`...) anywhere a color is accepted.

For raw access, use `Device` (fill/blit/text/line ops) plus `Screen.present_rgb(image)`, which sends only the changed tiles.

New themes subclass `Theme` in `host/vga32/theme.py`. A theme supplies:

- window chrome
- the bar (a bottom taskbar or a top menu bar)
- menus and icons
- palette, cursor and semantic colors
