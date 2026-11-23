# Speech to Text pour Windows (via Claude Code)

Application Windows de dictée « push-to-talk » : **maintenez une touche (F9 par défaut), parlez, relâchez**, et le texte transcrit est collé dans le champ de texte qui a le focus.

La transcription est faite par le mode vocal de [Claude Code](https://claude.com/claude-code), qui tourne dans une session **tmux** cachée, dans **WSL**. L'application simule la touche espace maintenue (le geste de dictée de Claude Code), lit le texte qui apparaît dans la boîte de saisie de Claude, le colle là où vous écriviez, puis vide la boîte de saisie.

## Fonctionnement

1. Au lancement, l'application démarre une session tmux `speech-to-text` qui exécute `claude` dans WSL (aucune fenêtre visible).
2. Quand vous **appuyez** sur la touche de dictée :
   - une pastille animée apparaît en bas à droite de l'écran : **ambre** pendant la préparation, **rouge** quand Claude écoute réellement. Parlez à partir du rouge.
3. Quand vous **relâchez** la touche :
   - l'application attend la fin de la transcription dans Claude, puis la saisie de Claude est effacée.

Le texte est inséré **au fur et à mesure** : chaque changement du texte dans Claude est répercuté dans le champ actif (collage avec `Ctrl+V`, presse-papiers d'origine restauré). Si Claude révise un passage déjà transcrit, l'application l'efface avec des retours arrière et le remplace.

L'application vit dans la zone de notification (icône près de l'horloge). Elle ne prend jamais le focus.

## Prérequis

- Windows 10/11 avec **WSL 2**
- Dans la distribution WSL (Ubuntu par défaut) :
  - **tmux**
  - **Claude Code** installé, avec le **mode vocal activé** (`/voice`), et le dossier du projet approuvé (la boîte de confirmation de confiance n'étant pas visible)
- Si votre distribution WSL par défaut n'est pas celle qui contient tmux et Claude (par exemple `docker-desktop`), définissez la variable d'environnement `VOICE_DISTRO` avec le nom de la bonne distribution (voir `wsl -l -v`).

Aucune installation de Python n'est nécessaire pour utiliser l'exécutable.

## Utilisation

1. Construisez ou récupérez `SpeechToText.exe` (voir ci-dessous) et lancez-le.
2. Cliquez dans le champ de texte où vous voulez écrire.
3. Maintenez **F9**, attendez la pastille rouge, parlez, puis relâchez.

**Mode continu (mains libres)** : faites un **double appui** sur la touche (deux appuis courts rapprochés). L'enregistrement continue sans tenir la touche, jusqu'à ce que vous appuyiez **une fois de plus** sur la même touche. Un appui court isolé est simplement ignoré (la dictée se termine sans texte).

### Menu de l'icône (clic droit)

- **Changer la touche (actuelle : F9)** : appuyez sur la nouvelle touche (Échap pour annuler). Elle est enregistrée dans `config.json` à côté de l'exécutable. Supprimez ce fichier pour revenir à F9.
- **Journal de debug** (case à cocher) : désactivé par défaut. Une fois coché, l'application écrit `voice_debug.log` à côté de l'exécutable. Le réglage est conservé dans `config.json`.
- **Quitter** : arrête la dictée en cours, ferme la session tmux et quitte l'application.

### Fichiers générés

- `voice_debug.log` : journal de debug, créé seulement si l'option est activée dans le menu. Utile en cas de problème.
- `config.json` : touche de dictée choisie et état du journal de debug.

## Construire l'exécutable

Prérequis : Python 3 sur Windows (nécessaire uniquement pour la construction).

```bat
build.bat
```

Cela installe les dépendances (`requirements.txt`) et produit `dist\SpeechToText.exe`, un fichier unique, sans console.

Pour lancer directement depuis les sources :

```bat
pip install -r requirements.txt
pythonw voice_claude.py
```

## Notes techniques

- La touche espace maintenue est simulée par un petit processus WSL persistant, piloté via stdin, qui envoie des espaces à tmux (`tmux send-keys`). Il signale `READY` dès que Claude affiche `listening…`.
- Le texte est extrait de la boîte de saisie de Claude : la ligne qui commence par `❯`, encadrée de deux lignes pleines (`────`). Le texte indicatif gris de Claude (`Try "…"`) est ignoré.
- Le script côté WSL est intégré dans `voice_claude.py` et réinstallé à chaque lancement dans `/tmp/speech-to-text-hold.sh`.

## Limitations

- L'exécutable n'est pas signé : Windows SmartScreen ou Defender peut afficher un avertissement.
- Le hook clavier global peut ne pas fonctionner dans des fenêtres lancées en administrateur, sauf si l'application l'est aussi.
- L'extraction du texte dépend de l'apparence de l'interface de Claude Code ; une mise à jour de celle-ci peut obliger à ajuster les motifs dans `extract_input()`.
