# Creative Tagger MCP Server

The MCP layer for [Creative Tagger](https://creativetagger.ai) — plug structured creative intelligence into any AI agent (Claude Desktop, Cursor, Windsurf, ChatGPT with MCP, etc.).

Status note (verified 2026-07-15): the production API, hosted remote MCP, and
`creative-tagger-mcp==0.2.1` package are published. The hosted and stdio
surfaces are separate clients of the same API and may expose different tool
counts. This branch documents the unreleased `0.2.2` candidate; install `0.2.1`
for the current PyPI release until `0.2.2` passes independent review, its API
dependencies are deployed, trusted-publishing CI succeeds, and PyPI is verified.

Your AI of choice gets:

- **Taxonomy** — 21 standardized dimensions for any ad creative (video, image, carousel, landing page, long video, email)
- **Memory** — every analysis is saved to the user's library; the agent can search it, recall patterns, and pull individual results
- **Brand-custom taxonomy** — extend the standard taxonomy with each brand's founders, products, segments, aliases, and naming variables
- **Meta performance memory** — read-only Meta sync/status/tools so agents can reason over objective-aware results, unproven tags, observational demographic delivery, and taxonomy gaps
- **Brain learnings** — auto-written account learnings in plain language, with agent-ready context for the next brief
- **Strategist** — recommendation + gap-analysis tools that reason over the user's library plus saved brand context (voice, audience, anti-patterns)
- **Competitive intelligence** — scan a competitor's Meta Ad Library through Creative Tagger's native Market access

## Quick Start

For clients that support remote MCP, connect the current hosted server:

```text
URL: https://api.creativetagger.ai/mcp/
Authorization: Bearer ct_your_key
```

The repository package is the stdio path for clients that require a local
command:

```bash
# Install the verified release
pip install creative-tagger-mcp==0.2.1

# Run against production (default)
CREATIVE_TAGGER_API_KEY=ct_your_key creative-tagger-mcp

# Or against a local API
CREATIVE_TAGGER_URL=http://localhost:8000 \
CREATIVE_TAGGER_API_KEY=ct_your_key \
  creative-tagger-mcp
```

Get an API key at [app.creativetagger.ai](https://app.creativetagger.ai).

## Release Verification

Before publishing a new MCP version, build the artifacts and smoke-test the
wheel that will be uploaded to PyPI:

```bash
python -m build
python scripts/smoke_release.py
python -m twine check \
  dist/creative_tagger_mcp-0.2.2-py3-none-any.whl \
  dist/creative_tagger_mcp-0.2.2.tar.gz
```

The smoke test installs the wheel into a temporary virtualenv, verifies the
`creative-tagger-mcp` console entry point, checks the package version, and
confirms the V1 tool surface is present from the installed artifact.

## Publishing to PyPI

The release workflow publishes from GitHub Actions after it builds the package,
runs `scripts/smoke_release.py`, and passes `twine check`.

After the `0.2.2` review and API-dependency gates pass, tag the candidate:

```bash
git tag v0.2.2
git push origin v0.2.2
```

The workflow supports PyPI trusted publishing with GitHub OIDC. Configure the
PyPI publisher for repository `stephenlavender/creative-tagger-mcp`, workflow
`.github/workflows/publish.yml`, environment `pypi`, then push the version tag.

Exact PyPI trusted publisher values:

- PyPI project: `creative-tagger-mcp`
- Publisher: GitHub
- Owner: `stephenlavender`
- Repository: `creative-tagger-mcp`
- Workflow filename: `publish.yml`
- Environment name: `pypi`

If the workflow fails with `invalid-publisher`, PyPI does not have a trusted
publisher matching those claims yet. Add the publisher above, then rerun the
failed workflow or push the version tag again.

Fallback path: add a GitHub Actions repository secret named `PYPI_API_TOKEN`
containing a PyPI project token. The same workflow will use that token when it
is present.

Local fallback:

```bash
python -m build
python scripts/smoke_release.py
python -m twine check \
  dist/creative_tagger_mcp-0.2.2-py3-none-any.whl \
  dist/creative_tagger_mcp-0.2.2.tar.gz
python -m twine upload \
  dist/creative_tagger_mcp-0.2.2-py3-none-any.whl \
  dist/creative_tagger_mcp-0.2.2.tar.gz
```

Always select the exact release artifacts for a local upload. A reused checkout
may contain older valid distributions in `dist/`; never publish with
`twine upload dist/*`.

## Add to Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "creative-tagger": {
      "command": "creative-tagger-mcp",
      "env": {
        "CREATIVE_TAGGER_URL": "https://api.creativetagger.ai",
        "CREATIVE_TAGGER_API_KEY": "ct_your_key_here"
      }
    }
  }
}
```

Restart Claude Desktop. The tools appear in the MCP picker.

## Tools

### `analyze_creative`
Analyze any ad creative and get structured classification across 21 dimensions.
```
{ "file_path": "./ad.mp4", "brand_name": "Brand" }
{ "url": "https://example.com/landing-page", "brand_name": "Brand" }
{ "html_content": "<html>...</html>", "brand_name": "Brand" }
```
Results auto-save to the user's library.

### `get_taxonomy`
Read taxonomy v2's versioned vocabulary or one dimension. The package returns
15 controlled dimensions, one derived/open `aspect_ratio` dimension, and two
intentionally dynamic, brand-specific dimensions. Aspect ratio includes common
canonical examples but sets `allow_other_values: true`: the API may derive any
reduced `WxH` ratio (such as `3x2` or `300x157`) or preserve a `W:H` ratio for
long video. The package does not infer enums from OpenAPI: several valid
classification fields are strings in that schema, so schema discovery would
silently return an incomplete taxonomy.
```
{}                                # all controlled + derived/open + dynamic dimensions
{ "dimension": "hook_type" }      # one dimension
{ "dimension": "aspect_ratio" }   # examples; other derived values remain valid
```
Taxonomy v2 splits three dimensions the old model mixed together: **media type**
(the auto-detected format — static image, video, carousel; never AI-classified),
**asset type** (production class: UGC, Studio, High Production, …), and
**visual format** (execution style: Talking Head, Demo, Testimonial, …).
`Static Image` and `Carousel` are media types and are no longer valid
`visual_format` values. `messaging_angle` is the canonical angle dimension.

### `list_workspaces`
List the authenticated user's available workspaces. Start every connected
account workflow here, select one returned `brand_name`, and pass that exact
value to every library, Meta status, report, and strategist call. Do not blend
observations across workspaces unless the user explicitly requests a comparison.
```
{}
```

### `list_library`
Browse saved analyses. Search by filename or hook, filter by format, messaging
angle, emotion, CTA, talent, offer, audio type, or seasonality, and sort by
joined performance.
```
{
  "brand_name": "Acme",
  "limit": 50,
  "search": "BFCM",
  "format": "video",
  "angle": "Social Proof",
  "talent": "Founder",
  "sort": "roas"
}
```

### `get_library_patterns`
Cross-library pattern insights — concentration and diversity per dimension, plus rule-based diversification flags.
Pass the exact workspace `brand_name` returned by `list_workspaces`.

### `get_analysis`
Pull the full 21-dimension result for one library item.
```
{ "brand_name": "Acme", "analysis_id": 42 }
```

### `recommend` ⭐
Ask the Creative Strategist a question grounded in the user's library + brand context.
```
{ "brand_name": "Acme", "question": "What kind of UGC should I test for Q4?" }
```
Returns concrete recommendations using taxonomy values + library observations.

### `analyze_gaps` ⭐
Identify concentration risk in the library and propose next creatives that diversify it.
```
{ "brand_name": "Acme" }
```

### `get_brand_context` / `set_brand_context`
Long-term memory per brand. Voice, target audience, top performers, anti-patterns, notes.
```
set_brand_context: {
  "brand_name": "Acme",
  "voice": "clinical, precise, no personality",
  "target_audience": "new moms 28-40, postpartum recovery",
  "top_performers": ["UGC TalkHead", "BeforeAfter visuals"],
  "anti_patterns": ["loud humor", "celebrity endorsement"],
  "notes": "Q4 focus: gift-shoppers + retention"
}
```
Strategist tools auto-include this context.

### `get_brand_taxonomy` / `set_brand_taxonomy_value` / `delete_brand_taxonomy_value` / `set_brand_entity` / `delete_brand_entity`
Customize the standard taxonomy for one brand without breaking cross-brand reporting.
```
set_brand_taxonomy_value: {
  "brand_name": "Acme",
  "dimension": "talent",
  "value": "Stephen Lavender / Founder",
  "aliases": ["Stephen", "founder"],
  "description": "Use when Stephen appears or is referenced"
}

