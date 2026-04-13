#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
PLIST_NAME="com.santibm.claude-telegram-bridge"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo "=== Claude Code Telegram Bridge ==="
echo ""

# --- Virtual environment ---
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
fi

echo "Installing dependencies..."
"$VENV/bin/pip" install -q -r "$DIR/requirements.txt"

# --- .env ---
if [ ! -f "$DIR/.env" ]; then
    cp "$DIR/.env.example" "$DIR/.env"
    echo "Created .env — you need to edit it:"
    echo "  $DIR/.env"
    echo ""
fi

# --- launchd plist ---
echo "Generating launchd plist..."

cat > "$PLIST_DST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/python</string>
        <string>$DIR/bridge.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DIR/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/logs/stderr.log</string>
</dict>
</plist>
EOF

mkdir -p "$DIR/logs"

echo ""
echo "=== Done ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Create a Telegram bot:"
echo "     Open Telegram → search @BotFather → /newbot"
echo "     Copy the token into .env → TELEGRAM_BOT_TOKEN"
echo ""
echo "  2. Run manually first to get your user ID:"
echo "     $VENV/bin/python $DIR/bridge.py"
echo "     Then send /id to your bot in Telegram"
echo "     Copy the ID into .env → ALLOWED_USER_IDS"
echo "     Ctrl+C and restart"
echo ""
echo "  3. Once working, enable auto-start:"
echo "     launchctl load $PLIST_DST"
echo ""
echo "  To stop:  launchctl unload $PLIST_DST"
echo "  Logs:     tail -f $DIR/logs/stderr.log"
