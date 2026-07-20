# Changelog

## Unreleased

- Aligned MCP positioning with the live launch surface: distinguish the
  47-tool unreleased stdio tree, 43-tool published `0.2.4` package, and 22-tool
  hosted catalog; document bearer-auth versus OAuth-only client compatibility;
  and stop advertising customer URL fetching, fresh competitor scans, or Meta
  imports as generally available while their production gates remain closed.
- Made the `competitive_whitespace` prompt reuse saved scans by default and
  require an explicit `run_fresh_scan=true` opt-in before attempting the
  provider-gated Meta Ad Library operation.

- Added three reporting tools wrapping the merged API surfaces:
  `get_creative_leaderboard` (ranked scale/kill list with a `below_min_spend`
  materiality floor and `rankings_withheld` observation mode),
  `get_batch_readout` (launch-cohort three-way verdicts against a batch-
  excluding baseline, including `insufficient_evidence`), and `compare_periods`
  (period-over-period deltas with a funnel decomposition that names the
  `dominant_factor` and passes through `revenue_caution`).
- Restructured the public tool catalog for description-budget headroom without
  changing any tool name or schema: shared workspace/date/fatigue-watch
  boilerplate now lives once in the server `instructions`, and the runtime
  catalog compaction layer trims duplicated per-tool prose while the source
  descriptions stay verbose for humans and the surface tests.
- Updated the report-recipe prompts to reach the new single-call primitives:
  `scale_kill_hold` now ranks via `get_creative_leaderboard`, `batch_readout`
  grades via `get_batch_readout`, and `monday_money_check` /
  `weekly_creative_report` compare via `compare_periods`.
- Fixed the remaining surfaces of the tag x demographic_segment cross
  (structurally `not_applicable` on the API -- demographics are account-level
  only, with no per-ad key): `export_demographics_context`'s and
  `export_brain_learnings_context`'s suggested strategy views/decision queue
  used to emit follow-up queries pairing a creative tag with a demographic
  axis, which always came back empty. These now route to a real,
  answerable read instead -- either a demographic x demographic pairing or a
  separate, account-wide `get_taxonomy_performance` call, never a joined
  cross. Also corrected a factual error in `hook_report`'s own step 4:
  `report_template="hook-performance"` crosses hook x format, not
  hook x messaging_angle.

## 0.2.4 - 2026-07-16

- Make `set_brand_context` a sparse PATCH so notes-only or voice-only updates
  preserve every omitted long-term-memory field and all saved reference assets.
- Preserve explicit empty strings/lists as intentional field clears.

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
