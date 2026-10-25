"""Desktop event bus: what happened, in order, for agents to wait on, stream or receive by webhook.

Every event is {"seq": n, "type": "panel.click", "time": unix, ...details}. Types:
  app.launched      a registered app was opened (app_id, window_id) - the owner fills it in
  window.opened / window.closed / window.focused
  panel.click / panel.key       (window_id, app_id, id | name/char/mods)
  menu.select       an agent-declared menu item was picked (window_id, app_id, id)
  dialog.result     ask_user answered (dialog_id, button, value)
  hotkey            a registered hotkey was pressed (combo, id)
  clipboard.changed, settings.changed, theme.changed, board.attached, board.detached
  assistant.reply   the Assistant finished answering (text)

Filters take type prefixes ("panel." matches panel.click and panel.key) plus optional
app_id / window_id.
"""
import collections
import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MAX_EVENTS = 1000


def matches(ev, types=None, app_id=None, window_id=None):
    if types and not any(ev["type"] == t or (t.endswith(".") and ev["type"].startswith(t))
                         or ev["type"].startswith(t + ".") for t in types):
        return False
    if app_id is not None and ev.get("app_id") != app_id:
        return False
    if window_id is not None and ev.get("window_id") != int(window_id):
        return False
    return True


class EventBus:
    def __init__(self, webhooks=lambda ev: []):
        self.events = collections.deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.cond = threading.Condition()
        self.webhooks = webhooks        # ev -> [url, ...]
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="webhook")
        self.webhook_errors = collections.deque(maxlen=20)

    def publish(self, type_, **data):
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "type": type_, "time": round(time.time(), 3), **data}
            self.events.append(ev)
            self.cond.notify_all()
        for url in self.webhooks(ev):
            self.pool.submit(self._post, url, ev)
        return ev

    def _post(self, url, ev):
        req = urllib.request.Request(url, json.dumps(ev, default=str).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "vga32"})
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            self.webhook_errors.append({"url": url, "seq": ev["seq"], "error": str(e)[:200]})

    def since(self, seq, **flt):
        with self.cond:
            return [e for e in self.events if e["seq"] > seq and matches(e, **flt)]

    def wait(self, seq, timeout, **flt):
        """Events after seq that match; blocks up to timeout seconds for the first one."""
        end = time.monotonic() + max(0.0, timeout)
        with self.cond:
            while True:
                out = [e for e in self.events if e["seq"] > seq and matches(e, **flt)]
                left = end - time.monotonic()
                if out or left <= 0:
                    return out
                self.cond.wait(left)

    def last_seq(self):
        with self.cond:
            return self.seq
