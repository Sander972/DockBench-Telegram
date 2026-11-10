"""Sessions at work: requests to sessions, one FIFO and worker thread per session, a prompt's run, /stop, the status ticker and restart recovery."""
import os
import queue
import re
import threading
import time
import urllib.parse
import uuid

from . import claude, commands, dockerapi, pins, state, telegram, usage
from .config import BUSY_CPU, DEFAULT_PERM, IDLE_LIMIT, IMAGE_EXTS, INBOX, MAX_IMAGES, OUTBOX, OUTBOX_MAX, RUN_TIMEOUT, log

# a session id the container's claude reports is kept only as a UUID: it goes into --resume and the agent's stop route later, and the container is not trusted
SID_RE = re.compile(r"[0-9a-f-]{36}")


def session_name(key):
    """What tells a session apart on its messages: the title Claude Code gave it, its badge until it has one."""
    return pins.TITLES.get(int(key)) or telegram.badge(key)


def label(key):
    """A session's tag for its next message: session_name() and the message's number in the session, e.g. 'Fix login bug · #3'."""
    with state.LOCK:
        s = state.STATE["sessions"].get(str(key)) or {}
        s["n"] = n = s.get("n", 0) + 1  # saved with the message's link
    return f"{session_name(key)} · #{n}"


class Run:
    """A session while it has work: a FIFO and a worker thread, so its prompts run one after the other while other sessions, in the same topic too, run in parallel. Dropped from ACTIVE when its queue is empty."""

    def __init__(self, key, tid):
        self.key, self.tid, self.q = key, tid, queue.Queue()
        self.busy, self.stopped, self.sid, self.cid = False, False, None, None
        self.status, self.head, self.started, self.req = None, "", 0.0, None  # status message, its text without session_name() and the time, monotonic start, the request it replies to
        self.turns, self.dirty = [], False  # Claude's turn messages not yet moved to messages of their own, and whether the status message lacks some
        self.active, self.probe, self.idle = 0.0, None, False  # monotonic time of the last sign of work, the watchdog's last (time, CPU seconds), killed as hung

    def loop(self):
        while True:
            with state.LOCK:  # queue_job puts under LOCK too, so a job is never left behind a worker that just quit
                try:
                    job = self.q.get_nowait()
                except queue.Empty:
                    state.ACTIVE.pop(self.key, None)
                    return
                self.busy, self.stopped = True, False
            try:
                run(self, *job)
            except Exception as e:
                log.exception("session %s: run failed", self.key)
                try:
                    telegram.send(self.tid, f"bot error: {e}", reply=job[1][0]["message_id"], tag=lambda: label(self.key))
                except Exception:
                    log.exception("session %s: could not report the error", self.key)
            finally:
                self.busy, self.sid, self.status = False, None, None
                self.q.task_done()  # q.unfinished_tasks then counts queued plus running jobs, with no gap between get() and busy
                pins.WAKE.set()
                usage.WAKE.set()


def queue_job(key, tid, job):
    """Returns how many jobs of this session run or wait ahead of this one."""
    with state.LOCK:
        r = state.ACTIVE.get(key)
        new = r is None
        if new:
            r = state.ACTIVE[key] = Run(key, tid)
        ahead = r.q.unfinished_tasks
        r.q.put(job)
    if new:
        threading.Thread(target=r.loop, daemon=True).start()
    pins.WAKE.set()
    return ahead


def busy_anywhere():
    with state.LOCK:
        return any(r.q.unfinished_tasks for r in state.ACTIVE.values())


def ask(tid, update_id, msgs):
    """A message that is not a reply to a session: ask model, effort and permission mode with a keyboard replying to it, the run starts on the last tap (see routing.callback)."""
    p = {"tid": tid, "uid": update_id, "msgs": msgs, "model": None, "effort": None, "name": state.settings(tid)["name"]}
    text, markup = telegram.picker(p["name"], None, None, state.last_pick(tid))
    pid = telegram.send(tid, text, reply=msgs[0]["message_id"], markup=markup)[-1]
    with state.LOCK:
        state.PENDING[pid] = p
    log.info("update %s thread %s: asking model, effort and mode", update_id, tid)


