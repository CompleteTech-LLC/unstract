import logging
import uuid

from django.conf import settings
from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.versioning import URLPathVersioning
from unstract.sdk1.auth.openai_oauth import OpenAIOAuthError
from utils.user_session import UserSessionUtils

from connector_auth_v2.constants import SocialAuthConstants
from connector_auth_v2.exceptions import KeyNotConfigured
from connector_auth_v2.openai_oauth import (
    OpenAIOAuthService,
    OpenAIOAuthSessionError,
)

logger = logging.getLogger(__name__)


class ConnectorAuthViewSet(viewsets.ViewSet):
    """Contains methods for Connector related authentication."""

    versioning_class = URLPathVersioning

    def cache_key(
        self: "ConnectorAuthViewSet", request: Request, backend: str
    ) -> Response:
        if backend == SocialAuthConstants.GOOGLE_OAUTH and (
            settings.SOCIAL_AUTH_GOOGLE_OAUTH2_KEY is None
            or settings.SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET is None
        ):
            msg = (
                f"Keys not configured for {backend}, add env vars "
                f"`GOOGLE_OAUTH2_KEY` and `GOOGLE_OAUTH2_SECRET`."
            )
            logger.warning(msg)
            raise KeyNotConfigured(
                f"{msg}\nRefer to: "
                "https://developers.google.com/identity/protocols/oauth2#1.-"
                "obtain-oauth-2.0-credentials-from-the-dynamic_data.setvar."
                "console_name-."
            )

        random = str(uuid.uuid4())
        user_id = request.user.user_id
        org_id = UserSessionUtils.get_organization_id(request)
        cache_key = f"oauth:{org_id}|{user_id}|{backend}|{random}"
        logger.info(f"Generated cache key: {cache_key}")
        return Response(
            status=status.HTTP_200_OK,
            data={"cache_key": f"{cache_key}"},
        )

    @staticmethod
    def _require_authenticated(request: Request) -> Response | None:
        if not getattr(request.user, "is_authenticated", False):
            return Response(
                {"message": "Authentication is required for OpenAI OAuth."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return None

    def openai_start(self, request: Request) -> Response:
        """Start a server-side OpenAI device-code login."""
        if unauthorized := self._require_authenticated(request):
            return unauthorized
        try:
            return Response(
                OpenAIOAuthService.start(request), status=status.HTTP_200_OK
            )
        except OpenAIOAuthSessionError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except OpenAIOAuthError as exc:
            logger.warning("OpenAI OAuth device login start failed: %s", exc)
            return Response(
                {"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY
            )

    def openai_poll(self, request: Request) -> Response:
        """Poll one OpenAI device-code login and exchange it when complete."""
        if unauthorized := self._require_authenticated(request):
            return unauthorized
        cache_key = request.query_params.get("oauth-key")
        try:
            result = OpenAIOAuthService.poll(request, cache_key or "")
            return Response(result, status=status.HTTP_200_OK)
        except OpenAIOAuthSessionError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except OpenAIOAuthError as exc:
            logger.warning("OpenAI OAuth device login poll failed: %s", exc)
            return Response(
                {"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY
            )
