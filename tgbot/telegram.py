"""Bot API calls, and the messages, buttons and texts the bot sends."""
import html
import http.client
import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.request

from . import markdown, state
from .config import BADGES, EFFORTS, MODELS, PERMS, TITLE_MAX, log

TAG_ROOM = 2 * TITLE_MAX + 12  # UTF-16 units a session's tag may take (Telegram counts those, an emoji takes 2): its title or badge, then " · #<n>"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"


class TgError(Exception):
    pass


class TgUnclear(TgError):
    """A call with idempotent=False failed in a way that does not say whether Telegram carried it out."""


def multipart(params, files):
    """multipart/form-data body and its Content-Type for an upload: files maps a field to (filename, bytes); other values go as text, objects as JSON."""
    boundary = secrets.token_hex(16)
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v if isinstance(v, str) else json.dumps(v)}\r\n'.encode() for k, v in params.items()]
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n" for k, (name, data) in files.items()]
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def tg(method, idempotent=True, files=None, **params):
    """Bot API call. Rate limits, 5xx and network errors are retried for about 30 s: a lost send would drop a finished answer. idempotent=False: a 5xx or network error that may have reached Telegram raises TgUnclear at once instead (a DNS failure or refused connection is still retried), since a retry could do the call twice. files: uploads, see multipart()."""
    params = {k: v for k, v in params.items() if v is not None}
    body, ctype = multipart(params, files) if files else (json.dumps(params).encode(), "application/json")
    req = urllib.request.Request(f"{API}/{method}", body, {"Content-Type": ctype})
    for attempt in range(5):
        unsent = False
        try:
            with urllib.request.urlopen(req, timeout=params.get("timeout", 0) + (120 if files else 30)) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:  # before OSError, it is a subclass
            try:
                body = json.load(e)
            except ValueError:
                body = {"error_code": e.code, "description": f"HTTP {e.code}"}
        except (OSError, http.client.HTTPException, ValueError) as e:
            body = {"error_code": 0, "description": f"{type(e).__name__}: {e}"}
            unsent = isinstance(getattr(e, "reason", None), (socket.gaierror, ConnectionRefusedError))  # DNS or refused connection: the request never left, so a retry is safe
        if body.get("ok"):
            return body["result"]
        retry = (body.get("parameters") or {}).get("retry_after")
        if not retry and 400 <= (body.get("error_code") or 0) < 500:
            raise TgError(f"{method}: {body.get('description')}")
        if not retry and not idempotent and not unsent:
            raise TgUnclear(f"{method}: {body.get('description')}")
        time.sleep(retry or 2 ** attempt)
    raise TgError(f"{method}: {body.get('description')}")


def send(thread, text, reply=None, markup=None, md=False, tag=None):
    """reply: message id the first part quotes; markup: inline keyboard of the last part. md: text is Claude's markdown, sent as Telegram HTML, and as plain text when Telegram refuses the markup. Otherwise plain text. tag: function called once per part for its plain first line (a session's runs.label(), numbered per message). Returns the message ids."""
    room = TAG_ROOM + 1 if tag else 0
    parts = (markdown.md_chunks(text, 4000 - room) if md else markdown.chunks(text, 4096 - room)) or ["(empty)"]
    ids = []
    for i, part in enumerate(parts):
        head = f"{tag()}\n" if tag else ""
        params = {"chat_id": state.GROUP_ID, "message_thread_id": thread, "reply_parameters": {"message_id": reply, "allow_sending_without_reply": True} if reply and i == 0 else None, "reply_markup": markup if i == len(parts) - 1 else None, "disable_notification": True}  # silent: a buzz per status split, answer and image is too much
        if md:
            try:
                ids.append(tg("sendMessage", text=html.escape(head) + markdown.md_html(part), parse_mode="HTML", **params)["message_id"])
                continue
            except TgError as e:
                if "can't parse entities" not in str(e).lower():
                    raise
                log.warning("thread %s: Telegram refused the converted markup, sending plain text", thread)  # not the error: it can quote the text
        ids.append(tg("sendMessage", text=head + part, **params)["message_id"])
    return ids


def send_images(thread, images, reply=None, caption=None):
    """images: (filename, bytes). Photos go in albums of up to 10; a GIF, an image over Telegram's 10 MB photo limit, and an album Telegram refuses (an image too long or wide) go one by one as files. caption: on every photo, album and file (a session's badge()). Returns the message ids."""
    params = {"chat_id": state.GROUP_ID, "message_thread_id": thread, "reply_parameters": {"message_id": reply, "allow_sending_without_reply": True} if reply else None, "disable_notification": True}
    photos = [i for i in images if not i[0].endswith(".gif") and len(i[1]) <= 10 * 2**20]
    docs, ids = [i for i in images if i not in photos], []
    for n in range(0, len(photos), 10):
        album = photos[n:n + 10]
        try:
            if len(album) == 1:
                ids.append(tg("sendPhoto", files={"photo": album[0]}, caption=caption, **params)["message_id"])
            else:
                media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(album))]
                if caption:
                    media[0]["caption"] = caption  # the first item's caption is the album's
                ids += [m["message_id"] for m in tg("sendMediaGroup", media=media, files={f"p{i}": a for i, a in enumerate(album)}, **params)]
        except TgError as e:
            log.warning("thread %s: photos refused, sending them as files: %s", thread, e)
            docs += album
    for doc in docs:
        ids.append(tg("sendDocument", files={"document": doc}, caption=caption, **params)["message_id"])
    return ids


