# Documentation hosting

The readable entry point today is [docs/README.md](README.md), with a runnable example
in the repository [README](../README.md). A [`mint.json`](../mint.json) navigation
configuration is included, but a configuration file alone does not provision a hosted
site, confirm a public URL, or validate a production build.

Before advertising a hosted documentation URL:

1. Choose and configure the hosting provider under the maintainer's account.
2. Validate the provider's current configuration and Markdown/MDX requirements.
3. Build every page, check assets and navigation, and test search and mobile layouts.
4. Verify the actual deployed URL, then update package metadata and repository links.

Keep examples executable and links tested regardless of hosting provider. The repository's
[contribution guide](../CONTRIBUTING.md) describes required checks. Assistant references
live in [llms.txt](../llms.txt) and [llms-full.txt](../llms-full.txt); they must stay consistent
with the same public API rather than becoming a separate specification.
