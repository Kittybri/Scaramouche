# Shared dependency compatibility policy

These two Discord bot repos intentionally have different application and
voice architectures. Standardization applies only to the following nine
shared **non-voice** dependencies. No production package is upgraded or
deployed merely by creating this draft pull request.

| Dependency | Proposed common pinned version |
|---|---|
| `groq` | `1.0.0` |
| `python-dotenv` | `1.2.1` |
| `aiosqlite` | `0.22.1` |
| `aiohttp` | `3.13.5` |
| `httpx` | `0.28.1` |
| `ormsgpack` | `1.11.0` |
| `python-pptx` | `1.0.2` |
| `python-docx` | `1.2.0` |
| `requests` | `2.32.5` |

**Do not silently change** `discord.py`, `PyNaCl`, `davey`,
`gtts`, `imageio-ffmpeg`, Opus/FFmpeg system packages, or Google Docs
integration dependencies in this compatibility batch. They require
independent live Discord voice and Google-service validation.

The shared pinned versions match the existing Scaramouche release baseline.
Wanderer's adoption remains a **review-only proposal** until its full
suite, provider startup, and actual Oracle compatibility are verified.
CI install success alone does not establish live Groq or provider behavior.
This is not a lockfile for transitive dependencies.

Keep the repositories' extra packages separate, and never deploy a
dependency-only PR without a rollback and pinned runtime/environment test.
