# DockBench Telegram: development notes

The technical side of the bot: how it is built, how it behaves in the edge cases, the security model, and how to build and change it. Setup and everyday use are in the [README](README.md).

## Architecture

- No inbound traffic from outside. The bot long-polls Telegram over outbound HTTPS: no webhook, no published port, no reverse proxy. The only listener is `GET /status` on port 8080 for the dashboard, reachable inside `devnet` only, where every dev container can read it too.
- Docker exec, nothing else. A session is `claude -p` executed in the dev container as its normal user, with the prompt passed as one argument. Screenshots go in through the container's archive API, and images come back the same way.
- One JSON file of state. Topic bindings, the last pick per topic and which message belongs to which session live in one volume. Message text is never stored or logged.
- No dependencies. Python standard library only, in a `python:3.13-alpine` image.
- The container joins the `devnet` network only and publishes no port. It answers `GET /status` on port 8080 inside `devnet` for the [dashboard](https://github.com/Sander972/DockBench-Dashboard): bot username, whether owner and group are set, the bound topics with running and queued counts, and the last successful poll. There is no auth, so any dev container on `devnet` can read it: it never returns ids, the setup code or the token.

## Behavior in detail

### Sessions

1. The picker asks the model (`fable`, `opus`, `sonnet`, `haiku`), then the effort (`low` to `max`), then the permission mode (`auto`, `acceptEdits`, `plan`, `manual`, `bypassPermissions`). The first row, `▶ <model> · <effort> · <mode> (as last time)`, repeats the topic's previous pick; `‹` goes back one step and `✖ Cancel` drops the request.
2. The picker message turns into the status: `Fix login bug · working · opus · effort high · auto · new session · 3m 05s`, with a **⏹ Stop** button and what Claude writes between its steps, as plain text. When that text fills the message, it stays in messages of its own, formatted like the answer, and a new status message carries on below them.
3. The answer arrives as a reply to the request and the status message is deleted. Claude's markdown becomes Telegram formatting: bold, italics, code, links, headings in bold, bullets, and tables as a monospace block.
4. Only when something went wrong, a line below the answer lists the tools denied without asking and why Claude could not run commands.

A follow-up is a reply to the session's first message, to any of its answers or to its status message. It runs with the model, effort and mode picked when the session started. Follow-ups of one session run one after the other (`queued in this session, N ahead`).

Every message of a session starts with the title Claude Code gave it (usually within its first minute) and the message's number: `Fix login bug · #3`. Until there is a title, a badge stands in: a colored shape and the session's id, like `🟢 #4518 · #1`. Telegram has no colored text, so the color is the emoji, and sessions started close together always differ in shape and color.

### Images

An album of several photos becomes one prompt. Screenshots stay in the container at `/home/dev/.telegram/inbox/` and can be opened from VS Code.

Claude saves the images it wants to send in its session's outbox in the container. After the answer the bot sends them as photos replying to the request, in albums of up to 10, at most 20 per answer. A GIF, an image over 10 MB, or one Telegram refuses as a photo is sent as a file. Telegram compresses photos, so a long full-page screenshot may be hard to read.

### Pinned status

The pin is refreshed about every minute, and within seconds of a session starting or ending. It shows:

- 📦 the workspace repo of the container, named after its `origin` remote (the URL is never shown), and the project name when it differs;
- 🌿 the branch, with `↑N to push`, `no upstream` or `upstream gone`;
- ✏️ the uncommitted changes (`+added −deleted in N files`, plus untracked files) or `clean`;
- 📂 the repos one folder level inside the workspace that have something to push (multi-repo workspaces);
- the topic's sessions with a link to each one's last message: running (`👨‍💻 4m`) and queued (`👀 queued`) first, then the idle ones a reply still continues (`💤 3h`, newest first). Past 10 idle sessions only a count is shown.

If the pin is deleted, the bot posts and pins a new one. Without the Pin messages right it is posted unpinned, and pinned as soon as the right is given.

### Pushing

The pin shows one **⬆ <repo> (N)** button per repo with commits its upstream lacks. A tap, or `/push` in the topic, asks for confirmation. **✔ Push** runs `git push` (no force) in the container as its normal user, with the container's own git credentials, and the message turns into git's output with remote URLs hidden. A confirmation answered after the branch moved pushes nothing, so ask again. Pushing is refused while a session of that container runs or waits, so a half-done change is never pushed.

### Claude usage

The General topic's pinned message shows the plan limits (current session, current week, per-model week) of every account signed in in a running dev container: one block per account with the containers that use it, then the containers that are signed out or stopped. It is refreshed every 10 minutes and shortly after a prompt ends, at most once every 2 minutes; not when a prompt starts, since each refresh is a whole Claude Code start of its own that would land on the prompt's. The bot runs Claude Code's own `/usage` in one container per account, so nothing is billed and the bot never handles a token; `claude auth status`, which tells which account a container uses, reruns only when the container's sign-in files change (asked from `dockbench-agent claude-key`, which never reads the token).

### Claude's commands in the menu

The group's `/` menu lists the bot's commands, then the slash commands Claude has in your containers: skills, plugin skills and your own commands, read from Claude's first stream event. Telegram's menu has no sections, so they are grouped by order and description: `🧩 skill` first, then `⚙️ command`, then one `🔌 <plugin>` block per plugin. Telegram only allows `a-z 0-9 _`, so `claude-optimize` shows as `/claude_optimize` and `plugin:skill` as `/plugin_skill`, and the bot turns the name back into Claude's. Built-ins that only work in a terminal (`/clear`, `/config`, `/model`, …) are left out, and the menu is capped at Telegram's 100 entries.

### Sessions in VS Code

Every run uses the same user, home and working directory as the VS Code tunnel, so Telegram sessions are stored where the Claude Code extension looks. The container's link is `https://vscode.dev/tunnel/<PROJECT>/home/dev/workspace`. A Telegram reply to a session resumes it, including what you added from VS Code.

The extension loads a session when you open it and does not follow a run that is still going. Typing in a VS Code tab of a session while Telegram is running it would make both write to the same conversation. `/stop` only kills the bot's own run, never the Claude process behind a VS Code tab.

`/clean` deletes the requests, answers and leftover status messages of the topic's idle sessions. Sent as a reply to a session's message (`/clear` works too, since Claude's own `/clear` does nothing in a headless run), it deletes only that session, and refuses one that is running; a reply to a message the bot does not know cleans nothing, so a mistaken reply never empties the whole topic.

`/purge` empties the whole topic, which `/clean` cannot: the Bot API cannot list a topic's messages, so the bot deletes the topic itself (`deleteForumTopic`, which needs the Delete messages admin right) and creates a new one named after the bound `PROJECT` (`createForumTopic`, Manage topics). After the confirmation tap it stops the topic's sessions, drops its waiting pickers and gives the stopped runs up to 30 seconds to post their last reply. The new topic keeps the old one's binding and last pick; the old topic's sessions are forgotten. If the delete is refused nothing else changes; if the create fails, General says so and a topic created by hand with the `PROJECT` name binds itself.

### Limits and recovery

- Limits. A run that looks hung is killed: nothing written to its session and almost no CPU for 15 minutes. Busy runs (builds, tests, subagents) keep going, up to 4 hours or 180 turns. The session is kept, so a reply continues it.
- Gone sessions. If a session no longer exists in the container (for example after its volume was recreated), the bot starts a new one for that prompt by itself.
- Bot restarts. A run keeps going inside the dev container when the bot stops, so a restarted bot first stops the runs a previous instance left behind and replies to their requests. Queued follow-ups and pickers waiting for a tap are dropped. Update the bot when nothing is running.
- Telegram hiccups. Calls to Telegram are retried for about 30 seconds on network errors, 5xx and rate limits. Topic creation is never retried, so a topic is never created twice; if one did not show up, create it by hand with the project's name.
- Topics by hand. A topic you create yourself must be named exactly like the `PROJECT` (letters, digits, `_ . -`). A topic renamed before the bot joined, or still saying "unbound topic", needs `/bind <PROJECT>`.
- Not logged in or not installed. If Claude Code is not signed in in that container, or was not installed (`INSTALL_CLAUDE=0`), the reply says so and how to fix it.

## Security

The bot is a shell in every dev container, so it is built to be hard to talk to:

- One user, one group. Exactly one Telegram user id is accepted, in exactly one group. Everything else is dropped before any processing and never answered.
- Claiming needs the logs. Until an owner is set, the bot answers only `/setup <code>` in a private chat, with a random code printed in its log, so only someone who can read the server's logs can claim it.
- Nothing sensitive is logged: update ids, thread ids, container names, session ids and exit codes, never message text, captions, file contents or the token.
- No shell strings. The prompt is one element of the exec command array, after `--`. Topic names are checked against `^[A-Za-z0-9_.-]+$` before they become container names, and only containers with the label `dockbench=true` are ever used, so a topic called `telegram` or `dashboard` cannot reach the bot or the dashboard.
- Non-root, no published port. The container runs as uid 1000 with only the socket's group, listens only for `/status` on `devnet` (no auth, readable by every dev container there), and uses six Docker API calls: list containers, create and start an exec, inspect it, put and get an archive.
- The socket is root. It gives root-equivalent access to the host, and `:ro` would not change that. A socket proxy such as `tecnativa/docker-socket-proxy` buys little, because the `CONTAINERS=1`, `EXEC=1` and `POST=1` it needs also allow creating privileged containers. If you add one anyway, put it on a network that only the bot shares, **never on `devnet`**, set `DOCKER_HOST=tcp://<proxy>:2375` and drop the socket mount and `group_add`.

## Build it yourself

The published image is `linux/arm64`. For x86 servers, or to customize it:

```sh
docker buildx build --platform linux/amd64 -t dockbench-telegram:amd64 --load .
```

Then set `image:` in `compose.yml` to your tag. The [GitHub Actions workflow](.github/workflows/build.yml) runs the self-check and builds the arm64 image on every push to `main`.

## Contributing

You need Python 3.13 (`mise install` reads it from [`.mise.toml`](.mise.toml)). There are no dependencies.

```sh
python bot.py --check     # the self-check: parsing, formatting, pickers, pins, push, clean, outbox, usage
python bot.py             # runs the bot, with TELEGRAM_BOT_TOKEN and DOCKER_HOST set
```

The self-check is one function of assertions in `tgbot/check.py`; new logic gets its assertions there. CI runs it before building the image. Rules the code keeps: standard library only, never log message text or the token, never make the prompt a shell string, and pass the flags listed below on every Docker exec.

### Implementation notes

- `--add-dir /home/dev/.telegram/inbox`: the inbox is outside the working directory, and without it Claude's Read of the screenshot is denied in headless mode.
- Outbox: `--add-dir /home/dev/.telegram/outbox/<session>` lets a headless Claude write there, and `--append-system-prompt` tells it to put images there. The folder is per session, not per run, because Claude Code records the system prompt on a session's first request and reuses it on every resume, so a per-run path would be stale from the second prompt on. The bot empties the folder before each run and after sending, reads it with `GET /containers/{id}/archive` and uploads with a hand-built `multipart/form-data` body.
- `CLAUDE_CODE_ENTRYPOINT=claude-vscode` in the exec environment: `claude -p` otherwise records its sessions as `sdk-cli`, which the VS Code extension hides from its session list.
- Answers go out with `parse_mode: HTML`, because Telegram's HTML needs only `<`, `>` and `&` escaped, unlike MarkdownV2. Long answers are split before converting, and a ``` block cut by a split is closed and reopened, so every part is valid on its own; a part Telegram still refuses is resent as plain text.
- `--permission-prompts none` is always passed, so nothing can ever wait on a prompt.
- The run is wrapped in `timeout -k 30 14400 nice -n 10`, so the 4-hour limit is enforced inside the container and Claude and its tools run at a lower priority than the container's VS Code and other containers (a Claude Code start alone takes about 1.5 cores for 2 seconds); the `/usage` refresh runs under `nice -n 10` too. Every minute the bot probes each run for the CPU time of the wrapper and its descendants and the age of the newest write to the session's transcript; a run with no transcript write and under 5% of a core for 15 minutes is killed like `/stop`. `/stop` sends `pkill` to that wrapper only, never to an interactive Claude, another topic's run or the VS Code extension's process for the same session.
- On startup the bot stops leftover runs of a previous instance, since a Docker exec outlives its client.
- The probes (the pin's git facts, a run's activity, a session's title, the sign-in fingerprint) are answered by `dockbench-agent`, the DockBench image's native helper, which reads git in-process. The bot asks it over HTTP on the container's port 8080 (`devnet`) with a bearer token the agent writes at start to `/home/dev/.dockbench-agent-token`; the bot reads that file once per container through the Docker archive API and again after a 401, so no process starts in the container for a probe. There is no exec fallback: a dev container needs a DockBench image from 2026-11-09 or later. When HTTP gives no answer (an older image, no token file yet, a refused connection) the probe gives nothing that pass and is asked again on the next: the pin keeps its last text (the bot logs why, once), the watchdog skips that minute, a title stays unknown and the usage pass runs `claude auth status`. `/push` and its confirmation read the repo state from the same `pin-info` route, so the only process a push starts in the container is `git push`; when the agent gives no answer, `/push` says the container's image needs an update.
- The prompt is passed after `--`, so text starting with `-` is not parsed as a flag.
- A session is keyed by the message id of its first request, and the state file maps every request, answer and status message id to its session, so a reply anywhere in the chain finds it. The Bot API cannot look up a message, so this map is the only way.
- Pickers wait in memory only, with the request; a restart drops them, so the message text never lands on disk.
- A topic is bound automatically from the first message in it, which Telegram sends as a reply to the topic's creation message, so `/bind` is only a fallback.
