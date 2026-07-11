"""Shared fixtures for HTTP-behavior tests.

These patch ``httpx.AsyncClient`` so the *real* async tool functions in
``creative_tagger_mcp.server`` run against a local, in-process mock transport
instead of the network. No server process, no sockets, no live API — but the
exact same client construction, header/param building, and response-parsing
code paths the shipped server uses.
"""

from __future__ import annotations

import collections
from typing import Any, Callable, Union

import httpx
import pytest

from creative_tagger_mcp import server

Responder = Callable[[httpx.Request], httpx.Response]
QueuedItem = Union[httpx.Response, BaseException, Responder]


class MockAPI:
    """Records every outgoing request and serves scripted responses in order."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._queue: collections.deque[QueuedItem] = collections.deque()
        self._default: Responder | None = None

    def queue(self, item: QueuedItem) -> None:
        """Queue one response, exception, or responder for the next request."""
        self._queue.append(item)

    def default(self, responder: Responder) -> None:
        """Set a fallback responder used once the queue is empty."""
        self._default = responder

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        # Fully drain the request body (needed to inspect JSON/form/multipart
        # bodies below) before recording it.
        await request.aread()
        self.requests.append(request)

        if self._queue:
            outcome: QueuedItem = self._queue.popleft()
        elif self._default is not None:
            outcome = self._default
        else:
            raise AssertionError(
                f"No mock response queued for {request.method} {request.url}"
            )

        if isinstance(outcome, httpx.Response):
            return outcome
        if isinstance(outcome, BaseException):
            raise outcome
        # It's a responder callable.
        result = outcome(request)
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def last_request(self) -> httpx.Request:
        return self.requests[-1]

    def query_params(self, index: int = -1) -> dict[str, Any]:
        return dict(httpx.QueryParams(self.requests[index].url.query))


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> MockAPI:
    """Patch httpx.AsyncClient so every ``async with httpx.AsyncClient(...)``
    inside server.py is transparently backed by an in-memory MockTransport.

    Also points the module at a fake API URL/key so tests never touch the
    real network and never depend on ambient env vars.
    """
    api = MockAPI()
    transport = httpx.MockTransport(api._handle)
    real_async_client = httpx.AsyncClient

    class PatchedAsyncClient(real_async_client):  # type: ignore[misc,valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedAsyncClient)
    monkeypatch.setattr(server, "API_URL", "http://mock.local")
    monkeypatch.setattr(server, "API_KEY", "test-api-key-123")
    return api


@pytest.fixture
def mock_api_no_key(mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch) -> MockAPI:
    """Same as mock_api, but with no API key configured (anonymous client)."""
    monkeypatch.setattr(server, "API_KEY", "")
    return mock_api