def follow_up(key, tid, update_id, msgs):
    state.link(key, [m["message_id"] for m in msgs])
    ahead = queue_job(key, tid, (update_id, msgs, None))  # before the notice, so a failed notice cannot lose the prompt
    log.info("update %s thread %s: follow-up queued in session %s", update_id, tid, key)
    if ahead:
        telegram.send(tid, f"queued in this session, {ahead} ahead", reply=msgs[0]["message_id"], tag=lambda: label(key))


def submit(tid, update_id, msgs):
    key = state.session_of(msgs)
    if key:
        return follow_up(key, tid, update_id, msgs)
    if not state.settings(tid).get("name"):
        return telegram.send(tid, "unbound topic, send /bind <PROJECT>")
    ask(tid, update_id, msgs)


def start_session(pid, p, model, effort, perm):
    """The picker's last tap: the session is keyed by the request's first message id, and the picker message becomes its status message."""
    tid, key = p["tid"], p["msgs"][0]["message_id"]
    with state.LOCK:
        state.STATE["sessions"][str(key)] = {"tid": tid, "name": p["name"], "sid": str(uuid.uuid4()), "model": model, "effort": effort, "perm": perm, "at": int(time.time())}
        t = state.STATE["threads"].setdefault(str(tid), {})
        t["model"], t["effort"], t["perm"] = model, effort, perm
        for i in [m["message_id"] for m in p["msgs"]] + [pid]:
            state.STATE["replies"][str(i)] = key
        state.prune()
        state.save()
    log.info("update %s thread %s: new session %s in dockbench-%s", p["uid"], tid, key, p["name"])
    queue_job(key, tid, (p["uid"], p["msgs"], pid))


def stop(key):
    """Kill the session's running prompt and drop its queued ones. Returns what happened, for the reply."""
    with state.LOCK:
        r = state.ACTIVE.get(key)
    if not r:
        return "nothing running"
    with r.q.mutex:
        dropped = list(r.q.queue)
        r.q.queue.clear()
        r.q.unfinished_tasks -= len(dropped)
    note = f", dropped {len(dropped)} queued" if dropped else ""
    if not r.busy:
        return f"nothing running{note}"
    r.stopped = True
    # the agent signals only this run's timeout wrapper, never the VS Code panel's claude of the same session (--resume=<sid>); timeout passes it on to its whole process group
    if (sid := r.sid) and r.cid and dockerapi.agent(r.cid, ["stop", sid]) is None:
        log.warning("session %s: stop: dockbench-agent gave no answer", key)
        return f"could not reach dockbench-agent in the container to stop the run{note}"
    return f"stopping{note}"


