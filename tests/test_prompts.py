"""Tests for the MCP prompts capability (list_prompts / get_prompt).

Unlike test_tool_surface.py (source-parsed) these import creative_tagger_mcp.server
for real, the same way test_http_behavior.py does, and exercise the actual
registered @server.list_prompts() / @server.get_prompt() handlers plus the
underlying _prompt_* builder functions directly.

Coverage:
- list_prompts(): exact count and name set, every prompt requires brand_name.
- get_prompt(): every prompt renders with only its required args (defaults
  fill the rest) and again with every argument set explicitly, and the
  explicit values actually show up in the rendered text.
- Argument validation: missing required args, bad enums, and cross-field
  agreement checks (goal_direction vs objective_metric) raise ValueError from
  the builder and INVALID_PARAMS through the real protocol dispatch.
- Tool/prompt drift prevention: every `tool_name(` reference inside a
  rendered template is parsed out and checked against the real public tool
  catalog from list_tools() — a renamed or removed tool fails this test
  immediately instead of silently breaking a client relying on the prompt.
- Every rendered prompt carries the shared measurement-honesty contract.
- prompts capability is advertised in initialization options.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.session import RequestResponder
from mcp.types import GetPromptRequest, GetPromptRequestParams, INVALID_PARAMS

from creative_tagger_mcp import server


def run(coro):
    return asyncio.run(coro)


TOOL_REFERENCE_PATTERN = re.compile(r"`([a-z][a-z0-9_]*)\(")

EXPECTED_PROMPT_NAMES = {
    "weekly_creative_report",
    "fatigue_check",
    "scale_kill_hold",
    "what_to_make_next_brief",
    "hook_report",
    "batch_readout",
    "monday_money_check",
    "competitive_whitespace",
    "audience_read",
    "client_review_pack",
}

# Minimal (required-args-only) call for each prompt.
MINIMAL_ARGS = {
    "weekly_creative_report": {"brand_name": "Acme"},
    "fatigue_check": {"brand_name": "Acme"},
    "scale_kill_hold": {
        "brand_name": "Acme",
        "objective_metric": "roas",
        "target_value": "2.5",
    },
    "what_to_make_next_brief": {"brand_name": "Acme"},
    "hook_report": {"brand_name": "Acme"},
    "batch_readout": {"brand_name": "Acme", "batch_start_date": "2026-07-01"},
    "monday_money_check": {"brand_name": "Acme", "breakeven_roas": "2.0"},
    "competitive_whitespace": {"brand_name": "Acme"},
    "audience_read": {
        "brand_name": "Acme",
        "objective_metric": "roas",
        "goal_direction": "maximize",
    },
    "client_review_pack": {
        "brand_name": "Acme",
        "period_start": "2026-06-01",
        "period_end": "2026-06-30",
    },
}

# Every argument set explicitly (including optional ones), each to a
# non-default value, so rendering-with-explicit-args is a real test of
# argument plumbing, not just a repeat of the defaults path.
EXPLICIT_ARGS = {
    "weekly_creative_report": {
        "brand_name": "Beta Brand",
        "date_preset": "last_30_days",
        "target_roas": "3.5",
        "target_cpa": "",
        "spend_threshold": "750",
    },
    "fatigue_check": {
        "brand_name": "Beta Brand",
        "top_n": "3",
        "metric": "cpa",
        "date_preset": "last_90d",
    },
    "scale_kill_hold": {
        "brand_name": "Beta Brand",
        "objective_metric": "cpa",
        "target_value": "40",
        "minimum_spend": "1000",
        "date_preset": "last_90_days",
    },
    "what_to_make_next_brief": {
        "brand_name": "Beta Brand",
        "production_slots": "3",
        "formats_available": "UGC video, static",
        "date_preset": "last_90_days",
    },
    "hook_report": {
        "brand_name": "Beta Brand",
        "date_preset": "last_90_days",
        "spend_threshold": "1000",
    },
    "batch_readout": {
        "brand_name": "Beta Brand",
        "batch_start_date": "2026-06-01",
        "batch_end_date": "2026-06-14",
        "baseline_preset": "all_time",
    },
    "monday_money_check": {
        "brand_name": "Beta Brand",
        "breakeven_roas": "",
        "target_cpa": "35",
        "date_preset": "last_30_days",
    },
    "competitive_whitespace": {
        "brand_name": "Beta Brand",
        "competitor": "Rival Co",
        "country": "GB",
        "run_fresh_scan": "true",
        "date_preset": "all_time",
    },
    "audience_read": {
        "brand_name": "Beta Brand",
        "objective_metric": "cpa",
        "goal_direction": "minimize",
        "date_preset": "last_90_days",
    },
    "client_review_pack": {
        "brand_name": "Beta Brand",
        "period_start": "2026-05-01",
        "period_end": "2026-05-31",
        "client_cpa_target": "45",
        "include_competitors": "false",
    },
}

CONTRACT_MARKERS = (
    "MEASUREMENT STATES.",
    "UNATTRIBUTED BUCKET.",
    "SPEND MATERIALITY.",
    "INSUFFICIENT EVIDENCE.",
    "FRESHNESS.",
    "OUTPUT FORMAT.",
    "not_applicable",
    "not_reported",
)

# tools_used per prompt, transcribed from the reporting-synthesis prompt_specs
# (kept here as a plain literal, not read from the external scratchpad file,
# so this test is self-contained and portable to any checkout).
EXPECTED_TOOLS_USED = {
    "weekly_creative_report": {
        "get_meta_status",
        "get_meta_performance_summary",
        "compare_periods",
        "get_prebuilt_reports",
        "get_performance_timeseries",
        "get_brain_learnings",
    },
    "fatigue_check": {
        "get_meta_status",
        "get_performance_timeseries",
        "get_creative_strategy_report",
        "export_performance_timeseries_context",
    },
    "scale_kill_hold": {
        "get_meta_status",
        "get_creative_leaderboard",
        "get_creative_strategy_report",
        "get_taxonomy_performance",
    },
    "what_to_make_next_brief": {
        "get_taxonomy_performance",
        "get_creative_strategy_report",
        "get_library_patterns",
        "analyze_gaps",
        "recommend",
    },
    "hook_report": {
        "get_prebuilt_reports",
        "get_taxonomy_performance",
        "get_performance_timeseries",
        "get_creative_strategy_report",
    },
    "batch_readout": {
        "get_batch_readout",
        "create_custom_report",
    },
    "monday_money_check": {
        "get_meta_status",
        "compare_periods",
        "get_performance_timeseries",
    },
    "competitive_whitespace": {
        "get_competitor_scan_history",
        "get_competitor_scan_detail",
        "scan_competitor",
        "get_library_patterns",
        "get_creative_strategy_report",
    },
    "audience_read": {
        "get_demographics_performance",
        "export_demographics_context",
        "get_creative_strategy_report",
    },
    "client_review_pack": {
        "get_meta_status",
        "get_meta_performance_summary",
        "get_prebuilt_reports",
        "get_brain_learnings",
        "get_demographics_performance",
        "get_competitor_scan_history",
    },
}


def _referenced_tool_names(text: str) -> set[str]:
    return set(TOOL_REFERENCE_PATTERN.findall(text))


# ---------------------------------------------------------------------------
# list_prompts()
# ---------------------------------------------------------------------------


def test_list_prompts_returns_exactly_the_ten_report_recipes():
    prompts = run(server.list_prompts())

    assert len(prompts) == 10
    assert {p.name for p in prompts} == EXPECTED_PROMPT_NAMES


def test_list_prompts_every_recipe_requires_brand_name():
    prompts = run(server.list_prompts())

    for prompt in prompts:
        arg_names = {a.name: a for a in (prompt.arguments or [])}
        assert "brand_name" in arg_names, f"{prompt.name} has no brand_name argument"
        assert arg_names["brand_name"].required is True, (
            f"{prompt.name}'s brand_name argument is not marked required"
        )
        assert prompt.description, f"{prompt.name} has no description"


def test_list_prompts_declares_only_documented_arguments():
    # EXPLICIT_ARGS enumerates every argument each prompt builder accepts;
    # list_prompts()'s declared arguments must be a subset (no undocumented
    # arg silently accepted, no documented arg that doesn't actually exist).
    prompts = {p.name: p for p in run(server.list_prompts())}

    for name, declared in prompts.items():
        declared_names = {a.name for a in (declared.arguments or [])}
        assert declared_names == set(EXPLICIT_ARGS[name]), name


def test_initialize_advertises_prompts_capability():
    options = server.server.create_initialization_options()

    assert options.capabilities.prompts is not None


# ---------------------------------------------------------------------------
# get_prompt(): renders with defaults and with explicit args
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPT_NAMES))
def test_get_prompt_renders_with_only_required_args(name):
    result = run(server.get_prompt(name, MINIMAL_ARGS[name]))

    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert message.content.type == "text"
    text = message.content.text
    assert "Acme" in text
    for marker in CONTRACT_MARKERS:
        assert marker in text, f"{name} is missing contract marker {marker!r}"


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPT_NAMES))
def test_get_prompt_renders_with_every_argument_set_explicitly(name):
    result = run(server.get_prompt(name, EXPLICIT_ARGS[name]))

    assert len(result.messages) == 1
    text = result.messages[0].content.text
    assert "Beta Brand" in text
    assert "Acme" not in text
    for marker in CONTRACT_MARKERS:
        assert marker in text, f"{name} is missing contract marker {marker!r}"


def test_weekly_creative_report_explicit_args_reflected_in_text():
    text = run(
        server.get_prompt(
            "weekly_creative_report",
            {
                "brand_name": "Beta Brand",
                "date_preset": "last_30_days",
                "target_roas": "3.5",
                "spend_threshold": "750",
            },
        )
    ).messages[0].content.text

    assert "last_30_days" in text
    assert "Target: ROAS >= 3.5" in text
    assert "spend_threshold=750" in text
    # get_performance_timeseries uses the translated last_30d, not last_30_days.
    assert 'date_preset="last_30d"' in text


def test_fatigue_check_explicit_args_reflected_in_text():
    text = run(
        server.get_prompt(
            "fatigue_check",
            {"brand_name": "Beta Brand", "top_n": "3", "metric": "cpa", "date_preset": "last_90d"},
        )
    ).messages[0].content.text

    assert 'metric="cpa"' in text
    assert "limit=3" in text
    # get_creative_strategy_report uses the translated last_90_days, not last_90d.
    assert 'date_preset="last_90_days"' in text
    assert 'date_preset="last_90d"' in text


def test_scale_kill_hold_uses_correct_target_param_name_per_metric():
    roas_text = run(
        server.get_prompt(
            "scale_kill_hold",
            {"brand_name": "Acme", "objective_metric": "roas", "target_value": "2.5"},
        )
    ).messages[0].content.text
    cpa_text = run(
        server.get_prompt(
            "scale_kill_hold",
            {"brand_name": "Acme", "objective_metric": "cpa", "target_value": "40"},
        )
    ).messages[0].content.text

    assert "roas_target=2.5" in roas_text
    assert "cpa_target=40" in cpa_text


def test_hook_report_uses_hook_not_hook_type_for_strategy_report_dimension():
    text = run(server.get_prompt("hook_report", {"brand_name": "Acme"})).messages[0].content.text

    assert 'rows="hook"' in text
    assert 'rows="hook_type"' not in text
    # But get_taxonomy_performance's own dimension IS hook_type.
    assert 'dimension="hook_type"' in text


def test_batch_readout_defaults_end_date_to_today():
    today = datetime.now(timezone.utc).date().isoformat()

    text = run(
        server.get_prompt(
            "batch_readout", {"brand_name": "Acme", "batch_start_date": "2026-07-01"}
        )
    ).messages[0].content.text

    assert f"to {today}" in text
    assert f'end_date="{today}"' in text


def test_monday_money_check_computes_prior_window_from_date_preset():
    text = run(
        server.get_prompt(
            "monday_money_check",
            {"brand_name": "Acme", "breakeven_roas": "2.0", "date_preset": "last_7_days"},
        )
    ).messages[0].content.text

    today = datetime.now(timezone.utc).date()
    this_start = today - timedelta(days=6)
    prior_end = this_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=6)

    assert f'period_a_start="{prior_start.isoformat()}"' in text
    assert f'period_a_end="{prior_end.isoformat()}"' in text
    assert f'period_b_start="{this_start.isoformat()}"' in text
    assert "no Shopify/blended-revenue connector" in text


def test_client_review_pack_omits_competitor_step_when_disabled():
    with_competitors = run(
        server.get_prompt(
            "client_review_pack",
            {
                "brand_name": "Acme",
                "period_start": "2026-06-01",
                "period_end": "2026-06-30",
                "include_competitors": "true",
            },
        )
    ).messages[0].content.text
    without_competitors = run(
        server.get_prompt(
            "client_review_pack",
            {
                "brand_name": "Acme",
                "period_start": "2026-06-01",
                "period_end": "2026-06-30",
                "include_competitors": "false",
            },
        )
    ).messages[0].content.text

    assert "get_competitor_scan_history" in with_competitors
    assert "get_competitor_scan_history" not in without_competitors


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, args, message_fragment",
    [
        ("weekly_creative_report", {}, "brand_name is required"),
        ("weekly_creative_report", {"brand_name": "Acme", "date_preset": "bogus"}, "date_preset must be one of"),
        ("scale_kill_hold", {"brand_name": "Acme"}, "objective_metric is required"),
        (
            "scale_kill_hold",
            {"brand_name": "Acme", "objective_metric": "roas"},
            "target_value is required",
        ),
        (
            "scale_kill_hold",
            {"brand_name": "Acme", "objective_metric": "revenue", "target_value": "2"},
            "objective_metric must be one of",
        ),
        ("monday_money_check", {"brand_name": "Acme"}, "breakeven_roas is required"),
        (
            "audience_read",
            {"brand_name": "Acme", "objective_metric": "cpa", "goal_direction": "maximize"},
            "goal_direction must be",
        ),
        (
            "audience_read",
            {"brand_name": "Acme", "objective_metric": "roas"},
            "goal_direction is required",
        ),
        (
            "batch_readout",
            {"brand_name": "Acme", "batch_start_date": "07/01/2026"},
            "batch_start_date must be YYYY-MM-DD",
        ),
        ("batch_readout", {"brand_name": "Acme"}, "batch_start_date is required"),
        (
            "client_review_pack",
            {"brand_name": "Acme", "period_start": "2026-01-01"},
            "period_end is required",
        ),
        ("client_review_pack", {"brand_name": "Acme"}, "period_start is required"),
    ],
)
def test_prompt_builder_raises_value_error_for_invalid_args(name, args, message_fragment):
    # The underlying _prompt_* builder is where the validation logic lives;
    # test it directly (plain ValueError) independent of get_prompt()'s
    # protocol-error wrapping, which has its own dedicated tests below.
    builder = server._PROMPT_BUILDERS[name]
    with pytest.raises(ValueError, match=re.escape(message_fragment)):
        builder(args)


@pytest.mark.parametrize(
    "name, args, message_fragment",
    [
        ("weekly_creative_report", {}, "brand_name is required"),
        ("scale_kill_hold", {"brand_name": "Acme"}, "objective_metric is required"),
        ("monday_money_check", {"brand_name": "Acme"}, "breakeven_roas is required"),
        (
            "audience_read",
            {"brand_name": "Acme", "objective_metric": "cpa", "goal_direction": "maximize"},
            "goal_direction must be",
        ),
        ("batch_readout", {"brand_name": "Acme"}, "batch_start_date is required"),
        ("client_review_pack", {"brand_name": "Acme"}, "period_start is required"),
    ],
)
def test_get_prompt_wraps_invalid_args_as_mcperror(name, args, message_fragment):
    # get_prompt() itself (the registered protocol handler) converts the same
    # ValueError into McpError(INVALID_PARAMS) — this is the contract any real
    # MCP client actually observes.
    with pytest.raises(McpError) as exc_info:
        run(server.get_prompt(name, args))
    assert exc_info.value.error.code == INVALID_PARAMS
    assert message_fragment in exc_info.value.error.message


def test_get_prompt_raises_mcperror_for_unknown_prompt_name():
    with pytest.raises(McpError) as exc_info:
        run(server.get_prompt("not_a_real_prompt", {}))
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "Unknown prompt: not_a_real_prompt" in exc_info.value.error.message


# "custom" is unsafe on every prompt that exposes date_preset as a plain
# string with no start_date/end_date argument of its own: some of the tools
# behind these prompts (get_taxonomy_performance, get_demographics_performance)
# silently resolve a dateless "custom" to all-time history, while
# get_creative_strategy_report's own API rejects a dateless "custom" with an
# HTTP 400 — so it must be rejected at the prompt layer for all five, not
# just scale_kill_hold.
CUSTOM_PRESET_REJECTION_CASES = {
    "scale_kill_hold": {
        "brand_name": "Acme",
        "objective_metric": "roas",
        "target_value": "2.5",
        "date_preset": "custom",
    },
    "what_to_make_next_brief": {"brand_name": "Acme", "date_preset": "custom"},
    "hook_report": {"brand_name": "Acme", "date_preset": "custom"},
    "competitive_whitespace": {"brand_name": "Acme", "date_preset": "custom"},
    "audience_read": {
        "brand_name": "Acme",
        "objective_metric": "roas",
        "goal_direction": "maximize",
        "date_preset": "custom",
    },
}


@pytest.mark.parametrize("name", sorted(CUSTOM_PRESET_REJECTION_CASES))
def test_custom_date_preset_is_rejected_where_no_start_end_args_exist(name):
    # Builder layer: plain ValueError.
    builder = server._PROMPT_BUILDERS[name]
    with pytest.raises(ValueError, match="date_preset must be one of"):
        builder(CUSTOM_PRESET_REJECTION_CASES[name])

    # Protocol layer: McpError(INVALID_PARAMS), the contract a real client sees.
    with pytest.raises(McpError) as exc_info:
        run(server.get_prompt(name, CUSTOM_PRESET_REJECTION_CASES[name]))
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "custom" in exc_info.value.error.message


@pytest.mark.parametrize("name", sorted(CUSTOM_PRESET_REJECTION_CASES))
def test_validated_date_presets_never_include_custom(name):
    # list_prompts()'s own description must not advertise a value get_prompt
    # then rejects.
    prompts = {p.name: p for p in run(server.list_prompts())}
    date_preset_arg = next(
        a for a in prompts[name].arguments if a.name == "date_preset"
    )
    assert "custom" not in date_preset_arg.description


REVERSED_DATE_PAIR_CASES = [
    (
        "batch_readout",
        {
            "brand_name": "Acme",
            "batch_start_date": "2026-07-10",
            "batch_end_date": "2026-07-01",
        },
        "batch_start_date must be on or before batch_end_date",
    ),
    (
        "client_review_pack",
        {
            "brand_name": "Acme",
            "period_start": "2026-07-10",
            "period_end": "2026-07-01",
        },
        "period_start must be on or before period_end",
    ),
]


@pytest.mark.parametrize("name, args, message_fragment", REVERSED_DATE_PAIR_CASES)
def test_reversed_date_pair_is_rejected(name, args, message_fragment):
    builder = server._PROMPT_BUILDERS[name]
    with pytest.raises(ValueError, match=re.escape(message_fragment)):
        builder(args)

    with pytest.raises(McpError) as exc_info:
        run(server.get_prompt(name, args))
    assert exc_info.value.error.code == INVALID_PARAMS
    assert message_fragment in exc_info.value.error.message


def test_equal_start_and_end_dates_are_not_a_reversed_pair():
    # A same-day batch/period is valid — the ordering check must be strict
    # (>), not >=.
    batch_result = run(
        server.get_prompt(
            "batch_readout",
            {
                "brand_name": "Acme",
                "batch_start_date": "2026-07-01",
                "batch_end_date": "2026-07-01",
            },
        )
    )
    assert "2026-07-01" in batch_result.messages[0].content.text

    review_result = run(
        server.get_prompt(
            "client_review_pack",
            {
                "brand_name": "Acme",
                "period_start": "2026-07-01",
                "period_end": "2026-07-01",
            },
        )
    )
    assert "2026-07-01" in review_result.messages[0].content.text


def test_batch_readout_default_end_date_never_precedes_explicit_start():
    # batch_end_date defaults to "today"; a batch_start_date in the future
    # relative to today must still be caught, not silently accepted.
    future_start = (datetime.now(timezone.utc).date() + timedelta(days=5)).isoformat()
    with pytest.raises(McpError) as exc_info:
        run(
            server.get_prompt(
                "batch_readout",
                {"brand_name": "Acme", "batch_start_date": future_start},
            )
        )
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "batch_start_date must be on or before batch_end_date" in exc_info.value.error.message


def test_shared_contract_documents_utc_anchored_windows():
    text = run(
        server.get_prompt("weekly_creative_report", {"brand_name": "Acme"})
    ).messages[0].content.text
    assert "UTC" in text
    assert "WINDOWS." in text


def test_monday_money_check_mer_wording_uses_not_applicable_not_unavailable():
    prompts = {p.name: p for p in run(server.list_prompts())}
    description = prompts["monday_money_check"].description
    assert "not_applicable" in description
    assert "unavailable" not in description


def test_competitive_whitespace_competitor_arg_does_not_promise_page_id_routing():
    prompts = {p.name: p for p in run(server.list_prompts())}
    competitor_arg = next(
        a for a in prompts["competitive_whitespace"].arguments if a.name == "competitor"
    )
    assert "page_id" not in competitor_arg.description
    # And the template itself never wires anything but page_name.
    text = run(
        server.get_prompt("competitive_whitespace", {"brand_name": "Acme", "competitor": "Rival Co"})
    ).messages[0].content.text
    assert "page_name=" in text
    assert "page_id=" not in text


def _drive_handle_request(request):
    """Push one request through the real low-level dispatch path
    (server.server._handle_request), the same machinery a live stdio/HTTP
    transport uses, so protocol-level error codes are exercised for real
    rather than asserted against the bare Python exception."""
    message = MagicMock(spec=RequestResponder)
    message.request_id = 1
    message.request_meta = None
    message.message_metadata = None
    message.cancelled = False
    responses = []

    async def fake_respond(resp):
        responses.append(resp)

    message.respond = fake_respond
    session = MagicMock()
    session.client_params = None

    run(
        server.server._handle_request(
            message, request, session, None, raise_exceptions=False
        )
    )
    return responses[0]


def test_protocol_dispatch_returns_invalid_params_for_missing_required_arg():
    request = GetPromptRequest(
        params=GetPromptRequestParams(name="weekly_creative_report", arguments={})
    )

    response = _drive_handle_request(request)

    assert response.code == INVALID_PARAMS
    assert "brand_name is required" in response.message


def test_protocol_dispatch_returns_invalid_params_for_unknown_prompt():
    request = GetPromptRequest(params=GetPromptRequestParams(name="nope", arguments={}))

    response = _drive_handle_request(request)

    assert response.code == INVALID_PARAMS
    assert "Unknown prompt: nope" in response.message


def test_protocol_dispatch_succeeds_for_a_valid_request():
    request = GetPromptRequest(
        params=GetPromptRequestParams(
            name="weekly_creative_report", arguments={"brand_name": "Acme"}
        )
    )

    response = _drive_handle_request(request)

    assert "Acme" in response.root.messages[0].content.text


# ---------------------------------------------------------------------------
# Tool/prompt drift prevention: every referenced tool name must be real
# ---------------------------------------------------------------------------


def test_every_tool_referenced_in_a_prompt_template_exists_in_the_public_catalog():
    real_tool_names = {tool.name for tool in run(server.list_tools())}

    for name in sorted(EXPECTED_PROMPT_NAMES):
        text = run(server.get_prompt(name, MINIMAL_ARGS[name])).messages[0].content.text
        referenced = _referenced_tool_names(text)
        assert referenced, f"{name}'s template references no tools at all"
        unknown = referenced - real_tool_names
        assert not unknown, f"{name} references nonexistent tool(s): {sorted(unknown)}"


def test_no_prompt_template_references_an_internal_backfill_tool():
    for name in sorted(EXPECTED_PROMPT_NAMES):
        text = run(server.get_prompt(name, MINIMAL_ARGS[name])).messages[0].content.text
        referenced = _referenced_tool_names(text)
        assert not referenced & server.INTERNAL_BACKFILL_TOOLS, name


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPT_NAMES))
def test_prompt_template_calls_every_tool_in_its_documented_tools_used(name):
    text = run(server.get_prompt(name, MINIMAL_ARGS[name])).messages[0].content.text
    referenced = _referenced_tool_names(text)

    assert referenced == EXPECTED_TOOLS_USED[name], (
        f"{name}: referenced {sorted(referenced)} != documented "
        f"tools_used {sorted(EXPECTED_TOOLS_USED[name])}"
    )
