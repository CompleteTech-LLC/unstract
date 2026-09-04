"""OpenAI ChatGPT OAuth LLM adapter.

This adapter deliberately has a separate id and provider from the API-key
OpenAI adapter.  OAuth credentials are injected by the authenticated web/API
flow and are never part of the configuration form.
"""

from typing import Any

from unstract.sdk1.adapters.base1 import BaseAdapter, BaseChatCompletionParameters
from unstract.sdk1.adapters.enums import AdapterTypes
from unstract.sdk1.auth.openai_oauth import (
    OPENAI_OAUTH_CHATGPT_API_BASE,
    OPENAI_OAUTH_PROVIDER,
)


class OpenAIOAuthLLMParameters(BaseChatCompletionParameters):
    """Validated per-adapter credentials for the ChatGPT Responses API."""

    oauth_access_token: str
    oauth_refresh_token: str
    oauth_id_token: str | None = None
    oauth_account_id: str
    oauth_account_email: str | None = None
    oauth_expires_at: float | int | None = None
    # This is fixed by the adapter.  It is not exposed in the JSON schema.
    api_base: str = OPENAI_OAUTH_CHATGPT_API_BASE
    reasoning_effort: str | None = None

    @staticmethod
    def validate(adapter_metadata: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(adapter_metadata)
        model = str(metadata.get("model", "")).strip()
        for prefix in ("openai/", "chatgpt/"):
            if model.startswith(prefix):
                model = model[len(prefix) :]
                break
        for field in ("oauth_access_token", "oauth_refresh_token", "oauth_account_id"):
            if not isinstance(metadata.get(field), str) or not metadata[field].strip():
                raise ValueError(f"Missing required OpenAI OAuth field: {field}")
        if not model:
            raise ValueError("Missing required OpenAI OAuth field: model")
        metadata["model"] = model
        metadata["api_base"] = OPENAI_OAUTH_CHATGPT_API_BASE

        validated = OpenAIOAuthLLMParameters(**metadata).model_dump()
        # The LLM wrapper uses this for usage/cost bookkeeping; it is not sent
        # as a provider parameter.
        validated["cost_model"] = model
        return validated

    @staticmethod
    def validate_model(adapter_metadata: dict[str, Any]) -> str:
        model = str(adapter_metadata.get("model", "")).strip()
        return model.removeprefix("openai/").removeprefix("chatgpt/")


class OpenAIOAuthLLMAdapter(OpenAIOAuthLLMParameters, BaseAdapter):
    """OpenAI LLM backed by a user-authorized ChatGPT account."""

    @staticmethod
    def get_id() -> str:
        return "openai-oauth|a5ce9b7d-5f8a-4c6d-8a65-6e1f626b2b6e"

    @staticmethod
    def get_metadata() -> dict[str, Any]:
        return {
            "name": "OpenAI (OAuth)",
            "version": "1.0.0",
            "adapter": OpenAIOAuthLLMAdapter,
            "description": "OpenAI LLM adapter authenticated with ChatGPT OAuth",
            "is_active": True,
        }

    @classmethod
    def get_auth_metadata(cls) -> dict[str, Any]:
        return {
            "oauth": True,
            "oauth_provider": "openai",
            "python_social_auth_backend": OPENAI_OAUTH_PROVIDER,
        }

    @staticmethod
    def get_name() -> str:
        return "OpenAI (OAuth)"

    @staticmethod
    def get_description() -> str:
        return "OpenAI LLM adapter authenticated with ChatGPT OAuth"

    @staticmethod
    def get_provider() -> str:
        return OPENAI_OAUTH_PROVIDER

    @staticmethod
    def get_icon() -> str:
        return "/icons/adapter-icons/OpenAI.png"

    @staticmethod
    def get_doc_url() -> str:
        return "https://developers.openai.com/codex/auth/"

    @staticmethod
    def get_adapter_type() -> AdapterTypes:
        return AdapterTypes.LLM
