"""Creative Tagger MCP Server.

Exposes the Creative Tagger API as MCP tools so any AI agent (Claude Desktop,
Cursor, Windsurf, ChatGPT with MCP, etc.) can:

- Analyze ad creatives across 28 taxonomy dimensions
- Browse and search the user's creative library (memory)
- Get strategist recommendations grounded in library + brand context
- Set brand voice / audience / top performers / anti-patterns
- Scan competitor ads from the Meta Ad Library
- Generate V1-compatible standard naming conventions locally

Usage:
    creative-tagger-mcp
    CREATIVE_TAGGER_URL=https://api.creativetagger.ai \\
    CREATIVE_TAGGER_API_KEY=ct_xxx creative-tagger-mcp
"""

import json
import os
from pathlib import Path
from typing import Any, BinaryIO

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_URL = os.environ.get("CREATIVE_TAGGER_URL", "https://api.creativetagger.ai")
API_KEY = os.environ.get("CREATIVE_TAGGER_API_KEY", "")
INTERNAL_BACKFILL_TOOLS = {"import_meta_performance", "import_competitor_ads"}

server = Server("creative-tagger")


def _headers() -> dict:
    h = {}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def _auth_params() -> dict:
    """Some endpoints take api_key as a query param rather than header."""
    return {"api_key": API_KEY} if API_KEY else {}


def _text(payload: Any) -> list[TextContent]:
    """Wrap any JSON-able payload as a TextContent response."""
    if isinstance(payload, str):
        return [TextContent(type="text", text=payload)]
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "default"}:
            return default
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def _csv_arg(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item or "").strip()]
        return ",".join(parts)
    return str(value).strip()


def _string_list_arg(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [item.strip() for item in value.split(",") if item.strip()]
        return parts or None
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item or "").strip()]
        return parts or None
    text = str(value).strip()
    return [text] if text else None


def _infer_strategy_template(
    report_template: Any,
    *,
    rows: Any,
    columns: Any,
) -> str:
    demographic_dimensions = {
        "demographic_age",
        "demographic_gender",
        "demographic_segment",
        "demographic_signal",
    }
    explicit = str(report_template or "").strip()
    if explicit:
        return explicit
    row_value = str(rows or "").strip()
    col_value = str(columns or "").strip()
    if not row_value or not col_value:
        return ""
    demographic_axes = [
        axis
        for axis in (row_value, col_value)
        if axis in demographic_dimensions
    ]
    if len(demographic_axes) == 2:
        pair = frozenset(demographic_axes)
        if pair == {"demographic_segment", "demographic_signal"}:
            return "audience-signals"
        return "demographic-read"
    if len(demographic_axes) == 1:
        creative_axis = next(
            (
                axis
                for axis in (row_value, col_value)
                if axis not in demographic_dimensions
            ),
            "",
        )
        if creative_axis == "messaging_angle":
            return "angle-audience-fit"
        if creative_axis == "hook":
            return "hook-audience-fit"
        return ""
    return ""


def _strategy_params(args: dict) -> dict[str, Any]:
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "date_preset": args.get("date_preset", "all_time"),
        "start_date": args.get("start_date", ""),
        "end_date": args.get("end_date", ""),
        "limit": args.get("limit", 10),
    }
    report_template = _infer_strategy_template(
        args.get("report_template"),
        rows=args.get("rows"),
        columns=args.get("columns"),
    )
    if report_template:
        params["report_template"] = report_template
    for key in ("rows", "columns", "status_focus", "metric_preset"):
        if args.get(key) not in (None, ""):
            params[key] = args[key]
    metrics = _csv_arg(args.get("metrics"))
    if metrics:
        params["metrics"] = metrics
    for key in (
        "cpa_target",
        "roas_target",
        "minimum_spend",
        "learning_spend",
        "fatigue_minimum_calendar_days",
        "watch_group_by",
        "watch_metric",
        "watch_signal_focus",
        "watch_trajectory_focus",
        "watch_coverage_focus",
        "watch_minimum_points",
        "watch_minimum_calendar_days",
        "watch_maximum_gap_days",
        "watch_limit",
    ):
        if args.get(key) is not None:
            params[key] = args[key]
    return params


def _is_internal_backfill_enabled() -> bool:
    return os.environ.get("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", "") == "1"


def _visible_tools(tools: list[Tool]) -> list[Tool]:
    if _is_internal_backfill_enabled():
        return tools
    return [tool for tool in tools if tool.name not in INTERNAL_BACKFILL_TOOLS]


