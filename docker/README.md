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

## Local Qwen3 embeddings

The Compose stack includes nine optional Hugging Face Text Embeddings
Inference (TEI) services for the three selected Qwen3 embedding models. CPU,
NVIDIA GPU, and Intel XPU services are separate so each runtime can be
benchmarked against the same model and workload. Model files are cached in
persistent, model-specific volumes.

The CPU services are defined in `docker-compose-local-embeddings.yaml`; the
NVIDIA services are defined in the separately included
`docker-compose-local-embeddings-gpu.yaml`; Intel services are defined in
`docker-compose-local-embeddings-xpu.yaml`. This keeps each accelerator
runtime isolated while the root Compose file still exposes one consistent
service matrix.

| Model | Vector dimensions | CPU service / host port | NVIDIA GPU service / host port | Intel XPU service / host port |
|-------|-------------------:|-------------------------|--------------------------------|------------------------------|
| Qwen3-Embedding-0.6B | 1024 | qwen3-embedding-06b-cpu / 8101 | qwen3-embedding-06b-gpu / 8201 | qwen3-embedding-06b-xpu / 8301 |
| Qwen3-Embedding-4B | 2560 | qwen3-embedding-4b-cpu / 8102 | qwen3-embedding-4b-gpu / 8202 | qwen3-embedding-4b-xpu / 8302 |
| Qwen3-Embedding-8B | 4096 | qwen3-embedding-8b-cpu / 8103 | qwen3-embedding-8b-gpu / 8203 | qwen3-embedding-8b-xpu / 8303 |

Start the CPU, NVIDIA GPU, or Intel XPU profile from the docker directory:

```bash
cd docker

# CPU services
VERSION=dev docker compose -f docker-compose.yaml --profile embeddings-cpu up -d \
  qwen3-embedding-06b-cpu qwen3-embedding-4b-cpu qwen3-embedding-8b-cpu

# GPU services
VERSION=dev docker compose -f docker-compose.yaml --profile embeddings-gpu up -d \
  qwen3-embedding-06b-gpu qwen3-embedding-4b-gpu qwen3-embedding-8b-gpu

# Intel XPU services
VERSION=dev docker compose -f docker-compose.yaml --profile embeddings-xpu up -d \
  qwen3-embedding-06b-xpu qwen3-embedding-4b-xpu qwen3-embedding-8b-xpu

# CPU and NVIDIA GPU comparison matrix
VERSION=dev docker compose -f docker-compose.yaml --profile embeddings-both up -d \
  qwen3-embedding-06b-cpu qwen3-embedding-4b-cpu qwen3-embedding-8b-cpu \
  qwen3-embedding-06b-gpu qwen3-embedding-4b-gpu qwen3-embedding-8b-gpu
```

Before starting an accelerator profile, verify that the host can see a
supported device:

```bash
python3 docker/benchmarks/gpu_preflight.py --backend auto --json
```

NVIDIA services require the NVIDIA Container Toolkit and a compatible NVIDIA
driver. The default NVIDIA image targets the TEI CUDA 1.9 runtime; set
`QWEN3_TEI_GPU_IMAGE` when an architecture-specific image is needed. Intel
XPU services use the `xpu-ipex-latest` TEI image by default and pass through
`/dev/dri`; set `QWEN3_TEI_XPU_IMAGE` to use a different Intel-compatible
image. Use `--backend intel` to require Intel telemetry explicitly.
The default CPU image targets x86_64; set QWEN3_TEI_CPU_IMAGE to the TEI
cpu-arm64-1.9 image on ARM64 hosts. Accelerator profiles may exceed available
device memory when another model is already using the GPU. For a fair
comparison, start one profile at a time or benchmark endpoints sequentially.

Accelerator services have independent tuning variables so an accelerator run
does not change the CPU run's request limits. NVIDIA uses
`QWEN3_GPU_MAX_*`; Intel XPU uses `QWEN3_XPU_MAX_BATCH_TOKENS` (default
`1024`), `QWEN3_XPU_MAX_CLIENT_BATCH_SIZE` (default `16`),
`QWEN3_XPU_MAX_CONCURRENT_REQUESTS` (default `4`), and
`QWEN3_XPU_TOKENIZATION_WORKERS` (default `4`).

Each service exposes an OpenAI-compatible endpoint. From an Unstract
container, use the internal URL; from the host, use the localhost URL:

| Service family | Internal API base | Host API base |
|----------------|-------------------|---------------|
| 0.6B CPU | http://qwen3-embedding-06b-cpu/v1 | http://localhost:8101/v1 |
| 0.6B GPU | http://qwen3-embedding-06b-gpu/v1 | http://localhost:8201/v1 |
| 0.6B Intel XPU | http://qwen3-embedding-06b-xpu/v1 | http://localhost:8301/v1 |
| 4B CPU | http://qwen3-embedding-4b-cpu/v1 | http://localhost:8102/v1 |
| 4B GPU | http://qwen3-embedding-4b-gpu/v1 | http://localhost:8202/v1 |
| 4B Intel XPU | http://qwen3-embedding-4b-xpu/v1 | http://localhost:8302/v1 |
| 8B CPU | http://qwen3-embedding-8b-cpu/v1 | http://localhost:8103/v1 |
| 8B GPU | http://qwen3-embedding-8b-gpu/v1 | http://localhost:8203/v1 |
| 8B Intel XPU | http://qwen3-embedding-8b-xpu/v1 | http://localhost:8303/v1 |

For the OpenAI Compatible Embedding adapter, set Model to the matching
served-model alias, API Base to the appropriate URL above, and API Key to the
local placeholder accepted by the adapter. Qwen3 retrieval expects this query
prefix:

```text
Instruct: Given a web search query, retrieve relevant passages that answer the query
Query:
```

Leave Passage Prefix empty. Append one ASCII space after Query: before the
query text. These fields are available in the adapter schema and are also
recorded with model IDs, dimensions, and endpoints in
docker/local-embeddings.config.json. Use one model consistently for indexing
and querying. A model with different vector dimensions requires a separate
collection and a full reindex.

Run the benchmark harness after starting the desired services:

```bash
python3 docker/benchmarks/embedding_benchmark.py --mode cpu \
  --output /tmp/qwen3-cpu.json --strict
python3 docker/benchmarks/embedding_benchmark.py --mode gpu \
  --output /tmp/qwen3-gpu.json --strict
python3 docker/benchmarks/embedding_benchmark.py --mode xpu \
  --output /tmp/qwen3-xpu.json --strict
python3 docker/benchmarks/embedding_benchmark.py --mode both \
  --output /tmp/qwen3-both.json --strict
```

The harness waits for health, warms each endpoint, measures single-request
and batch latency/throughput, checks the expected dimensions, and reports
fixture retrieval metrics. Replace
docker/benchmarks/qwen3_smoke_dataset.json with a representative corpus and
labeled queries before using quality scores to select a production model.
When `--mode both` is used, the JSON report also contains one comparison per
model with GPU speedups and GPU-minus-CPU retrieval-quality deltas. This makes
the combined profile suitable for choosing a deployment target from one run.
This change intentionally retains all nine variants; unselected services can
be removed in a follow-up after the benchmark review.

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
