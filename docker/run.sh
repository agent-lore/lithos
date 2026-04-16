#!/usr/bin/env bash
# Launch/manage a Lithos stack for a given environment.
#
# Usage:
#   ./run.sh <env> [action]
#
#   env     One of: prod, staging, fuzz (matches docker/.env.<env>)
#   action  up      Build and start the stack in detached mode (default)
#           down    Stop and remove the stack
#           logs    Tail container logs (Ctrl-C to detach)
#           status  Show running containers for this project
#           restart Shortcut for down + up
#
# Examples:
#   ./run.sh prod            # build & start production
#   ./run.sh staging up      # same, explicit
#   ./run.sh fuzz down       # stop the fuzz stack
#   ./run.sh staging logs    # follow staging logs

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

env_name="${1:-}"
action="${2:-up}"

if [[ -z "${env_name}" ]]; then
    echo "Error: environment name is required" >&2
    echo "Usage: $0 <prod|staging|fuzz> [up|down|logs|status|restart]" >&2
    exit 1
fi

env_file="${SCRIPT_DIR}/.env.${env_name}"
if [[ ! -f "${env_file}" ]]; then
    echo "Error: env file not found: ${env_file}" >&2
    exit 1
fi

project_name="lithos-${env_name}"
compose_args=(-p "${project_name}" --env-file "${env_file}")

cd "${SCRIPT_DIR}"

case "${action}" in
    up)
        docker compose "${compose_args[@]}" up -d --build
        ;;
    down)
        docker compose "${compose_args[@]}" down
        ;;
    restart)
        docker compose "${compose_args[@]}" down
        docker compose "${compose_args[@]}" up -d --build
        ;;
    logs)
        docker compose "${compose_args[@]}" logs -f
        ;;
    status)
        docker compose "${compose_args[@]}" ps
        ;;
    *)
        echo "Error: unknown action '${action}'" >&2
        echo "Valid actions: up, down, restart, logs, status" >&2
        exit 1
        ;;
esac