def run(r, update_id, msgs, status):
    """One prompt of session r. status: the picker message to turn into the status message, else a new one is sent."""
    key, tid, req = r.key, r.tid, msgs[0]["message_id"]
    meta = state.sess(key)
    name = meta["name"]
    c = dockerapi.container(name)
    if not c or c["State"] != "running":
        if status:
            telegram.delete(status)
        telegram.send(tid, f"dockbench-{name} is {c['State'] if c else 'not found'}, prompt not run.", reply=req, tag=lambda: label(key))
        return
    resume = bool(meta.get("used"))
    r.head = f"working · {meta['model']} · effort {meta['effort']} · {meta.get('perm') or DEFAULT_PERM} · {'resumed' if resume else 'new'} session"
    if status:
        telegram.edit(status, f"{session_name(key)} · {r.head}", telegram.stop_markup(key))  # the picker's buttons go at once, before the downloads
    else:
        status = telegram.send(tid, f"{session_name(key)} · {r.head}", reply=req, markup=telegram.stop_markup(key))[-1]
        state.link(key, [status])
    r.status, r.started, r.req, r.turns, r.dirty = status, time.monotonic(), req, [], False
    r.active, r.probe, r.idle = r.started, None, False
    pins.WAKE.set()  # no usage.WAKE here: its pass is another whole Claude Code start, which would land on this run's own start; the one when the run ends is enough
    state.sess_update(key, working=status, req=req, at=int(time.time()))  # lets a restarted bot tell this request that the run was killed
    out = f"{OUTBOX}/{key}"
    try:
        files = []
        for n, (file_id, is_image) in enumerate(telegram.attachments(msgs)):
            ext, data = telegram.download(file_id)
            files.append((f"{update_id}-{n}{ext}", data, is_image))
        # the agent drops old attachments, or the home volume grows with every photo, and empties the outbox, so it holds only this run's images
        if dockerapi.agent(c["Id"], ["prepare", str(key)]) is None:
            raise RuntimeError(f"dockbench-agent in dockbench-{name} gave no answer, prompt not run: update its image (DockBench from 2026-11-10 or later)")
        if files:
            dockerapi.docker("PUT", f"/containers/{c['Id']}/archive?path={urllib.parse.quote(INBOX)}", dockerapi.tarball([(f, d) for f, d, _ in files]))
    except Exception:
        r.status = None
        state.sess_update(key, working=None)
        telegram.delete(status)
        raise  # Run.loop answers with the error
    text = "\n\n".join(t for m in msgs if (t := m.get("text") or m.get("caption")))
    prompt = claude.prompt_for(text, [(f, img) for f, _, img in files])

    def remember(s):  # the first stream event: the session now exists in the container, follow-ups resume it
        if SID_RE.fullmatch(s):
            state.sess_update(key, sid=s, used=True)

    def said(t):  # ticker() puts it in the status message: no Telegram call here, it would hold up the stream
        with state.LOCK:
            r.turns, r.dirty, r.active = r.turns + [t], True, time.monotonic()

    result = code = err = init = None
    try:
        for again in ([True, False] if resume else [False]):
            if r.stopped:
                break
            r.sid, r.cid = (meta["sid"] if again or not resume else str(uuid.uuid4())), c["Id"]  # /stop targets this container
            log.info("update %s session %s: claude in dockbench-%s, session %s", update_id, key, name, r.sid)
            result, code, err, init = claude.run_claude(c["Id"], meta, prompt, r.sid, again, out, remember, said)
            if not (again and "No conversation found" in err):
                break
            log.info("session %s: claude session %s gone from the container, starting a new one", key, r.sid)
    finally:
        with state.LOCK:
            status, r.status = r.status, None  # ticker() may have moved it to a new message
        state.sess_update(key, working=None)
        telegram.delete(status)
    log.info("update %s session %s: exit %s", update_id, key, code)
    if int(key) not in pins.TITLES and r.sid:  # a first run can end before the pins pass reads the title Claude wrote
        try:
            if t := claude.title(c["Id"], r.sid):
                pins.TITLES[int(key)] = t
        except Exception as e:
            log.warning("session %s: title: %s", key, e)
    commands.learn(name, init)  # the menu follows on the next pins pass, which Run.loop wakes
    if r.stopped and (result is None or result.get("is_error")):  # a /stop that came after the answer does not hide it
        answer = "stopped."
    elif r.idle and (result is None or result.get("is_error")):
        answer = f"no activity for {IDLE_LIMIT // 60} min (nothing written to the session, almost no CPU use), killed as hung. Reply to continue the session."
    elif code == 124:
        answer = f"timed out after {RUN_TIMEOUT // 60} min, killed. Reply to continue the session."
    else:
        answer = claude.summarize(result, code, err, init, meta.get("perm") or DEFAULT_PERM)
    state.link(key, telegram.send(tid, answer, reply=req, md=True, tag=lambda: label(key)))
    send_outbox(key, tid, c["Id"], out, req)


