# Domain Context: etl.home.complete.tech

Local home: `C:\Users\timot\Documents\projects\domains\etl.home.complete.tech`

Public URL: `https://etl.home.complete.tech/`

Source: `https://github.com/Zipstack/unstract`

Service host: `completetrain@train.home.complete.tech`

Remote root: `/home/completetrain/etl.home.complete.tech`

Runtime: rootless Podman under `completetrain`, fronted by the shared rootful
Caddy container `home-complete-tech-domain-proxy`.

## Deployment shape

- Upstream source is pinned to the release tag `v0.187.2`.
- Unstract is run with the upstream Docker Compose files plus
  `docker/compose.train.yaml`.
- The Unstract Traefik service is published only on host port `13110`; all
  database, broker, storage, and worker ports remain internal to
  `unstract-network`.
- The runner and Unstract Traefik use the rootless Podman API socket at
  `/run/user/1000/podman/podman.sock`, mounted at `/var/run/docker.sock` in
  their containers. This preserves Unstract's Docker-compatible container
  spawning path without exposing the rootful host socket.
- The frontend is mounted with `docker/frontend-nginx.conf` and listens on
  container port `8080`, because the published frontend image runs Nginx as a
  non-root user under rootless Podman.
- Shared Caddy proxies `etl.home.complete.tech` to `172.30.88.1:13110` and
  uses the existing wildcard certificate mounted at `/certs`.
- LAN DNS maps `etl.home.complete.tech` to `192.168.1.146` on
  `router.complete.tech`.

## Persistent data

The Compose named volumes are owned by the rootless project and must not be
removed during upgrades:

- `unstract-etl-home-complete-tech_postgres_data`
- `unstract-etl-home-complete-tech_redis_data`
- `unstract-etl-home-complete-tech_minio_data`
- `unstract-etl-home-complete-tech_qdrant_data`
- `unstract-etl-home-complete-tech_prompt_studio_data`
- `unstract-etl-home-complete-tech_rabbitmq_data`
- `unstract-etl-home-complete-tech_flipt_data`
- `/home/completetrain/etl.home.complete.tech/docker/workflow_data`

The backend and platform-service `ENCRYPTION_KEY` values are generated once
and must be backed up securely. Losing or changing that key makes encrypted
adapter credentials inaccessible.

## Operational commands

```sh
cd /home/completetrain/etl.home.complete.tech
export DOCKER_HOST=unix:///run/user/1000/podman/podman.sock
export VERSION=v0.187.2
docker compose -f docker/docker-compose.yaml -f docker/compose.train.yaml ps
docker compose -f docker/docker-compose.yaml -f docker/compose.train.yaml logs --tail=100 backend
```

The rootless Podman user socket and the shared rootful Caddy proxy are separate
ownership domains. Inspect both before changing either one.

## Verification

```sh
curl -fsSI https://etl.home.complete.tech/
curl -ksS -o /dev/null -w '%{http_code}\n' https://etl.home.complete.tech/api/v1/health
ssh router.complete.tech "nslookup etl.home.complete.tech 127.0.0.1"
```

The unauthenticated health request should return `401`; an authenticated health
request should return `200`. The public root should return the Unstract
frontend, with API and WebSocket paths routed by Traefik to the backend.
