"""The `claude -p` command, its stream-json output, and the answer text."""
import http.client
import json
import re

from . import dockerapi
from .config import DEFAULT_PERM, DEFAULT_PROMPT, INBOX, NICE, OUTBOX_PROMPT, RUN_TIMEOUT, TITLE_MAX


def claude_cmd(cfg, prompt, sid, resume, out):
    """out: the session's outbox folder, named in the appended system prompt."""
    # timeout kills the whole process group, so tool subprocesses die too and the exec stream closes. nice: a run and its tools yield the cores to the container's VS Code and to other containers (a Claude Code start alone takes ~1.5 cores for 2 s). "--" keeps a prompt starting with "-" from being read as a flag.
    return ["timeout", "-k", "30", str(RUN_TIMEOUT), "nice", "-n", NICE, "claude", "-p",
            "--model", cfg["model"], "--effort", cfg["effort"], "--permission-mode", cfg.get("perm") or DEFAULT_PERM,
            "--permission-prompts", "none", "--add-dir", INBOX, "--add-dir", out, "--append-system-prompt", OUTBOX_PROMPT.format(dir=out),
            "--output-format", "stream-json", "--verbose", "--max-turns", "180",
            "--resume" if resume else "--session-id", sid, "--", prompt]


def activity(cid, sid):
    """(CPU seconds used so far, seconds since the session last wrote its transcript or None) of session sid's run in the container, or None when it is not found."""
    code, out = dockerapi.agent(cid, ["activity", sid]) or (None, b"")
    try:
        cpu, age = out.split()
        return (float(cpu), int(age) if int(age) >= 0 else None) if code == 0 else None
    except ValueError:
        return None


def title(cid, sid):
    """The title Claude Code gave session sid, on one line and at most TITLE_MAX characters; "" when it has none yet."""
    code, out = dockerapi.agent(cid, ["title", sid]) or (None, b"")
    return " ".join(out.decode(errors="replace").split())[:TITLE_MAX] if code == 0 else ""


def turn_text(ev):
    """Claude's text in an assistant stream event, without its tool calls and thinking. "" when there is none."""
    if ev.get("type") != "assistant":
        return ""
    blocks = (ev.get("message") or {}).get("content") or []
    return "\n".join(b["text"].strip() for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip())


def parse_stream(frames_iter, on_session, on_text=None):
    """Read stream-json from stdout frames. on_text gets Claude's text of each turn as it comes. Returns (final result event or None, tail of stderr and non-JSON stdout, the system init event or None: it lists the session's tools and permission mode)."""
    result, err, buf, seen, init = None, b"", bytearray(), None, None
    for kind, data in frames_iter:
        if kind != 1:
            err = (err + data)[-2000:]
            continue
        buf += data
        if b"\n" not in data:
            continue
        *lines, buf = buf.split(b"\n")
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                err = (err + bytes(line) + b"\n")[-2000:]
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("session_id") and ev["session_id"] != seen:
                seen = ev["session_id"]
                on_session(seen)
            if on_text and (t := turn_text(ev)):
                on_text(t)
            if ev.get("type") == "result":
                result = ev
            elif ev.get("type") == "system" and ev.get("subtype") == "init" and init is None:
                init = ev
    return result, err.decode(errors="replace").strip(), init


def run_claude(cid, cfg, prompt, sid, resume, out, on_session, on_text=None):
    eid, resp = dockerapi.exec_start(cid, claude_cmd(cfg, prompt, sid, resume, out), timeout=RUN_TIMEOUT + 300)
    try:
        with resp:
            result, err, init = parse_stream(dockerapi.frames(resp), on_session, on_text)
    except (OSError, http.client.HTTPException) as e:
        # A socket proxy cuts streams that stay silent for 10 min, but the run goes on: wait for it below, so a topic never has two runs at once.
        result, err, init = None, f"Lost the output stream from Docker ({type(e).__name__}). The run went on in the container; open its session in VS Code to see what it did.", None
    return result, dockerapi.exec_exit(eid, RUN_TIMEOUT + 60), err, init


def tool_notes(init, perm=DEFAULT_PERM):
    """Why Claude could not run commands, read from the session's init event: a Bash missing from its tools (a deny rule or disallowedTools in the container's ~/.claude settings removes it, verified with Claude Code 2.1.299), or a permission mode other than the one asked for (the init event reports manual as default, seen with 2.1.300)."""
    if not init:
        return []
    notes = []
    if init.get("permissionMode") and init["permissionMode"] != {"manual": "default"}.get(perm, perm):
        notes.append(f"⚠️ Claude ran in permission mode {init['permissionMode']}, not {perm}, so anything needing approval was denied." + (" Auto mode may not be available for this account or model." if perm == "auto" else ""))
    if isinstance(init.get("tools"), list) and "Bash" not in init["tools"]:
        notes.append("⚠️ Claude had no Bash tool in this container, so it could not build, test or run any command. A deny rule for Bash in ~/.claude/settings.json (your Claude config repo, copied into every container) or in the project's .claude/settings.json removes it.")
    return notes


def summarize(result, code, err, init=None, perm=DEFAULT_PERM):
    """The answer as Claude wrote it (markdown, converted when sent), plus what went wrong. No turn count or cost."""
    if result is None:
        text, failed = f"claude exited with code {code} and no result.\n{err}", True
    else:
        text = (result.get("result") or "; ".join(result.get("errors") or []) or result.get("subtype") or "no result").strip()
        failed = bool(result.get("is_error"))
        if failed:
            text = f"error ({result.get('subtype')}): {text}"
        if result.get("subtype") == "error_max_turns":
            text += "\nSend a follow-up to continue."
        denied = sorted({d.get("tool_name") or "?" for d in result.get("permission_denials") or []})
        if denied:
            text += f"\n\n⚠️ Denied without asking: {', '.join(denied)}"
    for note in tool_notes(init, perm):
        text += "\n\n" + note
    if failed and re.search(r"/login|not logged in|invalid api key|authenticat|oauth", text, re.I):
        text += "\n\nClaude does not seem logged in in this container: open it in VS Code through the tunnel and run `claude` once to log in."
    if result is None and code == 127:
        # timeout(1) exits 127 when it cannot find claude: the container was deployed with INSTALL_CLAUDE=0
        text += "\n\nClaude Code is not installed in this container (deployed with INSTALL_CLAUDE=0). Set INSTALL_CLAUDE=1 on its stack and restart it, or run `curl -fsSL https://claude.ai/install.sh | bash` in its terminal."
    return text


def prompt_for(text, files):
    return "\n".join([text or DEFAULT_PROMPT] + [f"{'Screenshot' if img else 'File'}: {INBOX}/{name}" for name, img in files])
