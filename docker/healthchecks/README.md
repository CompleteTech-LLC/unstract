# Unstract service health probes

`unstract-services.sh` is a bounded, read-only probe entrypoint for the core
services in the Train Compose deployment.  It is kept separate from the base
Compose file so a Train-specific deployment can bind-mount it read-only and
select the service name in that service's native Podman healthcheck.

The probe returns zero only after the service-specific readiness contract has
passed.  It never prints response bodies, environment values, or credentials;
failure output names only the service whose probe failed.  The helper uses the
clients already present in the target images.  In particular, Qdrant's minimal
image has no curl/wget, so its check uses the Bash runtime and the official
`/healthz` endpoint with a 512-byte response bound.

The Train deployment should mount this file at
`/usr/local/bin/unstract-services.sh` with `:ro`, then use for example:

```yaml
healthcheck:
  test: ["CMD-SHELL", "/usr/local/bin/unstract-services.sh backend"]
  interval: 30s
  timeout: 5s
  start_period: 120s
  retries: 3
```

The tracked `docker/compose.train.healthchecks.yaml` overlay mounts this probe
read-only and supplies the eleven core service checks.  The companion
`docker/compose.train.worker-healthchecks.yaml` overlay supplies the runner and
twelve worker checks while preserving the Train checkout's local Compose
changes.  Apply both after the Train-only `docker/compose.train.yaml` file.  The
script's endpoint and command paths can be overridden with environment
variables for isolated contract tests; production defaults target service-local
listeners.
