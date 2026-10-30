# Copilot instructions

Follow [AGENTS.md](../AGENTS.md): it is the single source of agent instructions for this
repository. Task-specific knowledge is in [context/](../context/README.md).

The non-negotiables, in short:

- Backend batches are atomic; rejected requests consume nothing.
- `backends/redis/batch.lua` must stay identical in behaviour to the Python algorithms.
- No sync Redis calls on async paths.
- No raw identities in keys or logs.
- The core never imports a web framework.
- Real Redis in tests; never weaken a test.
