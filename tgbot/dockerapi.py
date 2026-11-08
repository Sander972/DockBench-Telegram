"""Docker Engine API over the unix socket (or tcp:// via DOCKER_HOST): dev containers, exec, archive upload."""
import http.client
import io
import json
import os
import socket
import tarfile
import time
import urllib.parse

from .config import EXEC_ENV, WORKSPACE

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
    return docker("GET", f"/containers/json?all=true&filters={filters}")


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


AGENT = "/usr/local/bin/dockbench-agent"
NO_AGENT = set()  # ids of containers whose image has no dockbench-agent (built before it): their probes take the older execs


def agent(cid, args, timeout=60):
    """(exit code, output) of `dockbench-agent <args>` in the container, one native process where the older probes ran a `python3 -c` script or several git execs. None when the image has no agent: the caller then takes its older execs. Remembered per container id, so an old container costs one failed exec, not one per probe (a recreated container gets a new id and is asked again)."""
    if cid in NO_AGENT:
        return None
    code, out = exec_run(cid, [AGENT, *args], timeout)
    if code in (126, 127) or b"exec failed" in out:  # Docker's answer to a missing binary: 127 and "OCI runtime exec failed: ..." in the stream
        NO_AGENT.add(cid)
        return None
    return code, out


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
