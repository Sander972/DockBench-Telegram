"""GET /status on port 8080 for the dashboard."""
import http.server
import json
import time

from . import state


def status_payload():
    """What GET /status answers. Every dev container on devnet can read it, so booleans and topic names only: no Telegram ids, no setup code, no token."""
    with state.LOCK:
        items = []
        for tid, t in state.STATE["threads"].items():
            if not t.get("name"):
                continue
            active = [r for r in state.ACTIVE.values() if r.tid == int(tid)]
            busy = sum(r.busy for r in active)
            items.append({"name": t["name"], "running": busy > 0, "runs": busy, "queued": sum(state.queued(r) for r in active)})
        return {"bot": state.BOT_NAME, "owner": bool(state.OWNER_ID), "group": bool(state.GROUP_ID), "topics": sorted(items, key=lambda x: x["name"]), "last_poll_at": state.LAST_POLL, "uptime_seconds": int(time.monotonic() - state.STARTED)}


class StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(status_payload()).encode() if self.path == "/status" else b'{"error": "not found"}'
        self.send_response(200 if self.path == "/status" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # the dashboard polls every few seconds
