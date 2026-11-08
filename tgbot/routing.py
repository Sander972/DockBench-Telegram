"""Incoming updates: the allowlist, topic events, commands, button taps and albums."""
import re
import threading
import time

from . import commands, dockerapi, onboarding, push, runs, state, telegram
from .config import COMMANDS, DEFAULT_PERM, EFFORTS, HELP, MODELS, NAME_RE, PERMS, WORKSPACE, log


def command(tid, cmd, arg, m):
    cfg = state.settings(tid)
    if cmd == "bind":
        if not NAME_RE.match(arg):
            return telegram.send(tid, "usage: /bind <PROJECT> (letters, digits, _ . - only)")
        state.bind(tid, arg)
        return telegram.send(tid, f"bound to dockbench-{arg}" + ("" if dockerapi.container(arg) else ", which does not exist (yet)") + ". Sessions already started stay in their container.")
    if cmd == "help":
        return telegram.send(tid, HELP)
    if cmd == "push":
        return push.spawn(push.pick, tid, arg)
    if cmd in ("clean", "clear"):  # clear: what Claude's own command would be, and that one does nothing headless
        if key := state.session_of([m]):
            return clean(tid, key=key)
        if (m.get("reply_to_message") or {}).get("message_id", tid) != tid:  # a real reply, not the topic's implicit one: never fall back to cleaning the whole topic
            return telegram.send(tid, "that message is not part of a session the bot knows, nothing cleaned", reply=m["message_id"])
        return clean(tid)
    if cmd == "stop":
        if key := state.session_of([m]):
            return telegram.send(tid, runs.stop(key), reply=m["message_id"], tag=lambda: runs.label(key))
        with state.LOCK:
            keys = [k for k, r in state.ACTIVE.items() if r.tid == tid]
        notes = [runs.stop(k) for k in keys]
        return telegram.send(tid, f"stopping {sum(n.startswith('stopping') for n in notes)} session(s)" if keys else "nothing running")

    def status_work():  # a Docker inspect and exec: off the polling thread, like /clean
        try:
            telegram.send(tid, status_text(tid, cfg))
        except Exception:
            log.exception("thread %s: status failed", tid)
    threading.Thread(target=status_work, daemon=True).start()


def clean(tid, mid=None, key=None):
    """/clean (as a reply to a session, only session key), or the pin's Clean button confirmed on message mid (its reply edited in there): runs.clean() makes many Telegram calls, so off the polling thread."""
    def work():
        try:
            note = runs.clean(tid, key)
            telegram.edit(mid, note) if mid else telegram.send(tid, note)
        except Exception:
            log.exception("thread %s: clean failed", tid)
    threading.Thread(target=work, daemon=True).start()


def status_text(tid, cfg):
    lines = []
    if cfg.get("name"):
        try:
            c = dockerapi.container(cfg["name"])
            health = c["State"] if c else "not found"
            if health == "running":
                health += ", claude " + ("logged in" if dockerapi.exec_run(c["Id"], ["claude", "auth", "status"])[0] == 0 else "NOT logged in: run claude once in VS Code")
        except Exception as e:
            health = f"docker error: {e}"
        lines += [f"dockbench-{cfg['name']}: {health}", f"VS Code: https://vscode.dev/tunnel/{cfg['name']}{WORKSPACE}"]
    else:
        lines.append("unbound topic, send /bind <PROJECT>")
    with state.LOCK:
        mine = [(r, state.STATE["sessions"].get(str(r.key)) or {}) for r in state.ACTIVE.values() if r.tid == tid]
        asking = sum(p["tid"] == tid for p in state.PENDING.values())
    for r, s in sorted(mine, key=lambda x: x[0].key):
        queued = state.queued(r)
        where = telegram.msg_link(tid, r.key) or f"message {r.key}"
        lines.append(f"{telegram.badge(r.key)} · {s.get('model')} · {s.get('effort')} · {s.get('perm') or DEFAULT_PERM} · {'running ' + telegram.elapsed(time.monotonic() - r.started) if r.busy else 'starting'}{f', {queued} queued' if queued > 0 else ''} · {where}")
    lines.append(f"sessions: {len(mine)} running{f', {asking} waiting for a model, effort and mode' if asking else ''}")
    if last := state.last_pick(tid):
        lines.append(f"last pick: {last[0]} · effort {last[1]} · permission mode {last[2]}")
    return "\n".join(lines)


