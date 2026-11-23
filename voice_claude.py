"""Dictée push-to-talk (F9) via Claude dans tmux (WSL).
Maintenir F9 = dicter ; relâcher = le texte transcrit est collé dans le champ
qui a le focus. Application de zone de notification (icône près de l'horloge).
Prérequis Windows: pip install -r requirements.txt
"""
import re, time, subprocess, sys, threading, os, keyboard, pyperclip
import math, ctypes, ctypes.wintypes
import tkinter as tk
import pystray
from PIL import Image, ImageDraw

SESSION = "speech-to-text"
TARGET = f"{SESSION}:0.0"
# Distro WSL à utiliser (le défaut de wsl.exe peut être docker-desktop, sans bash/tmux)
DISTRO = os.environ.get("VOICE_DISTRO", "Ubuntu")
# dossier de l'app (à côté de l'.exe une fois empaqueté avec PyInstaller --onedir)
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # pas de fenêtre console qui clignote
LOG = os.path.join(HERE, "voice_debug.log")
# Script côté WSL, embarqué dans l'exécutable et installé au démarrage
SCRIPT = "/tmp/speech-to-text-hold.sh"
HOLD_SH = r'''#!/bin/bash
# Usage: voice-hold.sh daemon|capture|clear   (exécuté dans WSL)
TARGET="${2:-speech-to-text:0.0}"
PIDFILE="/tmp/voice-hold.pid"

case "$1" in
  daemon)
    # Processus persistant piloté par l'app via stdin (évite ~0,2 s de démarrage de
    # wsl.exe à chaque appui). Il doit rester vivant, sinon WSL tue la boucle d'espaces.
    LOOP=""; WATCH=""
    stop_all() {
      for p in $LOOP $WATCH; do pkill -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; done
      LOOP=""; WATCH=""
    }
    while read -r cmd; do
      case "$cmd" in
        hold)
          stop_all
          ( tmux send-keys -t "$TARGET" -l " "
            sleep "${HOLD_DELAY:-0.1}"
            while true; do tmux send-keys -t "$TARGET" -l " "; sleep 0.03; done ) &
          LOOP=$!
          # signale à l'app quand Claude écoute réellement
          ( for i in $(seq 1 150); do
              tmux capture-pane -t "$TARGET" -p | grep -qi "listening" && { echo READY; break; }
              sleep 0.03
            done ) &
          WATCH=$!
          ;;
        stop) stop_all ;;
        quit) stop_all; exit 0 ;;
      esac
    done
    ;;
  clear)
    # Vide la boîte de saisie de Claude (Ctrl+C une fois = efface la saisie)
    tmux send-keys -t "$TARGET" C-c
    ;;
  capture)
    tmux capture-pane -t "$TARGET" -p -J
    ;;
esac
'''

def wsl(*args, **kw):
    return subprocess.run(["wsl.exe", "-d", DISTRO, *args], capture_output=True, text=True,
                          encoding="utf-8", creationflags=NO_WINDOW, **kw)

tray = None

def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((22, 6, 42, 38), 10, fill=color)       # micro
    d.arc((14, 20, 50, 48), 0, 180, fill=color, width=4)        # support
    d.line((32, 48, 32, 58), fill=color, width=4)
    d.line((22, 58, 42, 58), fill=color, width=4)
    return img

IDLE, REC, WORK = (120, 120, 120, 255), (220, 40, 40, 255), (230, 160, 20, 255)

