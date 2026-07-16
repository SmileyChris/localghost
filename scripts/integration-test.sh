#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PROXY_COMPOSE_FILE=${PROXY_COMPOSE_FILE:-"${ROOT_DIR}/compose.yaml"}
EXAMPLE_COMPOSE_FILE="${ROOT_DIR}/examples/compose.yaml"
PROJECT_A=${TEST_PROJECT_A:-ldp-fixture-a}
PROJECT_B=${TEST_PROJECT_B:-ldp-fixture-b}
HOST_PROJECT=${TEST_HOST_PROJECT:-ldp-fixture-host}
DOCKERFILE_PROJECT=${TEST_DOCKERFILE_PROJECT:-ldp-fixture-dockerfile}
DEFAULT_PORT=${TEST_DEFAULT_PORT:-80}
ALTERNATE_PORT=${TEST_ALTERNATE_PORT:-18080}
HOST_APP_PORT=${TEST_HOST_APP_PORT:-19090}
ACTIVE_PORT=${DEFAULT_PORT}
OWNS_RESOURCES=0
HOST_DIR=''
DOCKERFILE_DIR=''
HOST_SERVER_PID=''

log() {
  printf '\n==> %s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

proxy() {
  LOCAL_DEV_PROXY_HTTP_PORT="${ACTIVE_PORT}" \
    docker compose -f "${PROXY_COMPOSE_FILE}" "$@"
}

app() {
  local project=$1
  shift
  COMPOSE_PROJECT_NAME="${project}" \
    docker compose -f "${EXAMPLE_COMPOSE_FILE}" "$@"
}

host_bridge() {
  COMPOSE_PROJECT_NAME="${HOST_PROJECT}" \
    docker compose -f "${HOST_DIR}/compose.yaml" "$@"
}

dockerfile_app() {
  COMPOSE_PROJECT_NAME="${DOCKERFILE_PROJECT}" \
    docker compose -f "${DOCKERFILE_DIR}/compose.yaml" "$@"
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ ${OWNS_RESOURCES} == 1 ]]; then
    if [[ -n ${HOST_DIR} && -f ${HOST_DIR}/compose.yaml ]]; then
      host_bridge down --remove-orphans --volumes >/dev/null 2>&1 || true
    fi
    if [[ -n ${DOCKERFILE_DIR} && -f ${DOCKERFILE_DIR}/compose.yaml ]]; then
      dockerfile_app down --remove-orphans --volumes >/dev/null 2>&1 || true
    fi
    app "${PROJECT_A}" down --remove-orphans --volumes >/dev/null 2>&1 || true
    app "${PROJECT_B}" down --remove-orphans --volumes >/dev/null 2>&1 || true
    proxy down --remove-orphans --volumes >/dev/null 2>&1 || true
  fi
  if [[ -n ${HOST_SERVER_PID} ]]; then
    kill "${HOST_SERVER_PID}" >/dev/null 2>&1 || true
    wait "${HOST_SERVER_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n ${HOST_DIR} ]]; then
    rm -rf "${HOST_DIR}"
  fi
  if [[ -n ${DOCKERFILE_DIR} ]]; then
    rm -rf "${DOCKERFILE_DIR}"
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

assert_equal() {
  local expected=$1
  local actual=$2
  local description=$3
  [[ ${actual} == "${expected}" ]] || \
    fail "${description}: expected '${expected}', got '${actual}'"
}

wait_for_body() {
  local host=$1
  local expected=$2
  local body=''
  local attempt

  for attempt in $(seq 1 30); do
    body=$(curl --noproxy '*' --silent --show-error --max-time 2 \
      --header "Host: ${host}" "http://127.0.0.1:${ACTIVE_PORT}/" 2>/dev/null || true)
    if [[ ${body} == *"${expected}"* ]]; then
      printf '%s' "${body}"
      return 0
    fi
    sleep 1
  done

  fail "${host} did not return a response containing '${expected}'"
}

wait_for_healthy_proxy() {
  local container_id
  local health
  local attempt
  container_id=$(proxy ps -q traefik)
  [[ -n ${container_id} ]] || fail 'Traefik container was not created'

  for attempt in $(seq 1 60); do
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
      "${container_id}")
    if [[ ${health} == healthy ]]; then
      return 0
    fi
    sleep 1
  done

  proxy logs traefik >&2 || true
  fail 'Traefik did not become healthy'
}

assert_loopback_publication() {
  local container_id=$1
  local expected_port=$2
  local publication
  publication=$(docker port "${container_id}" 80/tcp)
  assert_equal "127.0.0.1:${expected_port}" "${publication}" \
    'HTTP entrypoint publication'

  if docker port "${container_id}" 8080/tcp >/dev/null 2>&1; then
    fail 'Traefik raw API port 8080 must not be published'
  fi
}

