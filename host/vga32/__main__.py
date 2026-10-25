"""python -m vga32 {discover|test|desktop|mirror|mcp} [--host IP] [--key KEY]"""
import argparse
import os
import queue
import socket
import sys
import time
import urllib.parse

from .client import Device, Disconnected, Key, Mouse, discover
from .palette import dither
from .screen import Screen


def find_host(args, required=True):
    if args.host:
        return args.host
    print("Looking for the board (UDP 7778)...")
    found = discover(timeout=args.wait)
    if not found:
        msg = ("No board found. Pass --host with the IP shown on the VGA screen "
               "(and allow Python through the Windows firewall for auto-discovery).")
        if required:
            sys.exit(msg)
        raise OSError("no board found on the network")
    ip, f = found[0]
    print(f"Found {f.get('name', '?')} at {ip}  {f.get('w')}x{f.get('h')}"
          f"{'  (busy - will take over)' if f.get('busy') == '1' else ''}")
    return ip


def connect(args, events=None, required=True):
    host = find_host(args, required)
    dev = Device(host, key=args.key, events=events)
    i = dev.info
    print(f"Connected: {i.name} {i.width}x{i.height} proto {i.proto} "
          f"keyboard={'yes' if i.keyboard else 'no'} mouse={'yes' if i.mouse else 'no'}")
    return dev


def forever(args, body):
    """Run body(dev), reconnecting whenever the board drops or reboots."""
    while True:
        try:
            dev = connect(args)
        except (OSError, Disconnected) as e:
            print(f"Connect failed: {e}; retrying in 3s")
            time.sleep(3)
            continue
        try:
            body(dev)
        except Disconnected as e:
            print(f"Disconnected: {e}; reconnecting")
        finally:
            dev.close()
        time.sleep(1)


def cmd_discover(args):
    found = discover(timeout=args.wait, want_free=True)
    if not found:
        print("Nothing heard. Is the board on WiFi, and is UDP 7778 allowed through your firewall?")
    for ip, f in found:
        print(f"{ip:15}  name={f.get('name')}  {f.get('w')}x{f.get('h')}  port={f.get('port')}  busy={f.get('busy')}")


def cmd_test(args):
    dev = connect(args)
    W, H = dev.info.width, dev.info.height
    dev.clear(0)
    bw = W // 16
    for i in range(16):
        dev.fill(i * bw, 0, bw, 60, i)
        dev.text(i * bw + 4, 64, str(i), 15, 255, 1)
    for f, (fw, fh) in enumerate(dev.info.fonts):
        dev.text(10, 90 + f * 24, f"font {f}: {fw}x{fh}  The quick brown fox 0123456789", 15, 255, f)
    for k in range(0, W, 20):
        dev.line(W // 2, H - 1, k, 230, 1 + k // 20 % 15)
    dev.rect(4, 4, W - 8, H - 8, 13)
    dev.text(10, H - 30, "Type or move the mouse - events print on the PC. Ctrl+C to quit.", 15, 0, 3)
    dev.wait_sync(dev.sync())
    print("Test pattern drawn. Press keys / move the mouse on the board.")
    try:
        while True:
            ev = dev.events.get()
            if ev is None:
                print("Disconnected.")
                return
            if isinstance(ev, Key):
                print(f"key {ev.name:12} {'down' if ev.down else 'up  '} char={ev.char!r} mods=0x{ev.mods:02x}")
            elif isinstance(ev, Mouse) and (ev.buttons or ev.wheel):
                print(f"mouse {ev.x},{ev.y} buttons={ev.buttons} wheel={ev.wheel}")
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        s.close()


def cmd_desktop(args):
    import threading
    from .desktop import Attach, Desktop

    w, h = (int(v) for v in args.size.lower().split("x"))
    desk = Desktop(w, h)
    if args.web_port:
        from .web import WebUI
        desk.web = WebUI(args.key, args.web_port, desk).start()
        q = urllib.parse.quote(args.key)
        base = f"http://{lan_ip()}:{args.web_port}"
        print(f"Web UI:  http://localhost:{args.web_port}/?key={q}  (LAN: {base}/?key={q})")
        print(f"Agents:  MCP {base}/mcp   REST {base}/api   (header: Authorization: Bearer <key>)")

    def connector():
        # The desktop keeps running without a board; this thread (re)attaches one when it appears.
        while True:
            try:
                dev = connect(args, events=desk.inbox, required=False)
            except (OSError, Disconnected) as e:
                print(f"Board: {e}; retrying in 3s")
                time.sleep(3)
                continue
            desk.inbox.put(Attach(dev))
            dev.closed_event.wait()
            print("Board disconnected; reconnecting")
            time.sleep(1)

    if not args.no_board:
        threading.Thread(target=connector, daemon=True).start()
    try:
        desk.run()
    finally:
        desk.shutdown()


def cmd_mcp(args):
    """stdio MCP server for agents that launch tools as subprocesses; forwards to a running desktop."""
    import json
    import urllib.request

    url = args.url.rstrip("/") + "/mcp"
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        req = urllib.request.Request(url, line.encode(), method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + args.key})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
        except Exception as e:
            if isinstance(msg, dict) and msg.get("id") is not None:
                err = {"jsonrpc": "2.0", "id": msg["id"],
                       "error": {"code": -32000, "message": f"VGA32 desktop unreachable at {url}: {e}"}}
                out.write(json.dumps(err) + "\n")
                out.flush()
            continue
        if body.strip():
            out.write(body.decode().strip() + "\n")
            out.flush()


