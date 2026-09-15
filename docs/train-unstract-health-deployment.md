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

`docker/scripts/train_health_deployment_guard.py` is the transaction guard.
`capture` and `lock` are read-only. `preflight` verifies the candidate and
refreshes private mode-600 replay files beside the lock, so it is an intentional
state write. `start`, `apply`, and `rollback` can mutate the running Compose
project; `start` requires `--confirm START_UNSTRACT_HEALTH`, while `apply` and
`rollback` require `--confirm APPLY_UNSTRACT_HEALTH`. Every command has a
finite subprocess timeout and every operation has a finite monotonic deadline.
The guard never builds, pulls, deletes source, resets a checkout, or runs
project-wide destructive `down` commands.

The target set is fixed at 24 services: runner plus twelve workers, followed
by db, Redis, MinIO, reverse proxy, Qdrant, RabbitMQ, Weaviate, x2text,
platform, backend, and frontend. The completed one-shot `minio-bootstrap`
container is intentionally excluded.

## Candidate preparation

Build the two changed application images from a clean detached worktree at the
exact integrated source commit. The runner source and shared worker source are
the only image-bearing changes. Runner and the twelve worker services use those
new image builds; the eleven core services use immutable image IDs/digests
already captured from the live stack. Run this on an external builder or
disposable build host; do not build from the dirty Train checkout:

```sh
CANDIDATE_COMMIT="$(git rev-parse HEAD)"
CANDIDATE_SHORT="${CANDIDATE_COMMIT:0:8}"
CANDIDATE_VERSION="goal09-${CANDIDATE_SHORT}"
git worktree add --detach /var/tmp/unstract-goal09 "$CANDIDATE_COMMIT"
cd /var/tmp/unstract-goal09
VERSION="$CANDIDATE_VERSION" docker compose -f docker/docker-compose.build.yaml build --pull never \
  runner worker-unified
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
  --image backend=localhost/unstract/backend:all-active-prs-20260903-oauthfix-3a273af \
  --image frontend=localhost/unstract/frontend:all-active-prs-20260903-oauthfix-a4da8ff \
  --image platform-service=localhost/unstract/platform-service:all-active-prs-20260903-7b95921 \
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
  --image x2text-service=localhost/unstract/x2text-service:all-active-prs-20260903-7b95921
```

The final lock must include exactly one mapping for every target service and
must contain no placeholder IDs or digests. The static image references should
be the exact names and IDs already captured from the live stack, unless a
deliberate static image change has separately been reviewed. The lock also
contains the candidate commit's tree hash and hashes for the two overlays, the
core and database probes, and the development essentials Compose file. The
guard writes private mode-600 image, settings, runtime-environment, and
replay-manifest files from
this lock, so every health, environment, and image replay uses the same
immutable references, `VERSION`, and staged probe path. The replay manifest
binds those three private files, the candidate lock, source commit/tree, probe
digest, reviewed environment-key set, and a retained Compose snapshot. That
snapshot copies every Compose file and literal `include` target into a
daemon-visible tree with the original relative layout, stores the live env
file and private overrides in the same tree, and places the probe at a stable
helper mount path. Snapshot files and directories are non-writable and every
entry is hash-checked before Compose starts. These artifacts remain in the
preflight state directory and apply backup directory for later startup or
recovery; the guard validates their SHA-256 values before each Compose
invocation. Only `runner` and the twelve
worker services point at the new build; backend, frontend, platform-service,
x2text-service, and the seven core data services point at the captured static
references.

Stage only the committed guard, overlays, and probe into a separate directory
such as `/run/user/1000/unstract-goal09/source`; never copy over the live
checkout and never use `rsync --delete`. The private Train Compose file and
all existing `.env` files stay in their current location. The core overlay's
`UNSTRACT_HEALTHCHECK_SOURCE` points at the staged probe.

## Existing Train startup owner

The Train user manager currently has two relevant owners. The enabled
`train-rootless-boot-recovery.service` runs the host's bounded helper, whose
contract is to validate and start only the exact rows in its private manifest;
that helper deliberately does not run Compose. The enabled generic
`podman-restart.service` starts existing containers selected by restart policy.
That generic path is how Unstract's existing `unless-stopped` containers can
return after a user-manager restart, but it does not read the candidate lock,
the staged overlays, or the durable replay files. A deployment that installs
only the guard therefore still loses its candidate state on ordinary recovery.

