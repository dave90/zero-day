#!/usr/bin/env bash
#
# run.sh — one-command BountyBench benchmark for `zero`.
#
# Drives BountyBench with `zero` as the agent under test and prints a scorecard.
# Bakes in the environment setup that the run actually needs (learned the hard way):
#   * BountyBench's 3.11 venv/bin first on PATH, so verify.sh's `python3` can import
#     psycopg2 (Homebrew Python 3.14 + PEP 668 otherwise makes the verifier crash and
#     score every exploit "fail").
#   * the ZERO_BB_* knobs the overlay agent reads (backend / model / budget / network);
#   * a clean slate (removes leftover target/sandbox containers from a prior run).
# It also auto-installs the overlay and `zero`/psycopg2 into BB's venv if missing.
#
# Prereqs (one time — see README.md): BountyBench cloned with `./setup.sh --all`, the
# target codebase submodule fetched, Docker running, and the `zero-sandbox` image built.
#
# Usage:
#   ./run.sh                                  # lunary#0 exploit (the proven default)
#   ./run.sh --system lunary --task detect    # a different task type
#   ./run.sh --system lunary --task detect --use-cwe
#   ./run.sh --model claude-sonnet-4-6        # a different model under test
#   ./run.sh --pilot                          # every available bounty in pilot.py
#   BOUNTYBENCH_HOME=/path/to/bb ./run.sh     # or pass --bountybench
#
set -euo pipefail

# --- locate things --------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZERO_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- defaults -------------------------------------------------------------
BB="${BOUNTYBENCH_HOME:-}"
SYSTEM="lunary"
BOUNTY=0
TASK="exploit"
MODEL="claude-opus-4-8[1m]"
BACKEND="claude"
DEADLINE=1800
MAX_TURNS=40
NETWORK="shared_net"
PHASE_ITER=2
PILOT=false
NO_CONTAINER=false
SKIP_SETUP=false
USE_CWE=false

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --bountybench)      BB="$2"; shift 2;;
    --system)           SYSTEM="$2"; shift 2;;
    --bounty)           BOUNTY="$2"; shift 2;;
    --task)             TASK="$2"; shift 2;;
    --model)            MODEL="$2"; shift 2;;
    --backend)          BACKEND="$2"; shift 2;;
    --deadline)         DEADLINE="$2"; shift 2;;
    --max-turns)        MAX_TURNS="$2"; shift 2;;
    --network)          NETWORK="$2"; shift 2;;
    --phase-iterations) PHASE_ITER="$2"; shift 2;;
    --pilot)            PILOT=true; shift;;
    --no-container)     NO_CONTAINER=true; shift;;
    --skip-setup)       SKIP_SETUP=true; shift;;
    --use-cwe)          USE_CWE=true; shift;;
    -h|--help)          usage 0;;
    *) echo "unknown arg: $1" >&2; usage 1;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
note() { echo "[run.sh] $*"; }

# --- resolve + validate BountyBench --------------------------------------
# Auto-detect a checkout in common locations if not given, so `./run.sh` just works.
if [ -z "$BB" ]; then
  for cand in "$ZERO_REPO/../bountybench" "$HOME/git/bountybench" \
              "$HOME/bountybench" "$ZERO_REPO/bountybench" "$PWD/bountybench"; do
    if [ -f "$cand/workflows/runner.py" ]; then BB="$cand"; break; fi
  done
fi
[ -n "$BB" ] || die "no BountyBench checkout found — set \$BOUNTYBENCH_HOME or pass --bountybench <path>"
BB="$(cd "$BB" 2>/dev/null && pwd)" || die "BountyBench path not found: $BB"
note "BountyBench: $BB"
[ -f "$BB/workflows/runner.py" ] || die "$BB is not a BountyBench checkout"
VENV_PY="$BB/venv/bin/python"
[ -x "$VENV_PY" ] || die "no BountyBench venv at $BB/venv (run ./setup.sh --all there)"

# --- prereq checks --------------------------------------------------------
command -v docker >/dev/null || die "docker not found / not running"
docker info >/dev/null 2>&1 || die "docker daemon not reachable"
command -v claude >/dev/null || die "claude CLI not on PATH (needed by the zero backend)"
# Use `docker images -q`, not `docker image inspect`: under Docker Desktop's containerd
# image store, `inspect zero-sandbox:latest` fails on the short name (wants the fully
# qualified docker.io/library/… ref) even though `docker run` resolves it fine — a false
# "image missing". `images -q` resolves the short name correctly, like `docker run` does.
[ -n "$(docker images -q zero-sandbox:latest 2>/dev/null)" ] \
  || die "zero-sandbox:latest image missing — build it: (cd $ZERO_REPO && docker build -t zero-sandbox:latest -f docker/Dockerfile .)"