def cmd_mirror(args):
    """Show this PC's screen on the VGA monitor (view only)."""
    from PIL import Image, ImageGrab

    def body(dev):
        screen = Screen(dev)
        dev.cursor_hide()
        period = 1.0 / args.fps
        while True:
            t0 = time.monotonic()
            if dev.closed:
                raise Disconnected("board went away")
            while True:     # drain input so the queue doesn't grow
                try:
                    dev.events.get_nowait()
                except queue.Empty:
                    break
            img = ImageGrab.grab(all_screens=False).convert("RGB")
            img.thumbnail((screen.w, screen.h), Image.BILINEAR)
            frame = Image.new("RGB", (screen.w, screen.h))
            frame.paste(img, ((screen.w - img.width) // 2, (screen.h - img.height) // 2))
            screen.present_rgb(dither(frame) if args.dither else frame)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    forever(args, body)


def main():
    p = argparse.ArgumentParser(prog="python -m vga32", description="VGA32 thin client host")
    p.add_argument("command", choices=["discover", "test", "desktop", "mirror", "mcp"])
    p.add_argument("--host", default=os.environ.get("VGA32_HOST"), help="board IP (default: auto-discover)")
    p.add_argument("--key", default=os.environ.get("VGA32_KEY", "12345678"), help="HOST_KEY from the sketch")
    p.add_argument("--wait", type=float, default=5.0, help="seconds to listen for beacons")
    p.add_argument("--fps", type=float, default=8.0, help="mirror: frames per second")
    p.add_argument("--dither", action="store_true", help="mirror: Floyd-Steinberg dither (more bandwidth)")
    p.add_argument("--web-port", type=int, default=8080, help="desktop: web UI / agent API port, 0 = off")
    p.add_argument("--size", default="640x400", help="desktop: size to use until a board reports its own")
    p.add_argument("--no-board", action="store_true", help="desktop: run without connecting to a board")
    p.add_argument("--url", default=os.environ.get("VGA32_URL", "http://127.0.0.1:8080"),
                   help="mcp: address of the running desktop's web server")
    args = p.parse_args()
    try:
        {"discover": cmd_discover, "test": cmd_test, "desktop": cmd_desktop, "mirror": cmd_mirror,
         "mcp": cmd_mcp}[args.command](args)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
