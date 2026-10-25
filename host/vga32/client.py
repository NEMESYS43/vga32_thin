"""TCP connection to the board: framing, authentication, draw ops and input events.

Frame (both directions): op u8, body length u16 LE, body. All ints little endian.
"""
import contextlib
import queue
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

from .keys import VK_NAMES
from .palette import PALETTE

TCP_PORT = 7777
BEACON_PORT = 7778
MAX_BODY = 4200

OP_HELLO, OP_PALETTE, OP_FILL, OP_BLIT4, OP_BLITRLE, OP_COPY, OP_TEXT = range(1, 8)
OP_LINE, OP_RECT, OP_CURSOR, OP_WARP, OP_SYNC, OP_PING, OP_CLEAR = range(8, 15)
EV_HELLO, EV_KEY, EV_MOUSE, EV_SYNC, EV_PONG, EV_ERROR, EV_MOUSE_REL = range(0x81, 0x88)
ERRORS = {1: "auth", 2: "bad op", 3: "bad args", 4: "too big"}

# Modifier bits in EV_KEY.
M_CTRL, M_LALT, M_RALT, M_SHIFT, M_GUI, M_CAPS, M_NUM, M_SCROLL = (1 << i for i in range(8))

# With numlock off the keypad reports KP_LEFT etc.; apps only care about the plain key.
_KP_ALIAS = {"KP_" + n: n for n in ("UP", "DOWN", "LEFT", "RIGHT", "HOME", "END", "PAGEUP",
                                     "PAGEDOWN", "INSERT", "DELETE")}
_KP_ALIAS["KP_ENTER"] = "RETURN"


@dataclass
class Info:
    proto: int
    width: int
    height: int
    bpp: int
    keyboard: bool
    mouse: bool
    fonts: list = field(default_factory=list)
    name: str = ""


@dataclass
class Key:
    name: str      # FabGL VirtualKey name, e.g. "a", "RETURN", "F1"
    char: str      # printable character or ""
    down: bool
    mods: int

    @property
    def ctrl(self): return bool(self.mods & M_CTRL)

    @property
    def alt(self): return bool(self.mods & (M_LALT | M_RALT))

    @property
    def shift(self): return bool(self.mods & M_SHIFT)


@dataclass
class Mouse:
    x: int
    y: int
    buttons: int   # 1 left, 2 right, 4 middle
    wheel: int
    web: bool = False   # came from the browser UI, not the PS/2 mouse
    rel: bool = False   # x, y are a relative motion (browser pointer lock), not a position


class Disconnected(Exception):
    pass


