#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-/opt/kalshi-signal-logger}"
cd "$ROOT_DIR"

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building logger and api images"
docker compose build logger api

echo "==> Running idempotent migrations"
migrations=(
  scripts/migrate_add_backtest_tables.py
  scripts/migrate_add_clc_reversal_observations.py
  scripts/migrate_add_contract_value_bounce.py
  scripts/migrate_add_dcvrb_observations.py
  scripts/migrate_add_followthrough_backtest.py
  scripts/migrate_add_post_move_continuation.py
  scripts/migrate_add_research_runs.py
  scripts/migrate_add_signal_observations.py
  scripts/migrate_add_time_features.py
)

for migration in "${migrations[@]}"; do
  if [[ -f "$migration" ]]; then
    echo "   -> $migration"
    docker compose run --rm logger python "$migration"
  fi
done

echo "==> Restarting services"
docker compose up -d

echo "==> Service status"
docker compose ps

echo "==> Logger health"
docker inspect kalshi_logger --format='RestartCount={{.RestartCount}} ExitCode={{.State.ExitCode}} StartedAt={{.State.StartedAt}}'

if docker compose ps api >/dev/null 2>&1; then
  echo "==> API health"
  docker inspect kalshi_api --format='RestartCount={{.RestartCount}} ExitCode={{.State.ExitCode}} StartedAt={{.State.StartedAt}}'
fi

echo "==> Recent logger errors"
docker compose logs --since "2m" logger | grep -E "Traceback|ERROR|CRITICAL|ProgrammingError|Failed" || true
