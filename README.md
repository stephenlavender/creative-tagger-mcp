# Creative Tagger MCP Server

The MCP layer for [Creative Tagger](https://creativetagger.ai) — plug structured creative intelligence into any AI agent (Claude Desktop, Cursor, Windsurf, ChatGPT with MCP, etc.).

Your AI of choice gets:

- **Taxonomy** — 28 standardized dimensions for any ad creative (video, image, carousel, landing page, email)
- **Memory** — every analysis is saved to the user's library; the agent can search it, recall patterns, and pull individual results
- **Brand-custom taxonomy** — extend the standard taxonomy with each brand's founders, products, segments, aliases, and naming variables
- **Meta performance memory** — read-only Meta sync/status tools so agents can reason over winners and losers by taxonomy
- **Strategist** — recommendation + gap-analysis tools that reason over the user's library plus saved brand context (voice, audience, anti-patterns)
- **Competitive intelligence** — scan a competitor's Meta Ad Library and get classified strategy breakdowns

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

### `get_brand_taxonomy` / `set_brand_taxonomy_value` / `set_brand_entity`
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
```

### `get_meta_status` / `sync_meta_performance`
Check or trigger read-only Meta performance memory. No campaign creation, no budget edits.
```
{ "brand_name": "Acme", "date_preset": "last_30d" }
```

### `scan_competitor`
Classify a competitor's Meta Ad Library ads and get strategy breakdown.
```
{ "page_name": "Hims & Hers", "limit": 25 }
```

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