This repository now carries the small owner integration:

- `docker/systemd/unstract-durable-replay.service` runs the guarded whole-stack
  `start` path after the bounded rootless recovery and before generic restart.
- `docker/systemd/podman-restart.service.d/60-unstract-durable-replay.conf`
  makes the ordering a requirement even when an operator starts
  `podman-restart.service` manually.
- `docker/scripts/unstract_durable_replay.py` validates the mode-600 systemd
  environment contract and invokes the guard without a shell.

The unit is deliberately separate from the application checkout. Install the
launcher, unit, and drop-in only after `apply` has produced a verified backup
directory. The environment file below contains paths and the explicit startup
confirmation token; it contains no credentials or copied `.env` values:

```sh
REMOTE_ROOT=/home/completetrain/train-health-coverage-20260908/unstract-build-03404993-2042
REMOTE_SOURCE="$REMOTE_ROOT/guard-source-4ee386a0"
REMOTE_LOCK="$REMOTE_ROOT/candidate-lock-4ee386a0.json"
REMOTE_BACKUP="$REMOTE_ROOT/deployment-backup-4ee386a0"
LIVE_ROOT=/home/completetrain/etl.home.complete.tech
LIVE_ENV="$LIVE_ROOT/docker/.env"
REMOTE_PROBE="$REMOTE_SOURCE/docker/healthchecks/unstract-services.sh"

install -d -m700 "$HOME/.config/unstract" "$HOME/.local/libexec"
umask 077
cat >"$HOME/.config/unstract/durable-replay.env.new" <<EOF
UNSTRACT_GUARD=$REMOTE_SOURCE/docker/scripts/train_health_deployment_guard.py
UNSTRACT_STATE_DIR=$REMOTE_BACKUP
UNSTRACT_PROJECT_DIR=$LIVE_ROOT
UNSTRACT_LIVE_ENV_FILE=$LIVE_ENV
UNSTRACT_BASELINE=$REMOTE_BACKUP/baseline.json
UNSTRACT_CANDIDATE_SOURCE=$REMOTE_SOURCE
UNSTRACT_CANDIDATE_LOCK=$REMOTE_LOCK
UNSTRACT_PROBE_SOURCE=$REMOTE_PROBE
UNSTRACT_COMPOSE_BASE=$LIVE_ROOT/docker/docker-compose.yaml
UNSTRACT_COMPOSE_TRAIN=$LIVE_ROOT/docker/compose.train.yaml
UNSTRACT_COMPOSE_WORKER_HEALTHCHECKS=$REMOTE_SOURCE/docker/compose.train.worker-healthchecks.yaml
UNSTRACT_COMPOSE_CORE_HEALTHCHECKS=$REMOTE_SOURCE/docker/compose.train.healthchecks.yaml
UNSTRACT_COMPOSE_ENV_DIR=$LIVE_ROOT/docker
UNSTRACT_OPERATION_TIMEOUT=2400
UNSTRACT_START_CONFIRM=START_UNSTRACT_HEALTH
EOF
chmod 600 "$HOME/.config/unstract/durable-replay.env.new"
mv -f "$HOME/.config/unstract/durable-replay.env.new" "$HOME/.config/unstract/durable-replay.env"

install -m755 "$REMOTE_SOURCE/docker/scripts/unstract_durable_replay.py" \
  "$HOME/.local/libexec/unstract-durable-replay.py.new"
mv -f "$HOME/.local/libexec/unstract-durable-replay.py.new" \
  "$HOME/.local/libexec/unstract-durable-replay.py"
install -m644 "$REMOTE_SOURCE/docker/systemd/unstract-durable-replay.service" \
  "$HOME/.config/systemd/user/unstract-durable-replay.service.new"
mv -f "$HOME/.config/systemd/user/unstract-durable-replay.service.new" \
  "$HOME/.config/systemd/user/unstract-durable-replay.service"
install -d -m755 "$HOME/.config/systemd/user/podman-restart.service.d"
install -m644 "$REMOTE_SOURCE/docker/systemd/podman-restart.service.d/60-unstract-durable-replay.conf" \
  "$HOME/.config/systemd/user/podman-restart.service.d/60-unstract-durable-replay.conf.new"
mv -f "$HOME/.config/systemd/user/podman-restart.service.d/60-unstract-durable-replay.conf.new" \
  "$HOME/.config/systemd/user/podman-restart.service.d/60-unstract-durable-replay.conf"
systemctl --user daemon-reload
systemd-analyze --user verify unstract-durable-replay.service podman-restart.service
systemctl --user enable unstract-durable-replay.service
```

