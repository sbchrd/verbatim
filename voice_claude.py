"""Dictée push-to-talk (F9 par défaut, modifiable) via Claude dans tmux (WSL).
Maintenir la touche = dicter ; relâcher = le texte transcrit est collé dans le champ
qui a le focus. Application de zone de notification (icône près de l'horloge).
Prérequis Windows: pip install -r requirements.txt
"""
import base64, json, re, time, subprocess, sys, threading, os, keyboard, pyperclip
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
CONFIG = os.path.join(HERE, "config.json")

def load_hotkey():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f).get("hotkey") or "f9"
    except Exception:
        return "f9"

HOTKEY = load_hotkey()  # touche de dictée (modifiable depuis le menu de l'icône)
hooks = []
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
    kill_tree() { [ -n "$1" ] && { pkill -P "$1" 2>/dev/null; kill "$1" 2>/dev/null; }; return 0; }
    while read -r cmd; do
      case "$cmd" in
        hold)
          kill_tree "$LOOP"; kill_tree "$WATCH"
          ( tmux send-keys -t "$TARGET" -l " "
            sleep "${HOLD_DELAY:-0.1}"
            while true; do tmux send-keys -t "$TARGET" -l " "; sleep 0.03; done ) &
          LOOP=$!
          # surveille le pane: READY quand Claude écoute, PANE <base64> à chaque changement
          ( last=""; ready=""
            for i in $(seq 1 400); do
              p=$(tmux capture-pane -t "$TARGET" -p -J)
              if [ "$p" != "$last" ]; then
                last="$p"
                printf 'PANE %s\n' "$(printf '%s' "$p" | base64 -w0)"
              fi
              if [ -z "$ready" ] && printf '%s' "$p" | grep -qi listening; then echo READY; ready=1; fi
              sleep 0.05
            done ) &
          WATCH=$!
          ;;
        stop) kill_tree "$LOOP"; LOOP="" ;;
        unwatch) kill_tree "$WATCH"; WATCH="" ;;
        quit) kill_tree "$LOOP"; kill_tree "$WATCH"; exit 0 ;;
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