class Overlay(threading.Thread):
    """Pastille animée en bas à droite de l'écran pendant l'enregistrement.
    Fenêtre sans bordure, toujours au-dessus, transparente aux clics, sans voler le focus."""
    SIZE, KEY = 96, "#010203"

    def __init__(self):
        super().__init__(daemon=True)
        self.state = 0  # 0 = caché, 1 = préparation (ambre), 2 = enregistrement (rouge)
        self.ready = threading.Event()

    def run(self):
        S = self.SIZE
        root = self.root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-transparentcolor", self.KEY, "-alpha", 0.0)
        root.configure(bg=self.KEY)
        # zone de travail (au-dessus de la barre des tâches)
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        root.geometry(f"{S}x{S}+{rect.right - S - 16}+{rect.bottom - S - 16}")
        cv = self.cv = tk.Canvas(root, width=S, height=S, bg=self.KEY, highlightthickness=0)
        cv.pack()
        root.update_idletasks()
        # ne jamais prendre le focus ni intercepter la souris
        # (uniquement sur la fenêtre de premier niveau, pas sur le widget enfant de Tk)
        top = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
        st = ctypes.windll.user32.GetWindowLongW(top, -20)
        ctypes.windll.user32.SetWindowLongW(top, -20, st | 0x20 | 0x80 | 0x08000000)
        self.t0 = time.time()
        self.ready.set()
        self.tick()
        root.mainloop()

    def tick(self):
        cv, S = self.cv, self.SIZE
        on = self.state != 0
        rec = self.state == 2
        col, col_r = ("#dc2828", "{g:02x}1a1a") if rec else ("#e6a014", "{g:02x}{h:02x}14")
        self.root.attributes("-alpha", 0.95 if on else 0.0)
        if on:
            t = time.time() - self.t0
            c = S / 2
            cv.delete("all")
            cv.create_oval(2, 2, S - 2, S - 2, fill="#1e1e1e", outline="")
            # ondes qui s'étendent depuis le centre
            for k in range(2):
                ph = (t * 0.9 + k * 0.5) % 1.0
                r = 20 + 24 * ph
                g = int(220 * (1 - ph) + 30 * ph)
                h = int(160 * (1 - ph) + 30 * ph)
                cv.create_oval(c - r, c - r, c + r, c + r, outline="#" + col_r.format(g=g, h=h), width=3)
            r = 20 + 2.5 * math.sin(t * 6)  # coeur qui « respire »
            cv.create_oval(c - r, c - r, c + r, c + r, fill=col, outline="")
            # micro
            cv.create_rectangle(c - 4, c - 11, c + 4, c + 3, fill="white", outline="", width=0)
            cv.create_oval(c - 4, c - 14, c + 4, c - 6, fill="white", outline="")
            cv.create_oval(c - 4, c - 1, c + 4, c + 7, fill="white", outline="")
            cv.create_arc(c - 8, c - 7, c + 8, c + 9, start=180, extent=180, style="arc", outline="white", width=2)
            cv.create_line(c, c + 9, c, c + 13, fill="white", width=2)
            cv.create_line(c - 4, c + 13, c + 4, c + 13, fill="white", width=2)
        self.root.after(33, self.tick)

    def prepare(self): self.state = 1
    def recording(self): self.state = 2
    def hide(self): self.state = 0

overlay = Overlay()

def say(msg, color=None):
    """Journal + info-bulle de l'icône (+ console si lancée depuis un terminal)."""
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    if sys.stdout:
        try:
            print(msg)
        except Exception:  # console Windows en cp1252 : emojis non affichables
            pass
    if tray:
        tray.title = msg.splitlines()[0][:120]
        if color:
            tray.icon = make_icon(color)

def fatal(msg):
    say(msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "Speech to text", 0x10)
    except Exception:
        pass
    sys.exit(1)

def paste(text):
    """Colle le texte dans le champ qui a le focus (via le presse-papiers, restauré ensuite)."""
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    time.sleep(0.2)
    if old is not None:
        pyperclip.copy(old)

def install_script():
    r = wsl("bash", "-c", f"tr -d '\\r' > {SCRIPT}", input=HOLD_SH.replace("\r\n", "\n"))
    if r.returncode != 0:
        fatal(f"Impossible d'installer le script dans WSL: {r.stderr.strip()}")

def start_claude():
    if wsl("tmux", "has-session", "-t", SESSION).returncode != 0:
        # shell de login: sinon ~/.local/bin (où se trouve claude) n'est pas dans le PATH
        r = wsl("tmux", "new-session", "-d", "-s", SESSION, "bash -lc claude")
        time.sleep(1)
        if r.returncode != 0 or wsl("tmux", "has-session", "-t", SESSION).returncode != 0:
            fatal(f"Impossible de démarrer Claude dans tmux: {r.stderr.strip()}")

RULE = re.compile(r"^\s*[─━—-]{10,}\s*$")