def edit(mid, text, markup=None, md=False, head=""):
    """No markup removes the buttons. md: text is one md_chunks() part of Claude's markdown, edited in as Telegram HTML under the plain head line, and as plain text when Telegram refuses the markup."""
    if md:
        try:
            return tg("editMessageText", chat_id=state.GROUP_ID, message_id=mid, text=html.escape(head) + "\n" + markdown.md_html(text), parse_mode="HTML", reply_markup=markup)
        except TgError as e:
            if "can't parse entities" not in str(e).lower():
                raise
            log.warning("message %s: Telegram refused the converted markup, editing in plain text", mid)
        text = f"{head}\n{text}"
    tg("editMessageText", chat_id=state.GROUP_ID, message_id=mid, text=markdown.chunks(text)[0] if text.strip() else "(empty)", reply_markup=markup)


def delete(mid):
    try:
        tg("deleteMessage", chat_id=state.GROUP_ID, message_id=mid)
    except Exception as e:
        log.warning("could not delete message %s: %s", mid, e)


def button(text, data):
    return {"text": text, "callback_data": data}


def picker(name, model, effort, last):
    """Text and inline keyboard asking a new session's model, then its effort, then its permission mode. last: the topic's previous (model, effort, mode), offered as a one-tap row."""
    if not model:
        rows = [[button(m, f"m:{m}") for m in MODELS]]
        if last:
            rows.insert(0, [button(f"▶ {' · '.join(last)} (as last time)", "last")])
        return f"New Claude session in dockbench-{name}. Model?", {"inline_keyboard": rows + [[button("✖ Cancel", "x")]]}
    if not effort:
        rows = [[button(e, f"e:{e}") for e in EFFORTS[:3]], [button(e, f"e:{e}") for e in EFFORTS[3:]], [button("‹ Model", "b"), button("✖ Cancel", "x")]]
        return f"New Claude session in dockbench-{name}, model {model}. Effort?", {"inline_keyboard": rows}
    rows = [[button(p, f"p:{p}") for p in PERMS[:3]], [button(p, f"p:{p}") for p in PERMS[3:]], [button("‹ Effort", "b"), button("✖ Cancel", "x")]]
    return f"New Claude session in dockbench-{name}, model {model}, effort {effort}. Permission mode?", {"inline_keyboard": rows}


def status_text(head, turns, limit=4096):
    """The status message: head, then Claude's turn messages so far as plain text, cut at the start to keep the latest within one message."""
    body = "\n\n".join(turns)
    if not body:
        return head
    room = limit - len(head) - 3
    return f"{head}\n\n{body}" if len(body) <= room else f"{head}\n\n…{body[-room:]}"


def overflow(head, turns, limit=4096):
    """Claude's turn text that no longer fits one status message under head: (parts that go to messages of their own, oldest first; the turns left for the status message). ([], turns) when it all fits."""
    body = "\n\n".join(turns)
    if len(head) + 2 + len(body) <= limit:
        return [], turns
    parts = markdown.md_chunks(body, limit - 96 - max(TAG_ROOM, len(head)) - 60)  # room for a part's tag, or the head and its growing time; md_chunks and its 96 of room (send()'s md limit is 4000) so a moved part goes as one markdown message that never ends inside a ``` block
    return parts[:-1], parts[-1:]


def badge(key):
    """A session's tag on its messages: colored shape plus its key, the first request's message id, e.g. '🟢 #4518'."""
    key = int(key)
    return f"{BADGES[key % 3][key % 7]} #{key}"


def stop_markup(key):
    return {"inline_keyboard": [[button("⏹ Stop", f"stop:{key}")]]}


def elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def msg_link(tid, mid):
    """Link to a message of the group, which only its members can open. None outside a supergroup."""
    g = str(state.GROUP_ID)
    return f"https://t.me/c/{g[4:]}/{tid}/{mid}" if g.startswith("-100") else None


def download(file_id):
    path = tg("getFile", file_id=file_id)["file_path"]
    with urllib.request.urlopen(f"{FILE_API}/{path}", timeout=120) as r:
        return os.path.splitext(path)[1] or ".bin", r.read()


def attachments(msgs):
    """(file_id, is_image) per photo or document. Photos come in several sizes: take the largest."""
    for m in msgs:
        if m.get("photo"):
            yield max(m["photo"], key=lambda p: p["width"] * p["height"])["file_id"], True
        elif m.get("document"):
            yield m["document"]["file_id"], (m["document"].get("mime_type") or "").startswith("image/")


def parse_command(text):
    """'/model@MyBot opus' -> ('model', 'opus')."""
    head, *rest = text.split(None, 1)
    return head[1:].split("@")[0].lower(), rest[0].strip() if rest else ""