# --- one-time setup (idempotent) -----------------------------------------
if [ "$SKIP_SETUP" = false ]; then
  # ALWAYS re-sync the overlay, not just when it's missing. install_overlay.py is
  # idempotent (it rmtree+copytree's zero_adapter/ and its runner patch is marker-
  # guarded), and the running benchmark is a COPY of bb_overlay/ — if we only installed
  # when absent, every edit to bb_overlay/ (driver, executor, workflows) would silently
  # never reach the run, and you'd debug a stale overlay. (--skip-setup opts out.)
  note "syncing overlay into BountyBench…"
  python3 "$SCRIPT_DIR/install_overlay.py" --bountybench "$BB" >/dev/null
  if ! "$VENV_PY" -c "import zero" 2>/dev/null; then
    note "installing zero (editable) into BountyBench's venv…"
    "$VENV_PY" -m pip install -q -e "$ZERO_REPO"
  fi
  if ! "$VENV_PY" -c "import psycopg2" 2>/dev/null; then
    note "installing psycopg2-binary into BountyBench's venv (verify.sh needs it)…"
    "$VENV_PY" -m pip install -q psycopg2-binary
  fi
  # docker-py must be able to actually talk to the daemon. BountyBench pins
  # docker==6.1.3, whose http+docker:// transport breaks under requests>=2.32
  # ("Not supported URL scheme http+docker") — and installing zero (above) pulls
  # in a modern requests, so BB's kali_env resource fails to initialize and EVERY
  # bounty errors at phase setup. docker-py 7.x speaks to the daemon with modern
  # requests; upgrade it if the from_env() handshake is broken. (The ML stack
  # already in BB's venv wants requests>=2.33, so we fix docker, not requests.)
  if ! "$VENV_PY" -c "import docker; docker.from_env().version()" >/dev/null 2>&1; then
    note "upgrading docker-py in BountyBench's venv (6.1.3 + modern requests = broken)…"
    "$VENV_PY" -m pip install -q 'docker>=7.1.0'
  fi
fi

# --- environment: BB venv first so verify.sh's python3 has psycopg2 -------
export PATH="$BB/venv/bin:$HOME/.local/bin:/opt/homebrew/bin:$PATH"
PY3="$(command -v python3)"
"$PY3" -c "import psycopg2" 2>/dev/null \
  || die "the python3 verify.sh will use ($PY3) cannot import psycopg2"
note "verify.sh python3 = $("$PY3" --version 2>&1) ($PY3)"

# --- guard: refuse to run if another BountyBench run is in flight ---------
# Two runs racing to stand up the same target (fixed container names + host ports
# 3333/8080/5433) collide and fail repo_setup. Bail out clearly instead.
if pgrep -f "workflows\.runner" >/dev/null 2>&1; then
  die "a BountyBench run is already in progress (workflows.runner). Wait for it, or: pkill -f workflows.runner"
fi

# --- clean slate: tear down any leftover target stack + sandbox/kali -------
if [ -f "$BB/bountytasks/$SYSTEM/docker-compose.yml" ]; then
  ( cd "$BB/bountytasks/$SYSTEM" && docker compose down -v --remove-orphans >/dev/null 2>&1 ) || true
fi
docker ps -aq --filter "name=$SYSTEM" --filter "name=kali_env" --filter "name=zero-" 2>/dev/null \
  | xargs -r docker rm -f >/dev/null 2>&1 || true
# Drop stale engagement dirs so token accounting reflects only this run.
rm -rf "$BB/bountytasks/$SYSTEM/bounties/bounty_$BOUNTY/zero_runs" 2>/dev/null || true

# --- run ------------------------------------------------------------------
STAMP="$(date +%s)"
RESULTS="$SCRIPT_DIR/results/pilot_${STAMP}.jsonl"
mkdir -p "$SCRIPT_DIR/results"

COMMON=( --bountybench "$BB" --backend "$BACKEND" --model "$MODEL"
         --deadline "$DEADLINE" --max-turns "$MAX_TURNS" --network "$NETWORK"
         --phase-iterations "$PHASE_ITER" --results "$RESULTS" )
[ "$NO_CONTAINER" = true ] && COMMON+=( --no-container )
[ "$USE_CWE" = true ] && COMMON+=( --use-cwe )

if [ "$PILOT" = true ]; then
  note "running the full pilot (every available bounty in pilot.py) with model=$MODEL"
  "$PY3" "$SCRIPT_DIR/run_pilot.py" "${COMMON[@]}"
else
  note "running $SYSTEM#$BOUNTY ($TASK) with backend=$BACKEND model=$MODEL deadline=${DEADLINE}s"
  "$PY3" "$SCRIPT_DIR/run_pilot.py" "${COMMON[@]}" \
      --system "$SYSTEM" --bounty-number "$BOUNTY" --task "$TASK"
fi

# --- scorecard ------------------------------------------------------------
echo
"$PY3" "$SCRIPT_DIR/evaluate.py" "$RESULTS" \
    --json-out "$SCRIPT_DIR/results/scorecard_${STAMP}.json"
note "results: $RESULTS"
