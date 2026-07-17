#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE="${ROOT_DIR}/tests/fixtures/generator/compose.yaml"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "${TEMP_DIR}"' EXIT
OVERRIDE="${TEMP_DIR}/compose.override.yaml"
MODEL="${TEMP_DIR}/model.json"
HOST_DIR="${TEMP_DIR}/host-app"
HOST_MODEL="${TEMP_DIR}/host-model.json"
DOCKERFILE_DIR="${TEMP_DIR}/dockerfile-app"
DOCKERFILE_MODEL="${TEMP_DIR}/dockerfile-model.json"
EXTENDED_OVERRIDE="${TEMP_DIR}/existing.override.yaml"

COMPOSE_PROJECT_NAME=generator-fixture uv run localhost generate \
  --no-input --file "${FIXTURE}" --output "${OVERRIDE}"

COMPOSE_PROJECT_NAME=generator-fixture \
  docker compose --file "${FIXTURE}" --file "${OVERRIDE}" \
  config --format json >"${MODEL}"

python3 - "${MODEL}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as model_file:
    model = json.load(model_file)

web = model["services"]["web"]
assert set(web["networks"]) == {"application", "localhost-proxy"}
assert web["labels"]["traefik.enable"] == "true"
assert web["labels"]["traefik.docker.network"] == "localhost-proxy"
assert web["labels"]["traefik.http.routers.generator-fixture-web.rule"] == (
    "Host(`generator-fixture.localhost`)"
)
assert web["labels"][
    "traefik.http.services.generator-fixture-web.loadbalancer.server.port"
] == "8000"
assert model["networks"]["localhost-proxy"]["external"] is True
assert "localhost-proxy" not in model["services"]["worker"]["networks"]
PY

mkdir "${HOST_DIR}"
(
  cd "${HOST_DIR}"
  COMPOSE_PROJECT_NAME=host-fixture uv run --frozen --project "${ROOT_DIR}" \
    localhost generate --no-input --mode host --port 3000
)
COMPOSE_PROJECT_NAME=host-fixture \
  docker compose --file "${HOST_DIR}/compose.yaml" \
  config --format json >"${HOST_MODEL}"

python3 - "${HOST_MODEL}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as model_file:
    model = json.load(model_file)

app = model["services"]["app"]
assert app["image"] == "caddy:2.11.4-alpine"
assert "http://host.docker.internal:3000" in app["command"]
assert set(app["networks"]) == {"localhost-proxy"}
assert app["labels"][
    "traefik.http.services.host-fixture-app.loadbalancer.server.port"
] == "8080"
assert model["networks"]["localhost-proxy"]["external"] is True
PY

mkdir "${DOCKERFILE_DIR}"
cp "${ROOT_DIR}/tests/fixtures/dockerfile-app/Dockerfile" \
  "${DOCKERFILE_DIR}/Dockerfile"
(
  cd "${DOCKERFILE_DIR}"
  COMPOSE_PROJECT_NAME=dockerfile-fixture uv run --frozen --project "${ROOT_DIR}" \
    localhost generate --no-input --mode dockerfile --port 80
)
COMPOSE_PROJECT_NAME=dockerfile-fixture \
  docker compose --file "${DOCKERFILE_DIR}/compose.yaml" \
  config --format json >"${DOCKERFILE_MODEL}"

python3 - "${DOCKERFILE_MODEL}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as model_file:
    model = json.load(model_file)

app = model["services"]["app"]
assert app["build"]["context"].endswith("dockerfile-app")
assert app["expose"] == ["80"]
assert set(app["networks"]) == {"default", "localhost-proxy"}
assert app["labels"][
    "traefik.http.routers.dockerfile-fixture-app.rule"
] == "Host(`dockerfile-fixture.localhost`)"
assert app["labels"][
    "traefik.http.services.dockerfile-fixture-app.loadbalancer.server.port"
] == "80"
PY

cp "${ROOT_DIR}/tests/fixtures/generator/compose.override.yaml" \
  "${EXTENDED_OVERRIDE}"
COMPOSE_PROJECT_NAME=generator-fixture uv run --frozen localhost generate \
  --no-input --extend --file "${FIXTURE}" --output "${EXTENDED_OVERRIDE}"
test -f "${EXTENDED_OVERRIDE}.bak"
grep -q 'Existing local settings must survive' "${EXTENDED_OVERRIDE}"

COMPOSE_PROJECT_NAME=generator-fixture \
  docker compose --file "${FIXTURE}" --file "${EXTENDED_OVERRIDE}" \
  config --quiet

if COMPOSE_PROJECT_NAME=generator-fixture \
  uv run localhost generate --no-input \
  --file "${FIXTURE}" --output "${OVERRIDE}" >/dev/null 2>&1; then
  printf 'generator overwrote an existing override\n' >&2
  exit 1
fi

printf 'Generator checks passed\n'
