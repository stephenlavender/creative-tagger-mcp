# Creative Tagger MCP Server

MCP (Model Context Protocol) server for [Creative Tagger](https://github.com/stephenlavender/creative-tagger) — structured creative intelligence for any ad format.

Enables any AI agent (Claude, Cursor, Windsurf, etc.) to analyze ad creatives against a standardized 28-dimension taxonomy.

## Quick Start

```bash
# Install
pip install creative-tagger-mcp

# Run (connects to local Creative Tagger API)
creative-tagger-mcp

# Or with a remote API
CREATIVE_TAGGER_URL=https://api.creativetagger.dev creative-tagger-mcp
```

## Add to Claude Code

```json
{
  "mcpServers": {
    "creative-tagger": {
      "command": "creative-tagger-mcp",
      "env": {
        "CREATIVE_TAGGER_URL": "http://localhost:8000"
      }
    }
  }
}
```

## Tools

### `analyze_creative`
Analyze any ad creative and get structured classification across 28 dimensions.

```
Input: { "file_path": "./ad.mp4", "brand_name": "Brand" }
   or: { "url": "https://example.com/landing-page", "brand_name": "Brand" }
   or: { "html_content": "<html>...</html>", "brand_name": "Brand" }
```

Returns: Full 28-dimension classification + naming conventions.

### `get_taxonomy`
Browse the complete taxonomy — all dimensions and enum values.

```
Input: {}                              # full taxonomy
Input: { "dimension": "hook_type" }    # specific dimension
```

### `generate_naming`
Generate naming convention strings from classified attributes.

```
Input: { "brand_name": "Brand", "hook_type": "UGC", "cta_type": "ShopNow", "aspect_ratio": "9:16" }
```

## 28 Taxonomy Dimensions

| # | Dimension | Values |
|---|-----------|--------|
| 1 | Format | video, image, carousel, landing_page, email, long_video |
| 2 | Aspect ratio | 9:16, 4:5, 1:1, 16:9, etc. |
| 3 | Video length | Micro, Short, Standard, Medium, Long, Extended, LongForm |
| 4 | Hook type | 18 values (UGC, ProdShot, Testi, ProbAg, etc.) |
| 5 | Hook style | 15 values (TalkingHead, VOBroll, GreenScreen, etc.) |
| 6 | Messaging angle | 20 values (ProbSol, BeforeAfter, Aspire, etc.) |
| 7 | CTA type | 20 values (ShopNow, LearnMore, TakeQuiz, etc.) |
| 8 | CTA placement | 9 values (EndCard, Verbal, Persistent, etc.) |
| 9 | Creative type | 24 values (Testimonial, ProdDemo, GRWM, etc.) |
| 10 | Talent type | 15 values (Creator, Founder, Expert, Celebrity, etc.) |
| 11 | Product presence | 10 values (Hero, Integrated, Reveal, etc.) |
| 12 | Offer type | 17 values (PctOff, FreeShip, BOGO, NoOffer, etc.) |
| 13 | Emotion | 15 values (Urgent, Curious, Trust, Humor, etc.) |
| 14 | Production type | 12 values (HighProd, LoFiUGC, MoGraph, AIGen, etc.) |
| 15 | Text overlay | 10 values (NoText, Subtitles, Kinetic, etc.) |
| 16 | Social proof | 16 values (StarRating, PressLogos, BAPhotos, etc.) |
| 17 | Brand presence | 9 values (LogoAll, LogoEnd, Unbranded, etc.) |
| 18 | Seasonality | 15 values (Evergreen, BFCM, Holiday, etc.) |
| 19-28 | Audio, copy style, user metadata | See full taxonomy |

## License

MIT
