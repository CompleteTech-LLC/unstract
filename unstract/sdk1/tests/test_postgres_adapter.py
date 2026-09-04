import pytest

from unstract.sdk1.adapters.vectordb.postgres.src import postgres as postgres_module


@pytest.mark.parametrize(
    ("enable_ssl", "sslmode"),
    ((True, "require"), (False, "disable")),
)
def test_postgres_adapter_applies_sslmode_to_all_connections(
    monkeypatch: pytest.MonkeyPatch,
    enable_ssl: bool,
    sslmode: str,
) -> None:
    captured: dict[str, object] = {}

    def fake_from_params(**kwargs: object) -> object:
        captured["vector_store"] = kwargs
        return object()

    def fake_connect(**kwargs: object) -> object:
        captured["probe"] = kwargs
        return object()

    monkeypatch.setattr(
        postgres_module.PGVectorStore,
        "from_params",
        fake_from_params,
    )
    monkeypatch.setattr(postgres_module.psycopg2, "connect", fake_connect)

    postgres_module.Postgres(
        {
            "database": "unstract_db",
            "host": "postgres-vector",
            "port": 5432,
            "user": "unstract_dev",
            "password": "unstract_pass",
            "enable_ssl": enable_ssl,
        }
    )

    vector_store_kwargs = captured["vector_store"]
    assert isinstance(vector_store_kwargs, dict)
    assert vector_store_kwargs["connection_string"].endswith(
        f"/unstract_db?sslmode={sslmode}"
    )
    assert vector_store_kwargs["async_connection_string"].endswith(
        f"/unstract_db?sslmode={sslmode}"
    )

    probe_kwargs = captured["probe"]
    assert isinstance(probe_kwargs, dict)
    assert probe_kwargs["sslmode"] == sslmode
