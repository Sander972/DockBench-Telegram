"""The pin's ⬆ Push buttons and /push: `git push` of the container's workspace repo or one of its subrepos, after a confirmation tap. Runs on its own thread, never the polling loop."""
import re
import threading

from . import dockerapi, pins, state, telegram
from .config import EXEC_ENV, WORKSPACE, log

BUSY = set()  # PROJECT names with a push in flight, so a double tap pushes once


def scrub(out):
    """git's output without remote URLs (https or scp-like): they can hold a credential."""
    return re.sub(r"\S+://\S+|\S+@[^\s:]+:\S+", "<remote>", out)


def running_in(name):
    """Whether a bot session runs or waits in container dockbench-<name>: a push then could carry half-done work."""
    with state.LOCK:
        return any(r.q.unfinished_tasks and (state.STATE["sessions"].get(str(r.key)) or {}).get("name") == name for r in state.ACTIVE.values())


def check(tid, sub=None):
    """(name, container id, git info, None) when the topic's workspace, or its subrepo in folder sub, has commits to push, else (.., the reason)."""
    name = state.settings(tid).get("name")
    if not name:
        return None, None, None, "unbound topic, send /bind <PROJECT>"
    c = dockerapi.container(name)
    if not c or c["State"] != "running":
        return name, None, None, f"dockbench-{name} is {c['State'] if c else 'not found'}, nothing pushed."
    if running_in(name):
        return name, None, None, f"A session is running in dockbench-{name}: push once it is done."
    if sub:
        g = next((dict(s, repo=sub) for s in pins.subrepos(c["Id"]) if s["dir"] == sub), None)
        if g is None:
            return name, None, None, f"No repo {sub} in the workspace of dockbench-{name}."
        if not pins.fits(tid, sub):
            return name, None, None, f"The folder name {sub} is too long for a Telegram button: push it from VS Code."
    else:
        g = pins.git_info(c["Id"])
    if g is None:
        return name, None, None, "The workspace is not a git repo."
    if not g["upstream"]:
        return name, None, None, f"Branch {g['branch']} {'has lost its upstream branch' if g['gone'] else 'has no upstream'}: push it once by hand with git push -u origin {g['branch']}."
    if not g["ahead"]:
        return name, None, None, f"Nothing to push: branch {g['branch']} is not ahead of its upstream."
    return name, c["Id"], g, None


def pick(tid, arg):
    """/push [repo]: ask for the named repo (a subrepo's folder or the workspace repo's name), else for the only repo with commits to push, else offer the pin's buttons to choose from."""
    name = state.settings(tid).get("name")
    c = dockerapi.container(name) if name else None
    if not c or c["State"] != "running":
        return ask(tid)  # check() says why
    g, subs = pins.git_info(c["Id"]), pins.subrepos(c["Id"])
    if arg:
        return ask(tid, None if g and arg == g["repo"] and arg not in [s["dir"] for s in subs] else arg)
    buttons = (pins.pin_markup(tid, g, subs) or {}).get("inline_keyboard", [])
    if len(buttons) > 1:
        return telegram.send(tid, "Which repo do you want to push?", markup={"inline_keyboard": buttons})
    data = buttons[0][0]["callback_data"] if buttons else f"push:{tid}"
    ask(tid, *data.split(":", 2)[2:])  # the only repo with commits to push; none: the workspace repo's reason


def ask(tid, sub=None):
    """A Push tap or /push: say what would be pushed and ask for a confirmation tap."""
    name, _, g, why = check(tid, sub)
    if why:
        return telegram.send(tid, why)
    n = g["ahead"]
    telegram.send(tid, f"Push {n} commit{'' if n == 1 else 's'} of branch {g['branch']} of {g['repo']} (dockbench-{name}) to its upstream?", markup={"inline_keyboard": [[telegram.button("✔ Push", f"pushgo:{tid}:{g['oid'][:12]}" + (f":{sub}" if sub else "")), telegram.button("✖ Cancel", "pushno")]]})


def go(tid, mid, oid, sub=None):
    """The confirmation tap on message mid: check again (sessions may have changed since) and that HEAD is still the commit oid the question was about, then push in the workspace or its subrepo sub. No force, no shell; GIT_TERMINAL_PROMPT=0 and timeout so a missing credential fails instead of hanging."""
    name, cid, g, why = check(tid, sub)
    if not why and not g["oid"].startswith(oid):
        why = "The branch moved since this question, nothing pushed: tap Push or send /push again."
    if why:
        return telegram.edit(mid, why)
    with state.LOCK:
        if name in BUSY:
            return
        BUSY.add(name)
    try:
        telegram.edit(mid, f"pushing {g['ahead']} commit{'' if g['ahead'] == 1 else 's'} of {g['branch']}…")
        code, out = dockerapi.exec_run(cid, ["timeout", "120", "git"] + (["-C", f"{WORKSPACE}/{sub}"] if sub else []) + ["push"], timeout=150, env=EXEC_ENV + ["GIT_TERMINAL_PROMPT=0"])
        log.info("dockbench-%s: git push exit %s", name, code)  # not its output: it names the remote
        text = scrub(out.decode(errors="replace").strip())
        telegram.edit(mid, (f"✅ pushed {g['branch']} of {g['repo']}" if code == 0 else f"❌ git push failed (exit {code})") + (f"\n\n{text[-3000:]}" if text else ""))
    finally:
        with state.LOCK:
            BUSY.discard(name)
        pins.WAKE.set()


def spawn(target, tid, *args):
    def work():
        try:
            target(tid, *args)
        except Exception as e:
            log.exception("thread %s: push failed", tid)
            try:
                telegram.send(tid, f"push: bot error: {type(e).__name__}")  # not str(e): a git or Docker message could name the remote
            except Exception:
                pass
    threading.Thread(target=work, daemon=True).start()
