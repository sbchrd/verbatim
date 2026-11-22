"""Push-to-talk (F9) pour Claude dans tmux (WSL).
Maintenir F9 = dicter ; relâcher = affiche le texte transcrit.
Prérequis Windows: pip install keyboard
"""
import re, time, subprocess, sys, threading, os, keyboard

SESSION = "speech-to-text"
TARGET = f"{SESSION}:0.0"
# Distro WSL à utiliser (le défaut de wsl.exe peut être docker-desktop, sans bash/tmux)
DISTRO = os.environ.get("VOICE_DISTRO", "Ubuntu")
HERE = os.path.dirname(os.path.abspath(__file__))
# chemin WSL du script (ex: C:\dev\... -> /mnt/c/dev/...)
drive, rest = os.path.splitdrive(HERE)
LOG = os.path.join(HERE, "voice_debug.log")
SCRIPT = f"/mnt/{drive[0].lower()}{rest.replace(os.sep, '/')}/voice-hold.sh"

def wsl(*args, **kw):
    return subprocess.run(["wsl.exe", "-d", DISTRO, *args], capture_output=True, text=True,
                          encoding="utf-8", **kw)

def start_claude():
    if wsl("tmux", "has-session", "-t", SESSION).returncode != 0:
        # shell de login: sinon ~/.local/bin (où se trouve claude) n'est pas dans le PATH
        r = wsl("tmux", "new-session", "-d", "-s", SESSION, "bash -lc claude")
        time.sleep(1)
        if r.returncode != 0 or wsl("tmux", "has-session", "-t", SESSION).returncode != 0:
            sys.exit(f"Impossible de démarrer Claude dans tmux: {r.stderr.strip()}")

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
hold_proc = None
baseline = ""  # contenu de la boîte avant la dictée (ex: texte indicatif « Try ... »)

def on_press(_):
    global held
    if held:  # ignore l'auto-repeat du clavier
        return
    held = True
    print("🎙  Dictée... (relâchez F9 pour terminer)")
    global baseline
    baseline = extract_input(wsl("bash", SCRIPT, "capture", TARGET).stdout)
    global hold_proc
    hold_proc = subprocess.Popen(["wsl.exe", "-d", DISTRO, "bash", SCRIPT, "hold", TARGET],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def on_release(_):
    global held
    if not held:
        return
    held = False
    def finish():
        t0 = time.time()
        print("⏳ Relâché — transcription en cours...")
        wsl("bash", SCRIPT, "stop", TARGET)
        if hold_proc and hold_proc.poll() is None:
            hold_proc.terminate()
        out = prev = pane = ""
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"\n===== relâche {time.strftime('%H:%M:%S')} =====\n")
            for i in range(30):  # max ~7 s
                pane = wsl("bash", SCRIPT, "capture", TARGET).stdout
                out = extract_input(pane)
                if out == baseline:  # texte indicatif ou ancien contenu: pas encore transcrit
                    out = ""
                log.write(f"--- +{time.time()-t0:.1f}s extrait={out!r}\n")
                log.write("\n".join(pane.splitlines()[-8:]) + "\n")
                if out and out == prev:
                    break
                prev = out
                time.sleep(0.2)
        print(f"📝 Texte ({time.time()-t0:.1f}s): {out}\n")
        if out:
            wsl("bash", SCRIPT, "clear", TARGET)
            time.sleep(0.3)
            left = extract_input(wsl("bash", SCRIPT, "capture", TARGET).stdout)
            if left and left != baseline:
                print("⚠️  Le message n'a pas pu être effacé dans Claude.")
        if not out:
            print(f"(aucun texte détecté — détails dans {LOG})")
            print("\n".join(pane.splitlines()[-10:]) + "\n")
    threading.Thread(target=finish, daemon=True).start()

if __name__ == "__main__":
    chk = wsl("tmux", "-V")
    if chk.returncode != 0:
        sys.exit(f"tmux introuvable dans la distro '{DISTRO}': {chk.stderr.strip()}\n"
                 "Définissez VOICE_DISTRO (voir: wsl -l -v).")
    start_claude()
    keyboard.on_press_key("f9", on_press, suppress=True)
    keyboard.on_release_key("f9", on_release, suppress=True)
    print("Prêt. Maintenez F9 pour dicter. Ctrl+C pour quitter.")
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if hold_proc and hold_proc.poll() is None:
            wsl("bash", SCRIPT, "stop", TARGET)
            hold_proc.terminate()
        wsl("tmux", "kill-session", "-t", SESSION)
        print("Session tmux fermée.")