command -v docker >/dev/null || fail 'docker is required'
command -v curl >/dev/null || fail 'curl is required'
command -v python3 >/dev/null || fail 'python3 is required'
command -v uv >/dev/null || fail 'uv is required'
docker compose version >/dev/null || fail 'Docker Compose is required'
docker info >/dev/null || fail 'A running Docker daemon is required'

for project in local-dev-proxy "${PROJECT_A}" "${PROJECT_B}" "${HOST_PROJECT}" \
  "${DOCKERFILE_PROJECT}"; do
  if [[ -n $(docker ps -aq --filter "label=com.docker.compose.project=${project}") ]]; then
    fail "Compose project '${project}' already exists; remove it before running this destructive integration test"
  fi
done
if docker network inspect local-dev-proxy >/dev/null 2>&1; then
  fail "Docker network 'local-dev-proxy' already exists; remove it before running this destructive integration test"
fi
OWNS_RESOURCES=1

log 'Validate Compose files'
proxy config --quiet
COMPOSE_PROJECT_NAME="${PROJECT_A}" docker compose -f "${EXAMPLE_COMPOSE_FILE}" config --quiet

log 'Prove consumers fail clearly before the external network exists'
missing_network_log=$(mktemp)
if app "${PROJECT_A}" up -d >"${missing_network_log}" 2>&1; then
  fail 'Fixture unexpectedly started without the external proxy network'
fi
if ! grep -q 'local-dev-proxy' "${missing_network_log}" || \
  ! grep -Eqi 'not( be)? found' "${missing_network_log}"; then
  sed -n '1,120p' "${missing_network_log}" >&2
  fail 'Missing-network failure did not identify local-dev-proxy'
fi
rm -f "${missing_network_log}"
app "${PROJECT_A}" down --remove-orphans >/dev/null 2>&1 || true

log 'Start the proxy and verify idempotence and health'
proxy up -d --wait --wait-timeout 90
first_proxy_id=$(proxy ps -q traefik)
proxy up -d --wait --wait-timeout 90
second_proxy_id=$(proxy ps -q traefik)
assert_equal "${first_proxy_id}" "${second_proxy_id}" 'Idempotent proxy container ID'
assert_equal 1 "$(proxy ps -q traefik | wc -l | tr -d ' ')" 'Proxy container count'
assert_equal 1 "$(docker network ls --filter name=^local-dev-proxy$ --format '{{.Name}}' | wc -l | tr -d ' ')" 'Proxy network count'
wait_for_healthy_proxy
assert_loopback_publication "${second_proxy_id}" "${DEFAULT_PORT}"

log 'Start two isolated consumers and verify primary and secondary routing'
app "${PROJECT_A}" up -d
app "${PROJECT_B}" up -d
a_web_id=$(app "${PROJECT_A}" ps -q web)
a_web_hostname=$(docker inspect --format '{{.Config.Hostname}}' "${a_web_id}")
b_web_id=$(app "${PROJECT_B}" ps -q web)
b_web_hostname=$(docker inspect --format '{{.Config.Hostname}}' "${b_web_id}")
b_web_started_at=$(docker inspect --format '{{.State.StartedAt}}' "${b_web_id}")
a_mailpit_id=$(app "${PROJECT_A}" ps -q mailpit)
a_mailpit_hostname=$(docker inspect --format '{{.Config.Hostname}}' "${a_mailpit_id}")

wait_for_body "${PROJECT_A}.localhost" "Hostname: ${a_web_hostname}" >/dev/null
wait_for_body "${PROJECT_B}.localhost" "Hostname: ${b_web_hostname}" >/dev/null
wait_for_body "mailpit.${PROJECT_A}.localhost" "Hostname: ${a_mailpit_hostname}" >/dev/null

log 'Generate and route a bridge to an HTTP application running on the host'
HOST_DIR=$(mktemp -d)
python3 -m http.server "${HOST_APP_PORT}" \
  --bind 0.0.0.0 \
  --directory "${ROOT_DIR}/tests/fixtures/host-app" \
  >"${HOST_DIR}/server.log" 2>&1 &
HOST_SERVER_PID=$!
for attempt in $(seq 1 20); do
  if curl --noproxy '*' --fail --silent --max-time 2 \
    "http://127.0.0.1:${HOST_APP_PORT}/" >/dev/null 2>&1; then
    break
  fi
  if [[ ${attempt} == 20 ]]; then
    fail 'Host fixture HTTP server did not start'
  fi
  sleep 1
done
(
  cd "${HOST_DIR}"
  COMPOSE_PROJECT_NAME="${HOST_PROJECT}" \
    uv run --frozen --project "${ROOT_DIR}" local-dev-proxy generate \
    --no-input --mode host --port "${HOST_APP_PORT}"
)
host_bridge up -d
wait_for_body "${HOST_PROJECT}.localhost" \
  'local-dev-proxy host bridge fixture' >/dev/null

