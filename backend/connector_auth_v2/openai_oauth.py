"""Server-side device login and credential hand-off for OpenAI OAuth."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.cache import cache
from rest_framework.request import Request
from unstract.sdk1.auth.openai_oauth import (
    OPENAI_OAUTH_CLIENT_ID,
    OPENAI_OAUTH_DEVICE_REDIRECT_URI,
    OPENAI_OAUTH_DEVICE_TOKEN_URL,
    OPENAI_OAUTH_DEVICE_USERCODE_URL,
    OPENAI_OAUTH_DEVICE_VERIFICATION_URL,
    OPENAI_OAUTH_PRIVATE_FIELDS,
    OPENAI_OAUTH_TOKEN_URL,
    OpenAIOAuthError,
    build_openai_oauth_json_schema,
    extract_account_id,
    extract_email,
    fetch_openai_oauth_model_catalog,
    refresh_openai_oauth_metadata,
)
from utils.user_session import UserSessionUtils

from connector_auth_v2.models import OpenAIOAuthCredential

_CACHE_PREFIX = "openai-oauth:"
_DEFAULT_STATE_TTL_SECONDS = 900
_STATE_TTL_SECONDS = int(
    os.environ.get("OPENAI_OAUTH_STATE_TTL_SECONDS", str(_DEFAULT_STATE_TTL_SECONDS))
)


class OpenAIOAuthSessionError(OpenAIOAuthError):
    """Raised for an invalid, expired, or unauthorized browser login session."""


def _safe_response_json(response: httpx.Response, operation: str) -> dict[str, Any]:
    if not 200 <= response.status_code < 300:
        raise OpenAIOAuthError(
            f"OpenAI OAuth {operation} failed with status {response.status_code}"
        )
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise OpenAIOAuthError(
            f"OpenAI OAuth {operation} returned an invalid response"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenAIOAuthError(f"OpenAI OAuth {operation} returned an invalid response")
    return payload


def _post_json(url: str, *, json_body: dict[str, Any], operation: str) -> dict[str, Any]:
    try:
        response = httpx.post(url, json=json_body, timeout=15.0)
    except httpx.HTTPError as exc:
        raise OpenAIOAuthError(
            f"OpenAI OAuth {operation} could not reach the authorization server"
        ) from exc
    return _safe_response_json(response, operation)


def _post_form(url: str, *, form_data: dict[str, str], operation: str) -> dict[str, Any]:
    try:
        response = httpx.post(
            url,
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise OpenAIOAuthError(
            f"OpenAI OAuth {operation} could not reach the authorization server"
        ) from exc
    return _safe_response_json(response, operation)


def _as_expiry_seconds(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def _account_label(account_id: str, email: str | None) -> str:
    if email:
        return email
    suffix = account_id[-8:] if len(account_id) > 8 else account_id
    return f"OpenAI account ({suffix})"


class OpenAIOAuthService:
    """Own one short-lived OAuth login session per browser/account."""

    @staticmethod
    def _identity(request: Request) -> tuple[str, str]:
        user_id = getattr(request.user, "pk", None) or getattr(request.user, "id", None)
        organization_id = UserSessionUtils.get_organization_id(request)
        if user_id is None or not organization_id:
            raise OpenAIOAuthSessionError(
                "An authenticated organization session is required for OpenAI OAuth"
            )
        return str(user_id), str(organization_id)

    @staticmethod
    def _encrypt(credentials: dict[str, Any]) -> str:
        fernet = Fernet(str(settings.ENCRYPTION_KEY).encode("utf-8"))
        return fernet.encrypt(json.dumps(credentials).encode("utf-8")).decode("utf-8")

    @staticmethod
    def _decrypt(value: str) -> dict[str, Any]:
        try:
            fernet = Fernet(str(settings.ENCRYPTION_KEY).encode("utf-8"))
            credentials = json.loads(fernet.decrypt(value.encode("utf-8")))
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            raise OpenAIOAuthSessionError(
                "OpenAI OAuth login session is no longer valid"
            ) from exc
        if not isinstance(credentials, dict):
            raise OpenAIOAuthSessionError("OpenAI OAuth login session is no longer valid")
        return credentials

    @classmethod
    def _persist_credentials(
        cls, request: Request, credentials: dict[str, Any]
    ) -> OpenAIOAuthCredential:
        """Upsert encrypted credentials for the current user and organization."""
        _, organization_id = cls._identity(request)
        account_id = credentials.get("oauth_account_id")
        if not isinstance(account_id, str) or not account_id:
            raise OpenAIOAuthSessionError(
                "OpenAI OAuth credentials returned no ChatGPT account"
            )

        return OpenAIOAuthCredential.objects.update_or_create(
            user_id=request.user.pk,
            organization_id=organization_id,
            account_id=account_id,
            defaults={
                "account_label": _account_label(
                    account_id, credentials.get("oauth_account_email")
                ),
                "encrypted_credentials": cls._encrypt(credentials),
            },
        )[0]

    @classmethod
    def _load_persisted_credentials(
        cls, request: Request, credential: OpenAIOAuthCredential
    ) -> dict[str, Any]:
        """Decrypt and refresh one durable credential record when necessary."""
        credentials = cls._decrypt(credential.encrypted_credentials)
        refreshed = refresh_openai_oauth_metadata(credentials)
        account_id = refreshed.get("oauth_account_id")
        if not isinstance(account_id, str) or not account_id:
            raise OpenAIOAuthSessionError(
                "OpenAI OAuth credentials returned no ChatGPT account"
            )

        if refreshed != credentials or credential.account_id != account_id:
            credential.account_id = account_id
            credential.account_label = _account_label(
                account_id, refreshed.get("oauth_account_email")
            )
            credential.encrypted_credentials = cls._encrypt(refreshed)
            credential.save(
                update_fields=[
                    "account_id",
                    "account_label",
                    "encrypted_credentials",
                    "modified_at",
                ]
            )
        return refreshed

    @classmethod
    def _save_state(cls, cache_key: str, state: dict[str, Any]) -> None:
        expiry = _as_expiry_seconds(state.get("expires_at"))
        ttl = _STATE_TTL_SECONDS
        if expiry is not None:
            ttl = min(ttl, max(30, int(expiry - time.time())))
        cache.set(cache_key, state, max(ttl, 30))

    @classmethod
    def _get_owned_state(cls, cache_key: str, request: Request) -> dict[str, Any]:
        if not cache_key or not cache_key.startswith(_CACHE_PREFIX):
            raise OpenAIOAuthSessionError("OpenAI OAuth login session is invalid")
        user_id, organization_id = cls._identity(request)
        state = cache.get(cache_key)
        if not isinstance(state, dict):
            raise OpenAIOAuthSessionError(
                "OpenAI OAuth login session was not found or has expired"
            )
        if (
            state.get("owner_id") != user_id
            or state.get("organization_id") != organization_id
        ):
            raise OpenAIOAuthSessionError(
                "OpenAI OAuth login session was not found or is not owned by this user"
            )
        expiry = _as_expiry_seconds(state.get("expires_at"))
        if expiry is not None and expiry <= time.time():
            cache.delete(cache_key)
            raise OpenAIOAuthSessionError("OpenAI OAuth login session has expired")
        return state

    @classmethod
    def start(cls, request: Request) -> dict[str, Any]:
        user_id, organization_id = cls._identity(request)
        payload = _post_json(
            OPENAI_OAUTH_DEVICE_USERCODE_URL,
            json_body={"client_id": OPENAI_OAUTH_CLIENT_ID},
            operation="device login start",
        )
        device_auth_id = payload.get("device_auth_id")
        user_code = payload.get("user_code") or payload.get("usercode")
        if not isinstance(device_auth_id, str) or not device_auth_id:
            raise OpenAIOAuthError("OpenAI OAuth device login returned no device id")
        if not isinstance(user_code, str) or not user_code:
            raise OpenAIOAuthError("OpenAI OAuth device login returned no user code")

        try:
            interval = max(1, int(payload.get("interval", 5)))
        except (TypeError, ValueError):
            interval = 5
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, str):
            expires_at = datetime.fromtimestamp(
                time.time() + _STATE_TTL_SECONDS, tz=UTC
            ).isoformat()

        cache_key = f"{_CACHE_PREFIX}{uuid.uuid4().hex}"
        cls._save_state(
            cache_key,
            {
                "status": "pending",
                "owner_id": user_id,
                "organization_id": organization_id,
                "device_auth_id": device_auth_id,
                "user_code": user_code,
                "poll_interval": interval,
                "expires_at": expires_at,
            },
        )
        return {
            "cache_key": cache_key,
            "verification_url": OPENAI_OAUTH_DEVICE_VERIFICATION_URL,
            "user_code": user_code,
            "expires_at": expires_at,
            "poll_interval": interval,
        }

    @classmethod
    def _success_response(cls, state: dict[str, Any]) -> dict[str, Any]:
        response = {
            "status": "success",
            "account_label": state.get("account_label", "OpenAI account"),
        }
        if state.get("restored"):
            response["restored"] = True
        return response

    @classmethod
    def _create_success_handoff(
        cls, request: Request, credentials: dict[str, Any], *, restored: bool = False
    ) -> dict[str, Any]:
        """Create a fresh short-lived hand-off for a durable account record."""
        user_id, organization_id = cls._identity(request)
        account_id = credentials.get("oauth_account_id")
        if not isinstance(account_id, str) or not account_id:
            raise OpenAIOAuthSessionError(
                "OpenAI OAuth credentials returned no ChatGPT account"
            )
        cache_key = f"{_CACHE_PREFIX}{uuid.uuid4().hex}"
        state = {
            "status": "success",
            "owner_id": user_id,
            "organization_id": organization_id,
            "credentials": cls._encrypt(credentials),
            "account_label": _account_label(
                account_id, credentials.get("oauth_account_email")
            ),
            "restored": restored,
        }
        cls._save_state(cache_key, state)
        return {"cache_key": cache_key, **cls._success_response(state)}

    @classmethod
    def poll(cls, request: Request, cache_key: str) -> dict[str, Any]:
        state = cls._get_owned_state(cache_key, request)
        if state.get("status") == "success":
            # A hand-off created by an older backend may have completed before
            # durable persistence was introduced. Backfill it on first use.
            if state.get("credentials"):
                credentials = cls._decrypt(state["credentials"])
                cls._persist_credentials(request, credentials)
            return cls._success_response(state)

        try:
            response = httpx.post(
                OPENAI_OAUTH_DEVICE_TOKEN_URL,
                json={
                    "device_auth_id": state["device_auth_id"],
                    "user_code": state["user_code"],
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise OpenAIOAuthError(
                "OpenAI OAuth device login could not reach the authorization server"
            ) from exc

        # The official device endpoint uses 403/404 while the user has not
        # finished entering the code.  Keep the browser poll alive for those
        # expected responses (and for rate limiting).
        if response.status_code in {403, 404, 429}:
            return {
                "status": "pending",
                "poll_interval": state.get("poll_interval", 5),
            }
        device_result = _safe_response_json(response, "device login poll")
        authorization_code = device_result.get("authorization_code")
        code_verifier = device_result.get("code_verifier")
        if not isinstance(authorization_code, str) or not isinstance(code_verifier, str):
            raise OpenAIOAuthError(
                "OpenAI OAuth device login returned incomplete authorization data"
            )

        token_data = _post_form(
            OPENAI_OAUTH_TOKEN_URL,
            form_data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": OPENAI_OAUTH_DEVICE_REDIRECT_URI,
                "client_id": OPENAI_OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            operation="token exchange",
        )
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        id_token = token_data.get("id_token")
        if not all(
            isinstance(value, str) and value for value in (access_token, refresh_token)
        ):
            raise OpenAIOAuthError(
                "OpenAI OAuth token exchange returned incomplete credentials"
            )

        account_id = extract_account_id(id_token, access_token)
        if not account_id:
            raise OpenAIOAuthError(
                "OpenAI OAuth token exchange returned no ChatGPT account"
            )
        email = extract_email(id_token, access_token)
        try:
            expires_in = float(token_data.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        credentials = {
            "oauth_access_token": access_token,
            "oauth_refresh_token": refresh_token,
            "oauth_id_token": id_token,
            "oauth_account_id": account_id,
            "oauth_account_email": email,
            "oauth_expires_at": time.time() + expires_in,
            "oauth_authenticated": True,
        }
        # Persist immediately after authorization succeeds. The Redis state
        # below remains only a short-lived, browser-scoped hand-off.
        cls._persist_credentials(request, credentials)
        state.update(
            {
                "status": "success",
                "credentials": cls._encrypt(credentials),
                "account_label": _account_label(account_id, email),
            }
        )
        cls._save_state(cache_key, state)
        return cls._success_response(state)

    @classmethod
    def credentials_for_request(cls, cache_key: str, request: Request) -> dict[str, Any]:
        state = cls._get_owned_state(cache_key, request)
        if state.get("status") != "success" or not state.get("credentials"):
            raise OpenAIOAuthSessionError("Complete OpenAI OAuth authentication first")
        credentials = cls._decrypt(state["credentials"])
        refreshed = refresh_openai_oauth_metadata(credentials)
        if refreshed != credentials:
            state["credentials"] = cls._encrypt(refreshed)
            cls._save_state(cache_key, state)
            cls._persist_credentials(request, refreshed)
        return refreshed

    @classmethod
    def restore(cls, request: Request) -> dict[str, Any] | None:
        """Restore the most recently used durable OpenAI account, if any."""
        user_id, organization_id = cls._identity(request)
        credential = (
            OpenAIOAuthCredential.objects.filter(
                user_id=user_id,
                organization_id=organization_id,
            )
            .order_by("-modified_at")
            .first()
        )
        if credential is None:
            return None

        credentials = cls._load_persisted_credentials(request, credential)
        return cls._create_success_handoff(request, credentials, restored=True)

    @staticmethod
    def dynamic_model_schema(
        credentials: dict[str, Any], *, current_model: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return a live account-specific form schema and refreshed credentials."""
        refreshed = refresh_openai_oauth_metadata(credentials)
        catalog = fetch_openai_oauth_model_catalog(refreshed)
        schema = build_openai_oauth_json_schema(
            catalog,
            current_model=current_model,
        )
        return schema, refreshed

    @classmethod
    def consume(cls, cache_key: str, request: Request) -> None:
        # Validate ownership before deleting so a leaked cache key cannot be
        # used to consume another user's pending login.
        cls._get_owned_state(cache_key, request)
        cache.delete(cache_key)


def redact_openai_oauth_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the non-secret account state safe for API responses."""
    redacted = {
        key: value
        for key, value in metadata.items()
        if key not in OPENAI_OAUTH_PRIVATE_FIELDS
    }
    redacted["oauth_authenticated"] = bool(metadata.get("oauth_access_token"))
    account_id = metadata.get("oauth_account_id")
    email = metadata.get("oauth_account_email")
    if isinstance(account_id, str):
        redacted["oauth_account_label"] = _account_label(account_id, email)
    return redacted
