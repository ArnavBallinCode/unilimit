# context/

Agent-oriented knowledge about UniLimit. [`AGENTS.md`](../AGENTS.md) has the rules;
these files explain the *why* and record what has been learned, so an agent with no
memory of earlier sessions can work safely.

| File | Read it when |
|---|---|
| [status.md](status.md) | Starting any work: what is done, what is next, what CI says. |
| [codebase-map.md](codebase-map.md) | Finding where something lives or where a change belongs. |
| [invariants.md](invariants.md) | Touching algorithms, Lua, backends, policy or failure handling. |
| [decisions.md](decisions.md) | Wondering why something is the way it is, before changing it. |
| [lessons.md](lessons.md) | Debugging, or before writing Redis/async/test-infra code. Most bugs we hit are here. |
| [testing.md](testing.md) | Writing or running tests, CI, version matrices, fuzzing. |
| [adoption-audit.md](adoption-audit.md) | Reviewing onboarding, competitor evidence, adoption priorities, or portfolio integration candidates. |
| [prior-art.md](prior-art.md) | Comparing with slowapi, limits, django-ratelimit, fastapi-limiter; borrowing from them. |
| [glossary.md](glossary.md) | Meeting an unfamiliar term (binding result, partial commit, storage key…). |

Authoritative design documents stay at the root: [ARCHITECTURE.md](../ARCHITECTURE.md)
(design and rationale) and [API_DESIGN.md](../API_DESIGN.md) (public API). The files
here summarise and link to them rather than repeating them.

**Keep this folder current.** A change that alters status, makes a decision or
uncovers a pitfall updates the matching file in the same commit.
