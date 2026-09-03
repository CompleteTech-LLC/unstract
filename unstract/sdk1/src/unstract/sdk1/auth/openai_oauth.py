"""Small, provider-specific helpers for OpenAI ChatGPT OAuth credentials.

The browser/device login belongs to the web application.  This module stays
free of Django and Flask so the platform service can refresh credentials for an
individual adapter instance without keeping a process-global account.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

OPENAI_OAUTH_PROVIDER = "openai-oauth"
OPENAI_OAUTH_ADAPTER_PREFIX = f"{OPENAI_OAUTH_PROVIDER}|"
OPENAI_OAUTH_AUTH_BASE = os.environ.get(
    "OPENAI_OAUTH_AUTH_BASE", "https://auth.openai.com"
).rstrip("/")
OPENAI_OAUTH_CLIENT_ID = os.environ.get(
    "OPENAI_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"
)
OPENAI_OAUTH_CHATGPT_API_BASE = "https://chatgpt.com/backend-api/codex"
OPENAI_OAUTH_MODELS_URL = f"{OPENAI_OAUTH_CHATGPT_API_BASE}/models"
OPENAI_OAUTH_CODEX_CLIENT_VERSION = os.environ.get(
    "OPENAI_OAUTH_CODEX_CLIENT_VERSION", "0.149.0"
)
OPENAI_OAUTH_DEVICE_USERCODE_URL = (
    f"{OPENAI_OAUTH_AUTH_BASE}/api/accounts/deviceauth/usercode"
)
OPENAI_OAUTH_DEVICE_TOKEN_URL = (
    f"{OPENAI_OAUTH_AUTH_BASE}/api/accounts/deviceauth/token"
)
OPENAI_OAUTH_TOKEN_URL = f"{OPENAI_OAUTH_AUTH_BASE}/oauth/token"
OPENAI_OAUTH_DEVICE_VERIFICATION_URL = f"{OPENAI_OAUTH_AUTH_BASE}/codex/device"
OPENAI_OAUTH_DEVICE_REDIRECT_URI = f"{OPENAI_OAUTH_AUTH_BASE}/deviceauth/callback"

# OAuth metadata names are intentionally namespaced.  It prevents a normal
# OpenAI API-key adapter from accidentally being treated as a ChatGPT OAuth
# adapter, and makes redaction easy at the API boundary.
OPENAI_OAUTH_SECRET_FIELDS = frozenset(
    {
        "oauth_access_token",
        "oauth_refresh_token",
        "oauth_id_token",
    }
)
OPENAI_OAUTH_PRIVATE_FIELDS = frozenset(
    {
        *OPENAI_OAUTH_SECRET_FIELDS,
        "oauth_account_id",
        "oauth_account_email",
        "oauth_expires_at",
    }
)


class OpenAIOAuthError(RuntimeError):
    """Base error for OpenAI OAuth credential handling."""


class OpenAIOAuthRefreshError(OpenAIOAuthError):
    """Raised when an expired access token cannot be refreshed."""


class OpenAIOAuthModelCatalogError(OpenAIOAuthError):
    """Raised when the account-specific Codex model catalog cannot be read."""


def is_openai_oauth_adapter(adapter_id: str | None) -> bool:
    """Return whether ``adapter_id`` identifies the OAuth-backed adapter."""
    return bool(adapter_id and adapter_id.startswith(OPENAI_OAUTH_ADAPTER_PREFIX))


def _decode_jwt_claims(token: str | None) -> dict[str, Any]:
    """Decode the unverified JWT payload for routing metadata only.

    The authorization server has already issued the token and the provider
    validates it on every model request.  We only use claims to find the
    account label and expiry; this function is not an authentication check.
    """
    if not token or not isinstance(token, str):
        return {}
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _nested_claim(claims: Mapping[str, Any], key: str) -> object | None:
    """Read a claim from the common OpenAI auth namespace as a fallback."""
    for namespace in (
        "https://api.openai.com/auth",
        "https://auth.openai.com/auth",
        "auth",
    ):
        auth_claims = claims.get(namespace)
        if isinstance(auth_claims, Mapping) and auth_claims.get(key):
            return auth_claims[key]
    return None


def extract_account_id(*tokens: str | None) -> str | None:
    """Extract the ChatGPT account/workspace id from issued JWT claims."""
    for token in tokens:
        claims = _decode_jwt_claims(token)
        account_id = claims.get("chatgpt_account_id") or claims.get("account_id")
        if not account_id:
            account_id = _nested_claim(claims, "chatgpt_account_id")
        if isinstance(account_id, str) and account_id.strip():
            return account_id.strip()
    return None


def extract_email(*tokens: str | None) -> str | None:
    """Extract an optional account email used only as a friendly label."""
    for token in tokens:
        claims = _decode_jwt_claims(token)
        email = claims.get("email")
        if not email and isinstance(claims.get("profile"), Mapping):
            email = claims["profile"].get("email")
        if not email:
            email = _nested_claim(claims, "email")
        if isinstance(email, str) and email.strip():
            return email.strip()
    return None


def _extract_expiry(*tokens: str | None) -> float | None:
    for token in tokens:
        expiry = _decode_jwt_claims(token).get("exp")
        try:
            if expiry is not None:
                return float(expiry)
        except (TypeError, ValueError):
            continue
    return None


def _response_error(response: httpx.Response, operation: str) -> OpenAIOAuthRefreshError:
    """Build a safe refresh error without including response/token contents."""
    return OpenAIOAuthRefreshError(
        f"OpenAI OAuth {operation} failed with status {response.status_code}"
    )


def _reasoning_levels(value: object) -> list[dict[str, str]]:
    """Normalize the reasoning options returned by the Codex catalog."""
    if not isinstance(value, list):
        return []

    levels: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        effort = item.get("effort") or item.get("reasoning_effort")
        if not isinstance(effort, str) or not effort.strip():
            continue
        effort = effort.strip()
        if effort in seen:
            continue
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            description = effort.replace("_", " ").replace("-", " ").title()
        levels.append({"effort": effort, "description": description.strip()})
        seen.add(effort)
    return levels


def _normalize_catalog_model(
    item: Mapping[str, Any], index: int
) -> dict[str, Any] | None:
    slug = item.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        return None
    slug = slug.strip()

    visibility = item.get("visibility")
    if isinstance(visibility, str) and visibility.lower() in {
        "hidden",
        "none",
        "unlisted",
    }:
        return None
    if item.get("supported_in_api") is False:
        return None

    display_name = item.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = slug
    description = item.get("description")
    if not isinstance(description, str):
        description = ""
    priority = item.get("priority")
    try:
        normalized_priority = int(priority)
    except (TypeError, ValueError):
        normalized_priority = 2**31 - 1

    return {
        "slug": slug,
        "display_name": display_name.strip(),
        "description": description.strip(),
        "default_reasoning_level": item.get("default_reasoning_level"),
        "supported_reasoning_levels": _reasoning_levels(
            item.get("supported_reasoning_levels")
        ),
        "priority": normalized_priority,
        "_catalog_index": index,
        "is_deprecated": bool(item.get("upgrade")),
    }


def _normalize_catalog_models(raw_models: list[object]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for index, item in enumerate(raw_models):
        if not isinstance(item, Mapping):
            continue
        model = _normalize_catalog_model(item, index)
        if model is None or model["slug"] in seen_slugs:
            continue
        models.append(model)
        seen_slugs.add(model["slug"])

    models.sort(key=lambda item: (item["priority"], item["_catalog_index"]))
    for item in models:
        item.pop("_catalog_index", None)
    return models


def _catalog_models_from_response(response: httpx.Response) -> list[object]:
    if not 200 <= response.status_code < 300:
        raise OpenAIOAuthModelCatalogError(
            f"OpenAI OAuth model discovery failed with status {response.status_code}"
        )

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise OpenAIOAuthModelCatalogError(
            "OpenAI OAuth model discovery returned an invalid response"
        ) from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("models"), list):
        raise OpenAIOAuthModelCatalogError(
            "OpenAI OAuth model discovery returned no model catalog"
        )
    return payload["models"]


def fetch_openai_oauth_model_catalog(
    metadata: Mapping[str, Any],
    *,
    client_version: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch the visible model catalog for one authenticated ChatGPT account.

    The Codex endpoint is account- and plan-aware.  No model ids or reasoning
    levels are defined here; the response is normalized only enough for the
    configuration UI to consume it safely.
    """
    access_token = metadata.get("oauth_access_token")
    account_id = metadata.get("oauth_account_id")
    if not isinstance(access_token, str) or not access_token.strip():
        raise OpenAIOAuthModelCatalogError(
            "OpenAI OAuth model discovery requires an access token"
        )
    if not isinstance(account_id, str) or not account_id.strip():
        raise OpenAIOAuthModelCatalogError(
            "OpenAI OAuth model discovery requires a ChatGPT account"
        )

    try:
        response = httpx.get(
            OPENAI_OAUTH_MODELS_URL,
            params={
                "client_version": client_version or OPENAI_OAUTH_CODEX_CLIENT_VERSION
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "ChatGPT-Account-ID": account_id,
                "Accept": "application/json",
                # Keep the catalog aligned with the originator used for model
                # requests by the Unstract adapter.
                "originator": "unstract",
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise OpenAIOAuthModelCatalogError(
            "OpenAI OAuth model discovery could not reach ChatGPT"
        ) from exc

    models = _normalize_catalog_models(_catalog_models_from_response(response))

    if not models:
        raise OpenAIOAuthModelCatalogError(
            "OpenAI OAuth model discovery returned no available models"
        )
    return models


def _valid_catalog_models(
    model_catalog: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in model_catalog
        if isinstance(item, Mapping)
        and isinstance(item.get("slug"), str)
        and item["slug"].strip()
    ]


def _model_labels(models: Sequence[Mapping[str, Any]]) -> list[str]:
    labels: list[str] = []
    for model in models:
        slug = str(model["slug"]).strip()
        label = str(model.get("display_name") or slug).strip()
        if model.get("is_deprecated") and "deprecated" not in label.lower():
            label = f"{label} (deprecated)"
        labels.append(label)
    return labels


def _selected_catalog_model(slugs: Sequence[str], current_model: str | None) -> str:
    selected_model = current_model.strip() if isinstance(current_model, str) else ""
    for prefix in ("openai/", "chatgpt/"):
        if selected_model.startswith(prefix):
            selected_model = selected_model[len(prefix) :]
            break
    return selected_model if selected_model in slugs else slugs[0]


def _reasoning_schema_for_model(
    model: Mapping[str, Any],
) -> dict[str, Any] | None:
    levels_value = model.get("supported_reasoning_levels")
    if not isinstance(levels_value, list):
        return None
    levels = [
        dict(level)
        for level in levels_value
        if isinstance(level, Mapping)
        and isinstance(level.get("effort"), str)
        and level["effort"].strip()
    ]
    if not levels:
        return None

    efforts = [str(level["effort"]).strip() for level in levels]
    effort_labels = [
        str(level.get("description") or effort).strip()
        for level, effort in zip(levels, efforts, strict=True)
    ]
    default_effort = model.get("default_reasoning_level")
    if default_effort not in efforts:
        default_effort = efforts[0]
    return {
        "type": "string",
        "enum": efforts,
        "enumNames": effort_labels,
        "default": default_effort,
        "title": "Reasoning Effort",
        "description": "Reasoning levels reported for this model by ChatGPT.",
    }


def _add_reasoning_conditions(
    all_of: list[dict[str, Any]], models: Sequence[Mapping[str, Any]]
) -> None:
    for model in models:
        slug = str(model["slug"]).strip()
        reasoning_schema = _reasoning_schema_for_model(model)
        if reasoning_schema is None:
            continue
        all_of.append(
            {
                "if": {
                    "required": ["enable_reasoning", "model"],
                    "properties": {
                        "enable_reasoning": {"const": True},
                        "model": {"const": slug},
                    },
                },
                "then": {
                    "properties": {"reasoning_effort": reasoning_schema},
                    "required": ["reasoning_effort"],
                },
            }
        )


def _remove_empty_reasoning_placeholder(all_of: list[dict[str, Any]]) -> None:
    """Remove the pre-auth reasoning rule before adding live model rules."""
    all_of[:] = [
        condition
        for condition in all_of
        if not (
            isinstance(condition, Mapping)
            and isinstance(condition.get("then"), Mapping)
            and isinstance(condition["then"].get("properties"), Mapping)
            and isinstance(
                condition["then"]["properties"].get("reasoning_effort"),
                Mapping,
            )
            and condition["then"]["properties"]["reasoning_effort"].get(
                "enum"
            )
            == []
        )
    ]


def build_openai_oauth_json_schema(
    model_catalog: Sequence[Mapping[str, Any]],
    *,
    current_model: str | None = None,
    base_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a form schema from one account's live Codex model catalog.

    ``base_schema`` is injectable for tests.  In production the adapter's
    schema is loaded lazily to avoid an import cycle with this auth module.
    """
    if base_schema is None:
        from unstract.sdk1.adapters.llm1.openai_oauth import OpenAIOAuthLLMAdapter

        base_schema = json.loads(OpenAIOAuthLLMAdapter.get_json_schema())

    schema = json.loads(json.dumps(base_schema))
    models = _valid_catalog_models(model_catalog)
    if not models:
        raise OpenAIOAuthModelCatalogError(
            "Cannot build an OpenAI OAuth form without available models"
        )

    model_property = schema.setdefault("properties", {}).setdefault("model", {})
    slugs = [str(item["slug"]).strip() for item in models]
    labels = _model_labels(models)
    selected_model = _selected_catalog_model(slugs, current_model)

    model_property["enum"] = slugs
    model_property["enumNames"] = labels
    model_property["default"] = selected_model
    model_property["description"] = (
        "Models reported as available by this ChatGPT/Codex account."
    )

    all_of = schema.setdefault("allOf", [])
    if not isinstance(all_of, list):
        all_of = []
        schema["allOf"] = all_of

    # The static adapter schema keeps an empty reasoning field so RJSF can
    # render the form before login. Once the account catalog is available,
    # that placeholder must not remain as an additional empty constraint.
    _remove_empty_reasoning_placeholder(all_of)
    _add_reasoning_conditions(all_of, models)

    schema["x-openai-oauth-model-source"] = "chatgpt-account"
    return schema


def refresh_openai_oauth_metadata(
    metadata: Mapping[str, Any],
    *,
    force: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Refresh one adapter's OAuth metadata when its access token is expiring.

    The returned dictionary is a copy.  The old refresh token is retained when
    the token endpoint does not rotate it, which is permitted by OAuth 2.0.
    Callers decide where the refreshed copy is persisted; no account is stored
    in module-level state.
    """
    refreshed = dict(metadata)
    access_token = refreshed.get("oauth_access_token")
    refresh_token = refreshed.get("oauth_refresh_token")
    account_id = refreshed.get("oauth_account_id") or extract_account_id(
        refreshed.get("oauth_id_token"), access_token
    )
    if not access_token or not refresh_token or not account_id:
        raise OpenAIOAuthRefreshError(
            "OpenAI OAuth metadata is missing the credentials required for refresh"
        )

    current_time = time.time() if now is None else now
    try:
        expires_at = float(refreshed.get("oauth_expires_at"))
    except (TypeError, ValueError):
        expires_at = _extract_expiry(access_token, refreshed.get("oauth_id_token"))

    # Refresh slightly before expiry so a long-running request does not start
    # with a token that expires while it is in flight.
    if not force and expires_at is not None and expires_at > current_time + 60:
        return refreshed

    try:
        response = httpx.post(
            OPENAI_OAUTH_TOKEN_URL,
            json={
                "client_id": OPENAI_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise OpenAIOAuthRefreshError(
            "OpenAI OAuth token refresh could not reach the authorization server"
        ) from exc

    if not 200 <= response.status_code < 300:
        raise _response_error(response, "token refresh")

    try:
        token_data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise OpenAIOAuthRefreshError(
            "OpenAI OAuth token refresh returned an invalid response"
        ) from exc

    new_access_token = token_data.get("access_token")
    if not isinstance(new_access_token, str) or not new_access_token:
        raise OpenAIOAuthRefreshError(
            "OpenAI OAuth token refresh returned no access token"
        )

    new_id_token = token_data.get("id_token") or refreshed.get("oauth_id_token")
    new_refresh_token = token_data.get("refresh_token") or refresh_token
    expires_in = token_data.get("expires_in")
    try:
        new_expires_at = current_time + float(expires_in)
    except (TypeError, ValueError):
        new_expires_at = _extract_expiry(new_access_token, new_id_token)

    refreshed.update(
        {
            "oauth_access_token": new_access_token,
            "oauth_refresh_token": new_refresh_token,
            "oauth_id_token": new_id_token,
            "oauth_account_id": extract_account_id(new_id_token, new_access_token)
            or account_id,
            "oauth_account_email": extract_email(new_id_token, new_access_token)
            or refreshed.get("oauth_account_email"),
            "oauth_expires_at": new_expires_at,
            "oauth_authenticated": True,
        }
    )
    return refreshed
