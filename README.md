# Creative Tagger MCP Server

The MCP layer for [Creative Tagger](https://creativetagger.ai) — plug structured creative intelligence into any AI agent (Claude Desktop, Cursor, Windsurf, ChatGPT with MCP, etc.).

Your AI of choice gets:

- **Taxonomy** — 28 standardized dimensions for any ad creative (video, image, carousel, landing page, email)
- **Memory** — every analysis is saved to the user's library; the agent can search it, recall patterns, and pull individual results
- **Brand-custom taxonomy** — extend the standard taxonomy with each brand's founders, products, segments, aliases, and naming variables
- **Meta performance memory** — read-only Meta sync/status/tools so agents can reason over winners, unproven tags, demographic opportunities, and taxonomy gaps
- **Strategist** — recommendation + gap-analysis tools that reason over the user's library plus saved brand context (voice, audience, anti-patterns)
- **Competitive intelligence** — scan a competitor's Meta Ad Library, or import rows gathered by the user's own browser/CSV/MCP workflow while app approval is pending

## Positioning and Status

As of June 15, 2026, the shipped `main` branch and PyPI package `creative-tagger-mcp==0.2.0`
expose **36 public MCP tools**.

- The current customer-facing path is: analyze creatives, search library memory,
  customize brand taxonomy, sync read-only Meta performance, run reports, ask the
  strategist, scan competitors, and generate naming.
- `import_meta_performance` and `import_competitor_ads` are still part of the
  public tool surface on `main` today.
- Open PR [#1](https://github.com/stephenlavender/creative-tagger-mcp/pull/1)
  proposes gating those two import tools behind
  `CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS=1`, reducing the default public
  surface from 36 tools to 34. Until that PR merges, the wheel and source in
  this repo still expose the 36-tool surface.
- Public `llms.txt` copy is lagging behind the MCP/API surface today. The
  current tool names are `recommend`, `get_taxonomy_performance`, and
  `get_demographics_performance` rather than older aliases such as
  `strategist_recommend`, `get_tag_performance`, or `get_demographics`.

## Quick Start

```bash
# Install
pip install creative-tagger-mcp

# Run against production (default)
CREATIVE_TAGGER_API_KEY=ct_your_key creative-tagger-mcp

# Or against a local API
CREATIVE_TAGGER_URL=http://localhost:8000 \
CREATIVE_TAGGER_API_KEY=ct_your_key \
  creative-tagger-mcp
```

Get an API key at [app.creativetagger.ai](https://app.creativetagger.ai).

## Connect to Any MCP Client

Every MCP client needs the same runtime shape:

```json
{
  "command": "creative-tagger-mcp",
  "env": {
    "CREATIVE_TAGGER_URL": "https://api.creativetagger.ai",
    "CREATIVE_TAGGER_API_KEY": "ct_your_key_here"
  }
}
```

Use that command/env block in Claude Desktop, Cursor, Windsurf, ChatGPT, or any
other MCP-capable client. If you are pointing at production, `CREATIVE_TAGGER_URL`
is optional because it defaults to `https://api.creativetagger.ai`.

## Release Verification

Before publishing a new MCP version, build the artifacts and smoke-test the
wheel that will be uploaded to PyPI:

```bash
python -m build
python scripts/smoke_release.py
python -m twine check dist/*
```

The smoke test installs the wheel into a temporary virtualenv, verifies the
`creative-tagger-mcp` console entry point, checks the package version, and
confirms the V1 tool surface is present from the installed artifact.

## Publishing to PyPI

The release workflow publishes from GitHub Actions after it builds the package,
runs `scripts/smoke_release.py`, and passes `twine check`.

Recommended path:

```bash
git tag v0.2.0
git push origin v0.2.0
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
python -m twine check dist/*
python -m twine upload dist/*
```

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

The sections below describe the 36-tool public surface on `main` as of
June 15, 2026. If PR [#1](https://github.com/stephenlavender/creative-tagger-mcp/pull/1)
lands, the default public surface will hide `import_meta_performance` and
`import_competitor_ads` unless `CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS=1` is
set.

### `analyze_creative`
Analyze any ad creative and get structured classification across 28 dimensions.
```
{ "file_path": "./ad.mp4", "brand_name": "Brand" }
{ "url": "https://example.com/landing-page", "brand_name": "Brand" }
{ "html_content": "<html>...</html>", "brand_name": "Brand" }
```
Results auto-save to the user's library.

### `get_taxonomy`
Live fetch of the complete taxonomy or a single dimension.
```
{}                                # all 28 dimensions
{ "dimension": "hook_type" }      # one dimension
```

### `list_library`
Browse saved analyses. Search by filename or hook, filter by format.
```
{ "limit": 50, "search": "BFCM", "format": "video" }
```

### `get_library_patterns`
Cross-library pattern insights — concentration and diversity per dimension, plus rule-based diversification flags.

### `get_analysis`
Pull the full 28-dimension result for one library item.
```
{ "analysis_id": 42 }
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

### `get_meta_status` / `sync_meta_performance` / `import_meta_performance`
Check, trigger, or import read-only Meta performance memory. No campaign creation, no budget edits.
Rows can include video metrics (`video_plays`, `video_p50`, `video_p100`) so
Creative Tagger can derive thumbstop, retention, and funnel scores.
```
{ "brand_name": "Acme", "date_preset": "last_30d" }
```

Default customer flow: connect read-only Meta OAuth and call
`sync_meta_performance`. If the import-tool gating in PR
[#1](https://github.com/stephenlavender/creative-tagger-mcp/pull/1) merges,
`import_meta_performance` becomes an internal migration/backfill tool instead of
part of the default public surface.

Current `main` behavior still allows a user to connect Meta through their own
Meta MCP/CLI and use `import_meta_performance` to hand rows back to Creative
Tagger:
```
{
  "brand_name": "Acme",
  "source": "meta_mcp",
  "rows": [{ "ad_name": "ACME_Static_Hook_V1", "spend": 100, "impressions": 5000 }]
}
```

### `get_meta_performance_summary`
Read saved Meta performance memory without triggering a sync.
```
{ "brand_name": "Acme" }
```
Returns account totals plus performance by standard taxonomy and brand-custom taxonomy.
Each aggregate can include `funnel_score` and a `funnel` explanation object for
capture -> hold -> bring-to-site -> convert diagnosis.

### `get_taxonomy_performance`
Find which tags scale, which are unproven, and which standard taxonomy values have
not been tested yet. Rows include ROAS, CTR, thumbstop, and funnel scores when
performance memory exists.
```
{ "brand_name": "Acme", "dimension": "hook_type", "spend_threshold": 500 }
```

### `get_prebuilt_reports`
Return ready-made Motion-style reports: best hooks, landing pages, messaging angles,
audiences, offers, CTAs, visual formats, and brand-custom values.
```
{ "brand_name": "Acme", "report_id": "best_hooks", "limit": 8 }
```

### `create_custom_report`
Build a custom report from selected standard or brand taxonomy dimensions and
rank the actual matched dimension combinations by ROAS, funnel score, spend,
CTR, or CPA. Use this for Motion-style views like best hook x landing page x
offer, founder x hook, audience x offer, or brand segment x product.
```
{
  "brand_name": "Acme",
  "dimensions": ["hook_type", "landing_page", "offer_type"],
  "layer": "all",
  "metric": "roas"
}
```
Rows can include `parts` and `values`, so the agent can explain a winning
combination instead of treating each tag independently.

### Saved custom reports
Save reusable report definitions, list them for a brand, rerun them by id, or
delete them when they are no longer needed.
```
{ "brand_name": "Acme", "name": "Hook + LP + Offer", "dimensions": ["hook_type", "landing_page", "offer_type"] }
{ "brand_name": "Acme" }
{ "report_id": 7 }
```
Tools: `save_custom_report`, `list_custom_reports`, `run_saved_custom_report`,
`delete_custom_report`.

### `predict_creative`
Score a saved analysis or draft attributes before it spends, using the brand's
own performance memory. Returns a fit score, per-tag ratings, and recommended
swaps.
```
{ "brand_name": "Acme", "attributes": { "hook_type": "Question", "cta": "Shop Now" } }
```

### `get_demographics_performance`
Read age x gender performance memory with opportunity and waste flags.
```
{ "brand_name": "Acme" }
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
{ "page_name": "Hims & Hers", "limit": 25 }
```

### `import_competitor_ads`
Import competitor Meta Ad Library rows gathered outside Creative Tagger. Use
this when the user's own browser, CSV export, CLI, or Meta MCP can access the
rows before Creative Tagger's native Meta Ad Library token/app approval is
available.
```
{
  "competitor_name": "Rival Brand",
  "ads": [
    {
      "ad_id": "manual-1",
      "page_name": "Rival Brand",
      "primary_text": "Founder story import hook",
      "headline": "Starter kit",
      "platforms": "instagram",
      "spend": "$100 - $499"
    }
  ]
}
```
Returns normalized ads, optional joined analyses, and the same aggregate
strategy breakdown as `scan_competitor`.

Default customer flow is still `scan_competitor`. If PR
[#1](https://github.com/stephenlavender/creative-tagger-mcp/pull/1) merges,
`import_competitor_ads` becomes an internal backfill/migration tool unless
`CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS=1` is set.

### `generate_naming`
Build naming strings from already-classified attributes (rarely needed — `analyze_creative` already includes naming).

## Architecture

```
Your AI agent  ←—stdio—→  creative-tagger-mcp  ←—HTTPS—→  api.creativetagger.ai
                                                              │
                                                              ├── Gemini 2.5 Flash (classifier)
                                                              ├── Claude Sonnet (fallback)
                                                              ├── SQLite (library + brand memory)
                                                              └── Meta Ad Library
```

You bring the agent. We provide the taxonomy, the memory, and the strategist.

## License

MIT
