"""
Portable host adapter: run the whole system anywhere Python runs
(Railway, Render, Fly, a VPS, Docker, your laptop) — no Azure needed.

    python worker.py                 # long-running scheduler (uses jobs.SCHEDULE)
    python worker.py run drain       # run one job once
    python worker.py run generate_a  # ...any name from jobs.SCHEDULE
    python worker.py generate d 48   # ad-hoc: group/company + lookback hours
    python worker.py status          # queue length, today's post count

Set STORAGE_BACKEND=file and DATA_DIR=/data (a persistent volume) plus the
env vars in .env.example. A tiny HTTP health server listens on $PORT so
platforms that expect a web process (Railway "web" service) stay happy.
"""

import os
import sys
import json
import time
import logging
import datetime
import threading
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")


# ---------- minimal 5-field cron matcher (UTC) ----------

def _field_matches(expr, value, lo, hi):
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
            if step == 1 and value == start:
                return True
            if step > 1:
                end = hi
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def cron_matches(cron5, dt):
    m, h, dom, mon, dow = cron5.split()
    return (_field_matches(m, dt.minute, 0, 59) and _field_matches(h, dt.hour, 0, 23)
            and _field_matches(dom, dt.day, 1, 31) and _field_matches(mon, dt.month, 1, 12)
            and _field_matches(dow, dt.isoweekday() % 7, 0, 6))   # 0=Sunday


# ---------- health endpoint ----------

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "utc": datetime.datetime.now(timezone.utc).isoformat()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):   # quiet
        pass


def _serve_health():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


# ---------- main loop ----------

def scheduler_loop():
    import jobs
    threading.Thread(target=_serve_health, daemon=True).start()
    log.info("worker up; %d scheduled jobs; storage=%s", len(jobs.SCHEDULE),
             os.getenv("STORAGE_BACKEND") or "auto")
    last_minute = None
    while True:
        now = datetime.datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if now != last_minute:
            last_minute = now
            for name, cron, _ in jobs.SCHEDULE:
                if cron_matches(cron, now):
                    threading.Thread(target=jobs.run, args=(name,), daemon=True).start()
        time.sleep(5)


def status():
    import jobs
    store = jobs.posts_store()
    try:
        q = json.loads(store.download_blob("li_queue.json").readall())
    except Exception:
        q = []
    try:
        plog = json.loads(store.download_blob("li_post_log.json").readall())
    except Exception:
        plog = []
    today = datetime.datetime.now(timezone.utc).date().isoformat()
    print(f"queue: {len(q)} | posted today: {sum(1 for p in plog if p.get('ts','').startswith(today))}"
          f" | posted total: {len(plog)}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        scheduler_loop()
    elif args[0] == "run" and len(args) > 1:
        import jobs
        print(jobs.run(args[1]))
    elif args[0] == "generate":
        import jobs
        print(jobs.generate(args[1] if len(args) > 1 else None,
                            int(args[2]) if len(args) > 2 else 24))
    elif args[0] == "status":
        status()
    else:
        print(__doc__)
