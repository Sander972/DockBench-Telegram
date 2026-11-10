"""Docker Engine API over the unix socket (or tcp:// via DOCKER_HOST), read only: the dev containers (followed through the events stream) and files read from them. Everything the bot does in a container goes to its dockbench-agent over HTTP: the probes, the run chores, uploads and the processes it starts."""
import http.client
import io
import json
import os
import socket
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import EXEC_ENV, log

DOCKER_HOST = os.environ.get("DOCKER_HOST") or "unix:///var/run/docker.sock"


class DockerError(Exception):
    pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def docker(method, path, timeout=60, stream=False):
    u = urllib.parse.urlparse(DOCKER_HOST)
    conn = UnixHTTPConnection(u.path, timeout) if u.scheme == "unix" else http.client.HTTPConnection(u.hostname, u.port or 2375, timeout=timeout)
    conn.request(method, path)
    resp = conn.getresponse()
    if resp.status >= 400:
        data = resp.read()
        conn.close()
        try:
            msg = json.loads(data)["message"]
        except (ValueError, KeyError, TypeError):
            msg = data[:200].decode(errors="replace")
        raise DockerError(f"{method} {path.split('?')[0]}: {resp.status} {msg}")
    if stream:
        return resp
    data = resp.read()
    conn.close()
    return json.loads(data) if data else None


CACHE = None  # the last list_containers() listing
LIVE = False  # an events stream is open, so every container change lists again and CACHE is current
# what changes the listing; not exec_*, so the bot's own execs make no event
EVENTS = {"type": ["container"], "label": ["dockbench=true"], "event": ["create", "start", "die", "destroy", "rename", "pause", "unpause"]}


def list_containers():
    """The label filter keeps non-dev containers like dockbench-telegram and dockbench-dashboard out."""
    global CACHE
    filters = urllib.parse.quote(json.dumps({"label": ["dockbench=true"]}))
    containers = docker("GET", f"/containers/json?all=true&filters={filters}")
    NAMES.update({c["Id"]: c["Names"][0].lstrip("/") for c in containers if c.get("Names")})
    CACHE = containers
    return containers


def dev_containers():
    """The dev containers: the cached listing while the events stream keeps it current, else a new one. Callers only read it."""
    return CACHE if LIVE else list_containers()


def watch_once(on_change):
    """One events stream's lifetime: list on connect and after every event, calling on_change() each time; returns when the stream ends, raises when it fails (a socket timeout after 10 quiet minutes, a proxy's cut, the daemon going away). The stream is opened before the first listing, so no change between the two is lost."""
    global LIVE
    try:
        with docker("GET", f"/events?filters={urllib.parse.quote(json.dumps(EVENTS))}", timeout=600, stream=True) as resp:
            list_containers()
            LIVE = True
            on_change()
            for line in resp:
                if line.strip():
                    list_containers()
                    on_change()
    finally:
        LIVE = False


def watch(on_change):
    """Own thread: watch_once() for ever, 5 s between streams, a failure logged once per distinct message; dev_containers() lists directly meanwhile."""
    last = None
    while True:
        try:
            watch_once(on_change)
            err = "stream ended"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if err != last:
            log.warning("docker events: %s, listing directly until it is back", err)
        last = err
        time.sleep(5)


def project(c):
    """PROJECT of container dockbench-<PROJECT>, else None."""
    n = (c.get("Names") or [""])[0].lstrip("/")
    return n[len("dockbench-"):] if n.startswith("dockbench-") else None


def container(name):
    return next((c for c in dev_containers() if project(c) == name), None)


# The image's dockbench-agent serves the probes, the run chores (a run's folders, stopping runs), uploads and process starts on its port, behind a token it writes at start (the agent's TOKEN_FILE): a request on devnet starts nothing in the container. The bot reads the token once per container through the archive API (no exec) and again after a 401 (the agent restarted with a new one). Images from before these routes (2026-11-10) are not supported: their agent answers nothing to them.
AGENT_PORT = 8080
AGENT_TOKEN = "/home/dev/.dockbench-agent-token"
ROUTES = {"pin-info": ("GET", "/pin-info?dir={}"), "activity": ("GET", "/activity?session={}"), "title": ("GET", "/title?session={}"), "claude-key": ("GET", "/claude-key"), "account": ("GET", "/account"),
          "prepare": ("POST", "/prepare?session={}"), "outbox-clear": ("POST", "/outbox-clear?session={}"), "stop": ("POST", "/stop?session={}"), "stop-all": ("POST", "/stop-all"),
          "inbox": ("POST", "/inbox?name={}"), "exec": ("POST", "/exec")}
NAMES = {}  # container id -> name, from the last dev_containers() listing; the name resolves on devnet
TOKENS = {}  # container id -> its agent's token


