"""Startup and the getUpdates polling loop."""
import http.server
import logging
import secrets
import sys
import threading
import time

from . import dockerapi, pins, routing, runs, state, status, telegram, topics, usage
from .config import STATUS_PORT, log


def main():
    if not telegram.TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    me = telegram.tg("getMe")
    state.BOT_NAME, state.BOT_ID = me["username"].lower(), me["id"]
    state.LAST_POLL = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # getMe answered, so Telegram is reachable with this token; else /status would read stale until the first 30 s long poll returns
    state.load()
    if state.GROUP_ID and state.STATE.get("group_id") not in (None, 0, state.GROUP_ID):  # TELEGRAM_GROUP_ID changed: thread and message ids of the old group mean nothing here, like /setup move
        state.STATE["threads"], state.STATE["created"], state.STATE["sessions"], state.STATE["replies"] = {}, [], {}, {}
    state.STATE["created"] += sorted({t["name"] for t in state.STATE["threads"].values() if t.get("name")} - set(state.STATE["created"]))  # bindings from before created existed, so created alone says which names have a topic
    state.GROUP_ID, state.OWNER_ID = state.GROUP_ID or state.STATE.get("group_id", 0), state.OWNER_ID or state.STATE.get("owner_id", 0)
    if state.GROUP_ID or state.OWNER_ID:
        state.STATE["group_id"], state.STATE["owner_id"] = state.GROUP_ID, state.OWNER_ID  # so env values survive removing TELEGRAM_GROUP_ID/TELEGRAM_OWNER_ID later, and an older owner does not come back
        state.save()
    if not state.OWNER_ID:
        state.SETUP_CODE = secrets.token_hex(8)  # 64 bits: no expiry or lockout needed at Telegram's message rates
        log.info("setup: open a private chat with @%s and send: /setup %s", state.BOT_NAME, state.SETUP_CODE)
    elif not state.GROUP_ID:
        log.info("setup: send /setup@%s in the forum group", state.BOT_NAME)
    threading.Thread(target=http.server.ThreadingHTTPServer(("0.0.0.0", STATUS_PORT), status.StatusHandler).serve_forever, daemon=True).start()
    runs.recover()
    log.info("polling from offset %s, docker at %s", state.STATE["offset"], dockerapi.DOCKER_HOST)
    threading.Thread(target=topics.autocreate_loop, daemon=True).start()
    threading.Thread(target=runs.ticker, daemon=True).start()
    threading.Thread(target=runs.watchdog, daemon=True).start()
    threading.Thread(target=pins.pins_loop, daemon=True).start()
    threading.Thread(target=usage.usage_loop, daemon=True).start()
    groups = {}
    while True:
        try:
            updates = telegram.tg("getUpdates", offset=state.STATE["offset"], timeout=1 if groups else 30, allowed_updates=["message", "callback_query"])
            state.LAST_POLL = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        except Exception as e:
            log.warning("getUpdates failed: %s", e)
            time.sleep(5)
            continue
        for u in updates:
            try:
                routing.handle(u, groups)
            except Exception:
                log.exception("update %s failed", u["update_id"])
            with state.LOCK:
                state.STATE["offset"] = u["update_id"] + 1
        if updates:
            state.save()
        routing.flush(groups, lambda gid, g: time.monotonic() - g["at"] >= 1)