def callback(cq):
    """A tap on an inline button: the model, effort and permission mode of a waiting request, Stop on a status message, or Push or Clean on a pin and its confirmation. Allowlisted like messages, and a stranger's tap is not answered."""
    msg = cq.get("message") or {}
    if (msg.get("chat") or {}).get("id") != state.GROUP_ID or (cq.get("from") or {}).get("id") != state.OWNER_ID:
        return
    data, pid, note = cq.get("data") or "", msg.get("message_id"), None
    try:
        if data.startswith("stop:") and data[5:].isdigit():
            note = runs.stop(int(data[5:]))
            return
        if data.startswith("push:") and (parts := data.split(":", 2))[1].isdigit():  # push:<topic>[:<subrepo folder>]
            push.spawn(push.ask, int(parts[1]), *parts[2:])
            return
        if data.startswith("pushgo:") and len(parts := data.split(":", 3)) >= 3 and parts[1].isdigit() and parts[2]:  # pushgo:<topic>:<HEAD>[:<subrepo folder>]
            push.spawn(push.go, int(parts[1]), pid, *parts[2:])
            return
        if data == "pushno":
            telegram.delete(pid)
        if data.startswith("push"):  # also a malformed push button: never the picker's "expired" edit below
            return
        if data.startswith("clean:") and data[6:].isdigit():  # clean:<topic>, the pin's Clean button: asks first, the messages cannot be brought back
            telegram.send(int(data[6:]), "Delete the messages of this topic's idle sessions? They can no longer be continued from Telegram.", markup={"inline_keyboard": [[telegram.button("✔ Clean", f"cleango:{data[6:]}"), telegram.button("✖ Cancel", "cleanno")]]})
        elif data.startswith("cleango:") and data[8:].isdigit():
            telegram.edit(pid, "cleaning…")
            clean(int(data[8:]), pid)
        elif data == "cleanno":
            telegram.delete(pid)
        if data.startswith("clean"):
            return
        with state.LOCK:
            p = state.PENDING.get(pid)
            if p and (data in ("x", "last") or data.startswith("p:")):
                state.PENDING.pop(pid)  # under LOCK: a double tap must not start two sessions
        if not p:
            with state.LOCK:
                known = str(pid) in state.STATE["replies"]
            if not known:  # a picker from before a restart, whose request is gone with the memory
                note = "This request is gone, the bot restarted. Send it again."
                telegram.edit(pid, "Expired: the bot restarted before a model, effort and mode were picked. Send the request again.")
            return
        last = state.last_pick(p["tid"])
        if data == "x":
            telegram.delete(pid)
        elif data == "last" and last:
            runs.start_session(pid, p, *last)
        elif data.startswith("p:") and data[2:] in PERMS and p["model"] and p["effort"]:
            runs.start_session(pid, p, p["model"], p["effort"], data[2:])
        elif data.startswith("e:") and data[2:] in EFFORTS and p["model"]:
            p["effort"] = data[2:]
            telegram.edit(pid, *telegram.picker(p["name"], p["model"], p["effort"], last))
        elif data.startswith("m:") and data[2:] in MODELS:
            p["model"], p["effort"] = data[2:], None
            telegram.edit(pid, *telegram.picker(p["name"], p["model"], None, last))
        elif data == "b":  # back one step: from the mode to the effort, from the effort to the model
            if p["effort"]:
                p["effort"] = None
            else:
                p["model"] = None
            telegram.edit(pid, *telegram.picker(p["name"], p["model"], None, last))
        else:  # a stale or unknown button: keep waiting
            with state.LOCK:
                state.PENDING.setdefault(pid, p)
    finally:
        try:
            telegram.tg("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        except Exception as e:
            log.warning("could not answer a button tap: %s", e)


def handle(u, groups):
    if cq := u.get("callback_query"):
        return callback(cq)
    m = u.get("message")
    if not m:
        return
    head = m["text"].split(None, 1)[0] if m.get("text", "").startswith("/") else ""
    if (other := re.fullmatch(r"/\w+@(\w+)", head)) and other.group(1).lower() != state.BOT_NAME:
        return  # a command for another bot in the group; a prompt starting with a path like /usr/lib/@scope/x is not one
    if head and telegram.parse_command(head)[0] in ("setup", "start"):  # /start: what Telegram sends for the t.me/<bot>?start=<code> and ?startgroup=setup links the dashboard shows
        return onboarding.setup(m, groups)
    if (m.get("chat") or {}).get("id") != state.GROUP_ID or (m.get("from") or {}).get("id") != state.OWNER_ID:
        return  # silently: replying would confirm the bot exists
    flush(groups, lambda gid, g: gid != m.get("media_group_id"))  # anything else arriving means a pending album is complete: queue it first, in order
    named = m.get("forum_topic_created") or m.get("forum_topic_edited")
    if named is not None:
        # A topic's thread id is the id of its creation message. A rename of General carries no thread id.
        named_tid = m.get("message_thread_id") or (m["message_id"] if "forum_topic_created" in m else None)
        if named_tid and NAME_RE.match(named.get("name") or ""):
            state.bind(named_tid, named["name"])
        return
    text = m.get("text") or m.get("caption") or ""
    if not text and not m.get("photo") and not m.get("document"):
        return  # stickers, voice, service messages
    # Outside forum topics a reply also carries message_thread_id, so only trust it for topic messages.
    tid = m.get("message_thread_id") if m.get("is_topic_message") else None
    if tid is None:
        return telegram.send(None, HELP)
    if not state.settings(tid).get("name"):
        # Topic messages that are not explicit replies are replies to the topic's creation message, which carries its name. That is the name at creation, so say what was bound.
        created = (m.get("reply_to_message") or {}).get("forum_topic_created") or {}
        if NAME_RE.match(created.get("name") or ""):
            state.bind(tid, created["name"])
            telegram.send(tid, f"bound to dockbench-{created['name']} from the topic name, /bind <PROJECT> to change")
    if m.get("text", "").startswith("/") and (cmd := telegram.parse_command(m["text"]))[0] in COMMANDS:
        log.info("update %s thread %s: command", u["update_id"], tid)
        return command(tid, *cmd, m)
    if m.get("text", "").startswith("/"):
        m["text"] = commands.translate(m["text"], state.settings(tid).get("name"))  # a menu entry back to Claude's own command name
    if m.get("media_group_id"):  # an album arrives as one update per item: collect for a second, run as one prompt
        g = groups.setdefault(m["media_group_id"], {"tid": tid, "uid": u["update_id"], "msgs": []})
        g["msgs"].append(m)
        g["at"] = time.monotonic()
        return
    runs.submit(tid, u["update_id"], [m])


def flush(groups, due):
    for gid, g in list(groups.items()):
        if due(gid, g):
            del groups[gid]
            try:
                runs.submit(g["tid"], g["uid"], g["msgs"])
            except Exception:
                log.exception("update %s failed", g["uid"])