def send_outbox(key, tid, cid, out, req):
    """After the answer: the images Claude left in the session's outbox, as a reply to the request, then the outbox is emptied. A failure is reported, not raised: the answer is already sent."""
    try:
        found = sorted((n, d) for n, d in dockerapi.get_files(cid, out, OUTBOX_MAX) if os.path.splitext(n)[1].lower() in IMAGE_EXTS)
        if found:
            images = [(f"{i + 1}{os.path.splitext(n)[1].lower()}", d) for i, (n, d) in enumerate(found[:MAX_IMAGES])]  # our names: the container's go into a multipart header
            state.link(key, telegram.send_images(tid, images, reply=req, caption=label(key)))
            log.info("session %s: sent %d images", key, len(images))
        if len(found) > MAX_IMAGES:
            state.link(key, telegram.send(tid, f"⚠️ {len(found) - MAX_IMAGES} more images not sent (at most {MAX_IMAGES} per answer).", reply=req, tag=lambda: label(key)))
    except Exception as e:
        log.warning("session %s: outbox: %s", key, e)
        state.link(key, telegram.send(tid, f"⚠️ could not send Claude's images: {e}", reply=req, tag=lambda: label(key)))
    if dockerapi.agent(cid, ["outbox-clear", str(key)]) is None:
        log.warning("session %s: could not empty the outbox: dockbench-agent gave no answer", key)


def ticker():
    """Own thread: shows that sessions run in the background. Every 5 s the bot is "typing" in each topic with a running session (Telegram shows it for 5 s), and each status message gets Claude's new turn messages, or every 30 s the elapsed time. Turn text that no longer fits the status message is never cut: it goes to messages of its own and a new status message follows (split())."""
    n = 0
    while True:
        time.sleep(5)
        n += 1
        with state.LOCK:
            shown = [(r, r.status, r.head, r.started, r.active, r.turns, r.dirty) for r in state.ACTIVE.values() if r.busy and r.status]
            for r in state.ACTIVE.values():
                r.dirty = False
        for tid in {x[0].tid for x in shown}:
            try:
                telegram.tg("sendChatAction", chat_id=state.GROUP_ID, message_thread_id=tid, action="typing")
            except Exception:
                pass
        for r, status, head, started, active, turns, dirty in shown:
            if n % 6 and not dirty:
                continue
            now = time.monotonic()
            quiet = f" · last activity {telegram.elapsed(now - active)} ago" if now - active >= 60 else ""
            head = f"{session_name(r.key)} · {head} · {telegram.elapsed(now - started)}{quiet}"
            try:
                full, rest = telegram.overflow(head, turns)
                if full:
                    split(r, status, head, turns, full, rest)
                else:
                    telegram.edit(status, telegram.status_text(head, turns), telegram.stop_markup(r.key))
            except Exception:
                pass  # finished and deleted in between


def split(r, status, head, turns, full, rest):
    """The status message of run r is full: its turn text so far is kept, the first part in it (tag instead of head, no Stop button) and the others in new messages, then a new status message under them holds the rest. turns: what r.turns held when head was made."""
    first = label(r.key)  # numbered before the parts that follow it
    ids = [telegram.send(r.tid, p, reply=r.req, md=True, tag=lambda: label(r.key))[-1] for p in full[1:]]
    new = telegram.send(r.tid, telegram.status_text(head, rest), reply=r.req, markup=telegram.stop_markup(r.key))[-1]
    with state.LOCK:
        moved = r.status == status
        if moved:  # run() only appends to r.turns while this status message is up
            r.status, r.turns, r.dirty = new, rest + r.turns[len(turns):], True
    if not moved:  # the run ended meanwhile and deleted its status message
        telegram.delete(new)
        state.link(r.key, ids)
        return
    state.link(r.key, ids + [new])
    state.sess_update(r.key, working=new)  # what a restarted bot deletes
    telegram.edit(status, full[0], md=True, head=first)


