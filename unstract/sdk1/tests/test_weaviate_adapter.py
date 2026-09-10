from unittest.mock import MagicMock, patch

from unstract.sdk1.adapters.vectordb.weaviate.src.weaviate import (
    Constants,
    Weaviate,
)


def _adapter(url: str, api_key: str = "") -> Weaviate:
    adapter = object.__new__(Weaviate)
    adapter._config = {
        Constants.URL: url,
        Constants.API_KEY: api_key,
    }
    return adapter


def test_local_url_uses_local_connector_with_api_key() -> None:
    client = MagicMock()

    with patch(
        "unstract.sdk1.adapters.vectordb.weaviate.src.weaviate.weaviate.connect_to_local",
        return_value=client,
    ) as connect_to_local:
        result = _adapter("http://weaviate:8080", "local-key")._connect()

    assert result is client
    connect_to_local.assert_called_once()
    call_kwargs = connect_to_local.call_args.kwargs
    assert call_kwargs["host"] == "weaviate"
    assert call_kwargs["port"] == 8080
    assert call_kwargs["grpc_port"] == 50051
    assert call_kwargs["auth_credentials"] is not None


def test_local_url_allows_anonymous_connection() -> None:
    client = MagicMock()

    with patch(
        "unstract.sdk1.adapters.vectordb.weaviate.src.weaviate.weaviate.connect_to_local",
        return_value=client,
    ) as connect_to_local:
        _adapter("http://localhost:8084")._connect()

    assert connect_to_local.call_args.kwargs["host"] == "localhost"
    assert connect_to_local.call_args.kwargs["port"] == 8084
    assert connect_to_local.call_args.kwargs["auth_credentials"] is None


def test_https_url_preserves_cloud_connector() -> None:
    client = MagicMock()

    with patch(
        "unstract.sdk1.adapters.vectordb.weaviate.src.weaviate.weaviate.connect_to_weaviate_cloud",
        return_value=client,
    ) as connect_to_cloud:
        result = _adapter("https://example.weaviate.cloud", "cloud-key")._connect()

    assert result is client
    connect_to_cloud.assert_called_once()
    assert connect_to_cloud.call_args.kwargs["cluster_url"] == (
        "https://example.weaviate.cloud"
    )
