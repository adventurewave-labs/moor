#!/usr/bin/env bash
# Moor — end-to-end narrated demo (the PRD's Section 11, executable).
#
# Five acts:
#   1. Baseline: a compliant stack
#   2. The problem: drift happens, Moor SEES it (advise mode)
#   3. Control:   auto mode repairs it
#   4. Resilience: the control plane itself is killed mid-flight and recovers
#   5. GitOps:    an intended declaration change converges the environment
#
# Everything below drives real containers through the real Docker API.
set -euo pipefail

cd "$(dirname "$0")/.."

API="http://localhost:8080"
CLI="docker compose exec -T moor moor"
COMPOSE_FILE="docker-compose.yml"
DETECT_WAIT="${DETECT_WAIT:-9}"

B="\033[1;36m"   # banner
Y="\033[1;33m"   # act
G="\033[0;32m"   # ok
R="\033[1;31m"   # drift
N="\033[0m"      # reset

banner() { echo -e "\n${B}════════════════════════════════════════════════════════════════${N}"; echo -e "${B}  $1${N}"; echo -e "${B}════════════════════════════════════════════════════════════════${N}"; }
act()    { echo -e "\n${Y}── $1 ──────────────────────────────────────────${N}"; }
note()   { echo -e "  ${G}▸${N} $1"; }
warn()   { echo -e "  ${R}▸${N} $1"; }

wait_healthy() {
  note "waiting for the control plane to come up..."
  for _ in $(seq 1 60); do
    if curl -sf "$API/api/health" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  echo "control plane did not become healthy at $API" >&2
  exit 1
}

wait_compliant() {
  for _ in $(seq 1 30); do
    if curl -sf "$API/api/state" | grep -q '"drifting"\|"orphaned"'; then
      sleep 1
      continue
    fi
    if curl -sf "$API/api/state" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

restore_declaration() {
  # Act 5 cleanup: re-comment the replicas block and restore the env var.
  sed -i 's/^    deploy:/    # deploy:/' "$COMPOSE_FILE" 2>/dev/null || true
  sed -i 's/^      replicas: 3/    #   replicas: 3/' "$COMPOSE_FILE" 2>/dev/null || true
  sed -i 's/MOOR_TIER: canary/MOOR_TIER: frontend/' "$COMPOSE_FILE" 2>/dev/null || true
}
trap restore_declaration EXIT

banner "MOOR — desired-state control plane for Docker environments"
note "dashboard:   http://localhost:8080     ← open this now"
note "alert sink:  http://localhost:9099"
note "CLI verbs:   make status | plan | apply | watch | chaos"

# ------------------------------------------------------------------ act 1
act "ACT 1 — Baseline: the world as declared"
docker compose up -d
wait_healthy
sleep 6
$CLI status

banner "ACT 2 — The problem: reality drifts (Moor in ADVISE mode)"
note "three real drifts, injected through the Docker API:"
warn "1. the web container is killed (stays dead — no restart policy)"
warn "2. cache is rogue-scaled from 1 to 4 replicas"
warn "3. db's POSTGRES_PASSWORD is mutated by hand"
echo
docker compose run --rm drift-injector inject kill --service web
docker compose run --rm drift-injector inject scale --service cache --count 4
docker compose run --rm drift-injector inject env --service db --key POSTGRES_PASSWORD --value rogue
echo
note "waiting ${DETECT_WAIT}s for the 5s reconcile loop to catch it..."
sleep "$DETECT_WAIT"
echo
$CLI status
echo
$CLI plan
echo
ALERTS=$(curl -s http://localhost:9099/api/alerts | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])' 2>/dev/null || echo "?")
note "alert sink received ${ALERTS} webhook card(s) — see http://localhost:9099"
warn "nothing was repaired: that is today's status quo — visibility without control."

# ------------------------------------------------------------------ act 3
banner "ACT 3 — Control: arm auto-remediation"
$CLI mode auto
echo
note "injecting drift again — and this time Moor repairs it:"
docker compose run --rm drift-injector inject kill --service web
docker compose run --rm drift-injector inject scale --service cache --count 4
echo
note "waiting for detection + remediation..."
for _ in $(seq 1 40); do
  if curl -sf "$API/api/state" | grep -q '"drifting"\|"orphaned"'; then sleep 1; continue; fi
  break
done
wait_compliant
$CLI status
echo
$CLI events 12
note "kill a container by hand, watch it come back correct — in seconds."

# ------------------------------------------------------------------ act 4
banner "ACT 4 — Resilience: kill the control plane mid-flight"
note "injecting drift, then restarting the Moor container itself:"
docker compose run --rm drift-injector inject kill --service web
docker compose restart moor >/dev/null
wait_healthy
note "Moor is back; its event store survived in moor-db."
note "waiting for the loop to detect and repair..."
sleep 6
for _ in $(seq 1 40); do
  if curl -sf "$API/api/state" | grep -q '"drifting"\|"orphaned"'; then sleep 1; continue; fi
  break
done
wait_compliant
$CLI status
note "audit trail continuity across the restart:"
$CLI events 8

# ------------------------------------------------------------------ act 5
banner "ACT 5 — GitOps: an intended change converges"
note "editing the declaration (what git push would do):"
note "  · MOOR_TIER frontend → canary      (env change)"
note "  · cache replicas 1 → 3             (scale-up)"
sed -i 's/MOOR_TIER: frontend/MOOR_TIER: canary/' "$COMPOSE_FILE"
sed -i 's/^    # deploy:/    deploy:/' "$COMPOSE_FILE"
sed -i 's/^    #   replicas: 3/      replicas: 3/' "$COMPOSE_FILE"
echo
note "Moor classifies this as an INTENDED change and converges forward..."
sleep "$DETECT_WAIT"
for _ in $(seq 1 40); do
  if curl -sf "$API/api/state" | grep -q '"drifting"\|"orphaned"'; then sleep 1; continue; fi
  break
done
wait_compliant
$CLI status
echo
note "converged: the live environment now runs the new declaration."
note "proof — the web container carries the new env:"
docker compose exec -T web sh -c 'echo "   MOOR_TIER=$MOOR_TIER"' 2>/dev/null \
  || docker exec moordemo-web-1 sh -c 'echo "   MOOR_TIER=$MOOR_TIER"'
note "proof — cache now runs 3 replicas:"
docker compose ps cache --format '{{.Name}}  {{.State}}' 2>/dev/null || docker ps --filter name=moordemo-cache

banner "DEMO COMPLETE — every act ran against the real Docker Engine"
note "PRD:      Moor_PRD.pdf (Section 11 maps 1:1 to these acts)"
note "explore:  make chaos        (random drift, auto mode will eat it)"
note "          make watch        (live event stream)"
note "          make logs         (control-plane logs)"
echo
