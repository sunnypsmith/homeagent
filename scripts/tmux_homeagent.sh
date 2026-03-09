#!/usr/bin/env bash
set -euo pipefail

SESSION="${HOMEAGENT_TMUX_SESSION:-homeagent}"

usage() {
  cat <<'EOF'
Usage:
  scripts/tmux_homeagent.sh            # create (or attach) session
  scripts/tmux_homeagent.sh --kill     # kill session

Layout:
  Window 0: core       — time-trigger, sonos-gateway, event-recorder, watchdog
  Window 1: agents     — wakeup, morning-briefing, hourly-chime, fixed-announcements
  Window 2: integr     — camect, caseta, camera-lighting, monitor
  Window 3: briefings  — hourly-house-check, exec-briefing, ui-gateway, voice-service
  Window 4: shell      — free shell for ad-hoc commands

Environment:
  HOMEAGENT_TMUX_SESSION  Session name (default: homeagent)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed." >&2
  exit 1
fi

if [[ "${1:-}" == "--kill" ]]; then
  tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"
  exit 0
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  exec tmux attach -t "$SESSION"
fi

run() {
  # $1 = tag, $2... = command
  local tag="$1"; shift
  printf "export PYTHONDONTWRITEBYTECODE=1; cd /workspace && %s 2>&1 | sed -u 's/^/[%s] /'\n" "$*" "$tag"
}

# NFS cache bust: force attribute revalidation for all Python sources
# and remove any leaked __pycache__ dirs (belt-and-suspenders for actimeo=0)
find /workspace/src -name '*.py' -exec stat {} + > /dev/null 2>&1 || true
find /workspace -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

tmux new-session -d -s "$SESSION" -n core
tmux set-environment -t "$SESSION" PYTHONDONTWRITEBYTECODE 1
tmux set-option -g -t "$SESSION" pane-border-status top
tmux set-option -g -t "$SESSION" pane-border-format "#{pane_title}"

# ── Window 0: core (2x2) ──────────────────────────────────────────
tmux split-window -h -t "$SESSION:0"
tmux split-window -v -t "$SESSION:0.0"
tmux split-window -v -t "$SESSION:0.1"
tmux select-layout -t "$SESSION:0" tiled

tmux select-pane -t "$SESSION:0.0" -T "time-trigger"
tmux select-pane -t "$SESSION:0.1" -T "sonos-gateway"
tmux select-pane -t "$SESSION:0.2" -T "event-recorder"
tmux select-pane -t "$SESSION:0.3" -T "watchdog"

tmux send-keys -t "$SESSION:0.0" "$(run time-trigger   'home-agent time-trigger')" C-m
tmux send-keys -t "$SESSION:0.1" "$(run sonos-gateway   'home-agent sonos-gateway')" C-m
tmux send-keys -t "$SESSION:0.2" "$(run event-recorder  'home-agent event-recorder')" C-m
tmux send-keys -t "$SESSION:0.3" "$(run watchdog        'home-agent watchdog')" C-m

# ── Window 1: agents (2x2) ────────────────────────────────────────
tmux new-window -t "$SESSION:1" -n agents
tmux split-window -h -t "$SESSION:1"
tmux split-window -v -t "$SESSION:1.0"
tmux split-window -v -t "$SESSION:1.1"
tmux select-layout -t "$SESSION:1" tiled

tmux select-pane -t "$SESSION:1.0" -T "wakeup-agent"
tmux select-pane -t "$SESSION:1.1" -T "morning-briefing"
tmux select-pane -t "$SESSION:1.2" -T "hourly-chime"
tmux select-pane -t "$SESSION:1.3" -T "fixed-announcements"

tmux send-keys -t "$SESSION:1.0" "$(run wakeup-agent        'home-agent wakeup-agent')" C-m
tmux send-keys -t "$SESSION:1.1" "$(run morning-briefing     'home-agent morning-briefing-agent')" C-m
tmux send-keys -t "$SESSION:1.2" "$(run hourly-chime         'home-agent hourly-chime-agent')" C-m
tmux send-keys -t "$SESSION:1.3" "$(run fixed-announcements  'home-agent fixed-announcement-agent')" C-m

# ── Window 2: integrations (2x2) ──────────────────────────────────
tmux new-window -t "$SESSION:2" -n integr
tmux split-window -h -t "$SESSION:2"
tmux split-window -v -t "$SESSION:2.0"
tmux split-window -v -t "$SESSION:2.1"
tmux select-layout -t "$SESSION:2" tiled

tmux select-pane -t "$SESSION:2.0" -T "camect-agent"
tmux select-pane -t "$SESSION:2.1" -T "caseta-agent"
tmux select-pane -t "$SESSION:2.2" -T "camera-lighting"
tmux select-pane -t "$SESSION:2.3" -T "monitor"

tmux send-keys -t "$SESSION:2.0" "$(run camect           'home-agent camect-agent')" C-m
tmux send-keys -t "$SESSION:2.1" "$(run caseta           'home-agent caseta-agent')" C-m
tmux send-keys -t "$SESSION:2.2" "$(run camera-lighting  'home-agent camera-lighting-agent')" C-m
tmux send-keys -t "$SESSION:2.3" "$(run monitor          'home-agent monitor')" C-m

# ── Window 3: briefings + ui + voice (2x2) ────────────────────────
tmux new-window -t "$SESSION:3" -n services
tmux split-window -h -t "$SESSION:3"
tmux split-window -v -t "$SESSION:3.0"
tmux split-window -v -t "$SESSION:3.1"
tmux select-layout -t "$SESSION:3" tiled

tmux select-pane -t "$SESSION:3.0" -T "house-check"
tmux select-pane -t "$SESSION:3.1" -T "exec-briefing"
tmux select-pane -t "$SESSION:3.2" -T "ui-gateway"
tmux select-pane -t "$SESSION:3.3" -T "voice-service"

tmux send-keys -t "$SESSION:3.0" "$(run hourly-house-check  'home-agent hourly-house-check-agent')" C-m
tmux send-keys -t "$SESSION:3.1" "$(run exec-briefing       'home-agent exec-briefing-agent')" C-m
tmux send-keys -t "$SESSION:3.2" "$(run ui-gateway          'home-agent ui-gateway')" C-m
tmux send-keys -t "$SESSION:3.3" "$(run voice               'home-agent voice-service')" C-m

# ── Window 4: voice + intent (2 panes) ────────────────────────────
tmux new-window -t "$SESSION:4" -n voice
tmux split-window -v -t "$SESSION:4"
tmux select-pane -t "$SESSION:4.0" -T "voice-intent"
tmux select-pane -t "$SESSION:4.1" -T "voice-logs"

tmux send-keys -t "$SESSION:4.0" "$(run voice-intent 'home-agent voice-intent-agent')" C-m
tmux send-keys -t "$SESSION:4.1" "cd /workspace" C-m

# ── Window 5: shell ───────────────────────────────────────────────
tmux new-window -t "$SESSION:5" -n shell
tmux select-pane -t "$SESSION:5.0" -T "shell"
tmux send-keys -t "$SESSION:5.0" "cd /workspace" C-m

tmux select-window -t "$SESSION:0"
exec tmux attach -t "$SESSION"
