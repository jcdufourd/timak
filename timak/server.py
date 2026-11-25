"""Local web interface: `timak serve`.

A small HTTP server on 127.0.0.1 serves one page (web/index.html) and a JSON
API. Long commands (scan, extract, verify, dedupe, compare, du) run as `python -m timak`
subprocesses, one at a time, and the page polls their output. The server runs
with the privileges it was started with (sudo for APFS backups), so:

  - it only listens on 127.0.0.1 and rejects other Host headers (DNS rebinding);
  - every API call needs a random token, sent in a custom header that other
    web sites cannot set (it travels in the page's URL fragment, never to the server);
  - commands are built from a fixed set of options, never from a shell string.
"""

from __future__ import annotations

import hmac
import json
import errno
import os
import pwd
import secrets
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlparse

from . import __version__
from .archive import read_info
from .archiveimage import find_bundle, open_archive
from .diskimage import DiskImageError
from .diskimage import is_image
from .extract import ExtractError, find_rsync
from .rules import Rules, RulesError, default_rules_text
from .sources.base import STAMP_RE
from .util import PARTIAL_SUFFIX, has_full_disk_access

MAX_BODY = 4 << 20
MAX_LINES = 50_000
LIST_TIMEOUT = 1800  # attaching images and mounting many APFS snapshots takes a while
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


class BadRequest(ValueError):
    pass


# --- paths and options -------------------------------------------------------


def user_home() -> Path:
    """Home of the person using the page, also when the server runs under sudo."""
    name = os.environ.get("SUDO_USER")
    if name and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(name).pw_dir)
        except KeyError:
            pass
    return Path.home()


def expand(path, what: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise BadRequest(f"{what}: no path given")
    p = path.strip()
    if p == "~" or p.startswith("~/"):
        p = str(user_home()) + p[1:]
    if not p.startswith("/"):
        raise BadRequest(f"{what}: use a full path starting with / (got {path!r})")
    return p


def _text(opts: dict, key: str) -> str:
    v = opts.get(key)
    if v is None:
        return ""
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        raise BadRequest(f"{key}: expected text")
    return str(v).strip()


def _items(opts: dict, key: str) -> list[str]:
    v = opts.get(key) or []
    if isinstance(v, str):
        v = v.splitlines()
    if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
        raise BadRequest(f"{key}: expected a list of text")
    return [i.strip() for i in v if i.strip()]


def source_argv(opts: dict) -> list[str]:
    """Command-line arguments shared by list, scan, extract and verify."""
    sources = _items(opts, "sources")
    if not sources:
        raise BadRequest("add at least one backup source")
    argv = [expand(s, "source") for s in sources]
    if rules := _text(opts, "rules"):
        argv.append("--rules=" + expand(rules, "rules file"))
    for key in ("machine", "since", "until", "stamp"):
        if v := _text(opts, key):
            argv.append(f"--{key}={v}")  # "=" form: a value starting with "-" stays a value
    if v := _text(opts, "last"):
        if not v.isdigit() or int(v) == 0:
            raise BadRequest("last: expected a positive number")
        argv.append(f"--last={v}")
    for v in _items(opts, "volumes"):
        argv.append(f"--volume={v}")
    for key, flag in (("rename_volumes", "--rename-volume"), ("rename_machines", "--rename-machine")):
        for v in _items(opts, key):
            if "=" not in v:
                raise BadRequest(f"{flag} expects OLD=NEW, got {v!r}")
            argv.append(f"{flag}={v}")
    if opts.get("include_in_progress"):
        argv.append("--include-in-progress")
    return argv


def command_argv(command: str, opts: dict) -> list[str]:
    archive = lambda: expand(_text(opts, "archive"), "archive folder")
    if command == "scan":
        return ["scan", *source_argv(opts)] + (["--kept-only"] if opts.get("kept_only") else [])
    if command == "extract":
        return ["extract", *source_argv(opts), "--archive=" + archive()]
    if command == "verify":
        return ["verify", *source_argv(opts), "--archive=" + archive()] + (["--content"] if opts.get("content") else [])
    if command == "dedupe":
        flags = (["--dry-run"] if opts.get("dry_run") else []) + (["--loose"] if opts.get("loose") else [])
        return ["dedupe", archive(), *flags]
    if command == "prune":  # a dry run unless asked: the page confirms first
        rules = _text(opts, "rules")
        if not rules:
            raise BadRequest("prune needs your rules file: it removes what that file now drops")
        return ["prune", archive(), "--rules=" + expand(rules, "rules file")] + (["--apply"] if opts.get("apply") else [])
    if command == "biggest":
        rules = _text(opts, "rules")  # optional: its home_volumes shape the suggested rules
        return ["biggest", archive()] + (["--rules=" + expand(rules, "rules file")] if rules else [])
    if command == "kinds":
        return ["kinds", archive()]
    if command == "compare":
        other = expand(_text(opts, "other_archive"), "other archive")
        return ["compare", archive(), other] + (["--loose"] if opts.get("loose") else [])
    if command == "du":
        return ["du", archive()]
    raise BadRequest(f"unknown command {command!r}")


def _child_env() -> dict:
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [PACKAGE_PARENT, env.get("PYTHONPATH")]))
    return env


