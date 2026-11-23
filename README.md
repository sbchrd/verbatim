# Verbatim — voice dictation for Windows (via Claude Code)

A push-to-talk dictation app for Windows: **hold a key (F9 by default), speak, release**, and the transcribed text is pasted into whichever text field has focus.

Transcription is done by the voice mode of [Claude Code](https://claude.com/claude-code), running in a hidden **tmux** session inside **WSL**. The app simulates the held space key (Claude Code's dictation gesture), reads the text that appears in Claude's input box, pastes it where you were typing, then clears the input box.

## How it works

1. On launch, the app starts a tmux session named `verbatim` that runs `claude` in WSL (no visible window).
2. When you **press** the dictation key:
   - an animated dot appears at the bottom right of the screen: **amber** while preparing, **green** when Claude is actually listening. Start speaking once it turns green.
3. When you **release** the key:
   - the app waits for Claude to finish transcribing, then clears Claude's input.

Text is inserted **as you speak**: every change to the text in Claude is mirrored into the active field (pasted with `Ctrl+V`, original clipboard restored). If Claude revises a passage that was already transcribed, the app erases it with backspaces and replaces it.

The app lives in the notification area (icon near the clock). It never takes focus.

## Requirements

- Windows 10/11 with **WSL 2**
- In the WSL distribution (Ubuntu by default):
  - **tmux**
  - **Claude Code** installed, with **voice mode enabled** (`/voice`), and the project folder trusted (the trust confirmation dialog is not visible)
- If your default WSL distribution is not the one containing tmux and Claude (for example `docker-desktop`), set the `VOICE_DISTRO` environment variable to the name of the right distribution (see `wsl -l -v`).

No Python installation is needed to use the executable.

## Usage

1. Build or obtain `Verbatim.exe` (see below) and launch it.
2. Click in the text field where you want to write.
3. Hold **F9**, wait for the green dot, speak, then release.

**Continuous (hands-free) mode**: **double-tap** the key (two quick presses in a row). Recording continues without holding the key, until you press the same key **once more**. A single short tap is simply ignored (the dictation ends with no text).

### Tray icon menu (right-click)

- **Change key (current: F9)**: press the new key (Esc to cancel). It is saved in `config.json` next to the executable. Delete that file to go back to F9.
- **Debug log** (checkbox): off by default. When checked, the app writes `voice_debug.log` next to the executable. The setting is stored in `config.json`.
- **Quit**: stops any dictation in progress, closes the tmux session and exits the app.

### Generated files

- `voice_debug.log`: debug log, created only if the option is enabled in the menu. Useful when troubleshooting.
- `config.json`: the chosen dictation key and the debug log state.

## Building the executable

Requirement: Python 3 on Windows (needed only for building).

```bat
build.bat
```

This installs the dependencies (`requirements.txt`) and produces `dist\Verbatim.exe`, a single file with no console.

To run directly from source:

```bat
pip install -r requirements.txt
pythonw voice_claude.py
```

## Technical notes

- The held space key is simulated by a small persistent WSL process, driven through stdin, that sends spaces to tmux (`tmux send-keys`). It reports `READY` as soon as Claude displays `listening…`.
- Text is extracted from Claude's input box: the line starting with `❯`, framed by two full-width rules (`────`). Claude's gray placeholder text (`Try "…"`) is ignored.
- The WSL-side script is embedded in `voice_claude.py` and reinstalled on every launch at `/tmp/verbatim-hold.sh`.

## Limitations

- The executable is not signed: Windows SmartScreen or Defender may show a warning.
- The global keyboard hook may not work in windows running as administrator, unless the app is too.
- Text extraction depends on the look of Claude Code's interface; an update to it may require adjusting the patterns in `extract_input()`.