# ---------- Tools ----------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _visible_tools([
        Tool(
            name="analyze_creative",
            description=(
                "Analyze any ad creative (image, video, carousel, landing page, email) "
                "and return structured classification across 28 taxonomy dimensions: "
                "hook type, messaging angle, creative type, visual style, talent, CTA, "
                "emotion, production type, offer type, social proof, brand presence, "
                "seasonality, audio attributes, and more. Also generates standardized "
                "naming conventions. Provide one of: file_path, url, or html_content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local file path to analyze (image or video)",
                    },
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Multiple local image paths to analyze as a carousel. "
                            "Posts them to the API's `files` field."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "URL to analyze. Direct file URL (image/video) or landing page URL."
                        ),
                    },
                    "html_content": {
                        "type": "string",
                        "description": "Raw HTML for email creative analysis",
                    },
                    "brand_name": {
                        "type": "string",
                        "description": "Brand name for naming conventions",
                        "default": "Brand",
                    },
                    "version": {
                        "type": "integer",
                        "default": 1,
                        "description": "Naming convention version number",
                    },
                    "format": {
                        "type": "string",
                        "description": (
                            "Optional explicit format: image, video, long_video, "
                            "carousel, landing_page, or email."
                        ),
                    },
                    "include_transcript": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include transcript for video analysis",
                    },
                    "forensic_mode": {
                        "type": "boolean",
                        "default": False,
                        "description": "Request first-3-second forensic frame extraction for video",
                    },
                },
                "oneOf": [
                    {"required": ["file_path"]},
                    {"required": ["file_paths"]},
                    {"required": ["url"]},
                    {"required": ["html_content"]},
                ],
            },
        ),
        Tool(
            name="get_taxonomy",
            description=(
                "Get the complete Creative Tagger taxonomy — all 28 dimensions with "
                "every enum value. Pulled live from the API so it's always current. "
                "Use this before analyze_creative when you want to know the full "
                "vocabulary the system understands."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": (
                            "Optional: fetch one dimension only "
                            "(e.g. 'hook_type', 'messaging_angle')."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="list_library",
            description=(
                "Browse the authenticated user's saved analysis library (memory). "
                "Every analyze_creative call is automatically saved. Use this to "
                "recall what has been analyzed before — search by filename, hook, "
                "angle, emotion, CTA, talent, offer, audio, season, or format, "
                "then sort by recency or joined performance (spend, reach, "
                "frequency, ROAS, CTR, CPM, CPA)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                    "search": {
                        "type": "string",
                        "description": "Search filename, naming, hook, or creative type",
                    },
                    "format": {
                        "type": "string",
                        "description": "Filter by format: video, image, carousel, landing_page, email, long_video",
                    },
                    "hook": {
                        "type": "string",
                        "description": "Filter by hook type (UGC, Demo, TalkHead, etc.)",
                    },
                    "angle": {
                        "type": "string",
                        "description": "Filter by messaging angle",
                    },
                    "emotion": {
                        "type": "string",
                        "description": "Filter by emotion",
                    },
                    "cta": {
                        "type": "string",
                        "description": "Filter by CTA",
                    },
                    "talent": {
                        "type": "string",
                        "description": "Filter by talent classification",
                    },
                    "offer": {
                        "type": "string",
                        "description": "Filter by offer type",
                    },
                    "audio": {
                        "type": "string",
                        "description": "Filter by audio type",
                    },
                    "season": {
                        "type": "string",
                        "description": "Filter by seasonality",
                    },
                    "sort": {
                        "type": "string",
                        "default": "recent",
                        "enum": [
                            "recent",
                            "spend",
                            "reach",
                            "roas",
                            "ctr",
                            "frequency",
                            "cpm",
                            "cpa",
                        ],
                        "description": (
                            "Sort by recent, spend, reach, roas, ctr, frequency, "
                            "cpm, or cpa. Performance sorts use joined Meta "
                            "performance when it exists."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="get_library_patterns",
            description=(
                "Get pattern insights across the user's entire library: which hooks, "
                "angles, creative types, emotions they over- or under-index on. "
                "Returns top values per dimension with percentages plus "
                "rule-based diversification insights. Use this for portfolio analysis "
                "before recommending what to make next."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_analysis",
            description=(
                "Get the full analysis result for a single saved library item by ID. "
                "Use after list_library when you need the complete 28-dimension classification "
                "(list_library returns a summary; this returns the full JSON)."
            ),
            inputSchema={
                "type": "object",
                "required": ["analysis_id"],
                "properties": {
                    "analysis_id": {
                        "type": "integer",
                        "description": "ID of the analysis to fetch",
                    },
                },
            },
        ),
        Tool(
            name="recommend",
            description=(
                "Ask the Creative Strategist a question, grounded in the user's library + "
                "brand context. The strategist auto-loads patterns from prior analyses "
                "and any saved brand voice/audience/anti-patterns for the brand, then "
                "answers with concrete creative recommendations using taxonomy values. "
                "Use this for open-ended strategic questions ('what should I test next', "
                "'how should I approach Q4', 'what kind of UGC would work for this audience')."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "question"],
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": "Brand to ground the recommendation in",
                    },
                    "question": {
                        "type": "string",
                        "description": "The strategic question",
                    },
                },
            },
        ),
        Tool(
            name="analyze_gaps",
            description=(
                "Identify gaps in the user's creative library for a given brand and "
                "propose concrete next creatives that fill them. Surfaces concentration "
                "risk (e.g., 78% UGC TalkHead) and recommends under-represented hook "
                "types, messaging angles, creative types. Returns JSON with gap analysis + "
                "ready-to-produce briefs."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": "Brand to analyze",
                    },
                },
            },
        ),
        Tool(
            name="get_brand_context",
            description=(
                "Retrieve the saved brand context for a brand: voice, target audience, "
                "top performers, anti-patterns, and notes. Strategist tools auto-include "
                "this; this tool exposes the raw stored context."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                },
            },
        ),
        Tool(
            name="set_brand_context",
            description=(
                "Create or update brand context for a brand. Stored per-user. This is "
                "the brand's long-term memory — voice, audience, what works, what to "
                "avoid. Future strategist and brief calls automatically include this "
                "context. Upserts on (user, brand_name)."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "voice": {
                        "type": "string",
                        "description": "Brand voice / tone (e.g., 'clinical, precise')",
                    },
                    "target_audience": {
                        "type": "string",
                        "description": "Who the brand is for",
                    },
                    "top_performers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Patterns/elements that work for this brand",
                    },
                    "anti_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Patterns/elements to avoid for this brand",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Free-form additional context",
                    },
                },
            },
        ),
        Tool(
            name="get_brand_taxonomy",
            description=(
                "Retrieve the brand-custom taxonomy for a brand: custom values, aliases, "
                "and entities such as founder, recurring creators, products, offers, "
                "customer segments, ICPs, and campaign labels. Standard taxonomy still "
                "lives in attributes; this is the brand-specific extension layer."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                },
            },
        ),
        Tool(
            name="set_brand_taxonomy_value",
            description=(
                "Create or update one brand-specific allowed value for an existing "
                "Creative Tagger dimension, with aliases. Example: dimension=talent, "
                "value='Stephen Lavender / Founder', aliases=['Stephen','founder']."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "dimension", "value"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "dimension": {"type": "string"},
                    "value": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        Tool(
            name="delete_brand_taxonomy_value",
            description=(
                "Delete one brand-specific taxonomy value by brand, dimension, and "
                "canonical value. Use this to prune stale founders, segments, offers, "
                "or internal labels from Brand Taxonomy Studio."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "dimension", "value"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "dimension": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        ),
        Tool(
            name="set_brand_entity",
            description=(
                "Create or update a prompt/entity-based brand entity to recognize in "
                "creative analysis: founder, creator, customer, spokesperson, product, "
                "offer, customer_segment, icp, or campaign_label."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "entity_type", "name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        Tool(
            name="delete_brand_entity",
            description=(
                "Delete one brand entity by brand, entity_type, and canonical name. "
                "Useful when a creator, product, offer, customer segment, ICP, or "
                "campaign label should no longer be recognized for the brand."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "entity_type", "name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "entity_type": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
        ),
        Tool(
            name="get_naming_variables",
            description=(
                "List every variable available in saved naming templates, including "
                "standard taxonomy fields plus brand-custom variables like founder, "
                "product, offer, customer_segment, icp, and campaign_label."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_naming_templates",
            description=(
                "List the authenticated user's saved naming templates. Templates are "
                "applied automatically to future analyze_creative results."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="save_naming_template",
            description=(
                "Create or update a saved naming template using {variable} placeholders. "
                "Supports standard taxonomy fields and brand-custom variables."
            ),
            inputSchema={
                "type": "object",
                "required": ["template"],
                "properties": {
                    "template": {
                        "type": "string",
                        "description": (
                            "Template such as "
                            "{brand}_{founder}_{hook_type}_{cta}_{ratio}_{version}"
                        ),
                    },
                    "name": {"type": "string", "default": "default"},
                    "separator": {"type": "string", "default": "_"},
                },
            },
        ),
        Tool(
            name="delete_naming_template",
            description="Delete a saved naming template by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "default": "default"},
                },
            },
        ),
        Tool(
            name="preview_naming_template",
            description=(
                "Preview a naming template with sample taxonomy values before saving it."
            ),
            inputSchema={
                "type": "object",
                "required": ["template"],
                "properties": {
                    "template": {"type": "string"},
                    "name": {"type": "string", "default": "default"},
                    "separator": {"type": "string", "default": "_"},
                },
            },
        ),
        Tool(
            name="get_meta_status",
            description=(
                "Check whether read-only Meta performance sync is connected for the "
                "authenticated user. Returns account id, scopes, read-only status, "
                "and latest sync metadata."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="sync_meta_performance",
            description=(
                "Trigger a read-only Meta ads performance sync for a brand. Syncs ad "
                "performance rows and reports summaries by standard and brand-custom "
                "taxonomy values. Supports explicit attribution/lookback windows so "
                "agents can match the buyer's Ads Manager view. Does not create "
                "campaigns or edit budgets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "account_id": {"type": "string"},
                    "date_preset": {"type": "string", "default": "last_30d"},
                    "attribution_windows": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional Meta attribution/lookback windows such as "
                            "7d_click and 1d_view. Defaults to Meta's standard "
                            "7d_click + 1d_view reporting if omitted."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="import_meta_performance",
            description=(
                "Import Meta-style performance rows for internal backfills or "
                "controlled migrations. The launch customer flow should use native "
                "Creative Tagger Meta OAuth plus sync_meta_performance. Does not "
                "create campaigns or edit budgets."
            ),
            inputSchema={
                "type": "object",
                "required": ["rows"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Rows with ad_name/ad_id/spend/impressions/clicks/"
                            "conversions/revenue/date fields. Video metrics such as "
                            "video_plays, video_p50, and video_p100 are used for "
                            "thumbstop, retention, and funnel scoring."
                        ),
                    },
                    "source": {"type": "string", "default": "meta_mcp"},
                },
            },
        ),
        Tool(
            name="get_meta_performance_summary",
            description=(
                "Read the saved Meta performance memory for a brand without triggering "
                "a sync. Returns totals plus winners/losers by standard taxonomy and "
                "brand-custom taxonomy values, including explainable funnel_score "
                "signals for capture, hold, bring-to-site, and convert stages. "
                "Supports all_time, last_7_days, last_30_days, last_90_days, or "
                "custom date windows."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "all_time",
                        "description": "Optional date window preset: all_time, last_7_days, last_30_days, last_90_days, or custom",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                },
            },
        ),
        Tool(
            name="get_taxonomy_performance",
            description=(
                "Return tag-level performance with significance gating and coverage "
                "gaps. Use this to find which taxonomy values scale, which are "
                "unproven, and which standard values have never been tried. Rows include "
                "ROAS, CTR, thumbstop, and funnel_score when performance memory exists. "
                "Supports the same date presets as the main performance summary."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "dimension": {
                        "type": "string",
                        "description": "Optional dimension filter, e.g. hook_type",
                    },
                    "spend_threshold": {
                        "type": "number",
                        "default": 500,
                        "description": "Spend floor before a tag is treated as proven",
                    },
                    "date_preset": {
                        "type": "string",
                        "default": "all_time",
                        "description": "Optional date window preset: all_time, last_7_days, last_30_days, last_90_days, or custom",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                },
            },
        ),
        Tool(
            name="get_prebuilt_reports",
            description=(
                "Return Motion-style prebuilt creative reports for a brand: best hooks, "
                "landing pages, messaging angles, audiences, offers, CTAs, visual formats, "
                "and brand-custom values. Rows include ROAS, spend, CTR, thumbstop, "
                "and funnel_score when performance memory exists. Optional start_date/"
                "end_date (YYYY-MM-DD) scope the report window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "report_id": {
                        "type": "string",
                        "description": (
                            "Optional report filter, e.g. best_hooks, best_landing_pages, "
                            "best_angles, best_audiences, best_offers"
                        ),
                    },
                    "spend_threshold": {
                        "type": "number",
                        "default": 500,
                        "description": "Spend floor before a row is treated as proven",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 8,
                        "description": "Rows per report",
                    },
                },
            },
        ),
        Tool(
            name="get_creative_strategy_report",
            description=(
                "Return the strategist matrix for deciding what to test next on Meta. "
                "Defaults to ad_type rows by messaging_angle columns, with text and "
                "color-coded states for next tests, live learning, winners, losers, "
                "fatigue, and gaps. Also supports audience-mode matrices with "
                "demographic_age, demographic_gender, demographic_segment, and "
                "demographic_signal axes, plus mixed creative x audience reads such "
                "as messaging_angle by demographic_segment. Includes the decision "
                "queue, report table, and agent_context payload so an LLM can brief "
                "next tests from the same source of truth as the Creative Tagger UI. "
                "Supports CTR, thumbstop, hook, hold, video milestone, CPA, CVR, "
                "ROAS, revenue, spend, and funnel metrics."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "all_time",
                        "description": "Optional date window preset: all_time, last_7_days, last_30_days, last_90_days, or custom",
                    },
                    "report_template": {
                        "type": "string",
                        "default": "next-tests",
                        "description": (
                            "Template preset: next-tests, creative-winners, fatigue-watch, "
                            "coverage-gaps, hook-performance, persona-read, demographic-read, "
                            "or audience-signals. You can also skip the preset and request a "
                            "mixed creative x audience cut via rows/columns such as "
                            "messaging_angle by demographic_segment."
                        ),
                    },
                    "rows": {
                        "type": "string",
                        "default": "ad_type",
                        "description": (
                            "Matrix row dimension, e.g. ad_type, messaging_angle, format, "
                            "hook, persona, offer_type, demographic_age, demographic_gender, "
                            "demographic_segment, or demographic_signal. Combine a creative "
                            "dimension here with a demographic column for mixed audience reads."
                        ),
                    },
                    "columns": {
                        "type": "string",
                        "default": "messaging_angle",
                        "description": (
                            "Matrix column dimension, e.g. messaging_angle, ad_type, format, "
                            "hook, persona, offer_type, demographic_gender, demographic_age, "
                            "demographic_segment, or demographic_signal. Set one axis to a "
                            "creative tag and the other to a demographic axis for a mixed "
                            "creative x audience matrix."
                        ),
                    },
                    "status_focus": {
                        "type": "string",
                        "default": "all",
                        "description": "all, next, winner, learning, fatigued, loser, untested",
                    },
                    "metrics": {
                        "type": "string",
                        "default": "spend,ctr,thumbstop_rate,hook_rate,hold_rate,cpa",
                        "description": (
                            "Comma-separated metrics to show in each cell, e.g. spend,ctr,"
                            "thumbstop_rate,hook_rate,hold_rate,cpa"
                        ),
                    },
                    "metric_preset": {
                        "type": "string",
                        "description": "Optional metric preset key: diagnostics, conversion, delivery, video, scale, or all",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                    "cpa_target": {"type": "number"},
                    "roas_target": {"type": "number"},
                    "minimum_spend": {"type": "number"},
                    "learning_spend": {"type": "number"},
                    "fatigue_minimum_calendar_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum elapsed calendar days before a fatigue read is treated as meaningful",
                    },
                    "watch_group_by": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Optional fatigue watch grouping for the strategy report: "
                            "ad_name, campaign_name, landing_page_domain, analysis_id, "
                            "hook_type, messaging_angle, ad_type, format, visual_style, cta, emotion, "
                            "demographic_age, demographic_gender, demographic_segment, or demographic_signal"
                        ),
                    },
                    "watch_metric": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Optional fatigue watch metric for the strategy report: "
                            "roas, cpa, ctr, spend, hook_rate, hold_rate, thumbstop_rate, "
                            "or demographic-safe metrics such as conversions and revenue"
                        ),
                    },
                    "watch_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional fatigue watch signal filter for the strategy report: "
                            "all, fatigued, stable, or insufficient_data"
                        ),
                    },
                    "watch_trajectory_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional fatigue watch trend filter for the strategy report: "
                            "all, worsening, improving, flat, or insufficient_data"
                        ),
                    },
                    "watch_coverage_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional coverage-risk filter for the strategy report watch: "
                            "all, call_ready, gappy, insufficient_points, short_window, or windowed_history"
                        ),
                    },
                    "watch_minimum_points": {
                        "type": "integer",
                        "default": 2,
                        "description": "Minimum observed timeseries points before a fatigue watch group is eligible",
                    },
                    "watch_minimum_calendar_days": {
                        "type": "integer",
                        "description": "Optional elapsed calendar-day gate for fatigue watch groups; defaults to the report fatigue cadence gate",
                    },
                    "watch_maximum_gap_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Maximum sync gap in calendar days before a fatigue watch group is eligible",
                    },
                    "watch_limit": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum fatigue watch groups to rank in the strategy report",
                    },
                    "limit": {"type": "integer", "default": 10},
                },
            },
        ),
        Tool(
            name="get_brain_learnings",
            description=(
                "Return auto-written Brand Brain learnings from saved performance, "
                "strategy, taxonomy, and audience data. Use this when an agent needs "
                "the current test conclusions, working patterns, watchouts, audience "
                "opportunities, fatigue, and gap learnings plus an agent_context "
                "brief seed. Supports focused reads like conclusion-only, "
                "working-only, or audience-only learnings, including audience "
                "fatigue reads grouped by demographic_age, demographic_gender, "
                "demographic_segment, or demographic_signal. Audience filters can "
                "also isolate opportunities-only or waste-only learnings, and "
                "watch_coverage_focus can isolate call-ready, gappy, short-window, "
                "insufficient-point, or windowed-history time-series reads."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "all_time",
                        "description": "Optional date window preset: all_time, last_7_days, last_30_days, last_90_days, or custom",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                    "minimum_spend": {
                        "type": "number",
                        "description": "Spend floor before a pattern is treated as significant",
                    },
                    "learning_spend": {
                        "type": "number",
                        "description": "Spend target before a cell graduates from live learning",
                    },
                    "cpa_target": {"type": "number"},
                    "roas_target": {"type": "number"},
                    "watch_group_by": {
                        "type": "string",
                        "default": "messaging_angle",
                        "description": (
                            "Timeseries grouping for watch/fatigue learnings: "
                            "ad_name, campaign_name, landing_page_domain, analysis_id, "
                            "hook_type, messaging_angle, ad_type, format, visual_style, cta, emotion, "
                            "demographic_age, demographic_gender, demographic_segment, or demographic_signal"
                        ),
                    },
                    "watch_metric": {
                        "type": "string",
                        "default": "roas",
                        "description": (
                            "Timeseries metric used for watch/fatigue learnings: "
                            "roas, cpa, ctr, cpm, thumbstop_rate, video_completion_rate, funnel_score, etc."
                        ),
                    },
                    "watch_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional signal filter for watch/fatigue learnings: "
                            "all, fatigued, stable, or insufficient_data"
                        ),
                    },
                    "watch_trajectory_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional trend filter for watch/fatigue learnings: "
                            "all, worsening, improving, flat, or insufficient_data"
                        ),
                    },
                    "watch_coverage_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional coverage-risk filter for watch/fatigue learnings: "
                            "all, call_ready, gappy, insufficient_points, short_window, or windowed_history"
                        ),
                    },
                    "watch_minimum_points": {
                        "type": "integer",
                        "default": 2,
                        "description": "Minimum observed timeseries points before a watch group is eligible",
                    },
                    "watch_minimum_calendar_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum elapsed calendar days before a watch group is eligible",
                    },
                    "watch_maximum_gap_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Maximum sync gap in calendar days before a watch group is eligible",
                    },
                    "watch_sources": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated watch sources: timeseries, "
                            "strategy, patterns, or all"
                        ),
                    },
                    "fatigue_decay_threshold": {
                        "type": "number",
                        "default": 0.18,
                        "description": "Decay threshold that flips a watch trend to fatigued",
                    },
                    "kinds": {
                        "type": "string",
                        "description": "Optional comma-separated kinds: conclusion, working, watch, audience, gap, or all",
                    },
                    "conclusion_statuses": {
                        "type": "string",
                        "description": "Optional comma-separated conclusion statuses when kinds includes conclusion: winner, fatigued, loser, or all",
                    },
                    "audience_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": "Optional audience signal filter when kinds includes audience: all, opportunity, or waste",
                    },
                    "audience_limit": {
                        "type": "integer",
                        "default": 3,
                        "description": "Maximum audience learning stories to return when audience signals are included",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 8,
                        "description": "Maximum learning stories to return",
                    },
                },
            },
        ),
        Tool(
            name="save_brain_learnings",
            description=(
                "Persist the current auto-written Brand Brain learnings into saved "
                "Brand Brain notes for a brand. Use this after reviewing a filtered "
                "learning set when the user wants those conclusions, working "
                "patterns, watchouts, audience signals, or gaps saved as reusable "
                "strategist context, including audience watchouts grouped by "
                "demographic_age, demographic_gender, demographic_segment, or "
                "demographic_signal. Audience filters can isolate opportunities-only "
                "or waste-only learnings before saving, and watch_coverage_focus can "
                "save only gappy, short-window, insufficient-point, or "
                "windowed-history watchouts."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "all_time",
                        "description": "Optional date window preset: all_time, last_7_days, last_30_days, last_90_days, or custom",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                    "minimum_spend": {
                        "type": "number",
                        "description": "Spend floor before a pattern is treated as significant",
                    },
                    "learning_spend": {
                        "type": "number",
                        "description": "Spend target before a cell graduates from live learning",
                    },
                    "cpa_target": {"type": "number"},
                    "roas_target": {"type": "number"},
                    "watch_group_by": {
                        "type": "string",
                        "default": "messaging_angle",
                        "description": (
                            "Timeseries grouping for watch/fatigue learnings: "
                            "ad_name, campaign_name, landing_page_domain, analysis_id, "
                            "hook_type, messaging_angle, ad_type, format, visual_style, cta, emotion, "
                            "demographic_age, demographic_gender, demographic_segment, or demographic_signal"
                        ),
                    },
                    "watch_metric": {
                        "type": "string",
                        "default": "roas",
                        "description": (
                            "Timeseries metric used for watch/fatigue learnings: "
                            "roas, cpa, ctr, cpm, thumbstop_rate, video_completion_rate, funnel_score, etc."
                        ),
                    },
                    "watch_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional signal filter for watch/fatigue learnings: "
                            "all, fatigued, stable, or insufficient_data"
                        ),
                    },
                    "watch_trajectory_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional trend filter for watch/fatigue learnings: "
                            "all, worsening, improving, flat, or insufficient_data"
                        ),
                    },
                    "watch_coverage_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional coverage-risk filter for watch/fatigue learnings: "
                            "all, call_ready, gappy, insufficient_points, short_window, or windowed_history"
                        ),
                    },
                    "watch_minimum_points": {
                        "type": "integer",
                        "default": 2,
                        "description": "Minimum observed timeseries points before a watch group is eligible",
                    },
                    "watch_minimum_calendar_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum elapsed calendar days before a watch group is eligible",
                    },
                    "watch_maximum_gap_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Maximum sync gap in calendar days before a watch group is eligible",
                    },
                    "watch_sources": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated watch sources: timeseries, "
                            "strategy, patterns, or all"
                        ),
                    },
                    "fatigue_decay_threshold": {
                        "type": "number",
                        "default": 0.18,
                        "description": "Decay threshold that flips a watch trend to fatigued",
                    },
                    "kinds": {
                        "type": "string",
                        "description": "Optional comma-separated kinds: conclusion, working, watch, audience, gap, or all",
                    },
                    "conclusion_statuses": {
                        "type": "string",
                        "description": "Optional comma-separated conclusion statuses when kinds includes conclusion: winner, fatigued, loser, or all",
                    },
                    "audience_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": "Optional audience signal filter when kinds includes audience: all, opportunity, or waste",
                    },
                    "audience_limit": {
                        "type": "integer",
                        "default": 3,
                        "description": "Maximum audience learning stories to persist when audience signals are included",
                    },
                    "include_gaps_in_notes": {
                        "type": "boolean",
                        "default": False,
                        "description": "Keep gap learnings in the persisted notes block",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 8,
                        "description": "Maximum learning stories to persist",
                    },
                },
            },
        ),
        Tool(
            name="export_brain_learnings_context",
            description=(
                "Return the reusable agent_context payload from auto-written Brand "
                "Brain learnings. Use this when another agent or workflow needs a "
                "brief-ready prompt seed plus the filtered learnings, evidence "
                "thresholds, saved Brand Brain context, and active watch or audience "
                "filters without the full response wrapper, including "
                "watch_coverage_focus for time-series sync-quality reads."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "all_time",
                        "description": "Optional date window preset: all_time, last_7_days, last_30_days, last_90_days, or custom",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                    "minimum_spend": {
                        "type": "number",
                        "description": "Spend floor before a pattern is treated as significant",
                    },
                    "learning_spend": {
                        "type": "number",
                        "description": "Spend target before a cell graduates from live learning",
                    },
                    "cpa_target": {"type": "number"},
                    "roas_target": {"type": "number"},
                    "watch_group_by": {
                        "type": "string",
                        "default": "messaging_angle",
                        "description": (
                            "Timeseries grouping for watch/fatigue learnings: "
                            "ad_name, campaign_name, landing_page_domain, analysis_id, "
                            "hook_type, messaging_angle, ad_type, format, visual_style, cta, emotion, "
                            "demographic_age, demographic_gender, demographic_segment, or demographic_signal"
                        ),
                    },
                    "watch_metric": {
                        "type": "string",
                        "default": "roas",
                        "description": (
                            "Timeseries metric used for watch/fatigue learnings: "
                            "roas, cpa, ctr, cpm, thumbstop_rate, video_completion_rate, funnel_score, etc."
                        ),
                    },
                    "watch_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional signal filter for watch/fatigue learnings: "
                            "all, fatigued, stable, or insufficient_data"
                        ),
                    },
                    "watch_trajectory_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional trend filter for watch/fatigue learnings: "
                            "all, worsening, improving, flat, or insufficient_data"
                        ),
                    },
                    "watch_coverage_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional coverage-risk filter for watch/fatigue learnings: "
                            "all, call_ready, gappy, insufficient_points, short_window, or windowed_history"
                        ),
                    },
                    "watch_minimum_points": {
                        "type": "integer",
                        "default": 2,
                        "description": "Minimum observed timeseries points before a watch group is eligible",
                    },
                    "watch_minimum_calendar_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum elapsed calendar days before a watch group is eligible",
                    },
                    "watch_maximum_gap_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Maximum sync gap in calendar days before a watch group is eligible",
                    },
                    "watch_sources": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated watch sources: timeseries, "
                            "strategy, patterns, or all"
                        ),
                    },
                    "fatigue_decay_threshold": {
                        "type": "number",
                        "default": 0.18,
                        "description": "Decay threshold that flips a watch trend to fatigued",
                    },
                    "kinds": {
                        "type": "string",
                        "description": "Optional comma-separated kinds: conclusion, working, watch, audience, gap, or all",
                    },
                    "conclusion_statuses": {
                        "type": "string",
                        "description": "Optional comma-separated conclusion statuses when kinds includes conclusion: winner, fatigued, loser, or all",
                    },
                    "audience_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": "Optional audience signal filter when kinds includes audience: all, opportunity, or waste",
                    },
                    "audience_limit": {
                        "type": "integer",
                        "default": 3,
                        "description": "Maximum audience learning stories to include in the exported context",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 8,
                        "description": "Maximum learning stories to include in the exported context",
                    },
                },
            },
        ),
        Tool(
            name="get_performance_timeseries",
            description=(
                "Return saved performance time series for creative or campaign fatigue "
                "checks. Use this to inspect dated ROAS, CPA, CTR, CPM, thumbstop, "
                "completion, or funnel trends per creative, campaign, landing page, "
                "hook, angle, ad type, format, visual style, CTA, analysis id, or "
                "audience slice, plus the same fatigue decay signal the strategy "
                "matrix uses. Supports trajectory filters for worsening, improving, "
                "flat, or insufficient-data reads, plus coverage-risk filters for "
                "gappy, short-window, or call-ready histories."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "last_30d",
                        "description": "Optional date window preset: all_time, last_7d, last_30d, last_90d, maximum, or custom",
                    },
                    "group_by": {
                        "type": "string",
                        "default": "ad_name",
                        "description": (
                            "ad_name, campaign_name, landing_page_domain, analysis_id, "
                            "hook_type, messaging_angle, ad_type, format, visual_style, cta, emotion, "
                            "demographic_age, demographic_gender, demographic_segment, or demographic_signal"
                        ),
                    },
                    "metric": {
                        "type": "string",
                        "default": "roas",
                        "description": (
                            "roas, cpa, ctr, cpm, cvr, thumbstop_rate, "
                            "video_completion_rate, or funnel_score"
                        ),
                    },
                    "signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional fatigue filter: all, fatigued, stable, "
                            "or insufficient_data"
                        ),
                    },
                    "trajectory_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional trend filter: all, worsening, improving, "
                            "flat, or insufficient_data"
                        ),
                    },
                    "coverage_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional sync coverage filter: all, call_ready, "
                            "gappy, insufficient_points, short_window, or "
                            "windowed_history"
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum grouped series to return",
                    },
                    "minimum_spend": {
                        "type": "number",
                        "default": 500,
                        "description": "Spend floor before fatigue is treated as meaningful",
                    },
                    "minimum_points": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum observed points required before a grouped series is returned",
                    },
                    "minimum_calendar_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum elapsed calendar days required before a grouped series is returned",
                    },
                    "maximum_gap_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Maximum sync gap in calendar days allowed before a grouped series is returned",
                    },
                    "fatigue_decay_threshold": {
                        "type": "number",
                        "default": 0.18,
                        "description": "Decay threshold that flips a series to fatigued",
                    },
                },
            },
        ),
        Tool(
            name="export_performance_timeseries_context",
            description=(
                "Return the reusable agent_context payload from saved performance "
                "time series so another agent can decide what to refresh, watch, "
                "scale, hold, or sync more data without opening the dashboard. "
                "Exports the decision queue, summary text, action mix, top fatigue "
                "or coverage-risk groups, and the prompt-ready context built from "
                "the same fatigue/time-series logic as the Creative Tagger UI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "last_30d",
                        "description": "Optional date window preset: all_time, last_7d, last_30d, last_90d, maximum, or custom",
                    },
                    "group_by": {
                        "type": "string",
                        "default": "ad_name",
                        "description": (
                            "ad_name, campaign_name, landing_page_domain, analysis_id, "
                            "hook_type, messaging_angle, ad_type, format, visual_style, cta, emotion, "
                            "demographic_age, demographic_gender, demographic_segment, or demographic_signal"
                        ),
                    },
                    "metric": {
                        "type": "string",
                        "default": "roas",
                        "description": (
                            "roas, cpa, ctr, cpm, cvr, thumbstop_rate, "
                            "video_completion_rate, or funnel_score"
                        ),
                    },
                    "signal_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional fatigue filter: all, fatigued, stable, "
                            "or insufficient_data"
                        ),
                    },
                    "trajectory_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional trend filter: all, worsening, improving, "
                            "flat, or insufficient_data"
                        ),
                    },
                    "coverage_focus": {
                        "type": "string",
                        "default": "all",
                        "description": (
                            "Optional sync coverage filter: all, call_ready, "
                            "gappy, insufficient_points, short_window, or "
                            "windowed_history"
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start date",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end date",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum grouped series to return",
                    },
                    "minimum_spend": {
                        "type": "number",
                        "default": 500,
                        "description": "Spend floor before fatigue is treated as meaningful",
                    },
                    "minimum_points": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum observed points required before a grouped series is returned",
                    },
                    "minimum_calendar_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Minimum elapsed calendar days required before a grouped series is returned",
                    },
                    "maximum_gap_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Maximum sync gap in calendar days allowed before a grouped series is returned",
                    },
                    "fatigue_decay_threshold": {
                        "type": "number",
                        "default": 0.18,
                        "description": "Decay threshold that flips a series to fatigued",
                    },
                },
            },
        ),
        Tool(
            name="create_custom_report",
            description=(
                "Create a custom performance report by selecting standard and/or "
                "brand taxonomy dimensions, then ranking the actual matched "
                "dimension combinations by ROAS, funnel_score, spend, CTR, or CPA. "
                "Use this when the user asks for a custom Motion-style view like "
                "hook x landing_page x offer_type, founder x hook, offer x audience, "
                "or custom segments. Optional start_date and end_date let an agent "
                "isolate a specific test window before explaining the winning "
                "combination. Rows can include `parts` and `values` so the agent "
                "can explain the winning combination."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "dimensions"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "title": {"type": "string", "default": "Custom Report"},
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Dimensions such as hook_type, landing_page, audience, "
                            "offer_type, founder, product, or customer_segment"
                        ),
                    },
                    "layer": {
                        "type": "string",
                        "default": "standard",
                        "description": "standard, brand, or all",
                    },
                    "metric": {
                        "type": "string",
                        "default": "roas",
                        "description": "roas, funnel_score, spend, ctr, cpa",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD lookback start for the report window",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD lookback end for the report window",
                    },
                    "spend_threshold": {"type": "number", "default": 500},
                    "limit": {"type": "integer", "default": 12},
                },
            },
        ),
        Tool(
            name="list_custom_reports",
            description="List saved custom report definitions for a brand.",
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                },
            },
        ),
        Tool(
            name="save_custom_report",
            description=(
                "Save or update a reusable custom report definition for a brand. "
                "Use this when the user wants the same Motion-style combination "
                "view available later, such as hook_type x landing_page x offer_type, "
                "including custom report windows scoped to a specific test period "
                "or a richer dashboard preset with a saved view type, grouping, "
                "metric set, filters, sort, and metric preset."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "name", "dimensions"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "name": {"type": "string", "description": "Saved report name"},
                    "description": {"type": "string"},
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Dimensions such as hook_type, landing_page, audience, "
                            "offer_type, founder, product, or customer_segment"
                        ),
                    },
                    "layer": {
                        "type": "string",
                        "default": "standard",
                        "description": "standard, brand, or all",
                    },
                    "metric": {
                        "type": "string",
                        "default": "roas",
                        "description": "roas, funnel_score, spend, ctr, cpa",
                    },
                    "view_type": {
                        "type": "string",
                        "default": "table",
                        "description": "Saved dashboard view mode: table, matrix, comparison, or cards",
                    },
                    "date_range": {
                        "type": "string",
                        "default": "last_30_days",
                        "description": "Saved dashboard range preset: last_7_days, last_30_days, last_90_days, custom, or all_time",
                    },
                    "group_by": {
                        "type": "string",
                        "default": "creative",
                        "description": "Saved dashboard grouping mode such as creative, dimension, or matrix",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional saved dashboard metric set, e.g. spend, roas, cpa, ctr",
                    },
                    "filters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "value": {"type": "string"},
                            },
                        },
                        "description": "Optional saved dashboard filters as field/value pairs",
                    },
                    "sort": {
                        "type": "string",
                        "default": "desc",
                        "description": "Saved dashboard sort direction: asc or desc",
                    },
                    "saved_metric_preset": {
                        "type": "string",
                        "description": "Optional saved dashboard metric preset key such as diagnostics, conversion, delivery, video, scale, or all",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD lookback start to persist with the saved report",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD lookback end to persist with the saved report",
                    },
                    "spend_threshold": {"type": "number", "default": 500},
                    "limit": {"type": "integer", "default": 12},
                },
            },
        ),
        Tool(
            name="run_saved_custom_report",
            description="Run a saved custom report definition by id.",
            inputSchema={
                "type": "object",
                "required": ["report_id"],
                "properties": {
                    "report_id": {"type": "integer"},
                },
            },
        ),
        Tool(
            name="delete_custom_report",
            description="Delete a saved custom report definition by id.",
            inputSchema={
                "type": "object",
                "required": ["report_id"],
                "properties": {
                    "report_id": {"type": "integer"},
                },
            },
        ),
        Tool(
            name="predict_creative",
            description=(
                "Pre-flight: predict how a creative will perform for a brand BEFORE it "
                "spends, by scoring its classified tags against the brand's OWN historical "
                "tag-level ROAS. Returns a 0-100 fit score, per-tag brand-relative ratings, "
                "and concrete 'swap X for Y' fixes. The one thing connected-account tools "
                "can't do: grade a concept before launch. Pass a saved analysis_id (from "
                "analyze_creative) or a raw attributes object."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "analysis_id": {
                        "type": "integer",
                        "description": "Saved analysis id to score (from analyze_creative)",
                    },
                    "attributes": {
                        "type": "object",
                        "description": (
                            "Creative attributes, alternative to analysis_id, e.g. "
                            "{hook_type, visual_format, cta, emotion, offer_type}"
                        ),
                    },
                },
            },
        ),
        Tool(
            name="get_demographics_performance",
            description=(
                "Return saved age x gender performance memory with opportunity and "
                "waste flags. Useful for audience strategy and Advantage+ diagnostics. "
                "Supports report date presets like last_30_days or a custom "
                "start_date/end_date (YYYY-MM-DD) to scope the audience read to "
                "a specific performance window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "date_preset": {
                        "type": "string",
                        "default": "all_time",
                        "description": "Optional date window preset: all_time, last_7_days, last_30_days, last_90_days, or custom",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional start date in YYYY-MM-DD format",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional end date in YYYY-MM-DD format",
                    },
                },
            },
        ),
        Tool(
            name="generate_brand_taxonomy",
            description=(
                "Auto-build a brand's ENTIRE custom taxonomy from trends in its analyzed "
                "creative library — messaging themes, intended audiences, AND entities "
                "(products, founders, creators, offers, customer segments, campaign "
                "labels). Lets a brand get the full brand-custom layer with zero manual "
                "setup. Optionally persists everything into Brand Taxonomy Studio so "
                "future analyses, predictions, and naming templates use them."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "persist": {
                        "type": "boolean",
                        "default": True,
                        "description": "Save generated values to the brand taxonomy",
                    },
                },
            },
        ),
        Tool(
            name="scan_competitor",
            description=(
                "Scan a competitor's ads from the Meta Ad Library and return classified "
                "results plus an aggregate strategy breakdown (top hook types, visual "
                "styles, CTAs, emotions, estimated spend). Provide page_id, page_name, "
                "or keyword. Returns ad metadata, full Creative Tagger analysis per ad, "
                "and strategy insights."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": (
                            "Optional workspace brand to attach the scan to for saved "
                            "Market history and follow-up briefing."
                        ),
                    },
                    "page_id": {"type": "string"},
                    "page_name": {"type": "string"},
                    "keyword": {"type": "string"},
                    "country": {"type": "string", "default": "US"},
                    "limit": {"type": "integer", "default": 25},
                    "analyze_creatives": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="get_competitor_scan_history",
            description=(
                "Return saved competitor Market scans/imports for the current workspace "
                "without re-running Meta Ad Library access. Useful for re-briefing past "
                "market reads, checking the latest tagged competitor patterns, or "
                "building strategy prompts from previously saved scans."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": (
                            "Optional workspace brand filter. When omitted, returns the "
                            "latest saved scans across brands for the current account."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of saved scans/imports to return",
                    },
                },
            },
        ),
        Tool(
            name="import_competitor_ads",
            description=(
                "Import normalized competitor Meta Ad Library rows for internal "
                "backfills or controlled migrations. The launch customer flow should "
                "use native scan_competitor once Meta Ad Library access is approved. "
                "Returns normalized ads, optional joined analyses, and the "
                "same aggregate strategy breakdown as scan_competitor."
            ),
            inputSchema={
                "type": "object",
                "required": ["ads"],
                "properties": {
                    "competitor_name": {
                        "type": "string",
                        "description": (
                            "Fallback competitor/brand name when rows omit page_name."
                        ),
                    },
                    "ads": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Raw Meta Ad Library rows or normalized rows. Supports "
                            "fields like ad_id, page_name, primary_text/body_text, "
                            "headline, platforms, spend/spend_lower/spend_upper, "
                            "impressions, and snapshot_url."
                        ),
                    },
                    "analyses": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Optional Creative Tagger analysis rows keyed by ad_id. "
                            "Useful when the external agent already analyzed assets."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="generate_naming",
            description=(
                "Generate V1-compatible standard, full, compact, and reporting naming "
                "convention strings from creative attributes. Use when you already have "
                "classified attributes (for example from analyze_creative) and need the "
                "same naming structure the API returns."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "asset_type": {"type": "string", "default": "UGC"},
                    "visual_format": {"type": "string", "default": "Talking Head"},
                    "visual_style": {"type": "string", "default": "Native"},
                    "talent": {"type": "string"},
                    "talent_type": {"type": "string", "default": "No Talent"},
                    "audience": {"type": "string"},
                    "messaging_angle": {"type": "string"},
                    "seasonality": {"type": "string"},
                    "offer_type": {"type": "string"},
                    "hook_type": {"type": "string"},
                    "cta": {"type": "string"},
                    "cta_type": {"type": "string"},
                    "aspect_ratio": {"type": "string", "default": "9:16"},
                    "duration": {"type": "string"},
                    "audio_type": {"type": "string"},
                    "voiceover_tone": {"type": "string"},
                    "emotion": {"type": "string"},
                    "version": {"type": "integer", "default": 1},
                },
            },
        ),
    ])


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name in INTERNAL_BACKFILL_TOOLS and not _is_internal_backfill_enabled():
            return _err(
                "Internal backfill tools are disabled. Connect Meta through "
                "Creative Tagger OAuth and use sync_meta_performance or "
                "scan_competitor for launch customer workflows."
            )
        if name == "analyze_creative":
            return await _analyze_creative(arguments)
        if name == "get_taxonomy":
            return await _get_taxonomy(arguments)
        if name == "list_library":
            return await _list_library(arguments)
        if name == "get_library_patterns":
            return await _get_library_patterns(arguments)
        if name == "get_analysis":
            return await _get_analysis(arguments)
        if name == "recommend":
            return await _recommend(arguments)
        if name == "analyze_gaps":
            return await _analyze_gaps(arguments)
        if name == "get_brand_context":
            return await _get_brand_context(arguments)
        if name == "set_brand_context":
            return await _set_brand_context(arguments)
        if name == "get_brand_taxonomy":
            return await _get_brand_taxonomy(arguments)
        if name == "set_brand_taxonomy_value":
            return await _set_brand_taxonomy_value(arguments)
        if name == "delete_brand_taxonomy_value":
            return await _delete_brand_taxonomy_value(arguments)
        if name == "set_brand_entity":
            return await _set_brand_entity(arguments)
        if name == "delete_brand_entity":
            return await _delete_brand_entity(arguments)
        if name == "get_naming_variables":
            return await _get_naming_variables(arguments)
        if name == "list_naming_templates":
            return await _list_naming_templates(arguments)
        if name == "save_naming_template":
            return await _save_naming_template(arguments)
        if name == "delete_naming_template":
            return await _delete_naming_template(arguments)
        if name == "preview_naming_template":
            return await _preview_naming_template(arguments)
        if name == "get_meta_status":
            return await _get_meta_status(arguments)
        if name == "sync_meta_performance":
            return await _sync_meta_performance(arguments)
        if name == "import_meta_performance":
            return await _import_meta_performance(arguments)
        if name == "get_meta_performance_summary":
            return await _get_meta_performance_summary(arguments)
        if name == "get_taxonomy_performance":
            return await _get_taxonomy_performance(arguments)
        if name == "get_prebuilt_reports":
            return await _get_prebuilt_reports(arguments)
        if name == "get_creative_strategy_report":
            return await _get_creative_strategy_report(arguments)
        if name == "get_brain_learnings":
            return await _get_brain_learnings(arguments)
        if name == "save_brain_learnings":
            return await _save_brain_learnings(arguments)
        if name == "export_brain_learnings_context":
            return await _export_brain_learnings_context(arguments)
        if name == "get_performance_timeseries":
            return await _get_performance_timeseries(arguments)
        if name == "export_performance_timeseries_context":
            return await _export_performance_timeseries_context(arguments)
        if name == "create_custom_report":
            return await _create_custom_report(arguments)
        if name == "list_custom_reports":
            return await _list_custom_reports(arguments)
        if name == "save_custom_report":
            return await _save_custom_report(arguments)
        if name == "run_saved_custom_report":
            return await _run_saved_custom_report(arguments)
        if name == "delete_custom_report":
            return await _delete_saved_custom_report(arguments)
        if name == "predict_creative":
            return await _predict_creative(arguments)
        if name == "get_demographics_performance":
            return await _get_demographics_performance(arguments)
        if name == "generate_brand_taxonomy":
            return await _generate_brand_taxonomy(arguments)
        if name == "scan_competitor":
            return await _scan_competitor(arguments)
        if name == "get_competitor_scan_history":
            return await _get_competitor_scan_history(arguments)
        if name == "import_competitor_ads":
            return await _import_competitor_ads(arguments)
        if name == "generate_naming":
            return _generate_naming(arguments)
        return _err(f"Unknown tool: {name}")
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        return _err(f"API error ({e.response.status_code}): {detail}")
    except httpx.ConnectError:
        return _err(
            f"Cannot connect to Creative Tagger API at {API_URL}. "
            "Set CREATIVE_TAGGER_URL or check the API is running."
        )
    except Exception as e:
        return _err(str(e))