def extract_input(pane):
    """Texte de la boîte de saisie: entre deux lignes pleines, précédé de '❯'."""
    lines = pane.splitlines()
    # dernière paire de lignes pleines encadrant une ligne commençant par un chevron
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].lstrip().startswith(("❯", ">")) and RULE.match(lines[i - 1]):
            body = []
            for l in lines[i:]:
                if RULE.match(l):
                    break
                body.append(l)
            else:
                continue  # pas de ligne fermante: boîte incomplète
            first = body[0].lstrip()[1:].strip()
            rest = [l.strip() for l in body[1:]]
            return " ".join(x for x in [first, *rest] if x)
    return ""

held = False
daemon = None  # processus WSL persistant (boucle d'espaces)

def start_daemon():
    global daemon
    daemon = subprocess.Popen(["wsl.exe", "-d", DISTRO, "bash", SCRIPT, "daemon", TARGET],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    threading.Thread(target=read_daemon, args=(daemon,), daemon=True).start()

def read_daemon(proc):
    for line in proc.stdout:
        if line.strip() == b"READY" and held:
            overlay.recording()
            say("🎙 Parlez maintenant (relâchez F9 pour terminer)", REC)

def daemon_cmd(cmd):
    global daemon
    if daemon is None or daemon.poll() is not None:
        start_daemon()
    daemon.stdin.write(cmd.encode() + b"\n")
    daemon.stdin.flush()
baseline = ""  # contenu de la boîte avant la dictée (ex: texte indicatif « Try ... »)

def on_press(_):
    global held
    if held:  # ignore l'auto-repeat du clavier
        return
    held = True
    say("⏳ Préparation... attendez le rouge pour parler", WORK)
    overlay.prepare()
    daemon_cmd("hold")

def on_release(_):
    global held
    if not held:
        return
    held = False
    overlay.hide()
    daemon_cmd("stop")
    def finish():
        global baseline
        t0 = time.time()
        say("⏳ Transcription en cours...", WORK)
        out = prev = pane = ""
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"\n===== relâche {time.strftime('%H:%M:%S')} =====\n")
            for i in range(30):  # max ~7 s
                pane = wsl("bash", SCRIPT, "capture", TARGET).stdout
                out = extract_input(pane)
                if out == baseline or out.startswith('Try "'):  # texte indicatif ou ancien contenu
                    out = ""
                log.write(f"--- +{time.time()-t0:.1f}s extrait={out!r}\n")
                log.write("\n".join(pane.splitlines()[-8:]) + "\n")
                if out and out == prev:
                    break
                prev = out
                time.sleep(0.2)
        say(f"📝 Texte ({time.time()-t0:.1f}s): {out}" if out else "Aucun texte détecté", IDLE)
        if out:
            paste(out)
            wsl("bash", SCRIPT, "clear", TARGET)
            time.sleep(0.3)
            left = extract_input(wsl("bash", SCRIPT, "capture", TARGET).stdout)
            if left and left != baseline:
                say("⚠️ Le message n'a pas pu être effacé dans Claude.")
        if not out:
            say(f"(aucun texte détecté — détails dans {LOG})")
    threading.Thread(target=finish, daemon=True).start()

def cleanup():
    if daemon and daemon.poll() is None:
        try:
            daemon_cmd("quit")
        except Exception:
            pass
        daemon.terminate()
    wsl("tmux", "kill-session", "-t", SESSION)

def quit_app(icon, _item=None):
    cleanup()
    icon.stop()

if __name__ == "__main__":
    chk = wsl("tmux", "-V")
    if chk.returncode != 0:
        fatal(f"tmux introuvable dans la distro '{DISTRO}': {chk.stderr.strip()}\n"
              "Définissez VOICE_DISTRO (voir: wsl -l -v).")
    overlay.start()
    overlay.ready.wait(5)
    install_script()
    start_claude()
    start_daemon()
    time.sleep(3)  # laisse Claude afficher sa boîte de saisie pour mémoriser son état au repos
    baseline = extract_input(wsl("bash", SCRIPT, "capture", TARGET).stdout)
    keyboard.on_press_key("f9", on_press, suppress=True)
    keyboard.on_release_key("f9", on_release, suppress=True)
    tray = pystray.Icon("speech-to-text", make_icon(IDLE), "Prêt — maintenez F9 pour dicter",
                        menu=pystray.Menu(pystray.MenuItem("Quitter", quit_app)))
    say("Prêt. Maintenez F9 pour dicter.")
    try:
        tray.run()  # bloque jusqu'à « Quitter » (ou Ctrl+C depuis un terminal)
    except KeyboardInterrupt:
        quit_app(tray)
