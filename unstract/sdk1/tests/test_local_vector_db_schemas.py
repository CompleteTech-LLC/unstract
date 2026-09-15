import json
from pathlib import Path

SCHEMA_ROOT = (
    Path(__file__).parents[1]
    / "src"
    / "unstract"
    / "sdk1"
    / "adapters"
    / "vectordb"
)

EXPECTED_DEFAULTS = {
    "qdrant": {
        "adapter_name": "qdrant-local",
        "url": "http://qdrant:6333",
        "api_key": "",
    },
    "postgres": {
        "adapter_name": "postgres-vector-local",
        "database": "unstract_db",
        "host": "postgres-vector",
        "port": 5432,
        "user": "unstract_dev",
        "password": "unstract_pass",
        "enable_ssl": True,
    },
    "weaviate": {
        "adapter_name": "weaviate-local",
        "url": "http://weaviate:8080",
        "api_key": "",
    },
    "milvus": {
        "adapter_name": "milvus-local",
        "uri": "http://milvus:19530",
        "token": "",
    },
}


def test_local_vector_db_schemas_prefill_compose_connection_values() -> None:
    for adapter_name, expected_defaults in EXPECTED_DEFAULTS.items():
        schema_path = SCHEMA_ROOT / adapter_name / "src" / "static" / "json_schema.json"
        schema = json.loads(schema_path.read_text())
        properties = schema["properties"]

        for field_name, expected_value in expected_defaults.items():
            assert properties[field_name]["default"] == expected_value