def http(method, url, headers, timeout, body=None, stream=False):
    """(status, body) of a request, or the open response when stream; raises urllib.error.HTTPError on 4xx/5xx and OSError (URLError included) when nothing answers."""
    r = urllib.request.urlopen(urllib.request.Request(url, data=body if body is not None else b"" if method == "POST" else None, headers=headers, method=method), timeout=timeout)
    if stream:
        return r
    with r:
        return r.status, r.read()


class AgentError(DockerError):
    """The agent gave no usable answer; code is its HTTP status, None when it gave none."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def token(cid):
    """The agent's token in the container, None when there is none (an image before the probe routes, or the agent could not write it)."""
    try:
        files = get_files(cid, AGENT_TOKEN, 4096)
    except (DockerError, tarfile.TarError, OSError):
        return None
    tok = files[0][1].strip() if files else b""
    return tok.decode(errors="replace") if tok else None


def call(cid, args, timeout=60, body=None, stream=False):
    """dockbench-agent route `args` (a ROUTES name, then its arguments) of the container over HTTP: (status, body), or the open response when stream. The token is read again once after a 401 (the agent restarted). Raises AgentError when there is no name or token for the container (an image from before the routes has no token file), on an error status, or when nothing answers."""
    name = NAMES.get(cid)
    if not name:
        raise AgentError(f"container {cid[:12]} not listed yet")
    method, path = ROUTES[args[0]]
    path = path.format(*(urllib.parse.quote(a, safe="") for a in args[1:]))
    route = path.split("?")[0]
    for retry in (True, False):
        tok = TOKENS.get(cid) or token(cid)
        if not tok:
            raise AgentError(f"{name}: no dockbench-agent token")
        TOKENS[cid] = tok
        try:
            return http(method, f"http://{name}:{AGENT_PORT}{path}", {"Authorization": f"Bearer {tok}"}, timeout, body, stream)
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                TOKENS.pop(cid, None)
                continue
            raise AgentError(f"{name}: dockbench-agent {route} answered {e.code}", e.code) from None
        except OSError as e:  # refused, name not resolved, timeout
            raise AgentError(f"{name}: dockbench-agent {route}: {type(e).__name__}") from None
    raise AgentError(f"{name}: dockbench-agent {route} answered 401", 401)


def agent(cid, args, timeout=60):
    """(exit code, output) of the agent route `args`, shaped like a subcommand's (204, the run is not there, is exit 3). None when it gives nothing this time (see call()); the next call asks again."""
    try:
        status, body = call(cid, args, timeout)
    except AgentError as e:
        if e.code not in (None, 404):  # 404: an agent from before the route, which its caller reports
            log.warning("%s", e)
        return None
    return (3 if status == 204 else 0), body


def frames(resp):
    """Demultiplex an exec stream: 8-byte header, byte 0 is the stream (1 stdout, 2 stderr, 3 the agent's exit code frame), bytes 4-7 the big-endian size."""
    while len(head := resp.read(8)) == 8:
        yield head[0], resp.read(int.from_bytes(head[4:], "big"))


def exec_frames(cid, cmd, timeout=60, env=EXEC_ENV):
    """The frames of argv cmd run by the container's agent as dev in the workspace (POST /exec), the last one (3, b"<exit code>"). Raises AgentError before the first frame when the agent cannot be asked; a stream lost later raises OSError or http.client.HTTPException. timeout is per read: a run may be silent for long."""
    with call(cid, ["exec"], timeout, json.dumps({"cmd": cmd, "env": env}).encode(), stream=True) as resp:
        yield from frames(resp)


def exec_run(cid, cmd, timeout=60, env=EXEC_ENV):
    """(exit code, stdout and stderr joined) of argv cmd in the container; the code is None when the stream ended without one."""
    code, out = None, []
    for kind, data in exec_frames(cid, cmd, timeout, env):
        if kind == 3:
            code = int(data) if data.isdigit() else None
        else:
            out.append(data)
    return code, b"".join(out)


def put_inbox(cid, name, data):
    """A prompt's attachment into the container's inbox (POST /inbox); raises AgentError unless the agent took it."""
    status, _ = call(cid, ["inbox", name], 60, data)
    if status != 200:
        raise AgentError(f"inbox upload answered {status}", status)


def get_files(cid, path, limit):
    """The regular files under path in the container, as (name inside path, bytes). Symlinks, links and devices are left out and a tar over limit bytes raises: the container is not trusted."""
    with docker("GET", f"/containers/{cid}/archive?path={urllib.parse.quote(path)}", stream=True) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise DockerError(f"{path} holds over {limit // 2**20} MB")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return [(m.name.partition("/")[2], tar.extractfile(m).read()) for m in tar.getmembers() if m.isfile()]