class Device:
    def __init__(self, host, port=TCP_PORT, key="12345678", timeout=5.0, events=None):
        """events: queue to deliver Key/Mouse (and a final None on disconnect) into."""
        self.host, self.port = host, port
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.events = events if events is not None else queue.Queue()
        self.info = None
        self.closed = False
        self.closed_event = threading.Event()
        self._batch = None             # bytearray while inside batch()
        self._lock = threading.Lock()
        self._sync_cond = threading.Condition()
        self._sync_sent = 0
        self._sync_acked = 0
        self._last_send = time.monotonic()
        self._hello = threading.Event()

        self._send(OP_HELLO, key.encode())
        self.sock.settimeout(None)
        threading.Thread(target=self._reader, daemon=True).start()
        if not self._hello.wait(timeout) or self.info is None:
            self.close()
            raise Disconnected("board did not answer HELLO (wrong key?)")
        threading.Thread(target=self._keepalive, daemon=True).start()
        self.palette(PALETTE)
        self.cursor_arrow()

    # ---------- transport ----------
    def _send(self, op, body=b""):
        if len(body) > MAX_BODY:
            raise ValueError(f"body {len(body)} > {MAX_BODY}")
        if self.closed:
            raise Disconnected("connection closed")
        frame = struct.pack("<BH", op, len(body)) + body
        try:
            with self._lock:
                if self._batch is not None:
                    self._batch += frame
                else:
                    self.sock.sendall(frame)
                self._last_send = time.monotonic()
        except OSError as e:
            self.close()
            raise Disconnected(str(e)) from e

    @contextlib.contextmanager
    def batch(self):
        """Collect ops and send them in one write. With TCP_NODELAY every op would otherwise
        become its own small packet, and the ESP32's WiFi stack pays a few ms per packet."""
        with self._lock:
            self._batch = bytearray()
        try:
            yield
        finally:
            with self._lock:
                data, self._batch = self._batch, None
            if data and not self.closed:
                try:
                    with self._lock:
                        self.sock.sendall(data)
                except OSError as e:
                    self.close()
                    raise Disconnected(str(e)) from e

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise Disconnected("board closed the connection")
            buf += chunk
        return bytes(buf)

    def _reader(self):
        try:
            while True:
                ev, n = struct.unpack("<BH", self._recv_exact(3))
                self._handle(ev, self._recv_exact(n) if n else b"")
        except (OSError, Disconnected):
            pass
        finally:
            self.close()

    def _handle(self, ev, b):
        if ev == EV_HELLO:
            nf = b[8]
            fonts = [(b[9 + 2 * i], b[10 + 2 * i]) for i in range(nf)]
            self.info = Info(b[0], b[1] | b[2] << 8, b[3] | b[4] << 8, b[5], bool(b[6]), bool(b[7]),
                             fonts, b[9 + 2 * nf:].decode(errors="replace"))
            self._hello.set()
        elif ev == EV_KEY:
            vk = b[0] | b[1] << 8
            name = VK_NAMES[vk] if vk < len(VK_NAMES) else f"VK{vk}"
            name = _KP_ALIAS.get(name, name)
            ch = chr(b[3]) if 32 <= b[3] < 127 else ""
            self.events.put(Key(name, ch, bool(b[2]), b[4]))
        elif ev == EV_MOUSE:
            self.events.put(Mouse(b[0] | b[1] << 8, b[2] | b[3] << 8, b[4], struct.unpack("<b", b[5:6])[0]))
        elif ev == EV_MOUSE_REL:
            dx, dy, buttons, wheel = struct.unpack("<hhBb", b[:6])
            self.events.put(Mouse(dx, dy, buttons, wheel, rel=True))
        elif ev == EV_SYNC:
            with self._sync_cond:
                self._sync_acked = struct.unpack("<I", b)[0]
                self._sync_cond.notify_all()
        elif ev == EV_ERROR:
            print(f"[vga32] board error: {ERRORS.get(b[0], b[0])} on op 0x{b[1]:02x}", file=sys.stderr)
            if b[0] == 1:
                self._hello.set()

    def _keepalive(self):
        # The board drops a silent host after 20s.
        while not self.closed:
            time.sleep(1)
            if time.monotonic() - self._last_send > 5:
                try:
                    self.ping()
                except Disconnected:
                    return

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.events.put(None)
        self._hello.set()
        self.closed_event.set()
        with self._sync_cond:
            self._sync_cond.notify_all()

    # ---------- flow control ----------
    def sync(self):
        """Queue a sync marker; returns its token."""
        self._sync_sent += 1
        self._send(OP_SYNC, struct.pack("<I", self._sync_sent))
        return self._sync_sent

    def wait_sync(self, token=None, timeout=3.0):
        """Block until the board has drawn everything sent before `token` (default: latest)."""
        token = self._sync_sent if token is None else token
        with self._sync_cond:
            ok = self._sync_cond.wait_for(lambda: self.closed or self._sync_acked >= token, timeout)
        if self.closed:
            raise Disconnected("connection closed")
        return ok

    # ---------- draw ops ----------
    def palette(self, colors, start=0):
        self._send(OP_PALETTE, b"".join(bytes((start + i, *rgb)) for i, rgb in enumerate(colors)))

    def fill(self, x, y, w, h, c):
        self._send(OP_FILL, struct.pack("<hhhhB", x, y, w, h, c))

    def rect(self, x, y, w, h, c):
        self._send(OP_RECT, struct.pack("<hhhhB", x, y, w, h, c))

    def line(self, x1, y1, x2, y2, c):
        self._send(OP_LINE, struct.pack("<hhhhB", x1, y1, x2, y2, c))

    def blit4(self, x, y, w, h, packed):
        self._send(OP_BLIT4, struct.pack("<HHHH", x, y, w, h) + packed)

    def blitrle(self, x, y, w, h, runs):
        self._send(OP_BLITRLE, struct.pack("<HHHH", x, y, w, h) + runs)

    def copy(self, sx, sy, w, h, dx, dy):
        self._send(OP_COPY, struct.pack("<HHHHHH", sx, sy, w, h, dx, dy))

    def text(self, x, y, s, fg, bg=255, font=3):
        self._send(OP_TEXT, struct.pack("<hhBBB", x, y, fg, bg, font) + s.encode("latin-1", "replace"))

    def clear(self, c=0):
        self._send(OP_CLEAR, bytes((c,)))

    def cursor_hide(self):
        self._send(OP_CURSOR, b"\x00")

    def cursor_arrow(self):
        self._send(OP_CURSOR, b"\x01")

    def cursor_image(self, pixels, hx=0, hy=0):
        """pixels: list of rows of palette indices, 255 = transparent (max 32x32)."""
        h, w = len(pixels), len(pixels[0])
        self._send(OP_CURSOR, bytes((2, w, h, hx, hy)) + bytes(v for r in pixels for v in r))

    @property
    def relative_mouse(self):
        """True if the firmware has the relative mouse mode (protocol 2+)."""
        return self.info.proto >= 2

    def cursor_relative(self):
        """Hide the pointer and report raw motion (EV_MOUSE_REL) until another cursor op."""
        self._send(OP_CURSOR, b"\x03")

    def warp(self, x, y):
        self._send(OP_WARP, struct.pack("<HH", x, y))

    def ping(self):
        self._send(OP_PING)


def discover(timeout=3.0, want_free=False):
    """Listen for UDP beacons. Returns list of (ip, fields) seen within `timeout`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", BEACON_PORT))
    s.settimeout(0.25)
    found = {}
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(256)
            except socket.timeout:
                if found and not want_free:
                    break
                continue
            msg = data.decode(errors="replace").split()
            if not msg or not msg[0].startswith("VGA32TC/"):
                continue
            fields = dict(kv.split("=", 1) for kv in msg[1:] if "=" in kv)
            found[ip] = fields
    finally:
        s.close()
    return list(found.items())
