# Disk exhaustion recovery

On September 7, the deployment host's root filesystem had zero available bytes.
The PostgreSQL database failed WAL recovery because it could not extend a data
file. After host log recovery freed capacity, the existing stopped database
container was started without replacement or volume changes. `pg_isready`
returned accepting connections and the public application route returned HTTP
200. This verifies database readiness and frontend reachability, not document
processing.

For recurrence, first restore host capacity and inspect the database logs.
Resolve the exact database container ID with `podman inspect <database-container>`,
verify that it is stopped, and start that existing ID. Check
`podman exec <database-container> pg_isready` before checking the public route.
Do not reset WAL, remove volumes, or recreate the database as a disk-space
remedy. Keep unrelated one-shot bootstrap jobs stopped during recovery.
