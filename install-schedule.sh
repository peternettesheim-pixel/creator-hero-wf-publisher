#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# install-schedule.sh
# Installs the Creator Hero blog publisher as a weekly macOS Launch Agent.
# Runs automatically every Wednesday at 11:00 am (your Mac's local time).
#
# Usage:  bash install-schedule.sh
# ─────────────────────────────────────────────────────────────────────────────

PLIST_NAME="com.influencer-hero.blog-publisher.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_DEST="$LAUNCH_AGENTS/$PLIST_NAME"

echo "═══════════════════════════════════════════════════"
echo "  Creator Hero Blog Publisher — Schedule Installer"
echo "═══════════════════════════════════════════════════"

# 1. Check plist file exists
if [ ! -f "$PLIST_SRC" ]; then
    echo "❌  Plist not found: $PLIST_SRC"
    exit 1
fi

# 2. Create LaunchAgents folder if needed
mkdir -p "$LAUNCH_AGENTS"

# 3. Unload existing job if already installed (ignore errors)
launchctl unload "$PLIST_DEST" 2>/dev/null || true

# 4. Copy plist to LaunchAgents
cp "$PLIST_SRC" "$PLIST_DEST"
echo "✅  Plist copied to $PLIST_DEST"

# 5. Load the job
launchctl load "$PLIST_DEST"
echo "✅  Launch Agent loaded — will run every Wednesday at 11:00 am"

# 6. Confirm it is loaded
if launchctl list | grep -q "com.influencer-hero.blog-publisher"; then
    echo "✅  Confirmed active: com.influencer-hero.blog-publisher"
else
    echo "⚠️   Could not confirm — check Console.app for errors"
fi

echo ""
echo "📋  Log files:"
echo "    Output : ~/Library/Logs/blog-publisher.log"
echo "    Errors : ~/Library/Logs/blog-publisher-error.log"
echo ""
echo "To run manually right now:"
echo "    bash \"$SCRIPT_DIR/run-now.sh\""
echo ""
echo "To uninstall:"
echo "    launchctl unload \"$PLIST_DEST\" && rm \"$PLIST_DEST\""
