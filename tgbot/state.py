"""The state file and the in-memory state shared by the threads, plus the values set at runtime (owner, group, bot name). Read them as `state.X`: a name imported from here would not see a reassignment."""
import json
import os
import threading
import time

from .config import DEFAULT_PERM, EFFORTS, MAX_SESSIONS, MODELS, PERMS, log

STATE_FILE = os.environ.get("STATE_FILE") or "/data/state.json"
# Optional: when unset, /setup stores them in the state file (see onboarding.setup).
GROUP_ID = int(os.environ.get("TELEGRAM_GROUP_ID") or 0)
OWNER_ID = int(os.environ.get("TELEGRAM_OWNER_ID") or 0)

LOCK = threading.RLock()
# created: PROJECT names that ever had a topic (bound by hand or by autocreate), autocreate never makes another one even once unbound. sessions: per root request message id, see runs.start_session. replies: message id -> session key, for every request and answer of a session. commands: PROJECT name -> its Claude slash commands, for the Telegram menu (see commands.py). skills: PROJECT name -> which of them are skills, to group the menu
STATE = {"offset": 0, "threads": {}, "created": [], "sessions": {}, "replies": {}, "commands": {}, "skills": {}}
ACTIVE = {}  # session key -> Run, only while it has a job running or queued
PENDING = {}  # picker message id -> request waiting for its model and effort, in memory: a restart drops it like the queue
RETRY = {}  # PROJECT name -> monotonic time before which autocreate does not retry a failed create
AUTO = threading.Lock()  # held for a whole autocreate pass, so /setup move cannot swap the group under it
BOT_NAME, BOT_ID = "", 0  # from getMe, name lowercase
SETUP_CODE = None  # one-time code in the log while no owner is set
STARTED = time.monotonic()
LAST_POLL = None  # UTC time of the boot getMe or the last getUpdates that answered, for /status: stale means a revoked token or no Telegram


def load():
    try:
        with open(STATE_FILE) as f:
            STATE.update(json.load(f))
    except FileNotFoundError:
        pass
    except ValueError:  # a truncated file (power loss) must not crash every boot: set it aside and start empty, the owner re-runs /setup
        log.error("%s is not valid JSON, moving it to .bad and starting with an empty state", STATE_FILE)
        os.replace(STATE_FILE, STATE_FILE + ".bad")


def save():
    with LOCK:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(STATE, f, indent=1)
            f.flush()
            os.fsync(f.fileno())  # the rename below is atomic, the data behind it is not until synced
        os.replace(tmp, STATE_FILE)


def settings(tid):
    """Per-topic values: name (the bound PROJECT), model, effort and permission mode of the last session started here."""
    with LOCK:
        return dict(STATE["threads"].get(str(tid), {}))


def update(tid, **values):
    """Set per-thread values, None deletes. Persists immediately."""
    with LOCK:
        t = STATE["threads"].setdefault(str(tid), {})
        for k, v in values.items():
            if v is None:
                t.pop(k, None)
            else:
                t[k] = v
        save()


def last_pick(tid):
    s = settings(tid)
    perm = s.get("perm", DEFAULT_PERM)  # a pick from before the mode step ran in auto
    return (s["model"], s["effort"], perm) if s.get("model") in MODELS and s.get("effort") in EFFORTS and perm in PERMS else None


def bind(tid, name):
    """Sessions keep the container they started in, so rebinding only changes where new sessions go."""
    if settings(tid).get("name") != name:
        with LOCK:
            if name not in STATE["created"]:
                STATE["created"].append(name)
        update(tid, name=name)
        log.info("thread %s bound to dockbench-%s", tid, name)


def sess(key):
    with LOCK:
        return dict(STATE["sessions"].get(str(key)) or {})


def sess_update(key, **values):
    with LOCK:
        s = STATE["sessions"].get(str(key))
        if s is not None:
            s.update(values)
            save()


def link(key, ids):
    """Replies to any of these messages continue session key."""
    with LOCK:
        for i in ids:
            STATE["replies"][str(i)] = key
        save()


def session_of(msgs):
    """Key of the session a message (or album) replies to, else None: then it starts a new session."""
    with LOCK:
        for m in msgs:
            key = STATE["replies"].get(str((m.get("reply_to_message") or {}).get("message_id")))
            if key and str(key) in STATE["sessions"]:
                return key
    return None


def queued(r):
    """Jobs of a Run waiting behind the one at work (or about to be): the one rule for /status, the pin and GET /status. Here and not in runs, which pins imports."""
    return max(0, r.q.unfinished_tasks - 1)


def prune():
    """Forget the oldest idle sessions past MAX_SESSIONS, and the replies that pointed at them. Caller holds LOCK."""
    s = STATE["sessions"]
    idle = sorted((k for k in s if int(k) not in ACTIVE and not s[k].get("working")), key=lambda k: s[k].get("at", 0))
    gone = set(idle[:max(0, len(s) - MAX_SESSIONS)])
    for k in gone:
        del s[k]
    STATE["replies"] = {m: k for m, k in STATE["replies"].items() if str(k) not in gone}