def timak_cmd(argv: list[str]) -> list[str]:
    return [sys.executable, "-m", "timak", *argv]


# --- jobs ----------------------------------------------------------------------


class Job:
    """One timak command running in the background, with its output.

    With `echo`, the output is also written there as it comes (the server log, under
    the Mac app): it can then be read even when no page can reach the server any more.
    """

    def __init__(self, argv: list[str], echo: TextIO | None = None):
        self.argv = argv
        self.echo = echo
        self.started = time.time()
        self.ended: float | None = None
        self.returncode: int | None = None
        self.stopping = False
        self.lines: list[str] = []
        self.offset = 0  # number of lines dropped from the front
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            timak_cmd(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", env=_child_env(),
            start_new_session=True,  # its own process group, so Stop reaches rsync too
        )  # fmt: skip
        self._say(f"started: timak {' '.join(argv)} (pid {self.proc.pid})")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _say(self, text: str) -> None:
        if self.echo is None:
            return
        try:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.argv[0]}: {text}", file=self.echo, flush=True)
        except (OSError, ValueError):
            self.echo = None  # the log went away: keep the output for the page only

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._say(line)
            with self.lock:
                self.lines.append(line)
                if len(self.lines) > MAX_LINES:
                    drop = len(self.lines) - MAX_LINES + MAX_LINES // 5
                    del self.lines[:drop]
                    self.offset += drop
        self.proc.stdout.close()
        self.returncode = self.proc.wait()
        self.ended = time.time()
        self._say(f"finished: {self.status} (exit {self.returncode})")

    @property
    def running(self) -> bool:
        return self.returncode is None

    @property
    def status(self) -> str:
        if self.running:
            return "stopping" if self.stopping else "running"
        if self.stopping or self.returncode in (130, -signal.SIGINT):
            return "stopped"
        return {0: "done", 1: "problems"}.get(self.returncode, "failed")

    def stop(self) -> None:
        """Like Ctrl-C in a terminal: timak stops cleanly and can resume later."""
        if self.running:
            self.stopping = True
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def wait(self, timeout: float | None = None) -> None:
        self.reader.join(timeout)

    def view(self, since: int = 0) -> dict:
        with self.lock:
            start = max(since - self.offset, 0)
            lines = self.lines[start:]
            nxt = self.offset + len(self.lines)
        return {
            "command": self.argv[0], "argv": ["timak", *self.argv], "status": self.status,
            "returncode": self.returncode, "started": self.started, "ended": self.ended,
            "lines": lines, "first": max(since, self.offset), "next": nxt,
        }  # fmt: skip


# --- read-only views -------------------------------------------------------------


