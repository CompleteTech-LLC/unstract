# Guarded Train health deployment

The two Train overlays are deployable without checking out over the dirty Train
source directory:

- `docker/compose.train.worker-healthchecks.yaml` adds the runner and twelve
  worker healthchecks and the two health-port environment additions needed by
  the log consumers.
- `docker/compose.train.healthchecks.yaml` adds read-only probe mounts and
  bounded checks for the eleven core services. Set
  `UNSTRACT_HEALTHCHECK_SOURCE` when the probe is staged outside the live
  checkout; the default remains the repository-relative path for local use.
- `docker/docker-compose-dev-essentials.yaml` uses the bounded
  `postgres-readiness.sh` and `http-readiness.sh` probes for its pgvector,
  Milvus MinIO, and Milvus services while preserving the local SSL entrypoint
  and volume configuration.

`docker/scripts/train_health_deployment_guard.py` is the transaction guard. Its
`capture`, `lock`, and `preflight` commands are read-only. Only `apply` and
`rollback` mutate the host, and both require `--confirm APPLY_UNSTRACT_HEALTH`.
Every command has a finite subprocess timeout and every operation has a finite
monotonic deadline. The guard never builds, pulls, deletes source, resets a
checkout, or runs project-wide `up`/`down` commands.

The target set is fixed at 24 services: runner plus twelve workers, followed
by db, Redis, MinIO, reverse proxy, Qdrant, RabbitMQ, Weaviate, x2text,
platform, backend, and frontend. The completed one-shot `minio-bootstrap`
container is intentionally excluded.

## Candidate preparation

Build the six changed images from a clean detached worktree at the exact
integrated source commit. Run this on an external builder or disposable build
host; do not build from the dirty Train checkout:

```sh
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
CANDIDATE_SHORT="${CANDIDATE_COMMIT:0:8}"
CANDIDATE_VERSION="goal09-${CANDIDATE_SHORT}"
git worktree add --detach /var/tmp/unstract-goal09 "$CANDIDATE_COMMIT"
cd /var/tmp/unstract-goal09
VERSION="$CANDIDATE_VERSION" docker compose -f docker/docker-compose.build.yaml build --pull never \
  backend frontend runner platform-service x2text-service worker-unified
```

The static database, broker, storage, proxy, and vector images are pinned by
the candidate lock as well. Publish or import every image under an immutable
reference without replacing an existing tag. After all 24 references are
available to the rootless Train Podman context, generate the lock; the command
inspects each image and records its full local ID and immutable digest:

```sh
python3 docker/scripts/train_health_deployment_guard.py lock \
  --candidate-source /var/tmp/unstract-goal09 \
  --candidate-version "$CANDIDATE_VERSION" \
  --output /run/user/1000/unstract-goal09/candidate-lock.json \
  --image runner=localhost/unstract/runner:"$CANDIDATE_VERSION" \
  --image backend=localhost/unstract/backend:"$CANDIDATE_VERSION" \
  --image frontend=localhost/unstract/frontend:"$CANDIDATE_VERSION" \
  --image platform-service=localhost/unstract/platform-service:"$CANDIDATE_VERSION" \
  --image worker-pg-orchestrator-api=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-orchestrator-general=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-fileproc=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-callback=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-scheduler=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-metrics=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-log-stream-consumer=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-executor=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-ide-callback=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-notification=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-pg-reaper=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image worker-log-history-scheduler-v2=localhost/unstract/worker-unified:"$CANDIDATE_VERSION" \
  --image db=docker.io/pgvector/pgvector:pg15 \
  --image redis=docker.io/library/redis:7.2.3 \
  --image minio=docker.io/minio/minio:latest \
  --image reverse-proxy=docker.io/library/traefik:v3.6.2 \
  --image qdrant=docker.io/qdrant/qdrant:v1.16.1 \
  --image rabbitmq=docker.io/library/rabbitmq:4.1.0-management \
  --image weaviate=docker.io/semitechnologies/weaviate:1.39.2 \
  --image x2text-service=localhost/unstract/x2text-service:"$CANDIDATE_VERSION"
```

The final lock must include exactly one mapping for every target service and
must contain no placeholder IDs or digests. The static image references should
be the exact names and IDs already captured from the live stack, unless a
deliberate static image change has separately been reviewed. The lock also
contains the candidate commit's tree hash and hashes for the two overlays, the
core and database probes, and the development essentials Compose file. The
guard writes a temporary image override from this lock, so Compose cannot
silently resolve a different registry or tag.

Stage only the committed guard, overlays, and probe into a separate directory
such as `/run/user/1000/unstract-goal09/source`; never copy over the live
checkout and never use `rsync --delete`. The private Train Compose file and
all existing `.env` files stay in their current location. The core overlay's
`UNSTRACT_HEALTHCHECK_SOURCE` points at the staged probe.

## Read-only preflight and bounded apply

Capture a fresh baseline immediately before preflight, even if the earlier
runtime snapshot is available. The capture hashes every dirty source file by
length and SHA-256, and records active PostgreSQL claims/barriers and claimed
queue rows in addition to RabbitMQ ready/unacknowledged messages. All queue
checks are SELECT/list operations; they do not claim, consume, drain, or mutate
work:

