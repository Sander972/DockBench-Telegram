"""Pinned status per topic: the workspace repo, its branch, its uncommitted changes and the topic's sessions (running, queued and idle), kept up to date in one pinned bot message."""
import html
import json
import threading
import time

from . import claude, commands, dockerapi, state, telegram
from .config import DEFAULT_PERM, IDLE, PIN_IDLE, PROBE_EVERY, PROBE_MAX, QUEUED, RUNNING, WORKSPACE, log

WAKE = threading.Event()  # set when a session is queued, starts or ends, so the pin shows it without waiting for the next minute
SHOWN = {}  # topic id -> (message id, text) last shown, so an unchanged pin is not edited
FAILED = {}  # topic id -> last error, logged once
PROBED = {}  # session key -> time.monotonic() of its last sweep() check
TITLES = {}  # session key -> the title Claude Code gave it; memory only, never state.json: it is made from the prompts
UNTITLED = set()  # idle session keys whose transcript had no title when read, not read again until they run again


def parse_status(out):
    """Branch, HEAD commit, untracked count, whether the branch has a usable upstream, whether its upstream is gone, and how many commits it is ahead of it, from `git status --porcelain=v2 --branch`. Git leaves out `# branch.ab` when the upstream ref is gone (a remote branch deleted after a merge). Other lines (tracked changes, a stray stderr warning) are ignored."""
    head, oid, untracked, named, ahead = None, "", 0, False, None
    for line in out.splitlines():
        if line.startswith("# branch.upstream "):
            named = True
        elif line.startswith("# branch.ab +") and line.split()[2][1:].isdigit():
            ahead = int(line.split()[2][1:])
        elif line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
        elif line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):]
        elif line.startswith("? "):
            untracked += 1
    return {"branch": f"detached {oid[:7]}" if head == "(detached)" else head or "?", "oid": oid, "untracked": untracked, "upstream": named and ahead is not None, "gone": named and ahead is None, "ahead": ahead or 0}


def parse_numstat(out):
    """Added and deleted lines and changed files from `git diff HEAD --numstat`; a binary file counts as a file with no lines."""
    add = dele = files = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not all(p.isdigit() or p == "-" for p in parts[:2]):
            continue
        files += 1
        add += int(parts[0]) if parts[0].isdigit() else 0
        dele += int(parts[1]) if parts[1].isdigit() else 0
    return {"add": add, "del": dele, "files": files}


def repo_name(url):
    """Last path part of the origin URL without .git: the URL may hold a credential, so only this part is ever shown."""
    name = url.strip().rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name or "workspace"


def git_info(cid):
    """Git facts of the container's workspace, None when it is not a git repo. Plain argv execs as dev, no shell."""
    code, out = dockerapi.exec_run(cid, ["git", "status", "--porcelain=v2", "--branch"])
    if code != 0:
        return None
    info = parse_status(out.decode(errors="replace"))
    code, out = dockerapi.exec_run(cid, ["git", "diff", "HEAD", "--numstat"])
    info.update(parse_numstat(out.decode(errors="replace") if code == 0 else ""))  # fails before the first commit: no HEAD
    code, out = dockerapi.exec_run(cid, ["git", "remote", "get-url", "origin"])
    info["repo"] = repo_name(out.decode(errors="replace")) if code == 0 else "workspace"
    return info


def parse_subrepos(out):
    """Folder names of the repos one level inside the workspace, from `find` lines `<WORKSPACE>/<folder>/.git`; anything else (a `find` error on stderr) is ignored."""
    head = WORKSPACE + "/"
    return sorted({line[len(head):-5] for line in out.splitlines() if line.startswith(head) and line.endswith("/.git") and "/" not in line[len(head):-5] and line[len(head):-5]})


def subrepos(cid):
    """Branch and push facts of each repo one level inside the workspace (the repos of a multi-repo workspace), sorted by folder, `dir` = the folder name. Their uncommitted changes are not read."""
    code, out = dockerapi.exec_run(cid, ["find", WORKSPACE, "-mindepth", "2", "-maxdepth", "2", "-name", ".git"])  # exit 1 on an unreadable folder, the lines found still count
    subs = []
    for d in parse_subrepos(out.decode(errors="replace")):
        code, out = dockerapi.exec_run(cid, ["git", "-C", f"{WORKSPACE}/{d}", "status", "--porcelain=v2", "--branch", "--untracked-files=no"])
        if code == 0:
            subs.append(dict(parse_status(out.decode(errors="replace")), dir=d))
    return subs


