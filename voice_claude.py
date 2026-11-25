"""Dictée push-to-talk (F9 par défaut, modifiable) via Claude dans tmux (WSL).
Maintenir la touche = dicter ; relâcher = le texte transcrit est collé dans le champ
qui a le focus. Application de zone de notification (icône près de l'horloge).
Prérequis Windows: pip install -r requirements.txt
"""
import base64, json, re, time, subprocess, sys, threading, os, keyboard, pyperclip
import math, ctypes, ctypes.wintypes
import pystray
from PIL import Image, ImageDraw

SESSION = "verbatim"
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

def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config():
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump({"hotkey": HOTKEY, "debug": DEBUG}, f)
    except Exception:
        pass

_cfg = load_config()
HOTKEY = _cfg.get("hotkey") or "f9"   # touche de dictée (modifiable depuis le menu de l'icône)
DEBUG = bool(_cfg.get("debug"))       # journal de debug (désactivé par défaut)
hooks = []
# Script côté WSL, embarqué dans l'exécutable et installé au démarrage
SCRIPT = "/tmp/verbatim-hold.sh"
HOLD_SH = r'''#!/bin/bash
# Usage: voice-hold.sh daemon|capture|clear   (exécuté dans WSL)
TARGET="${2:-verbatim:0.0}"
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
          # surveille le pane: READY quand Claude écoute, PANE <base64> à chaque changement.
          # Pas de limite de durée (une dictée longue doit rester suivie) : la boucle tourne
          # jusqu'à « unwatch »/« hold »/« quit », ou jusqu'à la mort du démon (kill -0 $$).
          ( last=""; ready=""
            while kill -0 $$ 2>/dev/null; do
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
    # stdin fermé (l'app s'est arrêtée) : ne laisse rien tourner derrière
    kill_tree "$LOOP"; kill_tree "$WATCH"
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

IDLE, REC, WORK = (120, 120, 120, 255), (34, 197, 94, 255), (230, 160, 20, 255)

# --- Pastille animée (fenêtre Win32 à transparence par pixel => bords lissés) ---
from ctypes import wintypes as wt
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # rendu net sur écrans à échelle > 100 %
except Exception:
    pass
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HANDLE,
                                   wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.GetDC.argtypes = [wt.HWND]; user32.GetDC.restype = wt.HDC
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
                                       wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]; gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HANDLE
gdi32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]; gdi32.SelectObject.restype = wt.HANDLE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]; kernel32.GetModuleHandleW.restype = wt.HINSTANCE

GREEN = (34, 197, 94)
AMBER = (230, 160, 20)

class Overlay(threading.Thread):
    """Pastille animée en bas à droite de l'écran pendant l'enregistrement.
    Fenêtre sans bordure, toujours au-dessus, transparente aux clics, sans voler le focus.
    Dessinée avec PIL en suréchantillonnage (anti-alias) et affichée via UpdateLayeredWindow."""
    BASE, SS = 96, 4  # taille logique (px) et facteur de suréchantillonnage

    def __init__(self):
        super().__init__(daemon=True)
        self.state = 0  # 0 = caché, 1 = préparation (ambre), 2 = écoute (vert)
        self.ready = threading.Event()

    def render(self, t, color, S):
        SS = self.SS
        N = S * SS
        f = N / 96.0
        c = N / 2
        img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((2 * f, 2 * f, N - 2 * f, N - 2 * f), fill=(24, 24, 24, 235))
        for k in range(2):  # ondes qui s'étendent et s'estompent (vrai alpha)
            ph = (t * 0.9 + k * 0.5) % 1.0
            r = (20 + 24 * ph) * f
            layer = Image.new("RGBA", (N, N), (0, 0, 0, 0))
            ImageDraw.Draw(layer).ellipse((c - r, c - r, c + r, c + r),
                                          outline=color + (int(230 * (1 - ph)),), width=max(1, int(3 * f)))
            img = Image.alpha_composite(img, layer)
        d = ImageDraw.Draw(img)
        r = (20 + 2.5 * math.sin(t * 6)) * f  # cœur qui « respire »
        d.ellipse((c - r, c - r, c + r, c + r), fill=color + (255,))
        w = (255, 255, 255, 255)  # micro
        d.rounded_rectangle((c - 4.5 * f, c - 14 * f, c + 4.5 * f, c + 3 * f), radius=4.5 * f, fill=w)
        d.arc((c - 8 * f, c - 7 * f, c + 8 * f, c + 9 * f), 0, 180, fill=w, width=int(2 * f))
        d.line((c, c + 9 * f, c, c + 13 * f), fill=w, width=int(2 * f))
        d.line((c - 4 * f, c + 13 * f, c + 4 * f, c + 13 * f), fill=w, width=int(2 * f))
        return img.resize((S, S), Image.LANCZOS)

    def run(self):
        cls = "VerbatimOverlay"
        wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.lpszClassName = wndproc, cls
        wc.hInstance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(wc))
        try:
            scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            scale = 1.0
        S = int(self.BASE * scale)
        rect = wt.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        margin = int(16 * scale)
        pos = wt.POINT(rect.right - S - margin, rect.bottom - S - margin)
        # LAYERED | TOPMOST | TOOLWINDOW | NOACTIVATE | TRANSPARENT (clics traversants)
        ex = 0x80000 | 0x8 | 0x80 | 0x08000000 | 0x20
        hwnd = user32.CreateWindowExW(ex, cls, "Verbatim", 0x80000000, pos.x, pos.y, S, S,
                                      None, None, wc.hInstance, None)
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), S, -S, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject(mem_dc, bmp)
        size, src = wt.SIZE(S, S), wt.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, alpha par pixel
        msg, visible, t0 = wt.MSG(), False, time.time()
        self.ready.set()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            st = self.state
            if st:
                img = self.render(time.time() - t0, GREEN if st == 2 else AMBER, S)
                r_, g_, b_, a_ = img.convert("RGBa").split()  # alpha prémultiplié, ordre BGRA
                ctypes.memmove(bits, Image.merge("RGBA", (b_, g_, r_, a_)).tobytes(), S * S * 4)
                user32.UpdateLayeredWindow(hwnd, screen_dc, ctypes.byref(pos), ctypes.byref(size),
                                           mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), 2)
                if not visible:
                    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                    visible = True
            elif visible:
                user32.ShowWindow(hwnd, 0)
                visible = False
            time.sleep(0.033)

    def prepare(self): self.state = 1
    def recording(self): self.state = 2
    def hide(self): self.state = 0

overlay = Overlay()

def dlog(msg, force=False):
    """Écrit dans le journal si le debug est activé (ou si force)."""
    if DEBUG or force:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

def say(msg, color=None):
    """Journal + info-bulle de l'icône (+ console si lancée depuis un terminal)."""
    dlog(msg)
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
    dlog(msg, force=True)  # les erreurs fatales sont toujours journalisées
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "Verbatim", 0x10)
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
        # shell de login: sinon ~/.local/bin (où se trouve claude) n'est pas dans le PATH.
        # Suggestions de message désactivées: affichées en gris dans la boîte de saisie,
        # elles seraient lues comme du texte dicté.
        r = wsl("tmux", "new-session", "-d", "-s", SESSION,
                "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false bash -lc claude")
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
            insert(target[k:])
        typed = target

def insert(text):
    """Insère `text` dans le champ actif. Les espaces en début et en fin sont tapés au clavier:
    certains champs (ex: barre d'adresse de Chrome) les retirent du texte collé."""
    core = text.strip()
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):] if core else ""
    for _ in lead:
        keyboard.send("space")
    if core:
        paste(core)
    for _ in trail:
        keyboard.send("space")

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
    say("⏳ Préparation... attendez le vert pour parler", WORK)
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
            say("(aucun texte détecté — activez « Journal de debug » dans le menu de l'icône pour les détails)")
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

def toggle_debug(icon=None, _item=None):
    global DEBUG
    DEBUG = not DEBUG
    save_config()
    say(f"Journal de debug {'activé' if DEBUG else 'désactivé'}" + (f" ({LOG})" if DEBUG else ""))
    if tray:
        tray.update_menu()

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
            save_config()
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
    tray = pystray.Icon("verbatim", make_icon(IDLE), f"Prêt — maintenez {HOTKEY.upper()} pour dicter",
                        menu=pystray.Menu(
                            pystray.MenuItem(lambda item: f"Changer la touche (actuelle : {HOTKEY.upper()})",
                                             change_hotkey),
                            pystray.MenuItem("Journal de debug", toggle_debug, checked=lambda item: DEBUG),
                            pystray.MenuItem("Quitter", quit_app)))
    say(f"Prêt. Maintenez {HOTKEY.upper()} pour dicter.")
    try:
        tray.run()  # bloque jusqu'à « Quitter » (ou Ctrl+C depuis un terminal)
    except KeyboardInterrupt:
        quit_app(tray)
