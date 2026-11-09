"""Constants shared by the modules, and the logger."""
import logging
import re

WORKSPACE = "/home/dev/workspace"
INBOX = "/home/dev/.telegram/inbox"
# one folder per session (<OUTBOX>/<session key>): Claude puts there the images to send after its answer. Per session, not per run, because Claude Code records the system prompt that names it on a session's first request and reuses it on every resume.
OUTBOX = "/home/dev/.telegram/outbox"
OUTBOX_PROMPT = "You run headless behind a Telegram bot: the user reads your final answer in Telegram. To show the user images (screenshots, charts, diagrams), save or copy them as PNG, JPEG, WebP or GIF files into {dir}: after your answer the bot sends every image found there to the user, then empties the folder. Say in your answer what each image shows."
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_IMAGES = 20  # per run; the rest are left out with a note
OUTBOX_MAX = 100 * 2**20  # bytes of the outbox tar read from the container
# claude -p records itself as "sdk-cli", and the VS Code extension hides those sessions from its list. Recording "claude-vscode" makes Telegram sessions show up (and resumable) in the container's VS Code.
EXEC_ENV = ["HOME=/home/dev", "CLAUDE_CODE_ENTRYPOINT=claude-vscode"]
RUN_TIMEOUT = 4 * 3600  # hard limit, for a run that keeps busy without end; a hung one is killed sooner, see IDLE_LIMIT
# A run that neither wrote to its session nor used BUSY_CPU of a core for this long is hung and killed (runs.watchdog). An idle claude waiting on a tool uses ~1% (measured 2026-11-01).
IDLE_LIMIT = 15 * 60
BUSY_CPU = 0.05
# Only the bot's own runs start like this (see claude.claude_cmd), never an interactive or VS Code claude, so pkill on it is safe.
RUN_MARK = "^timeout -k 30 [0-9]+ claude -p "
COMMANDS = ("bind", "clean", "clear", "help", "push", "status", "stop")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MODELS = ("fable", "opus", "sonnet", "haiku")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMS = ("auto", "acceptEdits", "plan", "manual", "bypassPermissions")  # the picker's third step; manual is the CLI's name for default
DEFAULT_PERM = "auto"  # sessions started before the picker asked the mode ran in auto
# Claude slash commands kept out of the Telegram menu: built-ins that only work in the interactive terminal UI (seen in the init event of Claude Code 2.1.300, which flags only some of them in terminal_slash_commands), and the user's pick of commands not wanted from Telegram (2026-10-15: the superpowers plugin and backup).
HIDDEN_COMMANDS = frozenset({"advisor", "agents", "auto-mode-setup", "autocompact", "backup", "clear", "color", "config", "design-consent", "design-revoke", "doctor", "effort", "extra-usage", "fast", "focus", "goal", "heapdump", "import", "list-agents", "mcp", "model", "output-style", "release-notes", "reload-plugins", "reload-skills", "rename", "team-onboarding", "usage-credits", "workflow-launch-exec"})
HIDDEN_PREFIXES = ("superpowers:", "_")
# the bot's own entries of the Telegram command menu, before Claude's (Telegram allows 100 in all)
BOT_MENU = (("status", "Container state and the sessions of this topic"), ("stop", "Stop the sessions of this topic, or the replied-to one"), ("push", "Push a repo's commits, after a confirmation"), ("clean", "Delete the idle sessions' messages, or the replied-to one's"), ("bind", "Link this topic to dockbench-<PROJECT>"), ("setup", "Check what the bot needs in this group"), ("help", "How the bot works"))
MAX_SESSIONS = 300  # sessions kept in state.json for replies; the oldest idle ones are forgotten first
PIN_IDLE = 10  # idle sessions listed on a topic's pin, newest first; the rest are counted, so the pin stays far below Telegram's 4096 characters
PROBE_EVERY = 1800  # seconds between two checks that a listed idle session still has a message in the chat (pins.sweep)
PROBE_MAX = 5  # Telegram calls per pins pass for those checks, so they do not eat the group's rate limit
TITLE_MAX = 60  # characters of a session's title kept for its pin row (Claude Code's titles are shorter, the container is not trusted)
IDLE = "💤"  # pin row of a session with no prompt running or queued
# the session badge on its pin row and /status line, and on its messages until Claude Code titles it (see telegram.badge, runs.name): 3 shapes x 7 colors, picked by key % 3 and key % 7, which walk all 21 pairs, so sessions started close together (close message ids) differ in both shape and color
BADGES = (("🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤"), ("🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫"), ("❤️", "🧡", "💛", "💚", "💙", "💜", "🤎"))
# pin row of a session with a prompt running, or only queued
RUNNING, QUEUED = "👨‍💻", "👀"
DEFAULT_PROMPT = "Look at the attached screenshot(s) and fix the bug shown."
HELP = f"""One topic per dev container: the topic name is the PROJECT of container dockbench-<PROJECT>.
Every message in a topic (text, screenshots or files, with a caption) starts a new Claude Code session in that container: pick the model, the effort and the permission mode on the buttons. The answer comes as a reply to your message. Send several messages to run several sessions at once.
Each session's messages start with its name, the title Claude Code gives it (until then its badge, a colored shape and its id: 🟢 #4518), and the message's number in the session (Fix login bug · #3), so parallel sessions are told apart at a glance.
To continue a session, reply to its first message or to one of its answers. Sessions also appear in the Claude Code session list in the container's VS Code.
While a session runs, its status message shows the elapsed time, Claude's messages so far and a Stop button, and the topic shows the bot typing. When Claude's messages no longer fit, they stay in messages of their own and a new status message follows.
Claude can send images back (screenshots, charts): they come after its answer, as photos.
The pinned message of each topic shows the workspace repo, its branch, the commits to push, its uncommitted changes, the repos inside the workspace that have commits to push, a Push button per repo, the sessions here and a Clean button for the idle ones.

/status - container state and the sessions of this topic
/stop - stop every session running in this topic; as a reply to a session, only that one
/push [repo] - push the commits of the workspace repo, or of a repo folder inside it, to its upstream (asks first; with several to push, asks which; also the Push buttons on the pin)
/clean - delete the messages of the sessions of this topic that are not running (requests, answers, status messages); they can no longer be continued from Telegram (also the Clean button on the pin, after a confirmation). As a reply to a session (also /clear), only that one
/bind <PROJECT> - link this topic to dockbench-<PROJECT>
/setup - check what the bot needs in this group (also /start, which the dashboard's claim and Add to group links send)
/help
Claude's skills and slash commands are in the / menu (as /name_with_underscores), grouped: 🧩 skills, ⚙️ other commands, then 🔌 one block per plugin. Any other /command goes to Claude as a prompt too."""
SETUP_STEPS = """You are the owner. Next:
1. Create a group, open its settings and enable Topics.
2. Add @{bot} to it as admin with the Manage topics, Pin messages and Delete messages rights.
3. Send /setup@{bot} in the group: the bot checks what it needs there and starts working in it.
Send /setup@{bot} in the group again any time to re-check."""
STATUS_PORT = 8080

log = logging.getLogger("dockbench-telegram")