# ---------- Tool Implementations ----------


async def _analyze_creative(args: dict) -> list[TextContent]:
    file_path = args.get("file_path")
    file_paths = args.get("file_paths") or []
    url = args.get("url")
    html_content = args.get("html_content")
    brand_name = args.get("brand_name", "Brand")
    data = _analysis_form_data(args, brand_name)

    async with httpx.AsyncClient(timeout=180.0) as client:
        if file_paths:
            if not isinstance(file_paths, list):
                return _err("file_paths must be a list of local image paths")
            paths = [Path(str(path)).expanduser().resolve() for path in file_paths]
            missing = [str(path) for path in paths if not path.exists()]
            if missing:
                return _err(f"File not found: {missing[0]}")
            handles: list[BinaryIO] = []
            try:
                files = []
                for path in paths:
                    handle = open(path, "rb")
                    handles.append(handle)
                    files.append(("files", (path.name, handle)))
                resp = await client.post(
                    f"{API_URL}/analyze",
                    files=files,
                    data=data,
                    headers=_headers(),
                )
            finally:
                for handle in handles:
                    handle.close()
        elif file_path:
            path = Path(file_path).expanduser().resolve()
            if not path.exists():
                return _err(f"File not found: {file_path}")
            with open(path, "rb") as f:
                resp = await client.post(
                    f"{API_URL}/analyze",
                    files={"file": (path.name, f)},
                    data=data,
                    headers=_headers(),
                )
        elif url:
            is_page = not any(
                url.lower().endswith(ext)
                for ext in (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".gif")
            )
            data = dict(data)
            if is_page:
                data["page_url"] = url
            else:
                data["file_url"] = url
            resp = await client.post(
                f"{API_URL}/analyze", data=data, headers=_headers()
            )
        elif html_content:
            resp = await client.post(
                f"{API_URL}/analyze",
                data={**data, "html_content": html_content},
                headers=_headers(),
            )
        else:
            return _err("Provide file_path, file_paths, url, or html_content")

        resp.raise_for_status()
        return _text(resp.json())


