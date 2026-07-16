"""Creative Tagger MCP Server.

Exposes the Creative Tagger API as MCP tools so any AI agent (Claude Desktop,
Cursor, Windsurf, ChatGPT with MCP, etc.) can:

- Analyze ad creatives across the 21-dimension classification surface
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
import math
import os
from pathlib import Path
from typing import Any, BinaryIO

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from creative_tagger_mcp import __version__

from creative_tagger_mcp.taxonomy import (
    CONTROLLED_DIMENSIONS,
    DERIVED_OPEN_DIMENSIONS,
    DYNAMIC_DIMENSIONS,
    TAXONOMY_VERSION,
    taxonomy_payload,
)

API_URL = os.environ.get("CREATIVE_TAGGER_URL", "https://api.creativetagger.ai")
API_KEY = os.environ.get("CREATIVE_TAGGER_API_KEY", "")
INTERNAL_BACKFILL_TOOLS = {"import_competitor_ads"}
LIBRARY_PAGE_LIMIT = 100
PREBUILT_REPORT_LIMIT = 50
STRATEGY_DECISION_LIMIT = 25
STRATEGY_WATCH_LIMIT = 10
STRATEGY_MAX_CELLS = 200
BRAIN_STORY_LIMIT = 12
BRAIN_AUDIENCE_LIMIT = 10
TIMESERIES_SERIES_LIMIT = 10
CUSTOM_REPORT_LIMIT = 50
COMPETITOR_RESULT_LIMIT = 50
DEMOGRAPHICS_EXPORT_LIMIT = 100

_AUDIENCE_SIGNAL_FOCUS_ALIASES = {
    "all": "all",
    "higher": "higher_observed_efficiency",
    "higher_observed_efficiency": "higher_observed_efficiency",
    "opportunity": "higher_observed_efficiency",
    "opportunities": "higher_observed_efficiency",
    "opportunity_only": "higher_observed_efficiency",
    "lower": "lower_observed_efficiency",
    "lower_observed_efficiency": "lower_observed_efficiency",
    "waste": "lower_observed_efficiency",
    "waste_only": "lower_observed_efficiency",
}

PLAYBOOK_INSTRUCTIONS = """\
Creative Tagger is observational decision support, not a causal attribution or
forecasting system. For authenticated work, call list_workspaces first and pass
the exact returned brand_name to every scoped tool; never blend or infer across
workspaces. Treat ROAS, CPA, CTR, fatigue, demographic, and taxonomy outputs as
historical associations. Turn a promising association into a falsifiable
controlled test: state the hypothesis, change one variable, choose a primary
metric and guardrails, define a minimum data/duration rule, and set ship/stop
criteria before launch. Preserve read-only behavior and say when evidence is
sparse, confounded, stale, or missing.
"""

server = Server(
    "creative-tagger",
    version=__version__,
    instructions=PLAYBOOK_INSTRUCTIONS,
)


def _headers() -> dict:
    h = {}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def _auth_params() -> dict:
    """Auth moved to the X-API-Key header (set client-wide in _headers()).

    Query-param keys leaked into access logs and proxies. Kept as an
    empty-dict shim so call sites stay simple. Requires an API deploy
    that accepts header auth on /auth/* routes.
    """
    return {}


def _text(payload: Any) -> list[TextContent]:
    """Wrap any JSON-able payload as a TextContent response."""
    if isinstance(payload, str):
        return [TextContent(type="text", text=payload)]
    return [
        TextContent(
            type="text",
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    ]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


def _mcp_error(msg: str) -> CallToolResult:
    """Return a protocol-level tool error, not successful text that says Error."""
    return CallToolResult(content=_err(msg), isError=True)


def _mcp_result(result: list[TextContent] | CallToolResult) -> list[TextContent] | CallToolResult:
    """Promote local validation errors to MCP's isError contract."""
    if isinstance(result, CallToolResult):
        return result
    if result and isinstance(result[0], TextContent) and result[0].text.startswith("Error: "):
        return CallToolResult(content=result, isError=True)
    return result


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


def _clamped_int_arg(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
    field_name: str,
) -> int:
    raw = default if value is None else value
    if isinstance(raw, bool):
        raise ValueError(f"{field_name} must be an integer")
    if isinstance(raw, int):
        parsed = raw
    elif isinstance(raw, float) and math.isfinite(raw) and raw.is_integer():
        parsed = int(raw)
    else:
        raise ValueError(f"{field_name} must be an integer")
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _canonical_audience_signal_focus(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _AUDIENCE_SIGNAL_FOCUS_ALIASES.get(normalized, normalized)


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


PREDICTION_CONTRACT_VERSION = "predict_observational.v2"
_LEGACY_PREDICTION_FIELDS = frozenset(
    {"fit_score", "verdict", "headline", "recommended_swaps", "lift_pct"}
)


def _validated_observational_prediction(payload: Any) -> dict[str, Any]:
    """Accept only the explicit observational contract; never decorate legacy data."""

    if not isinstance(payload, dict):
        raise ValueError("Creative Tagger returned an invalid prediction response")
    # A free-floor response is a pricing boundary, not prediction evidence, and
    # is safe to pass through without inventing a contract around it.
    if set(payload) == {"free_floor"}:
        return payload
    if (
        payload.get("schema_version") != PREDICTION_CONTRACT_VERSION
        or payload.get("evidence_type") != "observational_association"
        or payload.get("causal_claim") is not False
        or payload.get("outcome_prediction") is not False
    ):
        raise ValueError(
            "Creative Tagger prediction contract mismatch; no evidence was returned"
        )

    def walk(value: Any):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield str(key)
                yield from walk(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from walk(nested)

    legacy = _LEGACY_PREDICTION_FIELDS.intersection(walk(payload))
    if legacy:
        raise ValueError(
            "Creative Tagger prediction response contained legacy causal fields; "
            "no evidence was returned"
        )
    return payload


def _infer_strategy_template(
    report_template: Any,
    *,
    rows: Any,
    columns: Any,
) -> str:
    template_aliases = {
        "audience": "demographic-read",
        "audience_read": "demographic-read",
        "demographic": "demographic-read",
        "demographic_read": "demographic-read",
        "demographics": "demographic-read",
        "audience_signal": "audience-signals",
        "audience_signals": "audience-signals",
        "signal": "audience-signals",
        "signals": "audience-signals",
        "angle_audience": "angle-audience-fit",
        "angle_audience_fit": "angle-audience-fit",
        "mixed_audience": "angle-audience-fit",
        "hook_audience": "hook-audience-fit",
        "hook_audience_fit": "hook-audience-fit",
        "next": "next-tests",
        "next_tests": "next-tests",
        "winner": "creative-winners",
        "winners": "creative-winners",
        "creative_winners": "creative-winners",
        "fatigue": "fatigue-watch",
        "watch": "fatigue-watch",
        "fatigue_watch": "fatigue-watch",
        "gap": "coverage-gaps",
        "gaps": "coverage-gaps",
        "coverage": "coverage-gaps",
        "coverage_gaps": "coverage-gaps",
        "hook": "hook-performance",
        "hooks": "hook-performance",
        "hook_performance": "hook-performance",
        "persona": "persona-read",
        "personas": "persona-read",
        "persona_read": "persona-read",
    }
    demographic_dimensions = {
        "demographic_age",
        "demographic_gender",
        "demographic_segment",
        "demographic_signal",
    }
    explicit = str(report_template or "").strip()
    if explicit:
        normalized = explicit.lower().replace("-", "_").replace(" ", "_")
        return template_aliases.get(normalized, explicit)
    row_value = _normalize_strategy_axis(rows)
    col_value = _normalize_strategy_axis(columns)
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


def _normalize_strategy_axis(value: Any) -> str:
    axis = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        # Taxonomy v2: asset_type (production class), media_type (auto-detected
        # format), and product are distinct canonical axes — they pass through
        # untouched and must never collapse into the visual-format axis (the
        # pre-v2 coalesce mixed three dimensions under one "ad_type" label).
        # visual_format is the canonical execution-style key, but the API's
        # watch/timeseries space still groups by the deprecated "ad_type"
        # alias (same sources), so visual_format resolves to it here — this
        # normalizer also feeds get_performance_timeseries group_by.
        "creative_type": "ad_type",
        "visual_format": "ad_type",
        "ad": "ad_type",
        "adtype": "ad_type",
        "creative": "ad_type",
        "angle": "messaging_angle",
        "message_angle": "messaging_angle",
        "message": "messaging_angle",
        "hook_type": "hook",
        "offer": "offer_type",
        "age": "demographic_age",
        "audience_age": "demographic_age",
        "demographic_age_range": "demographic_age",
        "gender": "demographic_gender",
        "audience_gender": "demographic_gender",
        "segment": "demographic_segment",
        "audience_segment": "demographic_segment",
        "demographic": "demographic_segment",
        "signal": "demographic_signal",
        "audience_signal": "demographic_signal",
    }
    return aliases.get(axis, axis)


def _strategy_params(args: dict) -> dict[str, Any]:
    limit = _clamped_int_arg(
        args.get("limit"),
        default=10,
        minimum=1,
        maximum=STRATEGY_DECISION_LIMIT,
        field_name="limit",
    )
    watch_limit = _clamped_int_arg(
        args.get("watch_limit"),
        default=5,
        minimum=1,
        maximum=STRATEGY_WATCH_LIMIT,
        field_name="watch_limit",
    )
    max_cells = _clamped_int_arg(
        args.get("max_cells"),
        default=24,
        minimum=1,
        maximum=STRATEGY_MAX_CELLS,
        field_name="max_cells",
    )
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "date_preset": args.get("date_preset", "all_time"),
        "start_date": args.get("start_date", ""),
        "end_date": args.get("end_date", ""),
        "limit": limit,
        "watch_limit": watch_limit,
        "response_format": args.get("response_format", "concise"),
        "max_cells": max_cells,
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


_COMPACT_TOOL_DESCRIPTIONS = {
    "get_creative_strategy_report": (
        "Read one workspace's observational Strategy matrix and decision queue. "
        "Defaults to a bounded concise response; use detailed only for an explicit "
        "deep dive. Treat cells as test hypotheses, not causal effects."
    ),
    "get_brain_learnings": (
        "Read one workspace's current Brand Brain observations, conclusions, "
        "watchouts, audience signals, gaps, and agent_context. Validate associations "
        "with controlled tests before changing allocation. Audience filters use "
        "higher_observed_efficiency or lower_observed_efficiency."
    ),
    "save_brain_learnings": (
        "Persist a reviewed get_brain_learnings slice as Brand Brain notes. Uses the "
        "same canonical audience filters; saving memory does not prove causality."
    ),
    "export_brain_learnings_context": (
        "Export a bounded, prompt-ready context from get_brain_learnings, including "
        "follow-up Strategy and time-series queries and the same canonical audience "
        "filters."
    ),
    "get_performance_timeseries": (
        "Read one workspace's saved performance series for observational fatigue, "
        "trajectory, and data-coverage checks."
    ),
    "export_performance_timeseries_context": (
        "Export the bounded agent_context from get_performance_timeseries with its "
        "decision queue and data-quality warnings."
    ),
}

_SCHEMA_DESCRIPTION_FIELDS = {
    "get_creative_strategy_report": {
        "report_template",
        "rows",
        "columns",
        "status_focus",
        "metrics",
        "metric_preset",
        "watch_group_by",
        "watch_metric",
        "watch_signal_focus",
        "watch_trajectory_focus",
        "watch_coverage_focus",
        "response_format",
        "max_cells",
    },
    "get_brain_learnings": {
        "kinds",
        "conclusion_statuses",
        "watch_group_by",
        "watch_metric",
        "watch_signal_focus",
        "watch_trajectory_focus",
        "watch_coverage_focus",
        "watch_sources",
        "audience_signal_focus",
    },
    "save_brain_learnings": {"audience_signal_focus"},
    "export_brain_learnings_context": {"audience_signal_focus"},
    "export_performance_timeseries_context": set(),
}


def _compact_tool_catalog(tools: list[Tool]) -> list[Tool]:
    """Remove duplicated prose while keeping names, types, defaults, and enums."""
    for tool in tools:
        description = _COMPACT_TOOL_DESCRIPTIONS.get(tool.name)
        if description:
            tool.description = description
        keep = _SCHEMA_DESCRIPTION_FIELDS.get(tool.name)
        if keep is None:
            continue
        schema = json.loads(json.dumps(tool.inputSchema))
        for field_name, field_schema in schema.get("properties", {}).items():
            if field_name not in keep and isinstance(field_schema, dict):
                field_schema.pop("description", None)
        tool.inputSchema = schema
    return tools


# ---------- Tools ----------


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools = [
        Tool(
            name="analyze_creative",
            description=(
                "Analyze any ad creative (image, video, carousel, landing page, email) "
                "and return structured classification across 21 dimensions: "
                "media type, asset type, visual format, visual style, talent and talent "
                "demographics, hook type, messaging angle, audience, CTA, emotion, "
                "audio type, voiceover tone, seasonality, offer type, aspect ratio, "
                "duration, and more. Also generates standardized "
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
                "Get Creative Tagger taxonomy v2's 15 controlled dimensions, one "
                "derived/open aspect-ratio dimension, and two dynamic, brand-specific "
                "dimensions. The package ships a "
                "versioned vocabulary because the API schema does not expose enums "
                "for every classification field. Use this before analyze_creative "
                "when you want to know the vocabulary the system understands. "
                "Taxonomy v2: media type (the "
                "auto-detected format — static image, video, carousel), asset type "
                "(production class), and visual format (execution style) are three "
                "separate dimensions; 'Static Image' and 'Carousel' are media types, "
                "not visual_format values."
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
            name="list_workspaces",
            description=(
                "List the authenticated user's workspaces and their exact brand_name "
                "scope. Call this first, then reuse one returned brand_name on every "
                "library, status, report, and strategist request."
            ),
            inputSchema={"type": "object", "properties": {}},
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
                    "brand_name": {
                        "type": "string",
                        "description": "Exact workspace brand_name from list_workspaces",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
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
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": "Exact workspace brand_name from list_workspaces",
                    },
                },
            },
        ),
        Tool(
            name="get_analysis",
            description=(
                "Get the full analysis result for a single saved library item by ID. "
                "Use after list_library when you need the complete 21-dimension classification "
                "(list_library returns a summary; this returns the full JSON)."
            ),
            inputSchema={
                "type": "object",
                "required": ["analysis_id"],
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": "Exact workspace brand_name from list_workspaces",
                    },
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
                "Create or partially update brand context for a brand. Stored per-user. "
                "Omitted fields retain their saved values; an explicit empty string or "
                "list clears only that field. This is "
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
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": "Exact workspace brand_name from list_workspaces",
                    },
                },
            },
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
                "gaps. Use this to find which taxonomy values are associated with "
                "stronger historical outcomes, which are under-observed, and which "
                "standard values have never been tried. Rows include "
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
                        "description": "Spend floor before reporting a tag-level observation",
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
                        "description": "Spend floor before reporting a row-level observation",
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
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Rows per report",
                    },
                },
            },
        ),
        Tool(
            name="get_creative_strategy_report",
            description=(
                "Return the strategist matrix for deciding what to test next on Meta. "
                "Defaults to ad_type rows (deprecated alias for visual_format — "
                "taxonomy v2 splits visual_format, asset_type, and media_type into "
                "three separate axes) by messaging_angle columns, with text and "
                "color-coded states for next tests, live learning, winners, losers, "
                "fatigue, and gaps. Also supports audience-mode matrices with "
                "demographic_age, demographic_gender, demographic_segment, and "
                "demographic_signal axes, plus mixed creative x audience reads such "
                "as messaging_angle by demographic_segment. Includes the decision "
                "queue and report table so an LLM can brief next tests from the same "
                "report contract as the Creative Tagger UI; detailed responses also "
                "include the agent_context payload. "
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
                            "Matrix row dimension, e.g. visual_format (execution style), "
                            "asset_type (production class), media_type (auto-detected "
                            "format), messaging_angle, format, hook, persona, product, "
                            "offer_type, demographic_age, demographic_gender, "
                            "demographic_segment, or demographic_signal. Taxonomy v2 "
                            "splits media type, asset type, and visual format into three "
                            "separate axes; ad_type is a deprecated alias for "
                            "visual_format. Combine a creative dimension here with a "
                            "demographic column for mixed audience reads."
                        ),
                    },
                    "columns": {
                        "type": "string",
                        "default": "messaging_angle",
                        "description": (
                            "Matrix column dimension, e.g. messaging_angle, visual_format, "
                            "asset_type, media_type, format, hook, persona, product, "
                            "offer_type, demographic_gender, demographic_age, "
                            "demographic_segment, or demographic_signal (ad_type is a "
                            "deprecated alias for visual_format). Set one axis to a "
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
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Maximum fatigue watch groups to rank in the strategy report",
                    },
                    "response_format": {
                        "type": "string",
                        "enum": ["concise", "detailed"],
                        "default": "concise",
                        "description": (
                            "concise returns an agent-ready bounded report; detailed "
                            "adds richer report fields for explicit deep dives. Both "
                            "formats remain bounded by max_cells"
                        ),
                    },
                    "max_cells": {
                        "type": "integer",
                        "default": 24,
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Maximum matrix cells returned in either response format",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 25,
                    },
                },
            },
        ),
        Tool(
            name="get_brain_learnings",
            description=(
                "Return auto-written Brand Brain learnings from saved performance, "
                "strategy, taxonomy, and audience data. Use this when an agent needs "
                "the current test conclusions, working patterns, watchouts, audience "
                "efficiency observations, fatigue, and gap learnings plus an agent_context "
                "brief seed. Supports focused reads like conclusion-only, "
                "working-only, or audience-only learnings, including audience "
                "fatigue reads grouped by demographic_age, demographic_gender, "
                "demographic_segment, or demographic_signal. Audience filters can "
                "also isolate higher- or lower-observed-efficiency learnings, and "
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
                            "roas, cpa, ctr, cpm, cvr, thumbstop_rate, hook_rate, hold_rate, "
                            "video_completion_rate, video_50_rate, video_75_rate, funnel_score, "
                            "frequency, outbound_ctr, outbound_clicks, landing_page_views, adds_to_cart, "
                            "atc_per_lpv, or video_3s_views"
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
                    "conclusion_recency_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Optional recency filter for conclusion stories relative to the report end date",
                    },
                    "audience_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "enum": [
                            "all",
                            "higher_observed_efficiency",
                            "lower_observed_efficiency",
                        ],
                        "description": "Optional audience signal filter when kinds includes audience: all, higher_observed_efficiency, or lower_observed_efficiency",
                    },
                    "audience_limit": {
                        "type": "integer",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Maximum audience learning stories to return when audience signals are included",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 8,
                        "minimum": 1,
                        "maximum": 12,
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
                "demographic_signal. Audience filters can isolate higher- or "
                "lower-observed-efficiency learnings before saving, and "
                "watch_coverage_focus can "
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
                            "roas, cpa, ctr, cpm, cvr, thumbstop_rate, hook_rate, hold_rate, "
                            "video_completion_rate, video_50_rate, video_75_rate, funnel_score, "
                            "frequency, outbound_ctr, outbound_clicks, landing_page_views, adds_to_cart, "
                            "atc_per_lpv, or video_3s_views"
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
                    "conclusion_recency_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Optional recency filter for conclusion stories relative to the report end date",
                    },
                    "audience_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "enum": [
                            "all",
                            "higher_observed_efficiency",
                            "lower_observed_efficiency",
                        ],
                        "description": "Optional audience signal filter when kinds includes audience: all, higher_observed_efficiency, or lower_observed_efficiency",
                    },
                    "audience_limit": {
                        "type": "integer",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 10,
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
                        "minimum": 1,
                        "maximum": 12,
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
                "filters without the full response wrapper, including strategy queries "
                "for the next matrix view, time-series follow-up queries, and "
                "watch_coverage_focus for time-series sync-quality reads. Audience "
                "filters use higher_observed_efficiency or lower_observed_efficiency."
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
                            "roas, cpa, ctr, cpm, cvr, thumbstop_rate, hook_rate, hold_rate, "
                            "video_completion_rate, video_50_rate, video_75_rate, funnel_score, "
                            "frequency, outbound_ctr, outbound_clicks, landing_page_views, adds_to_cart, "
                            "atc_per_lpv, or video_3s_views"
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
                    "conclusion_recency_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Optional recency filter for conclusion stories relative to the report end date",
                    },
                    "audience_signal_focus": {
                        "type": "string",
                        "default": "all",
                        "enum": [
                            "all",
                            "higher_observed_efficiency",
                            "lower_observed_efficiency",
                        ],
                        "description": "Optional audience signal filter when kinds includes audience: all, higher_observed_efficiency, or lower_observed_efficiency",
                    },
                    "audience_limit": {
                        "type": "integer",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Maximum audience learning stories to include in the exported context",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 8,
                        "minimum": 1,
                        "maximum": 12,
                        "description": "Maximum learning stories to include in the exported context",
                    },
                },
            },
        ),
        Tool(
            name="get_performance_timeseries",
            description=(
                "Return saved performance time series for creative or campaign fatigue "
                "checks. Use this to inspect dated ROAS, CPA, CTR, CPM, CVR, "
                "thumbstop, hook, hold, video quartile, delivery, outbound, "
                "mid-funnel, or funnel trends per creative, campaign, landing page, "
                "hook, angle, ad type, format, visual style, CTA, analysis id, or "
                "audience slice, plus the same fatigue decay signal the strategy "
                "matrix uses. Supports trajectory filters for worsening, improving, "
                "flat, or insufficient-data reads, plus coverage-risk filters for "
                "gappy, short-window, insufficient-point, windowed-history, or "
                "call-ready histories."
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
                            "roas, cpa, ctr, cpm, cvr, thumbstop_rate, hook_rate, hold_rate, "
                            "video_completion_rate, video_50_rate, video_75_rate, funnel_score, "
                            "frequency, outbound_ctr, outbound_clicks, landing_page_views, adds_to_cart, "
                            "atc_per_lpv, or video_3s_views"
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
                        "minimum": 1,
                        "maximum": 10,
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
                "validate, hold, or sync more data without opening the dashboard. "
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
                            "roas, cpa, ctr, cpm, cvr, thumbstop_rate, hook_rate, hold_rate, "
                            "video_completion_rate, video_50_rate, video_75_rate, funnel_score, "
                            "frequency, outbound_ctr, outbound_clicks, landing_page_views, adds_to_cart, "
                            "atc_per_lpv, or video_3s_views"
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
                        "minimum": 1,
                        "maximum": 10,
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
                    "limit": {
                        "type": "integer",
                        "default": 12,
                        "minimum": 1,
                        "maximum": 50,
                    },
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
                    "limit": {
                        "type": "integer",
                        "default": 12,
                        "minimum": 1,
                        "maximum": 50,
                    },
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
                "Observational pre-flight against the brand's tag-level history; not a "
                "forecast or causal estimate. Pass analysis_id or attributes. Returns "
                "evidence and controlled-test hypothesis candidates; predeclare objective_metric "
                "for mixed, blank, or unknown objectives."
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
                    "objective_metric": {
                        "type": "string",
                        "enum": [
                            "roas",
                            "cpa",
                            "ctr",
                            "thumbstop_rate",
                            "video_completion_rate",
                            "funnel_score",
                        ],
                        "description": "Predeclared metric for mixed/blank/unknown objectives",
                    },
                    "goal_direction": {
                        "type": "string",
                        "enum": ["higher_better", "lower_better"],
                        "description": "Optional direction; must agree with the metric",
                    },
                },
            },
        ),
        Tool(
            name="get_demographics_performance",
            description=(
                "Return saved age x gender delivery with account-relative higher and "
                "lower observed-return-per-spend bands. These are descriptive "
                "associations, not audience outcome or action verdicts. "
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
            name="export_demographics_context",
            description=(
                "Return an agent-ready audience context payload from saved age x "
                "gender performance memory. Use this when another agent or workflow "
                "needs the higher and lower observed-efficiency bands, account totals, "
                "summary text, a prompt-ready descriptive review queue, and date-scoped "
                "mixed creative x audience strategy queries plus time-series "
                "follow-up queries without opening the dashboard."
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
                    "limit": {
                        "type": "integer",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Maximum segments from each observed-efficiency band to include in the exported context",
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
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "minimum": 1,
                        "maximum": 50,
                    },
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
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum number of saved scans/imports to return",
                    },
                },
            },
        ),
        Tool(
            name="get_competitor_scan_detail",
            description=(
                "Return one saved competitor scan/import: ads, analyses, and "
                "strategy breakdown."
            ),
            inputSchema={
                "type": "object",
                "required": ["scan_id"],
                "properties": {
                    "scan_id": {"type": "integer"},
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
    ]
    return _visible_tools(_compact_tool_catalog(tools))


@server.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[TextContent] | CallToolResult:
    try:
        if name in INTERNAL_BACKFILL_TOOLS and not _is_internal_backfill_enabled():
            return _mcp_error(
                "Internal backfill tools are disabled. Connect Meta through "
                "Creative Tagger OAuth and use sync_meta_performance or "
                "scan_competitor for launch customer workflows."
            )
        if name == "generate_naming":
            return _mcp_result(_generate_naming(arguments))
        handlers = {
            "analyze_creative": _analyze_creative,
            "get_taxonomy": _get_taxonomy,
            "list_workspaces": _list_workspaces,
            "list_library": _list_library,
            "get_library_patterns": _get_library_patterns,
            "get_analysis": _get_analysis,
            "recommend": _recommend,
            "analyze_gaps": _analyze_gaps,
            "get_brand_context": _get_brand_context,
            "set_brand_context": _set_brand_context,
            "get_brand_taxonomy": _get_brand_taxonomy,
            "set_brand_taxonomy_value": _set_brand_taxonomy_value,
            "delete_brand_taxonomy_value": _delete_brand_taxonomy_value,
            "set_brand_entity": _set_brand_entity,
            "delete_brand_entity": _delete_brand_entity,
            "get_naming_variables": _get_naming_variables,
            "list_naming_templates": _list_naming_templates,
            "save_naming_template": _save_naming_template,
            "delete_naming_template": _delete_naming_template,
            "preview_naming_template": _preview_naming_template,
            "get_meta_status": _get_meta_status,
            "sync_meta_performance": _sync_meta_performance,
            "get_meta_performance_summary": _get_meta_performance_summary,
            "get_taxonomy_performance": _get_taxonomy_performance,
            "get_prebuilt_reports": _get_prebuilt_reports,
            "get_creative_strategy_report": _get_creative_strategy_report,
            "get_brain_learnings": _get_brain_learnings,
            "save_brain_learnings": _save_brain_learnings,
            "export_brain_learnings_context": _export_brain_learnings_context,
            "get_performance_timeseries": _get_performance_timeseries,
            "export_performance_timeseries_context": _export_performance_timeseries_context,
            "create_custom_report": _create_custom_report,
            "list_custom_reports": _list_custom_reports,
            "save_custom_report": _save_custom_report,
            "run_saved_custom_report": _run_saved_custom_report,
            "delete_custom_report": _delete_saved_custom_report,
            "predict_creative": _predict_creative,
            "get_demographics_performance": _get_demographics_performance,
            "export_demographics_context": _export_demographics_context,
            "generate_brand_taxonomy": _generate_brand_taxonomy,
            "scan_competitor": _scan_competitor,
            "get_competitor_scan_history": _get_competitor_scan_history,
            "get_competitor_scan_detail": _get_competitor_scan_detail,
            "import_competitor_ads": _import_competitor_ads,
        }
        handler = handlers.get(name)
        if handler is None:
            return _mcp_error(f"Unknown tool: {name}")
        return _mcp_result(await handler(arguments))
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        return _mcp_error(f"API error ({e.response.status_code}): {detail}")
    except httpx.ConnectError:
        return _mcp_error(
            f"Cannot connect to Creative Tagger API at {API_URL}. "
            "Set CREATIVE_TAGGER_URL or check the API is running."
        )
    except httpx.TimeoutException:
        return _mcp_error(
            f"Timed out waiting for a response from {API_URL}. "
            "The API may be slow or unreachable."
        )
    except Exception as e:
        return _mcp_error(str(e))


# ---------- Tool Implementations ----------


async def _analyze_creative(args: dict) -> list[TextContent]:
    file_path = args.get("file_path")
    file_paths = args.get("file_paths") or []
    url = args.get("url")
    html_content = args.get("html_content")
    brand_name = args.get("brand_name", "Brand")
    data = _analysis_form_data(args, brand_name)

    async with httpx.AsyncClient(timeout=180.0, headers=_headers()) as client:
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
    dimension = str(args.get("dimension") or "").strip().lower()
    if not dimension:
        return _text(taxonomy_payload())

    normalized = dimension.replace("-", "_").replace(" ", "_")
    if normalized in CONTROLLED_DIMENSIONS:
        return _text(
            {
                "taxonomy_version": TAXONOMY_VERSION,
                "dimension": normalized,
                "kind": "controlled",
                "values": list(CONTROLLED_DIMENSIONS[normalized]),
            }
        )
    if normalized in DERIVED_OPEN_DIMENSIONS:
        spec = DERIVED_OPEN_DIMENSIONS[normalized]
        return _text(
            {
                "taxonomy_version": TAXONOMY_VERSION,
                "dimension": normalized,
                "kind": "derived_open",
                "canonical_values": list(spec["canonical_values"]),
                "allow_other_values": spec["allow_other_values"],
                "description": spec["description"],
            }
        )
    if normalized in DYNAMIC_DIMENSIONS:
        return _text(
            {
                "taxonomy_version": TAXONOMY_VERSION,
                "dimension": normalized,
                "kind": "dynamic",
                "description": DYNAMIC_DIMENSIONS[normalized],
            }
        )

    available = sorted(
        (*CONTROLLED_DIMENSIONS, *DERIVED_OPEN_DIMENSIONS, *DYNAMIC_DIMENSIONS)
    )
    return _err(f"Unknown dimension: {dimension}. Available: {', '.join(available)}")


async def _list_workspaces(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(f"{API_URL}/auth/workspaces", params=_auth_params())
        resp.raise_for_status()
        return _text(resp.json())


async def _list_library(args: dict) -> list[TextContent]:
    params: dict[str, Any] = {**_auth_params()}
    try:
        if args.get("limit") is not None:
            params["limit"] = _clamped_int_arg(
                args["limit"],
                default=50,
                minimum=1,
                maximum=LIBRARY_PAGE_LIMIT,
                field_name="limit",
            )
        if args.get("offset") is not None:
            params["offset"] = _clamped_int_arg(
                args["offset"],
                default=0,
                minimum=0,
                field_name="offset",
            )
    except ValueError as exc:
        return _err(str(exc))
    for k in (
        "brand_name",
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(f"{API_URL}/auth/library", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_library_patterns(args: dict) -> list[TextContent]:
    params = {**_auth_params()}
    if args.get("brand_name") is not None:
        params["brand_name"] = args["brand_name"]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(f"{API_URL}/auth/library/patterns", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_analysis(args: dict) -> list[TextContent]:
    analysis_id = args.get("analysis_id")
    if not analysis_id:
        return _err("analysis_id is required")
    params = {**_auth_params()}
    if args.get("brand_name") is not None:
        params["brand_name"] = args["brand_name"]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(f"{API_URL}/auth/library/{analysis_id}", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _recommend(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    question = args.get("question", "")
    if not brand_name or not question:
        return _err("brand_name and question are required")
    async with httpx.AsyncClient(timeout=120.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=120.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    # PATCH is intentionally sparse: the API distinguishes an omitted field
    # (preserve) from an explicit empty string/list (clear). Materializing
    # defaults here would erase long-term memory and reference assets during a
    # notes-only or voice-only update.
    body = {"brand_name": brand_name}
    for field in (
        "voice",
        "target_audience",
        "top_performers",
        "anti_patterns",
        "notes",
    ):
        if field in args:
            body[field] = args[field]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.patch(
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.delete(f"{API_URL}/auth/brand-taxonomy/entities", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_naming_variables(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(f"{API_URL}/auth/naming/variables")
        resp.raise_for_status()
        return _text(resp.json())


async def _list_naming_templates(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.post(
            f"{API_URL}/auth/naming/templates",
            params=_auth_params(),
            json=body,
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _delete_naming_template(args: dict) -> list[TextContent]:
    params = {**_auth_params(), "name": args.get("name", "default")}
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.post(f"{API_URL}/auth/naming/preview", json=body)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_meta_status(args: dict) -> list[TextContent]:
    params = {**_auth_params()}
    if args.get("brand_name") is not None:
        params["brand_name"] = args["brand_name"]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/auth/meta/status", params=params, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _sync_meta_performance(args: dict) -> list[TextContent]:
    body = {
        "brand_name": args.get("brand_name", ""),
        "account_id": args.get("account_id", ""),
        "date_preset": args.get("date_preset", "last_30d"),
        "attribution_windows": _string_list_arg(args.get("attribution_windows")),
    }
    async with httpx.AsyncClient(timeout=120.0, headers=_headers()) as client:
        resp = await client.post(
            f"{API_URL}/meta/sync", json=body, headers=_headers()
        )
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    data: dict[str, Any] = {
        "contract_version": PREDICTION_CONTRACT_VERSION,
        "brand_name": brand_name,
    }
    if args.get("analysis_id") is not None:
        data["analysis_id"] = args["analysis_id"]
    if args.get("attributes"):
        import json as _json

        data["attributes"] = _json.dumps(args["attributes"])
    if args.get("objective_metric"):
        data["objective_metric"] = str(args["objective_metric"])
    if args.get("goal_direction"):
        data["goal_direction"] = str(args["goal_direction"])
    async with httpx.AsyncClient(timeout=60.0, headers=_headers()) as client:
        resp = await client.post(f"{API_URL}/predict", data=data, headers=_headers())
        resp.raise_for_status()
        return _text(_validated_observational_prediction(resp.json()))


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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/performance/by-taxonomy",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_prebuilt_reports(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=8,
            minimum=1,
            maximum=PREBUILT_REPORT_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "spend_threshold": args.get("spend_threshold", 500),
        "limit": limit,
    }
    if args.get("report_id"):
        params["report_id"] = args["report_id"]
    if args.get("start_date"):
        params["start_date"] = args["start_date"]
    if args.get("end_date"):
        params["end_date"] = args["end_date"]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/reports/prebuilt",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_creative_strategy_report(args: dict) -> list[TextContent]:
    try:
        params = _strategy_params(args)
    except ValueError as exc:
        return _err(str(exc))
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/reports/creative-strategy",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_brain_learnings(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=8,
            minimum=1,
            maximum=BRAIN_STORY_LIMIT,
            field_name="limit",
        )
        audience_limit = _clamped_int_arg(
            args.get("audience_limit"),
            default=3,
            minimum=1,
            maximum=BRAIN_AUDIENCE_LIMIT,
            field_name="audience_limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "date_preset": args.get("date_preset", "all_time"),
        "limit": limit,
        "audience_limit": audience_limit,
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
        "conclusion_recency_days",
        "audience_signal_focus",
    ):
        if key in {"watch_sources", "kinds", "conclusion_statuses"}:
            value = _csv_arg(args.get(key))
            if value:
                params[key] = value
            continue
        if args.get(key) not in (None, ""):
            value = args[key]
            if key == "audience_signal_focus":
                value = _canonical_audience_signal_focus(value)
            params[key] = value
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=8,
            minimum=1,
            maximum=BRAIN_STORY_LIMIT,
            field_name="limit",
        )
        audience_limit = _clamped_int_arg(
            args.get("audience_limit"),
            default=3,
            minimum=1,
            maximum=BRAIN_AUDIENCE_LIMIT,
            field_name="audience_limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    body: dict[str, Any] = {
        "brand_name": brand_name,
        "date_preset": args.get("date_preset", "all_time"),
        "include_gaps_in_notes": _coerce_bool(
            args.get("include_gaps_in_notes", False)
        ),
        "limit": limit,
        "audience_limit": audience_limit,
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
        "conclusion_recency_days",
        "audience_signal_focus",
    ):
        if key in {"watch_sources", "kinds", "conclusion_statuses"}:
            value = _csv_arg(args.get(key))
            if value:
                body[key] = value
            continue
        if args.get(key) not in (None, ""):
            value = args[key]
            if key == "audience_signal_focus":
                value = _canonical_audience_signal_focus(value)
            body[key] = value
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    summary = parsed.get("summary") or {}
    try:
        limit = _clamped_int_arg(
            summary.get("requested_limit", args.get("limit")),
            default=8,
            minimum=1,
            maximum=BRAIN_STORY_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    brand_name = parsed.get("brand_name", args.get("brand_name", ""))
    learnings = [
        item
        for item in list(parsed.get("learnings") or [])[:limit]
        if isinstance(item, dict)
    ]
    export = {
        **context,
        "brand_name": brand_name,
        "generated_at": parsed.get("generated_at", ""),
        "hero": parsed.get("hero") or {},
        "summary": summary,
        "controls": parsed.get("controls") or {},
        "source_summary": parsed.get("source_summary") or {},
        "learnings": learnings,
        "decision_queue": _build_brain_learning_decision_queue(
            learnings=learnings,
            brand_name=brand_name,
            date_preset=str(summary.get("date_preset") or args.get("date_preset") or "all_time"),
            start_date=str(summary.get("start_date") or args.get("start_date") or ""),
            end_date=str(summary.get("end_date") or args.get("end_date") or ""),
            watch_metric=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("metric")
                or summary.get("watch_metric")
                or args.get("watch_metric")
                or "roas"
            ),
            watch_signal_focus=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("signal_focus")
                or summary.get("watch_signal_focus")
                or args.get("watch_signal_focus")
                or "all"
            ),
            watch_trajectory_focus=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("trajectory_focus")
                or summary.get("watch_trajectory_focus")
                or args.get("watch_trajectory_focus")
                or "all"
            ),
            watch_coverage_focus=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("coverage_focus")
                or summary.get("watch_coverage_focus")
                or args.get("watch_coverage_focus")
                or "all"
            ),
            watch_minimum_points=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("minimum_points")
                or summary.get("watch_minimum_points")
                or args.get("watch_minimum_points")
                or 2
            ),
            watch_minimum_calendar_days=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get(
                    "minimum_calendar_days"
                )
                or summary.get("watch_minimum_calendar_days")
                or args.get("watch_minimum_calendar_days")
                or 0
            ),
            watch_maximum_gap_days=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get(
                    "maximum_gap_days"
                )
                or summary.get("watch_maximum_gap_days")
                or args.get("watch_maximum_gap_days")
                or 0
            ),
            fatigue_decay_threshold=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get(
                    "fatigue_decay_threshold"
                )
                or summary.get("fatigue_decay_threshold")
                or args.get("fatigue_decay_threshold")
                or 0.18
            ),
            limit=limit,
        ),
        "suggested_strategy_views": _build_brain_learning_strategy_views(
            learnings=learnings,
            brand_name=brand_name,
            date_preset=str(summary.get("date_preset") or args.get("date_preset") or "all_time"),
            start_date=str(summary.get("start_date") or args.get("start_date") or ""),
            end_date=str(summary.get("end_date") or args.get("end_date") or ""),
            limit=limit,
        ),
        "suggested_timeseries_views": _build_brain_learning_timeseries_views(
            learnings=learnings,
            brand_name=brand_name,
            date_preset=str(summary.get("date_preset") or args.get("date_preset") or "all_time"),
            start_date=str(summary.get("start_date") or args.get("start_date") or ""),
            end_date=str(summary.get("end_date") or args.get("end_date") or ""),
            watch_metric=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("metric")
                or summary.get("watch_metric")
                or args.get("watch_metric")
                or "roas"
            ),
            watch_signal_focus=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("signal_focus")
                or summary.get("watch_signal_focus")
                or args.get("watch_signal_focus")
                or "all"
            ),
            watch_trajectory_focus=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("trajectory_focus")
                or summary.get("watch_trajectory_focus")
                or args.get("watch_trajectory_focus")
                or "all"
            ),
            watch_coverage_focus=str(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("coverage_focus")
                or summary.get("watch_coverage_focus")
                or args.get("watch_coverage_focus")
                or "all"
            ),
            watch_minimum_points=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get("minimum_points")
                or summary.get("watch_minimum_points")
                or args.get("watch_minimum_points")
                or 2
            ),
            watch_minimum_calendar_days=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get(
                    "minimum_calendar_days"
                )
                or summary.get("watch_minimum_calendar_days")
                or args.get("watch_minimum_calendar_days")
                or 0
            ),
            watch_maximum_gap_days=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get(
                    "maximum_gap_days"
                )
                or summary.get("watch_maximum_gap_days")
                or args.get("watch_maximum_gap_days")
                or 0
            ),
            fatigue_decay_threshold=(
                ((parsed.get("source_summary") or {}).get("timeseries") or {}).get(
                    "fatigue_decay_threshold"
                )
                or summary.get("fatigue_decay_threshold")
                or args.get("fatigue_decay_threshold")
                or 0.18
            ),
            limit=limit,
        ),
    }
    return _text(export)


async def _get_performance_timeseries(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=10,
            minimum=1,
            maximum=TIMESERIES_SERIES_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "date_preset": args.get("date_preset", "last_30d"),
        "group_by": args.get("group_by", "ad_name"),
        "metric": args.get("metric", "roas"),
        "signal_focus": args.get("signal_focus", "all"),
        "trajectory_focus": args.get("trajectory_focus", "all"),
        "coverage_focus": args.get("coverage_focus", "all"),
        "limit": limit,
        "minimum_spend": args.get("minimum_spend", 500),
        "minimum_points": args.get("minimum_points", 0),
        "minimum_calendar_days": args.get("minimum_calendar_days", 0),
        "maximum_gap_days": args.get("maximum_gap_days", 0),
        "fatigue_decay_threshold": args.get("fatigue_decay_threshold", 0.18),
    }
    for key in ("start_date", "end_date"):
        if args.get(key) not in (None, ""):
            params[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=12,
            minimum=1,
            maximum=CUSTOM_REPORT_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    payload = {
        "brand_name": brand_name,
        "title": args.get("title") or "Custom Report",
        "dimensions": dimensions,
        "layer": args.get("layer", "standard"),
        "metric": args.get("metric", "roas"),
        "spend_threshold": args.get("spend_threshold", 500),
        "limit": limit,
    }
    for key in ("start_date", "end_date"):
        if args.get(key) not in (None, ""):
            payload[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.post(
            f"{API_URL}/reports/custom",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _list_custom_reports(args: dict) -> list[TextContent]:
    params = {"brand_name": args.get("brand_name", "")}
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=12,
            minimum=1,
            maximum=CUSTOM_REPORT_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
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
        "limit": limit,
    }
    for key in ("start_date", "end_date"):
        if args.get(key) not in (None, ""):
            payload[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
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
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/performance/demographics",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


def _demographic_segment_label(segment: dict[str, Any]) -> str:
    age = str(segment.get("age") or "unknown").strip() or "unknown"
    gender = str(segment.get("gender") or "unknown").strip() or "unknown"
    return f"{age} / {gender}"


def _compact_demographic_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment": _demographic_segment_label(segment),
        "observed_efficiency_band": (
            segment.get("observed_efficiency_band")
            or "near_account_observed_return_per_spend"
        ),
        "return_per_spend_percentile": segment.get("return_per_spend_percentile"),
        "spend": segment.get("spend", 0),
        "revenue": segment.get("revenue", 0),
        "roas": segment.get("roas", 0),
        "ctr": segment.get("ctr", 0),
        "cpa": segment.get("cpa", 0),
        "conversions": segment.get("conversions", 0),
        "lpv_rate": segment.get("lpv_rate", 0),
        "atc_per_lpv": segment.get("atc_per_lpv", 0),
        "goodness": segment.get("goodness"),
    }


def _format_demographic_evidence(segment):
    spend = float(segment.get("spend") or 0)
    roas = float(segment.get("roas") or 0)
    ctr = float(segment.get("ctr") or 0)
    cpa = float(segment.get("cpa") or 0)
    conversions = float(segment.get("conversions") or 0)
    return (
        f"${spend:.0f} spend, {roas:.2f}x ROAS, {ctr:.2f}% CTR, "
        f"${cpa:.2f} CPA, {conversions:.0f} conversions"
    )


def _build_demographics_decision_queue(
    higher_observed_efficiency,
    lower_observed_efficiency,
    *,
    limit,
):
    capped_limit = max(1, min(int(limit or 3), 6))
    queue = []
    for segment in list(higher_observed_efficiency or [])[:capped_limit]:
        compact = _compact_demographic_segment(segment)
        queue.append(
            {
                **compact,
                "action": "review_observed_delivery",
                "recommendation": (
                    f"Review {compact['segment']} as a higher observed-return-per-spend "
                    "band in this account window; do not infer an audience outcome."
                ),
                "evidence_summary": _format_demographic_evidence(compact),
                "evidence_type": "observational_association",
                "causal_claim": False,
                "observation_plan": {
                    "mode": "descriptive_observation",
                    "measure": "pending_predeclare",
                    "direction": "pending_predeclare",
                    "comparison": "account_context_only",
                    "interpretation": "association_not_causation",
                    "next_step": "collect_comparable_delivery",
                },
            }
        )
    remaining = max(1, capped_limit - len(queue))
    for segment in list(lower_observed_efficiency or [])[:remaining]:
        compact = _compact_demographic_segment(segment)
        queue.append(
            {
                **compact,
                "action": "review_observed_delivery",
                "recommendation": (
                    f"Review {compact['segment']} as a lower observed-return-per-spend "
                    "band in this account window; do not infer an audience outcome."
                ),
                "evidence_summary": _format_demographic_evidence(compact),
                "evidence_type": "observational_association",
                "causal_claim": False,
                "observation_plan": {
                    "mode": "descriptive_observation",
                    "measure": "pending_predeclare",
                    "direction": "pending_predeclare",
                    "comparison": "account_context_only",
                    "interpretation": "association_not_causation",
                    "next_step": "collect_comparable_delivery",
                },
            }
        )
    for index, item in enumerate(queue, start=1):
        item["rank"] = index
    return queue


def _build_demographic_focus_views(
    segments,
    *,
    brand_name: str = "",
    date_preset: str = "all_time",
    start_date: str = "",
    end_date: str = "",
    limit: int = 2,
):
    capped_limit = max(1, min(int(limit or 2), 4))
    focus_views = []
    view_specs = [
        {
            "label_prefix": "Angles for",
            "report_template": "angle-audience-fit",
            "rows": "messaging_angle",
            "columns": "demographic_segment",
            "fill_metric": "roas",
            "metrics": ["spend", "roas", "ctr", "cpa", "conversions", "revenue"],
            "why": "Open the audience-segment column first, then compare historical messaging-angle associations inside that pocket.",
        },
        {
            "label_prefix": "Hooks for",
            "report_template": "hook-audience-fit",
            "rows": "hook",
            "columns": "demographic_segment",
            "fill_metric": "hook_rate",
            "metrics": ["spend", "hook_rate", "hold_rate", "roas", "ctr", "cpa"],
            "why": "Open the same audience-segment column to see whether the opening pattern, not the whole concept, needs to change.",
        },
    ]
    for segment in list(segments or [])[:capped_limit]:
        compact = _compact_demographic_segment(segment)
        item = {
            **compact,
            "evidence_summary": _format_demographic_evidence(compact),
            "strategy_views": [],
        }
        for spec in view_specs:
            query = _build_demographics_strategy_query(
                brand_name=brand_name,
                report_template=spec["report_template"],
                rows=spec["rows"],
                columns=spec["columns"],
                fill_metric=spec["fill_metric"],
                metrics=spec["metrics"],
                date_preset=date_preset,
                start_date=start_date,
                end_date=end_date,
            )
            query["focus_segment"] = compact["segment"]
            item["strategy_views"].append(
                {
                    "label": f"{spec['label_prefix']} {compact['segment']}",
                    "focus_segment": compact["segment"],
                    "observed_efficiency_band": compact[
                        "observed_efficiency_band"
                    ],
                    "why": spec["why"],
                    "strategy_query": query,
                }
            )
        focus_views.append(item)
    return focus_views


def _build_demographic_timeseries_query(
    *,
    brand_name: str,
    group_by: str,
    metric: str,
    date_preset: str,
    start_date: str,
    end_date: str,
    signal_focus: str = "all",
    trajectory_focus: str = "all",
    coverage_focus: str = "all",
    minimum_spend: float = 0,
    minimum_points: int = 2,
    minimum_calendar_days: int = 0,
    maximum_gap_days: int = 0,
    fatigue_decay_threshold: float = 0.18,
    focus_value: str = "",
) -> dict[str, Any]:
    query = {
        "tool": "get_performance_timeseries",
        "brand_name": brand_name,
        "group_by": group_by,
        "metric": metric,
        "date_preset": date_preset or "all_time",
        "signal_focus": signal_focus or "all",
        "trajectory_focus": trajectory_focus or "all",
        "coverage_focus": coverage_focus or "all",
        "minimum_spend": minimum_spend,
        "minimum_points": minimum_points,
        "minimum_calendar_days": minimum_calendar_days,
        "maximum_gap_days": maximum_gap_days,
        "fatigue_decay_threshold": fatigue_decay_threshold,
    }
    if focus_value:
        query["focus_value"] = focus_value
    if start_date:
        query["start_date"] = start_date
    if end_date:
        query["end_date"] = end_date
    return query


def _build_demographic_timeseries_views(
    *,
    brand_name: str = "",
    date_preset: str = "all_time",
    start_date: str = "",
    end_date: str = "",
):
    views = [
        {
            "label": "Audience trend watch",
            "group_by": "demographic_segment",
            "metric": "roas",
            "signal_focus": "all",
            "trajectory_focus": "all",
            "coverage_focus": "all",
            "why": "Track raw audience movement and data coverage without assigning an outcome direction.",
        },
        {
            "label": "Audience signal trend",
            "group_by": "demographic_signal",
            "metric": "roas",
            "signal_focus": "all",
            "trajectory_focus": "all",
            "coverage_focus": "all",
            "why": "Watch how account-relative observed-efficiency bands move across repeated sync windows.",
        },
    ]
    for view in views:
        view["timeseries_query"] = _build_demographic_timeseries_query(
            brand_name=brand_name,
            group_by=view["group_by"],
            metric=view["metric"],
            date_preset=date_preset,
            start_date=start_date,
            end_date=end_date,
            signal_focus=view["signal_focus"],
            trajectory_focus=view["trajectory_focus"],
            coverage_focus=view["coverage_focus"],
        )
    return views


def _build_demographic_segment_timeseries_views(
    segments,
    *,
    brand_name: str = "",
    date_preset: str = "all_time",
    start_date: str = "",
    end_date: str = "",
    limit: int = 2,
):
    capped_limit = max(1, min(int(limit or 2), 4))
    focus_views = []
    for segment in list(segments or [])[:capped_limit]:
        compact = _compact_demographic_segment(segment)
        focus_views.append(
            {
                **compact,
                "evidence_summary": _format_demographic_evidence(compact),
                "timeseries_views": [
                    {
                        "label": f"Trend for {compact['segment']}",
                        "focus_segment": compact["segment"],
                        "observed_efficiency_band": compact[
                            "observed_efficiency_band"
                        ],
                        "why": "Compare raw movement across repeated sync windows without assigning an outcome direction.",
                        "timeseries_query": _build_demographic_timeseries_query(
                            brand_name=brand_name,
                            group_by="demographic_segment",
                            metric="roas",
                            date_preset=date_preset,
                            start_date=start_date,
                            end_date=end_date,
                            focus_value=compact["segment"],
                        ),
                    }
                ],
            }
        )
    return focus_views


def _build_demographics_strategy_query(
    *,
    brand_name: str,
    report_template: str,
    rows: str,
    columns: str,
    fill_metric: str,
    metrics: list[str],
    date_preset: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    query = {
        "tool": "get_creative_strategy_report",
        "brand_name": brand_name,
        "report_template": report_template,
        "rows": rows,
        "columns": columns,
        "status_focus": "all",
        "metric_preset": "custom",
        "metrics": list(metrics),
        "date_preset": date_preset or "all_time",
    }
    if fill_metric not in query["metrics"]:
        query["metrics"].append(fill_metric)
    if start_date:
        query["start_date"] = start_date
    if end_date:
        query["end_date"] = end_date
    return query


def _build_demographics_strategy_views(
    *,
    brand_name: str = "",
    date_preset: str = "all_time",
    start_date: str = "",
    end_date: str = "",
):
    views = [
        {
            "label": "Audience matrix",
            "report_template": "demographic-read",
            "rows": "demographic_age",
            "columns": "demographic_gender",
            "fill_metric": "roas",
            "metrics": ["spend", "roas", "ctr", "cpa", "conversions", "revenue"],
            "why": "Start with the age x gender matrix to describe account-relative delivery bands.",
        },
        {
            "label": "Audience signals",
            "report_template": "audience-signals",
            "rows": "demographic_segment",
            "columns": "demographic_signal",
            "fill_metric": "roas",
            "metrics": ["spend", "roas", "ctr", "cpa", "conversions", "revenue"],
            "why": "Compare higher and lower observed-return-per-spend bands before mixing in creative angles or hooks.",
        },
        {
            "label": "Angle x audience",
            "report_template": "angle-audience-fit",
            "rows": "messaging_angle",
            "columns": "demographic_segment",
            "fill_metric": "roas",
            "metrics": ["spend", "roas", "ctr", "cpa", "conversions", "revenue"],
            "why": "Compare historical messaging-angle associations inside each audience pocket before briefing the next test.",
        },
        {
            "label": "Hook x audience",
            "report_template": "hook-audience-fit",
            "rows": "hook",
            "columns": "demographic_segment",
            "fill_metric": "hook_rate",
            "metrics": ["spend", "hook_rate", "hold_rate", "roas", "ctr", "cpa"],
            "why": "Check whether the opening pattern changes by audience segment before rewriting the whole ad.",
        },
    ]
    for view in views:
        view["strategy_query"] = _build_demographics_strategy_query(
            brand_name=brand_name,
            report_template=view["report_template"],
            rows=view["rows"],
            columns=view["columns"],
            fill_metric=view["fill_metric"],
            metrics=view["metrics"],
            date_preset=date_preset,
            start_date=start_date,
            end_date=end_date,
        )
    return views


def _brain_learning_status_action(status: str) -> tuple[str, str]:
    normalized = str(status or "").strip().lower()
    if normalized == "winner":
        return "validate", "Retest the observed pattern before changing allocation."
    if normalized in {"fatigued", "loser"}:
        return "investigate", "Test one refresh variable before changing allocation."
    return "investigate", "Inspect the underlying slice before briefing a test."


def _brain_learning_strategy_query(
    *,
    learning: dict[str, Any],
    brand_name: str,
    date_preset: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    evidence = learning.get("evidence") or {}
    dimension = _normalize_strategy_axis(evidence.get("dimension"))
    kind = str(learning.get("kind") or "").strip().lower()
    current_status = str(evidence.get("current_status") or "").strip().lower()
    report_template = "next-tests"
    rows = "ad_type"
    columns = "messaging_angle"
    fill_metric = "roas"
    metrics = ["spend", "roas", "ctr", "cpa", "conversions", "revenue"]

    if kind == "conclusion":
        if current_status == "winner":
            report_template = "creative-winners"
        elif current_status in {"fatigued", "loser"}:
            report_template = "fatigue-watch"
    elif kind == "working":
        if dimension == "hook":
            report_template = "hook-performance"
            rows = "hook"
            columns = "ad_type"
            fill_metric = "hook_rate"
            metrics = ["spend", "hook_rate", "hold_rate", "roas", "ctr", "cpa"]
        elif dimension == "persona":
            report_template = "persona-read"
            rows = "persona"
            columns = "messaging_angle"
        elif dimension in {"messaging_angle", "ad_type", "offer_type"}:
            rows = dimension
            columns = "ad_type" if dimension != "ad_type" else "messaging_angle"
    elif kind == "watch":
        report_template = "fatigue-watch"
        if dimension in {
            "demographic_age",
            "demographic_gender",
            "demographic_segment",
            "demographic_signal",
        }:
            report_template = "angle-audience-fit"
            rows = "messaging_angle"
            columns = "demographic_segment"
        elif dimension == "hook":
            rows = "hook"
            columns = "ad_type"
            fill_metric = "hook_rate"
            metrics = ["spend", "hook_rate", "hold_rate", "roas", "ctr", "cpa"]
        elif dimension:
            rows = dimension
            columns = "ad_type" if dimension != "ad_type" else "messaging_angle"
    elif kind == "audience":
        report_template = "angle-audience-fit"
        rows = "messaging_angle"
        columns = "demographic_segment"
    elif kind == "gap":
        report_template = "coverage-gaps"
        if dimension in {"hook", "persona", "offer_type", "messaging_angle", "ad_type"}:
            rows = dimension
            columns = "ad_type" if dimension != "ad_type" else "messaging_angle"

    query = _build_demographics_strategy_query(
        brand_name=brand_name,
        report_template=report_template,
        rows=rows,
        columns=columns,
        fill_metric=fill_metric,
        metrics=metrics,
        date_preset=date_preset,
        start_date=start_date,
        end_date=end_date,
    )
    query["focus_value"] = evidence.get("value") or ""
    if current_status:
        query["focus_status"] = current_status
    return query


def _brain_learning_timeseries_query(
    *,
    learning: dict[str, Any],
    brand_name: str,
    date_preset: str,
    start_date: str,
    end_date: str,
    watch_metric: str,
    watch_signal_focus: str,
    watch_trajectory_focus: str,
    watch_coverage_focus: str,
    watch_minimum_points: Any,
    watch_minimum_calendar_days: Any,
    watch_maximum_gap_days: Any,
    fatigue_decay_threshold: Any,
) -> dict[str, Any]:
    evidence = learning.get("evidence") or {}
    kind = str(learning.get("kind") or "").strip().lower()
    source = str(learning.get("source") or "").strip().lower()
    if kind != "watch" or source != "timeseries":
        return {}
    group_by = _normalize_strategy_axis(evidence.get("dimension"))
    if not group_by or group_by in {"matrix_cell", "creative_conclusion"}:
        return {}
    query = {
        "tool": "get_performance_timeseries",
        "brand_name": brand_name,
        "group_by": group_by,
        "metric": str(evidence.get("timeseries_metric") or watch_metric or "roas"),
        "date_preset": date_preset or "last_30d",
        "signal_focus": watch_signal_focus or "all",
        "trajectory_focus": watch_trajectory_focus or "all",
        "coverage_focus": watch_coverage_focus or "all",
        "minimum_points": watch_minimum_points,
        "minimum_calendar_days": watch_minimum_calendar_days,
        "maximum_gap_days": watch_maximum_gap_days,
        "fatigue_decay_threshold": fatigue_decay_threshold,
        "focus_value": evidence.get("value") or "",
    }
    if start_date:
        query["start_date"] = start_date
    if end_date:
        query["end_date"] = end_date
    return query


def _build_brain_learning_decision_queue(
    *,
    learnings: list[dict[str, Any]],
    brand_name: str,
    date_preset: str,
    start_date: str,
    end_date: str,
    watch_metric: str = "roas",
    watch_signal_focus: str = "all",
    watch_trajectory_focus: str = "all",
    watch_coverage_focus: str = "all",
    watch_minimum_points: Any = 0,
    watch_minimum_calendar_days: Any = 0,
    watch_maximum_gap_days: Any = 0,
    fatigue_decay_threshold: Any = 0.18,
    limit: int,
) -> list[dict[str, Any]]:
    queue = []
    for rank, learning in enumerate(list(learnings or [])[:limit], start=1):
        evidence = learning.get("evidence") or {}
        kind = str(learning.get("kind") or "").strip().lower()
        if kind == "conclusion":
            action, why = _brain_learning_status_action(evidence.get("current_status") or "")
        elif kind == "working":
            action, why = "validate", "Retest the observed association against a control."
        elif kind == "watch":
            action, why = "investigate", "Open the fatigue view and test one refresh variable."
        elif kind == "audience":
            action, why = "validate_segment", "Open the mixed matrix before testing a segment-specific variant."
        elif kind == "gap":
            action, why = "test", "Brief the missing pattern instead of repeating a covered cell."
        else:
            action, why = "investigate", "Inspect the learning in Strategy before acting."
        queue.append(
            {
                "rank": rank,
                "kind": kind,
                "action": action,
                "title": learning.get("title") or "",
                "recommendation": (
                    "Hypothesis to validate: "
                    f"{learning.get('action') or learning.get('summary') or ''}"
                ),
                "evidence_summary": learning.get("summary") or "",
                "evidence_type": "observational_association",
                "causal_claim": False,
                "why": why,
                "controlled_test": {
                    "hypothesis": "Changing one named creative variable changes the primary metric.",
                    "single_variable": "declare from the selected Strategy slice",
                    "primary_metric": "declare before launch",
                    "guardrails": ["spend", "frequency", "conversion volume"],
                    "decision_rule": "Predeclare minimum data and ship/stop thresholds.",
                },
                "strategy_query": _brain_learning_strategy_query(
                    learning=learning,
                    brand_name=brand_name,
                    date_preset=date_preset,
                    start_date=start_date,
                    end_date=end_date,
                ),
                "timeseries_query": _brain_learning_timeseries_query(
                    learning=learning,
                    brand_name=brand_name,
                    date_preset=date_preset,
                    start_date=start_date,
                    end_date=end_date,
                    watch_metric=watch_metric,
                    watch_signal_focus=watch_signal_focus,
                    watch_trajectory_focus=watch_trajectory_focus,
                    watch_coverage_focus=watch_coverage_focus,
                    watch_minimum_points=watch_minimum_points,
                    watch_minimum_calendar_days=watch_minimum_calendar_days,
                    watch_maximum_gap_days=watch_maximum_gap_days,
                    fatigue_decay_threshold=fatigue_decay_threshold,
                ),
            }
        )
    return queue


def _build_brain_learning_strategy_views(
    *,
    learnings: list[dict[str, Any]],
    brand_name: str,
    date_preset: str,
    start_date: str,
    end_date: str,
    limit: int,
) -> list[dict[str, Any]]:
    views = []
    for learning in list(learnings or [])[:limit]:
        evidence = learning.get("evidence") or {}
        views.append(
            {
                "learning_id": learning.get("id") or "",
                "kind": learning.get("kind") or "",
                "title": learning.get("title") or "",
                "focus_value": evidence.get("value") or "",
                "why": (
                    "Observation to validate: "
                    f"{learning.get('action') or learning.get('summary') or ''}"
                ),
                "strategy_query": _brain_learning_strategy_query(
                    learning=learning,
                    brand_name=brand_name,
                    date_preset=date_preset,
                    start_date=start_date,
                    end_date=end_date,
                ),
            }
        )
    return views


def _build_brain_learning_timeseries_views(
    *,
    learnings: list[dict[str, Any]],
    brand_name: str,
    date_preset: str,
    start_date: str,
    end_date: str,
    watch_metric: str,
    watch_signal_focus: str,
    watch_trajectory_focus: str,
    watch_coverage_focus: str,
    watch_minimum_points: Any,
    watch_minimum_calendar_days: Any,
    watch_maximum_gap_days: Any,
    fatigue_decay_threshold: Any,
    limit: int,
) -> list[dict[str, Any]]:
    views = []
    for learning in list(learnings or [])[:limit]:
        query = _brain_learning_timeseries_query(
            learning=learning,
            brand_name=brand_name,
            date_preset=date_preset,
            start_date=start_date,
            end_date=end_date,
            watch_metric=watch_metric,
            watch_signal_focus=watch_signal_focus,
            watch_trajectory_focus=watch_trajectory_focus,
            watch_coverage_focus=watch_coverage_focus,
            watch_minimum_points=watch_minimum_points,
            watch_minimum_calendar_days=watch_minimum_calendar_days,
            watch_maximum_gap_days=watch_maximum_gap_days,
            fatigue_decay_threshold=fatigue_decay_threshold,
        )
        if not query:
            continue
        evidence = learning.get("evidence") or {}
        views.append(
            {
                "learning_id": learning.get("id") or "",
                "kind": learning.get("kind") or "",
                "title": learning.get("title") or "",
                "focus_value": evidence.get("value") or "",
                "why": (
                    "Observation to validate: "
                    f"{learning.get('action') or learning.get('summary') or ''}"
                ),
                "timeseries_query": query,
            }
        )
    return views


async def _export_demographics_context(args: dict) -> list[TextContent]:
    payload = await _get_demographics_performance(args)
    if not payload:
        return payload
    raw = getattr(payload[0], "text", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return payload
    if not isinstance(parsed, dict):
        return payload

    requested_limit = args.get("limit", 3)
    try:
        limit = _clamped_int_arg(
            requested_limit,
            default=3,
            minimum=1,
            maximum=DEMOGRAPHICS_EXPORT_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))

    higher_observed_efficiency = [
        _compact_demographic_segment(segment)
        for segment in list(parsed.get("higher_observed_efficiency") or [])[:limit]
        if isinstance(segment, dict)
    ]
    lower_observed_efficiency = [
        _compact_demographic_segment(segment)
        for segment in list(parsed.get("lower_observed_efficiency") or [])[:limit]
        if isinstance(segment, dict)
    ]
    totals = dict(parsed.get("totals") or {})
    date_window = parsed.get("date_window") or "All time"
    brand_name = parsed.get("brand_name", args.get("brand_name", ""))
    decision_queue = _build_demographics_decision_queue(
        parsed.get("higher_observed_efficiency") or [],
        parsed.get("lower_observed_efficiency") or [],
        limit=limit,
    )
    summary_text = (
        f"{brand_name or 'Audience read'}: "
        f"{len(higher_observed_efficiency)} higher and "
        f"{len(lower_observed_efficiency)} lower observed-return-per-spend "
        f"segment{'s' if (len(higher_observed_efficiency) + len(lower_observed_efficiency)) != 1 else ''} "
        f"for {date_window}. Outcome direction and audience action are withheld "
        "until a metric and direction are predeclared."
    )
    export = {
        "tool": "export_demographics_context",
        "brand_name": brand_name,
        "date_preset": parsed.get("date_preset", args.get("date_preset", "all_time")),
        "start_date": parsed.get("start_date", args.get("start_date", "")),
        "end_date": parsed.get("end_date", args.get("end_date", "")),
        "date_window": date_window,
        "total_segments": parsed.get("total_segments", 0),
        "totals": totals,
        "higher_observed_efficiency_count": len(
            parsed.get("higher_observed_efficiency") or []
        ),
        "lower_observed_efficiency_count": len(
            parsed.get("lower_observed_efficiency") or []
        ),
        "top_higher_observed_efficiency": higher_observed_efficiency,
        "top_lower_observed_efficiency": lower_observed_efficiency,
        "outcome_verdicts_withheld": bool(
            parsed.get("outcome_verdicts_withheld", True)
        ),
        "metric_predeclaration_required": bool(
            parsed.get("metric_predeclaration_required", True)
        ),
        "goal_direction_predeclaration_required": bool(
            parsed.get("goal_direction_predeclaration_required", True)
        ),
        "interpretation": parsed.get("interpretation") or (
            "observational age/gender delivery only"
        ),
        "decision_queue": decision_queue,
        "segment_strategy_views": {
            "higher_observed_efficiency": _build_demographic_focus_views(
                parsed.get("higher_observed_efficiency") or [],
                brand_name=brand_name,
                date_preset=parsed.get("date_preset", args.get("date_preset", "all_time")),
                start_date=parsed.get("start_date", args.get("start_date", "")),
                end_date=parsed.get("end_date", args.get("end_date", "")),
                limit=limit,
            ),
            "lower_observed_efficiency": _build_demographic_focus_views(
                parsed.get("lower_observed_efficiency") or [],
                brand_name=brand_name,
                date_preset=parsed.get("date_preset", args.get("date_preset", "all_time")),
                start_date=parsed.get("start_date", args.get("start_date", "")),
                end_date=parsed.get("end_date", args.get("end_date", "")),
                limit=limit,
            ),
        },
        "segment_timeseries_views": {
            "higher_observed_efficiency": _build_demographic_segment_timeseries_views(
                parsed.get("higher_observed_efficiency") or [],
                brand_name=brand_name,
                date_preset=parsed.get("date_preset", args.get("date_preset", "all_time")),
                start_date=parsed.get("start_date", args.get("start_date", "")),
                end_date=parsed.get("end_date", args.get("end_date", "")),
                limit=limit,
            ),
            "lower_observed_efficiency": _build_demographic_segment_timeseries_views(
                parsed.get("lower_observed_efficiency") or [],
                brand_name=brand_name,
                date_preset=parsed.get("date_preset", args.get("date_preset", "all_time")),
                start_date=parsed.get("start_date", args.get("start_date", "")),
                end_date=parsed.get("end_date", args.get("end_date", "")),
                limit=limit,
            ),
        },
        "suggested_strategy_views": _build_demographics_strategy_views(
            brand_name=brand_name,
            date_preset=parsed.get("date_preset", args.get("date_preset", "all_time")),
            start_date=parsed.get("start_date", args.get("start_date", "")),
            end_date=parsed.get("end_date", args.get("end_date", "")),
        ),
        "suggested_timeseries_views": _build_demographic_timeseries_views(
            brand_name=brand_name,
            date_preset=parsed.get("date_preset", args.get("date_preset", "all_time")),
            start_date=parsed.get("start_date", args.get("start_date", "")),
            end_date=parsed.get("end_date", args.get("end_date", "")),
        ),
        "summary_text": summary_text,
        "prompt": (
            "Treat these audience bands as descriptive historical associations, "
            "not causal effects or outcome verdicts. Open the per-segment mixed "
            "creative and trend views, then predeclare the objective metric and "
            "direction before interpreting raw movement."
        ),
    }
    return _text(export)


async def _generate_brand_taxonomy(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    data = {
        "brand_name": brand_name,
        "persist": str(args.get("persist", True)).lower(),
    }
    async with httpx.AsyncClient(timeout=180.0, headers=_headers()) as client:
        resp = await client.post(
            f"{API_URL}/brand-taxonomy/generate",
            data=data,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _scan_competitor(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=25,
            minimum=1,
            maximum=COMPETITOR_RESULT_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    body = {
        "brand_name": args.get("brand_name"),
        "page_id": args.get("page_id"),
        "page_name": args.get("page_name"),
        "keyword": args.get("keyword"),
        "country": args.get("country", "US"),
        "limit": limit,
        "analyze_creatives": args.get("analyze_creatives", True),
    }
    async with httpx.AsyncClient(timeout=300.0, headers=_headers()) as client:
        resp = await client.post(
            f"{API_URL}/competitors/scan", json=body, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_competitor_scan_history(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=10,
            minimum=1,
            maximum=COMPETITOR_RESULT_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    params = {
        "brand_name": args.get("brand_name", ""),
        "limit": limit,
    }
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/competitors/history", params=params, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_competitor_scan_detail(args: dict) -> list[TextContent]:
    scan_id = args.get("scan_id")
    if not scan_id:
        return _err("scan_id is required")
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/competitors/history/{scan_id}", headers=_headers()
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
    async with httpx.AsyncClient(timeout=120.0, headers=_headers()) as client:
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
