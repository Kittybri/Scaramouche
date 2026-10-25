# Development notes

Run `python -m unittest discover -s tests -v` before submitting a change. Add a regression case when fixing output repetition or history isolation. Use fake provider responses and synthetic users for new tests; live APIs should not be needed for ordinary regression checks.

Describe the user-visible failure, the evidence, the repair, and what the tests do not establish. Keep unrelated behavior changes separate. Never commit credentials, private conversations, recordings, databases, or uploaded study material.

Development uses AI coding assistance. Explain that assistance honestly in change descriptions and be prepared to explain the resulting code. See the shared [contribution statement](https://github.com/Kittybri/agent-repair-lab/blob/main/CONTRIBUTIONS.md).