def seen(r, now, cpu, age):
    """One watchdog probe of run r (claude.activity): moves r.active to its last sign of work, a session write or BUSY_CPU since the previous probe. Returns True when the run is hung."""
    with state.LOCK:
        if age is not None:
            r.active = max(r.active, now - age)
        if r.probe and now > r.probe[0] and (cpu - r.probe[1]) / (now - r.probe[0]) >= BUSY_CPU:
            r.active = now
        r.probe = (now, cpu)
        return now - r.active >= IDLE_LIMIT


def watchdog():
    """Own thread: every minute, the activity of each running prompt in its container. A hung one is killed like /stop, but its queued prompts are kept."""
    while True:
        time.sleep(60)
        with state.LOCK:
            watched = [(r, r.cid, r.sid, r.started) for r in state.ACTIVE.values() if r.busy and r.cid and r.sid and not r.stopped and not r.idle]
        for r, cid, sid, started in watched:
            try:
                found = claude.activity(cid, sid)
            except Exception as e:
                log.warning("session %s: watchdog: %s", r.key, e)
                continue
            if not found or not seen(r, time.monotonic(), *found) or r.started != started or r.sid != sid:  # the prompt ended, or the next one began, since the snapshot
                continue
            r.idle = True
            log.info("session %s: no activity for %d min, killing the run", r.key, IDLE_LIMIT // 60)
            if dockerapi.agent(cid, ["stop", sid]) is None:
                log.warning("session %s: watchdog kill: dockbench-agent gave no answer", r.key)


def clean(tid, key=None):
    """/clean: delete the messages recorded for this topic's idle sessions (requests, follow-ups, answers, status messages), or only for session key (/clean as a reply to it), then forget the sessions whose messages are all gone. Returns the reply. Slow (Telegram calls), so not on the polling thread."""
    with state.LOCK:
        keys = {k for k, s in state.STATE["sessions"].items() if s.get("tid") == tid and int(k) not in state.ACTIVE and not s.get("working") and (key is None or k == str(key))}
        ids = sorted(int(m) for m, k in state.STATE["replies"].items() if str(k) in keys)
    if not keys:
        return "no idle sessions to clean" if key is None else "this session is running, /stop it first"
    failed = set()
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        try:
            telegram.tg("deleteMessages", chat_id=state.GROUP_ID, message_ids=batch)
        except telegram.TgError:  # one message Telegram refuses may fail the whole batch: try them one by one
            for mid in batch:
                try:
                    telegram.tg("deleteMessage", chat_id=state.GROUP_ID, message_id=mid)
                except telegram.TgError as e:
                    if "not found" not in str(e).lower():  # already deleted by hand
                        failed.add(mid)
    with state.LOCK:
        done = {k for k in keys if int(k) not in state.ACTIVE}  # a reply to an old message in the meantime made it a session at work again
        state.STATE["replies"] = {m: k for m, k in state.STATE["replies"].items() if str(k) not in done or int(m) in failed}
        left = {str(k) for k in state.STATE["replies"].values()}
        for k in done - left:
            state.STATE["sessions"].pop(k, None)  # prune() may have dropped it in between
        for k in done & left:
            pins.PROBED.pop(int(k), None)  # a refused message may be one deleted by hand: the next pins pass checks, and forgets the session if none is left
        state.save()
    pins.WAKE.set()  # the pin lists idle sessions: drop the cleaned ones
    log.info("thread %s: cleaned %d sessions, %d messages not deleted", tid, len(done - left), len(failed))
    note = f"deleted the messages of {len(done - left)} idle session(s)" if key is None else f"deleted the messages of session {session_name(key)}"
    if failed:
        note += f"; {len(failed)} message(s) could not be deleted (the bot needs the Delete messages admin right, and Telegram may refuse old messages), send /clean again to retry"
    return note


def purge(tid):
    """/purge confirmed: stop the topic's sessions, delete the topic with every message in it (a bot cannot list a topic's messages, so deleting them one by one would miss some), then create a new topic of the bound name with the same settings and forget the old topic's sessions. Returns the new topic's id. Slow, so not on the polling thread."""
    name = state.settings(tid)["name"]
    with state.LOCK:
        keys = [k for k, r in state.ACTIVE.items() if r.tid == tid]
        for pid in [p for p, w in state.PENDING.items() if w["tid"] == tid]:
            state.PENDING.pop(pid)
    for k in keys:
        stop(k)
    for _ in range(30):  # a stopped run still posts its reply: let it land before its topic goes
        with state.LOCK:
            if not any(r.tid == tid for r in state.ACTIVE.values()):
                break
        time.sleep(1)
    telegram.tg("deleteForumTopic", chat_id=state.GROUP_ID, message_thread_id=tid)  # first: if refused, nothing has changed
    with state.LOCK:
        cfg = state.STATE["threads"].pop(str(tid), {})
        gone = {k for k, s in state.STATE["sessions"].items() if s.get("tid") == tid}
        for k in gone:
            del state.STATE["sessions"][k]
        state.STATE["replies"] = {m: k for m, k in state.STATE["replies"].items() if str(k) not in gone}
        state.save()
    log.info("thread %s: purged, %d sessions forgotten", tid, len(gone))
    try:
        new = telegram.tg("createForumTopic", idempotent=False, chat_id=state.GROUP_ID, name=name)["message_thread_id"]
    except telegram.TgError as e:  # the name stays in STATE["created"], so autocreate cannot make a duplicate: a topic created by hand binds by its name
        log.warning("thread %s: purged, but no new topic: %s", tid, e)
        telegram.send(None, f"The topic of dockbench-{name} was purged, but a new one could not be created ({type(e).__name__}): create a topic named {name}.")
        return None
    state.update(new, **{k: v for k, v in cfg.items() if k not in ("pin", "pinned", "working")})
    pins.WAKE.set()
    telegram.send(new, f"dockbench-{name}: the old topic was deleted with all its messages and {len(gone)} session(s). Post a prompt here to start a new session.")
    return new


def recover():
    """A fresh process owns no runs, but a docker exec outlives the client that started it: stop what a previous instance left running (each running container's agent, /stop-all), and tell its requests."""
    running = None
    for _ in range(12):  # a socket proxy restarted with the bot may need a moment
        try:
            running = [c for c in dockerapi.dev_containers() if c["State"] == "running"]
            break
        except Exception as e:
            log.warning("could not list the dev containers to stop leftover runs: %s", e)
            time.sleep(5)
    answers = [dockerapi.agent(c["Id"], ["stop-all"]) for c in running or []]  # every container is asked, even after one gave no answer
    stopped = running is not None and None not in answers
    note = ("The bot restarted during this run, so the run was stopped. Reply to continue the session." if stopped else f"The bot restarted during this run and could not reach Docker or the container's dockbench-agent to stop it, so it may keep going for up to {RUN_TIMEOUT // 60} min. Wait for it before replying.") + " Prompts queued behind it, if any, were dropped too."
    with state.LOCK:
        left = [(k, s["tid"], s["working"], s.get("req")) for k, s in state.STATE["sessions"].items() if s.get("working")]
        left += [(None, int(k), t["working"], None) for k, t in state.STATE["threads"].items() if t.get("working")]  # a run of the topic-session bot this one replaced
    for key, tid, working, req in left:
        if key:
            state.sess_update(key, working=None)
        else:
            state.update(tid, working=None)
        telegram.delete(working)
        try:
            ids = telegram.send(tid, note, reply=req, tag=(lambda: label(key)) if key else None)
            if key:
                state.link(int(key), ids)
        except Exception as e:
            log.warning("thread %s: could not report the stopped run: %s", tid, e)
