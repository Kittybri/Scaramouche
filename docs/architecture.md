# Architecture and evidence map

Scaramouche is a Python Discord character bot with persistent conversation state, provider-backed responses, optional voice output, and interaction with Wanderer. This document maps the published repository. Deployed releases can differ; a case study is not proof that every newer feature exists on this branch.

```mermaid
flowchart TD
    D[Discord events and commands] --> B[bot.py: routing and orchestration]
    B <--> M[memory.py: persistent user and conversation state]
    B --> R[relationship_engine.py: state and context rules]
    B --> A[anti_repeat.py: recent output checks]
    B --> G[Groq: response generation]
    B --> V[voice_handler.py: optional voice output]
    B --> S[grounded_search.py: optional search]
    B --> I[character_vision.py: image-related behavior]
    B --> L[llm.py: Groq key rotation and model fallback]
    B --> T[text_utils.py: narration stripping, mentions, keyword matching]
    B --> D[doc_extract.py: uploaded documents opened in a resource-limited child process]
    B <--> P[Wanderer: partner interactions]
```

## Boundaries

`bot.py` currently holds much of the orchestration. Large-file size is not a quality metric; progressively separating transport, provider access, memory, and delivery would improve maintainability. Such refactoring should be backed by tests and performed independently of documentation updates.

The offline tests exercise the real implementations of `anti_repeat.py`, `text_utils.py`, `relationship_engine.py`, `llm.py`, `doc_extract.py` (including zip and PDF bombs), `grounded_search.py` (against saved result pages) and `memory.py` (on a temporary database). A further module drives `bot.py` through a mock Discord and a fake model to check message routing, privacy scoping and commands. None of it connects to Discord or a model provider, so it is not a measure of live quality. The separate [repair lab](https://github.com/Kittybri/agent-repair-lab) tests simplified, explicit contracts for retrieval, routing, voice confidence, memory attribution, and complete delivery.

## Operational concerns

Provider errors, reconnects, duplicate events, memory attribution, and incomplete output are distinct failure modes. The [case studies](https://github.com/Kittybri/agent-repair-lab/tree/main/docs) explain specific evidence and tradeoffs. There are no published uptime, latency, accuracy, or model-comparison measurements.

User messages, voice data, memory databases, uploaded documents, and credentials are not demonstration fixtures. Use synthetic examples and review [PRIVACY.md](../PRIVACY.md) before operating the bot with other people.
