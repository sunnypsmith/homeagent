#!/usr/bin/env bash
set -euo pipefail

SESSION="${HOMEAGENT_TMUX_SESSION:-homeagent}"

usage() {
  cat <<'EOF'
Usage:
  scripts/tmux_homeagent.sh            # create (or attach) session
  scripts/tmux_homeagent.sh --kill     # kill session

Environment:
  HOMEAGENT_TMUX_SESSION  Session name (default: homeagent)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed. Install it first (Debian/Ubuntu): apt-get update && apt-get install -y tmux" >&2
  exit 1
fi

if [[ "${1:-}" == "--kill" ]]; then
  tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"
  exit 0
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  exec tmux attach -t "$SESSION"
fi

cmd() {
  # $1 = tag, $2... = command
  local tag="$1"
  shift
  # Run in repo root, prefix output with a stable tag.
  printf "export PATH=\"\\$HOME/.local/bin:\\$PATH\"; cd /workspace && %s 2>&1 | sed -u 's/^/[%s] /'\n" "$*" "$tag"
}

# Create session and windows.
tmux new-session -d -s "$SESSION" -n core

# Pane titles in the border (much easier to scan).
tmux set-option -g -t "$SESSION" pane-border-status top
tmux set-option -g -t "$SESSION" pane-border-format "#{pane_title}"

#
# Window 0: core (2x2) -- fits smaller terminals
#
tmux split-window -h -t "$SESSION:0"
tmux split-window -v -t "$SESSION:0.0"
tmux split-window -v -t "$SESSION:0.1"
tmux select-layout -t "$SESSION:0" tiled

tmux select-pane -t "$SESSION:0.0" -T "time-trigger"
tmux select-pane -t "$SESSION:0.1" -T "sonos-gateway"
tmux select-pane -t "$SESSION:0.2" -T "event-recorder"
tmux select-pane -t "$SESSION:0.3" -T "shell"

tmux send-keys -t "$SESSION:0.0" "$(cmd time-trigger 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent time-trigger')" C-m
tmux send-keys -t "$SESSION:0.1" "$(cmd sonos-gateway 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent sonos-gateway')" C-m
tmux send-keys -t "$SESSION:0.2" "$(cmd event-recorder 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent event-recorder')" C-m
tmux send-keys -t "$SESSION:0.3" "cd /workspace" C-m

#
# Window 1: agents (2x2)
#
tmux new-window -t "$SESSION:1" -n agents
tmux split-window -h -t "$SESSION:1"
tmux split-window -v -t "$SESSION:1.0"
tmux split-window -v -t "$SESSION:1.1"
tmux select-layout -t "$SESSION:1" tiled

tmux select-pane -t "$SESSION:1.0" -T "wakeup-agent"
tmux select-pane -t "$SESSION:1.1" -T "morning-briefing"
tmux select-pane -t "$SESSION:1.2" -T "hourly-chime"
tmux select-pane -t "$SESSION:1.3" -T "fixed-announcements"

tmux send-keys -t "$SESSION:1.0" "$(cmd wakeup-agent 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent wakeup-agent')" C-m
tmux send-keys -t "$SESSION:1.1" "$(cmd morning-briefing 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent morning-briefing-agent')" C-m
tmux send-keys -t "$SESSION:1.2" "$(cmd hourly-chime 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent hourly-chime-agent')" C-m
tmux send-keys -t "$SESSION:1.3" "$(cmd fixed-announcements 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent fixed-announcement-agent')" C-m

#
# Window 2: integrations (2x2)
#
tmux new-window -t "$SESSION:2" -n integrations
tmux split-window -h -t "$SESSION:2"
tmux split-window -v -t "$SESSION:2.0"
tmux split-window -v -t "$SESSION:2.1"
tmux select-layout -t "$SESSION:2" tiled

tmux select-pane -t "$SESSION:2.0" -T "camect-agent"
tmux select-pane -t "$SESSION:2.1" -T "caseta-agent"
tmux select-pane -t "$SESSION:2.2" -T "camera-lighting"
tmux select-pane -t "$SESSION:2.3" -T "shell"

tmux send-keys -t "$SESSION:2.0" "$(cmd camect 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent camect-agent')" C-m
tmux send-keys -t "$SESSION:2.1" "$(cmd caseta 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent caseta-agent')" C-m
tmux send-keys -t "$SESSION:2.2" "$(cmd camera-lighting 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent camera-lighting-agent')" C-m
tmux send-keys -t "$SESSION:2.3" "cd /workspace" C-m

#
# Window 3: ui (single pane)
#
tmux new-window -t "$SESSION:3" -n ui
tmux select-pane -t "$SESSION:3.0" -T "ui-gateway"
tmux send-keys -t "$SESSION:3.0" "$(cmd ui-gateway 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent ui-gateway')" C-m

#
# Window 4: checks + exec briefing (2 panes)
#
tmux new-window -t "$SESSION:4" -n checks
tmux split-window -v -t "$SESSION:4"
tmux select-pane -t "$SESSION:4.0" -T "hourly-house-check"
tmux select-pane -t "$SESSION:4.1" -T "exec-briefing"
tmux send-keys -t "$SESSION:4.0" "$(cmd hourly-house-check 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent hourly-house-check-agent')" C-m
tmux send-keys -t "$SESSION:4.1" "$(cmd exec-briefing 'HOME_AGENT_LOG_LEVEL=DEBUG home-agent exec-briefing-agent')" C-m

tmux select-window -t "$SESSION:0"
exec tmux attach -t "$SESSION"

