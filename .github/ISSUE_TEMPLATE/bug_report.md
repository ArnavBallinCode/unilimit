---
name: Bug report
about: Report incorrect enforcement, integration behavior, or documentation
---

## Expected and actual behavior

Include HTTP status/headers or the decision you expected and what happened instead.

## Minimal reproduction

Provide a small runnable example and the command to run it. Remove credentials, tokens,
real IPs, and user identities.

## Environment

- Python and UniLimit versions:
- Framework and version:
- Storage (memory / Redis standalone / Cluster / Sentinel), Redis and redis-py versions:
- Worker count and sync/async deployment:
- Algorithm, identity source, proxy setup, and failure policy:

## Evidence

Include redacted traceback/output. For concurrency or performance reports include the
request count, concurrency, expected admissions, actual admissions, and reproduction command.
