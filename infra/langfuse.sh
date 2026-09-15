#!/usr/bin/env sh
# Self-hosted Langfuse, as its OWN compose project, from the official langfuse/langfuse repo.
#
#   ./infra/langfuse.sh            # fetch compose file (if absent) and start
#   ./infra/langfuse.sh pull       # re-download the compose file
#   ./infra/langfuse.sh down       # stop (keeps volumes)
#   LANGFUSE_REF=v3.x.y ./infra/langfuse.sh   # pin a tag/branch/sha (default: main)
#
# How the api reaches it: Langfuse's compose publishes langfuse-web on host port 3000 (and binds
# its other services to 127.0.0.1). The api container reaches that through the host:
# LANGFUSE_HOST=http://host.docker.internal:3000, with `extra_hosts: host-gateway` in our
# docker-compose.yml so the name also resolves on Linux. No ports beyond what Langfuse's own
# compose publishes, and no shared Docker network, so our stack starts with or without Langfuse.
#
# Tension: "only nginx publishes a port" holds for THIS app's compose project. Langfuse is a
# separate, operator-facing tool and its compose publishes :3000 (and loopback-only ports for
# minio/clickhouse/postgres/redis). See README "Tracing with Langfuse".
#
# Before exposing this anywhere, edit infra/langfuse/docker-compose.yml: every value marked
# `# CHANGEME` (NEXTAUTH_SECRET, SALT, ENCRYPTION_KEY, database/minio/redis/clickhouse passwords).

set -eu

REF="${LANGFUSE_REF:-main}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HERE/langfuse"
FILE="$DIR/docker-compose.yml"
URL="https://raw.githubusercontent.com/langfuse/langfuse/${REF}/docker-compose.yml"
PROJECT="langfuse"

fetch() {
  mkdir -p "$DIR"
  echo "Downloading $URL"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$FILE.tmp"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$FILE.tmp" "$URL"
  else
    echo "need curl or wget" >&2
    exit 1
  fi
  mv "$FILE.tmp" "$FILE"
  echo "Saved $FILE (ref: $REF)"
}

case "${1:-up}" in
  pull)
    fetch
    ;;
  down)
    docker compose -p "$PROJECT" -f "$FILE" down
    ;;
  up)
    [ -f "$FILE" ] || fetch
    if grep -q "CHANGEME" "$FILE"; then
      echo "WARNING: $FILE still contains CHANGEME defaults. Fine for local use only." >&2
    fi
    docker compose -p "$PROJECT" -f "$FILE" up -d
    echo
    echo "Langfuse UI: http://localhost:3000  — create a project, then set LANGFUSE_PUBLIC_KEY /"
    echo "LANGFUSE_SECRET_KEY in .env and restart the api: docker compose up -d api"
    ;;
  *)
    echo "usage: $0 [up|pull|down]" >&2
    exit 2
    ;;
esac
