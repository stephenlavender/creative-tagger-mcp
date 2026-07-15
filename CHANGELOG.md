# Changelog

## 0.2.1 - 2026-07-15

- Fixed installed-artifact release smoke tests on Python 3.12 by materializing
  `EntryPoints` before indexing the console entry point.
- Documented the canonical hosted MCP endpoint as
  `https://api.creativetagger.ai/mcp/` while keeping the local stdio package
  path explicit.
- Aligned taxonomy v2 with the API contract: 15 controlled dimensions, one
  derived/open `aspect_ratio` dimension, and two dynamic brand dimensions.
