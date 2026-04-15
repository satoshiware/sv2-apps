#!/usr/bin/env bash
set -u

source "$HOME/.cargo/env"

APP_DIR="/Users/baveetsinghhora/Desktop/stratumv2/sv2-apps/miner-apps/translator"
CFG="/Users/baveetsinghhora/Desktop/stratumv2/config/tproxy-local.toml"
LOG_DIR="/Users/baveetsinghhora/Desktop/stratumv2/logs"

mkdir -p "$LOG_DIR"

while true; do
echo "[$(date '+%F %T')] starting translator..." | tee -a "$LOG_DIR/translator-supervisor.log"
cd "$APP_DIR" || exit 1
cargo run --release -- -c "$CFG" 2>&1 | tee -a "$LOG_DIR/translator.log"
rc=${PIPESTATUS[0]}

echo "[$(date '+%F %T')] translator exited with code $rc; restarting in 5s..." | tee -a "$LOG_DIR/translator-supervisor.log"
sleep 5
done
