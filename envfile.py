"""Load this project's ``.env`` - and nothing else unless asked.

python-dotenv's default is to search every parent folder for a ``.env`` and use the first one it finds, which can
silently pull in another project's keys (or the other bot's Discord token) when the checkout sits inside a bigger
folder. Here only ``<project>/.env`` is read; a parent folder's file is read as well when ``LOAD_PARENT_ENV=1``
(a shared checkout with Wanderer). A variable that is already set in the environment is never overridden.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent


def load_project_env(project_dir: Path | str | None = None) -> None:
    base = Path(project_dir) if project_dir is not None else PROJECT_DIR
    load_dotenv(base / ".env")
    if (os.getenv("LOAD_PARENT_ENV") or "").strip() == "1":
        load_dotenv(base.parent / ".env")
