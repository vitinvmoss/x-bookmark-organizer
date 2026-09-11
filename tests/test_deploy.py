"""Deployment entrypoint tests.

Locks the production WSGI entrypoint to ``server:app`` (never the legacy
``app:app``), verifies the Render start command / health check / PORT
handling, and proves the WSGI object imports and serves ``/healthz`` over
a real socket.

The import checks run in an isolated subprocess so they do not disturb the
module state used by ``tests.test_prod``.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env():
    env = dict(os.environ)
    env["DATA_DIR"] = tempfile.mkdtemp(prefix="xbo-deploy-")
    env["SESSION_SECRET"] = "test-session-secret-deploy-0123456789abcdef"
    env["APP_USERNAME"] = "owner"
    env.pop("RENDER", None)
    return env


def _run(code):
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=_env(), capture_output=True, text=True, timeout=60)
    return proc


class WsgiEntrypointTest(unittest.TestCase):
    def test_import_server_app(self):
        proc = _run("from server import app; assert app is not None; "
                    "print(app.name)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "server")

    def test_healthz_served_over_real_socket(self):
        code = (
            "import json, threading, urllib.request, wsgiref.simple_server\n"
            "from server import app\n"
            "srv = wsgiref.simple_server.make_server('127.0.0.1', 0, app)\n"
            "host, port = srv.server_address\n"
            "threading.Thread(target=srv.handle_request, daemon=True).start()\n"
            "with urllib.request.urlopen("
            "'http://%s:%d/healthz' % (host, port), timeout=5) as r:\n"
            "    print(r.status, r.read().decode())\n"
            "srv.server_close()\n")
        proc = _run(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status, body = proc.stdout.strip().split(" ", 1)
        self.assertEqual(int(status), 200)
        self.assertTrue(json.loads(body)["ok"])


class RenderConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "render.yaml"), encoding="utf-8") as fh:
            cls.cfg = fh.read()

    def _value(self, key):
        m = re.search(r"%s:\s*[\"']?([^\"'\n]+)" % re.escape(key), self.cfg)
        self.assertIsNotNone(m, "render.yaml missing %s" % key)
        return m.group(1).strip()

    def test_start_command_uses_server_app(self):
        cmd = self._value("startCommand")
        self.assertIn("gunicorn server:app", cmd)
        self.assertIn("$PORT", cmd)
        self.assertNotIn("gunicorn app:app", cmd)

    def test_health_check_path_is_healthz(self):
        self.assertEqual(self._value("healthCheckPath"), "/healthz")

    def test_server_reads_port_for_local_boot(self):
        with open(os.path.join(ROOT, "server.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('os.environ.get("PORT"', src)


if __name__ == "__main__":
    unittest.main()
