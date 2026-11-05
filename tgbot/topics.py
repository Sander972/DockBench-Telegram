"""Auto-created topics: a running dev container without a topic gets one."""
import time

from . import dockerapi, state, telegram
from .config import NAME_RE, log


def missing_topics(containers, bound):
    """PROJECT names of running dev containers that no topic is bound to."""
    names = {dockerapi.project(c) for c in containers if c["State"] == "running"}
    return sorted(n for n in names - bound if n and NAME_RE.match(n))


def autocreate(containers):
    """A dev container that comes up without a topic gets one named after its PROJECT, with a hi in it so the topic shows up and notifies. Caller holds AUTO."""
    now = time.monotonic()
    with state.LOCK:
        skip = set(state.STATE["created"]) | {n for n, t in state.RETRY.items() if t > now}
    for name in missing_topics(containers, skip):
        with state.LOCK:
            if name in state.STATE["created"]:  # bound by hand since the snapshot
                continue
        try:
            tid = telegram.tg("createForumTopic", idempotent=False, chat_id=state.GROUP_ID, name=name)["message_thread_id"]
        except telegram.TgUnclear as e:
            with state.LOCK:  # the topic may exist: never try again, so there is never a duplicate
                if name not in state.STATE["created"]:
                    state.STATE["created"].append(name)
                state.save()
            log.warning("creating a topic for dockbench-%s failed unclearly, not retrying: if no such topic exists, create it by hand: %s", name, e)
            continue
        except telegram.TgError as e:
            with state.LOCK:
                state.RETRY[name] = now + 600  # a missing "Manage topics" right must not turn into a create loop
            log.warning("could not create a topic for dockbench-%s, retrying in 10 min or on /setup: %s", name, e)
            continue
        state.bind(tid, name)  # also adds name to STATE["created"]
        try:
            telegram.send(tid, f"hi, dockbench-{name} is up. Post a prompt here to start a Claude Code session in it, reply to a session's messages to continue it.")
        except telegram.TgError as e:
            log.warning("thread %s: could not send the hi: %s", tid, e)


def autocreate_loop():
    """Own thread: slow Docker or Telegram calls here must not hold up getUpdates."""
    last = None
    while True:
        if state.GROUP_ID and state.OWNER_ID:  # no topics before someone can use them
            try:
                containers = dockerapi.dev_containers()  # outside AUTO: a slow Docker must not make /setup move say topics are being created
                with state.AUTO:
                    autocreate(containers)
                last = None
            except Exception as e:
                if str(e) != last:  # once per outage, not every 30 s
                    log.warning("topic auto-create failed: %s", e)
                last = str(e)
        time.sleep(30)  # ponytail: polls every ~30 s, Docker's /events stream would make it instant
