"""Authentication helpers shared by SDK adapters and runtime services."""

from unstract.sdk1.auth.openai_oauth import (
    OPENAI_OAUTH_ADAPTER_PREFIX,
    OPENAI_OAUTH_CHATGPT_API_BASE,
    OPENAI_OAUTH_CODEX_CLIENT_VERSION,
    OPENAI_OAUTH_MODELS_URL,
    OPENAI_OAUTH_PROVIDER,
    OpenAIOAuthError,
    OpenAIOAuthModelCatalogError,
    OpenAIOAuthRefreshError,
    build_openai_oauth_json_schema,
    extract_account_id,
    extract_email,
    fetch_openai_oauth_model_catalog,
    is_openai_oauth_adapter,
    refresh_openai_oauth_metadata,
)

__all__ = [
    "OPENAI_OAUTH_ADAPTER_PREFIX",
    "OPENAI_OAUTH_CHATGPT_API_BASE",
    "OPENAI_OAUTH_CODEX_CLIENT_VERSION",
    "OPENAI_OAUTH_MODELS_URL",
    "OPENAI_OAUTH_PROVIDER",
    "OpenAIOAuthError",
    "OpenAIOAuthModelCatalogError",
    "OpenAIOAuthRefreshError",
    "build_openai_oauth_json_schema",
    "extract_account_id",
    "extract_email",
    "fetch_openai_oauth_model_catalog",
    "is_openai_oauth_adapter",
    "refresh_openai_oauth_metadata",
]