def dlog(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

WAVE = re.compile("[▁▂▃▄▅▆▇█]")

def clean_text(pane):
    """Texte transcrit dans la boîte de saisie (sans le texte indicatif ni l'indicateur sonore)."""
    t = WAVE.sub("", extract_input(pane)).strip()
    if t == baseline or t.startswith('Try "'):
        return ""
    return t

# --- état de la dictée en cours ---
dictating = False       # entre l'appui et la fin du traitement
gen = 0                 # incrémenté à chaque appui (invalide un traitement précédent)
cur_text = ""           # dernier texte vu dans Claude
last_change = 0.0       # heure du dernier changement de cur_text
typed = ""              # texte déjà inséré dans le champ actif
field_lock = threading.Lock()

def read_daemon(proc):
    global cur_text, last_change
    for raw in proc.stdout:
        line = raw.strip()
        if line == b"READY" and held:
            overlay.recording()
            say(f"🎙 Parlez maintenant ({stop_hint()})", REC)
        elif line.startswith(b"PANE ") and dictating:
            try:
                pane = base64.b64decode(line[5:]).decode("utf-8", "replace")
            except Exception:
                continue
            t = clean_text(pane)
            if t != cur_text:
                cur_text, last_change = t, time.time()
                dlog(f"texte dans Claude: {t!r}")

def sync_field(target):
    """Met à jour le champ actif pour qu'il contienne `target` (corrige la fin si Claude révise)."""
    global typed
    with field_lock:
        k = 0
        while k < min(len(typed), len(target)) and typed[k] == target[k]:
            k += 1
        for _ in range(len(typed) - k):
            keyboard.send("backspace")
        if target[k:]:
            paste(target[k:])
        typed = target

def field_worker():
    while True:
        time.sleep(0.05)
        target = cur_text
        if dictating and target and target != typed:
            try:
                sync_field(target)
            except Exception as e:
                dlog(f"erreur de mise à jour du champ: {e}")

def daemon_cmd(cmd):
    global daemon
    if daemon is None or daemon.poll() is not None:
        start_daemon()
    daemon.stdin.write(cmd.encode() + b"\n")
    daemon.stdin.flush()
baseline = ""  # contenu de la boîte avant la dictée (ex: texte indicatif « Try ... »)

def start_dictation():
    global held, dictating, gen, cur_text, typed
    held = True
    with field_lock:
        gen += 1
        cur_text = typed = ""
        dictating = True
    say("⏳ Préparation... attendez le rouge pour parler", WORK)
    overlay.prepare()
    daemon_cmd("hold")

def stop_dictation():
    global held
    held = False
    overlay.hide()
    daemon_cmd("stop")
    my_gen = gen
    def finish():
        global dictating, typed
        t0 = time.time()
        say("⏳ Transcription en cours...", WORK)
        # attend que le texte soit complet, stable et entièrement inséré dans le champ
        while time.time() - t0 < 10:
            time.sleep(0.1)
            if gen != my_gen:
                return  # une nouvelle dictée a commencé
            if cur_text and typed == cur_text and time.time() - last_change > 0.6:
                break
        if gen != my_gen:
            return
        out = typed
        say(f"📝 Texte ({time.time()-t0:.1f}s): {out}" if out else "Aucun texte détecté", IDLE)
        dictating = False
        daemon_cmd("unwatch")
        if out:
            wsl("bash", SCRIPT, "clear", TARGET)
            time.sleep(0.3)
            left = extract_input(wsl("bash", SCRIPT, "capture", TARGET).stdout)
            if left and left != baseline and not left.startswith('Try "'):
                say("⚠️ Le message n'a pas pu être effacé dans Claude.")
        else:
            say(f"(aucun texte détecté — détails dans {LOG})")
        typed = ""
    threading.Thread(target=finish, daemon=True).start()

# --- gestes : maintenir (push-to-talk) ou double-appui (mode continu) ---
TAP_MAX = 0.30      # un appui plus court que ça est un « tap »
DOUBLE_WINDOW = 0.40  # délai max entre la fin du 1er tap et le 2e appui
mode = "idle"       # idle | hold | pending (tap relâché, attend un 2e appui) | latched
down = False        # touche physiquement enfoncée (filtre l'auto-repeat)
press_t = 0.0
pending_timer = None
mode_lock = threading.Lock()

def stop_hint():
    return (f"appuyez de nouveau sur {HOTKEY.upper()} pour terminer" if mode == "latched"
            else f"relâchez {HOTKEY.upper()} pour terminer")

def on_press(_):
    global mode, down, press_t, pending_timer
    with mode_lock:
        if down:  # auto-repeat du clavier
            return
        down = True
        if mode == "idle":
            mode, press_t = "hold", time.time()
            start_dictation()
        elif mode == "pending":  # double appui: l'enregistrement continue sans tenir la touche
            if pending_timer:
                pending_timer.cancel()
            mode = "latched"
            say(f"🎙 Mode continu — {stop_hint()}", REC)
        elif mode == "latched":  # appui suivant: on arrête
            mode = "idle"
            stop_dictation()

def on_release(_):
    global mode, down, pending_timer
    with mode_lock:
        if not down:
            return
        down = False
        if mode == "hold":
            if time.time() - press_t < TAP_MAX:  # tap: attend un éventuel 2e appui
                mode = "pending"
                pending_timer = threading.Timer(DOUBLE_WINDOW, expire_pending)
                pending_timer.daemon = True
                pending_timer.start()
            else:  # maintien normal: fin de la dictée
                mode = "idle"
                stop_dictation()

def expire_pending():
    global mode
    with mode_lock:
        if mode == "pending":  # pas de 2e appui: tap simple, on termine
            mode = "idle"
            stop_dictation()

def unbind_hotkey():
    for remove in hooks:  # keyboard.on_*_key renvoie la fonction qui retire le hook
        try:
            remove()
        except Exception:
            pass
    hooks.clear()

def bind_hotkey():
    unbind_hotkey()
    hooks[:] = [keyboard.on_press_key(HOTKEY, on_press, suppress=True),
                keyboard.on_release_key(HOTKEY, on_release, suppress=True)]
    if tray:
        tray.title = f"Prêt — maintenez {HOTKEY.upper()} pour dicter"
        tray.update_menu()

def change_hotkey(icon=None, _item=None):
    """Attend la prochaine touche pressée et en fait la nouvelle touche de dictée (Échap = annuler)."""
    def work():
        global HOTKEY
        global mode, down
        with mode_lock:  # abandonne tout geste en cours
            if held:
                stop_dictation()
            mode, down = "idle", False
        unbind_hotkey()  # libère la touche actuelle pour pouvoir la lire
        say("⌨️ Appuyez sur la nouvelle touche de dictée... (Échap pour annuler)", WORK)
        key = keyboard.read_key(suppress=True)  # suppress: la touche n'est pas tapée dans l'appli active
        if key and key != "esc":
            HOTKEY = key
            try:
                with open(CONFIG, "w", encoding="utf-8") as f:
                    json.dump({"hotkey": HOTKEY}, f)
            except Exception as e:
                say(f"Impossible d'enregistrer la touche: {e}")
        bind_hotkey()
        say(f"Prêt. Maintenez {HOTKEY.upper()} pour dicter.", IDLE)
    threading.Thread(target=work, daemon=True).start()

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
    threading.Thread(target=field_worker, daemon=True).start()
    time.sleep(3)  # laisse Claude afficher sa boîte de saisie pour mémoriser son état au repos
    baseline = extract_input(wsl("bash", SCRIPT, "capture", TARGET).stdout)
    bind_hotkey()
    tray = pystray.Icon("speech-to-text", make_icon(IDLE), f"Prêt — maintenez {HOTKEY.upper()} pour dicter",
                        menu=pystray.Menu(
                            pystray.MenuItem(lambda item: f"Changer la touche (actuelle : {HOTKEY.upper()})",
                                             change_hotkey),
                            pystray.MenuItem("Quitter", quit_app)))
    say(f"Prêt. Maintenez {HOTKEY.upper()} pour dicter.")
    try:
        tray.run()  # bloque jusqu'à « Quitter » (ou Ctrl+C depuis un terminal)
    except KeyboardInterrupt:
        quit_app(tray)
