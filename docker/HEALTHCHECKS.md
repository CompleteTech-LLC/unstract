# Compose health checks

The Compose files report application HTTP liveness, database readiness, and
worker progress separately. A healthy container does not prove a successful
document extraction, authenticated API operation, or delivery to every log sink.
Unhealthy status alone does not restart a container; these probes also do not
repair stale Podman process metadata.

| Services | Signal |
| --- | --- |
| Backend | `/api/v1/health` returns 200 or its expected unauthenticated 401. This is HTTP liveness, not database or authentication readiness. |
| Frontend | Nginx serves `/` successfully on its internal port. |
| Platform, x2text, runner | Their existing local HTTP health endpoint answers successfully. |
| PG queue workers and reaper | Existing `/health` endpoint, which checks queue-loop freshness and, for prefork workers, child progress. Existing long-task stale thresholds remain in effect. |
| Log stream consumer | A successful Redis poll or completed envelope disposition refreshes a local heartbeat. Redis errors do not refresh it. Poison-envelope disposal counts as loop progress; individual sink delivery is not proven. |
| Log history scheduler | Both history processing and notification-buffer processing must finish successfully and refresh separate heartbeats. |
| PostgreSQL instances | `pg_isready` over TCP, excluding the initialization-only Unix-socket server. |
| Redis | `redis-cli ping` must return `PONG`. |
| RabbitMQ | The application must be running and its configured listener ports reachable. |
| MinIO | The existing local readiness endpoint must return success. |
| Traefik | Built-in `traefik healthcheck --ping`. This checks the proxy itself, not every routed upstream. |
| Qdrant | `/readyz` must return HTTP 200, using the Bash shipped in the image. |
| Weaviate | `/v1/.well-known/ready` via the pinned Alpine image's BusyBox wget. |
| Milvus, its etcd and MinIO | Existing service-specific health probes are retained. |

Log heartbeats allow a 120-second processing grace beyond the configured poll
interval. Repeated failed or blocked processing is intentionally unhealthy;
process existence does not refresh a heartbeat. Probe startup grace is 90 seconds
for workers. Backend migration startup receives 120 seconds. PostgreSQL health
gates backend, platform-service, and x2text startup.

## Deployment requirements

- Rebuild and deploy `worker-unified` from this source before enabling the log
  heartbeat probes. They require `log_consumer/heartbeat.py` and the updated
  scheduler/consumer in the image. Merely changing Compose on an old image will
  fail these probes.
- Frontend health defaults to internal port 80. A rootless deployment with an
  Nginx override on port 8080 must set `FRONTEND_HEALTH_PORT=8080` in the frontend
  container environment. Host published-port numbers are not health ports.
- A deployment overlay replacing Traefik's `command` must retain `--ping=true`.
  The base Compose command now enables it.
- Apply changes to the final merged Compose configuration and recreate affected
  containers. Container restart alone does not install new health configuration.
- Preserve named volumes, encryption keys, credentials, and unrelated deployment
  overlays. Never use `down -v` to apply health checks.

The `minio-bootstrap` service is an intentional one-shot job and has no health
probe. Its successful exit is required by the backend dependency. Optional
`celery-flower` and `unstructured-io` profiles are outside the observed active
fleet. `feature-flag` was absent from that fleet; no unverified check is added for
it. These services must not be described as newly health-verified.

## Verification evidence and limits

Read-only probes on Train verified backend's expected 401, frontend HTTP success
on port 8080, platform and runner HTTP 200, representative PG worker/reaper HTTP
200, Redis PONG, PostgreSQL TCP readiness, MinIO readiness, and Qdrant HTTP 200.
Several containers had stale runtime process metadata, preventing execution of
RabbitMQ, Traefik, and Weaviate probes during this inspection. Weaviate tooling
is supported by the [pinned upstream Dockerfile](https://github.com/weaviate/weaviate/blob/v1.39.2/Dockerfile);
it still requires verification in the recovered live container. x2text was
unavailable, so its endpoint was checked in source only.

Local tests execute the actual Compose HTTP commands against isolated servers,
including HTTP failures and a closed listener. Worker tests cover missing,
stale, future-dated, and partially successful scheduler heartbeats, existing log
delivery behavior, and Redis failures that must not refresh progress. These are
component checks, not live deployment or end-to-end extraction evidence.

```sh
python3 -m unittest discover -s docker/scripts/tests -p test_compose_healthchecks.py
PYTHONPATH=workers:unstract/core/src uv run --no-project --with pytest --with redis \
  pytest --noconftest -o addopts= -q \
  workers/tests/test_log_consumer_heartbeat.py workers/tests/test_log_stream_consumer.py
bash -n workers/log_consumer/scheduler.sh
```

The first command requires PyYAML; alternatively use
`uv run --no-project --with pyyaml python -m unittest discover -s docker/scripts/tests -p test_compose_healthchecks.py`.
