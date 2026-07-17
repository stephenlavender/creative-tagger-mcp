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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import (
    INVALID_PARAMS,
    CallToolResult,
    ErrorData,
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
    Tool,
)

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
CREATIVE_LEADERBOARD_LIMIT = 50
CREATIVE_BATCH_LIMIT = 50
PERIOD_COMPARE_LIMIT = 25

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

Conventions shared across scoped tools (stated once here instead of on every
tool): every scoped tool takes brand_name (pass the exact list_workspaces
value). Most single-window reporting tools take a date_preset of all_time,
last_7_days, last_30_days, last_90_days, or custom, with start_date/end_date
as YYYY-MM-DD; get_performance_timeseries uses the short forms
last_7d/last_30d/last_90d/maximum instead. compare_periods has its own,
narrower preset family -- period_a_preset/period_b_preset accept only
this_week, last_week, last_7_days, last_14_days, last_30_days, last_90_days,
this_month, or last_month (no all_time, and no literal "custom" value -- an
explicit period_a_start/period_a_end or period_b_start/period_b_end pair IS
that tool's custom window). Reporting tools that rank or judge creatives withhold the
comparative layer (rankings_withheld / outcome_verdicts_withheld) when the
workspace's performance evidence is not decision-safe -- stale sync, no Meta
connection -- while still returning the raw measured facts; a row spending below
the materiality floor is flagged below_min_spend and left unranked rather than
crowned on thin spend.

Fatigue-watch filters shared by get_performance_timeseries (signal_focus /
trajectory_focus / coverage_focus) and the watch_* variants on
get_brain_learnings and get_creative_strategy_report take fixed vocabularies:
the fatigue signal is all, fatigued, stable, or insufficient_data; the
trajectory is all, worsening, improving, flat, or insufficient_data; the
sync-coverage trust class is all, call_ready, gappy, insufficient_points,
short_window, or windowed_history.
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


def _parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp the same way the API's own connection
    freshness calculation does: naive timestamps are treated as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _freshness_envelope(payload: Any) -> dict[str, Any] | None:
    """Build a last_synced_at / data_age_hours / stale envelope from a
    response the API already stamped with Meta sync freshness.

    Only tools whose API response already carries an explicit top-level
    `stale` verdict (get_meta_status, get_meta_performance_summary today)
    are eligible: this never invents a staleness verdict for a tool the API
    itself hasn't judged. data_age_hours is the one derived value here — a
    precision upgrade over the API's own boolean, computed from the same
    last_synced_at the API already returned, never a new source of truth.
    Returns None when the payload carries no such signal, so callers never
    stamp a tool the API hasn't informed.
    """
    if not isinstance(payload, dict) or "stale" not in payload:
        return None
    last_synced_at = payload.get("last_synced_at")
    parsed = _parse_utc_timestamp(last_synced_at)
    data_age_hours = None
    if parsed is not None:
        age_hours = (datetime.now(timezone.utc) - parsed).total_seconds() / 3600
        data_age_hours = round(age_hours, 2)
    return {
        "last_synced_at": last_synced_at,
        "data_age_hours": data_age_hours,
        "stale": bool(payload.get("stale")),
    }


def _with_freshness_stamp(payload: Any) -> Any:
    """Attach a `freshness` envelope to a dict response when the underlying
    API payload already exposes sync freshness. The one shared helper every
    freshness-eligible tool handler calls before returning — no per-tool
    copies of the last_synced_at/stale extraction or age math.
    """
    envelope = _freshness_envelope(payload)
    if envelope is None or not isinstance(payload, dict):
        return payload
    return {**payload, "freshness": envelope}


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


# Runtime tool descriptions, compacted from the richer source descriptions above
# to keep the public catalog under its context budget. The source strings stay
# verbose for humans and for the source-parsing surface tests; every workspace/
# date/read-only convention repeated across tools lives once in
# PLAYBOOK_INSTRUCTIONS, so a compact line only carries what is unique to the
# tool plus its own honesty caveat.
_COMPACT_TOOL_DESCRIPTIONS = {
    "analyze_creative": (
        "Analyze any ad creative (image, video, carousel, landing page, or email) "
        "into structured classification across 21 taxonomy dimensions "
        "(media/asset/visual type, hook, angle, audience, CTA, emotion, audio, "
        "offer, aspect ratio, duration, and more) plus standardized naming. Provide "
        "one of file_path, url, or html_content."
    ),
    "get_taxonomy": (
        "Get Creative Tagger taxonomy v2's controlled classification vocabulary and "
        "allowed values (the API does not expose enums for every field): 15 "
        "controlled dimensions, one derived/open aspect-ratio dimension, and two "
        "dynamic brand dimensions. Call before analyze_creative; media_type, "
        "asset_type, and visual_format are three separate axes."
    ),
    "list_library": (
        "Browse the workspace's saved analysis library -- every analyze_creative "
        "result is stored. Search by filename, hook, angle, emotion, CTA, talent, "
        "offer, audio, season, or format, then sort by recency or joined Meta "
        "performance (spend, reach, roas, ctr, cpm, cpa)."
    ),
    "get_library_patterns": (
        "Portfolio pattern insights across the workspace's whole library: which "
        "hooks, angles, creative types, and emotions it over- or under-indexes on, "
        "with percentages and diversification notes. Use before recommending what to "
        "make next."
    ),
    "get_analysis": (
        "Get the full 21-dimension analysis for one saved library item by id "
        "(list_library returns summaries; this returns the complete result)."
    ),
    "recommend": (
        "Ask the Creative Strategist an open-ended question, grounded in the "
        "workspace's library patterns and saved brand context. Returns concrete "
        "creative recommendations in taxonomy values (what to test next, how to "
        "approach Q4, what UGC fits an audience)."
    ),
    "analyze_gaps": (
        "Identify coverage gaps in a brand's creative library and propose concrete "
        "next creatives that fill them. Surfaces concentration risk (e.g. 78% UGC "
        "TalkHead) and returns gap analysis plus ready-to-produce briefs."
    ),
    "set_brand_context": (
        "Create or partially update a brand's long-term context (voice, audience, "
        "top performers, anti-patterns, notes) that strategist and brief tools "
        "auto-include. Omitted fields keep saved values; an explicit empty string or "
        "list clears one. Upserts on (user, brand_name)."
    ),
    "get_brand_taxonomy": (
        "Retrieve a brand's custom taxonomy: custom values, aliases, and entities "
        "(founder, creators, products, offers, customer segments, ICPs, campaign "
        "labels) layered on top of the standard taxonomy."
    ),
    "sync_meta_performance": (
        "Trigger a read-only Meta ads performance sync for a brand, reporting "
        "summaries by standard and brand-custom taxonomy. Supports explicit "
        "attribution/lookback windows to match Ads Manager. Never creates campaigns "
        "or edits budgets."
    ),
    "get_meta_performance_summary": (
        "Read saved Meta performance memory for a brand without triggering a sync: "
        "account totals plus winners/losers by standard and brand-custom taxonomy, "
        "with explainable funnel_score signals (capture, hold, bring-to-site, "
        "convert)."
    ),
    "get_taxonomy_performance": (
        "Tag-level performance with significance gating and coverage gaps: which "
        "taxonomy values are associated with stronger historical outcomes, which are "
        "under-observed, and which standard values were never tried. Rows carry "
        "ROAS/CTR/thumbstop/funnel_score when performance memory exists."
    ),
    "get_prebuilt_reports": (
        "Motion-style prebuilt reports for a brand: best hooks, landing pages, "
        "angles, audiences, offers, CTAs, visual formats, and brand-custom values, "
        "with ROAS/spend/CTR/thumbstop/funnel_score when memory exists. Optional "
        "start_date/end_date scope the window."
    ),
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
    "create_custom_report": (
        "Build a custom performance report from chosen standard/brand taxonomy "
        "dimensions, ranking the actual matched combinations by roas, funnel_score, "
        "spend, ctr, or cpa (e.g. hook x landing_page x offer_type). Rows include "
        "parts/values so the winning combination can be explained."
    ),
    "save_custom_report": (
        "Save or update a reusable custom report definition for a brand: the "
        "taxonomy-combination view plus optional dashboard preset state (view_type, "
        "date_range, grouping, metric set, filters, sort, metric preset) and a "
        "persisted report window."
    ),
    "get_creative_leaderboard": (
        "Per-creative ranked leaderboard: which creatives to scale or kill. One row "
        "per creative for the window (default last 14 days), ranked by rank_by "
        "(roas/cpa/spend/ctr/thumbstop) with thumbnail, spend_share, measurement "
        "states, days_running, and a first/second-half trend. A creative below "
        "min_spend (default $500) is flagged below_min_spend, left unranked, and "
        "never crowned on thin spend -- but still returned and counted. "
        "direction=winners/losers slices the ranked half. rankings_withheld=true "
        "(observation-only, no ranks) when evidence is not decision-safe. "
        "launched_after/before scope the ranked population; use get_batch_readout for "
        "a same-period verdict against the rest of the account."
    ),
    "get_batch_readout": (
        "Launch-cohort batch readout: per-creative verdicts vs the rest of the "
        "account. Given a launch window (launched_after/launched_before, YYYY-MM-DD "
        "-- at least one required), returns every creative first synced in it with a "
        "three-way verdict against the same-window baseline of every OTHER creative: "
        "promising, underperforming, or insufficient_evidence (with a verdict_reason "
        "-- most often below_min_spend, expected for ~half of batches; or "
        "metric_not_applicable, e.g. roas on a leads creative, which also excludes it "
        "from the baseline). The baseline excludes the batch, so a new cohort is "
        "judged like-for-like. rank_by is roas/cpa/ctr/thumbstop. Verdicts are "
        "withheld when evidence is not decision-safe."
    ),
    "compare_periods": (
        "Period-over-period comparison: is it the ads, the auction, or the site? "
        "Compares period_a (baseline) to period_b (after); each period is a preset OR "
        "an explicit start/end (never both, non-overlapping). Returns account deltas "
        "with measurement states honored, plus a multiplicative funnel decomposition "
        "whose dominant_factor NAMES why roas/cpa moved: auction (cpm), "
        "creative_engagement (ctr), landing_conversion (cvr), order_value (aov), or "
        "mixed. When revenue reported a measured $0 it falls back to cpa and returns "
        "revenue_caution -- trust it when present: the revenue collapse, not the "
        "delivery reading, is then the likely explanation. Optional group_by adds "
        "per-value deltas and biggest_movers; verdicts withheld when evidence is not "
        "decision-safe."
    ),
    "predict_creative": (
        "Observational pre-flight against the brand's tag-level history; not a "
        "forecast or causal estimate. Pass analysis_id or attributes; returns "
        "evidence and controlled-test hypotheses. Predeclare objective_metric for "
        "mixed, blank, or unknown objectives."
    ),
    "get_demographics_performance": (
        "Read saved age x gender delivery with account-relative higher/lower "
        "observed-return-per-spend bands. These are descriptive associations, not "
        "audience outcome or action verdicts. Use date_preset or start_date/end_date "
        "to scope the window."
    ),
    "export_demographics_context": (
        "Agent-ready audience context from saved age x gender memory: higher/lower "
        "observed-efficiency bands, account totals, a descriptive review queue, and "
        "date-scoped demographic strategy plus time-series follow-up queries. "
        "Demographics are account-level only -- never a joined tag x demographic cross; "
        "the API states this via cross_contract: not_applicable, not a populated grid."
    ),
    "generate_brand_taxonomy": (
        "Auto-build a brand's entire custom taxonomy from its analyzed library: "
        "messaging themes, intended audiences, and entities (products, founders, "
        "creators, offers, segments, campaign labels). Optionally persist into Brand "
        "Taxonomy Studio for future analyses, predictions, and naming."
    ),
    "scan_competitor": (
        "Scan a competitor's Meta Ad Library ads and return classified results plus "
        "an aggregate strategy breakdown (top hooks, visual styles, CTAs, emotions, "
        "estimated spend). Provide page_id, page_name, or keyword."
    ),
    "get_competitor_scan_history": (
        "Return saved competitor Market scans/imports for the workspace without "
        "re-hitting the Meta Ad Library. Use to re-brief past market reads or build "
        "strategy prompts from saved scans."
    ),
    "generate_naming": (
        "Generate V1-compatible standard, full, compact, and reporting naming "
        "strings from creative attributes you already have (e.g. from "
        "analyze_creative), matching the API's naming structure."
    ),
}

# Per-tool allowlist of schema properties whose description survives compaction.
# A property not listed for its tool keeps its type/default/enum but loses the
# prose description at runtime -- used for boilerplate params (brand_name, dates,
# limits, obvious filters) whose meaning is already carried by the param name,
# its enum, or PLAYBOOK_INSTRUCTIONS. Load-bearing enum-prose value lists (the
# only place those values are documented) are always kept.
_SCHEMA_DESCRIPTION_FIELDS = {
    "analyze_creative": {"format"},
    "get_taxonomy": set(),
    "list_library": {"sort", "format"},
    "get_library_patterns": set(),
    "get_analysis": set(),
    "recommend": set(),
    "analyze_gaps": set(),
    "set_brand_context": set(),
    "save_naming_template": {"template"},
    "get_meta_status": set(),
    "sync_meta_performance": {"attribution_windows"},
    "get_meta_performance_summary": set(),
    "get_taxonomy_performance": set(),
    "get_prebuilt_reports": {"report_id"},
    # watch_signal_focus / watch_trajectory_focus / watch_coverage_focus (and the
    # timeseries signal_focus / trajectory_focus / coverage_focus) share one fixed
    # vocabulary documented once in PLAYBOOK_INSTRUCTIONS, so their per-tool prose
    # is dropped here; the load-bearing group_by/metric value lists stay in-surface.
    "get_creative_strategy_report": {
        "report_template",
        "rows",
        "columns",
        "status_focus",
        "metrics",
        "metric_preset",
        "watch_group_by",
        "watch_metric",
        "response_format",
        "max_cells",
    },
    "get_brain_learnings": {
        "kinds",
        "conclusion_statuses",
        "watch_group_by",
        "watch_metric",
        "watch_sources",
        "audience_signal_focus",
    },
    "save_brain_learnings": {"audience_signal_focus"},
    "export_brain_learnings_context": {"audience_signal_focus"},
    "get_performance_timeseries": {
        "date_preset",
        "group_by",
        "metric",
    },
    "export_performance_timeseries_context": set(),
    "create_custom_report": {"dimensions", "layer", "metric"},
    "save_custom_report": {
        "dimensions",
        "layer",
        "metric",
        "view_type",
        "date_range",
        "group_by",
        "saved_metric_preset",
    },
    "get_creative_leaderboard": {"window"},
    "get_batch_readout": {"window"},
    "compare_periods": {"period_a_preset", "period_b_preset", "group_by"},
    "predict_creative": {"attributes", "objective_metric", "goal_direction"},
    "get_demographics_performance": set(),
    "export_demographics_context": set(),
    "generate_brand_taxonomy": set(),
    "scan_competitor": set(),
    "get_competitor_scan_history": set(),
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
                "fatigue, and gaps. Also supports audience-mode matrices when BOTH "
                "rows and columns are demographic axes (demographic_age, "
                "demographic_gender, demographic_segment, or demographic_signal — "
                "e.g. age by gender). Demographics are account-level only, with no "
                "per-ad key: pairing a creative tag axis with a demographic axis is "
                "structurally not_applicable and returns an explicit cross_contract "
                "instead of a populated grid. Includes the decision queue and report "
                "table so an LLM can brief next tests from the same report contract "
                "as the Creative Tagger UI; detailed responses also include the "
                "agent_context payload. "
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
                            "or audience-signals. demographic-read and audience-signals are "
                            "the only demographic templates — both rows and columns are "
                            "demographic axes there. There is no template, and no rows/columns "
                            "pairing, for crossing a creative tag with a demographic axis: that "
                            "pairing is structurally not_applicable, not a hidden feature to "
                            "request manually."
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
                            "visual_format. A demographic row only returns a populated "
                            "matrix when columns is also a demographic axis (e.g. "
                            "demographic_age by demographic_gender) — demographics are "
                            "account-level only, so pairing this with a creative tag "
                            "column is not_applicable, not a mixed audience read."
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
                            "deprecated alias for visual_format). Demographics are "
                            "account-level only, with no per-ad key: pairing a creative "
                            "tag row with a demographic column returns cross_contract: "
                            "not_applicable, never a populated cross — pair two "
                            "demographic axes instead (e.g. demographic_age by "
                            "demographic_gender)."
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
            name="get_creative_leaderboard",
            description=(
                "Per-creative ranked leaderboard: which creatives to scale or kill, "
                "as one ranked list. One row per creative for the window (default "
                "last 14 days), ranked by rank_by (roas, cpa, spend, ctr, or "
                "thumbstop). Each row carries the creative's thumbnail/media, spend, "
                "spend_share, core metrics with measurement states, days_running, an "
                "honestly-labeled first/second-half trend, and its rank. Honesty "
                "contract: a creative spending below min_spend (default $500, the "
                "shared materiality floor) is flagged below_min_spend, excluded from "
                "ranks, and never crowned a winner on thin spend -- yet still returned "
                "and counted, never dropped. direction=winners/losers slices the top "
                "or bottom half of the ranked rows (sub-floor rows appear only under "
                "all). When the workspace's performance evidence is not decision-safe "
                "(stale sync, no Meta connection), rankings_withheld=true and every "
                "row drops to observation_only with no rank -- the raw measured facts "
                "stay visible, the comparative ranking does not. launched_after / "
                "launched_before (YYYY-MM-DD) scope the ranked population to creatives "
                "first synced in that window; rank and spend_share are then computed "
                "within that scoped population. Use get_batch_readout for a same-"
                "period verdict against the REST of the account instead of a rank."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "window": {
                        "type": "string",
                        "default": "last_14_days",
                        "description": (
                            "Date window preset: last_7_days, last_14_days, "
                            "last_30_days, last_90_days, this_month, last_month, "
                            "all_time, or custom (with start_date/end_date)."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start when window=custom.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end when window=custom.",
                    },
                    "rank_by": {
                        "type": "string",
                        "default": "roas",
                        "enum": ["roas", "cpa", "spend", "ctr", "thumbstop"],
                        "description": "Metric to rank creatives by.",
                    },
                    "order": {
                        "type": "string",
                        "default": "desc",
                        "enum": ["asc", "desc"],
                        "description": (
                            "Display order. Rank always reflects best-to-worst "
                            "quality regardless of order."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Rows to return (clamped to 1-50).",
                    },
                    "min_spend": {
                        "type": "number",
                        "default": 500,
                        "description": (
                            "Spend materiality floor. Below it a row is "
                            "below_min_spend and unranked."
                        ),
                    },
                    "direction": {
                        "type": "string",
                        "default": "all",
                        "enum": ["winners", "losers", "all"],
                        "description": (
                            "winners=top ranked half, losers=bottom ranked half, "
                            "all=every row including sub-floor."
                        ),
                    },
                    "launched_after": {
                        "type": "string",
                        "description": (
                            "Optional YYYY-MM-DD: only creatives first synced on or "
                            "after this date."
                        ),
                    },
                    "launched_before": {
                        "type": "string",
                        "description": (
                            "Optional YYYY-MM-DD: only creatives first synced on or "
                            "before this date."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="get_batch_readout",
            description=(
                "Launch-cohort batch readout: per-creative verdicts vs the rest of "
                "the account. Given a launch window (launched_after / "
                "launched_before, YYYY-MM-DD -- at least one is required), returns "
                "every creative first synced in that window with an explicit three-"
                "way verdict against the account-wide baseline built from every OTHER "
                "creative over the same reporting window (default last 14 days): "
                "promising, underperforming, or insufficient_evidence. The baseline "
                "excludes the batch, so a brand-new cohort is judged like-for-like "
                "against a same-period baseline, never against its own numbers or the "
                "account's lifetime average. insufficient_evidence always carries a "
                "verdict_reason -- most often below_min_spend (expected for roughly "
                "half of most batches; that honesty is the feature, not a bug), or "
                "metric_not_applicable when the creative's own objective/format does "
                "not track rank_by (e.g. roas on a leads creative, thumbstop on a "
                "static image), in which case that creative is also excluded from the "
                "baseline itself, never folded in as a real zero. Verdict_counts "
                "totals the three buckets. rank_by is roas, cpa, ctr, or thumbstop "
                "(spend has no baseline-comparison direction). Verdicts are withheld "
                "when workspace evidence is not decision-safe. Each row also carries "
                "thumbnail/media, spend_share, measurement states, and a first/second-"
                "half trend."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "window": {
                        "type": "string",
                        "default": "last_14_days",
                        "description": (
                            "Reporting window for metrics and baseline: last_7_days, "
                            "last_14_days, last_30_days, last_90_days, this_month, "
                            "last_month, all_time, or custom (with start_date/"
                            "end_date). Distinct from the launch window below."
                        ),
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD start when window=custom.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Optional YYYY-MM-DD end when window=custom.",
                    },
                    "launched_after": {
                        "type": "string",
                        "description": (
                            "YYYY-MM-DD lower bound of the launch cohort (first-synced "
                            "date). At least one launch bound is required."
                        ),
                    },
                    "launched_before": {
                        "type": "string",
                        "description": (
                            "YYYY-MM-DD upper bound of the launch cohort (first-synced "
                            "date). At least one launch bound is required."
                        ),
                    },
                    "rank_by": {
                        "type": "string",
                        "default": "roas",
                        "enum": ["roas", "cpa", "ctr", "thumbstop"],
                        "description": "Metric for the verdict and the baseline.",
                    },
                    "order": {
                        "type": "string",
                        "default": "desc",
                        "enum": ["asc", "desc"],
                        "description": "Display order of the returned rows.",
                    },
                    "min_spend": {
                        "type": "number",
                        "default": 500,
                        "description": (
                            "Spend materiality floor. A creative below it verdicts "
                            "insufficient_evidence / below_min_spend."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Rows to return (clamped to 1-50).",
                    },
                },
            },
        ),
        Tool(
            name="compare_periods",
            description=(
                "Period-over-period comparison: is it the ads, the auction, or the "
                "site? Compares period_a (the baseline, 'before') to period_b (the "
                "comparison window, 'after', e.g. this week); every delta is period_b "
                "minus period_a. Give each period EITHER a preset (this_week, "
                "last_week, last_7_days, last_14_days, last_30_days, last_90_days, "
                "this_month, last_month) OR an explicit period_X_start / period_X_end "
                "(YYYY-MM-DD) pair -- never both, never neither, and the two windows "
                "must not overlap. Returns account-level deltas (spend, revenue, "
                "roas, cpa, cpm, ctr, thumbstop_rate, click_to_purchase_rate, aov) "
                "with measurement states honored: a metric that could not be honestly "
                "measured in either period gets no fabricated delta. The core payload "
                "is a multiplicative funnel decomposition that NAMES why a roas/cpa "
                "change happened -- account.decomposition.dominant_factor is one of "
                "auction (cpm), creative_engagement (ctr), landing_conversion (cvr), "
                "order_value (aov), or mixed -- so the move is routed to the auction, "
                "the ads, the landing page, or basket size instead of being left "
                "implied by a chart. When revenue reported a measured $0 in a period, "
                "the decomposition falls back to cpa and returns a non-null "
                "revenue_caution string: trust it when present -- it means the revenue "
                "collapse itself is the more likely primary explanation, not the "
                "delivery/traffic reading, so check the revenue delta directly before "
                "acting on the routing. Optional group_by (creative or a taxonomy "
                "dimension such as hook_type or product) adds per-value deltas and "
                "biggest_movers up/down lists. Comparative verdicts are withheld "
                "(outcome_verdicts_withheld) when evidence is not decision-safe."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "brand_name": {"type": "string"},
                    "period_a_preset": {
                        "type": "string",
                        "description": (
                            "Baseline window preset (this_week, last_week, "
                            "last_7_days, last_14_days, last_30_days, last_90_days, "
                            "this_month, last_month). Use this OR period_a_start/"
                            "period_a_end, not both."
                        ),
                    },
                    "period_a_start": {
                        "type": "string",
                        "description": "Baseline explicit start (YYYY-MM-DD).",
                    },
                    "period_a_end": {
                        "type": "string",
                        "description": "Baseline explicit end (YYYY-MM-DD).",
                    },
                    "period_b_preset": {
                        "type": "string",
                        "description": (
                            "Comparison ('after') window preset, same vocabulary as "
                            "period_a_preset. Use this OR period_b_start/period_b_end, "
                            "not both."
                        ),
                    },
                    "period_b_start": {
                        "type": "string",
                        "description": "Comparison explicit start (YYYY-MM-DD).",
                    },
                    "period_b_end": {
                        "type": "string",
                        "description": "Comparison explicit end (YYYY-MM-DD).",
                    },
                    "group_by": {
                        "type": "string",
                        "default": "none",
                        "description": (
                            "none (account only), creative (per ad_name), or a "
                            "taxonomy dimension (e.g. hook_type, messaging_angle, "
                            "product) for per-value deltas and biggest_movers."
                        ),
                    },
                    "layer": {
                        "type": "string",
                        "default": "standard",
                        "enum": ["standard", "brand", "all"],
                        "description": "Taxonomy layer for a taxonomy group_by.",
                    },
                    "metric": {
                        "type": "string",
                        "default": "roas",
                        "enum": [
                            "spend",
                            "revenue",
                            "roas",
                            "cpa",
                            "cpm",
                            "ctr",
                            "thumbstop_rate",
                            "click_to_purchase_rate",
                            "aov",
                        ],
                        "description": "Primary metric for the verdict and movers.",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional subset of the delta metrics to compute; "
                            "defaults to all nine. The primary metric is always "
                            "included."
                        ),
                    },
                    "spend_threshold": {
                        "type": "number",
                        "default": 500,
                        "description": "Spend floor a group must clear to be a mover.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 25,
                        "description": (
                            "Biggest_movers per direction when grouped (clamped to "
                            "1-25)."
                        ),
                    },
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
                "demographic strategy queries plus time-series follow-up queries "
                "without opening the dashboard. Demographics are account-level only, "
                "with no per-ad key — every follow-up query stays a separate "
                "demographic-only or tag-only read, never a joined tag x demographic "
                "cross, which the API refuses as not_applicable."
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
            "get_creative_leaderboard": _get_creative_leaderboard,
            "get_batch_readout": _get_batch_readout,
            "compare_periods": _compare_periods,
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
        return _text(_with_freshness_stamp(resp.json()))


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
        return _text(_with_freshness_stamp(resp.json()))


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


async def _get_creative_leaderboard(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=20,
            minimum=1,
            maximum=CREATIVE_LEADERBOARD_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "window": args.get("window", "last_14_days"),
        "rank_by": args.get("rank_by", "roas"),
        "order": args.get("order", "desc"),
        "direction": args.get("direction", "all"),
        "min_spend": args.get("min_spend", 500),
        "limit": limit,
    }
    for key in ("start_date", "end_date", "launched_after", "launched_before"):
        if args.get(key):
            params[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/reports/creatives/leaderboard",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_batch_readout(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=20,
            minimum=1,
            maximum=CREATIVE_BATCH_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "window": args.get("window", "last_14_days"),
        "rank_by": args.get("rank_by", "roas"),
        "order": args.get("order", "desc"),
        "min_spend": args.get("min_spend", 500),
        "limit": limit,
    }
    for key in ("start_date", "end_date", "launched_after", "launched_before"):
        if args.get(key):
            params[key] = args[key]
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/reports/creatives/batch",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _compare_periods(args: dict) -> list[TextContent]:
    try:
        limit = _clamped_int_arg(
            args.get("limit"),
            default=5,
            minimum=1,
            maximum=PERIOD_COMPARE_LIMIT,
            field_name="limit",
        )
    except ValueError as exc:
        return _err(str(exc))
    params: dict[str, Any] = {
        "brand_name": args.get("brand_name", ""),
        "group_by": args.get("group_by", "none"),
        "layer": args.get("layer", "standard"),
        "metric": args.get("metric", "roas"),
        "spend_threshold": args.get("spend_threshold", 500),
        "limit": limit,
    }
    for key in (
        "period_a_preset",
        "period_a_start",
        "period_a_end",
        "period_b_preset",
        "period_b_start",
        "period_b_end",
    ):
        if args.get(key):
            params[key] = args[key]
    metrics = _csv_arg(args.get("metrics"))
    if metrics:
        params["metrics"] = metrics
    async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as client:
        resp = await client.get(
            f"{API_URL}/reports/compare",
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


# ---------- Prompts ----------
#
# MCP prompts are report *recipes*: each one tells the calling LLM exactly
# which of this server's tools to call, in what order, with what arguments,
# and how to write up the result. Argument values arrive as strings (the MCP
# prompts wire format is dict[str, str] | None) — the _prompt_* helpers below
# parse and validate them the same way tool arguments are validated elsewhere
# in this file, then every template embeds already-resolved values (dates,
# translated date_preset vocabulary, spend floors) so the calling LLM is
# handed exact tool calls to make, never left to compute or guess one itself.

_PROMPT_REPORT_CONTRACT = """\
MEASUREMENT STATES. Every figure in your report is exactly one of three states — say which:
- measured: a real number from synced data at or above the tool's own spend/sample floor.
- not_applicable: the metric does not exist for this system or this read (e.g. blended
  Shopify/MER when only Meta-attributed revenue is wired here — say "not_applicable", never
  "$0" or a guess).
- not_reported: the metric could exist but is not available right now (not connected, not
  synced, below the spend/sample floor, too new to judge, or the tool returned
  insufficient_data / an "unproven" label). Never invent a number for a not_reported value.
  "Unknowable" and "not_reported" are the same state — always say "not_reported".

UNATTRIBUTED BUCKET. If a report groups by product (or any dimension) and an "Unattributed"
bucket appears, it is coverage evidence only — the share of spend/creatives that could not be
resolved to a specific value. Report its share as a gap. Never rank it, and never call it a
winner, loser, or top performer.

SPEND MATERIALITY. Never call a taxonomy value, hook, ad, or matrix cell a winner or loser below
the tool's own spend/sample floor (spend_threshold, minimum_spend, or equivalent — these tools
default to $500 unless the prompt sets one explicitly). A row the tool already labeled unproven,
insufficient_data, or insufficient_points stays labeled that way in your report; do not upgrade
it to a confident verdict of your own.

INSUFFICIENT EVIDENCE. When a tool response carries insufficient_data / insufficient_detail, or
everything in scope is unproven, your first line IS that verdict: state plainly there is not
enough evidence to call it, and name what would need to sync (more spend, more days, a
reconnected account) before it could be judged.

FRESHNESS. When a tool response includes a freshness envelope (last_synced_at, data_age_hours,
stale) or its own top-level last_synced_at/stale fields, open with it if stale is true — a stale
sync can invalidate every verdict built on the same call, so say so before anything else.

WINDOWS. Every "today" and default date window in these tool calls is anchored to UTC
(datetime.now(timezone.utc)), not the operator's local timezone — near a UTC day boundary,
"today" here can be a different calendar day than the operator's.

OUTPUT FORMAT. Verdict first, receipts after. The first three lines must give a busy operator
the answer with no scrolling: the state, the direction/magnitude, and the one thing to do next.
Then back it with the specific numbers, each labeled measured/not_applicable/not_reported, and
any freshness or coverage caveats. Tight markdown. No preamble, no hedging beyond the three
measurement states above, no hype.\
"""


def _prompt_str(args: dict[str, str], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _prompt_required_str(args: dict[str, str], name: str) -> str:
    value = _prompt_str(args, name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _prompt_enum(
    args: dict[str, str], name: str, allowed: tuple[str, ...], *, default: str
) -> str:
    value = _prompt_str(args, name) or default
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def _prompt_float(
    args: dict[str, str], name: str, *, default: float | None = None
) -> float | None:
    raw = _prompt_str(args, name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number; got {raw!r}") from None


def _prompt_required_float(args: dict[str, str], name: str) -> float:
    raw = _prompt_str(args, name)
    if not raw:
        raise ValueError(f"{name} is required")
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number; got {raw!r}") from None


def _prompt_int(
    args: dict[str, str], name: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw = _prompt_str(args, name)
    if not raw:
        parsed = default
    else:
        try:
            parsed = int(float(raw))
        except ValueError:
            raise ValueError(f"{name} must be an integer; got {raw!r}") from None
    return max(minimum, min(parsed, maximum))


def _prompt_bool(args: dict[str, str], name: str, *, default: bool) -> bool:
    return _coerce_bool(args.get(name), default=default)


def _prompt_date(args: dict[str, str], name: str, *, default: str = "") -> str:
    raw = _prompt_str(args, name) or default
    if not raw:
        return raw
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD; got {raw!r}") from None
    return raw


def _prompt_required_date(args: dict[str, str], name: str) -> str:
    raw = _prompt_str(args, name)
    if not raw:
        raise ValueError(f"{name} is required (YYYY-MM-DD)")
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD; got {raw!r}") from None
    return raw


def _prompt_require_date_order(
    start_date: str, end_date: str, *, start_name: str, end_name: str
) -> None:
    """Reject a reversed date pair. YYYY-MM-DD strings sort lexically the same
    as chronologically, so a plain string compare is exact. Mirrors the API's
    own resolve_reporting_date_window message wording."""
    if start_date and end_date and start_date > end_date:
        raise ValueError(f"{start_name} must be on or before {end_name}")


def _fmt_num(value: float | int | None) -> str:
    if value is None:
        return ""
    return f"{value:g}"


_WINDOW_DAYS_BY_PRESET = {
    "last_7_days": 7,
    "last_7d": 7,
    "last_30_days": 30,
    "last_30d": 30,
    "last_90_days": 90,
    "last_90d": 90,
}


def _approx_window_for_preset(preset: str) -> tuple[str, str]:
    """Translate a last_N_days-style preset into concrete YYYY-MM-DD bounds.

    Used only for tools with no native date_preset parameter (get_prebuilt_reports
    takes start_date/end_date but not date_preset). An approximate inclusive
    N-calendar-day window ending today in UTC — not a guarantee of another
    tool's own preset windowing semantics, so every template that uses this
    labels the result an approximate window for the operator.
    """
    days = _WINDOW_DAYS_BY_PRESET.get(preset)
    if days is None:
        return "", ""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    return start.isoformat(), today.isoformat()


_TIMESERIES_PRESET_BY_SUMMARY_PRESET = {
    "last_7_days": "last_7d",
    "last_30_days": "last_30d",
    "last_90_days": "last_90d",
    "all_time": "all_time",
    "custom": "custom",
}


def _as_timeseries_preset(preset: str) -> str:
    """get_performance_timeseries/export_performance_timeseries_context use
    last_7d/last_30d/last_90d/maximum, not the last_7_days/last_30_days/
    last_90_days vocabulary get_meta_performance_summary, get_taxonomy_performance,
    get_creative_strategy_report, get_brain_learnings, and get_demographics_performance
    all share. Translate once, here, so every template hands the calling LLM an
    already-correct value per tool rather than asking it to convert one itself.
    """
    return _TIMESERIES_PRESET_BY_SUMMARY_PRESET.get(preset, preset)


_SUMMARY_PRESET_BY_TIMESERIES_PRESET = {
    "last_7d": "last_7_days",
    "last_30d": "last_30_days",
    "last_90d": "last_90_days",
    "all_time": "all_time",
    "custom": "custom",
    "maximum": "all_time",
}


def _as_summary_preset(preset: str) -> str:
    """Inverse of _as_timeseries_preset, for prompts whose own date_preset
    argument is expressed in get_performance_timeseries's vocabulary."""
    return _SUMMARY_PRESET_BY_TIMESERIES_PRESET.get(preset, preset)


# Deliberately excludes "custom": every prompt below that validates against
# this set exposes date_preset as a plain string with no accompanying
# start_date/end_date arguments. "custom" with no dates is not just
# undocumented here, it is unsafe across these tools' own APIs: some (e.g.
# get_taxonomy_performance, get_demographics_performance) silently resolve a
# dateless "custom" to all-time history, while get_creative_strategy_report's
# own API rejects a dateless "custom" with an HTTP 400 — so the same prompt
# argument would either lie about its window or hard-fail depending which
# tool call hit it. Prompts that need an explicit window take real
# start_date/end_date arguments instead (batch_readout, client_review_pack).
_SUMMARY_DATE_PRESETS = ("all_time", "last_7_days", "last_30_days", "last_90_days")


def _prompt_result(description: str, text: str) -> GetPromptResult:
    return GetPromptResult(
        description=description,
        messages=[
            PromptMessage(role="user", content=TextContent(type="text", text=text))
        ],
    )


# ---------- weekly_creative_report ----------

_WEEKLY_REPORT_DATE_PRESETS = ("last_7_days", "last_30_days")


def _prompt_weekly_creative_report(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    date_preset = _prompt_enum(
        args, "date_preset", _WEEKLY_REPORT_DATE_PRESETS, default="last_7_days"
    )
    target_roas = _prompt_float(args, "target_roas")
    target_cpa = _prompt_float(args, "target_cpa")
    spend_threshold = _prompt_float(args, "spend_threshold", default=500.0)
    timeseries_preset = _as_timeseries_preset(date_preset)
    approx_start, approx_end = _approx_window_for_preset(date_preset)
    prior_start, prior_end = _prior_window(approx_start, approx_end)
    compare_metric = "cpa" if (target_cpa is not None and target_roas is None) else "roas"

    if target_roas is not None:
        target_line = f"Target: ROAS >= {_fmt_num(target_roas)}."
    elif target_cpa is not None:
        target_line = f"Target: CPA <= {_fmt_num(target_cpa)}."
    else:
        target_line = (
            "No target supplied — report relative winners/losers only (no "
            "above/below-target framing); target_roas or target_cpa unlocks it."
        )

    text = f"""\
Build the Monday creative report for "{brand_name}", window {date_preset}. {target_line}

Call these tools in order and use their real outputs — never fill in a number you did not get
back from one of them:

1. `get_meta_status(brand_name="{brand_name}")` — check connected/stale first. If stale is
   true, that is the first line of your report: every number below is only as fresh as this
   sync, name the freshness.data_age_hours.
2. `get_meta_performance_summary(brand_name="{brand_name}", date_preset="{date_preset}")` —
   account totals (spend, revenue, ROAS) and winners/losers by standard and brand taxonomy.
3. `compare_periods(brand_name="{brand_name}", period_b_start="{approx_start}", period_b_end="{approx_end}", period_a_start="{prior_start}", period_a_end="{prior_end}", metric="{compare_metric}")` —
   this window (period_b) vs the prior comparable window (period_a) in one call: the
   period-over-period deltas plus account.decomposition.dominant_factor naming WHY {compare_metric}
   moved (auction/creative_engagement/landing_conversion/order_value). Use this for the "up or down
   vs last week, and why" line instead of a second summary call. Trust revenue_caution over the
   delivery reading when it is present.
4. `get_prebuilt_reports(brand_name="{brand_name}", spend_threshold={_fmt_num(spend_threshold)}, start_date="{approx_start}", end_date="{approx_end}")` —
   best hooks/angles/formats/landing pages/offers/CTAs. This tool has no date_preset
   parameter, so the start_date/end_date above are an approximate {date_preset} window, not
   an exact match to the other calls' windowing — say so if the two disagree materially.
5. `get_performance_timeseries(brand_name="{brand_name}", date_preset="{timeseries_preset}", group_by="ad_name", metric="roas", limit=5, minimum_spend={_fmt_num(spend_threshold)})` —
   fatigue/trajectory/coverage for the top 5 spenders. Note: get_performance_timeseries uses
   last_7d/last_30d, not last_7_days/last_30_days — {timeseries_preset} is the correct value
   for this call, already translated from {date_preset}.
6. `get_brain_learnings(brand_name="{brand_name}", date_preset="{date_preset}", kinds="conclusion,watch", limit=8)` —
   recent test conclusions (winner/fatigued/loser) and fatigue watch stories for the
   this-week narrative.

Write the report as: (a) one verdict line — beat/missed target or "no target set", fresh or
stale; (b) up or down vs the prior window with the dominant_factor from step 3 as the reason;
(c) tag-level winners and losers actually above {_fmt_num(spend_threshold)} spend, each labeled
measured/not_reported/not_applicable; (d) the fatigue watchlist from step 5, ranked
worst-trajectory first; (e) a decision queue of 3-5 concrete next actions in the persona's own
vocabulary (refresh X, brief a test on Y, hold Z). Client-ready markdown; every verdict carries
its evidence and measurement-state inline, not in a footnote.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Build this week's creative report for a brand: totals with data freshness, which "
        "hooks/angles/formats won and lost on real spend, which top spenders are fatiguing, "
        "and what to do next — with sparse or stale evidence called out explicitly.",
        text,
    )


# ---------- fatigue_check ----------

_FATIGUE_CHECK_METRICS = ("roas", "cpa", "ctr", "thumbstop_rate")
_FATIGUE_CHECK_DATE_PRESETS = ("last_30d", "last_90d")


def _prompt_fatigue_check(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    top_n = _prompt_int(args, "top_n", default=5, minimum=1, maximum=10)
    metric = _prompt_enum(args, "metric", _FATIGUE_CHECK_METRICS, default="roas")
    date_preset = _prompt_enum(
        args, "date_preset", _FATIGUE_CHECK_DATE_PRESETS, default="last_30d"
    )
    summary_preset = _as_summary_preset(date_preset)

    text = f"""\
Check the top {top_n} spenders on "{brand_name}" for creative fatigue before it shows up in
{metric}, window {date_preset}.

Call these tools in order:

1. `get_meta_status(brand_name="{brand_name}")` — connection + freshness check. If stale is
   true, lead with it: a stale sync makes every trajectory read below unreliable.
2. `get_performance_timeseries(brand_name="{brand_name}", date_preset="{date_preset}", group_by="ad_name", metric="{metric}", limit={top_n}, signal_focus="all", trajectory_focus="all")` —
   per-ad series for the top {top_n} spenders: fatigue signal, trajectory (worsening/improving/
   flat/insufficient_data), and coverage class (call_ready/gappy/short_window/
   insufficient_points/windowed_history). Coverage class is your trust label for each read —
   report it next to every ad, not just the signal.
3. `get_creative_strategy_report(brand_name="{brand_name}", date_preset="{summary_preset}", report_template="fatigue-watch", watch_metric="{metric}")` —
   cross-check: does the same fatigue signal show up at the taxonomy-cell level (e.g. this
   hook/angle broadly wearing out), or is it isolated to individual ads? Note:
   get_creative_strategy_report uses {summary_preset} (last_N_days), not
   get_performance_timeseries's {date_preset} (last_Nd) — already translated above.
4. `export_performance_timeseries_context(brand_name="{brand_name}", date_preset="{date_preset}", group_by="ad_name", metric="{metric}", limit={top_n})` —
   pull the agent_context decision queue (refresh/watch/validate/hold) built from the same
   fatigue logic, so your action list matches the dashboard's own read.

Rank the {top_n} ads worst-trajectory-first. For each: fatigue signal, trajectory, coverage
class (state plainly when a read is insufficient_data or short_window — that ad is
not_reported for fatigue, not "stable"), and the one action (refresh brief, taper spend, hold).
Ads with a call_ready coverage class and a fatigued+worsening read need a refresh briefed now —
say so explicitly in your first three lines if any exist.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Check the account's top-spending ads for creative fatigue before it shows up in "
        "ROAS: decay signals, trajectory, and how trustworthy each read is, plus which "
        "winners need a refresh briefed now.",
        text,
    )


# ---------- scale_kill_hold ----------

_SCALE_KILL_HOLD_OBJECTIVE_METRICS = ("roas", "cpa")


def _prompt_scale_kill_hold(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    objective_metric = _prompt_enum(
        args, "objective_metric", _SCALE_KILL_HOLD_OBJECTIVE_METRICS, default="roas"
    )
    if not _prompt_str(args, "objective_metric"):
        raise ValueError("objective_metric is required (roas or cpa)")
    target_value = _prompt_required_float(args, "target_value")
    minimum_spend = _prompt_float(args, "minimum_spend", default=500.0)
    date_preset = _prompt_enum(
        args, "date_preset", _SUMMARY_DATE_PRESETS, default="last_30_days"
    )
    better_direction = "higher is better" if objective_metric == "roas" else "lower is better"

    text = f"""\
Produce today's scale/kill/hold triage for "{brand_name}" against objective_metric="{objective_metric}"
(target_value={_fmt_num(target_value)}, {better_direction}), window {date_preset},
minimum_spend={_fmt_num(minimum_spend)}.

Call these tools in order:

1. `get_meta_status(brand_name="{brand_name}")` — freshness check. A stale sync means today's
   triage is provisional; say so first if stale is true.
2. `get_creative_leaderboard(brand_name="{brand_name}", window="{date_preset}", rank_by="{objective_metric}", min_spend={_fmt_num(minimum_spend)}, limit=50)` —
   the ranked per-creative triage list in one call. Each row's rank_value is that creative's real
   aggregate {objective_metric} for the window — compare it directly to target_value — and each
   row carries a first/second-half trend and a below_min_spend flag. Rows flagged below_min_spend
   are unranked: they go straight to the insufficient-evidence bucket, never scaled or killed on
   thin spend. If rankings_withheld is true, the account's evidence is not decision-safe — say so
   and treat every call below as provisional.
3. `get_creative_strategy_report(brand_name="{brand_name}", date_preset="{date_preset}", report_template="next-tests", minimum_spend={_fmt_num(minimum_spend)}, {objective_metric}_target={_fmt_num(target_value)})` —
   the taxonomy-cell view of the same account: does a whole hook/angle/format cell justify the
   same call the individual creative triage made? Use it to corroborate, never as a substitute
   for the per-creative list above.
4. `get_taxonomy_performance(brand_name="{brand_name}", date_preset="{date_preset}", spend_threshold={_fmt_num(minimum_spend)})` —
   coverage_gaps and unproven tags, so a creative riding a broadly-unproven taxonomy cell is
   flagged as thinner evidence even if its own number looks decisive.

Bucket every creative with spend >= {_fmt_num(minimum_spend)} into exactly one of:
- scale: {objective_metric} clears target_value with an improving or flat trend.
- kill: {objective_metric} misses target_value decisively, or misses it with a declining trend.
- hold: {objective_metric} is close to target_value with no clear trend either way — real spend,
  ambiguous read.
Anything flagged below_min_spend, insufficient_data, or rankings_withheld by a tool goes in a
separate insufficient-evidence bucket — never forced into scale/kill/hold. Lead your report with
the counts in each bucket, then the one or two numbers justifying each individual call.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Produce today's scale/kill/hold verdict list against your declared target, with "
        "evidence attached to every call and an explicit 'not enough data to judge' bucket "
        "instead of false confidence.",
        text,
    )


# ---------- what_to_make_next_brief ----------


def _prompt_what_to_make_next_brief(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    production_slots = _prompt_int(
        args, "production_slots", default=5, minimum=1, maximum=50
    )
    formats_available = _prompt_str(args, "formats_available")
    date_preset = _prompt_enum(
        args, "date_preset", _SUMMARY_DATE_PRESETS, default="last_30_days"
    )
    formats_clause = (
        f" Available formats this sprint: {formats_available}."
        if formats_available
        else " No format constraint given — brief across whatever formats the evidence supports."
    )

    text = f"""\
Build the next-sprint creative brief for "{brand_name}": {production_slots} production
slot(s), window {date_preset}.{formats_clause}

Call these tools in order:

1. `get_taxonomy_performance(brand_name="{brand_name}", date_preset="{date_preset}")` —
   tag-level winners (proven, spend-gated) and coverage_gaps (standard taxonomy values never
   tried at all).
2. `get_creative_strategy_report(brand_name="{brand_name}", date_preset="{date_preset}", report_template="coverage-gaps")` —
   the matrix view of untested hook x angle (or configured rows/columns) cells, so whitespace
   bets are adjacent to proven cells, not random.
3. `get_library_patterns(brand_name="{brand_name}")` — concentration risk: which hooks, angles,
   creative types the library over- or under-indexes on across its whole history (this tool has
   no date_preset — it reads the full library).
4. `analyze_gaps(brand_name="{brand_name}")` — ready-to-produce briefs the API already drafted
   from the same gap analysis; use these as a starting set, not a replacement for your own
   evidence-grounded prioritization.
5. `recommend(brand_name="{brand_name}", question="Given {production_slots} production slots{(' for ' + formats_available) if formats_available else ''}, what should we iterate on proven winners vs bet on as net-new whitespace this sprint?")` —
   open-ended strategist synthesis grounded in this brand's library + saved brand context.

Produce exactly {production_slots} per-slot briefs, each in taxonomy vocabulary (hook_type,
messaging_angle, visual_format, etc. — not freeform description), ranked proven-iteration
first, whitespace-bet second. Label every brief one of:
- iterate: measured — grounded in a specific spend-gated winner from step 1 or 2 (name the
  metric and the spend it cleared).
- whitespace bet: not_reported — an untested-but-adjacent cell from steps 1/2/4; say why it is
  adjacent to a proven cell, not just untested.
If get_library_patterns shows a concentration risk (e.g. one hook/angle far over-indexed), open
with it — that risk should shape which slots go to diversification vs doubling down.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Turn performance and coverage gaps into next sprint's brief: what to iterate, what "
        "net-new cells to test, and what to stop making — each recommendation grounded in "
        "spend-weighted evidence or flagged as a whitespace bet.",
        text,
    )


# ---------- hook_report ----------


def _prompt_hook_report(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    date_preset = _prompt_enum(
        args, "date_preset", _SUMMARY_DATE_PRESETS, default="last_30_days"
    )
    spend_threshold = _prompt_float(args, "spend_threshold", default=500.0)
    timeseries_preset = _as_timeseries_preset(date_preset)
    approx_start, approx_end = _approx_window_for_preset(date_preset)

    text = f"""\
Diagnose hook performance for "{brand_name}", window {date_preset}, spend_threshold={_fmt_num(spend_threshold)}.

Call these tools in order:

1. `get_prebuilt_reports(brand_name="{brand_name}", report_id="best_hooks", spend_threshold={_fmt_num(spend_threshold)}, start_date="{approx_start}", end_date="{approx_end}")` —
   thumbstop AND downstream CPA/ROAS per hook type in one ranked view. This tool has no
   date_preset parameter, so the start_date/end_date above approximate {date_preset}.
2. `get_taxonomy_performance(brand_name="{brand_name}", dimension="hook_type", spend_threshold={_fmt_num(spend_threshold)}, date_preset="{date_preset}")` —
   the same hook_type dimension with its coverage_gaps: hook types never tried at all, and
   which tried ones are still "unproven" below the spend floor.
3. `get_performance_timeseries(brand_name="{brand_name}", date_preset="{timeseries_preset}", group_by="hook_type", metric="thumbstop_rate", minimum_spend={_fmt_num(spend_threshold)})` —
   is thumbstop for each hook type decaying over time (the hook itself wearing out across the
   account), independent of any one creative? Note the translated {timeseries_preset} value.
4. `get_creative_strategy_report(brand_name="{brand_name}", date_preset="{date_preset}", report_template="hook-performance", minimum_spend={_fmt_num(spend_threshold)})` —
   the named hook x messaging_angle matrix with hook/hold/thumbstop/CPA metrics, so a hook's
   read can be qualified by which angle it was paired with.
5. `get_demographics_performance(brand_name="{brand_name}", date_preset="{date_preset}")` —
   account-level age x gender efficiency bands for the SAME window, as a SEPARATE read next
   to the hook numbers above. Demographics are account-level only (creative_demographics has
   no per-ad key), so this MCP surface cannot cross a hook with a demographic segment — the
   API returns cross_contract: not_applicable if that pairing is attempted. Never state or
   imply "this hook wins with this audience."

For every hook type with spend >= {_fmt_num(spend_threshold)}: report thumbstop_rate
AND the downstream metric (CPA or ROAS) side by side — flag any hook with high thumbstop but
poor downstream as a "stops the scroll, doesn't convert" trap, not a winner. Flag any hook with
a worsening thumbstop trend from step 3 as wearing out, even if its all-time average still looks
fine. Report step 5's account-level demographic bands in their own section, never attributed to
a specific hook. Close with which hooks are proven enough to cut onto a different (winning) body
next, and which hook types in coverage_gaps are untested whitespace.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Which hooks are earning the view AND the purchase, which are getting skipped, and "
        "which are wearing out — with minimum-spend gating so a $50 fluke never reads as a "
        "trend.",
        text,
    )


# ---------- batch_readout ----------


def _prompt_batch_readout(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    batch_start_date = _prompt_required_date(args, "batch_start_date")
    today = datetime.now(timezone.utc).date().isoformat()
    batch_end_date = _prompt_date(args, "batch_end_date", default=today)
    _prompt_require_date_order(
        batch_start_date,
        batch_end_date,
        start_name="batch_start_date",
        end_name="batch_end_date",
    )
    baseline_preset = _prompt_str(args, "baseline_preset") or "last_90_days"

    text = f"""\
Grade the creative batch launched {batch_start_date} to {batch_end_date} for "{brand_name}",
against the rest of the account over the {baseline_preset} window.

Call these tools in order:

1. `get_batch_readout(brand_name="{brand_name}", launched_after="{batch_start_date}", launched_before="{batch_end_date}", window="{baseline_preset}", rank_by="roas")` —
   the whole launch-cohort verdict in one call: every creative first synced in the batch window
   gets a three-way verdict (promising / underperforming / insufficient_evidence) against a
   same-window baseline built from every OTHER creative, so "won" means beat the account's own
   bar, not an arbitrary number. verdict_counts totals the three buckets. Read verdict_reason on
   each insufficient_evidence row: below_min_spend is the honest "too early to judge" case
   (expected for roughly half of most batches), and metric_not_applicable means roas is not a
   real signal for that creative's objective (e.g. a leads creative). If rankings_withheld is
   true the account's evidence is not decision-safe — say so before ranking anything.
2. `create_custom_report(brand_name="{brand_name}", dimensions=["hook_type", "messaging_angle"], metric="roas", start_date="{batch_start_date}", end_date="{batch_end_date}")` —
   which taxonomy attribute combinations actually occurring in the batch window correlate with
   wins, so you can name what the promising creatives share. Rows include parts/values to name
   the winning combination precisely.

Report three groups straight from step 1's verdicts: (a) promising creatives — name the taxonomy
attributes step 2 shows they share; (b) underperforming creatives; (c) an explicit
too-early-to-judge bucket for every insufficient_evidence creative (list these by name with their
verdict_reason — do not fold them into winners or losers). Lead with the three counts from
verdict_counts, then the shared attributes behind the promising group.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Grade a creative batch: what won, what lost, what taxonomy attributes the winners "
        "share, and — honestly — which creatives never got enough spend to call either way.",
        text,
    )


# ---------- monday_money_check ----------

_MONDAY_MONEY_CHECK_DATE_PRESETS = ("last_7_days", "last_30_days")


def _prior_window(start_date: str, end_date: str) -> tuple[str, str]:
    """The immediately-preceding window of the same length as [start, end].

    Supplies the period_a (baseline) bounds for a compare_periods call so the
    Monday check reads this window against its own prior comparable window.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    length = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=length - 1)
    return prior_start.isoformat(), prior_end.isoformat()


def _prompt_monday_money_check(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    breakeven_roas = _prompt_float(args, "breakeven_roas")
    target_cpa = _prompt_float(args, "target_cpa")
    if breakeven_roas is None and target_cpa is None:
        raise ValueError("breakeven_roas is required unless target_cpa is given")
    date_preset = _prompt_enum(
        args, "date_preset", _MONDAY_MONEY_CHECK_DATE_PRESETS, default="last_7_days"
    )
    timeseries_preset = _as_timeseries_preset(date_preset)
    this_start, this_end = _approx_window_for_preset(date_preset)
    prior_start, prior_end = _prior_window(this_start, this_end)
    objective_metric = "cpa" if breakeven_roas is None else "roas"
    target_line = (
        f"breakeven_roas={_fmt_num(breakeven_roas)}"
        if breakeven_roas is not None
        else f"target_cpa={_fmt_num(target_cpa)}"
    )

    text = f"""\
Monday money check for "{brand_name}": {target_line}, window {date_preset}
({this_start} to {this_end}) vs the prior comparable window ({prior_start} to {prior_end}).

Call these tools in order:

1. `get_meta_status(brand_name="{brand_name}")` — connection + freshness first. If stale is
   true, that IS the verdict: say the numbers below are stale before anything else.
2. `compare_periods(brand_name="{brand_name}", period_b_start="{this_start}", period_b_end="{this_end}", period_a_start="{prior_start}", period_a_end="{prior_end}", metric="{objective_metric}")` —
   this window (period_b) vs the prior comparable window (period_a) in one call; every delta is
   period_b minus period_a. Read period_b's own {objective_metric} against {target_line}, then the
   delta for better/worse-than-prior. account.decomposition.dominant_factor names WHY
   {objective_metric} moved — auction (cpm), creative_engagement (ctr), landing_conversion (cvr),
   or order_value (aov) — so "down this week" comes with a cause, not a shrug. If revenue_caution
   is present, trust it over the delivery reading: a measured $0 revenue is the more likely story.
   If outcome_verdicts_withheld is true, the evidence is not decision-safe — say so.
3. `get_performance_timeseries(brand_name="{brand_name}", date_preset="{timeseries_preset}", group_by="ad_name", metric="{objective_metric}", limit=5)` —
   the biggest per-ad mover this window, to point at one concrete thing worth digging into
   rather than a vague "performance is down."

Report ONLY Meta-attributed figures — this system has no Shopify/blended-revenue connector, so
blended MER is not_applicable this window, not "unavailable" or a guess. Your first three lines:
(1) above or below {target_line} this window; (2) better or worse than the prior window, with the
dominant_factor as the reason; (3) the one ad/driver from step 3 worth a look, or "nothing stands
out" if nothing does. Everything else is receipts.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Bottom line for the week: above or below your breakeven, better or worse than last "
        "week, and whether anything needs your attention — one verdict, evidence attached, no "
        "dashboard to interpret. (Meta-attributed only; blended MER reported as not_applicable.)",
        text,
    )


# ---------- competitive_whitespace ----------


def _prompt_competitive_whitespace(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    competitor = _prompt_str(args, "competitor")
    country = _prompt_str(args, "country") or "US"
    run_fresh_scan = _prompt_bool(args, "run_fresh_scan", default=False)
    date_preset = _prompt_enum(
        args, "date_preset", _SUMMARY_DATE_PRESETS, default="last_90_days"
    )
    competitor_clause = f' for "{competitor}"' if competitor else " (no specific competitor named — use the most recent saved scan)"
    scan_call = (
        f'`scan_competitor(brand_name="{brand_name}", page_name="{competitor}", country="{country}")`'
        if competitor
        else f'`scan_competitor(brand_name="{brand_name}", country="{country}")`'
    )

    text = f"""\
Diff competitor creative strategy against "{brand_name}"'s own library{competitor_clause},
country={country}, run_fresh_scan={run_fresh_scan}.

Call these tools in order:

1. `get_competitor_scan_history(brand_name="{brand_name}", limit=10)` — saved scans/imports
   already on file. Look for one matching {competitor or "the target competitor"}.
2. Branch here: if run_fresh_scan is {run_fresh_scan} and a matching saved scan exists in step
   1's results, call `get_competitor_scan_detail(scan_id=<that id>)` to reuse it. Otherwise —
   run_fresh_scan is true, or nothing matching was saved — call {scan_call} (a live Meta Ad
   Library scan; can take up to 5 minutes). Either path gives you the competitor's ads,
   per-ad Creative Tagger analyses, and an aggregate strategy breakdown (dominant hook types,
   visual styles, CTAs, emotions, estimated spend).
3. `get_library_patterns(brand_name="{brand_name}")` — our own hook/angle/format concentration
   across the whole library (no date filter on this tool).
4. `get_creative_strategy_report(brand_name="{brand_name}", date_preset="{date_preset}", report_template="coverage-gaps")` —
   our own untested taxonomy cells, window {date_preset}.

Diff the competitor's dominant hooks/angles/formats/CTAs (step 2's strategy breakdown) against
our coverage_gaps (step 4) and concentration (step 3). This tool has no first-seen/last-seen
longevity signal — you cannot say how long a competitor angle has run, only that it appears in
this scan; do not claim "60+ days" or any duration not_reported. Native Meta Ad Library access
depth may also be gated — if step 2 returns thin results, say so rather than treating a small
sample as the competitor's full strategy. Close with 1-2 test briefs: competitor cells with
real presence in their scan that are also empty in our coverage_gaps, in taxonomy vocabulary.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Diff a competitor's live ad strategy against your own library on the same taxonomy: "
        "their dominant hooks/angles/formats, the cells you have zero coverage on, and which "
        "of their proven angles deserve a test brief.",
        text,
    )


# ---------- audience_read ----------

_AUDIENCE_READ_OBJECTIVE_METRICS = ("roas", "cpa", "ctr")
_AUDIENCE_READ_GOAL_DIRECTIONS = ("maximize", "minimize")
_AUDIENCE_READ_EXPECTED_DIRECTION = {"roas": "maximize", "cpa": "minimize", "ctr": "maximize"}


def _prompt_audience_read(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    objective_metric = _prompt_enum(
        args, "objective_metric", _AUDIENCE_READ_OBJECTIVE_METRICS, default="roas"
    )
    if not _prompt_str(args, "objective_metric"):
        raise ValueError("objective_metric is required (roas, cpa, or ctr)")
    goal_direction = _prompt_enum(
        args, "goal_direction", _AUDIENCE_READ_GOAL_DIRECTIONS, default="maximize"
    )
    if not _prompt_str(args, "goal_direction"):
        raise ValueError("goal_direction is required (maximize or minimize)")
    expected_direction = _AUDIENCE_READ_EXPECTED_DIRECTION[objective_metric]
    if goal_direction != expected_direction:
        raise ValueError(
            f"goal_direction must be {expected_direction!r} for objective_metric={objective_metric!r}, "
            f"got {goal_direction!r}"
        )
    date_preset = _prompt_enum(
        args, "date_preset", _SUMMARY_DATE_PRESETS, default="last_30_days"
    )
    custom_ranking_note = (
        ""
        if objective_metric == "roas"
        else (
            f' get_demographics_performance\'s own opportunity/waste signal is computed on '
            f"ROAS internally — since objective_metric is {objective_metric}, do not reuse that "
            f"signal; independently rank segments by their own segment.{objective_metric} field "
            f"({goal_direction}) and say plainly you built a custom ranking because the tool's "
            f"built-in signal is ROAS-specific."
        )
    )

    text = f"""\
Read audience efficiency for "{brand_name}" on objective_metric="{objective_metric}"
({goal_direction}), window {date_preset}.

Call these tools in order:

1. `get_demographics_performance(brand_name="{brand_name}", date_preset="{date_preset}")` —
   age x gender segments with spend, roas, cpa, ctr, and account-relative
   higher_observed_efficiency / lower_observed_efficiency bands per segment.{custom_ranking_note}
2. `export_demographics_context(brand_name="{brand_name}", date_preset="{date_preset}", limit=5)` —
   the bounded, prompt-ready review queue plus follow-up strategy/time-series queries the API
   already drafted for these segments.
3. `get_taxonomy_performance(brand_name="{brand_name}", dimension="messaging_angle", date_preset="{date_preset}")` —
   messaging-angle performance across the WHOLE account, as a SEPARATE read next to steps
   1-2 above — never a join. Demographics are account-level only (creative_demographics has
   no per-ad key), so this MCP surface cannot cross messaging_angle with demographic_segment
   — the API returns cross_contract: not_applicable if that pairing is attempted.
4. `get_taxonomy_performance(brand_name="{brand_name}", dimension="hook_type", date_preset="{date_preset}")` —
   the same account-wide, separate read for hooks.

This tool covers age x gender only — no geo or placement axis exists on this MCP surface; do
not claim a placement or geo read. Report segments in higher_observed_efficiency vs
lower_observed_efficiency bands (never "best"/"worst audience" — these are observed
associations, not causal outcomes), then report steps 3-4's messaging-angle and hook
performance in their OWN section, scoped to the whole account — never state or imply that a
specific angle or hook performs better FOR a specific segment; that cross does not exist on
this surface. Close every efficiency claim with the controlled test that would confirm it
(e.g. a holdout or geo split), and explicitly do NOT issue a budget/spend-shift instruction
from this observational read alone — that requires the validation test, not this report.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Who your spend is reaching vs who is efficiently converting, broken by age and "
        "gender, reported alongside (never crossed with) messaging-angle and hook "
        "performance — as observed associations with the controlled tests that would "
        "confirm them.",
        text,
    )


# ---------- client_review_pack ----------


def _prompt_client_review_pack(args: dict[str, str]) -> GetPromptResult:
    brand_name = _prompt_required_str(args, "brand_name")
    period_start = _prompt_required_date(args, "period_start")
    period_end = _prompt_required_date(args, "period_end")
    _prompt_require_date_order(
        period_start, period_end, start_name="period_start", end_name="period_end"
    )
    client_cpa_target = _prompt_float(args, "client_cpa_target")
    include_competitors = _prompt_bool(args, "include_competitors", default=True)
    target_clause = (
        f" against a client CPA target of {_fmt_num(client_cpa_target)}"
        if client_cpa_target is not None
        else " (no client_cpa_target supplied — report totals without an against-target verdict)"
    )
    competitor_step = (
        f'6. `get_competitor_scan_history(brand_name="{brand_name}", limit=5)` — competitor '
        "context from saved scans only (no fresh scan triggered — this report stays bounded "
        "and fast). If nothing is saved, say competitor context is not_reported, not empty.\n"
        if include_competitors
        else ""
    )
    competitor_section = (
        "competitor context, "
        if include_competitors
        else "no competitor section requested this run, "
    )

    text = f"""\
Assemble the monthly client review for "{brand_name}", period {period_start} to {period_end}{target_clause}.

Call these tools in order:

1. `get_meta_status(brand_name="{brand_name}")` — connection + freshness. This report must
   reconcile with what the client sees in Ads Manager, so lead with sync state and
   last_synced_at before any figure below.
2. `get_meta_performance_summary(brand_name="{brand_name}", start_date="{period_start}", end_date="{period_end}")` —
   period totals (spend, revenue, ROAS) plus the same freshness envelope.
3. `get_prebuilt_reports(brand_name="{brand_name}", start_date="{period_start}", end_date="{period_end}")` —
   omit report_id to get every prebuilt report (hooks, angles, formats, landing pages,
   offers, CTAs) for the winner-pattern headline.
4. `get_brain_learnings(brand_name="{brand_name}", start_date="{period_start}", end_date="{period_end}", kinds="conclusion", limit=8)` —
   the testing slide: recent winner/fatigued/loser conclusions with their evidence.
5. `get_demographics_performance(brand_name="{brand_name}", start_date="{period_start}", end_date="{period_end}")` —
   the audience-read section, age x gender only.
{competitor_step}
Structure the review as: (a) period totals with freshness/attribution notes so the client can
reconcile against Ads Manager, framed against the client CPA target when one was given; (b) the
winner-pattern headline — what the winners share, in taxonomy vocabulary; (c) the testing slide
— what was tested and what was concluded, each conclusion with its evidence and
measurement-state; (d) the audience read, framed as observed associations, never causal; (e)
{competitor_section}and (f) 1-2 approved-test proposals for next month grounded in this
period's evidence. Every figure in every section carries its freshness/measurement-state — this
is a client-facing document, not an internal note.

{_PROMPT_REPORT_CONTRACT}
"""
    return _prompt_result(
        "Assemble the monthly client review: what drove results, what the winners have in "
        "common, what we tested and learned, and what to test next — every figure stamped "
        "with sync freshness so it reconciles with what the client sees in Ads Manager.",
        text,
    )


# ---------- Prompt registry ----------

_PROMPTS: list[Prompt] = [
    Prompt(
        name="weekly_creative_report",
        title="Weekly Creative Report",
        description=(
            "Build this week's creative report for a brand: totals with data freshness, "
            "which hooks/angles/formats won and lost on real spend, which top spenders are "
            "fatiguing, and what to do next — with sparse or stale evidence called out "
            "explicitly."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="date_preset",
                description="last_7_days or last_30_days (default last_7_days)",
                required=False,
            ),
            PromptArgument(
                name="target_roas",
                description="Target ROAS to frame winners/losers against (optional)",
                required=False,
            ),
            PromptArgument(
                name="target_cpa",
                description="Target CPA, an alternative to target_roas (optional)",
                required=False,
            ),
            PromptArgument(
                name="spend_threshold",
                description="Spend floor before a tag counts as a winner/loser (default 500)",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="fatigue_check",
        title="Fatigue Check",
        description=(
            "Check the account's top-spending ads for creative fatigue before it shows up "
            "in ROAS: decay signals, trajectory, and how trustworthy each read is, plus "
            "which winners need a refresh briefed now."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="top_n",
                description="Top N spenders to check, 1-10 (default 5)",
                required=False,
            ),
            PromptArgument(
                name="metric",
                description="roas, cpa, ctr, or thumbstop_rate (default roas)",
                required=False,
            ),
            PromptArgument(
                name="date_preset",
                description="last_30d or last_90d (default last_30d)",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="scale_kill_hold",
        title="Scale / Kill / Hold",
        description=(
            "Produce today's scale/kill/hold verdict list against your declared target, "
            "with evidence attached to every call and an explicit 'not enough data to "
            "judge' bucket instead of false confidence."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="objective_metric",
                description="roas or cpa — predeclared per the measurement contract",
                required=True,
            ),
            PromptArgument(
                name="target_value",
                description="Target value for objective_metric",
                required=True,
            ),
            PromptArgument(
                name="minimum_spend",
                description="Spend floor before a call counts as evidence (default 500)",
                required=False,
            ),
            PromptArgument(
                name="date_preset",
                description=(
                    "all_time, last_7_days, last_30_days, or last_90_days "
                    "(default last_30_days)"
                ),
                required=False,
            ),
        ],
    ),
    Prompt(
        name="what_to_make_next_brief",
        title="What To Make Next",
        description=(
            "Turn performance and coverage gaps into next sprint's brief: what to iterate, "
            "what net-new cells to test, and what to stop making — each recommendation "
            "grounded in spend-weighted evidence or flagged as a whitespace bet."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="production_slots",
                description="Number of creative briefs to produce (default 5)",
                required=False,
            ),
            PromptArgument(
                name="formats_available",
                description="Constrain briefs to these formats, e.g. 'UGC video, static' (optional)",
                required=False,
            ),
            PromptArgument(
                name="date_preset",
                description=(
                    "all_time, last_7_days, last_30_days, or last_90_days "
                    "(default last_30_days)"
                ),
                required=False,
            ),
        ],
    ),
    Prompt(
        name="hook_report",
        title="Hook Report",
        description=(
            "Which hooks are earning the view AND the purchase, which are getting "
            "skipped, and which are wearing out — with minimum-spend gating so a $50 "
            "fluke never reads as a trend."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="date_preset",
                description=(
                    "all_time, last_7_days, last_30_days, or last_90_days "
                    "(default last_30_days)"
                ),
                required=False,
            ),
            PromptArgument(
                name="spend_threshold",
                description="Spend floor before a hook counts as proven (default 500)",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="batch_readout",
        title="Batch Readout",
        description=(
            "Grade a creative batch: what won, what lost, what taxonomy attributes the "
            "winners share, and — honestly — which ads never got enough spend to call "
            "either way."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="batch_start_date",
                description="Batch launch window start, YYYY-MM-DD",
                required=True,
            ),
            PromptArgument(
                name="batch_end_date",
                description="Batch launch window end, YYYY-MM-DD (default today)",
                required=False,
            ),
            PromptArgument(
                name="baseline_preset",
                description="Account baseline window to grade against (default last_90_days)",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="monday_money_check",
        title="Monday Money Check",
        description=(
            "Bottom line for the week: above or below your breakeven, better or worse "
            "than last week, and whether anything needs your attention — one verdict, "
            "evidence attached, no dashboard to interpret. (Meta-attributed only; "
            "blended MER reported as not_applicable.)"
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="breakeven_roas",
                description="Breakeven ROAS target; required unless target_cpa is given",
                required=False,
            ),
            PromptArgument(
                name="target_cpa",
                description="Target CPA, an alternative to breakeven_roas",
                required=False,
            ),
            PromptArgument(
                name="date_preset",
                description="last_7_days or last_30_days — the 'week' window (default last_7_days)",
                required=False,
            ),
        ],
    ),
    Prompt(
        name="competitive_whitespace",
        title="Competitive Whitespace",
        description=(
            "Diff a competitor's live ad strategy against your own library on the same "
            "taxonomy: their dominant hooks/angles/formats, the cells you have zero "
            "coverage on, and which of their proven angles deserve a test brief."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="competitor",
                description="Competitor page name (optional — omit to reuse the latest saved scan)",
                required=False,
            ),
            PromptArgument(
                name="country",
                description="ISO-2 country code for the scan (default US)",
                required=False,
            ),
            PromptArgument(
                name="run_fresh_scan",
                description="Force a fresh Meta Ad Library scan instead of reusing a saved one (default false; can take up to 5 minutes)",
                required=False,
            ),
            PromptArgument(
                name="date_preset",
                description=(
                    "Window for our own coverage-gaps read: all_time, last_7_days, "
                    "last_30_days, or last_90_days (default last_90_days)"
                ),
                required=False,
            ),
        ],
    ),
    Prompt(
        name="audience_read",
        title="Audience Read",
        description=(
            "Who your spend is reaching vs who is efficiently converting, broken by age "
            "and gender, reported alongside (never crossed with) messaging-angle and hook "
            "performance — as observed associations with the controlled tests that would "
            "confirm them."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="objective_metric",
                description="roas, cpa, or ctr — required to unlock outcome direction",
                required=True,
            ),
            PromptArgument(
                name="goal_direction",
                description="maximize or minimize; must agree with objective_metric",
                required=True,
            ),
            PromptArgument(
                name="date_preset",
                description=(
                    "all_time, last_7_days, last_30_days, or last_90_days "
                    "(default last_30_days)"
                ),
                required=False,
            ),
        ],
    ),
    Prompt(
        name="client_review_pack",
        title="Client Review Pack",
        description=(
            "Assemble the monthly client review: what drove results, what the winners "
            "have in common, what we tested and learned, and what to test next — every "
            "figure stamped with sync freshness so it reconciles with what the client "
            "sees in Ads Manager."
        ),
        arguments=[
            PromptArgument(
                name="brand_name",
                description="Exact workspace brand_name from list_workspaces",
                required=True,
            ),
            PromptArgument(
                name="period_start",
                description="Review period start, YYYY-MM-DD",
                required=True,
            ),
            PromptArgument(
                name="period_end",
                description="Review period end, YYYY-MM-DD",
                required=True,
            ),
            PromptArgument(
                name="client_cpa_target",
                description="Client's CPA target, for an against-target verdict (optional)",
                required=False,
            ),
            PromptArgument(
                name="include_competitors",
                description="Include a competitor-context section from saved scans (default true)",
                required=False,
            ),
        ],
    ),
]

_PROMPT_BUILDERS: dict[str, Any] = {
    "weekly_creative_report": _prompt_weekly_creative_report,
    "fatigue_check": _prompt_fatigue_check,
    "scale_kill_hold": _prompt_scale_kill_hold,
    "what_to_make_next_brief": _prompt_what_to_make_next_brief,
    "hook_report": _prompt_hook_report,
    "batch_readout": _prompt_batch_readout,
    "monday_money_check": _prompt_monday_money_check,
    "competitive_whitespace": _prompt_competitive_whitespace,
    "audience_read": _prompt_audience_read,
    "client_review_pack": _prompt_client_review_pack,
}


@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    return _PROMPTS


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    """Render one prompt's messages. Unknown names and invalid/missing argument
    values (from the per-prompt _prompt_* validators) surface as a protocol-level
    INVALID_PARAMS error, not a silently-wrong rendered report."""
    builder = _PROMPT_BUILDERS.get(name)
    if builder is None:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown prompt: {name}"))
    try:
        return builder(arguments or {})
    except ValueError as e:
        raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e


# ---------- Main ----------


def main():
    import asyncio

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
