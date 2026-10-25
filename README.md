<div align="center">

<img src="assets/balladeer-banner-v2.webp" alt="Scaramouche, the Balladeer" width="100%">

# Scaramouche

**A persistent conversational character bot for Discord**

Python orchestration, conversation memory, optional voice and media, and interaction with [Wanderer](https://github.com/Kittybri/Wanderer).

</div>

## Engineering overview

This project explores how a character bot can retain conversational history and respond consistently while handling real Discord events and external AI services. The character is the interface; the engineering work includes routing, stored user state, response repetition controls, provider integration, and failure diagnosis.

**Start here:** [architecture](docs/architecture.md) | [setup and tests](docs/quickstart.md) | [repair tasks](https://github.com/Kittybri/agent-repair-lab) | [contribution and AI assistance](https://github.com/Kittybri/agent-repair-lab/blob/main/CONTRIBUTIONS.md)

## What to inspect

| Area | Source or evidence |
| --- | --- |
| Event handling and commands | `bot.py` |
| Persistent user and conversation state | `memory.py` |
| Relationship context | `relationship_engine.py` |
| Response repetition controls | `anti_repeat.py` and [offline tests](tests/test_anti_repeat.py) |
| Optional voice output | `voice_handler.py` |
| A real diagnosis and narrow repair | [PDF retrieval case study](https://github.com/Kittybri/agent-repair-lab/blob/main/docs/pdf-retrieval.md) |
| Design tradeoffs and acceptance checks | [Turn ownership case study](https://github.com/Kittybri/agent-repair-lab/blob/main/docs/turn-ownership.md) |

## Run the offline checks

```sh
python -m unittest discover -s tests -v
```

No Discord token or paid API is needed. GitHub Actions runs these checks on Python 3.11 and 3.12. They cover response-history isolation, bounded history, duplicate detection, and fresh fallback selection. They are not end-to-end bot or model-quality tests.

For live setup, see [quickstart](docs/quickstart.md). For a reproducible, credential-free engineering demonstration, prepare a task in the separate [repair lab](https://github.com/Kittybri/agent-repair-lab).

## Character experience

Scaramouche is sharp, theatrical, and difficult to impress. Conversation, stories, games, persistent familiarity, and optional media give that personality a place to operate. The character's fictional claims are not technical guarantees about memory or model reliability.

## Status and limitations

The public branch and deployed releases have historically diverged. Case studies identify what was actually observed; repair tasks are simplified reproductions rather than full production snapshots. Provider-backed responses remain probabilistic. This project does not publish uptime claims, model rankings, or measured accuracy results.

Development uses AI coding agents. Project direction and failure reports came from Abriana Meazzo; agents assisted implementation, diagnostics, tests, and documentation. See the linked contribution statement for the evidence boundaries.

Fan-made Genshin Impact character project, not affiliated with HoYoverse. Character imagery and names belong to their respective owners.

[Privacy policy](PRIVACY.md) | [Development notes](CONTRIBUTING.md)