The launcher sets `PWD` to `UNSTRACT_COMPOSE_ENV_DIR` before `exec`, so the
existing live `.env` can resolve its `${PWD}` path interpolation even when the
user manager starts the unit from a different working directory. Manual
`capture`, `preflight`, and `apply` commands must likewise export
`PWD=$LIVE_ROOT/docker` as shown below.

The install must preserve fresh backups of any prior env, launcher, unit, and
drop-in before replacement. If the owner integration must be reverted, stop
the durable unit, disable it, restore those exact backups, remove the drop-in
only if it was absent before, and run `systemctl --user daemon-reload`; do not
fall back to an unguarded `podman-restart.service` while the candidate state is
uncertain. A failed durable unit intentionally blocks the generic restart path
until the state or source hashes are repaired. Starting the unit is a normal
Compose startup mutation and is a separate approved action after installation.

## Preflight and bounded apply

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

Use the live dirty Compose files plus the staged overlays. Preflight freezes
those files, their literal includes, referenced env and bind files, the live
`docker/.env`, and the health probe into a retained daemon-visible snapshot.
Runtime data directories remain at their original paths through validated
snapshot passthroughs. This preserves the local embedding includes, port
changes, env files, named volumes, bind mounts, container names, and network
ownership. The live `docker/.env` derives `TOOL_REGISTRY_CONFIG_SRC_PATH` from
`PWD`, so preserve the Train Compose working-directory value while the guard
still passes the project root explicitly to Compose:

```sh
export PWD=/home/completetrain/etl.home.complete.tech/docker
python3 docker/scripts/train_health_deployment_guard.py preflight \
  --baseline /run/user/1000/unstract-goal09/baseline.json \
  --project-dir /home/completetrain/etl.home.complete.tech \
  --candidate-source /run/user/1000/unstract-goal09/source \
  --candidate-lock /run/user/1000/unstract-goal09/candidate-lock.json \
  --probe-source /run/user/1000/unstract-goal09/source/docker/healthchecks/unstract-services.sh \
  --compose-file docker/docker-compose.yaml \
  --compose-file docker/compose.train.yaml \
  --compose-file /run/user/1000/unstract-goal09/source/docker/compose.train.worker-healthchecks.yaml \
  --compose-file /run/user/1000/unstract-goal09/source/docker/compose.train.healthchecks.yaml \
  --operation-timeout 1200
```

Preflight refuses a changed dirty-path list, container name, persistent mount
source or access mode, network, candidate image ID/digest, source commit, or
artifact hash. It also binds Compose and direct Podman to the rootless
`/run/user/1000/podman/podman.sock` context, rejects duplicate service labels,
and checks the exact trusted health command and timing for every target. It
refuses missing images and any service outside the fixed target set. Each queue
capture requires three consecutive zero-work samples at two-second intervals;
the mutation phase takes a final settled sample immediately before each
targeted Compose change while holding the PostgreSQL advisory deployment lock.

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
export PWD=/home/completetrain/etl.home.complete.tech/docker
python3 docker/scripts/train_health_deployment_guard.py apply \
  --confirm APPLY_UNSTRACT_HEALTH \
  --backup-dir /run/user/1000/unstract-goal09/backup \
  --baseline /run/user/1000/unstract-goal09/baseline.json \
  --project-dir /home/completetrain/etl.home.complete.tech \
  --candidate-source /run/user/1000/unstract-goal09/source \
  --candidate-lock /run/user/1000/unstract-goal09/candidate-lock.json \
  --probe-source /run/user/1000/unstract-goal09/source/docker/healthchecks/unstract-services.sh \
  --compose-file docker/docker-compose.yaml \
  --compose-file docker/compose.train.yaml \
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
