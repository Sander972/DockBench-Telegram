"""Guided setup (/setup, /start): the owner claim and the group pick, with the requirements checklist."""
import os
import secrets
import threading

from . import dockerapi, runs, state, telegram
from .config import SETUP_STEPS, log


def setup(m, groups):
    """Guided first configuration, also reached by /start. The owner claims the bot in a DM with the one-time code from the log, then sends /setup in the forum group, which picks that group and checks what the bot needs there. Moving to another group takes /setup move. groups: pending albums."""
    chat, uid = m["chat"], (m.get("from") or {}).get("id")
    if chat.get("type") == "private":
        if not state.OWNER_ID and state.SETUP_CODE and secrets.compare_digest(telegram.parse_command(m["text"])[1].encode(), state.SETUP_CODE.encode()):
            state.OWNER_ID, state.SETUP_CODE = uid, None
            with state.LOCK:
                state.STATE["owner_id"] = uid
                state.save()
            log.info("setup: owner set")
        elif uid != state.OWNER_ID:
            return  # a wrong code or a stranger: silently, like everything else
        telegram.tg("sendMessage", chat_id=chat["id"], text=f"The bot already works in a group: send /setup@{state.BOT_NAME} there to re-check it." if state.GROUP_ID else SETUP_STEPS.format(bot=state.BOT_NAME))
        return
    if not state.OWNER_ID or uid != state.OWNER_ID:
        return
    tid = m.get("message_thread_id") if m.get("is_topic_message") else None

    def reply(text):
        telegram.tg("sendMessage", chat_id=chat["id"], message_thread_id=tid, text=text)

    if int(os.environ.get("TELEGRAM_GROUP_ID") or 0) and chat["id"] != state.GROUP_ID:  # parsed like GROUP_ID, so "0" does not pin
        return reply("TELEGRAM_GROUP_ID pins the bot to another group.")
    forum = bool(chat.get("is_forum"))
    if forum and chat["id"] != state.GROUP_ID:  # only a forum: enabling Topics on a basic group gives it a new id
        if state.GROUP_ID and telegram.parse_command(m["text"])[1] != "move":
            return reply(f"The bot already works in another group. To move it here, send /setup@{state.BOT_NAME} move: the topic bindings and sessions of the other group are dropped.")
        if groups or runs.busy_anywhere():  # their answers would land in this group
            return reply("A prompt is running or queued in the other group: wait for it or /stop it there, then send /setup again.")
        if not state.AUTO.acquire(blocking=False):
            return reply("The bot is creating topics in the other group right now: send /setup again in a minute.")
        try:
            with state.LOCK:
                if state.GROUP_ID:  # thread and message ids of another group mean nothing here. A first group keeps them: they may be from TELEGRAM_GROUP_ID, since removed
                    state.STATE["threads"], state.STATE["created"], state.STATE["sessions"], state.STATE["replies"] = {}, [], {}, {}
                    state.STATE.pop("usage", None)  # the usage message is in the old group's General
                state.PENDING.clear()  # their buttons are in the old group, where taps are now dropped
                state.GROUP_ID = state.STATE["group_id"] = chat["id"]
                state.save()
        finally:
            state.AUTO.release()
        log.info("setup: group %s", state.GROUP_ID)
    with state.LOCK:
        state.RETRY.clear()  # retry topics that failed before, for example without the Manage topics right

    def check():  # own thread: Telegram retries and a slow Docker must not hold up getUpdates
        try:
            member = telegram.tg("getChatMember", chat_id=chat["id"], user_id=state.BOT_ID)
            try:
                found = sum(c["State"] == "running" for c in dockerapi.dev_containers())  # only running ones get a topic
            except Exception as e:
                found = str(e)
            # can_manage_topics exists only in supergroups: in a basic group every admin can do everything
            admin = member.get("status") == "administrator" and (bool(member.get("can_manage_topics")) or chat.get("type") != "supergroup")
            pin = member.get("status") == "administrator" and (bool(member.get("can_pin_messages")) or chat.get("type") != "supergroup")
            delete = member.get("status") == "administrator" and (bool(member.get("can_delete_messages")) or chat.get("type") != "supergroup")
            reply(checklist(state.BOT_NAME, forum, admin, pin, delete, found))
        except Exception as e:
            log.exception("setup: check failed")
            try:
                reply(f"Setup check failed: {e}\nSend /setup@{state.BOT_NAME} again in a moment.")
            except Exception:
                log.exception("setup: could not report the failed check")

    threading.Thread(target=check, daemon=True).start()


def checklist(bot, forum, admin, pin, delete, found):
    """/setup report: one line per requirement, with the fix for what is missing. found is the number of dev containers or the Docker error. No privacy mode row: an admin bot gets every group message anyway."""
    rows = [
        (forum, "Topics are on", "Topics are off: enable them in the group settings"),
        (admin, "admin with Manage topics", f"not an admin with Manage topics: group settings > Administrators > @{bot}, enable Manage topics"),
        (pin, "admin with Pin messages", f"not an admin with Pin messages, so the status of each topic is not pinned: group settings > Administrators > @{bot}, enable Pin messages"),
        (delete, "admin with Delete messages", f"not an admin with Delete messages, so /clean can only delete the bot's own messages: group settings > Administrators > @{bot}, enable Delete messages"),
        (isinstance(found, int), f"Docker reachable, {found} running dev containers", f"Docker not reachable: {found}"),
    ]
    end = "All set: running dev containers get a topic within ~30 s." if all(r[0] for r in rows) else f"Fix the ❌ items, then send /setup@{bot} here again."
    return "\n".join(["Setup check:"] + [f"✅ {ok}" if good else f"❌ {bad}" for good, ok, bad in rows] + [end])
