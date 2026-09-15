#!/usr/bin/env bash

set -Eeuo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_SCHEMA:?POSTGRES_SCHEMA is required}"

echo "Configuring pgvector database '$POSTGRES_DB' and schema '$POSTGRES_SCHEMA'"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set schema="$POSTGRES_SCHEMA" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS :"schema";
SQL