def list_snapshots(opts: dict) -> dict:
    argv = ["list", "--json", *source_argv(opts)]
    try:
        r = subprocess.run(timak_cmd(argv), capture_output=True, encoding="utf-8", errors="replace",
                           env=_child_env(), timeout=LIST_TIMEOUT)  # fmt: skip
    except subprocess.TimeoutExpired:
        raise BadRequest("listing the backups took too long")
    log = [l for l in r.stderr.splitlines() if l.strip()]
    if r.returncode != 0:
        raise BadRequest("\n".join(l.removeprefix("timak: ") for l in log[-5:]) or f"timak list failed ({r.returncode})")
    data = json.loads(r.stdout)
    data["log"] = [l for l in log if l not in data["warnings"]]
    data["argv"] = ["timak", *argv]
    return data


def archive_summary(path: str) -> dict:
    """The snapshots of an archive, mounting its sparse bundle (read-only) for a moment if it has one."""
    given = Path(expand(path, "archive folder"))
    bundle = find_bundle(given)
    with open_archive(given, readonly=True) as root:
        summary = _archive_summary(root)
    summary["path"] = str(given)
    if bundle:
        summary["image"] = str(bundle[0])
    return summary


def _archive_summary(root: Path) -> dict:
    if not root.is_dir():
        return {"path": str(root), "exists": False, "machines": []}
    machines = []
    for mdir in sorted(root.iterdir()):
        if mdir.name.startswith(".") or not mdir.is_dir():
            continue
        snaps = []
        for p in sorted(mdir.iterdir()):
            partial = p.name.endswith(PARTIAL_SUFFIX)
            stamp = p.name.removesuffix(PARTIAL_SUFFIX)
            if not STAMP_RE.fullmatch(stamp) or not p.is_dir():
                continue
            info = {} if partial else read_info(p)
            snaps.append({
                "stamp": stamp, "partial": partial, "kind": info.get("kind"), "source": info.get("source"),
                "previous": info.get("previous"), "volumes": list(info.get("volumes", {})),
                "stats": info.get("stats"), "problems": info.get("problems"),
            })  # fmt: skip
        if snaps:
            machines.append({"name": mdir.name, "snapshots": snaps})
    return {"path": str(root), "exists": True, "machines": machines}


def browse(path: str | None) -> dict:
    p = Path(expand(path, "folder")) if path else user_home()
    while not p.is_dir() and p != p.parent:
        p = p.parent
    try:
        names = sorted(os.listdir(p), key=str.lower)
    except OSError as e:
        raise BadRequest(f"{p}: {e.strerror}")
    entries = []
    for name in names:
        if name.startswith("."):
            continue
        q = p / name
        try:
            is_dir = q.is_dir()
        except OSError:
            continue
        image = is_image(q)  # a .sparsebundle is a folder, but pick it as one image
        if is_dir or image or name.endswith(".toml"):
            entries.append({"name": name, "path": str(q), "dir": is_dir and not image, "image": image})
    places = [{"name": "Home", "path": str(user_home())}, {"name": "Volumes", "path": "/Volumes"},
              {"name": "Current folder", "path": os.getcwd()}]  # fmt: skip
    return {"path": str(p), "parent": str(p.parent) if p != p.parent else None, "entries": entries, "places": places}


def check_rules(text) -> dict:
    if not isinstance(text, str):
        raise BadRequest("rules: expected text")
    try:
        rules = Rules.from_text(text)
    except (RulesError, ValueError) as e:
        return {"ok": False, "error": str(e).removeprefix("rules: ")}
    return {"ok": True, "rules": len(rules.rules), "roots": rules.roots, "volume_roots": rules.volume_roots}


def read_rules(path: str | None) -> dict:
    if not path:
        return {"path": None, "exists": False, "text": default_rules_text()}
    p = Path(expand(path, "rules file"))
    if not p.exists():
        return {"path": str(p), "exists": False, "text": default_rules_text()}
    try:
        return {"path": str(p), "exists": True, "text": p.read_text()}
    except (OSError, UnicodeDecodeError) as e:
        raise BadRequest(f"{p}: {e}")


