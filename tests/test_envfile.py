"""Only the project's own .env is read, unless a shared parent file is asked for."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HAVE_DOTENV = importlib.util.find_spec("dotenv") is not None


@unittest.skipUnless(HAVE_DOTENV, "python-dotenv is not installed")
class EnvFileTests(unittest.TestCase):
    def setUp(self):
        import envfile

        self.envfile = envfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name)
        self.project = self.parent / "bot"
        self.project.mkdir()
        (self.parent / ".env").write_text("PARENT_ONLY_KEY=from-parent\nSHARED_KEY=parent-value\nDISCORD_TOKEN=the-other-bots-token\n")
        (self.project / ".env").write_text("OWN_KEY=from-project\nSHARED_KEY=project-value\n")
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("OWN_KEY", "PARENT_ONLY_KEY", "SHARED_KEY", "DISCORD_TOKEN", "LOAD_PARENT_ENV"):
            os.environ.pop(name, None)

    def test_a_parent_folders_env_file_is_ignored_by_default(self):
        self.envfile.load_project_env(self.project)
        self.assertEqual(os.environ["OWN_KEY"], "from-project")
        self.assertEqual(os.environ["SHARED_KEY"], "project-value")
        self.assertNotIn("PARENT_ONLY_KEY", os.environ)
        self.assertNotIn("DISCORD_TOKEN", os.environ)  # never borrows another project's token

    def test_a_missing_project_file_does_not_fall_back_to_the_parent(self):
        (self.project / ".env").unlink()
        self.envfile.load_project_env(self.project)
        self.assertNotIn("PARENT_ONLY_KEY", os.environ)
        self.assertNotIn("DISCORD_TOKEN", os.environ)

    def test_the_parent_file_can_be_requested_and_never_overrides(self):
        os.environ["LOAD_PARENT_ENV"] = "1"
        os.environ["DISCORD_TOKEN"] = "already-set"
        self.envfile.load_project_env(self.project)
        self.assertEqual(os.environ["PARENT_ONLY_KEY"], "from-parent")
        self.assertEqual(os.environ["SHARED_KEY"], "project-value")  # the project's own value wins
        self.assertEqual(os.environ["DISCORD_TOKEN"], "already-set")  # and so does the real environment


if __name__ == "__main__":
    unittest.main()
