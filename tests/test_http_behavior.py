"""HTTP-behavior tests for the real MCP tool functions.

Unlike test_tool_surface.py (which parses server.py as source so it can run
before the MCP/httpx runtime dependencies are installed), these tests import
creative_tagger_mcp.server for real and exercise the actual async tool
functions and the actual call_tool() dispatcher. The only thing that's fake
is the network: the `mock_api` fixture (tests/conftest.py) swaps
httpx.AsyncClient's transport for an in-process MockTransport, so every
`async with httpx.AsyncClient(...)` in server.py sends a real httpx.Request
through real httpx request-building code and gets back a scripted
httpx.Response — no server process, no sockets, no live API.

Coverage:
- request shape: X-API-Key header set, never an api_key query param
- response parsing: JSON success bodies, the get_brand_context 404 special
  case, and get_taxonomy's versioned package vocabulary
- error handling: 401 / 429 / 500 (JSON and non-JSON bodies), and malformed
  JSON on an otherwise-200 response, all resolve to a clean single
  TextContent error - never a raised exception/traceback
- timeout behavior: ReadTimeout / ConnectTimeout / PoolTimeout are all caught
  by call_tool()'s dedicated `except httpx.TimeoutException` branch and get
  the same friendly, actionable message regardless of the underlying
  exception's own text (mirroring the sibling ConnectError branch); a
  message-less timeout no longer degrades to an uninformative bare "Error: "
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from creative_tagger_mcp import __version__, server


def run(coro):
    return asyncio.run(coro)


def as_json(result) -> object:
    content = result.content if hasattr(result, "content") else result
    assert len(content) == 1
    return json.loads(content[0].text)


def as_text(result) -> str:
    content = result.content if hasattr(result, "content") else result
    assert len(content) == 1
    return content[0].text


# ---------------------------------------------------------------------------
# _headers() / _auth_params(): the header-only auth contract
# ---------------------------------------------------------------------------


def test_headers_include_api_key_when_configured(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "ct_abc123")
    assert server._headers() == {"X-API-Key": "ct_abc123"}


def test_headers_omit_api_key_when_unset(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")
    assert server._headers() == {}


def test_auth_params_is_always_an_empty_shim():
    # Auth moved to the X-API-Key header; _auth_params() must never leak a
    # key into a query string again.
    assert server._auth_params() == {}


# ---------------------------------------------------------------------------
# list_tools(): internal backfill visibility
# ---------------------------------------------------------------------------


def test_list_tools_hides_internal_backfill_tools_by_default(monkeypatch):
    monkeypatch.delenv("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", raising=False)
    tools = run(server.list_tools())
    names = {tool.name for tool in tools}
    assert "import_meta_performance" not in names
    assert "import_competitor_ads" not in names
    assert "analyze_creative" in names


def test_list_tools_shows_internal_backfill_tools_when_env_flag_set(monkeypatch):
    monkeypatch.setenv("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", "1")
    tools = run(server.list_tools())
    names = {tool.name for tool in tools}
    assert "import_meta_performance" not in names
    assert "import_competitor_ads" in names


def test_public_tool_catalog_stays_under_context_budget_without_losing_contracts(
    monkeypatch,
):
    monkeypatch.delenv("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", raising=False)
    tools = run(server.list_tools())
    payload = json.dumps(
        [tool.model_dump(exclude_none=True) for tool in tools],
        separators=(",", ":"),
    )
    by_name = {tool.name: tool for tool in tools}

    assert len(payload) < 40_000  # <10k conservative char/4 proxy tokens
    assert "opportunity" not in payload.lower()
    assert "waste" not in payload.lower()
    strategy = by_name["get_creative_strategy_report"]
    assert "observational" in strategy.description
    assert strategy.inputSchema["properties"]["response_format"]["default"] == "concise"
    assert strategy.inputSchema["properties"]["rows"]["description"]
    assert by_name["save_brain_learnings"].inputSchema["properties"]["limit"] == {
        "type": "integer",
        "default": 8,
        "minimum": 1,
        "maximum": 12,
    }


def test_initialize_reports_package_version_and_workspace_first_playbook():
    options = server.server.create_initialization_options()

    assert options.server_version == __version__ == "0.2.4"
    assert "call list_workspaces first" in options.instructions
    assert "historical associations" in options.instructions
    assert "falsifiable" in options.instructions
    assert "ship/stop" in options.instructions


def test_registered_mcp_handler_sets_is_error_for_tool_failures(mock_api):
    request = CallToolRequest(
        params=CallToolRequestParams(name="not_a_real_tool", arguments={})
    )
    result = run(server.server.request_handlers[CallToolRequest](request)).root

    assert result.isError is True
    assert result.content[0].text == "Error: Unknown tool: not_a_real_tool"


def test_registered_mcp_handler_sets_is_error_for_http_failures(mock_api):
    mock_api.queue(httpx.Response(401, json={"detail": "Invalid API key"}))
    request = CallToolRequest(
        params=CallToolRequestParams(name="list_workspaces", arguments={})
    )
    result = run(server.server.request_handlers[CallToolRequest](request)).root

    assert result.isError is True
    assert result.content[0].text == "Error: API error (401): Invalid API key"


# ---------------------------------------------------------------------------
# call_tool() dispatcher contract
# ---------------------------------------------------------------------------


def test_call_tool_unknown_tool_returns_clean_error(mock_api):
    result = run(server.call_tool("not_a_real_tool", {}))
    assert as_text(result) == "Error: Unknown tool: not_a_real_tool"
    assert result.isError is True
    assert mock_api.requests == []


def test_call_tool_rejects_removed_meta_import_tool(mock_api, monkeypatch):
    monkeypatch.setenv("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", "1")
    result = run(
        server.call_tool("import_meta_performance", {"rows": [{"ad_id": "1"}]})
    )
    assert as_text(result) == "Error: Unknown tool: import_meta_performance"
    assert result.isError is True
    assert mock_api.requests == []


def test_call_tool_dispatches_sync_tool_without_any_http_call(mock_api):
    # generate_naming is the one dispatched tool that isn't async / doesn't
    # touch the network at all.
    result = run(
        server.call_tool(
            "generate_naming",
            {"brand_name": "Acme", "hook_type": "Question", "cta_type": "Shop Now"},
        )
    )
    payload = as_json(result)
    assert "standard" in payload
    assert mock_api.requests == []


# ---------------------------------------------------------------------------
# Request shape: representative GET / POST(json) / POST(form) / DELETE calls
# ---------------------------------------------------------------------------


def test_get_tool_sends_header_auth_and_query_params_without_api_key(mock_api):
    mock_api.queue(httpx.Response(200, json={"items": []}))
    run(server._list_library({"limit": 5, "search": "BFCM", "sort": "roas"}))

    req = mock_api.last_request
    assert req.method == "GET"
    assert req.url.path == "/auth/library"
    assert req.headers.get("x-api-key") == "test-api-key-123"
    params = mock_api.query_params()
    assert params == {"limit": "5", "search": "BFCM", "sort": "roas"}
    assert "api_key" not in params


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"limit": -1}, {"limit": "1"}),
        ({"limit": 0}, {"limit": "1"}),
        ({"limit": 10_000}, {"limit": "100"}),
        ({"offset": -9}, {"offset": "0"}),
        (
            {"limit": 10_000, "offset": -9},
            {"limit": "100", "offset": "0"},
        ),
    ],
)
def test_list_library_clamps_pagination_before_api_request(mock_api, args, expected):
    mock_api.queue(httpx.Response(200, json={"items": []}))

    run(server._list_library(args))

    assert mock_api.query_params() == expected


def test_list_library_rejects_non_integer_pagination_without_http_call(mock_api):
    result = run(server._list_library({"limit": "many"}))

    assert as_text(result) == "Error: limit must be an integer"
    assert mock_api.requests == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 8),
        (-7, 1),
        (-7.0, 1),
        (-0.0, 1),
        (1, 1),
        (1.0, 1),
        (50, 50),
        (50.0, 50),
        (51.0, 50),
        (1e100, 50),
    ],
)
def test_clamped_int_arg_matches_json_schema_integer_semantics(value, expected):
    assert (
        server._clamped_int_arg(
            value,
            default=8,
            minimum=1,
            maximum=50,
            field_name="limit",
        )
        == expected
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(1.5, id="fractional-positive"),
        pytest.param(-1.5, id="fractional-negative"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="positive-infinity"),
        pytest.param(-math.inf, id="negative-infinity"),
        pytest.param("1", id="numeric-string"),
        pytest.param("many", id="string"),
        pytest.param([], id="array"),
        pytest.param({}, id="object"),
    ],
)
def test_clamped_int_arg_rejects_non_json_schema_integers(value):
    with pytest.raises(ValueError, match="^limit must be an integer$"):
        server._clamped_int_arg(
            value,
            default=8,
            minimum=1,
            maximum=50,
            field_name="limit",
        )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(-1, "1"), (0, "1"), (10_000, "10")],
)
def test_performance_timeseries_clamps_collection_limit_locally(
    mock_api, requested, expected
):
    mock_api.queue(httpx.Response(200, json={"series": []}))

    run(server._get_performance_timeseries({"limit": requested}))

    assert mock_api.query_params()["limit"] == expected


def test_performance_timeseries_export_uses_the_same_local_cap(mock_api):
    mock_api.queue(
        httpx.Response(
            200,
            json={"series": [], "agent_context": {}, "summary": {}},
        )
    )

    run(server._export_performance_timeseries_context({"limit": 10_000}))

    assert mock_api.query_params()["limit"] == "10"


PUBLIC_COLLECTION_LIMITS = [
    (
        "prebuilt.limit",
        "get_prebuilt_reports",
        "limit",
        server._get_prebuilt_reports,
        {"brand_name": "Acme"},
        8,
        50,
        "query",
        {"reports": []},
    ),
    (
        "strategy.limit",
        "get_creative_strategy_report",
        "limit",
        server._get_creative_strategy_report,
        {"brand_name": "Acme"},
        10,
        25,
        "query",
        {"cells": []},
    ),
    (
        "strategy.watch_limit",
        "get_creative_strategy_report",
        "watch_limit",
        server._get_creative_strategy_report,
        {"brand_name": "Acme"},
        5,
        10,
        "query",
        {"cells": []},
    ),
    (
        "strategy.max_cells",
        "get_creative_strategy_report",
        "max_cells",
        server._get_creative_strategy_report,
        {"brand_name": "Acme"},
        24,
        200,
        "query",
        {"cells": []},
    ),
    (
        "brain-get.limit",
        "get_brain_learnings",
        "limit",
        server._get_brain_learnings,
        {"brand_name": "Acme"},
        8,
        12,
        "query",
        {"learnings": []},
    ),
    (
        "brain-get.audience_limit",
        "get_brain_learnings",
        "audience_limit",
        server._get_brain_learnings,
        {"brand_name": "Acme"},
        3,
        10,
        "query",
        {"learnings": []},
    ),
    (
        "brain-save.limit",
        "save_brain_learnings",
        "limit",
        server._save_brain_learnings,
        {"brand_name": "Acme"},
        8,
        12,
        "json",
        {"saved": True},
    ),
    (
        "brain-save.audience_limit",
        "save_brain_learnings",
        "audience_limit",
        server._save_brain_learnings,
        {"brand_name": "Acme"},
        3,
        10,
        "json",
        {"saved": True},
    ),
    (
        "brain-export.limit",
        "export_brain_learnings_context",
        "limit",
        server._export_brain_learnings_context,
        {"brand_name": "Acme"},
        8,
        12,
        "query",
        {"agent_context": {}, "summary": {}, "learnings": []},
    ),
    (
        "brain-export.audience_limit",
        "export_brain_learnings_context",
        "audience_limit",
        server._export_brain_learnings_context,
        {"brand_name": "Acme"},
        3,
        10,
        "query",
        {"agent_context": {}, "summary": {}, "learnings": []},
    ),
    (
        "timeseries-get.limit",
        "get_performance_timeseries",
        "limit",
        server._get_performance_timeseries,
        {"brand_name": "Acme"},
        10,
        10,
        "query",
        {"series": []},
    ),
    (
        "timeseries-export.limit",
        "export_performance_timeseries_context",
        "limit",
        server._export_performance_timeseries_context,
        {"brand_name": "Acme"},
        10,
        10,
        "query",
        {"agent_context": {}, "summary": {}, "series": []},
    ),
    (
        "custom-create.limit",
        "create_custom_report",
        "limit",
        server._create_custom_report,
        {"brand_name": "Acme", "dimensions": ["hook_type"]},
        12,
        50,
        "json",
        {"rows": []},
    ),
    (
        "custom-save.limit",
        "save_custom_report",
        "limit",
        server._save_custom_report,
        {"brand_name": "Acme", "name": "Hooks", "dimensions": ["hook_type"]},
        12,
        50,
        "json",
        {"saved": True},
    ),
    (
        "competitor-scan.limit",
        "scan_competitor",
        "limit",
        server._scan_competitor,
        {"brand_name": "Acme", "page_name": "Rival"},
        25,
        50,
        "json",
        {"ads": []},
    ),
    (
        "competitor-history.limit",
        "get_competitor_scan_history",
        "limit",
        server._get_competitor_scan_history,
        {"brand_name": "Acme"},
        10,
        50,
        "query",
        {"items": []},
    ),
]


@pytest.mark.parametrize(
    ("case_id", "tool_name", "field_name", "_handler", "_base_args", "default", "maximum", "_transport", "_response"),
    PUBLIC_COLLECTION_LIMITS,
    ids=[case[0] for case in PUBLIC_COLLECTION_LIMITS],
)
def test_public_collection_limit_schemas_match_runtime_contract(
    case_id,
    tool_name,
    field_name,
    _handler,
    _base_args,
    default,
    maximum,
    _transport,
    _response,
):
    del case_id
    by_name = {tool.name: tool for tool in run(server.list_tools())}
    field = by_name[tool_name].inputSchema["properties"][field_name]

    assert field["type"] == "integer"
    assert field["default"] == default
    assert field["minimum"] == 1
    assert field["maximum"] == maximum


@pytest.mark.parametrize(
    "boundary",
    [
        "default",
        "below",
        "minimum",
        "integral-float",
        "maximum",
        "above",
        "huge",
    ],
)
@pytest.mark.parametrize(
    ("case_id", "_tool_name", "field_name", "handler", "base_args", "default", "maximum", "transport", "response"),
    PUBLIC_COLLECTION_LIMITS,
    ids=[case[0] for case in PUBLIC_COLLECTION_LIMITS],
)
def test_public_collection_limits_are_clamped_before_http(
    mock_api,
    boundary,
    case_id,
    _tool_name,
    field_name,
    handler,
    base_args,
    default,
    maximum,
    transport,
    response,
):
    del case_id
    requested_and_expected = {
        "default": (None, default),
        "below": (-7, 1),
        "minimum": (1, 1),
        "integral-float": (1.0, 1),
        "maximum": (maximum, maximum),
        "above": (maximum + 1, maximum),
        "huge": (10**12, maximum),
    }
    requested, expected = requested_and_expected[boundary]
    args = dict(base_args)
    if requested is not None:
        args[field_name] = requested
    mock_api.queue(httpx.Response(200, json=response))

    run(handler(args))

    if transport == "query":
        actual = int(mock_api.query_params()[field_name])
    else:
        actual = json.loads(mock_api.last_request.content)[field_name]
    assert actual == expected


@pytest.mark.parametrize("invalid", ["many", 1.5, True])
@pytest.mark.parametrize(
    ("case_id", "_tool_name", "field_name", "handler", "base_args", "_default", "_maximum", "_transport", "_response"),
    PUBLIC_COLLECTION_LIMITS,
    ids=[case[0] for case in PUBLIC_COLLECTION_LIMITS],
)
def test_public_collection_limits_reject_non_integers_without_http(
    mock_api,
    invalid,
    case_id,
    _tool_name,
    field_name,
    handler,
    base_args,
    _default,
    _maximum,
    _transport,
    _response,
):
    del case_id
    args = {**base_args, field_name: invalid}

    result = run(handler(args))

    assert as_text(result) == f"Error: {field_name} must be an integer"
    assert mock_api.requests == []


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("opportunity", "higher_observed_efficiency"),
        ("opportunity-only", "higher_observed_efficiency"),
        ("waste", "lower_observed_efficiency"),
        ("waste-only", "lower_observed_efficiency"),
    ],
)
def test_brain_get_normalizes_legacy_audience_filters_internally(
    mock_api, legacy, canonical
):
    mock_api.queue(httpx.Response(200, json={"learnings": []}))

    run(
        server._get_brain_learnings(
            {"brand_name": "Acme", "audience_signal_focus": legacy}
        )
    )

    assert mock_api.query_params()["audience_signal_focus"] == canonical


def test_brain_save_normalizes_legacy_audience_filter_internally(mock_api):
    mock_api.queue(httpx.Response(200, json={"saved": True}))

    run(
        server._save_brain_learnings(
            {"brand_name": "Acme", "audience_signal_focus": "waste"}
        )
    )

    assert json.loads(mock_api.last_request.content)["audience_signal_focus"] == (
        "lower_observed_efficiency"
    )


def test_list_workspaces_uses_authenticated_workspace_endpoint(mock_api):
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "workspaces": [
                    {"brand_name": "Acme"},
                    {"brand_name": "Beta Brand"},
                ],
                "total": 2,
            },
        )
    )

    payload = as_json(run(server._list_workspaces({})))

    assert payload["total"] == 2
    assert mock_api.last_request.url.path == "/auth/workspaces"
    assert mock_api.last_request.headers["x-api-key"] == "test-api-key-123"


@pytest.mark.parametrize(
    "handler,args,expected_path",
    [
        (server._list_library, {"brand_name": "Beta Brand"}, "/auth/library"),
        (
            server._get_library_patterns,
            {"brand_name": "Beta Brand"},
            "/auth/library/patterns",
        ),
        (
            server._get_analysis,
            {"brand_name": "Beta Brand", "analysis_id": 42},
            "/auth/library/42",
        ),
        (
            server._get_meta_status,
            {"brand_name": "Beta Brand"},
            "/auth/meta/status",
        ),
    ],
)
def test_workspace_sensitive_gets_forward_exact_brand_name(
    mock_api, handler, args, expected_path
):
    mock_api.queue(httpx.Response(200, json={"ok": True}))

    run(handler(args))

    assert mock_api.last_request.url.path == expected_path
    assert mock_api.query_params()["brand_name"] == "Beta Brand"


@pytest.mark.parametrize(
    "handler,args,expected_path",
    [
        (server._list_library, {"brand_name": ""}, "/auth/library"),
        (
            server._get_library_patterns,
            {"brand_name": ""},
            "/auth/library/patterns",
        ),
        (
            server._get_analysis,
            {"brand_name": "", "analysis_id": 42},
            "/auth/library/42",
        ),
        (
            server._get_meta_status,
            {"brand_name": ""},
            "/auth/meta/status",
        ),
    ],
)
def test_workspace_sensitive_gets_preserve_explicit_default_workspace(
    mock_api, handler, args, expected_path
):
    mock_api.queue(httpx.Response(200, json={"ok": True}))

    run(handler(args))

    assert mock_api.last_request.url.path == expected_path
    assert "brand_name=" in str(mock_api.last_request.url)
    assert mock_api.query_params()["brand_name"] == ""


def test_get_analysis_keeps_default_and_named_workspace_queries_isolated(mock_api):
    mock_api.queue(httpx.Response(200, json={"id": 42, "workspace": "default"}))
    mock_api.queue(httpx.Response(200, json={"id": 42, "workspace": "Beta Brand"}))

    default_result = as_json(
        run(server._get_analysis({"brand_name": "", "analysis_id": 42}))
    )
    named_result = as_json(
        run(server._get_analysis({"brand_name": "Beta Brand", "analysis_id": 42}))
    )

    assert default_result["workspace"] == "default"
    assert named_result["workspace"] == "Beta Brand"
    assert mock_api.query_params(0) == {"brand_name": ""}
    assert mock_api.query_params(1) == {"brand_name": "Beta Brand"}


def test_two_workspace_calls_never_reuse_the_other_workspace_scope(mock_api):
    mock_api.queue(httpx.Response(200, json={"items": [{"id": 1}]}))
    mock_api.queue(httpx.Response(200, json={"items": [{"id": 2}]}))

    first = as_json(run(server._list_library({"brand_name": "Acme", "limit": 1})))
    second = as_json(
        run(server._list_library({"brand_name": "Beta Brand", "limit": 1}))
    )

    assert first["items"][0]["id"] == 1
    assert second["items"][0]["id"] == 2
    assert mock_api.query_params(0)["brand_name"] == "Acme"
    assert mock_api.query_params(1)["brand_name"] == "Beta Brand"


def test_strategy_defaults_to_bounded_concise_response_and_forwards_max_cells(
    mock_api,
):
    mock_api.queue(httpx.Response(200, json={"cells": []}))

    run(server._get_creative_strategy_report({"brand_name": "Acme"}))

    params = mock_api.query_params()
    assert params["response_format"] == "concise"
    assert params["max_cells"] == "24"


def test_strategy_preserves_explicit_detailed_opt_in(mock_api):
    mock_api.queue(httpx.Response(200, json={"cells": []}))

    run(
        server._get_creative_strategy_report(
            {
                "brand_name": "Acme",
                "response_format": "detailed",
                "max_cells": 80,
            }
        )
    )

    params = mock_api.query_params()
    assert params["response_format"] == "detailed"
    assert params["max_cells"] == "80"


def test_demographics_export_consumes_observational_band_contract(mock_api):
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "brand_name": "Acme",
                "date_window": "All time",
                "total_segments": 2,
                "totals": {"spend": 1000, "roas": 2.2},
                "higher_observed_efficiency": [
                    {
                        "age": "25-34",
                        "gender": "female",
                        "observed_efficiency_band": (
                            "higher_observed_return_per_spend"
                        ),
                        "return_per_spend_percentile": 100,
                        "spend": 200,
                        "revenue": 800,
                        "roas": 4.0,
                    }
                ],
                "lower_observed_efficiency": [
                    {
                        "age": "45-54",
                        "gender": "male",
                        "observed_efficiency_band": (
                            "lower_observed_return_per_spend"
                        ),
                        "return_per_spend_percentile": 0,
                        "spend": 500,
                        "revenue": 250,
                        "roas": 0.5,
                    }
                ],
                "outcome_verdicts_withheld": True,
                "metric_predeclaration_required": True,
                "goal_direction_predeclaration_required": True,
                "interpretation": "observational age/gender delivery only",
            },
        )
    )

    payload = as_json(
        run(server._export_demographics_context({"brand_name": "Acme", "limit": 3}))
    )

    assert payload["higher_observed_efficiency_count"] == 1
    assert payload["lower_observed_efficiency_count"] == 1
    assert payload["top_higher_observed_efficiency"][0]["segment"] == (
        "25-34 / female"
    )
    assert payload["top_lower_observed_efficiency"][0]["segment"] == "45-54 / male"
    assert len(payload["decision_queue"]) == 2
    assert all(
        item["action"] == "review_observed_delivery"
        for item in payload["decision_queue"]
    )
    assert all(
        item["observation_plan"]["interpretation"] == "association_not_causation"
        for item in payload["decision_queue"]
    )
    assert payload["outcome_verdicts_withheld"] is True
    assert "1 higher and 1 lower observed-return-per-spend" in payload["summary_text"]
    serialized = json.dumps(payload).lower()
    assert "opportunity" not in serialized
    assert "waste" not in serialized

    assert mock_api.last_request.url.path == "/performance/demographics"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(-1, 1), (0, 1), (10_000, 100)],
)
def test_demographics_export_clamps_segment_collection_limit(
    mock_api, requested, expected
):
    segments = [
        {
            "age": f"segment-{index}",
            "gender": "unknown",
            "spend": index + 1,
            "revenue": (index + 1) * 2,
            "roas": 2.0,
        }
        for index in range(120)
    ]
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "brand_name": "Acme",
                "higher_observed_efficiency": segments,
                "lower_observed_efficiency": segments,
                "totals": {},
            },
        )
    )

    payload = as_json(
        run(server._export_demographics_context({"limit": requested}))
    )

    assert len(payload["top_higher_observed_efficiency"]) == expected
    assert len(payload["top_lower_observed_efficiency"]) == expected


def test_compact_concise_strategy_fixture_stays_within_agent_token_budget(mock_api):
    cells = [
        {
            "row": f"format-{index}",
            "column": f"angle-{index}",
            "status": "learning",
            "spend": 123.45,
            "roas": 2.1,
            "next_test": "Change one hook and hold audience/offer constant.",
        }
        for index in range(24)
    ]
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "response_format": "concise",
                "cells": cells,
                "agent_context": {
                    "evidence_type": "observational_association",
                    "prompt": "Run one-variable controlled tests.",
                },
            },
        )
    )

    result = run(server._get_creative_strategy_report({"brand_name": "Acme"}))
    text = as_text(result)

    assert "\n  " not in text
    assert len(text) / 4 < 2_000


def test_get_tool_with_path_param_builds_url_from_argument(mock_api):
    mock_api.queue(httpx.Response(200, json={"id": 42}))
    run(server._get_analysis({"analysis_id": 42}))

    req = mock_api.last_request
    assert req.method == "GET"
    assert req.url.path == "/auth/library/42"
    assert req.headers.get("x-api-key") == "test-api-key-123"


def test_partial_brand_context_tool_sends_only_supplied_fields(mock_api):
    mock_api.queue(httpx.Response(200, json={"saved": True}))
    run(
        server._set_brand_context(
            {
                "brand_name": "Acme",
                "voice": "clinical, precise",
                "top_performers": ["UGC TalkHead"],
            }
        )
    )

    req = mock_api.last_request
    assert req.method == "PATCH"
    assert req.url.path == "/auth/brand-context"
    assert req.headers.get("x-api-key") == "test-api-key-123"
    assert req.headers["content-type"] == "application/json"
    assert "api_key" not in mock_api.query_params()
    body = json.loads(req.content)
    assert body["brand_name"] == "Acme"
    assert body["voice"] == "clinical, precise"
    assert body["top_performers"] == ["UGC TalkHead"]
    assert "target_audience" not in body
    assert "anti_patterns" not in body
    assert "notes" not in body


def test_partial_brand_context_tool_keeps_explicit_empty_clears(mock_api):
    mock_api.queue(httpx.Response(200, json={"saved": True}))
    run(
        server._set_brand_context(
            {
                "brand_name": "Acme",
                "voice": "",
                "top_performers": [],
            }
        )
    )

    body = json.loads(mock_api.last_request.content)
    assert body == {
        "brand_name": "Acme",
        "voice": "",
        "top_performers": [],
    }


def test_post_form_tool_sends_header_auth_and_form_body(mock_api):
    mock_api.queue(httpx.Response(200, json={"answer": "test next"}))
    run(server._recommend({"brand_name": "Acme", "question": "What next?"}))

    req = mock_api.last_request
    assert req.method == "POST"
    assert req.url.path == "/strategist/recommend"
    assert req.headers.get("x-api-key") == "test-api-key-123"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    body = dict(httpx.QueryParams(req.content.decode()))
    assert body == {"brand_name": "Acme", "question": "What next?"}


def test_predict_requires_and_preserves_observational_v2_contract(mock_api):
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "schema_version": "predict_observational.v2",
                "evidence_type": "observational_association",
                "causal_claim": False,
                "outcome_prediction": False,
                "available": True,
                "association_score": 78,
                "observed_metric": "cpa",
                "goal_direction": "lower_better",
                "dimensions": [],
                "test_hypotheses": [],
            },
        )
    )

    payload = as_json(
        run(
            server._predict_creative(
                {
                    "brand_name": "Acme",
                    "attributes": {"hook_type": "Question"},
                    "objective_metric": "cpa",
                    "goal_direction": "lower_better",
                }
            )
        )
    )

    assert payload["association_score"] == 78
    assert payload["evidence_type"] == "observational_association"
    assert payload["causal_claim"] is False
    assert payload["outcome_prediction"] is False
    assert "fit_score" not in payload
    assert "recommended_swaps" not in payload

    request = mock_api.last_request
    body = dict(httpx.QueryParams(request.content.decode()))
    assert body["contract_version"] == "predict_observational.v2"
    assert body["objective_metric"] == "cpa"
    assert body["goal_direction"] == "lower_better"


def test_predict_fails_closed_on_legacy_or_mixed_contract(mock_api):
    for response in (
        {"fit_score": 78, "recommended_swaps": ["Hook A -> Hook B"]},
        {
            "schema_version": "predict_observational.v2",
            "evidence_type": "observational_association",
            "causal_claim": False,
            "outcome_prediction": False,
            "available": True,
            "association_score": 78,
            "recommended_swaps": ["legacy field"],
        },
    ):
        mock_api.queue(httpx.Response(200, json=response))
        with pytest.raises(ValueError, match="no evidence was returned"):
            run(server._predict_creative({"brand_name": "Acme", "analysis_id": 1}))


def test_delete_tool_sends_header_auth_and_no_api_key_query_param(mock_api):
    mock_api.queue(httpx.Response(200, json={"deleted": True}))
    run(
        server._delete_brand_taxonomy_value(
            {"brand_name": "Acme", "dimension": "talent", "value": "Old Founder"}
        )
    )

    req = mock_api.last_request
    assert req.method == "DELETE"
    assert req.url.path == "/auth/brand-taxonomy/values"
    assert req.headers.get("x-api-key") == "test-api-key-123"
    params = mock_api.query_params()
    assert params["brand_name"] == "Acme"
    assert params["dimension"] == "talent"
    assert params["value"] == "Old Founder"
    assert "api_key" not in params


def test_anonymous_client_sends_no_api_key_header_at_all(mock_api_no_key):
    mock_api_no_key.queue(httpx.Response(200, json={"items": []}))
    run(server._list_library({}))

    req = mock_api_no_key.last_request
    assert "x-api-key" not in req.headers


# ---------------------------------------------------------------------------
# Auth-header contract swept across the broader tool surface
# ---------------------------------------------------------------------------

# (tool name as seen by call_tool, minimal valid args, expected method,
# expected URL path). Covers every HTTP verb used by the server (GET, POST,
# DELETE) plus every JSON/form body-encoding style, so a regression that
# reintroduces query-param auth on any one of these routes gets caught.
AUTH_SWEEP_CASES = [
    ("list_library", {}, "GET", "/auth/library"),
    ("get_library_patterns", {}, "GET", "/auth/library/patterns"),
    ("get_analysis", {"analysis_id": 42}, "GET", "/auth/library/42"),
    ("get_brand_context", {"brand_name": "Acme"}, "GET", "/auth/brand-context"),
    ("get_brand_taxonomy", {"brand_name": "Acme"}, "GET", "/auth/brand-taxonomy"),
    ("get_naming_variables", {}, "GET", "/auth/naming/variables"),
    ("list_naming_templates", {}, "GET", "/auth/naming/templates"),
    ("get_meta_status", {}, "GET", "/auth/meta/status"),
    (
        "get_meta_performance_summary",
        {"brand_name": "Acme"},
        "GET",
        "/meta/performance/summary",
    ),
    (
        "get_taxonomy_performance",
        {"brand_name": "Acme"},
        "GET",
        "/performance/by-taxonomy",
    ),
    ("get_prebuilt_reports", {"brand_name": "Acme"}, "GET", "/reports/prebuilt"),
    (
        "get_creative_strategy_report",
        {"brand_name": "Acme"},
        "GET",
        "/reports/creative-strategy",
    ),
    (
        "get_performance_timeseries",
        {"brand_name": "Acme"},
        "GET",
        "/performance/timeseries",
    ),
    (
        "get_demographics_performance",
        {"brand_name": "Acme"},
        "GET",
        "/performance/demographics",
    ),
    (
        "get_competitor_scan_history",
        {"brand_name": "Acme"},
        "GET",
        "/competitors/history",
    ),
    (
        "get_competitor_scan_detail",
        {"scan_id": 42},
        "GET",
        "/competitors/history/42",
    ),
    ("list_custom_reports", {"brand_name": "Acme"}, "GET", "/reports/custom/saved"),
    (
        "get_creative_leaderboard",
        {"brand_name": "Acme"},
        "GET",
        "/reports/creatives/leaderboard",
    ),
    (
        "get_batch_readout",
        {"brand_name": "Acme", "launched_after": "2026-01-01"},
        "GET",
        "/reports/creatives/batch",
    ),
    (
        "compare_periods",
        {
            "brand_name": "Acme",
            "period_a_preset": "last_week",
            "period_b_preset": "this_week",
        },
        "GET",
        "/reports/compare",
    ),
    (
        "run_saved_custom_report",
        {"report_id": 7},
        "GET",
        "/reports/custom/saved/7/run",
    ),
    (
        "delete_brand_taxonomy_value",
        {"brand_name": "Acme", "dimension": "talent", "value": "X"},
        "DELETE",
        "/auth/brand-taxonomy/values",
    ),
    (
        "delete_brand_entity",
        {"brand_name": "Acme", "entity_type": "product", "name": "X"},
        "DELETE",
        "/auth/brand-taxonomy/entities",
    ),
    ("delete_naming_template", {}, "DELETE", "/auth/naming/templates"),
    ("delete_custom_report", {"report_id": 7}, "DELETE", "/reports/custom/saved/7"),
    ("set_brand_context", {"brand_name": "Acme"}, "PATCH", "/auth/brand-context"),
    (
        "recommend",
        {"brand_name": "Acme", "question": "q"},
        "POST",
        "/strategist/recommend",
    ),
    ("analyze_gaps", {"brand_name": "Acme"}, "POST", "/strategist/gaps"),
    (
        "create_custom_report",
        {"brand_name": "Acme", "dimensions": ["hook_type"]},
        "POST",
        "/reports/custom",
    ),
    ("scan_competitor", {"brand_name": "Acme"}, "POST", "/competitors/scan"),
]


@pytest.mark.parametrize(
    "tool_name, args, expected_method, expected_path", AUTH_SWEEP_CASES
)
def test_tool_surface_uses_header_auth_never_query_param(
    mock_api, tool_name, args, expected_method, expected_path
):
    mock_api.queue(httpx.Response(200, json={"ok": True}))

    result = run(server.call_tool(tool_name, args))

    assert as_json(result) == {"ok": True}, (
        f"{tool_name} did not round-trip its response"
    )
    assert len(mock_api.requests) == 1, (
        f"{tool_name} did not make exactly one HTTP call"
    )
    req = mock_api.last_request
    assert req.method == expected_method, f"{tool_name} used the wrong HTTP method"
    assert req.url.path == expected_path, f"{tool_name} hit the wrong path"
    assert req.headers.get("x-api-key") == "test-api-key-123", (
        f"{tool_name} did not send X-API-Key header"
    )
    assert "api_key" not in mock_api.query_params(), (
        f"{tool_name} leaked the API key into a query param"
    )


# ---------------------------------------------------------------------------
# analyze_creative: multipart / url / html_content request shapes
# ---------------------------------------------------------------------------


def test_analyze_creative_uploads_single_file_as_multipart(mock_api, tmp_path):
    image = tmp_path / "ad.png"
    image.write_bytes(b"\x89PNG\r\nfake-bytes-not-a-real-png")
    mock_api.queue(httpx.Response(200, json={"hook_type": "Question"}))

    run(
        server._analyze_creative(
            {
                "file_path": str(image),
                "brand_name": "Acme",
                "version": 2,
                "forensic_mode": True,
            }
        )
    )

    req = mock_api.last_request
    assert req.method == "POST"
    assert req.url.path == "/analyze"
    assert req.headers.get("x-api-key") == "test-api-key-123"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="file"; filename="ad.png"' in body
    assert b"fake-bytes-not-a-real-png" in body
    assert b'name="brand_name"' in body
    assert b"Acme" in body
    assert b'name="version"' in body
    assert b"\r\n2\r\n" in body
    assert b'name="forensic_mode"' in body
    assert b"\r\ntrue\r\n" in body


def test_analyze_creative_uploads_multiple_files_for_carousel(mock_api, tmp_path):
    first = tmp_path / "slide1.jpg"
    second = tmp_path / "slide2.jpg"
    first.write_bytes(b"slide-one-bytes")
    second.write_bytes(b"slide-two-bytes")
    mock_api.queue(httpx.Response(200, json={"format": "carousel"}))

    run(
        server._analyze_creative(
            {"file_paths": [str(first), str(second)], "brand_name": "Acme"}
        )
    )

    req = mock_api.last_request
    body = req.content
    assert body.count(b'name="files"') == 2
    assert b'filename="slide1.jpg"' in body
    assert b'filename="slide2.jpg"' in body
    assert b"slide-one-bytes" in body
    assert b"slide-two-bytes" in body


def test_analyze_creative_direct_media_url_uses_file_url_field(mock_api):
    mock_api.queue(httpx.Response(200, json={"ok": True}))
    run(
        server._analyze_creative(
            {"url": "https://example.com/creative.mp4", "brand_name": "Acme"}
        )
    )

    req = mock_api.last_request
    body = dict(httpx.QueryParams(req.content.decode()))
    assert body["file_url"] == "https://example.com/creative.mp4"
    assert "page_url" not in body


def test_analyze_creative_page_url_uses_page_url_field(mock_api):
    mock_api.queue(httpx.Response(200, json={"ok": True}))
    run(
        server._analyze_creative(
            {"url": "https://example.com/landing-page", "brand_name": "Acme"}
        )
    )

    req = mock_api.last_request
    body = dict(httpx.QueryParams(req.content.decode()))
    assert body["page_url"] == "https://example.com/landing-page"
    assert "file_url" not in body


def test_analyze_creative_html_content_posts_html_field(mock_api):
    mock_api.queue(httpx.Response(200, json={"ok": True}))
    run(
        server._analyze_creative(
            {"html_content": "<html>hi</html>", "brand_name": "Acme"}
        )
    )

    req = mock_api.last_request
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    body = dict(httpx.QueryParams(req.content.decode()))
    assert body["html_content"] == "<html>hi</html>"


def test_analyze_creative_requires_one_input_source(mock_api):
    result = run(server._analyze_creative({"brand_name": "Acme"}))
    assert (
        as_text(result) == "Error: Provide file_path, file_paths, url, or html_content"
    )
    assert mock_api.requests == []


def test_analyze_creative_missing_file_returns_clean_error_no_http_call(
    mock_api, tmp_path
):
    missing = tmp_path / "does-not-exist.mp4"
    result = run(server._analyze_creative({"file_path": str(missing)}))
    assert as_text(result) == f"Error: File not found: {missing}"
    assert mock_api.requests == []


# ---------------------------------------------------------------------------
# Response parsing: get_taxonomy (versioned package vocabulary) and the
# get_brand_context 404-is-not-an-error special case
# ---------------------------------------------------------------------------


def test_get_taxonomy_returns_complete_versioned_vocabulary_without_http(mock_api):
    result = run(server._get_taxonomy({}))
    payload = as_json(result)
    assert payload["taxonomy_version"] == "v2"
    assert payload["controlled_dimension_count"] == 15
    assert payload["derived_open_dimension_count"] == 1
    assert payload["dynamic_dimension_count"] == 2
    assert payload["controlled_dimensions"]["hook_type"][:2] == [
        "Question",
        "Bold Claim",
    ]
    assert payload["controlled_dimensions"]["media_type"] == [
        "video",
        "image",
        "carousel",
        "landing_page",
        "email",
        "long_video",
    ]
    aspect_ratio = payload["derived_open_dimensions"]["aspect_ratio"]
    assert aspect_ratio["allow_other_values"] is True
    assert "9x16" in aspect_ratio["canonical_values"]
    assert "300x157" in aspect_ratio["canonical_values"]
    assert "aspect_ratio" not in payload["controlled_dimensions"]
    assert "messaging_angle" in payload["dynamic_dimensions"]
    assert mock_api.requests == []


def test_get_taxonomy_filters_controlled_dimension_case_insensitively(mock_api):
    result = run(server._get_taxonomy({"dimension": "HOOK TYPE"}))
    payload = as_json(result)
    assert payload["dimension"] == "hook_type"
    assert payload["kind"] == "controlled"
    assert "Question" in payload["values"]
    assert mock_api.requests == []


def test_get_taxonomy_describes_dynamic_dimension_without_fake_values(mock_api):
    result = run(server._get_taxonomy({"dimension": "messaging-angle"}))
    payload = as_json(result)
    assert payload["dimension"] == "messaging_angle"
    assert payload["kind"] == "dynamic"
    assert "values" not in payload
    assert mock_api.requests == []


def test_get_taxonomy_describes_aspect_ratio_as_derived_and_open(mock_api):
    result = run(server._get_taxonomy({"dimension": "aspect ratio"}))
    payload = as_json(result)
    assert payload["dimension"] == "aspect_ratio"
    assert payload["kind"] == "derived_open"
    assert payload["allow_other_values"] is True
    assert payload["canonical_values"][:3] == ["1x1", "4x5", "5x4"]
    assert "300x157" in payload["canonical_values"]
    assert "values" not in payload
    assert mock_api.requests == []


def test_get_taxonomy_unknown_dimension_returns_clean_error(mock_api):
    result = run(server._get_taxonomy({"dimension": "not_a_dimension"}))
    text = as_text(result)
    assert text.startswith("Error: Unknown dimension: not_a_dimension. Available: ")
    assert "hook_type" in text
    assert "aspect_ratio" in text
    assert "messaging_angle" in text
    assert mock_api.requests == []


def test_get_brand_context_404_is_a_normal_response_not_an_error(mock_api):
    mock_api.queue(httpx.Response(404, json={"detail": "Not found"}))

    result = run(server._get_brand_context({"brand_name": "New Brand"}))
    payload = as_json(result)
    assert payload == {
        "brand_name": "New Brand",
        "exists": False,
        "message": "No brand context saved yet",
    }


# ---------------------------------------------------------------------------
# Error handling: 401 / 429 / 500 -> clean tool errors, never a traceback
# ---------------------------------------------------------------------------


def test_call_tool_401_returns_clean_error_with_api_detail(mock_api):
    mock_api.queue(httpx.Response(401, json={"detail": "Invalid API key"}))
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == "Error: API error (401): Invalid API key"


def test_call_tool_429_returns_clean_error_with_api_detail(mock_api):
    mock_api.queue(httpx.Response(429, json={"detail": "Rate limit exceeded"}))
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == "Error: API error (429): Rate limit exceeded"


def test_call_tool_500_with_json_detail_returns_clean_error(mock_api):
    mock_api.queue(httpx.Response(500, json={"detail": "Database unavailable"}))
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == "Error: API error (500): Database unavailable"


def test_call_tool_500_without_detail_key_falls_back_to_status_text(mock_api):
    mock_api.queue(httpx.Response(500, json={"error": "unstructured failure"}))
    result = run(server.call_tool("list_library", {}))
    text = as_text(result)
    assert text.startswith("Error: API error (500):")
    assert "500 Internal Server Error" in text


def test_call_tool_500_with_non_json_body_returns_clean_error(mock_api):
    mock_api.queue(httpx.Response(500, text="<html>Internal Server Error</html>"))
    result = run(server.call_tool("list_library", {}))
    text = as_text(result)
    assert text.startswith("Error: API error (500):")
    assert "Traceback" not in text


def test_call_tool_malformed_json_on_200_response_returns_clean_error(mock_api):
    # A 200 with a body that isn't valid JSON must not blow up resp.json()
    # into the caller's face - call_tool()'s generic except Exception clause
    # is what's supposed to catch this.
    mock_api.queue(httpx.Response(200, text="not-json{"))
    result = run(server.call_tool("list_library", {}))
    text = as_text(result)
    assert text.startswith("Error: ")
    assert "Traceback" not in text


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 500, 502, 503])
def test_call_tool_error_statuses_never_raise_out_of_the_dispatcher(mock_api, status):
    mock_api.queue(httpx.Response(status, json={"detail": f"boom {status}"}))
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == f"Error: API error ({status}): boom {status}"


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


TIMEOUT_MESSAGE = (
    "Error: Timed out waiting for a response from http://mock.local. "
    "The API may be slow or unreachable."
)


def test_call_tool_read_timeout_is_caught_cleanly(mock_api):
    mock_api.queue(
        lambda req: httpx.ReadTimeout("The read operation timed out", request=req)
    )
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == TIMEOUT_MESSAGE


def test_call_tool_connect_timeout_is_caught_cleanly(mock_api):
    mock_api.queue(
        lambda req: httpx.ConnectTimeout("Connection timed out", request=req)
    )
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == TIMEOUT_MESSAGE


def test_call_tool_pool_timeout_is_caught_cleanly(mock_api):
    mock_api.queue(lambda req: httpx.PoolTimeout("Pool timed out", request=req))
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == TIMEOUT_MESSAGE


def test_call_tool_connect_error_returns_friendly_actionable_message(mock_api):
    mock_api.queue(lambda req: httpx.ConnectError("Connection refused", request=req))
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == (
        "Error: Cannot connect to Creative Tagger API at http://mock.local. "
        "Set CREATIVE_TAGGER_URL or check the API is running."
    )


def test_read_timeout_with_no_message_still_gets_a_helpful_error(mock_api):
    """Regression guard for a defect found while writing this suite:
    httpcore/httpx frequently raise timeout exceptions with an empty message
    on genuine socket timeouts. Before call_tool() had a dedicated
    `except httpx.TimeoutException` branch, an empty-message ReadTimeout fell
    through to the generic `except Exception as e: return _err(str(e))` and
    produced an uninformative "Error: " with zero signal a timeout occurred.

    Now TimeoutException is caught before the generic Exception fallback
    (and ordering relative to the sibling ConnectError branch doesn't
    matter - see test_call_tool_connect_error_returns_friendly_actionable_message
    for proof that branch is untouched), so the friendly message always wins
    regardless of whether the underlying exception carried any text at all.
    """
    mock_api.queue(lambda req: httpx.ReadTimeout("", request=req))
    result = run(server.call_tool("list_library", {}))
    assert as_text(result) == TIMEOUT_MESSAGE


# ---------------------------------------------------------------------------
# Error handling composes through tools that wrap another tool's response
# ---------------------------------------------------------------------------


def test_export_performance_timeseries_context_wraps_underlying_500_cleanly(mock_api):
    # export_performance_timeseries_context calls _get_performance_timeseries
    # directly (not through call_tool), so the HTTPStatusError only gets
    # turned into a clean error once it unwinds up to call_tool's dispatcher.
    mock_api.queue(httpx.Response(500, json={"detail": "boom"}))
    result = run(
        server.call_tool(
            "export_performance_timeseries_context", {"brand_name": "Acme"}
        )
    )
    assert as_text(result) == "Error: API error (500): boom"


def test_export_brain_learnings_context_requires_agent_context(mock_api):
    # If the underlying payload parses as JSON but has no agent_context key,
    # the export tool should still return a clean, single error - not raise
    # a KeyError.
    mock_api.queue(httpx.Response(200, json={"learnings": []}))
    result = run(
        server.call_tool("export_brain_learnings_context", {"brand_name": "Acme"})
    )
    assert as_text(result) == (
        "Error: Brain learnings response did not include agent_context"
    )


# ---------------------------------------------------------------------------
# Freshness stamp: last_synced_at / data_age_hours / stale envelope
# ---------------------------------------------------------------------------


def test_freshness_envelope_computes_age_hours_from_last_synced_at():
    twenty_hours_ago = (
        (datetime.now(timezone.utc) - timedelta(hours=20))
        .isoformat()
        .replace("+00:00", "Z")
    )
    payload = {
        "connected": True,
        "last_synced_at": twenty_hours_ago,
        "stale": True,
        "account_id": "act_1",
    }

    envelope = server._freshness_envelope(payload)

    assert envelope["last_synced_at"] == twenty_hours_ago
    assert envelope["stale"] is True
    assert 19.9 < envelope["data_age_hours"] < 20.1


def test_freshness_envelope_handles_disconnected_null_timestamp():
    payload = {"connected": False, "last_synced_at": None, "stale": True}

    envelope = server._freshness_envelope(payload)

    assert envelope == {"last_synced_at": None, "data_age_hours": None, "stale": True}


def test_freshness_envelope_handles_unparseable_timestamp_without_crashing():
    payload = {"last_synced_at": "not-a-timestamp", "stale": False}

    envelope = server._freshness_envelope(payload)

    assert envelope == {
        "last_synced_at": "not-a-timestamp",
        "data_age_hours": None,
        "stale": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"brand_name": "Acme", "totals": {}},  # no "stale" key at all
        "a plain string response",
        [1, 2, 3],
        None,
    ],
)
def test_freshness_envelope_returns_none_when_signal_is_absent(payload):
    assert server._freshness_envelope(payload) is None


def test_with_freshness_stamp_merges_without_clobbering_other_keys():
    payload = {"brand_name": "Acme", "stale": False, "last_synced_at": "2026-01-01T00:00:00Z"}

    stamped = server._with_freshness_stamp(payload)

    assert stamped["brand_name"] == "Acme"
    assert stamped["stale"] is False
    assert stamped["freshness"]["stale"] is False
    assert stamped["freshness"]["last_synced_at"] == "2026-01-01T00:00:00Z"
    assert isinstance(stamped["freshness"]["data_age_hours"], float)


def test_with_freshness_stamp_is_a_no_op_when_ineligible():
    payload = {"brand_name": "Acme", "totals": {"spend": 100}}

    assert server._with_freshness_stamp(payload) == payload
    assert "freshness" not in server._with_freshness_stamp(payload)
    assert server._with_freshness_stamp("plain text") == "plain text"
    assert server._with_freshness_stamp([1, 2, 3]) == [1, 2, 3]


def test_get_meta_status_response_carries_freshness_envelope(mock_api):
    # Real /auth/meta/status shape: last_synced_at + stale sit at the top
    # level alongside connection metadata.
    stale_at = (
        (datetime.now(timezone.utc) - timedelta(hours=50))
        .isoformat()
        .replace("+00:00", "Z")
    )
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "connected": True,
                "status": "connected",
                "account_id": "act_123",
                "last_synced_at": stale_at,
                "last_sync_status": "success",
                "stale": True,
                "has_performance_rows": True,
            },
        )
    )

    result = run(server.call_tool("get_meta_status", {"brand_name": "Acme"}))
    payload = as_json(result)

    assert payload["freshness"]["last_synced_at"] == stale_at
    assert payload["freshness"]["stale"] is True
    assert 49.9 < payload["freshness"]["data_age_hours"] < 50.1
    # Original top-level fields are untouched, not renamed or removed.
    assert payload["last_synced_at"] == stale_at
    assert payload["account_id"] == "act_123"


def test_get_meta_performance_summary_response_carries_freshness_envelope(mock_api):
    # Real /meta/performance/summary shape: last_synced_at + stale at the top
    # level, plus a nested "sync" block merging the same freshness fields.
    fresh_at = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "brand_name": "Acme",
                "total_rows": 500,
                "totals": {"spend": 10000, "roas": 2.5},
                "last_synced_at": fresh_at,
                "stale": False,
                "sync": {"imported_at": fresh_at, "last_synced_at": fresh_at, "stale": False},
            },
        )
    )

    result = run(
        server.call_tool("get_meta_performance_summary", {"brand_name": "Acme"})
    )
    payload = as_json(result)

    assert payload["freshness"]["stale"] is False
    assert payload["freshness"]["last_synced_at"] == fresh_at
    assert 0.9 < payload["freshness"]["data_age_hours"] < 1.1
    assert payload["totals"]["spend"] == 10000


def test_freshness_stamp_never_applied_to_tools_the_api_has_not_stamped(mock_api):
    # get_taxonomy_performance's underlying API response has no last_synced_at
    # / stale contract today (verified against app/pipeline/tag_performance.py
    # upstream). Even a payload that happens to carry a "stale"-looking key
    # under a DIFFERENT name must not get a freshness envelope invented for
    # it, because that handler never calls the shared helper.
    mock_api.queue(
        httpx.Response(
            200,
            json={
                "brand_name": "Acme",
                "standard_taxonomy": {},
                "coverage_gaps": {},
                "is_stale_looking_but_not_the_real_field": True,
            },
        )
    )

    result = run(server.call_tool("get_taxonomy_performance", {"brand_name": "Acme"}))
    payload = as_json(result)

    assert "freshness" not in payload