def save_rules(path, text) -> dict:
    p = Path(expand(path, "rules file"))
    if p.suffix != ".toml":
        raise BadRequest("the rules file name must end with .toml")
    check = check_rules(text)
    if not check["ok"]:
        raise BadRequest(f"not saved: {check['error']}")
    created = not p.exists()
    try:
        p.write_text(text)
        if created and os.geteuid() == 0 and "SUDO_UID" in os.environ:  # leave it to the person, not root
            os.chown(p, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", -1)))
    except OSError as e:
        raise BadRequest(f"{p}: {e.strerror}")
    return {"path": str(p), **check}


# --- HTTP ----------------------------------------------------------------------------


class App:
    def __init__(self, token: str):
        self.token = token
        self.job: Job | None = None
        self.lock = threading.Lock()
        self.server: Server | None = None
        self.owner_pid: int | None = None  # the Mac app that started us, if any
        self.echo: TextIO | None = None  # where jobs also write their output

    def quit(self, stop_job: bool) -> dict:
        if self.job and self.job.running and not stop_job:
            raise BadRequest(f"`{self.job.argv[0]}` is still running")
        threading.Thread(target=self._quit, daemon=True).start()
        return {"quitting": True}

    def _quit(self) -> None:
        time.sleep(0.2)  # let the response go out first
        self.server.shutdown()  # serve() then stops the job, if any, and waits for it

    def watch_owner(self) -> None:
        """Exit once the app that started us is gone and no command is running."""
        while True:
            time.sleep(2)
            if self.owner_pid is None or _alive(self.owner_pid) or (self.job and self.job.running):
                continue
            print("the timak app is gone and nothing is running: stopping", file=sys.stderr, flush=True)
            self.server.shutdown()
            return

    def state(self) -> dict:
        try:
            rsync = find_rsync()
        except ExtractError:
            rsync = None
        suggested = Path.cwd() / "my-rules.toml"
        return {
            "version": __version__, "root": os.geteuid() == 0, "fda": has_full_disk_access(), "rsync": rsync,
            "cwd": os.getcwd(), "home": str(user_home()),
            "suggested_rules": str(suggested) if suggested.is_file() else None,
            "job": self.job.view(10**12) if self.job else None,
        }  # fmt: skip

    def run(self, command: str, opts: dict) -> dict:
        argv = command_argv(command, opts)
        with self.lock:
            if self.job and self.job.running:
                raise BadRequest(f"`{self.job.argv[0]}` is still running; stop it or wait for it to finish")
            self.job = Job(argv, self.echo)
            return self.job.view()

    def shutdown(self, timeout: float = 120) -> None:
        if self.job and self.job.running:
            print("stopping the running command (it unmounts what it mounted)…", file=sys.stderr)
            self.job.stop()
            self.job.wait(timeout)


class Handler(BaseHTTPRequestHandler):
    server_version = "timak"

    @property
    def app(self) -> App:
        return self.server.app

    def log_message(self, format, *args) -> None:  # quiet: the terminal shows only the address
        pass

    # responses

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    # checks

    def _host_ok(self) -> bool:
        port = self.server.server_port
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Timak-Token", ""), self.app.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise BadRequest("request too large")
        if self.headers.get_content_type() != "application/json":
            raise BadRequest("expected JSON")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise BadRequest("expected a JSON object")
        return data

    # routing

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
        url = urlparse(self.path)
        if method == "GET" and url.path in ("/", "/index.html"):
            page = resources.files("timak").joinpath("web/index.html").read_bytes()
            csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                   "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")  # fmt: skip
            return self._send(200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if method == "GET" and url.path == "/help.html":  # static, no token: the Mac app opens it in its own window
            page = resources.files("timak").joinpath("web/help.html").read_bytes()
            csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                   "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")  # fmt: skip
            return self._send(200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if method == "GET" and url.path == "/icon.svg":
            icon = resources.files("timak").joinpath("web/icon.svg").read_bytes()
            return self._send(200, icon, "image/svg+xml", {"Content-Security-Policy": "default-src 'none'"})
        if not url.path.startswith("/api/"):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not self._token_ok():
            return self._error(HTTPStatus.FORBIDDEN, "missing or wrong token: open the address printed by `timak serve`")
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            handler = ROUTES.get((method, url.path))
            if handler is None:
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            body = self._body() if method == "POST" else {}
            return self._json(handler(self.app, query, body))
        except (BadRequest, DiskImageError) as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        except (ValueError, json.JSONDecodeError) as e:
            return self._error(HTTPStatus.BAD_REQUEST, f"bad request: {e}")
        except OSError as e:
            where = f"{e.filename}: " if e.filename else ""
            hint = " (Full Disk Access missing?)" if e.errno == errno.EPERM else ""
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{where}{e.strerror or e}{hint}")


def _job(app: App, q: dict, body: dict) -> dict:
    if app.job is None:
        return {"status": "none"}
    try:
        since = int(q.get("since", 0))
    except ValueError:
        raise BadRequest("since: expected a number")
    return app.job.view(since)


def _stop(app: App, q: dict, body: dict) -> dict:
    if app.job:
        app.job.stop()
    return _job(app, {"since": 10**12}, body)


ROUTES = {
    ("GET", "/api/state"): lambda app, q, b: app.state(),
    ("GET", "/api/job"): _job,
    ("GET", "/api/archive"): lambda app, q, b: archive_summary(q.get("path", "")),
    ("GET", "/api/browse"): lambda app, q, b: browse(q.get("path")),
    ("GET", "/api/rules"): lambda app, q, b: read_rules(q.get("path")),
    ("POST", "/api/list"): lambda app, q, b: list_snapshots(b.get("options") or {}),
    ("POST", "/api/run"): lambda app, q, b: app.run(str(b.get("command")), b.get("options") or {}),
    ("POST", "/api/stop"): _stop,
    ("POST", "/api/rules/check"): lambda app, q, b: check_rules(b.get("text")),
    ("POST", "/api/rules/save"): lambda app, q, b: save_rules(b.get("path"), b.get("text")),
    ("POST", "/api/quit"): lambda app, q, b: app.quit(bool(b.get("stop_job"))),
    ("POST", "/api/adopt"): lambda app, q, b: _adopt(app, q, b),
}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, token: str):
        super().__init__(("127.0.0.1", port), Handler)
        self.app = App(token)
        self.app.server = self


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _adopt(app: App, q: dict, body: dict) -> dict:
    pid = body.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise BadRequest("pid: expected a process id")
    app.owner_pid = pid
    return {"owner_pid": pid}


