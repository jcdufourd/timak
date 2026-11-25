import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from timak.server import BadRequest, Server, command_argv, source_argv

from test_folder import RULES, build_copy
from test_pipeline import HAVE_RSYNC, chmod_writable

TOKEN = "test-token"


class JobEchoTest(unittest.TestCase):
    def test_output_is_also_written_to_the_log(self):
        import io

        from timak.server import Job

        log = io.StringIO()
        job = Job(["--version"], log)
        job.wait(30)
        lines = log.getvalue().splitlines()
        self.assertIn("started: timak --version", lines[0])
        self.assertTrue(any(job.lines[0] in l for l in lines[1:-1]))
        self.assertIn("finished: done (exit 0)", lines[-1])

    def test_a_closed_log_does_not_stop_the_job(self):
        import io

        from timak.server import Job

        log = io.StringIO()
        log.close()
        job = Job(["--version"], log)
        job.wait(30)
        self.assertEqual(job.status, "done")
        self.assertTrue(job.lines)


class ArgvTest(unittest.TestCase):
    def test_options_become_arguments(self):
        argv = source_argv({
            "sources": ["/Volumes/a.dmg"], "rules": "/r.toml", "since": "2015-01-01", "last": "3",
            "volumes": ["Large"], "rename_volumes": "Data=System\n", "rename_machines": ["Old=New"],
            "include_in_progress": True,
        })  # fmt: skip
        self.assertEqual(argv, ["/Volumes/a.dmg", "--rules=/r.toml", "--since=2015-01-01", "--last=3", "--volume=Large",
                                "--rename-volume=Data=System", "--rename-machine=Old=New", "--include-in-progress"])  # fmt: skip
        self.assertEqual(command_argv("du", {"archive": "/x"}), ["du", "/x"])
        self.assertEqual(command_argv("dedupe", {"archive": "/x", "dry_run": True}), ["dedupe", "/x", "--dry-run"])
        self.assertEqual(command_argv("compare", {"archive": "/x", "other_archive": "/y", "loose": True}),
                         ["compare", "/x", "/y", "--loose"])  # fmt: skip
        self.assertEqual(command_argv("prune", {"archive": "/x", "rules": "/r.toml"}), ["prune", "/x", "--rules=/r.toml"])
        self.assertEqual(command_argv("prune", {"archive": "/x", "rules": "/r.toml", "apply": True}),
                         ["prune", "/x", "--rules=/r.toml", "--apply"])  # fmt: skip
        self.assertEqual(command_argv("biggest", {"archive": "/x"}), ["biggest", "/x"])
        self.assertEqual(command_argv("kinds", {"archive": "/x"}), ["kinds", "/x"])
        self.assertEqual(command_argv("biggest", {"archive": "/x", "rules": "/r.toml"}), ["biggest", "/x", "--rules=/r.toml"])
        with self.assertRaisesRegex(BadRequest, "rules file"):
            command_argv("prune", {"archive": "/x"})

    def test_rejects_bad_input(self):
        for opts in ({"sources": []}, {"sources": ["relative.dmg"]}, {"sources": ["/a"], "last": "x"},
                     {"sources": ["/a"], "rename_volumes": ["noequals"]}, {"sources": "/a", "machine": ["x"]}):  # fmt: skip
            with self.assertRaises(BadRequest, msg=opts):
                source_argv(opts)
        with self.assertRaises(BadRequest):
            command_argv("rm", {"sources": ["/a"]})
        with self.assertRaises(BadRequest):
            command_argv("extract", {"sources": ["/a"]})  # no archive
        with self.assertRaises(BadRequest):
            command_argv("compare", {"archive": "/x"})  # no other archive


