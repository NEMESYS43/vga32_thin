"""Host-side framebuffer mirror: diff a new frame against what the board shows and
send only the damaged areas, picking FILL, BLITRLE or BLIT4 per band."""
import numpy as np

from .client import MAX_BODY
from .palette import rgb_to_index

TILE_W, TILE_H = 32, 16
BODY = MAX_BODY - 8   # pixel bytes per BLIT op after the x y w h header


def _rle_row(r, limit):
    """RLE bytes for one row, or None if it would exceed `limit` bytes."""
    starts = np.flatnonzero(np.diff(r)) + 1
    if len(starts) + 1 > limit:
        return None
    starts = np.concatenate(([0], starts))
    lens = np.diff(np.append(starts, len(r)))
    out = bytearray()
    for s, n in zip(starts.tolist(), lens.tolist()):
        c = int(r[s]) << 4
        while n:
            if n >= 16:
                take = min(n, 271)
                out += bytes((c, take - 16))
            else:
                take = n
                out.append(c | n)
            n -= take
        if len(out) > limit:
            return None
    return bytes(out)


def _pack4(block):
    if block.shape[1] & 1:
        block = np.pad(block, ((0, 0), (0, 1)))
    return ((block[:, 0::2] << 4) | block[:, 1::2]).astype(np.uint8).tobytes()


class Screen:
    def __init__(self, dev):
        self.dev = dev
        self.w, self.h = dev.info.width, dev.info.height
        self.sent = np.full((self.h, self.w), 255, np.uint8)   # 255 = unknown, forces a full send
        self._pending = None
        self.bytes_sent = 0

    def invalidate(self):
        self.sent[:] = 255

    def present_rgb(self, img):
        """img: PIL RGB image or HxWx3 array of the board's size."""
        self.present(rgb_to_index(np.asarray(img)))

    def present(self, idx):
        """idx: HxW uint8 palette indices. Waits for the previous frame to land first,
        so at most one frame is ever queued on the board."""
        if self._pending is not None:
            self.dev.wait_sync(self._pending)
            self._pending = None
        with self.dev.batch():                  # one write per frame -> full-size TCP packets
            for x, y, w, h in self._damage(idx):
                self._send_rect(idx, x, y, w, h)
            self._pending = self.dev.sync()
        self.sent[:] = idx

    def _damage(self, idx):
        th, tw = -(-self.h // TILE_H), -(-self.w // TILE_W)
        diff = np.zeros((th * TILE_H, tw * TILE_W), bool)
        diff[:self.h, :self.w] = idx != self.sent
        dirty = diff.reshape(th, TILE_H, tw, TILE_W).any(axis=(1, 3))
        rects, open_ = [], {}   # open_: (c0, c1) -> rect index still growing downward
        for ty in range(th):
            row, spans, c = dirty[ty], [], 0
            while c < tw:
                if row[c]:
                    c0 = c
                    while c < tw and row[c]:
                        c += 1
                    spans.append((c0, c))
                c += 1
            nxt = {}
            for sp in spans:
                if sp in open_:
                    rects[open_[sp]][3] += 1
                    nxt[sp] = open_[sp]
                else:
                    rects.append([sp[0], ty, sp[1] - sp[0], 1])
                    nxt[sp] = len(rects) - 1
            open_ = nxt
        out = []
        for c0, r0, cw, rh in rects:
            x, y = c0 * TILE_W, r0 * TILE_H
            out.append((x, y, min(cw * TILE_W, self.w - x), min(rh * TILE_H, self.h - y)))
        return out

    def _send_rect(self, idx, x, y, w, h):
        block = idx[y:y + h, x:x + w]
        if (block == block[0, 0]).all():
            self.dev.fill(x, y, w, h, int(block[0, 0]))
            self.bytes_sent += 12
            return
        rb = (w + 1) // 2
        # RLE only where it clearly wins: dithered rows hover near break-even, and flipping
        # between RLE and raw every row splits a band into many small ops.
        rle = [_rle_row(block[r], rb * 3 // 4) for r in range(h)]
        r = 0
        while r < h:
            r0 = r
            if rle[r] is not None:
                parts, size = [], 0
                while r < h and rle[r] is not None and size + len(rle[r]) <= BODY:
                    parts.append(rle[r]); size += len(rle[r]); r += 1
                self.dev.blitrle(x, y + r0, w, r - r0, b"".join(parts))
                self.bytes_sent += 11 + size
            else:
                max_rows = max(1, BODY // rb)
                while r < h and rle[r] is None and r - r0 < max_rows:
                    r += 1
                self.dev.blit4(x, y + r0, w, r - r0, _pack4(block[r0:r]))
                self.bytes_sent += 11 + rb * (r - r0)
