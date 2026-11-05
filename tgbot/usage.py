"""A message in General, pinned and kept up to date: the Claude plan limits of every account signed in to a running dev container, one block per account."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import dockerapi, state, telegram
from .config import log

EVERY = 5 * 60  # seconds between passes when no run starts or ends
WAKE = threading.Event()  # set when a run starts or ends: a prompt sent to an account, or its answer back, shows in its limits without waiting for the next pass
SHOWN = None  # (message id, text) last shown, so an unchanged message is not edited

# Both are local commands: Claude answers without a model call, so they cost nothing (checked with Claude Code 2.1.306). The container's own Claude reads the limits with its own credentials, so the bot never handles a token. Not persisted, and RUN_MARK does not match them, so recover() and /stop leave them alone.
AUTH = ["timeout", "30", "claude", "auth", "status"]
USAGE = ["timeout", "60", "claude", "-p", "--no-session-persistence", "--", "/usage"]


def account(cid):
    """(email, plan) Claude Code is signed in with in the container, None when signed out. The output is JSON, maybe after a warning on stderr."""
    code, out = dockerapi.exec_run(cid, AUTH)
    text = out.decode(errors="replace")
    try:
        info = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        return None
    if code != 0 or not info.get("loggedIn"):
        return None
    return info.get("email") or info.get("authMethod") or "unknown account", info.get("subscriptionType")


def limits(text):
    """The limit lines of /usage's answer: what comes before its per-machine breakdown, without the billing line."""
    head = text.split("What's contributing")[0]
    return [line.strip() for line in head.splitlines() if line.strip() and not line.startswith("You are currently using")]


def report():
    try:
        dev = [c for c in dockerapi.dev_containers() if dockerapi.project(c)]
    except Exception as e:
        return f"docker error: {e}"
    running = sorted((c for c in dev if c.get("State") == "running"), key=dockerapi.project)

    def who(c):
        try:
            return account(c["Id"])
        except Exception as e:
            log.warning("dockbench-%s: claude auth status failed: %s", dockerapi.project(c), e)
            return "error"

    with ThreadPoolExecutor(8) as pool:
        found = list(zip(running, pool.map(who, running)))
    accounts = {}  # (email, plan) -> containers signed in with it, in name order
    for c, a in found:
        if isinstance(a, tuple):
            accounts.setdefault(a, []).append(c)

    def read(cs):  # one container per account is enough: the limits belong to the account
        try:
            code, out = dockerapi.exec_run(cs[0]["Id"], USAGE, timeout=90)
        except Exception as e:
            return [f"usage unavailable: {e}"]
        text = out.decode(errors="replace")
        return limits(text) if code == 0 and limits(text) else [f"usage unavailable: {(text.strip().splitlines() or ['exit ' + str(code)])[-1][:200]}"]

    with ThreadPoolExecutor(8) as pool:
        usages = list(pool.map(read, accounts.values()))
    blocks = [f"👤 {email}{f' ({plan})' if plan else ''} · {', '.join(map(dockerapi.project, cs))}\n" + "\n".join(lines) for ((email, plan), cs), lines in zip(accounts.items(), usages)]
    rest = [("not signed in", [c for c, a in found if a is None]), ("error", [c for c, a in found if a == "error"]), ("not running", [c for c in dev if c.get("State") != "running"])]
    notes = [f"{label}: {', '.join(sorted(map(dockerapi.project, cs)))}" for label, cs in rest if cs]
    return "\n\n".join(blocks + ["\n".join(notes)] if notes else blocks) or "no dev containers"


def show(group, text):
    """Edit the usage message in General, or send and pin a new one when there is none or it was deleted."""
    global SHOWN
    mid = state.STATE.get("usage")
    if mid and SHOWN != (mid, text):
        try:
            telegram.tg("editMessageText", chat_id=group, message_id=mid, text=text)
        except telegram.TgError as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message_id_invalid" in err:
                mid = None
            elif "not modified" not in err:
                raise
    if not mid:
        mid = telegram.tg("sendMessage", chat_id=group, text=text, disable_notification=True)["message_id"]
        if state.GROUP_ID != group:  # /setup move in between
            return
        with state.LOCK:
            state.STATE["usage"] = mid
            state.save()
        telegram.tg("pinChatMessage", chat_id=group, message_id=mid, disable_notification=True)  # once: without the Pin messages right it stays unpinned
    SHOWN = (mid, text)


def usage_loop():
    """Own thread: a pass every EVERY seconds, or ~5 s after WAKE. The execs take seconds per container: they must not hold up getUpdates or the pins."""
    last = None
    while True:
        if state.GROUP_ID and state.OWNER_ID:
            try:
                show(state.GROUP_ID, report() + time.strftime("\n\nupdated %H:%M UTC", time.gmtime()))
                last = None
            except Exception as e:
                if str(e) != last:
                    log.warning("usage message update failed: %s", e)
                last = str(e)
        if WAKE.wait(EVERY):
            time.sleep(5)  # the account's usage catches up with the request that just ended, and a burst of wakes makes one pass
        WAKE.clear()

