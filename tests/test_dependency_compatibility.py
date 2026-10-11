"""Prevent unreviewed shared dependency drift across the chosen baseline."""
from pathlib import Path

EXPECTED = {
    "groq": "1.0.0",
    "python-dotenv": "1.2.1",
    "aiosqlite": "0.22.1",
    "aiohttp": "3.13.5",
    "httpx": "0.28.1",
    "ormsgpack": "1.11.0",
    "python-pptx": "1.0.2",
    "python-docx": "1.2.0",
    "requests": "2.32.5"
}
ROOT = Path(__file__).resolve().parents[1]


def test_common_non_voice_dependencies_match_reviewed_pins():
    installed = (ROOT / "requirements.txt").read_text().splitlines()
    for package, version in EXPECTED.items():
        assert f"{package}=={version}" in installed


def test_voice_and_google_versions_are_not_modified_by_this_guard():
    installed = (ROOT / "requirements.txt").read_text()
    assert "PyNaCl>=1.5.0,<1.6" in installed
    assert "davey>=0.1.0" in installed
