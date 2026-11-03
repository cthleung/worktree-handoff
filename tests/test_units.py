"""Fast unit tests for the pure pieces of the handoff package: the secrets guard, the summary,
the log and the pid check. They import the modules directly and never touch git, so they run
in well under a second. The end-to-end behavior lives in test_handoff.py.

Token-shaped strings are built at runtime from pieces, so this file never contains anything a
secret scanner would flag.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from handoff import config, gitops, guard, host  # noqa: E402
from handoff import log as hlog  # noqa: E402


def fake(prefix: str, body: str, n: int) -> str:
    """A token-shaped string: prefix + n copies of body, joined at runtime."""
    return prefix + body * n


class SecretContent(unittest.TestCase):
    def test_each_token_kind_is_caught(self):
        samples = {
            "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "aws": fake("AK" + "IA", "A", 16),
            "github": fake("gh" + "p_", "a", 36),
            "github pat": fake("github" + "_pat_", "a", 50),
            "anthropic": fake("sk-" + "ant-", "a", 20),
            "openai": fake("sk-", "a", 32),
            "slack": fake("xo" + "xb-", "a", 10),
            "google": fake("AI" + "za", "a", 35),
            "gitlab": fake("gl" + "pat-", "a", 20),
        }
        for kind, token in samples.items():
            with self.subTest(kind=kind):
                self.assertIsNotNone(guard.secret_hit(f"key = {token}\n"), kind)

    def test_ordinary_text_is_left_alone(self):
        for text in ("hello world", "sk-short", "AKIA is a prefix but not a key", "ghp_ too short"):
            with self.subTest(text=text):
                self.assertIsNone(guard.secret_hit(text))


class SecretNames(unittest.TestCase):
    def test_secret_looking_names(self):
        for path in (".env", "app/.env.local", "certs/server.pem", "id_rsa", "home/id_ed25519.pub",
                     "service-account-prod.json", "deploy/CREDENTIALS", "infra/prod.tfvars"):
            with self.subTest(path=path):
                self.assertIsNotNone(guard.secret_name(path))

    def test_ordinary_names(self):
        for path in ("README.md", "src/environment.py", "docs/keys.md", ".env.example.md/notes"):
            with self.subTest(path=path):
                self.assertIsNone(guard.secret_name(path))

    def test_only_the_file_name_counts(self):
        # a folder called ".env" doesn't make every file in it a secret
        self.assertIsNone(guard.secret_name(".env/README.md"))


class Summarize(unittest.TestCase):
    def test_buckets_and_count(self):
        changes = [{"status": "A", "path": "a"}, {"status": "M", "path": "m"},
                   {"status": "D", "path": "d"}, {"status": "M", "path": "m2"}]
        self.assertEqual(gitops.summarize(changes),
                         {"added": ["a"], "modified": ["m", "m2"], "deleted": ["d"], "count": 4})

    def test_empty(self):
        self.assertEqual(gitops.summarize([]), {"added": [], "modified": [], "deleted": [], "count": 0})


class Helpers(unittest.TestCase):
    def test_short_truncates_long_values(self):
        self.assertEqual(hlog._short("x" * 10), "x" * 10)
        self.assertEqual(len(hlog._short("x" * 1000)), 403)
        self.assertEqual(hlog._short(["a", 1]), '["a", 1]')

    def test_env_reads_the_prefixed_variable(self):
        with mock.patch.dict(os.environ, {"WORKTREE_HANDOFF_TICK_MIN": "5"}):
            self.assertEqual(config.env("TICK_MIN"), "5")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.env("TICK_MIN", "20"), "20")

    def test_pid_alive(self):
        self.assertTrue(host.pid_alive(os.getpid()))
        done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True)
        self.assertFalse(host.pid_alive(int(done.stdout.strip())))


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "handoff.log"

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_every_event_goes_to_the_file_with_its_context(self):
        log = hlog.Log(self.path, verbose=False).bind(branch="main")
        with redirect_stdout(io.StringIO()):
            log.info("tree.clean")
            log.warning("unwrap.blocked", why="test")
        rows = self.lines()
        self.assertEqual([r["event"] for r in rows], ["tree.clean", "unwrap.blocked"])
        self.assertTrue(all(r["branch"] == "main" for r in rows))
        self.assertEqual(rows[1]["level"], "warning")

    def test_quiet_events_stay_off_stdout(self):
        log = hlog.Log(self.path, verbose=False)
        out = io.StringIO()
        with redirect_stdout(out):
            log.info("tree.clean")
            log.info("handoff.unwrapped", files=1)
        printed = out.getvalue()
        self.assertNotIn("tree.clean", printed)
        self.assertIn("handoff.unwrapped", printed)

    def test_verbose_prints_everything(self):
        out = io.StringIO()
        with redirect_stdout(out):
            hlog.Log(self.path, verbose=True).info("tree.clean")
        self.assertIn("tree.clean", out.getvalue())

    def test_bind_does_not_change_the_parent(self):
        parent = hlog.Log(self.path, verbose=False)
        parent.bind(head="abc")
        self.assertEqual(parent.context, {})


if __name__ == "__main__":
    unittest.main()
