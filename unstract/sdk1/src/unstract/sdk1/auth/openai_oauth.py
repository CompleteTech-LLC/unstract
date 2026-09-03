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
from collections.abc import Mapping
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