def _open_browser(url: str) -> None:
    cmd = ["open", url]
    user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and user:
        cmd = ["sudo", "-u", user, "open", url]  # the person's browser, not one running as root
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def serve(port: int = 8765, open_browser: bool = True, token_file: Path | None = None,
          owner_pid: int | None = None) -> int:  # fmt: skip
    token = secrets.token_urlsafe(18)
    if token_file:  # chosen by the Mac app, which keeps it private (mode 600)
        try:
            token = token_file.read_text().strip()
        except OSError as e:
            print(f"timak: {token_file}: {e.strerror}", file=sys.stderr)
            return 2
        if len(token) < 16:
            print(f"timak: {token_file}: token too short", file=sys.stderr)
            return 2
    try:
        server = Server(port, token)
    except OSError as e:
        print(f"timak: cannot listen on 127.0.0.1:{port}: {e.strerror}; try --port", file=sys.stderr)
        return 2
    server.app.echo = sys.stdout  # the server log, under the Mac app
    if owner_pid:
        server.app.owner_pid = owner_pid
        threading.Thread(target=server.app.watch_owner, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/" + ("" if token_file else f"#token={token}")
    print(f"timak web interface: {url}", flush=True)
    if not owner_pid:
        print("Keep this terminal open; press Ctrl-C to stop.", flush=True)
    if os.geteuid() != 0 and not owner_pid:
        print("note: not running as root; APFS backups need `sudo /opt/homebrew/bin/python3 -m timak serve`",
              file=sys.stderr)  # fmt: skip
    if open_browser:
        _open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.shutdown()
        server.server_close()
    return 0
