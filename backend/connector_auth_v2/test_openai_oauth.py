from types import SimpleNamespace
from unittest.mock import patch

from account_v2.models import User
from cryptography.fernet import Fernet
from django.core.cache import cache
from django.test import TestCase, override_settings

from connector_auth_v2.models import OpenAIOAuthCredential
from connector_auth_v2.openai_oauth import OpenAIOAuthService

TEST_ENCRYPTION_KEY = Fernet.generate_key().decode("utf-8")
TEST_ORGANIZATION_ID = "oauth-test-org"
TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "openai-oauth-tests",
    }
}


@override_settings(ENCRYPTION_KEY=TEST_ENCRYPTION_KEY, CACHES=TEST_CACHES)
class OpenAIOAuthCredentialTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="oauth@example.com",
            email="oauth@example.com",
            password="not-used-in-this-test",
        )
        self.request = SimpleNamespace(user=self.user)
        cache.clear()

    def tearDown(self) -> None:
        cache.clear()

    def test_authenticated_account_is_encrypted_and_restorable(self) -> None:
        credentials = {
            "oauth_access_token": "access-token",
            "oauth_refresh_token": "refresh-token",
            "oauth_id_token": "id-token",
            "oauth_account_id": "account-123",
            "oauth_account_email": "oauth@example.com",
            "oauth_expires_at": 4_000_000_000,
            "oauth_authenticated": True,
        }

        with (
            patch.object(
                OpenAIOAuthService,
                "_identity",
                return_value=(str(self.user.pk), TEST_ORGANIZATION_ID),
            ),
            patch(
                "connector_auth_v2.openai_oauth.refresh_openai_oauth_metadata",
                side_effect=lambda metadata: dict(metadata),
            ),
        ):
            OpenAIOAuthService._persist_credentials(self.request, credentials)

            stored = OpenAIOAuthCredential.objects.get(user=self.user)
            assert "access-token" not in stored.encrypted_credentials
            assert stored.account_label == "oauth@example.com"

            restored = OpenAIOAuthService.restore(self.request)

        assert restored is not None
        assert restored["status"] == "success"
        assert restored["restored"] is True
        assert restored["cache_key"].startswith("openai-oauth:")

        state = cache.get(restored["cache_key"])
        assert state["status"] == "success"
        assert state["owner_id"] == str(self.user.pk)
        assert OpenAIOAuthService._decrypt(state["credentials"]) == credentials

    def test_restore_returns_no_account_before_first_login(self) -> None:
        with patch.object(
            OpenAIOAuthService,
            "_identity",
            return_value=(str(self.user.pk), TEST_ORGANIZATION_ID),
        ):
            assert OpenAIOAuthService.restore(self.request) is None