@unittest.skipUnless(HAVE_RSYNC, "rsync 3.x not installed")
class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.rules = self.tmp / "rules.toml"
        self.rules.write_text(RULES)
        self.first, self.second = self.tmp / "Backup20241029", self.tmp / "Backup20241108"
        build_copy(self.first, edited=False)
        build_copy(self.second, edited=True)
        self.server = Server(0, TOKEN)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.app.shutdown(30)
        self.server.shutdown()
        self.server.server_close()
        chmod_writable(self.tmp)
        shutil.rmtree(self.tmp)

    def call(self, path, body=None, token=TOKEN, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Timak-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
            conn.request("POST", path, json.dumps(body), headers)
        else:
            conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type") == "application/json" else data)

    def options(self, **extra):
        return {"sources": [str(self.first), str(self.second)], "rules": str(self.rules),
                "archive": str(self.tmp / "archive"), "rename_volumes": ["Data=System"], **extra}  # fmt: skip

    def wait(self):
        for _ in range(600):
            _, j = self.call("/api/job?since=0")
            if j["status"] not in ("running", "stopping"):
                return j
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_access_checks(self):
        status, page = self.call("/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>timak</title>", page)
        self.assertEqual(self.call("/api/state", token=None)[0], 403)
        self.assertEqual(self.call("/api/state", token="wrong")[0], 403)
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)  # DNS rebinding
        self.assertEqual(self.call("/", host="evil.example:80")[0], 403)
        status, icon = self.call("/icon.svg", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"<svg", icon)
        status, page = self.call("/help.html", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Timak Help</title>", page)
        status, state = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertIsNotNone(state["rsync"])

    def test_list_extract_and_archive(self):
        status, data = self.call("/api/list", {"options": self.options()})
        self.assertEqual(status, 200, data)
        self.assertEqual([s["stamp"] for s in data["snapshots"]], ["2024-10-29-000000", "2024-11-08-000000"])
        vols = {v["original_name"]: v for v in data["snapshots"][0]["volumes"]}
        self.assertEqual(vols["Data"]["name"], "System")
        self.assertTrue(vols["Large"]["selected"])

        status, j = self.call("/api/run", {"command": "extract", "options": self.options()})
        self.assertEqual(status, 200, j)
        self.assertEqual(j["argv"][:2], ["timak", "extract"])
        j = self.wait()
        self.assertEqual(j["status"], "done", "\n".join(j["lines"]))
        self.assertTrue(any(l.startswith("[2/2]") for l in j["lines"]))

        _, a = self.call("/api/archive?path=" + str(self.tmp / "archive"))
        (machine,) = a["machines"]
        self.assertEqual(machine["name"], "OldMac")
        second = machine["snapshots"][1]
        self.assertEqual(second["previous"], "2024-10-29-000000")
        self.assertGreater(second["stats"]["linked"], 0)

        status, data = self.call("/api/list", {"options": {"sources": [str(self.tmp / "nothing")]}})
        self.assertEqual(status, 400)
        self.assertIn("no such file", data["error"])

    def test_one_job_at_a_time_and_stop(self):
        big = self.first / "Data/Users/alice/Documents/big"
        big.mkdir()
        for i in range(3000):
            (big / f"f{i}").write_text("x")
        self.call("/api/run", {"command": "scan", "options": self.options()})
        status, err = self.call("/api/run", {"command": "du", "options": self.options()})
        self.assertEqual(status, 400)
        self.assertIn("still running", err["error"])
        self.call("/api/stop", {})
        j = self.wait()
        self.assertIn(j["status"], ("stopped", "done"))  # a fast machine may finish first

    def test_rules_editing(self):
        _, r = self.call("/api/rules/check", {"text": "rules = ['- x/']\n[volume_roots]\nLarge = ['/']"})
        self.assertTrue(r["ok"])
        _, r = self.call("/api/rules/check", {"text": "[volume_roots]\nrules = ['- x/']"})
        self.assertFalse(r["ok"])
        self.assertIn("at the end of the file", r["error"])
        _, r = self.call("/api/rules/check", {"text": "rules = [ oops"})
        self.assertFalse(r["ok"])

        path = self.tmp / "mine.toml"
        _, r = self.call("/api/rules?path=" + str(path))
        self.assertFalse(r["exists"])
        self.assertIn("roots", r["text"])  # starts from the built-in rules
        status, r = self.call("/api/rules/save", {"path": str(path), "text": "rules = [ oops"})
        self.assertEqual(status, 400)
        self.assertFalse(path.exists())
        status, r = self.call("/api/rules/save", {"path": str(path), "text": RULES})
        self.assertEqual(status, 200, r)
        self.assertEqual(path.read_text(), RULES)
        self.assertEqual(self.call("/api/rules/save", {"path": str(self.tmp / "x.txt"), "text": RULES})[0], 400)

    def test_browse(self):
        (self.tmp / "old.dmg").write_bytes(b"")
        _, b = self.call("/api/browse?path=" + str(self.tmp))
        names = {e["name"]: e for e in b["entries"]}
        self.assertTrue(names["old.dmg"]["image"])
        self.assertTrue(names["Backup20241029"]["dir"])
        self.assertIn("rules.toml", names)

    def test_unreadable_archive(self):
        locked = self.tmp / "locked"
        (locked / "Mac").mkdir(parents=True)
        os.chmod(locked / "Mac", 0)
        try:
            if os.access(locked / "Mac", os.R_OK):
                self.skipTest("running as root")
            status, r = self.call("/api/archive?path=" + str(locked))
        finally:
            os.chmod(locked / "Mac", 0o755)
        self.assertEqual(status, 500)
        self.assertIn("Permission denied", r["error"])


class AppModeTest(unittest.TestCase):
    """How the Mac app runs the server: its own token, and the server stops when the app is gone."""

    def test_token_file_and_owner(self):
        import os
        import socket
        import subprocess
        import sys

        tmp = Path(tempfile.mkdtemp(prefix="timak-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        token = tmp / "token"
        token.write_text("a-token-chosen-by-the-app\n")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        owner = subprocess.Popen(["sleep", "60"])
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        server = subprocess.Popen(
            [sys.executable, "-m", "timak", "serve", "--port", str(port), "--no-browser",
             "--token-file", str(token), "--owner-pid", str(owner.pid)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )  # fmt: skip
        self.addCleanup(server.kill)

        def call(path, body=None, tok="a-token-chosen-by-the-app"):
            for _ in range(50):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    headers = {"X-Timak-Token": tok, "Content-Type": "application/json"}
                    conn.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, headers)
                    r = conn.getresponse()
                    return r.status, json.loads(r.read())
                except ConnectionRefusedError:
                    time.sleep(0.1)
            self.fail("server did not start")

        self.assertEqual(call("/api/state")[0], 200)
        self.assertEqual(call("/api/state", tok="other")[0], 403)
        # a new app instance takes over, then the first one goes away: the server stays
        second = subprocess.Popen(["sleep", "60"])
        self.addCleanup(second.kill)
        self.assertEqual(call("/api/adopt", {"pid": second.pid})[0], 200)
        owner.kill()
        owner.wait()
        time.sleep(3)
        self.assertIsNone(server.poll())
        # ... until the app that adopted it goes away too
        second.kill()
        second.wait()
        server.wait(timeout=10)
        self.assertIn("stopping", server.stdout.read())

    def test_quit(self):
        server = Server(0, TOKEN)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request("POST", "/api/quit", "{}", {"X-Timak-Token": TOKEN, "Content-Type": "application/json",
                                                 "Host": f"127.0.0.1:{server.server_port}"})  # fmt: skip
        self.assertEqual(conn.getresponse().status, 200)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        server.server_close()


if __name__ == "__main__":
    unittest.main()