def _analysis_form_data(args: dict, brand_name: str) -> dict[str, str]:
    """Build form data accepted by the API `/analyze` endpoint."""
    data = {
        "brand_name": str(brand_name),
        "version": str(args.get("version", 1)),
        "include_transcript": str(args.get("include_transcript", True)).lower(),
        "forensic_mode": str(args.get("forensic_mode", False)).lower(),
    }
    if args.get("format"):
        data["format"] = str(args["format"])
    return data


async def _get_taxonomy(args: dict) -> list[TextContent]:
    dimension = args.get("dimension")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/openapi.json", headers=_headers())
        resp.raise_for_status()
        spec = resp.json()

    schemas = (spec.get("components") or {}).get("schemas") or {}
    enums: dict[str, list[str]] = {}
    for name, schema in schemas.items():
        values = schema.get("enum")
        if values and isinstance(values, list):
            enums[name] = values

    if dimension:
        match = next(
            (v for k, v in enums.items() if k.lower() == dimension.lower()),
            None,
        )
        if not match:
            return _err(
                f"Unknown dimension: {dimension}. Available: {', '.join(sorted(enums.keys()))}"
            )
        return _text({dimension: match})

    return _text({"dimensions": enums, "count": len(enums)})


async def _list_library(args: dict) -> list[TextContent]:
    params: dict[str, Any] = {**_auth_params()}
    for k in (
        "limit",
        "offset",
        "search",
        "format",
        "hook",
        "angle",
        "emotion",
        "cta",
        "talent",
        "offer",
        "audio",
        "season",
        "sort",
    ):
        if args.get(k) is not None:
            params[k] = args[k]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/auth/library", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_library_patterns(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/auth/library/patterns", params=_auth_params()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_analysis(args: dict) -> list[TextContent]:
    analysis_id = args.get("analysis_id")
    if not analysis_id:
        return _err("analysis_id is required")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/auth/library/{analysis_id}", params=_auth_params()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _recommend(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    question = args.get("question", "")
    if not brand_name or not question:
        return _err("brand_name and question are required")
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_URL}/strategist/recommend",
            data={"brand_name": brand_name, "question": question},
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _analyze_gaps(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_URL}/strategist/gaps",
            data={"brand_name": brand_name},
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_brand_context(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    params = {**_auth_params(), "brand_name": brand_name}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/auth/brand-context", params=params)
        if resp.status_code == 404:
            return _text(
                {"brand_name": brand_name, "exists": False, "message": "No brand context saved yet"}
            )
        resp.raise_for_status()
        return _text(resp.json())


async def _set_brand_context(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    body = {
        "brand_name": brand_name,
        "voice": args.get("voice", ""),
        "target_audience": args.get("target_audience", ""),
        "top_performers": args.get("top_performers") or [],
        "anti_patterns": args.get("anti_patterns") or [],
        "notes": args.get("notes", ""),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/auth/brand-context",
            params=_auth_params(),
            json=body,
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_brand_taxonomy(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    params = {**_auth_params(), "brand_name": brand_name}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/auth/brand-taxonomy", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _set_brand_taxonomy_value(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    dimension = args.get("dimension", "")
    value = args.get("value", "")
    if not brand_name or not dimension or not value:
        return _err("brand_name, dimension, and value are required")
    body = {
        "brand_name": brand_name,
        "dimension": dimension,
        "value": value,
        "description": args.get("description", ""),
        "aliases": args.get("aliases") or [],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/auth/brand-taxonomy/values",
            params=_auth_params(),
            json=body,
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _delete_brand_taxonomy_value(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    dimension = args.get("dimension", "")
    value = args.get("value", "")
    if not brand_name or not dimension or not value:
        return _err("brand_name, dimension, and value are required")
    params = {
        **_auth_params(),
        "brand_name": brand_name,
        "dimension": dimension,
        "value": value,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(f"{API_URL}/auth/brand-taxonomy/values", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _set_brand_entity(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    entity_type = args.get("entity_type", "")
    name = args.get("name", "")
    if not brand_name or not entity_type or not name:
        return _err("brand_name, entity_type, and name are required")
    body = {
        "brand_name": brand_name,
        "entity_type": entity_type,
        "name": name,
        "description": args.get("description", ""),
        "aliases": args.get("aliases") or [],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/auth/brand-taxonomy/entities",
            params=_auth_params(),
            json=body,
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _delete_brand_entity(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    entity_type = args.get("entity_type", "")
    name = args.get("name", "")
    if not brand_name or not entity_type or not name:
        return _err("brand_name, entity_type, and name are required")
    params = {
        **_auth_params(),
        "brand_name": brand_name,
        "entity_type": entity_type,
        "name": name,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(f"{API_URL}/auth/brand-taxonomy/entities", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_naming_variables(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/auth/naming/variables")
        resp.raise_for_status()
        return _text(resp.json())


async def _list_naming_templates(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/auth/naming/templates", params=_auth_params()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _save_naming_template(args: dict) -> list[TextContent]:
    template = args.get("template", "")
    if not template:
        return _err("template is required")
    body = {
        "template": template,
        "name": args.get("name", "default"),
        "separator": args.get("separator", "_"),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/auth/naming/templates",
            params=_auth_params(),
            json=body,
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _delete_naming_template(args: dict) -> list[TextContent]:
    params = {**_auth_params(), "name": args.get("name", "default")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(f"{API_URL}/auth/naming/templates", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _preview_naming_template(args: dict) -> list[TextContent]:
    template = args.get("template", "")
    if not template:
        return _err("template is required")
    body = {
        "template": template,
        "name": args.get("name", "default"),
        "separator": args.get("separator", "_"),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{API_URL}/auth/naming/preview", json=body)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_meta_status(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/auth/meta/status", headers=_headers())
        resp.raise_for_status()
        return _text(resp.json())


async def _sync_meta_performance(args: dict) -> list[TextContent]:
    body = {
        "brand_name": args.get("brand_name", ""),
        "account_id": args.get("account_id", ""),
        "date_preset": args.get("date_preset", "last_30d"),
        "attribution_windows": _string_list_arg(args.get("attribution_windows")),
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_URL}/meta/sync", json=body, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _import_meta_performance(args: dict) -> list[TextContent]:
    rows = args.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return _err("rows must be a non-empty list of Meta performance objects")
    body = {
        "brand_name": args.get("brand_name", ""),
        "rows": rows,
        "source": args.get("source", "meta_mcp"),
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{API_URL}/meta/import", json=body, headers=_headers())
        resp.raise_for_status()
        return _text(resp.json())


async def _get_meta_performance_summary(args: dict) -> list[TextContent]:
    params = {"brand_name": args.get("brand_name", "")}
    if args.get("date_preset"):
        params["date_preset"] = args["date_preset"]
    if args.get("start_date"):
        params["start_date"] = args["start_date"]
    if args.get("end_date"):
        params["end_date"] = args["end_date"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/meta/performance/summary",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _predict_creative(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    data: dict[str, Any] = {"brand_name": brand_name}
    if args.get("analysis_id") is not None:
        data["analysis_id"] = args["analysis_id"]
    if args.get("attributes"):
        import json as _json

        data["attributes"] = _json.dumps(args["attributes"])
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{API_URL}/predict", data=data, headers=_headers())
        resp.raise_for_status()
        return _text(resp.json())


async def _get_taxonomy_performance(args: dict) -> list[TextContent]:
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "spend_threshold": args.get("spend_threshold", 500),
    }
    if args.get("dimension"):
        params["dimension"] = args["dimension"]
    if args.get("date_preset"):
        params["date_preset"] = args["date_preset"]
    if args.get("start_date"):
        params["start_date"] = args["start_date"]
    if args.get("end_date"):
        params["end_date"] = args["end_date"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/performance/by-taxonomy",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_prebuilt_reports(args: dict) -> list[TextContent]:
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "spend_threshold": args.get("spend_threshold", 500),
        "limit": args.get("limit", 8),
    }
    if args.get("report_id"):
        params["report_id"] = args["report_id"]
    if args.get("start_date"):
        params["start_date"] = args["start_date"]
    if args.get("end_date"):
        params["end_date"] = args["end_date"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/reports/prebuilt",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_creative_strategy_report(args: dict) -> list[TextContent]:
    params = _strategy_params(args)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/reports/creative-strategy",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_brain_learnings(args: dict) -> list[TextContent]:
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "date_preset": args.get("date_preset", "all_time"),
        "limit": args.get("limit", 8),
    }
    for key in (
        "start_date",
        "end_date",
        "minimum_spend",
        "learning_spend",
        "cpa_target",
        "roas_target",
        "watch_group_by",
        "watch_metric",
        "watch_signal_focus",
        "watch_trajectory_focus",
        "watch_coverage_focus",
        "watch_minimum_points",
        "watch_minimum_calendar_days",
        "watch_maximum_gap_days",
        "watch_sources",
        "fatigue_decay_threshold",
        "kinds",
        "conclusion_statuses",
        "audience_signal_focus",
        "audience_limit",
    ):
        if key in {"watch_sources", "kinds", "conclusion_statuses"}:
            value = _csv_arg(args.get(key))
            if value:
                params[key] = value
            continue
        if args.get(key) not in (None, ""):
            params[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/brain/learnings",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _save_brain_learnings(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    body: dict[str, Any] = {
        "brand_name": brand_name,
        "date_preset": args.get("date_preset", "all_time"),
        "include_gaps_in_notes": _coerce_bool(
            args.get("include_gaps_in_notes", False)
        ),
        "limit": args.get("limit", 8),
    }
    for key in (
        "start_date",
        "end_date",
        "minimum_spend",
        "learning_spend",
        "cpa_target",
        "roas_target",
        "watch_group_by",
        "watch_metric",
        "watch_signal_focus",
        "watch_trajectory_focus",
        "watch_coverage_focus",
        "watch_minimum_points",
        "watch_minimum_calendar_days",
        "watch_maximum_gap_days",
        "watch_sources",
        "fatigue_decay_threshold",
        "kinds",
        "conclusion_statuses",
        "audience_signal_focus",
        "audience_limit",
    ):
        if key in {"watch_sources", "kinds", "conclusion_statuses"}:
            value = _csv_arg(args.get(key))
            if value:
                body[key] = value
            continue
        if args.get(key) not in (None, ""):
            body[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/brain/learnings/save",
            json=body,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _export_brain_learnings_context(args: dict) -> list[TextContent]:
    payload = await _get_brain_learnings(args)
    if not payload:
        return payload
    raw = getattr(payload[0], "text", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return payload
    if not isinstance(parsed, dict):
        return payload
    context = parsed.get("agent_context")
    if not isinstance(context, dict):
        return _err("Brain learnings response did not include agent_context")
    export = {
        **context,
        "brand_name": parsed.get("brand_name", args.get("brand_name", "")),
        "generated_at": parsed.get("generated_at", ""),
        "hero": parsed.get("hero") or {},
        "summary": parsed.get("summary") or {},
        "controls": parsed.get("controls") or {},
        "source_summary": parsed.get("source_summary") or {},
    }
    return _text(export)


async def _get_performance_timeseries(args: dict) -> list[TextContent]:
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "date_preset": args.get("date_preset", "last_30d"),
        "group_by": args.get("group_by", "ad_name"),
        "metric": args.get("metric", "roas"),
        "signal_focus": args.get("signal_focus", "all"),
        "trajectory_focus": args.get("trajectory_focus", "all"),
        "coverage_focus": args.get("coverage_focus", "all"),
        "limit": args.get("limit", 10),
        "minimum_spend": args.get("minimum_spend", 500),
        "minimum_points": args.get("minimum_points", 0),
        "minimum_calendar_days": args.get("minimum_calendar_days", 0),
        "maximum_gap_days": args.get("maximum_gap_days", 0),
        "fatigue_decay_threshold": args.get("fatigue_decay_threshold", 0.18),
    }
    for key in ("start_date", "end_date"):
        if args.get(key) not in (None, ""):
            params[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/performance/timeseries",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _export_performance_timeseries_context(args: dict) -> list[TextContent]:
    payload = await _get_performance_timeseries(args)
    if not payload:
        return payload
    raw = getattr(payload[0], "text", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return payload
    if not isinstance(parsed, dict):
        return payload
    context = parsed.get("agent_context")
    if not isinstance(context, dict):
        return _err("Performance timeseries response did not include agent_context")
    export = {
        **context,
        "brand_name": parsed.get("brand_name", args.get("brand_name", "")),
        "generated_at": parsed.get("generated_at", ""),
        "summary": parsed.get("summary") or {},
        "series": parsed.get("series") or [],
    }
    return _text(export)


async def _create_custom_report(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    dimensions = args.get("dimensions") or []
    if not brand_name:
        return _err("brand_name is required")
    if not isinstance(dimensions, list) or not dimensions:
        return _err("dimensions must be a non-empty list")
    payload = {
        "brand_name": brand_name,
        "title": args.get("title") or "Custom Report",
        "dimensions": dimensions,
        "layer": args.get("layer", "standard"),
        "metric": args.get("metric", "roas"),
        "spend_threshold": args.get("spend_threshold", 500),
        "limit": args.get("limit", 12),
    }
    for key in ("start_date", "end_date"):
        if args.get(key) not in (None, ""):
            payload[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/reports/custom",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _list_custom_reports(args: dict) -> list[TextContent]:
    params = {"brand_name": args.get("brand_name", "")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/reports/custom/saved",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _save_custom_report(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    name = args.get("name", "")
    dimensions = args.get("dimensions") or []
    if not brand_name:
        return _err("brand_name is required")
    if not name:
        return _err("name is required")
    if not isinstance(dimensions, list) or not dimensions:
        return _err("dimensions must be a non-empty list")
    payload = {
        "brand_name": brand_name,
        "name": name,
        "title": name,
        "description": args.get("description", ""),
        "dimensions": dimensions,
        "layer": args.get("layer", "standard"),
        "metric": args.get("metric", "roas"),
        "view_type": args.get("view_type", "table"),
        "date_range": args.get("date_range", "last_30_days"),
        "group_by": args.get("group_by", "creative"),
        "metrics": args.get("metrics") or [],
        "filters": args.get("filters") or [],
        "sort": args.get("sort", "desc"),
        "saved_metric_preset": args.get("saved_metric_preset", ""),
        "spend_threshold": args.get("spend_threshold", 500),
        "limit": args.get("limit", 12),
    }
    for key in ("start_date", "end_date"):
        if args.get(key) not in (None, ""):
            payload[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/reports/custom/saved",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _run_saved_custom_report(args: dict) -> list[TextContent]:
    report_id = args.get("report_id")
    if not report_id:
        return _err("report_id is required")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/reports/custom/saved/{report_id}/run",
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _delete_saved_custom_report(args: dict) -> list[TextContent]:
    report_id = args.get("report_id")
    if not report_id:
        return _err("report_id is required")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{API_URL}/reports/custom/saved/{report_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_demographics_performance(args: dict) -> list[TextContent]:
    params = {
        "brand_name": args.get("brand_name", ""),
        "date_preset": args.get("date_preset", "all_time"),
        "start_date": args.get("start_date", ""),
        "end_date": args.get("end_date", ""),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/performance/demographics",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _generate_brand_taxonomy(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    data = {
        "brand_name": brand_name,
        "persist": str(args.get("persist", True)).lower(),
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{API_URL}/brand-taxonomy/generate",
            data=data,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _scan_competitor(args: dict) -> list[TextContent]:
    body = {
        "brand_name": args.get("brand_name"),
        "page_id": args.get("page_id"),
        "page_name": args.get("page_name"),
        "keyword": args.get("keyword"),
        "country": args.get("country", "US"),
        "limit": args.get("limit", 25),
        "analyze_creatives": args.get("analyze_creatives", True),
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{API_URL}/competitors/scan", json=body, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_competitor_scan_history(args: dict) -> list[TextContent]:
    params = {
        "brand_name": args.get("brand_name", ""),
        "limit": args.get("limit", 10),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/competitors/history", params=params, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _import_competitor_ads(args: dict) -> list[TextContent]:
    ads = args.get("ads") or []
    analyses = args.get("analyses") or []
    if not isinstance(ads, list) or not ads:
        return _err("ads must be a non-empty list of Meta Ad Library row objects")
    if not isinstance(analyses, list):
        return _err("analyses must be a list when provided")
    body = {
        "competitor_name": args.get("competitor_name", ""),
        "ads": ads,
        "analyses": analyses,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_URL}/competitors/import", json=body, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


def _generate_naming(args: dict) -> list[TextContent]:
    brand = str(args.get("brand_name") or "BRAND").upper()
    ver = f"V{args.get('version', 1)}"

    attrs = {
        "asset_type": args.get("asset_type", ""),
        "visual_format": args.get("visual_format", ""),
        "visual_style": args.get("visual_style", ""),
        "talent": args.get("talent") or args.get("talent_type", ""),
        "audience": args.get("audience", ""),
        "messaging_angle": args.get("messaging_angle", ""),
        "seasonality": args.get("seasonality", ""),
        "offer_type": args.get("offer_type", ""),
        "hook_type": args.get("hook_type", ""),
        "cta": args.get("cta") or args.get("cta_type", ""),
        "audio_type": args.get("audio_type", ""),
        "voiceover_tone": args.get("voiceover_tone", ""),
        "emotion": args.get("emotion", ""),
        "aspect_ratio": args.get("aspect_ratio", ""),
        "duration": args.get("duration", ""),
    }
    ratio = _ratio(attrs.get("aspect_ratio"))

    standard = _join(
        brand,
        _sanitize(attrs.get("asset_type")),
        _sanitize(attrs.get("visual_format")),
        _sanitize(attrs.get("talent")),
        _sanitize(attrs.get("hook_type")),
        _sanitize(attrs.get("cta")),
        ratio,
        ver,
    )
    full = _join(
        brand,
        _sanitize(attrs.get("asset_type")),
        _sanitize(attrs.get("visual_format")),
        _sanitize(attrs.get("visual_style")),
        _sanitize(attrs.get("talent")),
        _sanitize(attrs.get("audience")),
        _sanitize(attrs.get("messaging_angle")),
        _sanitize(attrs.get("hook_type")),
        _sanitize(attrs.get("audio_type")),
        _sanitize(attrs.get("cta")),
        _sanitize(attrs.get("offer_type")),
        ratio,
        str(attrs.get("duration") or ""),
        ver,
    )
    compact = _join(
        brand,
        _sanitize(attrs.get("visual_format")),
        _sanitize(attrs.get("talent")),
        _sanitize(attrs.get("cta")),
        ratio,
        ver,
    )
    reporting = _join(
        brand,
        _sanitize(attrs.get("asset_type")),
        _sanitize(attrs.get("visual_format")),
        _sanitize(attrs.get("audience")),
        _sanitize(attrs.get("messaging_angle")),
        _sanitize(attrs.get("hook_type")),
        _sanitize(attrs.get("seasonality")),
        ver,
    )
    return _text(
        {
            "standard": standard,
            "full": full,
            "compact": compact,
            "reporting": reporting,
            "variables": {
                **attrs,
                "brand": brand,
                "version": ver,
                "aspect_ratio": ratio,
                "ratio": ratio,
            },
        }
    )


def _sanitize(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().replace(" ", "").replace("-", "")


def _ratio(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().replace(":", "x")


def _join(*parts: object) -> str:
    return "_".join(str(part) for part in parts if str(part or "").strip())


# ---------- Main ----------


def main():
    import asyncio

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
