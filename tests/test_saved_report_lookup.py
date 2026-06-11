from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

httpx_stub = ModuleType("httpx")
httpx_stub.AsyncClient = object


class _HTTPStatusError(Exception):
    pass


class _ConnectError(Exception):
    pass


httpx_stub.HTTPStatusError = _HTTPStatusError
httpx_stub.ConnectError = _ConnectError
sys.modules.setdefault("httpx", httpx_stub)

mcp_stub = ModuleType("mcp")
mcp_server_stub = ModuleType("mcp.server")
mcp_stdio_stub = ModuleType("mcp.server.stdio")
mcp_types_stub = ModuleType("mcp.types")


class _Server:
    def __init__(self, name):
        self.name = name

    def list_tools(self):
        def decorator(fn):
            return fn

        return decorator

    def call_tool(self):
        def decorator(fn):
            return fn

        return decorator


class _TextContent:
    def __init__(self, *, type, text):
        self.type = type
        self.text = text


class _Tool(SimpleNamespace):
    pass


async def _stdio_server():
    raise NotImplementedError


mcp_server_stub.Server = _Server
mcp_stdio_stub.stdio_server = _stdio_server
mcp_types_stub.TextContent = _TextContent
mcp_types_stub.Tool = _Tool
sys.modules.setdefault("mcp", mcp_stub)
sys.modules.setdefault("mcp.server", mcp_server_stub)
sys.modules.setdefault("mcp.server.stdio", mcp_stdio_stub)
sys.modules.setdefault("mcp.types", mcp_types_stub)

from creative_tagger_mcp import server


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        return self._responses.pop(0)


class SavedReportLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_saved_report_id_prefers_direct_id(self) -> None:
        report_id = await server._resolve_saved_report_id({"report_id": "7"})

        self.assertEqual(report_id, 7)

    async def test_resolve_saved_report_id_matches_exact_name(self) -> None:
        responses = [_FakeResponse([{"id": 9, "name": "Hook + LP + Offer"}])]

        with patch.object(
            server.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _FakeAsyncClient(responses=responses),
        ):
            report_id = await server._resolve_saved_report_id(
                {"brand_name": "Acme", "name": "Hook + LP + Offer"}
            )

        self.assertEqual(report_id, 9)

    async def test_resolve_saved_report_id_matches_case_insensitive_name(self) -> None:
        responses = [_FakeResponse({"reports": [{"id": 12, "name": "Hook + LP + Offer"}]})]

        with patch.object(
            server.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _FakeAsyncClient(responses=responses),
        ):
            report_id = await server._resolve_saved_report_id(
                {"brand_name": "Acme", "name": "hook + lp + offer"}
            )

        self.assertEqual(report_id, 12)

    async def test_resolve_saved_report_id_returns_available_names_on_miss(self) -> None:
        responses = [_FakeResponse([{"id": 4, "name": "Best Hooks"}])]

        with patch.object(
            server.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: _FakeAsyncClient(responses=responses),
        ):
            payload = await server._resolve_saved_report_id(
                {"brand_name": "Acme", "name": "Missing"}
            )

        self.assertEqual(len(payload), 1)
        self.assertIn("Available: Best Hooks", payload[0].text)
