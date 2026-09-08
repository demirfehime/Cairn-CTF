from __future__ import annotations

import pytest
from fastapi import HTTPException

from cairn.server.models import DiscoverAgentModelsRequest
from cairn.server.routers import agents as agents_router
from cairn.server.routers.agents import _extract_model_names, _model_endpoint_url


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"data": [{"id": " model-b "}, {"id": "model-a"}, "model-a"]},
            ["model-a", "model-b"],
        ),
        (
            {"models": [{"name": "claude-opus"}, {"model": "qwen"}]},
            ["claude-opus", "qwen"],
        ),
        (["top-level", {"model_name": "nested"}], ["nested", "top-level"]),
        (
            {"data": {"models": [{"display_name": "wrapped"}]}},
            ["wrapped"],
        ),
        (
            {"models": {"mapped-a": {}, "mapped-b": {"metadata": "ignored"}}},
            ["mapped-a", "mapped-b"],
        ),
        (
            {"data": [], "models": [{"id": "fallback"}]},
            ["fallback"],
        ),
        ({"name": "list", "data": [{"modelId": "preferred"}]}, ["preferred"]),
        ({"models": {"error": "unauthorized", "message": "try again"}}, []),
    ],
)
def test_extract_model_names_supports_common_provider_shapes(payload, expected) -> None:
    assert _extract_model_names(payload) == expected


def test_model_endpoint_url_does_not_append_models_twice() -> None:
    assert _model_endpoint_url("https://api.example.test/v1/") == "https://api.example.test/v1/models"
    assert _model_endpoint_url("https://api.example.test/v1/models/") == "https://api.example.test/v1/models"


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def test_discover_models_reads_openai_data(monkeypatch) -> None:
    def fake_get(url, headers, timeout):
        return _FakeResponse(200, {"data": [{"id": "live-model"}]})

    monkeypatch.setattr(agents_router.requests, "get", fake_get)
    result = agents_router.discover_models(
        DiscoverAgentModelsRequest(base_url="https://api.example.test/v1", api_key="test-key")
    )
    assert result.models == ["live-model"]


def test_discover_models_reports_upstream_error_after_html_fallback(monkeypatch) -> None:
    def fake_get(url, headers, timeout):
        if url.endswith("/v1/models"):
            return _FakeResponse(401, {"error": "invalid token"}, '{"error":"invalid token"}')
        return _FakeResponse(200, None, "<html>gateway page</html>")

    monkeypatch.setattr(agents_router.requests, "get", fake_get)
    with pytest.raises(HTTPException) as exc_info:
        agents_router.discover_models(
            DiscoverAgentModelsRequest(base_url="https://api.example.test/v1", api_key="test-key")
        )
    assert exc_info.value.status_code == 401
    assert "Model endpoint returned 401" in str(exc_info.value.detail)


def test_discover_models_does_not_retry_openai_auth_failure(monkeypatch) -> None:
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return _FakeResponse(401, {"error": "invalid token"}, '{"error":"invalid token"}')

    monkeypatch.setattr(agents_router.requests, "get", fake_get)
    with pytest.raises(HTTPException) as exc_info:
        agents_router.discover_models(
            DiscoverAgentModelsRequest(base_url="https://api.example.test/v1", api_key="test-key")
        )
    assert exc_info.value.status_code == 401
    assert len(calls) == 1
    assert calls[0][2] == (5, 10)
