#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# run-now.sh
# Runs the publisher immediately — same as the scheduled job, but on demand.
# Use this to test, or to publish outside the Wednesday schedule.
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/blog-publisher.log"

echo "🚀  Running Creator Hero publisher..."
echo "    Log: $LOG"
echo ""

cd "$SCRIPT_DIR" && python3 publish.py --site creator-hero 2>&1 | tee -a "$LOG"
