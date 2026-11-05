"""Claude's slash commands in the group's Telegram command menu: learned per container from the init event of Claude's stream, translated to Telegram command names and back."""
import json
import re

from . import dockerapi, state, telegram
from .config import BOT_MENU, HIDDEN_COMMANDS, HIDDEN_PREFIXES, log

# A local command: Claude emits its init event and answers without a model call, so the probe costs nothing (checked with Claude Code 2.1.300). Not persisted, and RUN_MARK does not match it, so recover() and /stop leave it alone.
PROBE = ["timeout", "60", "claude", "-p", "--no-session-persistence", "--permission-mode", "plan", "--permission-prompts", "none", "--output-format", "stream-json", "--verbose", "--", "/release-notes"]
SYNCED = None  # (group, menu) last sent with setMyCommands, so an unchanged menu is not sent again
PROBED = set()  # PROJECT names probed since start: a probe that finds nothing is not retried every minute


def tg_name(name):
    """Telegram's form of a Claude command name: lowercase letters, digits and _, at most 32 chars."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower())[:32]


def learn(name, init):
    """Keep the slash commands of an init event as the command list of container dockbench-<name>, without the hidden and terminal-only ones."""
    names, terminal = (init or {}).get("slash_commands"), (init or {}).get("terminal_slash_commands") or []
    if not isinstance(names, list):
        return
    names = [n for n in names if isinstance(n, str) and n and n not in terminal and n not in HIDDEN_COMMANDS and not n.startswith(HIDDEN_PREFIXES)]
    skills = [n for n in names if n in ((init or {}).get("skills") or [])]
    with state.LOCK:
        if state.STATE["commands"].get(name) == names and state.STATE["skills"].get(name) == skills:
            return
        state.STATE["commands"][name], state.STATE["skills"][name] = names, skills
        state.save()
    log.info("dockbench-%s: %d slash commands", name, len(names))


def table(first=None):
    """Telegram name -> Claude name for the commands of the bound containers, the first one kept when two map to the same name: container first (a topic's own) wins, then the others by name. The bot's own commands win over all. Lists of unbound containers are left out."""
    with state.LOCK:
        bound = sorted({t["name"] for t in state.STATE["threads"].values() if t.get("name")})
        lists = [state.STATE["commands"].get(n) or [] for n in ([first] if first else []) + bound]
    out, own = {}, {c for c, _ in BOT_MENU} | {"start"}
    for names in lists:
        for n in names:
            t = tg_name(n)
            if t not in own and t not in out:
                out[t] = n
    return out


def group(name, skills):
    """Sort key and label of a Claude command in the menu: skills, then the other commands (built-ins and ~/.claude/commands), then one block per plugin."""
    if ":" in name:
        plugin = name.split(":", 1)[0]
        return (2, plugin), f"🔌 {plugin}"
    return ((0, ""), "🧩 skill") if name in skills else ((1, ""), "⚙️ command")


def menu():
    """The bot's own commands, then Claude's grouped by group(): Telegram's menu has no sections, so a group is a run of entries with the same label."""
    own = [{"command": c, "description": d} for c, d in BOT_MENU]
    with state.LOCK:
        skills = {n for names in state.STATE["skills"].values() for n in names}
    claude = sorted(table().items(), key=lambda tn: (group(tn[1], skills)[0], tn[0]))
    return own + [{"command": t, "description": f"{group(n, skills)[1]} · /{n}"[:256]} for t, n in claude][:100 - len(own)]


def translate(text, name=None):
    """'/claude_optimize@bot args' -> '/claude-optimize args' for a command of the menu, as container dockbench-<name> calls it if it has it. The @bot part goes in any case (a menu tap in a group adds it), any other text is returned as is."""
    m = re.match(r"/([^\s@]+)(@\S*)?(.*)", text, re.S)
    if not m:
        return text
    return "/" + table(name).get(m[1].lower(), m[1]) + m[3]


def probe(cid):
    """The init event of a probe run in the container, None when there is none (Claude not installed, a crash)."""
    eid, resp = dockerapi.exec_start(cid, PROBE, timeout=90)
    with resp:
        out = b"".join(data for kind, data in dockerapi.frames(resp) if kind == 1)  # stdout only: a stderr frame could split a JSON line
    dockerapi.exec_exit(eid)
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev
    return None


def refresh():
    """Probe each running bound container once whose commands and skills are not known yet, then send the menu if it changed. Runs on the pins thread."""
    global SYNCED
    with state.LOCK:
        bound = {t["name"] for t in state.STATE["threads"].values() if t.get("name")}
        known = set(state.STATE["skills"])  # not commands: a list learned before skills were kept gets probed again once, to group the menu
    for c in dockerapi.dev_containers():
        name = dockerapi.project(c)
        if name in bound and name not in known and name not in PROBED and c["State"] == "running":
            PROBED.add(name)
            try:
                learn(name, probe(c["Id"]))
            except dockerapi.DockerError as e:
                log.warning("dockbench-%s: could not read its slash commands: %s", name, e)
    group, cmds = state.GROUP_ID, menu()
    if SYNCED != (group, cmds):
        telegram.tg("setMyCommands", commands=cmds, scope={"type": "chat", "chat_id": group})
        SYNCED = (group, cmds)
