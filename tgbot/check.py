"""`python bot.py --check`: the self-check, the only test suite. It stubs Telegram, Docker and the state file on their modules and never restores them, so it runs in its own process."""
import email
import io
import json
import os
import re
import tarfile
import threading
import time
import types
import urllib.error
import urllib.request

from . import claude, commands, dockerapi, markdown, onboarding, pins, push, routing, runs, state, status, telegram, topics, usage
from .config import BOT_MENU, DEFAULT_PROMPT, EFFORTS, IDLE_LIMIT, INBOX, MAX_SESSIONS, MODELS, PERMS, RUN_MARK, RUN_TIMEOUT, WORKSPACE


def self_check():
    parts = markdown.chunks(("para " * 300 + "\n\n") * 10 + "x" * 9000)
    assert all(len(p) <= 4096 for p in parts), [len(p) for p in parts]
    assert "".join(parts).replace("\n", "").replace(" ", "") == (("para" * 300) * 10 + "x" * 9000), "content lost"
    assert parts[0].endswith("para") and not parts[1].startswith("\n"), "not split on a paragraph"
    assert [len(p) for p in markdown.chunks("\U0001F600" * 5000)] == [4096, 904]
    assert markdown.chunks("  \n\n ") == [] and markdown.chunks("hi") == ["hi"]

    assert telegram.parse_command("/model@MyBot  opus ") == ("model", "opus")
    assert telegram.parse_command("/NEW") == ("new", "")
    assert telegram.parse_command("/bind myapp extra") == ("bind", "myapp extra")
    assert telegram.parse_command("/new\nsomething") == ("new", "something") and telegram.parse_command("/") == ("", "")

    raw = b"".join(bytes([k, 0, 0, 0]) + len(d).to_bytes(4, "big") + d for k, d in [(1, b"ab"), (2, b"err"), (1, b"")])
    assert list(dockerapi.frames(io.BytesIO(raw))) == [(1, b"ab"), (2, b"err"), (1, b"")]

    with tarfile.open(fileobj=io.BytesIO(dockerapi.tarball([("1-0.jpg", b"img")]))) as t:
        info = t.getmember("1-0.jpg")
        assert (info.uid, info.gid, info.size) == (1000, 1000, 3) and t.extractfile(info).read() == b"img"

    # the outbox tar from the container: regular files only, named inside the folder; over the limit raises
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, kind, data in [("7", tarfile.DIRTYPE, b""), ("7/a.png", tarfile.REGTYPE, b"png"), ("7/sub/b.jpg", tarfile.REGTYPE, b"jpg"), ("7/creds.png", tarfile.SYMTYPE, b""), ("7/hard.png", tarfile.LNKTYPE, b"")]:
            info = tarfile.TarInfo(name)
            info.type, info.size, info.linkname = kind, len(data), "/home/dev/.claude/.credentials.json" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            t.addfile(info, io.BytesIO(data))
    real_docker = dockerapi.docker
    dockerapi.docker = lambda method, path, body=None, timeout=60, stream=False: io.BytesIO(buf.getvalue())
    assert dockerapi.get_files("cid", "/o/7", 10**6) == [("a.png", b"png"), ("sub/b.jpg", b"jpg")]
    try:
        dockerapi.get_files("cid", "/o/7", 100)
        raise AssertionError("an outbox over the limit was read")
    except dockerapi.DockerError:
        pass
    dockerapi.docker = real_docker

    # uploads: a multipart body the stdlib email parser reads back, text and JSON fields plus the file
    body, ctype = telegram.multipart({"chat_id": -100, "media": [{"type": "photo"}], "caption": "hi"}, {"photo": ("1.png", b"\x89PNG\r\n--x")})
    parts = email.message_from_bytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body).get_payload()
    assert [(p.get_param("name", header="content-disposition"), p.get_filename(), p.get_payload(decode=True)) for p in parts] == [("chat_id", None, b"-100"), ("media", None, b'[{"type": "photo"}]'), ("caption", None, b"hi"), ("photo", "1.png", b"\x89PNG\r\n--x")], parts

    turn = {"type": "assistant", "session_id": "s1", "message": {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": " Checking the test. \n"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}, {"type": "text", "text": "  "}]}}
    assert claude.turn_text(turn) == "Checking the test." and claude.turn_text({"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}) == "" and claude.turn_text({"type": "user", "message": {"content": [{"type": "text", "text": "x"}]}}) == ""
    lines = [{"type": "system", "subtype": "init", "session_id": "s1"}, {"type": "assistant", "session_id": "s1"}, turn, {"type": "result", "result": "done", "session_id": "s1", "num_turns": 2, "total_cost_usd": 0.5}]
    blob = b"\n".join(json.dumps(x).encode() for x in lines) + b"\nnot json\n"
    seen, said = [], []
    result, err, init = claude.parse_stream([(1, blob[:7]), (2, b"warn"), (1, blob[7:40]), (1, blob[40:])], seen.append, said.append)
    assert result["result"] == "done" and seen == ["s1"] and "warn" in err and "not json" in err and init["subtype"] == "init", (result, seen, err)
    assert said == ["Checking the test."], said
    assert telegram.status_text("working", []) == "working" and telegram.status_text("working", ["a", "b"]) == "working\n\na\n\nb"
    full = telegram.status_text("working", ["old " * 1100, "latest"])
    assert len(full) == 4096 and full.startswith("working\n\n…") and full.endswith("\n\nlatest"), len(full)

    assert claude.summarize(result, 0, "") == "done", "no turn count or cost"
    assert claude.tool_notes({"permissionMode": "auto", "tools": ["Bash", "Edit"]}) == [] and claude.tool_notes(None) == []
    nobash = claude.summarize(result, 0, "", {"permissionMode": "auto", "tools": ["Edit"]})
    assert "no Bash tool" in nobash and "settings.json" in nobash, nobash
    assert "permission mode default" in claude.summarize(result, 0, "", {"permissionMode": "default", "tools": ["Bash"]})
    assert claude.tool_notes({"permissionMode": "default", "tools": ["Bash"]}, "manual") == [] and claude.tool_notes({"permissionMode": "plan", "tools": ["Bash"]}, "plan") == []
    assert "Auto mode" not in claude.summarize(result, 0, "", {"permissionMode": "default", "tools": ["Bash"]}, "acceptEdits")
    auth = claude.summarize({"is_error": True, "subtype": "success", "result": "Not logged in · Please run /login"}, 1, "")
    assert auth.startswith("error (success): Not logged in") and "VS Code" in auth
    assert "VS Code" not in claude.summarize({"result": "fixed the OAuth login bug"}, 0, "")
    assert claude.summarize({"is_error": True, "subtype": "error_max_turns", "errors": []}, 1, "").startswith("error (error_max_turns): error_max_turns")
    assert claude.summarize(None, 1, "boom").startswith("claude exited with code 1")
    assert "not installed" in claude.summarize(None, 127, "timeout: failed to run command 'claude': No such file or directory")
    assert "not installed" not in claude.summarize(None, 1, "boom")
    assert claude.summarize({"result": "x", "permission_denials": [{"tool_name": "Write"}, {"tool_name": "Bash"}, {"tool_name": "Bash"}]}, 0, "") == "x\n\n⚠️ Denied without asking: Bash, Write"

    # Claude's markdown as Telegram HTML
    md = "## Done\n\n- **Star on each row:** tap it, see `Icon.vue` & <x>\n* a [link](https://x.y/a?b=1&c=2)\n\nsnake_case_name, 2*3*4, _it_ and *it* ~~old~~\n> note\n---\n```ts\nconst a = b < c && **d**\n```\n| a | b |\n|---|---|\n| 1 | 2 |"
    out = markdown.md_html(md)
    assert out.split("\n")[:3] == ["<b>Done</b>", "", "• <b>Star on each row:</b> tap it, see <code>Icon.vue</code> &amp; &lt;x&gt;"], out
    assert '• a <a href="https://x.y/a?b=1&amp;c=2">link</a>' in out and "snake_case_name, 2*3*4, <i>it</i> and <i>it</i> <s>old</s>" in out, out
    assert "▎ note" in out and "──────────" in out, out
    assert '<pre><code class="language-ts">const a = b &lt; c &amp;&amp; **d**</code></pre>' in out and "<pre><code>| a | b |\n| 1 | 2 |</code></pre>" in out, out
    assert markdown.md_html("`a*b*c`") == "<code>a*b*c</code>" and markdown.md_html("```\nopen") == "<pre><code>open</code></pre>"
    long = "intro\n```py\n" + "x = 1\n" * 1200 + "```\nafter"
    parts = markdown.md_chunks(long)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts) and parts[1].startswith("```py\n") and parts[0].endswith("\n```"), [p[:20] for p in parts]
    assert all(markdown.md_html(p).count("<pre>") == markdown.md_html(p).count("</pre>") for p in parts)

    assert claude.prompt_for("", [("1-0.jpg", True)]) == f"{DEFAULT_PROMPT}\nScreenshot: {INBOX}/1-0.jpg"
    assert claude.prompt_for("why?", [("1-1.log", False)]) == f"why?\nFile: {INBOX}/1-1.log"

    cs = [{"Names": ["/dockbench-a"], "State": "running"}, {"Names": ["/dockbench-b"], "State": "running"}, {"Names": ["/dockbench-c"], "State": "exited"}, {"Names": ["/other"], "State": "running"}, {"Names": ["/dockbench-x y"], "State": "running"}]
    assert topics.missing_topics(cs, {"b", None}) == ["a"]
    assert dockerapi.project({"Names": ["/dockbench-a"]}) == "a" and dockerapi.project({"Names": ["/other"]}) is None and dockerapi.project({}) is None

    good = onboarding.checklist("b", True, True, True, True, 2)
    assert good.count("✅") == 5 and good.endswith("within ~30 s."), good
    bad = onboarding.checklist("b", False, True, False, False, "connect {x}")
    assert bad.count("\n❌") == 4 and "Pin messages" in bad and "Delete messages" in bad and "Topics are off" in bad and "connect {x}" in bad and bad.endswith("/setup@b here again."), bad

    # pinned status: git parsing, the origin name without its credential, the text
    assert pins.parse_status("# branch.oid 0123456789ab\n# branch.head main\n1 .M N... 100644 100644 100644 a b f.py\n? new.txt\n? tmp/\n") == {"branch": "main", "oid": "0123456789ab", "untracked": 2, "upstream": False, "gone": False, "ahead": 0}
    assert pins.parse_status("# branch.oid 0123456789ab\n# branch.head main\n# branch.upstream origin/main\n# branch.ab +3 -1\n") == {"branch": "main", "oid": "0123456789ab", "untracked": 0, "upstream": True, "gone": False, "ahead": 3}
    gone = pins.parse_status("# branch.oid 0123456789ab\n# branch.head feat\n# branch.upstream origin/feat\n")  # git leaves out branch.ab when the upstream ref is gone
    assert (gone["upstream"], gone["gone"], gone["ahead"]) == (False, True, 0), gone
    assert pins.parse_status("warning: x\n# branch.oid 0123456789ab\n# branch.head (detached)\n")["branch"] == "detached 0123456"
    assert pins.parse_numstat("3\t1\ta.py\n-\t-\timg.png\n10\t0\tb.py\nwarning: CRLF\n") == {"add": 13, "del": 1, "files": 3}
    assert pins.repo_name("https://user:tok@github.com/Sander972/DockBench-telegram.git\n") == "DockBench-telegram" and pins.repo_name("git@github.com:a/b") == "b" and pins.repo_name("/srv/repo/") == "repo"
    g = {"repo": "r", "branch": "main", "add": 0, "del": 0, "files": 0, "untracked": 0, "upstream": True, "gone": False, "ahead": 0}
    assert pins.pin_text("a", "running", dict(g, upstream=False, gone=True), []).split("\n")[1] == "🌿 main · upstream gone"
    assert pins.pin_text("a", "running", g, []) == "📦 r (a)\n🌿 main\n✏️ clean\nsessions: none" and pins.pin_text("r", "running", g, []).startswith("📦 r\n") and pins.pin_markup(7, g) is None
    assert pins.pin_text("a", "running", dict(g, ahead=2), []).split("\n")[1] == "🌿 main · ↑2 to push"
    assert pins.pin_text("a", "running", dict(g, upstream=False), []).split("\n")[1] == "🌿 main · no upstream" and pins.pin_text("a", "running", dict(g, upstream=False, branch="detached 0123456"), []).split("\n")[1] == "🌿 detached 0123456"
    assert pins.pin_markup(7, dict(g, ahead=1)) == {"inline_keyboard": [[{"text": "⬆ r (1)", "callback_data": "push:7"}]]} and pins.pin_markup(7, dict(g, ahead=2, upstream=False)) is None and pins.pin_markup(7, None) is None
    # subrepos: folders one level inside the workspace holding a .git, a line and a Push button each when there is something to push
    assert pins.parse_subrepos("/home/dev/workspace/B/.git\n/home/dev/workspace/A/.git\nfind: '/home/dev/workspace/x': Permission denied\n/home/dev/workspace/.git\n/home/dev/workspace/a/b/.git\n/elsewhere/C/.git\n") == ["A", "B"]
    subs = [dict(g, dir="A", ahead=1), dict(g, dir="B"), dict(g, dir="C", branch="feat", upstream=False), dict(g, dir="D", branch="detached 0123456", upstream=False), dict(g, dir="E" * 60, ahead=3)]
    assert pins.pin_text("a", "running", g, [], subs=subs).split("\n")[3:6] == ["📂 A · main · ↑1 to push", "📂 C · feat · no upstream", f"📂 {'E' * 60} · main · ↑3 to push"] and "📂" not in pins.pin_text("a", "exited", None, [], subs=subs)
    assert pins.pin_markup(7, dict(g, ahead=2), subs) == {"inline_keyboard": [[{"text": "⬆ r (2)", "callback_data": "push:7"}], [{"text": "⬆ A (1)", "callback_data": "push:7:A"}]]}, "no button for a folder name too long for callback_data"
    real_exec = dockerapi.exec_run
    execs = []
    dockerapi.exec_run = lambda cid, cmd, timeout=60, env=None: execs.append(cmd) or ((1, b"/home/dev/workspace/A/.git\n/home/dev/workspace/B/.git\nfind: denied\n") if cmd[0] == "find" else (0, b"# branch.oid 0123456789ab\n# branch.head main\n# branch.upstream origin/main\n# branch.ab +2 -0\n") if "/home/dev/workspace/A" in cmd else (128, b"fatal: not a git repository"))
    try:
        assert pins.subrepos("cid") == [{"branch": "main", "oid": "0123456789ab", "untracked": 0, "upstream": True, "gone": False, "ahead": 2, "dir": "A"}] and execs[1] == ["git", "-C", "/home/dev/workspace/A", "status", "--porcelain=v2", "--branch", "--untracked-files=no"], execs
    finally:
        dockerapi.exec_run = real_exec
    assert push.scrub("To https://x:tok@github.com/a/b.git\n   1..2  main -> main\nTo git@github.com:a/b.git\nfatal: 'ssh://h/r' denied") == "To <remote>\n   1..2  main -> main\nTo <remote>\nfatal: <remote> denied"
    busy = pins.pin_text("a", "running", dict(g, add=42, files=1, untracked=2), [("🟢 #1", "opus", "high", "auto", 4, 1, "https://t.me/c/1/7/100", None), ("🟥 #2", "haiku", "low", "plan", None, 0, None, None), ("💙 #3", "sonnet", "max", "bypassPermissions", 0, 0, None, None)])
    assert busy.split("\n")[2:] == ["✏️ +42 −0 in 1 file, 2 untracked", "sessions:", "🟢 #1 👨‍💻 4m · opus · high · auto · 1 queued · <a href=\"https://t.me/c/1/7/100\">last message</a>", "🟥 #2 👀 queued · haiku · low · plan", "💙 #3 👨‍💻 &lt;1m · sonnet · max · bypassPermissions"], busy
    rest = pins.pin_text("a", "exited", None, [], [("🟢 #1", "opus", "high", "auto", 1800, "https://t.me/c/1/7/90", None), ("🟥 #2", "haiku", "low", "plan", 3 * 3600, None, None), ("💙 #3", "sonnet", "max", "auto", 3 * 86400, None, None)])
    assert rest.split("\n")[2:] == ["sessions:", "🟢 #1 💤 &lt;1h · opus · high · auto · <a href=\"https://t.me/c/1/7/90\">last message</a>", "🟥 #2 💤 3h · haiku · low · plan", "💙 #3 💤 3d · sonnet · max · auto"], rest
    # a session's title names its link, escaped; without a link it is shown alone
    titled = pins.pin_text("a", "exited", None, [("🟢 #1", "opus", "high", "auto", 4, 0, "https://t.me/c/1/7/100", "Fix <b> & login")], [("🟥 #2", "haiku", "low", "plan", 60, None, "Bot timeout")]).split("\n")[3:]
    assert titled == ["🟢 #1 👨‍💻 4m · opus · high · auto · <a href=\"https://t.me/c/1/7/100\">Fix &lt;b&gt; &amp; login</a>", "🟥 #2 💤 &lt;1h · haiku · low · plan · Bot timeout"], titled
    assert pins.pin_text("a", "running", dict(g, branch="fix/<b>&c"), []).split("\n")[1] == "🌿 fix/&lt;b&gt;&amp;c", "plain parts are escaped, the pin is HTML"
    many = pins.pin_text("a", "running", g, [("🟢 #1", "opus", "high", "auto", 4, 0, None, None)], [("🟥 #2", "opus", "high", "auto", 7200, None, None)] * (pins.PIN_IDLE + 2)).split("\n")
    assert many[4].startswith("🟢 #1 👨‍💻 4m") and many.count("🟥 #2 💤 2h · opus · high · auto") == pins.PIN_IDLE and many[-1] == "💤 +2 older, /clean deletes the idle ones", many
    assert pins.pin_text("a", "exited", None, []) == "📦 a\ncontainer exited\nsessions: none" and pins.pin_text("a", "running", None, []).startswith("📦 a\nworkspace: not a git repo\n")

    pick = {"model": "opus", "effort": "high"}
    cmd = claude.claude_cmd(pick, "-x", "abc", True, "/o/7")
    assert cmd[-4:] == ["--resume", "abc", "--", "-x"] and cmd[:5] == ["timeout", "-k", "30", str(RUN_TIMEOUT), "claude"]
    assert cmd[cmd.index("--model") + 1] == "opus" and cmd[cmd.index("--effort") + 1] == "high" and cmd[cmd.index("--permission-mode") + 1] == "auto", cmd  # a session from before the mode step
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"] == [INBOX, "/o/7"] and "/o/7:" in cmd[cmd.index("--append-system-prompt") + 1], cmd
    plan = claude.claude_cmd(dict(pick, perm="plan"), "p", "abc", False, "/o/7")
    assert plan[plan.index("--permission-mode") + 1] == "plan" and re.match(RUN_MARK, " ".join(plan)), plan
    assert claude.claude_cmd(pick, "p", "abc", False, "/o/7")[-4:-2] == ["--session-id", "abc"]
    line = " ".join(cmd)  # what pkill -f matches against
    assert re.match(f"{RUN_MARK}.* abc -- ", line) and not re.match(RUN_MARK, "claude --output-format stream-json --verbose --input-format stream-json --resume=abc")

    # the picker: every button fits Telegram's 64-byte callback_data, the model step offers the last pick, the effort step every level, the mode step every mode
    text, kb = telegram.picker("a", None, None, ("opus", "high", "plan"))
    assert "Model?" in text and kb["inline_keyboard"][0][0]["callback_data"] == "last" and "opus · high · plan" in kb["inline_keyboard"][0][0]["text"] and [b["callback_data"] for b in kb["inline_keyboard"][1]] == [f"m:{m}" for m in MODELS], kb
    text, kb = telegram.picker("a", "opus", None, None)
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "Effort?" in text and [d for d in datas if d.startswith("e:")] == [f"e:{e}" for e in EFFORTS] and "b" in datas and "x" in datas, datas
    text, kb = telegram.picker("a", "opus", "high", None)
    datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "Permission mode?" in text and [d for d in datas if d.startswith("p:")] == [f"p:{p}" for p in PERMS] and "b" in datas and "x" in datas, datas
    assert all(len(b["callback_data"].encode()) <= 64 for row in telegram.picker("a" * 50, None, None, ("x" * 30, "max", "bypassPermissions"))[1]["inline_keyboard"] + telegram.picker("a", "x", "max", None)[1]["inline_keyboard"] + telegram.stop_markup(2 ** 53)["inline_keyboard"] for b in row)
    assert telegram.elapsed(42) == "42s" and telegram.elapsed(185) == "3m 05s"

    # a non-idempotent call that times out is not retried
    calls, real_urlopen = [], urllib.request.urlopen

    def lost(*a, **k):
        calls.append(a)
        raise TimeoutError("timed out")
    urllib.request.urlopen = lost
    try:
        telegram.tg("createForumTopic", idempotent=False, chat_id=1, name="a")
        assert False, "no TgUnclear"
    except telegram.TgUnclear:
        assert len(calls) == 1, calls
    # but a refused connection never reached Telegram, so it is retried
    calls.clear()

    def refused_once(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise urllib.error.URLError(ConnectionRefusedError())
        return io.BytesIO(b'{"ok": true, "result": 7}')
    urllib.request.urlopen = refused_once
    try:
        assert telegram.tg("createForumTopic", idempotent=False, chat_id=1, name="a") == 7 and len(calls) == 2, calls
    finally:
        urllib.request.urlopen = real_urlopen

    # setup with Telegram, Docker and the state file stubbed: owner claim, then the group pick and move
    os.environ.pop("TELEGRAM_GROUP_ID", None)
    sent, state.OWNER_ID, state.GROUP_ID, state.SETUP_CODE = [], 0, 0, "c0de"
    telegram.tg, state.save, dockerapi.dev_containers = (lambda method, **p: sent.append(p.get("text") or "") or {}), (lambda: None), (lambda: [])
    dm = lambda uid, text: {"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": text}
    onboarding.setup(dm(5, "/setup wrong"), {})
    assert state.OWNER_ID == 0 and sent == [], "wrong code accepted or answered"
    onboarding.setup(dm(5, "/setup c0de"), {})
    onboarding.setup(dm(6, "/setup c0de"), {})
    assert state.OWNER_ID == 5 and state.SETUP_CODE is None and len(sent) == 1, (state.OWNER_ID, sent)
    grp = lambda gid, text, uid=5: {"chat": {"id": gid, "type": "supergroup", "is_forum": True}, "from": {"id": uid}, "text": text}
    state.STATE["threads"], state.STATE["created"] = {"7": {"name": "a"}}, ["a"]
    onboarding.setup(grp(-1, "/setup", uid=6), {})
    assert state.GROUP_ID == 0, "a stranger picked the group"
    onboarding.setup(grp(-1, "/setup"), {})
    assert state.GROUP_ID == -1 and state.STATE["created"] == ["a"], "a first group must keep its bindings"
    onboarding.setup(grp(-2, "/setup"), {})
    onboarding.setup(grp(-2, "/setup move"), {"album": {}})
    assert state.GROUP_ID == -1 and state.STATE["created"] == ["a"], "moved without move or with an album pending"
    onboarding.setup(grp(-2, "/setup move"), {})
    assert state.GROUP_ID == -2 and state.STATE["threads"] == {} and state.STATE["created"] == [], "move must reset the bindings"

    # /start is /setup, so the t.me/<bot>?start=<code> link claims the bot
    state.OWNER_ID, state.SETUP_CODE, state.BOT_NAME = 0, "c0de", "b"
    routing.handle({"update_id": 1, "message": dm(9, "/start c0de")}, {})
    assert state.OWNER_ID == 9 and state.SETUP_CODE is None, "/start <code> did not claim"

    # /status: names and counts only, never ids or the setup code, since every dev container on devnet can read it
    state.OWNER_ID, state.GROUP_ID, state.SETUP_CODE = 123456789, -1009876543210, "feedc0de"
    state.STATE["threads"] = {"7": {"name": "a"}, "8": {}}
    state.ACTIVE.clear()
    s = status.status_payload()
    assert set(s) == {"bot", "owner", "group", "topics", "last_poll_at", "uptime_seconds"}, s
    assert s["owner"] is True and s["group"] is True and s["topics"] == [{"name": "a", "running": False, "runs": 0, "queued": 0}], s
    dump = json.dumps(s)
    assert "123456789" not in dump and "9876543210" not in dump and "feedc0de" not in dump, dump

    # a message asks model, effort and mode, the last tap starts a session; back from the mode goes to the effort; a reply to it continues that session; a stranger's tap is dropped
    calls, jobs, mids, done = [], [], iter(range(1000, 2000)), threading.Semaphore(0)
    telegram.tg = lambda method, **p: calls.append((method, p)) or ({"message_id": next(mids)} if method == "sendMessage" else True)
    runs.run = lambda r, uid, msgs, status: (jobs.append((r.key, uid, [m["message_id"] for m in msgs], status)), done.release())
    state.STATE["sessions"], state.STATE["replies"] = {}, {}
    state.PENDING.clear()
    topic_msg = lambda mid, text, reply=None: {"message_id": mid, "message_thread_id": 7, "is_topic_message": True, "chat": {"id": state.GROUP_ID}, "from": {"id": state.OWNER_ID}, "text": text, "reply_to_message": {"message_id": reply} if reply else None}
    tap = lambda data, mid, uid=state.OWNER_ID: routing.handle({"update_id": 9, "callback_query": {"id": "q", "data": data, "from": {"id": uid}, "message": {"message_id": mid, "chat": {"id": state.GROUP_ID}}}}, {})
    routing.handle({"update_id": 2, "message": topic_msg(100, "fix it")}, {})
    pid = next(iter(state.PENDING))
    assert calls[-1][0] == "sendMessage" and calls[-1][1]["reply_parameters"]["message_id"] == 100 and "Model?" in calls[-1][1]["text"], calls[-1]
    calls.clear()
    tap("m:opus", pid, uid=6)
    assert calls == [] and state.PENDING[pid]["model"] is None, "a stranger's tap was handled or answered"
    tap("m:opus", pid)
    assert state.PENDING[pid]["model"] == "opus" and calls[0][0] == "editMessageText" and calls[-1][0] == "answerCallbackQuery", calls
    tap("e:low", pid)
    tap("b", pid)
    assert state.PENDING[pid]["model"] == "opus" and state.PENDING[pid]["effort"] is None and "Effort?" in calls[-2][1]["text"], calls[-2]
    tap("e:high", pid)
    assert jobs == [] and "Permission mode?" in calls[-2][1]["text"], calls[-2]
    tap("p:plan", pid)
    tap("p:plan", pid)  # a double tap starts nothing more
    assert done.acquire(timeout=5) and not done.acquire(timeout=0.2), jobs
    s100 = state.STATE["sessions"]["100"]
    assert jobs == [(100, 2, [100], pid)] and (s100["model"], s100["effort"], s100["perm"]) == ("opus", "high", "plan"), (jobs, s100)
    assert state.last_pick(7) == ("opus", "high", "plan") and state.PENDING == {}
    routing.handle({"update_id": 3, "message": topic_msg(200, "and the tests", reply=pid)}, {})
    assert done.acquire(timeout=5) and jobs[-1] == (100, 3, [200], None) and state.STATE["replies"]["200"] == 100, jobs
    routing.handle({"update_id": 4, "message": topic_msg(300, "something else", reply=55)}, {})
    assert len(jobs) == 2 and len(state.PENDING) == 1, "a reply to an unknown message must start a new session"
    tap("x", next(iter(state.PENDING)))
    assert state.PENDING == {} and calls[-2][0] == "deleteMessage", calls[-2:]
    assert runs.stop(100) == "nothing running"
    # a prompt starting with a path is not a command for another bot; /cmd@other is
    jobs.clear()
    routing.handle({"update_id": 5, "message": topic_msg(400, "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js is broken")}, {})
    assert len(state.PENDING) == 1, "a prompt starting with /path@scope was dropped"
    tap("x", next(iter(state.PENDING)))
    n_calls = len(calls)
    routing.handle({"update_id": 6, "message": topic_msg(401, "/status@otherbot")}, {})
    assert state.PENDING == {} and len(calls) == n_calls, "another bot's command was handled"
    # a run that raises is reported, and the bot puts no reaction on requests; a session id that is not a UUID is not kept
    def failing_run(r, uid, msgs, status):
        done.release()
        raise RuntimeError("docker down")
    runs.run = failing_run  # the "run failed" traceback in the output is this test, not a failure
    calls.clear()
    routing.handle({"update_id": 7, "message": topic_msg(500, "again", reply=pid)}, {})
    assert done.acquire(timeout=5)
    for _ in range(50):
        if any(c[0] == "sendMessage" and "bot error: docker down" in c[1]["text"] for c in calls):
            break
        time.sleep(0.1)
    assert any(c[0] == "sendMessage" and "bot error: docker down" in c[1]["text"] for c in calls) and not any(c[0] == "setMessageReaction" for c in calls), calls
    block = threading.Event()
    runs.run = lambda r, uid, msgs, status: block.wait()
    routing.handle({"update_id": 8, "message": topic_msg(600, "one", reply=pid)}, {})
    routing.handle({"update_id": 9, "message": topic_msg(601, "two", reply=pid)}, {})
    calls.clear()
    assert runs.stop(100) == "stopping, dropped 1 queued" and calls == [], calls
    block.set()
    for _ in range(50):
        if 100 not in state.ACTIVE:
            break
        time.sleep(0.1)
    assert 100 not in state.ACTIVE, "the worker did not finish"
    assert not runs.SID_RE.fullmatch(".*") and runs.SID_RE.fullmatch("1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    # a damaged state file is set aside instead of crashing every boot
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        state.STATE_FILE = f"{d}/state.json"
        with open(state.STATE_FILE, "w") as f:
            f.write("{broken")
        state.load()
        assert os.path.exists(state.STATE_FILE + ".bad") and not os.path.exists(state.STATE_FILE)

    # pruning forgets the oldest idle sessions and the replies that led to them
    state.STATE["sessions"] = {str(k): {"tid": 7, "at": k} for k in range(1, MAX_SESSIONS + 3)}
    state.STATE["replies"] = {"901": 1, "902": 2, "903": 3}
    with state.LOCK:
        state.prune()
    assert len(state.STATE["sessions"]) == MAX_SESSIONS and "1" not in state.STATE["sessions"] and "3" in state.STATE["sessions"] and state.STATE["replies"] == {"903": 3}, state.STATE["replies"]
    # the pin's rows from one snapshot: a session in ACTIVE only as at work, idle ones newest last prompt first, one still `working` after a restart (/clean skips it) in neither
    state.STATE["sessions"].update({"5000": {"tid": 7, "at": 10 ** 12, "model": "opus", "effort": "high"}, "5001": {"tid": 8, "at": 10 ** 12}, "5002": {"tid": 7, "at": 10 ** 12, "working": 99}})
    state.ACTIVE[4] = types.SimpleNamespace(key=4, tid=7, busy=True, status=1, started=time.monotonic(), q=types.SimpleNamespace(unfinished_tasks=1))
    state.STATE["replies"].update({"5000": 5000, "5009": 5000, "5004": 5000})  # the row links the session's last message
    group, state.GROUP_ID = state.GROUP_ID, -1001234
    work, rows = pins.rows(7)
    state.GROUP_ID = group
    del state.ACTIVE[4]
    assert [w[4:6] for w in work] == [(0, 0)] and work[0][6].endswith("/7/4"), work
    assert rows[0][5] == "https://t.me/c/1234/7/5009", rows[0]
    assert len(rows) == MAX_SESSIONS and rows[0][:5] == ("💛 #5000", "opus", "high", "auto", 0) and rows[1][4] < rows[2][4] and all(r[4] > 0 for r in rows[1:]), rows[:3]
    del state.STATE["sessions"]["5000"], state.STATE["sessions"]["5001"], state.STATE["sessions"]["5002"]

    # titles(): a running session's title is read on every pass until it shows up, a listed idle one's once (again after it ran); only sessions that exist in the container
    saved, read = (state.STATE["sessions"], claude.title, pins.TITLES, pins.UNTITLED), []
    state.STATE["sessions"] = {"1": {"tid": 7, "at": 1, "sid": "s1", "used": True}, "2": {"tid": 7, "at": 2, "sid": "s2", "used": True}, "3": {"tid": 7, "at": 3, "sid": "s3"}, "4": {"tid": 8, "at": 4, "sid": "s4", "used": True}}
    state.ACTIVE[1] = types.SimpleNamespace(key=1, tid=7, busy=True)
    claude.title, pins.TITLES, pins.UNTITLED = (lambda cid, sid: read.append(sid) or ""), {}, set()
    try:
        pins.titles(7, "cid")
        pins.titles(7, "cid")
        assert sorted(read) == ["s1", "s1", "s2"] and pins.UNTITLED == {2}, read
        del state.ACTIVE[1]
        claude.title = lambda cid, sid: read.append(sid) or ("Fix login" if sid == "s1" else "")
        pins.titles(7, "cid")
        pins.titles(7, "cid")
        assert sorted(read) == ["s1", "s1", "s1", "s2"] and pins.TITLES == {1: "Fix login"}, read
    finally:
        state.STATE["sessions"], claude.title, pins.TITLES, pins.UNTITLED = saved
        state.ACTIVE.pop(1, None)
    # claude.TITLE reads the last ai-title line of the session's transcript, nothing for a session without one
    import subprocess
    import sys
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(f"{d}/.claude/projects/-w")
        with open(f"{d}/.claude/projects/-w/s1.jsonl", "w") as f:
            f.write('{"type":"user"}\nnot json\n{"type":"ai-title","aiTitle":"Old"}\n{"type":"ai-title","aiTitle":"Fix login"}\n{"type":"ai-title","aiTitle":null}\n')
        out = [subprocess.run([sys.executable, "-c", claude.TITLE, sid], env=dict(os.environ, HOME=d), capture_output=True, text=True).stdout for sid in ("s1", "s2")]
        assert out == ["Fix login", ""], out

    # sweep(): messages deleted by hand are forgotten, newest first up to the first one still there; a session with none left goes from the pin; ACTIVE, `working` and other topics untouched; at most PROBE_MAX calls a pass, each session again only after PROBE_EVERY
    saved = state.STATE["sessions"], state.STATE["replies"], telegram.tg
    state.STATE["sessions"] = {"10": {"tid": 7, "at": 3}, "20": {"tid": 7, "at": 2}, "30": {"tid": 7, "at": 1}, "40": {"tid": 8, "at": 4}, "50": {"tid": 7, "at": 5, "working": 52}, "60": {"tid": 7, "at": 6}}
    state.STATE["replies"] = {"10": 10, "11": 10, "12": 10, "20": 20, "21": 20, "30": 30, "40": 40, "50": 50, "60": 60, "61": 60}
    state.ACTIVE[60] = types.SimpleNamespace(key=60, tid=7)
    deleted, calls = {11, 12, 20, 21, 30, 40, 50, 60, 61}, []

    def probe(method, **p):
        calls.append(p["message_id"])
        if p["message_id"] in deleted:
            raise telegram.TgError(f"{method}: Bad Request: message to edit not found")
        raise telegram.TgError(f"{method}: Bad Request: message can't be edited" if p["message_id"] == 10 else f"{method}: Bad Request: message is not modified")
    telegram.tg, pins.PROBED = probe, {}
    pins.sweep({7})
    assert calls == [12, 11, 10, 21, 20], calls
    assert set(state.STATE["sessions"]) == {"10", "30", "40", "50", "60"} and state.STATE["replies"] == {"10": 10, "30": 30, "40": 40, "50": 50, "60": 60, "61": 60}, state.STATE
    calls = []
    pins.sweep({7})
    assert calls == [30] and set(state.STATE["sessions"]) == {"10", "40", "50", "60"} and "30" not in state.STATE["replies"], (calls, state.STATE["sessions"])
    calls = []
    pins.sweep({7})
    assert calls == [], "10 was checked less than PROBE_EVERY ago"
    del state.ACTIVE[60]
    state.STATE["sessions"], state.STATE["replies"], telegram.tg = saved

    # the pin is sent and pinned once, edited only when its text changes, sent again when deleted, and pinned again until that works
    calls, fail = [], set()

    def fake(method, **p):
        calls.append(method)
        if method in fail:
            raise telegram.TgError(f"{method}: Bad Request: " + ("message to edit not found" if method == "editMessageText" else "not enough rights"))
        return {"message_id": next(mids)} if method == "sendMessage" else True
    telegram.tg, state.STATE["threads"] = fake, {"7": {"name": "a"}}
    pins.SHOWN.clear()
    pins.show(state.GROUP_ID, 7, "one")
    pins.show(state.GROUP_ID, 7, "one")
    first = state.settings(7)
    assert calls == ["sendMessage", "pinChatMessage"] and first["pinned"] and first["pin"], (calls, first)
    pins.show(state.GROUP_ID, 7, "two")
    fail.add("editMessageText")
    pins.show(state.GROUP_ID, 7, "three")
    assert calls[2:] == ["editMessageText", "editMessageText", "sendMessage", "pinChatMessage"] and state.settings(7)["pin"] != first["pin"], calls
    calls.clear()
    fail.update({"pinChatMessage"})
    state.update(7, pin=None)
    try:
        pins.show(state.GROUP_ID, 7, "four")
        assert False, "a failed pin must raise, refresh() logs it"
    except telegram.TgError:
        pass
    fail.clear()
    pins.show(state.GROUP_ID, 7, "four")
    assert calls == ["sendMessage", "pinChatMessage", "pinChatMessage"] and state.settings(7)["pinned"], calls

    # Claude's slash commands in the menu: hidden ones dropped, Telegram names and back, the bot's own names win, at most 100
    assert commands.tg_name("superpowers:Brain-Storm") == "superpowers_brain_storm" and len(commands.tg_name("x" * 40)) == 32
    state.STATE["commands"], state.STATE["threads"] = {}, {"7": {"name": "a"}, "8": {"name": "b"}, "9": {"name": "d"}}
    commands.learn("a", {"slash_commands": ["claude-optimize", "backup", "superpowers:brainstorming", "clear", "doctor", "simplify", "__remote-workflow", "status", "ponytail:ponytail-review", 7], "terminal_slash_commands": ["doctor"], "skills": ["simplify", "ponytail:ponytail-review", "superpowers:brainstorming"]})
    commands.learn("b", {"slash_commands": ["claude_optimize", "init"]})
    commands.learn("c", {"type": "system"})  # no list: nothing learned
    assert state.STATE["commands"] == {"a": ["claude-optimize", "simplify", "status", "ponytail:ponytail-review"], "b": ["claude_optimize", "init"]}, state.STATE["commands"]
    assert commands.table() == {"claude_optimize": "claude-optimize", "simplify": "simplify", "ponytail_ponytail_review": "ponytail:ponytail-review", "init": "init"}, commands.table()
    assert state.STATE["skills"] == {"a": ["simplify", "ponytail:ponytail-review"], "b": []}, state.STATE["skills"]
    # grouped menu: skills, then other commands, then one block per plugin, each sorted by name
    assert [(c["command"], c["description"]) for c in commands.menu()[len(BOT_MENU):]] == [("simplify", "🧩 skill · /simplify"), ("claude_optimize", "⚙️ command · /claude-optimize"), ("init", "⚙️ command · /init"), ("ponytail_ponytail_review", "🔌 ponytail · /ponytail:ponytail-review")], commands.menu()
    assert commands.translate("/claude_optimize@b keep\nthis") == "/claude-optimize keep\nthis" and commands.translate("/ponytail_ponytail_review") == "/ponytail:ponytail-review"
    assert commands.translate("/unknown-cmd@b x") == "/unknown-cmd x" and commands.translate("/") == "/" and commands.translate("/ x") == "/ x"
    assert commands.translate("/claude_optimize", "b") == "/claude_optimize" and commands.translate("/claude_optimize", "a") == "/claude-optimize", "a topic's own container names its command"
    state.STATE["commands"]["gone"] = ["only-here"]
    assert "only_here" not in commands.table(), "an unbound container's commands left in the menu"
    state.STATE["commands"]["d"] = [f"cmd{i}" for i in range(200)]
    cmds = commands.menu()
    assert len(cmds) == 100 and [c["command"] for c in cmds[:len(BOT_MENU)]] == [c for c, _ in BOT_MENU] and all(re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]) and 1 <= len(c["description"]) <= 256 for c in cmds), cmds[:8]
    init_line = json.dumps({"type": "system", "subtype": "init", "slash_commands": ["opt"]}).encode()
    probe_out = b"".join(bytes([k, 0, 0, 0]) + len(d).to_bytes(4, "big") + d for k, d in [(1, b'{"type": "system", "subtype": "hook_started"}\n' + init_line[:9]), (2, b"warn\n"), (1, init_line[9:] + b"\n")])
    real_start, real_exit = dockerapi.exec_start, dockerapi.exec_exit
    dockerapi.exec_start, dockerapi.exec_exit = (lambda cid, cmd, timeout=60: ("e1", io.BytesIO(probe_out))), (lambda eid, wait=2: 0)
    try:
        assert commands.probe("cid")["slash_commands"] == ["opt"]
        # refresh() probes a bound running container once, then sends the menu only when it changed
        state.STATE["commands"], state.STATE["skills"], state.STATE["threads"], commands.PROBED, commands.SYNCED = {"a": ["opt"]}, {}, {"7": {"name": "a"}, "8": {"name": "z"}}, set(), None  # commands known, skills not: kept before skills were, so probed again
        dockerapi.dev_containers = lambda: [{"Names": ["/dockbench-a"], "State": "running", "Id": "cid"}, {"Names": ["/dockbench-z"], "State": "exited", "Id": "z"}]
        calls = []
        telegram.tg = lambda method, **p: calls.append((method, p)) or True
        commands.refresh()
        commands.refresh()
        assert state.STATE["commands"] == {"a": ["opt"]} and state.STATE["skills"] == {"a": []} and commands.PROBED == {"a"} and [c[0] for c in calls] == ["setMyCommands"], (state.STATE["commands"], calls)
        assert calls[0][1]["scope"] == {"type": "chat", "chat_id": state.GROUP_ID} and calls[0][1]["commands"][-1] == {"command": "opt", "description": "⚙️ command · /opt"}, calls
    finally:
        dockerapi.exec_start, dockerapi.exec_exit = real_start, real_exit

    # push: asks first, then one plain-argv git push that cannot prompt, with the remote hidden; refused while a session of the container runs or with nothing ahead
    calls, pushed = [], []
    telegram.tg = lambda method, **p: calls.append((method, p)) or ({"message_id": 900} if method == "sendMessage" else True)
    state.STATE["threads"], state.STATE["sessions"] = {"7": {"name": "a"}}, {"100": {"name": "a"}}
    state.ACTIVE.clear()
    dockerapi.container = lambda name: {"Id": "cid", "State": "running"}
    ahead = {"repo": "r", "branch": "main", "oid": "0123456789abcdef", "upstream": True, "gone": False, "ahead": 2}
    pins.git_info = lambda cid: dict(ahead)
    subs = []
    pins.subrepos = lambda cid: [dict(s) for s in subs]
    dockerapi.exec_run =lambda cid, cmd, timeout=60, env=None: pushed.append((cmd, env)) or (0, b"To https://x:tok@github.com/a/r.git\n   1..2  main -> main\n")
    push.spawn = lambda target, tid, *a: target(tid, *a)  # same thread, so the test can read the result
    routing.handle({"update_id": 5, "message": topic_msg(400, "/push")}, {})
    assert "Push 2 commits of branch main of r" in calls[-1][1]["text"] and [b["callback_data"] for b in calls[-1][1]["reply_markup"]["inline_keyboard"][0]] == ["pushgo:7:0123456789ab", "pushno"], calls[-1]
    ahead["oid"] = "fedcba9876543210"
    tap("pushgo:7:0123456789ab", 900)  # HEAD moved since the question: nothing pushed
    assert pushed == [] and calls[-2][1]["text"].startswith("The branch moved"), calls[-2]
    ahead["oid"] = "0123456789abcdef"
    n = len(calls)
    tap("pushgo:7", 900)  # a malformed confirmation: ignored, the message untouched
    assert pushed == [] and [c[0] for c in calls[n:]] == ["answerCallbackQuery"], calls[n:]
    tap("pushgo:7:0123456789ab", 900)
    edits = [p["text"] for m, p in calls if m == "editMessageText"]
    assert [c for c, _ in pushed] == [["timeout", "120", "git", "push"]] and "GIT_TERMINAL_PROMPT=0" in pushed[0][1] and "CLAUDE_CODE_ENTRYPOINT=claude-vscode" in pushed[0][1], pushed
    assert edits[-2].startswith("pushing 2 commits") and edits[-1].startswith("✅ pushed main of r") and "<remote>" in edits[-1] and "tok" not in edits[-1], edits
    tap("pushno", 900)
    assert calls[-2][0] == "deleteMessage", calls[-2:]
    r = runs.Run(100, 7)
    r.q.put(None)
    state.ACTIVE[100] = r
    tap("push:7", 1)
    assert "A session is running in dockbench-a" in calls[-2][1]["text"] and len(pushed) == 1, calls[-2]
    tap("pushgo:7:0123456789ab", 900)  # a confirmation tapped after a session started
    assert "A session is running" in calls[-2][1]["text"] and calls[-2][0] == "editMessageText" and len(pushed) == 1, calls[-2]
    state.ACTIVE.clear()
    ahead["ahead"] = 0
    push.ask(7)
    assert calls[-1][1]["text"].startswith("Nothing to push") and len(pushed) == 1, calls[-1]
    ahead.update(upstream=False, gone=True)
    push.ask(7)
    assert "lost its upstream" in calls[-1][1]["text"] and len(pushed) == 1, calls[-1]
    # a subrepo: /push alone pushes the only repo with commits, asks which one when several have, /push <folder> names it
    ahead.update(upstream=True, gone=False, ahead=0)
    subs = [dict(ahead, dir="B", ahead=1, oid="abcdef0123456789"), dict(ahead, dir="C")]
    routing.handle({"update_id": 6, "message": topic_msg(401, "/push")}, {})
    assert "Push 1 commit of branch main of B" in calls[-1][1]["text"] and calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pushgo:7:abcdef012345:B", calls[-1]
    ahead["ahead"] = 2
    routing.handle({"update_id": 7, "message": topic_msg(402, "/push")}, {})
    assert calls[-1][1]["text"] == "Which repo do you want to push?" and [row[0]["callback_data"] for row in calls[-1][1]["reply_markup"]["inline_keyboard"]] == ["push:7", "push:7:B"], calls[-1]
    tap("push:7:B", 1)
    assert "of B (dockbench-a)" in calls[-2][1]["text"], calls[-2]
    routing.handle({"update_id": 8, "message": topic_msg(403, "/push r")}, {})
    assert "Push 2 commits of branch main of r" in calls[-1][1]["text"], calls[-1]
    routing.handle({"update_id": 9, "message": topic_msg(404, "/push C")}, {})
    assert calls[-1][1]["text"].startswith("Nothing to push") and len(pushed) == 1, calls[-1]
    routing.handle({"update_id": 10, "message": topic_msg(405, "/push nope")}, {})
    assert calls[-1][1]["text"] == "No repo nope in the workspace of dockbench-a." and len(pushed) == 1, calls[-1]
    tap("pushgo:7:abcdef012345:B", 900)
    assert pushed[-1][0] == ["timeout", "120", "git", "-C", "/home/dev/workspace/B", "push"] and calls[-2][1]["text"].startswith("✅ pushed main of B"), (pushed, calls[-2])

    # /clean: deletes the recorded messages of this topic's idle sessions only, in batches, one by one when a batch is refused; a session with a message left is kept for a retry
    state.STATE["sessions"] = {"10": {"tid": 7}, "20": {"tid": 7}, "30": {"tid": 7}, "40": {"tid": 8}, "50": {"tid": 7, "working": 52}}
    state.STATE["replies"] = {"10": 10, "11": 10, "20": 20, "21": 20, "30": 30, "40": 40, "50": 50, "52": 50}
    state.ACTIVE[30] = runs.Run(30, 7)
    calls = []

    def refusing(method, **p):
        calls.append((method, p))
        if method == "deleteMessages" or p.get("message_id") == 21:
            raise telegram.TgError(f"{method}: Bad Request: message can't be deleted")
        if p.get("message_id") == 11:
            raise telegram.TgError(f"{method}: Bad Request: message to delete not found")
        return True
    telegram.tg = refusing
    note = runs.clean(7)
    assert calls[0] == ("deleteMessages", {"chat_id": state.GROUP_ID, "message_ids": [10, 11, 20, 21]}) and [c[1]["message_id"] for c in calls[1:]] == [10, 11, 20, 21], calls
    assert set(state.STATE["sessions"]) == {"20", "30", "40", "50"} and state.STATE["replies"] == {"21": 20, "30": 30, "40": 40, "50": 50, "52": 50}, state.STATE
    assert note.startswith("deleted the messages of 1 idle session(s); 1 message(s) could not be deleted"), note
    state.ACTIVE.clear()
    calls = []
    telegram.tg = lambda method, **p: calls.append((method, p)) or True
    assert runs.clean(7) == "deleted the messages of 2 idle session(s)" and calls == [("deleteMessages", {"chat_id": state.GROUP_ID, "message_ids": [21, 30]})], calls
    assert set(state.STATE["sessions"]) == {"40", "50"} and runs.clean(7) == "no idle sessions to clean", state.STATE["sessions"]
    # the pin's Clean button: asks first, Cancel deletes the question, Clean runs /clean and edits its reply into the question
    assert pins.pin_markup(7, None, idle=2) == {"inline_keyboard": [[{"text": "🧹 Clean idle sessions (2)", "callback_data": "clean:7"}]]} and pins.pin_markup(7, dict(g, ahead=1), idle=1)["inline_keyboard"][1][0]["callback_data"] == "clean:7"
    state.STATE["sessions"]["60"], state.STATE["replies"]["60"] = {"tid": 7}, 60
    calls = []
    telegram.tg = lambda method, **p: calls.append((method, p)) or {"message_id": 910}
    real_threading = routing.threading
    routing.threading = type("T", (), {"Thread": staticmethod(lambda target, daemon: type("S", (), {"start": staticmethod(target)}))})  # same thread, so the test can read the result
    tap("clean:7", 1)
    assert calls[0][0] == "sendMessage" and [b["callback_data"] for b in calls[0][1]["reply_markup"]["inline_keyboard"][0]] == ["cleango:7", "cleanno"] and "60" in state.STATE["sessions"], calls
    tap("cleanno", 910)
    assert calls[-2] == ("deleteMessage", {"chat_id": state.GROUP_ID, "message_id": 910}) and "60" in state.STATE["sessions"], calls[-2:]
    tap("cleango:7", 910)
    edits = [p["text"] for m, p in calls if m == "editMessageText"]
    assert edits == ["cleaning…", "deleted the messages of 1 idle session(s)"] and "60" not in state.STATE["sessions"], (edits, state.STATE["sessions"])
    # /clean or /clear as a reply to a session: only that one, never one at work; a reply to an unknown message cleans nothing
    state.STATE["sessions"].update({"70": {"tid": 7}, "80": {"tid": 7}, "90": {"tid": 7}})
    state.STATE["replies"].update({"70": 70, "71": 70, "80": 80, "90": 90})
    state.ACTIVE[90] = runs.Run(90, 7)
    calls = []
    routing.handle({"update_id": 11, "message": topic_msg(406, "/clear", reply=71)}, {})
    assert calls[0] == ("deleteMessages", {"chat_id": state.GROUP_ID, "message_ids": [70, 71]}) and calls[-1][1]["text"].endswith("deleted the messages of session 🟥 #70"), calls
    assert "70" not in state.STATE["sessions"] and {"80", "90"} <= set(state.STATE["sessions"]) and "71" not in state.STATE["replies"], state.STATE
    calls = []
    routing.handle({"update_id": 12, "message": topic_msg(407, "/clean", reply=90)}, {})
    assert [m for m, p in calls] == ["sendMessage"] and calls[0][1]["text"].endswith("this session is running, /stop it first") and "90" in state.STATE["sessions"], calls
    calls = []
    routing.handle({"update_id": 13, "message": topic_msg(408, "/clean", reply=12345)}, {})
    assert [m for m, p in calls] == ["sendMessage"] and "not part of a session" in calls[0][1]["text"] and "80" in state.STATE["sessions"], calls
    state.ACTIVE.clear()
    routing.threading = real_threading

    # a session's badge: close keys differ in shape and color; its tag: Claude's title once it has one, else the badge, and the message's number in the session, which send() puts on top of every part, still within 4096
    assert telegram.badge(70) == "🟥 #70" and telegram.badge("71") == "🧡 #71" and len({telegram.badge(k).split()[0] for k in range(100, 121)}) == 21
    saved = state.STATE["sessions"], state.STATE["replies"], pins.TITLES
    state.STATE["sessions"], state.STATE["replies"], pins.TITLES = {"70": {"tid": 7}}, dict(state.STATE["replies"]), {}
    calls = []
    telegram.tg = lambda method, **p: calls.append(p) or {"message_id": len(calls)}
    assert telegram.send(7, "x" * 5000, tag=lambda: runs.label(70)) == [1, 2] and [c["text"].split("\n")[0] for c in calls] == ["🟥 #70 · #1", "🟥 #70 · #2"] and all(len(c["text"]) < 4096 for c in calls), [c["text"][:12] for c in calls]
    pins.TITLES[70] = "é" * 30 + "🙂" * 30  # TITLE_MAX characters, 90 UTF-16 units
    calls = []
    telegram.send(7, "a < b", md=True, tag=lambda: runs.label("70"))
    assert calls[0]["text"] == pins.TITLES[70] + " · #3\n" + markdown.md_html("a < b") and calls[0]["parse_mode"] == "HTML" and state.STATE["sessions"]["70"]["n"] == 3, calls
    calls = []
    telegram.send(7, "y" * 9000, tag=lambda: runs.label(70))
    assert all(len(c["text"].encode("utf-16-le")) // 2 <= 4096 for c in calls), [len(c["text"]) for c in calls]
    assert runs.label(99) == telegram.badge(99) + " · #1" == runs.label(99), "a forgotten session counts nothing"

    # a full status message: its turn text is kept whole, the first part in it under the tag, the others in new messages, then a new status message holds the rest and the turns said since
    assert telegram.overflow("h", ["a", "b"]) == ([], ["a", "b"])
    turns = ["old " * 1100, "mid " * 1100, "latest"]
    full, rest = telegram.overflow("head", turns)
    assert len(full) == 2 and len(rest) == 1 and rest[0].endswith("\n\nlatest") and "".join("".join(full + rest).split()) == "".join("".join(turns).split()), (len(full), rest)
    r = runs.Run(70, 7)
    r.req, r.status, r.turns = 5, 500, turns + ["since"]
    state.STATE["sessions"]["70"]["working"] = 500
    n = state.STATE["sessions"]["70"]["n"]
    calls = []
    telegram.tg = lambda method, **p: calls.append((method, p)) or {"message_id": 600 + len(calls)}
    runs.split(r, 500, "head", turns, full, rest)
    assert [m for m, _ in calls] == ["sendMessage", "sendMessage", "editMessageText"], calls
    assert calls[0][1]["text"].startswith(f"{pins.TITLES[70]} · #{n + 2}\n") and calls[1][1]["text"] == "head\n\n" + rest[0] and calls[1][1]["reply_markup"] == telegram.stop_markup(70), calls[:2]
    assert calls[2][1]["message_id"] == 500 and calls[2][1]["text"].startswith(f"{pins.TITLES[70]} · #{n + 1}\nold") and calls[2][1]["reply_markup"] is None, calls[2]
    assert calls[0][1]["parse_mode"] == calls[2][1]["parse_mode"] == "HTML" and "parse_mode" not in calls[1][1], "moved parts are Claude's markdown as HTML, the status message stays plain"
    fenced = telegram.overflow("head", ["```\n" + "code\n" * 3000 + "```", "latest"])[0]
    assert len(fenced) > 1 and all(markdown.md_html(p).count("<pre>") == markdown.md_html(p).count("</pre>") == 1 for p in fenced), "a part never ends inside a ``` block"
    assert r.status == 602 and r.turns == rest + ["since"] and r.dirty and state.STATE["sessions"]["70"]["working"] == 602 and state.STATE["replies"]["601"] == state.STATE["replies"]["602"] == 70, (r.status, r.turns)
    calls = []
    r.status = None  # the run ended meanwhile: the new status message goes, the old one is already deleted
    runs.split(r, 602, "head", ["latest"], ["a"], ["b"])
    assert [m for m, _ in calls] == ["sendMessage", "deleteMessage"] and r.status is None, calls
    state.STATE["sessions"], state.STATE["replies"], pins.TITLES = saved

    # images: photos in albums of up to 10 replying to the request, a GIF or an image over 10 MB as a file, a refused album resent as files
    calls, mid = [], iter(range(600, 700))

    def uploads(method, files=None, **p):
        calls.append((method, sorted(files or {}), p))
        if method == "sendMediaGroup" and any(d == b"wide" for _, d in files.values()):
            raise telegram.TgError("sendMediaGroup: Bad Request: PHOTO_INVALID_DIMENSIONS")
        return [{"message_id": next(mid)} for _ in p["media"]] if method == "sendMediaGroup" else {"message_id": next(mid)}
    telegram.tg = uploads
    pngs = [(f"{i}.png", b"p") for i in range(1, 12)]
    assert telegram.send_images(7, pngs + [("12.gif", b"g"), ("13.jpg", b"j" * (10 * 2**20 + 1))], reply=55) == list(range(600, 613))
    assert [(c[0], c[1]) for c in calls] == [("sendMediaGroup", [f"p{i}" for i in range(10)]), ("sendPhoto", ["photo"]), ("sendDocument", ["document"]), ("sendDocument", ["document"])], calls
    assert calls[0][2]["media"][9] == {"type": "photo", "media": "attach://p9"} and calls[0][2]["reply_parameters"]["message_id"] == 55 and calls[0][2]["message_thread_id"] == 7, calls[0]
    calls = []
    assert len(telegram.send_images(7, [("1.png", b"p"), ("2.png", b"wide")])) == 2 and [c[0] for c in calls] == ["sendMediaGroup", "sendDocument", "sendDocument"] and calls[-1][2]["reply_parameters"] is None, calls

    # the outbox after a run: images only, sorted, renamed, linked to the session, then the folder removed; a failure becomes a note
    calls, removed = [], []
    dockerapi.get_files = lambda cid, path, limit: [("b.PNG", b"2"), ("notes.txt", b"t"), ("a.jpg", b"1")]
    dockerapi.exec_run = lambda cid, cmd, timeout=60, env=None: removed.append(cmd) or (0, b"")
    runs.send_outbox(70, 7, "cid", "/o/70", 71)
    assert [c[0] for c in calls] == ["sendMediaGroup"] and calls[0][1] == ["p0", "p1"] and removed == [["rm", "-rf", "/o/70"]], (calls, removed)
    assert calls[0][2]["media"][0]["caption"] == "🟥 #70 · #1" and "caption" not in calls[0][2]["media"][1], calls[0]
    assert [m for m, k in state.STATE["replies"].items() if k == 70] == ["615", "616"] and calls[0][2]["reply_parameters"]["message_id"] == 71, state.STATE["replies"]
    calls = []
    dockerapi.get_files = lambda cid, path, limit: [(f"{i:02}.png", b"p") for i in range(25)]
    runs.send_outbox(70, 7, "cid", "/o/70", 71)
    assert [c[0] for c in calls] == ["sendMediaGroup", "sendMediaGroup", "sendMessage"] and "5 more images not sent" in calls[-1][2]["text"], calls
    calls = []

    def broken(cid, path, limit):
        raise dockerapi.DockerError("GET /containers/cid/archive: 500 boom")
    dockerapi.get_files = broken
    runs.send_outbox(70, 7, "cid", "/o/70", 71)
    assert [c[0] for c in calls] == ["sendMessage"] and "could not send Claude's images" in calls[0][2]["text"] and removed[-1] == ["rm", "-rf", "/o/70"], calls
    calls = []
    dockerapi.get_files = lambda cid, path, limit: []
    runs.send_outbox(70, 7, "cid", "/o/70", 71)
    assert calls == [], calls

    # the watchdog: a session write, or BUSY_CPU since the previous probe, is work; IDLE_LIMIT without either is a hung run
    r = runs.Run(1, 7)
    assert not runs.seen(r, 60, 5.0, None) and r.active == 0.0, r.active  # first probe: no CPU rate yet
    assert not runs.seen(r, 600, 6.0, 30) and r.active == 570, r.active  # 1 CPU s in 540 s is idle, the write 30 s ago is work
    assert not runs.seen(r, 1200, 66.0, 600) and r.active == 1200, r.active  # 60 CPU s in 600 s
    assert not runs.seen(r, 1199 + IDLE_LIMIT, 66.5, None) and runs.seen(r, 1200 + IDLE_LIMIT, 66.5, IDLE_LIMIT), r.active
    for out, want in [((0, b"12.5 30\n"), (12.5, 30)), ((0, b"1.0 -1\n"), (1.0, None)), ((3, b""), None), ((0, b"x"), None), ((0, b"1 2 3"), None)]:
        dockerapi.exec_run = lambda cid, cmd, timeout=60, env=None, out=out: out
        assert claude.activity("c", "s") == want, (out, want)

    # /usage in General: one block per account, read once from its first container; signed-out, failed and stopped containers listed after
    out = "You are currently using your subscription to power your Claude Code usage\n\nCurrent session: 2% used · resets Nov 2, 9:20pm (UTC)\nCurrent week (all models): 53% used · resets Nov 3, 1am (UTC)\n\nWhat's contributing to your limits usage?\nLast 24h · 5 requests\n  82%  Turn off skills\n"
    assert usage.limits(out) == ["Current session: 2% used · resets Nov 2, 9:20pm (UTC)", "Current week (all models): 53% used · resets Nov 3, 1am (UTC)"]
    dockerapi.dev_containers = lambda: [{"Id": i, "Names": [f"/dockbench-{i}"], "State": s} for i, s in [("b", "running"), ("a", "running"), ("c", "running"), ("d", "exited"), ("e", "running"), ("f", "running")]] + [{"Id": "x", "Names": ["/other"], "State": "running"}]
    auth = {"a": (0, b'{"loggedIn": true, "email": "me@x", "subscriptionType": "max"}'), "b": (0, b'warn\n{"loggedIn": true, "email": "me@x", "subscriptionType": "max"}'), "c": (1, b'{"loggedIn": false}'), "f": (0, b'{"loggedIn": true, "email": "two@x"}')}
    execs = []

    def fake(cid, cmd, timeout=60, env=None):
        if cmd[0] == dockerapi.AGENT:  # images without dockbench-agent: account() on every pass
            return 127, b'OCI runtime exec failed: exec failed: unable to start container process: exec: "/usr/local/bin/dockbench-agent": stat /usr/local/bin/dockbench-agent: no such file or directory'
        execs.append((cid, cmd[3]))
        if cmd == usage.USAGE:
            return (0, out.encode()) if cid == "a" else (1, b"Error: rate limited\n")
        if cid == "e":
            raise dockerapi.DockerError("boom")
        return auth[cid]
    dockerapi.exec_run = fake
    report = usage.report()
    assert report == "👤 me@x (max) · a, b\nCurrent session: 2% used · resets Nov 2, 9:20pm (UTC)\nCurrent week (all models): 53% used · resets Nov 3, 1am (UTC)\n\n👤 two@x · f\nusage unavailable: Error: rate limited\n\nnot signed in: c\nerror: e\nnot running: d", report
    assert sorted(c for c, k in execs if k == "-p") == ["a", "f"], execs
    assert dockerapi.NO_AGENT >= {"a", "b", "c", "f"} and not usage.ACCOUNTS, (dockerapi.NO_AGENT, usage.ACCOUNTS)

    # with dockbench-agent, `claude auth status` reruns only when the sign-in fingerprint changes; a stopped container's entry goes
    dockerapi.NO_AGENT.clear()
    keys, execs = {"a": b"10:1:me@x\n", "b": b"10:2:me@x\n", "c": b"signed-out:\n", "e": b"x\n", "f": b"9:9:two@x\n"}, []

    def fake2(cid, cmd, timeout=60, env=None):
        if cmd == [dockerapi.AGENT, "claude-key"]:
            return 0, keys[cid]
        return fake(cid, cmd, timeout, env)
    dockerapi.exec_run = fake2
    assert usage.report() == report and sorted(c for c, k in execs if k == "auth") == ["a", "b", "c", "e", "f"], execs  # e fails, so it is never cached
    execs.clear()
    keys["b"] = b"11:3:me@x\n"
    assert usage.report() == report and sorted(c for c, k in execs if k == "auth") == ["b", "e"], execs
    dockerapi.dev_containers = lambda: [{"Id": "a", "Names": ["/dockbench-a"], "State": "running"}]
    usage.report()
    assert set(usage.ACCOUNTS) == {"a"}, usage.ACCOUNTS
    usage.ACCOUNTS.clear()

    # dockbench-agent probes: one exec each, the older execs only for an image without it (asked once per container id)
    seen = []

    def agent_exec(cid, cmd, timeout=60, env=None):
        seen.append((cid, cmd))
        if cid == "old":
            return (127, b"OCI runtime exec failed: exec failed") if cmd[0] == dockerapi.AGENT else (0, b"7.5 3\n" if cmd[0] == "python3" else b"")
        if cmd == [dockerapi.AGENT, "pin-info", WORKSPACE]:
            return 0, json.dumps({"git": {"branch": "main", "oid": "abc", "untracked": 1, "upstream": True, "gone": False, "ahead": 2, "add": 3, "del": 1, "files": 2, "repo": "shop"}, "subs": [{"branch": "feat", "oid": "def", "untracked": 0, "upstream": False, "gone": True, "ahead": 0, "dir": "A"}]}).encode()
        if cmd == [dockerapi.AGENT, "pin-info", "bad"]:
            return 0, b"not json"
        return {"activity": (0, b"12.5 30\n"), "title": (0, b"Fix login")}[cmd[1]]
    dockerapi.exec_run = agent_exec
    assert claude.activity("new", "s") == (12.5, 30) and claude.title("new", "s") == "Fix login" and seen == [("new", [dockerapi.AGENT, "activity", "s"]), ("new", [dockerapi.AGENT, "title", "s"])], seen
    g, subs = pins.pin_facts("new")
    assert (g["repo"], g["ahead"], subs[0]["dir"], subs[0]["gone"]) == ("shop", 2, "A", True) and len(seen) == 3, (g, subs)
    seen.clear()
    assert claude.activity("old", "s") == (7.5, 3) and claude.activity("old", "s") == (7.5, 3), seen
    assert [c[0] for _, c in seen] == [dockerapi.AGENT, "python3", "python3"] and dockerapi.NO_AGENT == {"old"}, seen
    real_info, real_subs = pins.git_info, pins.subrepos
    pins.git_info, pins.subrepos = (lambda cid: {"from": "git"}), (lambda cid: [])
    try:
        real_ws, pins.WORKSPACE = pins.WORKSPACE, "bad"
        assert pins.pin_facts("new") == ({"from": "git"}, []) and pins.pin_facts("old") == ({"from": "git"}, [])
    finally:
        pins.git_info, pins.subrepos, pins.WORKSPACE = real_info, real_subs, real_ws

    # the probes over HTTP: no exec while the agent answers on its port; the token is read once through the archive API and again after a 401; an agent without the routes (404) or without a token file is remembered per id, a connection failure is not
    real_files, real_http = dockerapi.get_files, dockerapi.http_get
    dockerapi.NAMES["new"] = "dockbench-new"
    tok, reads, http, answers = {"new": b"tok1\n"}, [], [], {"pin-info": (200, b'{"git": {"branch": "main", "oid": "abc", "untracked": 0, "upstream": True, "gone": False, "ahead": 0, "add": 0, "del": 0, "files": 0, "repo": "shop"}, "subs": []}'.replace(b"True", b"true").replace(b"False", b"false")), "activity": (204, b""), "title": (200, b"Fix login"), "claude-key": (200, b"1:2:me@x\n")}

    def fake_files(cid, path, limit):
        reads.append(cid)
        assert path == dockerapi.AGENT_TOKEN and limit == 4096, (path, limit)
        if cid not in tok:
            raise dockerapi.DockerError("GET /containers/new/archive: 404 no such file")
        return [("", tok[cid])]

    def fake_http(url, headers, timeout):
        route = url.split("/", 3)[3].split("?")[0]
        http.append((url, headers["Authorization"]))
        if headers["Authorization"] != f"Bearer {tok['new'].strip().decode()}":
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        if isinstance(answers[route], Exception):
            raise answers[route]
        return answers[route]
    dockerapi.get_files, dockerapi.http_get = fake_files, fake_http
    seen.clear()
    assert claude.activity("new", "s") is None and claude.title("new", "s") == "Fix login" and pins.pin_facts("new")[0]["repo"] == "shop" and dockerapi.agent("new", ["claude-key"]) == (0, b"1:2:me@x\n")
    assert seen == [] and reads == ["new"], (seen, reads)
    assert [u.split("8080")[1] for u, _ in http] == ["/activity?session=s", "/title?session=s", "/pin-info?dir=%2Fhome%2Fdev%2Fworkspace", "/claude-key"], http
    tok["new"] = b"tok2\n"  # the agent restarted: one 401, the token is read again, the probe answers
    assert claude.title("new", "s") == "Fix login" and reads == ["new", "new"] and [a for _, a in http[-2:]] == ["Bearer tok1", "Bearer tok2"] and seen == [], (reads, http, seen)
    answers["title"] = urllib.error.HTTPError("u", 404, "Not Found", {}, None)  # an agent without the routes: exec from now on, HTTP not asked again
    n = len(http)
    assert claude.title("new", "s") == "Fix login" and claude.title("new", "s") == "Fix login" and dockerapi.NO_HTTP == {"new"} and len(http) == n + 1 and [c[1] for _, c in seen] == ["title", "title"], (http[n:], seen)
    dockerapi.NO_HTTP.clear(), dockerapi.TOKENS.clear(), seen.clear()
    answers["title"] = OSError("connection refused")  # nothing answers this time: exec now, HTTP again next time
    assert claude.title("new", "s") == "Fix login" and not dockerapi.NO_HTTP and [c[1] for _, c in seen] == ["title"], seen
    del tok["new"]  # no token file: the image has an agent without the routes
    dockerapi.TOKENS.clear(), seen.clear()
    assert claude.title("new", "s") == "Fix login" and dockerapi.NO_HTTP == {"new"} and [c[1] for _, c in seen] == ["title"], seen
    dockerapi.get_files, dockerapi.http_get = real_files, real_http
    dockerapi.NAMES.clear(), dockerapi.TOKENS.clear(), dockerapi.NO_HTTP.clear()
    dockerapi.NO_AGENT.clear()

    # the usage message in General: sent and pinned once, then edited only when its text changed, sent again when deleted
    calls = []

    def tg(method, **p):
        calls.append(method)
        if method == "editMessageText" and p["message_id"] == 41:
            raise telegram.TgError("Bad Request: message to edit not found")
        return {"message_id": 41 if len(calls) == 1 else 42} if method == "sendMessage" else True
    telegram.tg = tg
    state.GROUP_ID = -100
    state.STATE.pop("usage", None)
    usage.show(-100, "one")
    usage.show(-100, "one")
    assert calls == ["sendMessage", "pinChatMessage"] and state.STATE["usage"] == 41, calls
    usage.show(-100, "two")
    assert calls[2:] == ["editMessageText", "sendMessage", "pinChatMessage"] and state.STATE["usage"] == 42, calls
    usage.show(-100, "three")
    assert calls[5:] == ["editMessageText"], calls
    print("ok")