log 'Generate, build, and route an application from a Dockerfile'
DOCKERFILE_DIR=$(mktemp -d)
cp "${ROOT_DIR}/tests/fixtures/dockerfile-app/Dockerfile" \
  "${DOCKERFILE_DIR}/Dockerfile"
(
  cd "${DOCKERFILE_DIR}"
  COMPOSE_PROJECT_NAME="${DOCKERFILE_PROJECT}" \
    uv run --frozen --project "${ROOT_DIR}" local-dev-proxy generate \
    --no-input --mode dockerfile --port 80
)
dockerfile_app up -d --build
dockerfile_app_id=$(dockerfile_app ps -q app)
dockerfile_app_hostname=$(
  docker inspect --format '{{.Config.Hostname}}' "${dockerfile_app_id}"
)
wait_for_body "${DOCKERFILE_PROJECT}.localhost" \
  "Hostname: ${dockerfile_app_hostname}" >/dev/null

# Exercise the operating system's special-use .localhost resolution as well as
# the explicit Host-header checks above.
localhost_body=$(curl --noproxy '*' --fail --silent --show-error --max-time 5 \
  "http://${PROJECT_A}.localhost:${ACTIVE_PORT}/")
[[ ${localhost_body} == *"Hostname: ${a_web_hostname}"* ]] || \
  fail '.localhost resolver request reached the wrong backend'

unlabelled_status=$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 5 --header "Host: unlabelled.${PROJECT_A}.localhost" \
  "http://127.0.0.1:${ACTIVE_PORT}/")
assert_equal 404 "${unlabelled_status}" 'Unlabelled service response status'

log 'Verify the internal dashboard and absence of an insecure API publication'
dashboard_host=traefik.localhost
if [[ ${ACTIVE_PORT} != 80 ]]; then
  dashboard_host="${dashboard_host}:${ACTIVE_PORT}"
fi
dashboard_root_headers=$(curl --noproxy '*' --silent --dump-header - --output /dev/null \
  --max-time 5 --header "Host: ${dashboard_host}" \
  "http://127.0.0.1:${ACTIVE_PORT}/")
[[ ${dashboard_root_headers} == *$'HTTP/1.1 301'* ]] || \
  fail 'Traefik dashboard root did not return a permanent redirect'
expected_dashboard_location="http://${dashboard_host}/dashboard/"
[[ ${dashboard_root_headers} == *$'Location: '"${expected_dashboard_location}"$'\r'* ]] || \
  fail "Traefik dashboard root did not redirect to ${expected_dashboard_location}"
dashboard_status=$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
  --max-time 5 --header "Host: ${dashboard_host}" \
  "http://127.0.0.1:${ACTIVE_PORT}/dashboard/")
assert_equal 200 "${dashboard_status}" 'Traefik dashboard response status'

log 'Remove one consumer without affecting the proxy or the other consumer'
app "${PROJECT_A}" down --remove-orphans
assert_equal "${second_proxy_id}" "$(proxy ps -q traefik)" 'Proxy ID after consumer removal'
wait_for_body "${PROJECT_B}.localhost" "Hostname: ${b_web_hostname}" >/dev/null

log 'Restart and reconcile the proxy without restarting consumers'
proxy restart traefik
wait_for_healthy_proxy
assert_equal "${b_web_id}" "$(app "${PROJECT_B}" ps -q web)" 'Consumer ID after proxy restart'
assert_equal "${b_web_started_at}" \
  "$(docker inspect --format '{{.State.StartedAt}}' "${b_web_id}")" \
  'Consumer start time after proxy restart'
wait_for_body "${PROJECT_B}.localhost" "Hostname: ${b_web_hostname}" >/dev/null

proxy up -d --force-recreate --wait --wait-timeout 90 traefik
wait_for_healthy_proxy
assert_equal "${b_web_id}" "$(app "${PROJECT_B}" ps -q web)" 'Consumer ID after proxy reconcile'
assert_equal "${b_web_started_at}" \
  "$(docker inspect --format '{{.State.StartedAt}}' "${b_web_id}")" \
  'Consumer start time after proxy reconcile'
wait_for_body "${PROJECT_B}.localhost" "Hostname: ${b_web_hostname}" >/dev/null

log 'Recreate the proxy on a non-default loopback port'
proxy stop traefik
proxy rm -f traefik
ACTIVE_PORT=${ALTERNATE_PORT}
proxy up -d --wait --wait-timeout 90
alternate_proxy_id=$(proxy ps -q traefik)
assert_loopback_publication "${alternate_proxy_id}" "${ALTERNATE_PORT}"
assert_equal "${b_web_id}" "$(app "${PROJECT_B}" ps -q web)" 'Consumer ID after port change'
wait_for_body "${PROJECT_B}.localhost" "Hostname: ${b_web_hostname}" >/dev/null

log 'All integration checks passed'
