# Changelog

## 0.2.3 - 2026-07-16

- Require and validate the `predict_observational.v2` handshake for the
  legacy-named `predict_creative` tool.
- Expose objective metric and direction inputs and fail closed on legacy or
  mixed causal-looking prediction payloads.

## 0.2.2 - 2026-07-15

- Added authenticated workspace discovery and explicit `brand_name` scoping
  across library, Meta, report, and strategist tools, including the empty
  default workspace.
- Defaulted creative-strategy reports to a bounded concise response with a
  detailed opt-in and configurable matrix-cell cap.
- Aligned demographic tools with the API's descriptive
  `higher_observed_efficiency` and `lower_observed_efficiency` bands, without
  causal audience or budget-allocation claims.
- Exposed that same canonical vocabulary on every Brain audience filter while
  retaining legacy aliases only inside the stdio compatibility layer.
- Aligned every public collection limit with the API contract and clamped it
  in stdio before transport, including library pagination, prebuilt/strategy/
  Brain/custom reports, performance series, and competitor results.
- Made validation and transport failures protocol-visible, strengthened agent
  instructions around objective-aware observational evidence, and advertised
  the installed package version in MCP initialization metadata.
- Upgraded the MCP dependency floor to `1.28.1`, expanded HTTP and tool-surface
  regressions, and hardened installed-wheel release verification for the new
  workspace and strategist contracts.

## 0.2.1 - 2026-07-15

- Fixed installed-artifact release smoke tests on Python 3.12 by materializing
  `EntryPoints` before indexing the console entry point.
- Documented the canonical hosted MCP endpoint as
  `https://api.creativetagger.ai/mcp/` while keeping the local stdio package
  path explicit.
- Aligned taxonomy v2 with the API contract: 15 controlled dimensions, one
  derived/open `aspect_ratio` dimension, and two dynamic brand dimensions.
