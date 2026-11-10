"""Docker Engine API over the unix socket (or tcp:// via DOCKER_HOST): dev containers, exec, archive upload."""
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

from .config import EXEC_ENV, WORKSPACE, log

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


def docker(method, path, body=None, timeout=60, stream=False):
    u = urllib.parse.urlparse(DOCKER_HOST)
    conn = UnixHTTPConnection(u.path, timeout) if u.scheme == "unix" else http.client.HTTPConnection(u.hostname, u.port or 2375, timeout=timeout)
    headers = {}
    if isinstance(body, dict):
        body, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
    elif body is not None:
        headers = {"Content-Type": "application/x-tar"}
    conn.request(method, path, body, headers)
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
        return resp  # the daemon hijacks the connection and closes it at the end, so the body is read until EOF
    data = resp.read()
    conn.close()
    return json.loads(data) if data else None


def dev_containers():
    """The label filter keeps non-dev containers like dockbench-telegram and dockbench-dashboard out."""
    filters = urllib.parse.quote(json.dumps({"label": ["dockbench=true"]}))
    containers = docker("GET", f"/containers/json?all=true&filters={filters}")
    NAMES.update({c["Id"]: c["Names"][0].lstrip("/") for c in containers if c.get("Names")})
    return containers


def project(c):
    """PROJECT of container dockbench-<PROJECT>, else None."""
    n = (c.get("Names") or [""])[0].lstrip("/")
    return n[len("dockbench-"):] if n.startswith("dockbench-") else None


def container(name):
    return next((c for c in dev_containers() if project(c) == name), None)


def exec_start(cid, cmd, timeout=60, env=EXEC_ENV):
    eid = docker("POST", f"/containers/{cid}/exec", {"User": "dev", "WorkingDir": WORKSPACE, "Env": env, "Cmd": cmd, "AttachStdin": False, "AttachStdout": True, "AttachStderr": True})["Id"]
    return eid, docker("POST", f"/exec/{eid}/start", {"Detach": False, "Tty": False}, timeout=timeout, stream=True)


def exec_exit(eid, wait=2):
    """Exit code once the exec is done. It can report Running for a moment after its stream closed, or much longer if the stream was cut."""
    end = time.monotonic() + wait
    while (info := docker("GET", f"/exec/{eid}/json")).get("Running") and time.monotonic() < end:
        time.sleep(0.25)
    return info.get("ExitCode")


def frames(resp):
    """Demultiplex a non-TTY exec stream: 8-byte header, byte 0 is the stream (1 stdout, 2 stderr), bytes 4-7 the big-endian size."""
    while len(head := resp.read(8)) == 8:
        yield head[0], resp.read(int.from_bytes(head[4:], "big"))


def exec_run(cid, cmd, timeout=60, env=EXEC_ENV):
    eid, resp = exec_start(cid, cmd, timeout, env)
    with resp:
        out = b"".join(data for _, data in frames(resp))
    return exec_exit(eid), out


# The image's dockbench-agent serves the probes and the run chores (a run's folders, stopping runs) on its port, behind a token it writes at start (the agent's TOKEN_FILE): a request on devnet starts nothing in the container. The bot reads the token once per container through the archive API (no exec) and again after a 401 (the agent restarted with a new one). Images from before these routes (2026-11-10) are not supported: their agent answers nothing to them.
AGENT_PORT = 8080
AGENT_TOKEN = "/home/dev/.dockbench-agent-token"
ROUTES = {"pin-info": ("GET", "/pin-info?dir={}"), "activity": ("GET", "/activity?session={}"), "title": ("GET", "/title?session={}"), "claude-key": ("GET", "/claude-key"), "account": ("GET", "/account"),
          "prepare": ("POST", "/prepare?session={}"), "outbox-clear": ("POST", "/outbox-clear?session={}"), "stop": ("POST", "/stop?session={}"), "stop-all": ("POST", "/stop-all")}
NAMES = {}  # container id -> name, from the last dev_containers() listing; the name resolves on devnet
TOKENS = {}  # container id -> its agent's token


def http(method, url, headers, timeout):
    """(status, body) of a request without a body; raises urllib.error.HTTPError on 4xx/5xx and OSError (URLError included) when nothing answers."""
    with urllib.request.urlopen(urllib.request.Request(url, data=b"" if method == "POST" else None, headers=headers, method=method), timeout=timeout) as r:
        return r.status, r.read()


def token(cid):
    """The agent's token in the container, None when there is none (an image before the probe routes, or the agent could not write it)."""
    try:
        files = get_files(cid, AGENT_TOKEN, 4096)
    except (DockerError, tarfile.TarError, OSError):
        return None
    tok = files[0][1].strip() if files else b""
    return tok.decode(errors="replace") if tok else None


def agent(cid, args, timeout=60):
    """(exit code, output) of the dockbench-agent route `args` (a ROUTES name, then its arguments) over HTTP, shaped like a subcommand's (204, the run is not there, is exit 3). None when it gives nothing this time: no name or token known for the container (an image from before the routes has no token file), an error status, or no answer; the next call asks again."""
    name = NAMES.get(cid)
    if not name:
        return None
    method, path = ROUTES[args[0]]
    path = path.format(*(urllib.parse.quote(a, safe="") for a in args[1:]))
    for retry in (True, False):
        tok = TOKENS.get(cid) or token(cid)
        if not tok:
            return None
        TOKENS[cid] = tok
        try:
            status, body = http(method, f"http://{name}:{AGENT_PORT}{path}", {"Authorization": f"Bearer {tok}"}, timeout)
            return (3 if status == 204 else 0), body
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                TOKENS.pop(cid, None)
                continue
            if e.code != 404:  # 404: an agent from before the routes, which its caller reports
                log.warning("%s: agent %s answered %s", name, path.split("?")[0], e.code)
            return None
        except OSError:  # refused, name not resolved, timeout
            return None
    return None


def get_files(cid, path, limit):
    """The regular files under path in the container, as (name inside path, bytes). Symlinks, links and devices are left out and a tar over limit bytes raises: the container is not trusted."""
    with docker("GET", f"/containers/{cid}/archive?path={urllib.parse.quote(path)}", stream=True) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise DockerError(f"{path} holds over {limit // 2**20} MB")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return [(m.name.partition("/")[2], tar.extractfile(m).read()) for m in tar.getmembers() if m.isfile()]


def tarball(files):
    """put-archive keeps the tar header owner, so files are written as dev (uid 1000)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size, info.uid, info.gid, info.mode, info.mtime = len(data), 1000, 1000, 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
