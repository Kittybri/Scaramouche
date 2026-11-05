# Setup and testing

## Offline first

Python 3.11 or 3.12 is used by the regression workflow. From the repository root:

```sh
python -m unittest discover -s tests -v
```

These tests use only synthetic data and need no credentials or network. The ones that exercise the database, the model client or the Discord pipeline need the project's dependencies, so run `python -m pip install -r requirements.txt` first (tests whose dependencies are missing are skipped, not failed). They drive `bot.py` through a mock Discord and a fake model; nothing talks to Discord or a provider.

## Live bot

Use a separate Discord test server and your own provider accounts. Provider availability, model names, permissions, and optional operating-system audio tools can change.

```sh
python -m venv .venv
```

Activate the environment with `.venv\Scripts\Activate.ps1` on Windows PowerShell, or `source .venv/bin/activate` on macOS/Linux. Then:

```sh
python -m pip install -r requirements.txt
```

Create a local `.env` from `.env.example` and set your Discord bot token and Groq key. Enable the Discord privileged intents required by the source: message content and server members. Give the test bot only permissions needed for the features you intend to exercise. Set the optional partner ID only when testing with another bot you control.

Useful optional settings (all listed in `.env.example`): extra Groq keys that are rotated automatically; `GROQ_MODELS` / `GROQ_VISION_MODELS`, comma-separated lists tried in order so a retired model degrades into the next one instead of an outage; `XAI_API_KEY` for xAI image descriptions (Groq vision is used without it); `FISH_AUDIO_API_KEY` for voice (Google TTS is the fallback); `MESSAGE_RETENTION_DAYS` to delete old conversation history; `BOT_TIMEZONE` and `DATA_DIR`. Video understanding and `!tedtalk` need `ffmpeg` (installed by `railway.toml`, or via `imageio-ffmpeg`).

Start with `python bot.py`. Optional voice and document features may require additional dependencies or external services. The published branch is not guaranteed to match the latest deployed release; review the relevant code path before enabling a feature. In particular, the PDF incident involved a separately deployed release and is documented in the repair lab.

Never commit your `.env`, provider keys, recordings, document uploads, or SQLite databases. Before publishing changes, inspect the staged filenames and content.
