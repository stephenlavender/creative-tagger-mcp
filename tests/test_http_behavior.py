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

import httpx
import pytest

from creative_tagger_mcp import server


def run(coro):
    return asyncio.run(coro)


def as_json(result) -> object:
    assert len(result) == 1
    return json.loads(result[0].text)


def as_text(result) -> str:
    assert len(result) == 1
    return result[0].text


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
    assert "import_meta_performance" in names
    assert "import_competitor_ads" in names


# ---------------------------------------------------------------------------
# call_tool() dispatcher contract
# ---------------------------------------------------------------------------


def test_call_tool_unknown_tool_returns_clean_error(mock_api):
    result = run(server.call_tool("not_a_real_tool", {}))
    assert as_text(result) == "Error: Unknown tool: not_a_real_tool"
    assert mock_api.requests == []


def test_call_tool_blocks_internal_backfill_tool_without_env_flag(
    mock_api, monkeypatch
):
    monkeypatch.delenv("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", raising=False)
    result = run(
        server.call_tool("import_meta_performance", {"rows": [{"ad_id": "1"}]})
    )
    text = as_text(result)
    assert text.startswith("Error: Internal backfill tools are disabled")
    assert mock_api.requests == []  # never touches the network


def test_call_tool_allows_internal_backfill_tool_with_env_flag(mock_api, monkeypatch):
    monkeypatch.setenv("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", "1")
    mock_api.queue(httpx.Response(200, json={"imported": 1}))
    result = run(
        server.call_tool("import_meta_performance", {"rows": [{"ad_id": "1"}]})
    )
    assert as_json(result) == {"imported": 1}
    assert mock_api.last_request.method == "POST"
    assert mock_api.last_request.url.path == "/meta/import"


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


def test_get_tool_with_path_param_builds_url_from_argument(mock_api):
    mock_api.queue(httpx.Response(200, json={"id": 42}))
    run(server._get_analysis({"analysis_id": 42}))

    req = mock_api.last_request
    assert req.method == "GET"
    assert req.url.path == "/auth/library/42"
    assert req.headers.get("x-api-key") == "test-api-key-123"


def test_post_json_tool_sends_header_auth_and_json_body(mock_api):
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
    assert req.method == "POST"
    assert req.url.path == "/auth/brand-context"
    assert req.headers.get("x-api-key") == "test-api-key-123"
    assert req.headers["content-type"] == "application/json"
    assert "api_key" not in mock_api.query_params()
    body = json.loads(req.content)
    assert body["brand_name"] == "Acme"
    assert body["voice"] == "clinical, precise"
    assert body["top_performers"] == ["UGC TalkHead"]


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
    ("list_custom_reports", {"brand_name": "Acme"}, "GET", "/reports/custom/saved"),
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
    ("set_brand_context", {"brand_name": "Acme"}, "POST", "/auth/brand-context"),
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
    assert payload["controlled_dimension_count"] == 16
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


def test_get_taxonomy_unknown_dimension_returns_clean_error(mock_api):
    result = run(server._get_taxonomy({"dimension": "not_a_dimension"}))
    text = as_text(result)
    assert text.startswith("Error: Unknown dimension: not_a_dimension. Available: ")
    assert "hook_type" in text
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
