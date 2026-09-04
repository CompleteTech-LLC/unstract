# Rootless startup

Podman's generic restart service starts containers in container-ID order. It
does not interpret Compose `depends_on` or wait for database readiness. A Python
service that opens its database while importing its application can exit before
the database has joined the network. A standalone vector database can likewise
fail during dependency startup.

`unstract-compose.service` delegates startup to the existing Docker Compose
provider connected to the user's Podman socket. The script first waits for the
configured database, broker, cache, and object-storage healthchecks, then starts
the rest of the configured project. Dependency failure aborts startup. Its two
phases each have a 300-second default convergence budget.
If `UNSTRACT_START_TIMEOUT` is increased, raise the unit's `TimeoutStartSec`
above twice that value plus configuration/startup overhead. The unit does not
retry a failed stack automatically: inspect the failing dependency, resolve it,
then explicitly restart this unit. Repeated blind starts can obscure the cause.

## Install after reviewing the deployment

Keep the unit disabled until the host's generic restart helper excludes this
exact Compose project from its start path. Preserve the existing shutdown path
and every other project's startup behavior. Otherwise two supervisors race each other
and the original dependency-order problem remains possible. This repository does
not overwrite a host-wide helper. User lingering and the Podman user socket must
already be enabled; installing this unit does not change host policy.

Copy `scripts/start-rootless-stack.sh` into
`~/.local/libexec/unstract/start-rootless-stack.sh` and the unit into
`~/.config/systemd/user/unstract-compose.service`. Create a mode-0600
`~/.config/unstract/compose.env` containing the *existing* project's values:

```ini
UNSTRACT_PROJECT_DIRECTORY=/absolute/path/to/checkout/docker
COMPOSE_FILE=/absolute/path/to/checkout/docker/docker-compose.yaml:/absolute/path/to/checkout/docker/compose.override.yaml
COMPOSE_PROJECT_NAME=existing-project-name
VERSION=existing-deployed-image-tag
```

Preserve any additional deployment variables, image overrides and profiles used
by the established deployment. Do not copy secret values into this document or
publish the environment file. Compose reads the existing service environment
files from the selected checkout. Confirm the selected files with `docker
compose config --quiet`, without logging rendered secrets.

Deploy the reviewed healthcheck definitions through the established narrow
service rollout first. `--no-recreate` deliberately does not apply new checks to
existing containers. It also cannot repair stale OCI runtime state: inspect and
recover that container separately before enabling startup. Keep application
image tags aligned with the actual deployed images.

When deployment overrides replace Traefik's command, retain `--ping=true` so its
local healthcheck is enabled. When Nginx listens on an internal port other than
80, set `FRONTEND_HEALTH_PORT` in the frontend service's environment to that
internal port (for example, `8080`). Changing only a published host port does
not change the internal healthcheck port.

The startup script uses `up --no-recreate --no-build --pull never --wait`:
existing containers retain their images and settings, while a missing service
can be created only from an already available image. This is a startup command,
not an upgrade command. Inspect the configured profiles and service set before
running it. A completed bootstrap service is expected to exit successfully;
other services must converge according to their configured readiness checks.

After reviewing the generic-helper exclusion, run `systemctl --user
daemon-reload`, then `systemctl --user enable --now unstract-compose.service`.
Check the unit result, service readiness, three scheduled healthcheck results,
and the application route. Report boot configuration review separately from an
actual reboot test. No reboot is needed to install the unit.

## Rollback

Back up any prior unit and configuration before replacement. To roll back this
startup integration, disable and stop `unstract-compose.service`, restore those
files and the previous generic-helper configuration, and reload the user daemon.
Stopping this oneshot unit does not stop or remove application containers. Check
the restored startup configuration and live readiness. Retain all volumes and
existing images; never use `down -v` or pruning for this rollback.
