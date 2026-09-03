# Docker Commands

## Docker Build

```bash
# Build all services
VERSION=dev docker compose -f docker-compose.build.yaml build

# Build a specific service alone
VERSION=dev docker compose -f docker-compose.build.yaml build frontend

# Build optional services also
VERSION=dev docker compose -f docker-compose.build.yaml --profile optional build
```

## Docker Run

**NOTE**: First copy `sample.*.env` files to `*.env` and update as required.

```bash
# Up all services
VERSION=dev docker compose -f docker-compose.yaml up -d

# Up a specific service alone
VERSION=dev docker compose -f docker-compose.yaml up -d frontend

# Up optional services also
VERSION=dev docker compose -f docker-compose.yaml --profile optional up -d
```

Now access frontend at http://frontend.unstract.localhost

## Local vector databases

The default development Compose stack starts the open-source vector database
backends supported by Unstract:

- Qdrant
- PostgreSQL with the `pgvector` extension
- Weaviate
- Milvus Standalone (with private etcd and MinIO dependencies)

Pinecone is supported as a hosted provider and is intentionally not included
in the local stack because it is not a self-hosted open-source service.

The services use persistent named volumes and are bound to loopback on the host.
Qdrant and Weaviate use anonymous access because this is a local development
stack; do not expose these ports beyond the local machine without adding
authentication and TLS. The Unstract workers connect over the Compose network,
so use the internal addresses below when creating an adapter in the UI:

| Adapter | UI fields | Address from Unstract containers | Host address | Local credentials |
|---------|-----------|----------------------------------|--------------|-------------------|
| Qdrant | URL, API Key | `http://qdrant:6333` | `http://localhost:6333` | Leave API Key empty |
| Postgres | Database, Host, Port, User, Password, Enable SSL | Host `postgres-vector`, port `5432` | Host `localhost`, port `5433` | Values from `docker/essentials.env`; set Enable SSL to `false` |
| Weaviate | URL, API Key | `http://weaviate:8080` | `http://localhost:8084` | Leave API Key empty |
| Milvus | URI, Token | `http://milvus:19530` | `http://localhost:19530` | Leave Token empty |

When the adapter schema is served by the local development stack, these
internal values are pre-filled in the form, including the sample PostgreSQL
credentials from `docker/essentials.env` and SSL disabled for the local
connection. If `essentials.env` has customized the PostgreSQL credentials,
replace the pre-filled values with the customized values before testing the
connection. Keep the container DNS names (`qdrant`, `postgres-vector`,
`weaviate`, and `milvus`) when the adapter is used by Unstract workers; use the
host addresses only for clients running outside the Compose network.

For the Postgres adapter, use the `POSTGRES_USER`, `POSTGRES_PASSWORD`, and
`POSTGRES_DB` values in `docker/essentials.env`; the vector database has its own
container and volume even though it reuses the platform's local development
credentials. The initialization script enables `vector` and creates the
`POSTGRES_SCHEMA` schema on first boot.

The default `run-platform.sh` flow creates `docker/.env` and
`docker/essentials.env` before starting Compose. To start only the vector
services during development, run:

```bash
cd docker
VERSION=dev docker compose -f docker-compose.yaml up -d \
  qdrant postgres-vector weaviate milvus
```

Do not use `docker compose down -v` unless deleting local vector data is
intentional.

## Overriding a service's config

By making use of the [merge compose files](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/) feature its possible to override some configuration that's used by the services.

Copy and rename the `sample.compose.override.yaml` to `compose.override.yaml` and update it as necessary.

```bash
cp sample.compose.override.yaml compose.override.yaml

# Configuration in docker-compose.yaml gets overridden
VERSION=dev docker compose -f docker-compose.yaml -f compose.override.yaml up -d
```

This can be useful during development to:

- Not run some memory intensive services
- Use commands with different arguments to save resources
- Mount additional volumes or define additional env to configure behaviour

## Development with Docker Compose Watch

[Docker Compose Watch](https://docs.docker.com/compose/how-tos/file-watch/) (available in Docker Compose v2.22.0+) enables a streamlined development workflow by automatically syncing code changes to containers and restarting services as needed.

### Setting Up Watch Mode

1. Ensure you're using Docker Compose v2.22.0 or higher

   ```bash
   docker compose version
   ```

2. Create your `compose.override.yaml` with watch configurations

   ```bash
   cp sample.compose.override.yaml compose.override.yaml
   ```

3. Start services with watch mode enabled

   ```bash
   VERSION=dev docker compose -f docker-compose.yaml -f compose.override.yaml watch
   ```

> **NOTE**: Make sure to specify the build definitions also in your `compose.override.yaml` file or specify [docker-compose.build.yaml](/docker/docker-compose.build.yaml) while running the above command.

### Example Workflow

1. Start services with watch mode:

   ```bash
   VERSION=dev docker compose -f docker-compose.yaml -f compose.override.yaml watch
   ```

2. Make changes to your code - they're automatically synced and services restart as needed

3. View logs: `docker compose logs -f [service_name]`

## Debugging Containers

Enable debugpy by adding `compose.debug.yaml`:

```bash
VERSION=dev docker compose -f docker-compose.yaml -f compose.override.yaml -f compose.debug.yaml watch
```

Debug ports per service:

| Service | Port |
|---------|------|
| backend | 5678 |
| runner | 5679 |
| platform-service | 5680 |
| prompt-service | 5681 |
| **V2 Workers** | |
| worker-pg-fileproc | 5682 |
| worker-pg-callback | 5683 |
| worker-pg-orchestrator-api | 5684 |
| worker-pg-orchestrator-general | 5685 |
| worker-pg-notification | 5686 |
| worker-log-stream-consumer | 5687 |
| worker-pg-scheduler | 5688 |

### VSCode Configuration

Example `launch.json` to attach to the `backend` container:

```json
{
  "name": "Docker: Backend Remote Debug",
  "type": "debugpy",
  "request": "attach",
  "connect": { "host": "localhost", "port": 5678 },
  "pathMappings": [
    { "localRoot": "${workspaceFolder:unstract}/backend", "remoteRoot": "/app" },
    { "localRoot": "${workspaceFolder:unstract}/unstract", "remoteRoot": "/unstract" }
  ],
  "justMyCode": false,
  "django": true
}
```

See [VSCode docs](https://code.visualstudio.com/docs/devcontainers/attach-container#_attach-to-a-docker-container) for more details.

## `src` Folder Layout and `gunicorn`

For the following project structure:

```bash
scheduler
  |- src
  |   |- unstract
  |       |- scheduler
  |           |- main.py
  |- uv.lock
  |- pyproject.toml
```

This will install the project to:

```bash
.venv/lib/python3.12/site-packages/unstract/scheduler/main.py
```

This will allow `gunicorn` to refer the package directly as:

```bash
$ gunicorn "-c" "python:unstract.scheduler.config.gunicorn" "unstract.scheduler.main:app"
```
