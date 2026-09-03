"""Tests for the per-account OpenAI ChatGPT OAuth adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unstract.sdk1.adapters.constants import Common
from unstract.sdk1.adapters.llm1 import adapters
from unstract.sdk1.adapters.llm1.openai_oauth import (
    OpenAIOAuthLLMAdapter,
    OpenAIOAuthLLMParameters,
)
from unstract.sdk1.auth.openai_oauth import (
    OPENAI_OAUTH_CHATGPT_API_BASE,
    OPENAI_OAUTH_CODEX_CLIENT_VERSION,
    OPENAI_OAUTH_MODELS_URL,
    OPENAI_OAUTH_TOKEN_URL,
    build_openai_oauth_json_schema,
    extract_account_id,
    extract_email,
    fetch_openai_oauth_model_catalog,
    refresh_openai_oauth_metadata,
)
from unstract.sdk1.llm import LLM


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _metadata(access_token: str, account_id: str) -> dict[str, object]:
    return {
        "model": "gpt-5-codex",
        "oauth_access_token": access_token,
        "oauth_refresh_token": f"refresh-{account_id}",
        "oauth_id_token": "id-token",
        "oauth_account_id": account_id,
        "oauth_account_email": f"{account_id}@example.test",
        "oauth_expires_at": 4_000_000_000,
    }


def test_openai_oauth_adapter_is_registered_with_auth_metadata() -> None:
    adapter_id = OpenAIOAuthLLMAdapter.get_id()
    assert adapters[adapter_id][Common.MODULE] is OpenAIOAuthLLMAdapter

    registry_metadata = adapters[adapter_id][Common.METADATA]
    assert registry_metadata[Common.ADAPTER] is OpenAIOAuthLLMAdapter
    assert OpenAIOAuthLLMAdapter.get_auth_metadata() == {
        "oauth": True,
        "oauth_provider": "openai",
        "python_social_auth_backend": "openai-oauth",
    }
    schema = json.loads(OpenAIOAuthLLMAdapter.get_json_schema())
    assert "enum" not in schema["properties"]["model"]
    assert "enum" not in schema["allOf"][0]["then"]["properties"]["reasoning_effort"]


def test_openai_oauth_model_catalog_is_normalized_per_account() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "models": [
            {
                "slug": "hidden-model",
                "display_name": "Hidden",
                "visibility": "hidden",
            },
            {
                "slug": "gpt-account-fast",
                "display_name": "Account Fast",
                "description": "Fast account model",
                "priority": 2,
                "default_reasoning_level": "low",
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Light"},
                    {"effort": "high", "description": "Deep"},
                ],
            },
            {
                "slug": "gpt-account-best",
                "display_name": "Account Best",
                "priority": 1,
                "supported_in_api": True,
                "supported_reasoning_levels": [{"effort": "max"}],
            },
            {"slug": "not-supported", "supported_in_api": False},
        ]
    }
    metadata = _metadata("account-token", "workspace-123")

    with patch("unstract.sdk1.auth.openai_oauth.httpx.get", return_value=response) as get:
        catalog = fetch_openai_oauth_model_catalog(metadata)

    assert [model["slug"] for model in catalog] == [
        "gpt-account-best",
        "gpt-account-fast",
    ]
    assert catalog[1]["supported_reasoning_levels"] == [
        {"effort": "low", "description": "Light"},
        {"effort": "high", "description": "Deep"},
    ]
    get.assert_called_once_with(
        OPENAI_OAUTH_MODELS_URL,
        params={"client_version": OPENAI_OAUTH_CODEX_CLIENT_VERSION},
        headers={
            "Authorization": "Bearer account-token",
            "ChatGPT-Account-ID": "workspace-123",
            "Accept": "application/json",
            "originator": "unstract",
        },
        timeout=15.0,
    )


def test_openai_oauth_dynamic_schema_uses_model_specific_reasoning_levels() -> None:
    base_schema = json.loads(OpenAIOAuthLLMAdapter.get_json_schema())
    schema = build_openai_oauth_json_schema(
        [
            {
                "slug": "account-model-a",
                "display_name": "Model A",
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Low"},
                    {"effort": "high", "description": "High"},
                ],
            },
            {
                "slug": "account-model-b",
                "display_name": "Model B",
                "supported_reasoning_levels": [
                    {"effort": "max", "description": "Maximum"},
                ],
            },
        ],
        current_model="account-model-b",
        base_schema=base_schema,
    )

    model = schema["properties"]["model"]
    assert model["enum"] == ["account-model-a", "account-model-b"]
    assert model["enumNames"] == ["Model A", "Model B"]
    assert model["default"] == "account-model-b"
    model_conditions = schema["allOf"][2:]
    assert model_conditions[0]["if"]["properties"]["model"] == {
        "const": "account-model-a"
    }
    assert model_conditions[0]["then"]["properties"]["reasoning_effort"]["enum"] == [
        "low",
        "high",
    ]
    assert model_conditions[1]["then"]["properties"]["reasoning_effort"]["enum"] == [
        "max"
    ]


def test_openai_oauth_parameters_normalize_model_and_fix_endpoint() -> None:
    metadata = _metadata("access", "account")
    metadata["model"] = "openai/gpt-5-codex"

    validated = OpenAIOAuthLLMParameters.validate(metadata)

    assert validated["model"] == "gpt-5-codex"
    assert validated["api_base"] == OPENAI_OAUTH_CHATGPT_API_BASE
    assert validated["cost_model"] == "gpt-5-codex"
    assert metadata["model"] == "openai/gpt-5-codex"


@pytest.mark.parametrize(
    "field",
    ["oauth_access_token", "oauth_refresh_token", "oauth_account_id"],
)
def test_openai_oauth_parameters_require_account_credentials(field: str) -> None:
    metadata = _metadata("access", "account")
    metadata[field] = "  "

    with pytest.raises(ValueError, match=field):
        OpenAIOAuthLLMParameters.validate(metadata)


def test_openai_oauth_claim_helpers_read_account_and_email() -> None:
    token = _jwt(
        {
            "chatgpt_account_id": "workspace-123",
            "email": "person@example.test",
        }
    )

    assert extract_account_id(token) == "workspace-123"
    assert extract_email(token) == "person@example.test"


def test_refresh_openai_oauth_metadata_is_scoped_to_one_account() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"access_token": "new-access", "expires_in": 3600}
    metadata = _metadata("old-access", "workspace-123")
    metadata["oauth_expires_at"] = 0

    with patch(
        "unstract.sdk1.auth.openai_oauth.httpx.post", return_value=response
    ) as post:
        refreshed = refresh_openai_oauth_metadata(metadata, now=100, force=True)

    assert refreshed["oauth_access_token"] == "new-access"
    assert refreshed["oauth_refresh_token"] == "refresh-workspace-123"
    assert refreshed["oauth_account_id"] == "workspace-123"
    post.assert_called_once_with(
        OPENAI_OAUTH_TOKEN_URL,
        json={
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            "grant_type": "refresh_token",
            "refresh_token": "refresh-workspace-123",
        },
        timeout=15.0,
    )


def test_llm_sends_each_account_token_and_workspace_header() -> None:
    first = LLM(
        adapter_id=OpenAIOAuthLLMAdapter.get_id(),
        adapter_metadata=_metadata("token-a", "account-a"),
    )
    second = LLM(
        adapter_id=OpenAIOAuthLLMAdapter.get_id(),
        adapter_metadata=_metadata("token-b", "account-b"),
    )

    with (
        patch(
            "unstract.sdk1.llm.litellm.responses",
            side_effect=[
                {"output_text": "first", "usage": {}},
                {"output_text": "second", "usage": {}},
            ],
        ) as responses,
        patch.object(first, "_record_usage"),
        patch.object(second, "_record_usage"),
    ):
        assert first.complete("hello")["response"].text == "first"
        assert second.complete("hello")["response"].text == "second"

    first_request = responses.call_args_list[0].kwargs
    second_request = responses.call_args_list[1].kwargs
    assert first_request["api_key"] == "token-a"
    assert second_request["api_key"] == "token-b"
    assert first_request["extra_headers"]["ChatGPT-Account-Id"] == "account-a"
    assert second_request["extra_headers"]["ChatGPT-Account-Id"] == "account-b"
    assert first_request["extra_headers"]["Authorization"] == "Bearer token-a"
    assert second_request["extra_headers"]["Authorization"] == "Bearer token-b"
    assert first_request["extra_headers"]["session-id"]
    assert first_request["include"] == ["reasoning.encrypted_content"]
    assert "oauth_refresh_token" not in first_request
    assert first_request["api_base"] == OPENAI_OAUTH_CHATGPT_API_BASE


def test_llm_streams_responses_text_and_records_completion_usage() -> None:
    llm = LLM(
        adapter_id=OpenAIOAuthLLMAdapter.get_id(),
        adapter_metadata=_metadata("token", "account"),
    )
    events = iter(
        [
            {"type": "response.output_text.delta", "delta": "hello"},
            {
                "type": "response.completed",
                "response": {
                    "id": "response-1",
                    "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                },
            },
        ]
    )

    with (
        patch("unstract.sdk1.llm.litellm.responses", return_value=events),
        patch.object(llm, "_record_usage") as record_usage,
    ):
        chunks = list(llm.stream_complete("hello"))

    assert [chunk.text for chunk in chunks] == ["hello"]
    record_usage.assert_called_once()
    assert record_usage.call_args.args[2] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
    }


def test_llm_async_completion_uses_responses_api() -> None:
    llm = LLM(
        adapter_id=OpenAIOAuthLLMAdapter.get_id(),
        adapter_metadata=_metadata("token", "account"),
    )
    response = {"output_text": "async response", "usage": {}}

    with (
        patch(
            "unstract.sdk1.llm.litellm.aresponses",
            new=AsyncMock(return_value=response),
        ) as responses,
        patch.object(llm, "_record_usage"),
    ):
        result = asyncio.run(llm.acomplete("hello"))

    assert result["response"].text == "async response"
    assert responses.await_args.kwargs["extra_headers"]["ChatGPT-Account-Id"] == "account"
