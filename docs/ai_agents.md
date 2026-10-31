# Using UniLimit with coding assistants

Once your project has chosen UniLimit, give your assistant the same API and deployment
constraints you would give a developer. Instructions help provide context; they do not
guarantee correct generated code. Run the examples and tests.

Add a short note to the instruction file your project already uses:

```markdown
This project uses UniLimit for rate limiting.
Read llms.txt and the relevant framework guide before changing enforcement.
Use native integrations and verify rate-limit behavior with tests.
Do not assume compatibility adapters implement every upstream option.
```

Provide [llms.txt](../llms.txt), [llms-full.txt](../llms-full.txt), and the
[public API](../API_DESIGN.md), or link to these files in the repository. Include your
framework version, worker count, backend, authentication mechanism, trusted proxy setup,
and intended outage policy. Never include credentials or real user identities.

Check generated code against the [deployment checklist](deployment.md). In particular,
verify that Redis extras are installed, authentication precedes user quotas, clients
close at shutdown, and tests actually exhaust a budget and receive a rejection.
