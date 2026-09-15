#!/usr/bin/env bash

set -Eeuo pipefail

# The local pgvector container uses a self-signed certificate because it is a
# loopback-only development service. The adapter uses sslmode=require, which
# encrypts the connection without requiring a separately distributed CA.
if [[ "${1:-}" == "postgres" ]]; then
    ssl_dir="${POSTGRES_SSL_DIR:-/var/lib/postgresql/ssl}"
    cert_file="${POSTGRES_SSL_CERT_FILE:-${ssl_dir}/server.crt}"
    key_file="${POSTGRES_SSL_KEY_FILE:-${ssl_dir}/server.key}"
    common_name="${POSTGRES_SSL_COMMON_NAME:-postgres-vector}"
    cert_days="${POSTGRES_SSL_CERT_DAYS:-3650}"

    mkdir -p "${ssl_dir}"
    if [[ "$(id -u)" -eq 0 ]]; then
        chown postgres:postgres "${ssl_dir}"
        chmod 700 "${ssl_dir}"
    fi

    if [[ ! -s "${cert_file}" || ! -s "${key_file}" ]]; then
        if [[ "$(id -u)" -ne 0 ]]; then
            echo "Postgres SSL certificate is missing and the entrypoint is not running as root" >&2
            exit 1
        fi

        temporary_dir="$(mktemp -d "${ssl_dir}/.postgres-ssl.XXXXXX")"
        trap 'rm -rf "${temporary_dir}"' EXIT

        openssl req -new -x509 -nodes -sha256 \
            -days "${cert_days}" \
            -subj "/CN=${common_name}" \
            -addext "subjectAltName=DNS:${common_name},DNS:localhost,IP:127.0.0.1" \
            -keyout "${temporary_dir}/server.key" \
            -out "${temporary_dir}/server.crt"

        chown postgres:postgres "${temporary_dir}/server.key" "${temporary_dir}/server.crt"
        chmod 600 "${temporary_dir}/server.key"
        chmod 644 "${temporary_dir}/server.crt"
        mv -f "${temporary_dir}/server.key" "${key_file}"
        mv -f "${temporary_dir}/server.crt" "${cert_file}"
    fi

    if [[ "$(id -u)" -eq 0 ]]; then
        chown postgres:postgres "${cert_file}" "${key_file}"
        chmod 600 "${key_file}"
        chmod 644 "${cert_file}"
    fi

    set -- "$@" \
        -c "ssl=on" \
        -c "ssl_cert_file=${cert_file}" \
        -c "ssl_key_file=${key_file}"
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
