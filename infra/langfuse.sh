#!/usr/bin/env sh
# Self-hosted Langfuse, as its OWN compose project, from the official langfuse/langfuse repo.
#
#   ./infra/langfuse.sh            # fetch compose file (if absent) and start
#   ./infra/langfuse.sh pull       # re-download the compose file
#   ./infra/langfuse.sh down       # stop (keeps volumes)
#   LANGFUSE_REF=v3.x.y ./infra/langfuse.sh   # pin a tag/branch/sha (default: main)
#
# The official compose file is used UNMODIFIED, with infra/langfuse.override.yml on top:
#   - every upstream port mapping is reset, so nginx :80 stays the only published port;
#   - langfuse-web joins this app's `app` network (pinned outside the trusted-proxy range), so
#     the api reaches it at LANGFUSE_HOST=http://langfuse-web:3000 and nginx serves the UI at
#     http://langfuse.localhost.
# Start the app stack first (`docker compose up -d`): it creates the network Langfuse joins.
#
# Before exposing this anywhere, edit infra/langfuse/docker-compose.yml: every value marked
# `# CHANGEME` (NEXTAUTH_SECRET, SALT, ENCRYPTION_KEY, database/minio/redis/clickhouse passwords).

set -eu

REF="${LANGFUSE_REF:-main}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HERE/langfuse"
FILE="$DIR/docker-compose.yml"
OVERRIDE="$HERE/langfuse.override.yml"
NETWORK="pr-review-agent_app"
URL="https://raw.githubusercontent.com/langfuse/langfuse/${REF}/docker-compose.yml"
# Namespaced on purpose: a generic "langfuse" project name collides with any other Langfuse the
# operator already runs, and `up` would recreate THEIR containers with this override (and newer
# upstream images, which migrate their data).
PROJECT="pr-review-agent-langfuse"

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
    docker compose -p "$PROJECT" -f "$FILE" -f "$OVERRIDE" down
    ;;
  up)
    [ -f "$FILE" ] || fetch
    if grep -q "CHANGEME" "$FILE"; then
      echo "WARNING: $FILE still contains CHANGEME defaults. Fine for local use only." >&2
    fi
    if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
      echo "network $NETWORK not found: start the app first with 'docker compose up -d'" >&2
      exit 1
    fi
    # Refuse to adopt containers this script did not create.
    foreign="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT"       --format '{{.Label "com.docker.compose.project.config_files"}}' | grep -v "langfuse.override.yml" | head -n 1 || true)"
    if [ -n "$foreign" ]; then
      echo "compose project $PROJECT exists but was not started by this script ($foreign); refusing" >&2
      exit 1
    fi
    docker compose -p "$PROJECT" -f "$FILE" -f "$OVERRIDE" up -d
    echo
    echo "Langfuse UI: http://langfuse.localhost  (via nginx; no port published)"
    echo "Create a project, set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in .env, then: docker compose up -d api"
    ;;
  *)
    echo "usage: $0 [up|pull|down]" >&2
    exit 2
    ;;
esac
