#!/bin/bash
# Usage: voice-hold.sh hold|stop|capture|clear   (exécuté dans WSL)
TARGET="${2:-speech-to-text:0.0}"
PIDFILE="/tmp/voice-hold.pid"

case "$1" in
  hold)
    # Reste au premier plan: le processus appelant (wsl.exe) doit rester vivant,
    # sinon WSL tue la boucle dès que wsl.exe se termine.
    echo $$ > "$PIDFILE"
    tmux send-keys -t "$TARGET" -l " "
    sleep 0.5
    while true; do tmux send-keys -t "$TARGET" -l " "; sleep 0.03; done
    ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      pkill -P "$(cat "$PIDFILE")" 2>/dev/null
      kill "$(cat "$PIDFILE")" 2>/dev/null
      rm -f "$PIDFILE"
    fi
    ;;
  clear)
    # Vide la boîte de saisie de Claude (Ctrl+C une fois = efface la saisie)
    tmux send-keys -t "$TARGET" C-c
    ;;
  capture)
    tmux capture-pane -t "$TARGET" -p -J
    ;;
esac