```sh
python3 docker/scripts/train_health_deployment_guard.py capture \
  --project-dir /home/completetrain/etl.home.complete.tech \
  --output /run/user/1000/unstract-goal09/baseline.json \
  --operation-timeout 300
```

The capture hashes environment values without printing them, records full
container IDs, image IDs/digests, writable-layer paths, mount source/target/
options, networks, health metadata without health output, source dirty paths,
and queue counts. It must show zero RabbitMQ messages, zero PostgreSQL queue
rows, zero claimed or scheduled queue rows, zero active barriers, and zero
orchestration claims. Terminal result and dedup rows are recorded separately
and are not mistaken for active jobs.

Use the live dirty Compose files plus the staged overlays. This preserves the
local embedding includes, port changes, env files, named volumes, bind mounts,
container names, and network ownership:

```sh
python3 docker/scripts/train_health_deployment_guard.py preflight \
  --baseline /run/user/1000/unstract-goal09/baseline.json \
  --project-dir /home/completetrain/etl.home.complete.tech \
  --candidate-source /run/user/1000/unstract-goal09/source \
  --candidate-lock /run/user/1000/unstract-goal09/candidate-lock.json \
  --probe-source /run/user/1000/unstract-goal09/source/docker/healthchecks/unstract-services.sh \
  --compose-file docker/docker-compose.yaml \
  --compose-file /run/user/1000/unstract-goal09/source/docker/compose.train.worker-healthchecks.yaml \
  --compose-file /run/user/1000/unstract-goal09/source/docker/compose.train.healthchecks.yaml \
  --operation-timeout 1200
```

Preflight refuses a changed dirty-path list, container name, persistent mount
source or access mode, network, candidate image ID/digest, source commit, or
artifact hash. It also refuses missing images and any service outside the
fixed target set. The mutation phase repeats the runtime identity and queue
checks while holding the PostgreSQL advisory deployment lock. The lock
serializes competing guarded deployments; the queue counts are rechecked
immediately before each batch because independently running application
producers cannot be stopped by an advisory lock.

After a fresh quiescence check and automatic `podman commit` backup of each
target container, `apply` recreates only the worker batch, waits for every
worker healthcheck, verifies exact candidate image ID/digest, names, network
attachments, container options, persistent mount sources/options, environment
digests, and queue/active-job state, then reacquires the deployment lock and
rechecks all of those preconditions before the eleven core services. It always
uses `--no-deps --force-recreate --no-build --pull never`. No volumes or images
are pruned and no source file is removed.

The guard holds a local file lock for the entire transaction because replacing
`db` necessarily disconnects a PostgreSQL advisory-lock session. It reacquires
the DB advisory lock before each batch and immediately rechecks source hashes,
untargeted container identities, queue quiescence, and candidate image IDs.

```sh
python3 docker/scripts/train_health_deployment_guard.py apply \
  --confirm APPLY_UNSTRACT_HEALTH \
  --backup-dir /run/user/1000/unstract-goal09/backup \
  --baseline /run/user/1000/unstract-goal09/baseline.json \
  --project-dir /home/completetrain/etl.home.complete.tech \
  --candidate-source /run/user/1000/unstract-goal09/source \
  --candidate-lock /run/user/1000/unstract-goal09/candidate-lock.json \
  --probe-source /run/user/1000/unstract-goal09/source/docker/healthchecks/unstract-services.sh \
  --compose-file docker/docker-compose.yaml \
  --compose-file /run/user/1000/unstract-goal09/source/docker/compose.train.worker-healthchecks.yaml \
  --compose-file /run/user/1000/unstract-goal09/source/docker/compose.train.healthchecks.yaml \
  --operation-timeout 2400
```

## Compensating rollback

The backup directory contains the sanitized baseline, committed backup images,
the old runtime contract, and exact replacement container IDs. If either batch
fails verification, the guard first captures the current IDs and image IDs. It
automatically rolls back only containers whose replacement ID is known and
whose image is the locked candidate; an unexpected or missing ID stops with a
manual-recovery blocker instead of overwriting an external change. The
compensating rollback recreates those services from the committed pre-change
rootfs, disables the newly added healthchecks, and verifies the old image ID,
health state, container options, network, mount/data, environment, source
hashes, and queue state. Persistent named and bind-mounted data is never
removed.

```sh
python3 docker/scripts/train_health_deployment_guard.py rollback \
  --confirm APPLY_UNSTRACT_HEALTH \
  --backup-dir /run/user/1000/unstract-goal09/backup \
  --project-dir /home/completetrain/etl.home.complete.tech \
  --probe-source /run/user/1000/unstract-goal09/source/docker/healthchecks/unstract-services.sh \
  --rollback-compose-file docker/docker-compose.yaml \
  --operation-timeout 1200
```

The current prepared state has no candidate image lock and has not executed
these mutating phases. That is intentional: the live checkout contains dirty
local Compose/embedding changes and the host remains unchanged until an
independent image build, exact lock, fresh quiescence check, and coordinator
authorization are available.