set_brand_entity: {
  "brand_name": "Acme",
  "entity_type": "product",
  "name": "Creative Tagger",
  "aliases": ["CT", "tagger"]
}

delete_brand_taxonomy_value: {
  "brand_name": "Acme",
  "dimension": "talent",
  "value": "Old Founder Label"
}

delete_brand_entity: {
  "brand_name": "Acme",
  "entity_type": "product",
  "name": "Retired Product"
}
```

### `get_naming_variables` / `list_naming_templates` / `save_naming_template`
Manage saved naming templates from your agent. Templates support standard taxonomy
fields plus brand-custom variables like founder, product, offer, customer_segment,
icp, and campaign_label. Saved templates auto-apply to future `analyze_creative`
results.
```
save_naming_template: {
  "name": "default",
  "template": "{brand}_{founder}_{customer_segment}_{hook_type}_{cta}_{ratio}_{version}"
}
```

Use `preview_naming_template` to test a template before saving, and
`delete_naming_template` to remove one.

### `get_meta_status` / `sync_meta_performance`
Check or trigger read-only Meta performance memory. No campaign creation, no budget edits.
Creative Tagger must have an approved native Meta OAuth connection before
customer accounts can sync Meta performance.
Pass `attribution_windows` when the buyer uses a non-default Meta lookback
window and Creative Tagger should match Ads Manager exactly.
```
{
  "brand_name": "Acme",
  "date_preset": "last_30d",
  "attribution_windows": ["7d_click", "1d_view"]
}
```

### `get_creative_strategy_report`
Pull the same strategy matrix shown in Creative Tagger Reports. Defaults to
visual formats by messaging angles, with states for next tests, live learning,
winners, losers, fatigue, and gaps. Returns the decision queue and a bounded
matrix slice for agent strategy work. Detailed responses also include an
`agent_context` payload that can be handed directly to an LLM.
Supports creative-diagnostics metrics such as CTR, thumbstop, hook, hold, video
milestone rates, CPA, CVR, ROAS, revenue, spend, and funnel score. For
audience-mode reads, switch the axes to demographic dimensions such as
`demographic_age` and `demographic_gender`, or use the `demographic-read` or
`audience-signals` templates. Other built-in templates include
`creative-winners`, `fatigue-watch`, `coverage-gaps`, `hook-performance`, and
`persona-read`. Creative axes follow taxonomy v2: `visual_format` (execution
style), `asset_type` (production class), and `media_type` (auto-detected
format) are three separate dimensions, with `ad_type` kept as a deprecated
alias for `visual_format`. For mixed creative × audience reads, keep one
creative axis such as `messaging_angle`, `visual_format`, `hook`, `persona`,
or `offer_type` and
set the other axis to `demographic_segment` or `demographic_signal`. Add
`fatigue_minimum_calendar_days` when fatigue should only count after a long
enough live window, not just after a few close-together synced points. For
fatigue-aware reads, pass the same embedded watch controls the app/API support:
`watch_group_by`, `watch_metric`, `watch_signal_focus`,
`watch_trajectory_focus`, `watch_coverage_focus`, `watch_minimum_points`,
`watch_minimum_calendar_days`, `watch_maximum_gap_days`, and `watch_limit`.
Responses default to `response_format: "concise"` with at most 24 matrix cells
to keep the result bounded. Set `response_format: "detailed"` explicitly for
the richer report fields, including `agent_context`. Both formats respect
`max_cells`; raise it (up to 200) when a larger matrix slice is needed.

```
{
  "brand_name": "Acme",
  "report_template": "next-tests",
  "rows": "visual_format",
  "columns": "messaging_angle",
  "metrics": "spend,ctr,thumbstop_rate,hook_rate,hold_rate,cpa",
  "response_format": "concise",
  "max_cells": 24
}
```

```json
{
  "brand_name": "Acme",
  "report_template": "demographic-read",
  "rows": "demographic_age",
  "columns": "demographic_gender",
  "metrics": "spend,roas,ctr,cpa,conversions,revenue",
  "roas_target": 2.5,
  "fatigue_minimum_calendar_days": 7,
  "watch_group_by": "hook_type",
  "watch_metric": "cpa",
  "watch_signal_focus": "fatigued",
  "watch_trajectory_focus": "worsening",
  "watch_coverage_focus": "windowed_history",
  "watch_minimum_points": 2,
  "watch_minimum_calendar_days": 7,
  "watch_maximum_gap_days": 7,
  "watch_limit": 5,
  "start_date": "2026-05-01",
  "end_date": "2026-05-31"
}
```

```json
{
  "brand_name": "Acme",
  "report_template": "audience-signals",
  "rows": "demographic_signal",
  "columns": "demographic_segment",
  "metrics": "spend,roas,ctr,cpa,conversions,revenue",
  "date_preset": "last_30_days"
}
```

```json
{
  "brand_name": "Acme",
  "rows": "messaging_angle",
  "columns": "demographic_segment",
  "status_focus": "all",
  "metrics": "spend,roas,ctr,cpa,conversions,revenue",
  "fatigue_minimum_calendar_days": 7,
  "date_preset": "last_30_days"
}
```

### `get_brain_learnings`
Read the auto-written Brand Brain learnings generated from performance memory,
strategy cells, taxonomy winners/watchouts, and audience signals. Returns a
hero learning, concise stories, and an `agent_context` payload for the next
brief or strategist prompt. Use `kinds` when an agent only wants a focused slice
such as `conclusion`, `working,audience`, or `watch`. Add
`conclusion_statuses` to narrow conclusion stories to `winner`, `fatigued`, or
`loser` outcomes only, and `conclusion_recency_days` to keep only the most
recent conclusion window. Use `watch_group_by`, `watch_metric`,
`watch_signal_focus`, `watch_trajectory_focus`, `watch_coverage_focus`,
`watch_minimum_points`, `watch_minimum_calendar_days`, `watch_sources`, and
`fatigue_decay_threshold` when the watchouts should be written from a different
fatigue lens such as fatigued-only CPA by ad type, weak taxonomy patterns only,
CTR by hook, or stable ROAS by `demographic_segment`.
```
{
  "brand_name": "Acme",
  "date_preset": "last_30_days",
  "minimum_spend": 500,
  "learning_spend": 1500,
  "kinds": "conclusion,watch",
  "conclusion_statuses": "winner,fatigued",
  "conclusion_recency_days": 21,
  "watch_group_by": "ad_type",
  "watch_metric": "cpa",
  "watch_signal_focus": "fatigued",
  "watch_trajectory_focus": "worsening",
  "watch_coverage_focus": "windowed_history",
  "watch_minimum_points": 3,
  "watch_minimum_calendar_days": 7,
  "watch_sources": "timeseries,patterns",
  "fatigue_decay_threshold": 0.25,
  "limit": 6
}
```

### `save_brain_learnings`
Persist the current auto-written Brand Brain learnings into saved Brain notes
for a brand, using the same filtering controls as `get_brain_learnings`. Use
this after reviewing a conclusion/working/watch/audience/gap slice when the
user wants the best current learnings saved back into reusable strategist context.
```
{
  "brand_name": "Acme",
  "date_preset": "last_30_days",
  "minimum_spend": 500,
  "learning_spend": 1500,
  "kinds": "conclusion,watch",
  "conclusion_statuses": "winner,fatigued",
  "conclusion_recency_days": 21,
  "watch_group_by": "ad_type",
  "watch_metric": "cpa",
  "watch_signal_focus": "fatigued",
  "watch_trajectory_focus": "worsening",
  "watch_coverage_focus": "windowed_history",
  "watch_minimum_points": 3,
  "watch_minimum_calendar_days": 7,
  "watch_sources": "timeseries,patterns",
  "include_gaps_in_notes": false,
  "limit": 6
}
```

### `get_performance_timeseries`
Read saved performance curves for fatigue checks without opening the dashboard.
Returns dated points plus a fatigue signal for each grouped series, using the
same decay threshold as Creative Tagger's strategy matrix. Group by creative,
campaign, landing page, `analysis_id`, or audience slices like
`demographic_age`, `demographic_gender`, `demographic_segment`, and
`demographic_signal`, and inspect metrics like ROAS, CPA, CTR, CPM, thumbstop,
completion rate, or funnel score. Use `signal_focus` when an agent only wants
the current fatigue watchlist or only stable controls, and `trajectory_focus`
when the agent wants only worsening, improving, flat, or insufficient-data
series. Use `coverage_focus` to isolate call-ready, gappy, short-window, or
windowed-history curves. Add `minimum_calendar_days` when fatigue should only
count after a trend has been live long enough, not just after a few
close-together points.
```
{
  "brand_name": "Acme",
  "date_preset": "last_30d",
  "group_by": "ad_name",
  "metric": "roas",
  "signal_focus": "fatigued",
  "trajectory_focus": "worsening",
  "coverage_focus": "call_ready",
  "minimum_spend": 500,
  "minimum_points": 3,
  "minimum_calendar_days": 7,
  "fatigue_decay_threshold": 0.18,
  "limit": 5
}
```

Use `date_preset` for a standard lookback window, or pass explicit
`start_date` / `end_date` to override it.

### `export_performance_timeseries_context`
Return the reusable `agent_context` payload from performance time series. Use
this when another agent needs the fatigue decision queue, summary text, action
mix, top groups, and prompt-ready export without carrying the full chart payload.
It accepts the same inputs as `get_performance_timeseries`.
```
{
  "brand_name": "Acme",
  "date_preset": "last_30d",
  "group_by": "ad_name",
  "metric": "roas",
  "signal_focus": "fatigued",
  "trajectory_focus": "worsening",
  "coverage_focus": "call_ready",
  "minimum_spend": 500,
  "minimum_points": 3,
  "minimum_calendar_days": 7,
  "fatigue_decay_threshold": 0.18,
  "limit": 5
}
```

Internal migration/backfill tools are hidden from the default published MCP
surface. They require `CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS=1` and should not
be used in customer flows or to avoid Meta approval.

### `get_meta_performance_summary`
Read saved Meta performance memory without triggering a sync.
```
{ "brand_name": "Acme" }
```
Returns account totals plus performance by standard taxonomy and brand-custom taxonomy.
Each aggregate can include `funnel_score` and a `funnel` explanation object for
capture -> hold -> bring-to-site -> convert diagnosis.

### `get_taxonomy_performance`
Find historical tag associations, under-observed tags, and standard taxonomy
values that have not been tested. Rows include ROAS, CTR, thumbstop, and funnel
scores when performance memory exists. These are observational comparisons;
validate a promising tag with a one-variable controlled test.
```
{ "brand_name": "Acme", "dimension": "hook_type", "spend_threshold": 500 }
```

### `get_prebuilt_reports`
Return ready-made Motion-style reports: best hooks, landing pages, messaging angles,
audiences, offers, CTAs, visual formats, and brand-custom values. Add
`start_date` / `end_date` when the report should only cover a specific synced
window.
```
{ "brand_name": "Acme", "report_id": "best_hooks", "limit": 8 }
{ "brand_name": "Acme", "report_id": "best_angles", "start_date": "2026-05-01", "end_date": "2026-05-31", "limit": 8 }
```

### `create_custom_report`
Build a custom report from selected standard or brand taxonomy dimensions and
rank the actual matched dimension combinations by ROAS, funnel score, spend,
CTR, or CPA. Use this for Motion-style views like best hook x landing page x
offer, founder x hook, audience x offer, or brand segment x product. Add
`start_date` and `end_date` when the report should isolate a specific test
window instead of the full synced history.
```
{
  "brand_name": "Acme",
  "dimensions": ["hook_type", "landing_page", "offer_type"],
  "layer": "all",
  "metric": "roas",
  "start_date": "2026-05-01",
  "end_date": "2026-05-31"
}
```
Rows can include `parts` and `values`, so the agent can explain a winning
combination instead of treating each tag independently.

### Saved custom reports
Save reusable report definitions, list them for a brand, rerun them by id, or
delete them when they are no longer needed. Saved reports can also persist a
custom `start_date` / `end_date` window for a specific launch or test period,
plus dashboard-style preset state such as `view_type`, `date_range`,
`group_by`, `metrics`, `filters`, `sort`, and `saved_metric_preset`.
Current chart view types are `table`, `bar`, `line`, and `pie`.
```
{
  "brand_name": "Acme",
  "name": "Hook + LP + Offer",
  "dimensions": ["hook_type", "landing_page", "offer_type"],
  "view_type": "table",
  "date_range": "custom",
  "group_by": "dimension",
  "metrics": ["spend", "roas", "cpa", "ctr"],
  "filters": [{"field": "status", "value": "winner"}],
  "sort": "desc",
  "saved_metric_preset": "delivery",
  "start_date": "2026-05-01",
  "end_date": "2026-05-31"
}
{ "brand_name": "Acme" }
{ "report_id": 7 }
```
Tools: `save_custom_report`, `list_custom_reports`, `run_saved_custom_report`,
`delete_custom_report`.

### `predict_creative`
Despite the legacy tool name, this is not a forecast. It compares a saved
analysis or draft attributes with the brand's historical tag-level performance
and returns an observational fit score plus explicit causal guardrails. Turn a
promising association into a falsifiable, one-variable controlled test with a
predeclared primary metric, minimum data, guardrails, and ship/stop criteria.
```
{ "brand_name": "Acme", "attributes": { "hook_type": "Question", "cta": "Shop Now" } }
```

### `get_demographics_performance`
Read age x gender delivery with account-relative higher and lower observed-
return-per-spend bands. These bands are descriptive associations, not audience
outcome or action verdicts. Use `date_preset` for a standard audience window,
or `start_date` / `end_date` to isolate a specific audience window.
```
{
  "brand_name": "Acme",
  "date_preset": "last_30_days",
  "start_date": "2026-05-01",
  "end_date": "2026-05-31"
}
```

### `export_demographics_context`
Return an agent-ready audience context payload from the saved demographics read.
Use this when another agent needs higher and lower observed-efficiency bands,
raw totals, per-segment mixed creative x audience views, and a prompt-ready
descriptive summary without the full wrapper. Outcome direction stays withheld
until an objective metric and direction are predeclared.
```
{
  "brand_name": "Acme",
  "date_preset": "last_30_days",
  "limit": 3
}
```

### `generate_brand_taxonomy`
Generate brand-specific messaging themes and intended audiences from the analyzed
creative library, then optionally save them to Brand Taxonomy Studio.
```
{ "brand_name": "Acme", "persist": true }
```

### `scan_competitor`
Classify a competitor's Meta Ad Library ads and get strategy breakdown.
```
{ "brand_name": "Acme", "page_name": "Hims & Hers", "limit": 25 }
```

Internal competitor-row backfill is also hidden from the default published MCP
surface. Customer-facing competitor intelligence should use `scan_competitor`
after native Meta Ad Library access is approved.

### `get_competitor_scan_history`
Read the saved Market scans/imports for a workspace without re-running Meta Ad
Library access. Useful when the agent needs the latest saved competitor hooks,
styles, or scan metadata before drafting briefs.
```
{ "brand_name": "Acme", "limit": 6 }
```

### `generate_naming`
Build naming strings from already-classified attributes (rarely needed — `analyze_creative` already includes naming).

## Architecture

```
Your AI agent  ←—stdio—→  creative-tagger-mcp  ←—HTTPS—→  api.creativetagger.ai
                                                              │
                                                              ├── Gemini 3.5 Flash (default classifier)
                                                              ├── Claude Sonnet 5 (configured fallback)
                                                              ├── SQLite (library + brand memory)
                                                              └── Meta Ad Library
```

You bring the agent. We provide the taxonomy, the memory, and the strategist.

## License

MIT
