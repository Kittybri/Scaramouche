# Setup and testing

## Offline first

Python 3.11 or 3.12 is used by the regression workflow. From the repository root:

```sh
python -m unittest discover -s tests -v
```

These tests require only the standard library and use synthetic text. They do not start the bot or require credentials.

## Live bot

Use a separate Discord test server and your own provider accounts. Provider availability, model names, permissions, and optional operating-system audio tools can change.

```sh
python -m venv .venv
```

Activate the environment with `.venv\Scripts\Activate.ps1` on Windows PowerShell, or `source .venv/bin/activate` on macOS/Linux. Then:

```sh
python -m pip install -r requirements.txt
```

Create a local `.env` from `.env.example` and set your Discord bot token and Groq key. Enable the Discord privileged intents required by the source, including message content. Give the test bot only permissions needed for the features you intend to exercise. Set the optional partner ID only when testing with another bot you control.

Start with `python bot.py`. Optional voice and document features may require additional dependencies or external services. The published branch is not guaranteed to match the latest deployed release; review the relevant code path before enabling a feature. In particular, the PDF incident involved a separately deployed release and is documented in the repair lab.

Never commit your `.env`, provider keys, recordings, document uploads, or SQLite databases. Before publishing changes, inspect the staged filenames and content.
