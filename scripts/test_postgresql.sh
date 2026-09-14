#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${TEST_DATABASE_URL:?Set TEST_DATABASE_URL to the dedicated disposable PostgreSQL test database}"
: "${SCENARIO_DIR:?Set SCENARIO_DIR to a validated scenario package}"
exec "${PYTHON:-python}" -m pytest forwantofanail/tests -m postgresql --require-postgresql "$@"
