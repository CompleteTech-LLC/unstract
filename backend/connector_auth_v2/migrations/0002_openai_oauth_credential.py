import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("connector_auth_v2", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="OpenAIOAuthCredential",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "organization_id",
                    models.CharField(max_length=64),
                ),
                ("account_id", models.CharField(max_length=255)),
                (
                    "account_label",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("encrypted_credentials", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("modified_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="openai_oauth_credentials",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "OpenAI OAuth credential",
                "verbose_name_plural": "OpenAI OAuth credentials",
                "db_table": "openai_oauth_credential",
            },
        ),
        migrations.AddConstraint(
            model_name="openaioauthcredential",
            constraint=models.UniqueConstraint(
                fields=("user", "organization_id", "account_id"),
                name="unique_openai_oauth_user_org_account",
            ),
        ),
        migrations.AddIndex(
            model_name="openaioauthcredential",
            index=models.Index(
                fields=("user", "organization_id", "-modified_at"),
                name="openai_oauth_user_org_mod_idx",
            ),
        ),
    ]