def pin_facts(cid):
    """(git_info(), subrepos()) of the container from dockbench-agent's pin-info route, which reads git in-process (only `git diff HEAD --numstat` still runs, and only while tracked files changed). Raises when it gives nothing usable, so refresh() leaves the pin as it was and logs it once."""
    r = dockerapi.agent(cid, ["pin-info", WORKSPACE])
    try:
        d = json.loads(r[1]) if r and r[0] == 0 else None
        return d["git"], d["subs"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("dockbench-agent pin-info gave no usable answer (an image from before 2026-11-09? update it)") from None


def push_note(g):
    """What the branch line says about pushing: commits ahead, or why there is no upstream to push to."""
    return f" · ↑{g['ahead']} to push" if g["upstream"] and g["ahead"] else " · upstream gone" if g["gone"] else " · no upstream" if not g["upstream"] and not g["branch"].startswith("detached ") else ""


def fits(tid, d):
    """Whether the confirmation button of subrepo d fits Telegram's 64-byte callback_data."""
    return len(f"pushgo:{tid}:{'0' * 12}:{d}".encode()) <= 64


def changes(g):
    parts = [f"+{g['add']} −{g['del']} in {g['files']} file{'' if g['files'] == 1 else 's'}"] if g["files"] else []
    if g["untracked"]:
        parts.append(f"{g['untracked']} untracked")
    return "✏️ " + (", ".join(parts) if parts else "clean")


def ago(seconds):
    """Coarse age of an idle session, so the pin is not edited every minute for it."""
    h = int(seconds) // 3600
    return "<1h" if h < 1 else f"{h}h" if h < 48 else f"{h // 24}d"


def anchor(link, title=None):
    """A session row's link to its last message as an anchor named after the session's title ("last message" until it has one), not the bare URL; the title alone without a link."""
    text = html.escape(title or "last message")
    return f' · <a href="{html.escape(link)}">{text}</a>' if link else f" · {text}" if title else ""


def pin_text(name, health, git, sessions, idle=(), subs=()):
    """Telegram HTML: every plain part escaped (a branch name may hold `<` or `&`), each session's link a "last message" anchor. health: container state; git: git_info() or None; sessions: (badge, model, effort, permission mode, minutes running or None when queued, queued behind, link, title or None); idle: (badge, model, effort, permission mode, seconds since its last prompt, link, title or None), newest first, only the first PIN_IDLE listed; subs: subrepos(), only those with something to say about pushing are listed."""
    repo = git["repo"] if health == "running" and git else name
    lines = [f"📦 {repo}" + ("" if repo == name else f" ({name})")]
    if health != "running":
        lines.append(f"container {health}")
    elif git is None:
        lines.append("workspace: not a git repo")
    else:
        lines += [f"🌿 {git['branch']}{push_note(git)}", changes(git)]
    if health == "running":
        lines += [f"📂 {s['dir']} · {s['branch']}{push_note(s)}" for s in subs if push_note(s)]
    lines = [html.escape(line) for line in lines]
    if not sessions and not idle:
        lines.append("sessions: none")
    else:
        lines.append("sessions:")
        for tag, model, effort, perm, minutes, queued, link, title in sessions:
            when = QUEUED + " queued" if minutes is None else f"{RUNNING} {minutes}m" if minutes else f"{RUNNING} <1m"
            lines.append(html.escape(f"{tag} {when} · {model} · {effort} · {perm}{f' · {queued} queued' if queued > 0 else ''}") + anchor(link, title))
        for tag, model, effort, perm, seconds, link, title in idle[:PIN_IDLE]:
            lines.append(html.escape(f"{tag} {IDLE} {ago(seconds)} · {model} · {effort} · {perm}") + anchor(link, title))
        if len(idle) > PIN_IDLE:
            lines.append(html.escape(f"{IDLE} +{len(idle) - PIN_IDLE} older, /clean deletes the idle ones"))
    return "\n".join(lines)


def rows(tid):
    """pin_text() sessions and idle rows of the topic, from one snapshot under LOCK so a run that starts or ends in between is listed exactly once. With work: a session counts as running once its status message is up, its other prompts as queued behind. Idle: the sessions in state.json with nothing running or queued and no `working` left by a killed run, the same set /clean deletes, which a reply still continues; newest last prompt first. Each row links the session's last message (message ids grow in a chat), so the pin leads to where the session is now."""
    mono, now = time.monotonic(), time.time()
    with state.LOCK:
        work = [(r.key, r.busy and r.status, r.started, state.queued(r), state.STATE["sessions"].get(str(r.key)) or {}) for r in state.ACTIVE.values() if r.tid == tid]
        rest = [(int(k), dict(s)) for k, s in state.STATE["sessions"].items() if s.get("tid") == tid and int(k) not in state.ACTIVE and not s.get("working")]
        last = {}
        for m, k in state.STATE["replies"].items():
            last[k] = max(last.get(k, k), int(m))
    return ([(telegram.badge(key), s.get("model"), s.get("effort"), s.get("perm") or DEFAULT_PERM, int(mono - started) // 60 if running else None, behind, telegram.msg_link(tid, last.get(key, key)), TITLES.get(key)) for key, running, started, behind, s in sorted(work, key=lambda x: x[0])],
            [(telegram.badge(key), s.get("model"), s.get("effort"), s.get("perm") or DEFAULT_PERM, max(0, now - s.get("at", 0)), telegram.msg_link(tid, last.get(key, key)), TITLES.get(key)) for key, s in sorted(rest, key=lambda x: x[1].get("at", 0), reverse=True)])


def titles(tid, cid):
    """Read the titles Claude Code gave the topic's sessions that have none yet, one exec each: a running session's on every pass until it shows up (Claude writes it during the first run), a listed idle one's once (again after its next run). Only sessions that exist in the container (`used`)."""
    with state.LOCK:
        busy = {r.key for r in state.ACTIVE.values() if r.tid == tid and r.busy}
        idle = [int(k) for k, s in sorted(state.STATE["sessions"].items(), key=lambda x: x[1].get("at", 0), reverse=True) if s.get("tid") == tid and int(k) not in state.ACTIVE and not s.get("working")][:PIN_IDLE]
        want = {k: s["sid"] for k in busy | set(idle) if k not in TITLES and (k in busy or k not in UNTITLED) and (s := state.STATE["sessions"].get(str(k)) or {}).get("used") and s.get("sid")}
    for key, sid in want.items():
        if t := claude.title(cid, sid):
            TITLES[key] = t
        elif key in busy:
            UNTITLED.discard(key)  # read once more when idle: a short run can end before its title is written
        else:
            UNTITLED.add(key)


def exists(mid):
    """Whether a message is still in the chat. The Bot API sends no update when one is deleted, so this asks for a markup edit that changes nothing: only a deleted message answers "not found" (the bot's own answer "not modified", others' "can't be edited"). Idle sessions' messages carry no buttons, so nothing is changed; any other error counts as still there."""
    try:
        telegram.tg("editMessageReplyMarkup", chat_id=state.GROUP_ID, message_id=mid)
    except telegram.TgError as e:
        return not any(s in str(e).lower() for s in ("message to edit not found", "message_id_invalid"))
    return True


def sweep(tids):
    """Forget the recorded messages deleted by hand from the chat, and an idle session once none is left, so the pin stops listing it. Only the idle sessions each topic's pin lists (newest first), each at most every PROBE_EVERY and at most PROBE_MAX calls a pass; a session's messages are checked newest first, stopping at the first one still there."""
    mono = time.monotonic()
    with state.LOCK:
        idle = {}
        for k, s in sorted(state.STATE["sessions"].items(), key=lambda x: x[1].get("at", 0), reverse=True):
            if s.get("tid") in tids and int(k) not in state.ACTIVE and not s.get("working"):
                idle.setdefault(s["tid"], []).append(int(k))
        mids = {}
        for m, k in state.STATE["replies"].items():
            mids.setdefault(int(k), []).append(int(m))
    budget, gone, empty = PROBE_MAX, set(), set()
    for key in [k for keys in idle.values() for k in keys[:PIN_IDLE] if mono - PROBED.get(k, mono - PROBE_EVERY) >= PROBE_EVERY]:
        if not budget:
            break
        left, alive = sorted(mids.get(key, ()), reverse=True), False
        while left and budget and not alive:
            budget -= 1
            alive = exists(left[0])
            if not alive:
                gone.add(left.pop(0))
        if alive or not left:  # not when the budget ran out first: the next pass goes on
            PROBED[key] = mono
        if not left:
            empty.add(key)
    if not gone and not empty:
        return
    with state.LOCK:
        state.STATE["replies"] = {m: k for m, k in state.STATE["replies"].items() if int(m) not in gone}
        left = {int(k) for k in state.STATE["replies"].values()}
        for key in empty - left:
            s = state.STATE["sessions"].get(str(key))
            if s and key not in state.ACTIVE and not s.get("working"):  # a reply in between made it a session at work again
                del state.STATE["sessions"][str(key)]
                PROBED.pop(key, None)
        state.save()
    log.info("forgot %d messages deleted from the chat and %d sessions without any left", len(gone), len(empty - left))


def pin_markup(tid, git, subs=(), idle=0):
    """The pin's ⬆ Push buttons, one per repo (the workspace, then its subrepos) whose branch has commits its upstream lacks (see push.py), then 🧹 Clean when the topic has idle sessions (/clean after a confirmation tap)."""
    ready = [(git["repo"], git["ahead"], f"push:{tid}")] if git and git["upstream"] and git["ahead"] else []
    ready += [(s["dir"], s["ahead"], f"push:{tid}:{s['dir']}") for s in subs if s["upstream"] and s["ahead"] and fits(tid, s["dir"])]
    keys = [[telegram.button(f"⬆ {repo} ({n})", data)] for repo, n, data in ready] + ([[telegram.button(f"🧹 Clean idle sessions ({idle})", f"clean:{tid}")]] if idle else [])
    return {"inline_keyboard": keys} if keys else None


def show(group, tid, text, markup=None):
    """Edit the topic's pin, or send a new one when it has none or it was deleted. A pin not pinned yet (no Pin messages right) is pinned again on every pass until it works. markup: pin_markup()."""
    mid = state.settings(tid).get("pin")
    if mid and SHOWN.get(tid) != (mid, text, markup):
        try:
            telegram.tg("editMessageText", chat_id=group, message_id=mid, text=text, parse_mode="HTML", reply_markup=markup, link_preview_options={"is_disabled": True})
        except telegram.TgError as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message_id_invalid" in err:
                mid = None
            elif "not modified" not in err:
                raise
    if not mid:
        mid = telegram.tg("sendMessage", chat_id=group, message_thread_id=tid, text=text, parse_mode="HTML", reply_markup=markup, disable_notification=True, link_preview_options={"is_disabled": True})["message_id"]
        if state.GROUP_ID != group:  # /setup move in between: the topic ids are gone
            return
        state.update(tid, pin=mid, pinned=None)
    SHOWN[tid] = (mid, text, markup)
    if not state.settings(tid).get("pinned"):
        telegram.tg("pinChatMessage", chat_id=group, message_id=mid, disable_notification=True)
        state.update(tid, pinned=True)


def refresh():
    group = state.GROUP_ID
    with state.LOCK:
        bound = {int(k): t["name"] for k, t in state.STATE["threads"].items() if t.get("name")}
    if not bound:
        return
    sweep(set(bound))
    by_name = {dockerapi.project(c): c for c in dockerapi.dev_containers()}
    gits, subs = {}, {}
    for tid, name in sorted(bound.items()):
        c = by_name.get(name)
        health = c["State"] if c else "not found"
        try:
            if health == "running" and name not in gits:
                gits[name], subs[name] = pin_facts(c["Id"])
            if health == "running":
                titles(tid, c["Id"])
            sessions, idle = rows(tid)
            show(group, tid, pin_text(name, health, gits.get(name), sessions, idle, subs=subs.get(name, ())), pin_markup(tid, gits.get(name), subs.get(name, ()), len(idle)))  # git info only for a running container: no Push buttons otherwise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if FAILED.get(tid) != err:  # once per error, not every minute
                log.warning("thread %s: could not update the pinned status: %s", tid, err)
            FAILED[tid] = err
        else:
            FAILED.pop(tid, None)


def pins_loop():
    """Own thread: a pass every minute, or ~2 s after WAKE. Git execs and Telegram retries must not hold up getUpdates. Also keeps the group's command menu up to date."""
    last = None
    while True:
        if state.GROUP_ID and state.OWNER_ID:
            try:
                refresh()
                commands.refresh()
                last = None
            except Exception as e:
                if str(e) != last:
                    log.warning("pinned status update failed: %s", e)
                last = str(e)
        if WAKE.wait(60):
            time.sleep(2)  # a run that ends lets its Run leave ACTIVE first, and a burst of wakes makes one pass
        WAKE.clear()
