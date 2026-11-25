"""Guided, repeatable first-run and repair wizard.

Never asserts that missing project-specific DB/E2E tests were automatically created.
Installs testing tools only after one explicit category-level approval.
Existing fully configured testing categories are detected and skipped.
"""
from __future__ import annotations
from project_layout import project_state, project_state_name, project_root
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import datetime as dt
import threading
import time
from setup_i18n import CURRENT_LANGUAGE, translate
from model_menu import (effort_choices,
                        valid_model_id, normalize_models, advisor_id, CLAUDE_ADVISORS,
                        model_selection_error, pick_effort, pick_role_model)
import terminal_ui as ui
import host_setup


TOOL_NAME = 'Gaston Agentic'


def tr(lang: str, english: str, french: str) -> str:
    return french if lang == 'fr' else english


def choose_language(current: dict, *, interactive: bool, language: str | None = None,
                    force_prompt: bool = False) -> str:
    """Reuse a saved language; only ask on first setup or explicit reconfiguration."""
    stored = current.get('preferences', {}).get('setup_language')
    saved = stored if stored in ('en', 'fr') else 'en'
    if language is not None:
        if language not in ('fr', 'en'):
            raise ValueError('Language must be fr or en')
        return language
    if not interactive:
        return saved
    if stored in ('en', 'fr') and not force_prompt:
        print('  Langue : Français (enregistrée)' if saved == 'fr'
              else '  Language: English (saved)')
        return saved
    ui.heading(TOOL_NAME, 'Language / Langue')
    print('  1. English')
    print('  2. Français')
    default_label = 'Français' if saved == 'fr' else 'English'
    question = (f'  Choisissez la langue (1/2) [défaut : {default_label}] : ' if saved == 'fr'
                else f'  Choose language (1/2) [default: {default_label}]: ')
    while True:
        answer = input(ui.prompt_label(question, default='[défaut' if saved == 'fr' else '[default')).strip().lower()
        if answer in ('fr', '2', 'français', 'francais'):
            return 'fr'
        if answer in ('en', '1', 'english', ''):
            return saved if answer == '' else 'en'
        print(tr(saved, '  Unknown language; please choose again.',
                 '  Langue inconnue ; veuillez réessayer.'))


def _atomic_json(path: Path, obj: dict) -> None:
    """Persist setup checkpoints without corrupting the prior file on interruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8',
                                         dir=str(path.parent), prefix='.setup-',
                                         suffix='.tmp', delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(obj, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _load_progress(local: Path, root: Path) -> dict:
    path = local / 'setup-progress.json'
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        if (isinstance(saved, dict) and saved.get('schema') == 1
                and saved.get('workspace') == str(root)):
            if not isinstance(saved.get('steps'), dict):
                saved['steps'] = {}
            if not isinstance(saved.get('approvals'), dict):
                saved['approvals'] = {}
            return saved
    except (OSError, json.JSONDecodeError):
        pass
    return {'schema': 1, 'workspace': str(root), 'state': 'new',
            'steps': {}, 'approvals': {}}


def report_registered_sessions(registry, builder: str, reviewer: str, lang: str) -> None:
    """Reuse only Gaston-enrolled sessions, never request or auto-enroll arbitrary IDs."""
    pending = False
    for agent, role in ((builder, 'builder'), (reviewer, 'reviewer')):
        entry = registry.get(agent, role)
        if entry:
            print(f'    {agent}/{role}: {entry["id"]} (' + tr(lang, 'preserved', 'conservée') + ')')
        else:
            print(f'    {agent}/{role}: ' + tr(lang,
                'will be created automatically on first use',
                'sera créée automatiquement à la première utilisation'))
            pending = True
    if pending:
        print(tr(lang, '    No session IDs to enter.',
                      '    Aucun identifiant de session à saisir.'))


def setup_preview(root: Path, current: dict, lang: str, *, interactive: bool) -> bool:
    """Read-only preflight. Interactive installs require explicit GO consent."""
    root = Path(root).resolve()
    discovered = discover_stacks(root)
    pref = current.get('preferences', {})
    selected_e2e = pref.get('mobile_e2e', 'maestro')
    ui.heading(TOOL_NAME, tr(lang, 'Setup plan', 'Plan d’installation'))
    print(tr(lang, f'  Workspace: {root}', f'  Espace de travail : {root}'))
    print(tr(lang, '  Detected Java projects: ', '  Projets Java détectés : ') +
          (', '.join(discovered['java']) if discovered['java'] else tr(lang, 'none', 'aucun')))
    print(tr(lang, '  Detected Flutter projects: ', '  Projets Flutter détectés : ') +
          (', '.join(discovered['flutter']) if discovered['flutter'] else tr(lang, 'none', 'aucun')))
    def provider_status(agent):
        if not ok_bin(agent):
            return tr(lang, 'missing', 'absent')
        auth = check_auth(agent)
        status = tr(lang, 'signed in', 'connecté') if auth is True else (
            tr(lang, 'not signed in', 'non connecté') if auth is False else
            tr(lang, 'login unverified', 'connexion non vérifiée'))
        return tr(lang, 'installed', 'installé') + ', ' + status

    if selected_e2e == 'maestro':
        installed = bool(ok_bin('maestro') or (Path.home()/'.maestro/bin/maestro').is_file())
        print(tr(lang, f'  Mobile testing: Maestro ({"installed" if installed else "missing"})',
                       f'  Tests mobiles : Maestro ({"installé" if installed else "absent"})'))
    else:
        print(tr(lang, f'  Mobile testing: {selected_e2e}', f'  Tests mobiles : {selected_e2e}'))
    current_mode = pref.get('provider_mode','both')
    mode_labels={'codex':('Codex only','Codex seul'),
                 'claude':('Claude only','Claude seul'),
                 'both':('Both','Les deux')}
    label=mode_labels.get(current_mode,mode_labels['both'])
    print(tr(lang, f'  AI mode: {label[0]}',f'  Configuration IA : {label[1]}'))
    print(f'  Codex: {provider_status("codex")}')
    print(f'  Claude: {provider_status("claude")}')
    print()
    ui.section(tr(lang, 'Setup steps', 'Étapes de configuration'))
    items = [
        ('Detect projects and Git repositories',
         'Détecter les projets et les dépôts Git'),
        ('Check development tools, mobile testing and Playwright',
         'Vérifier les outils, les tests mobiles et Playwright'),
        ('Choose AI assistants, sign in and prepare managed sessions',
         'Choisir les assistants IA, se connecter et préparer les sessions gérées'),
        ('Report current project test readiness (new repos are auto-detected later)',
         'Afficher les tests des projets actuels (nouveaux dépôts détectés automatiquement)'),
        ('Summary', 'Résumé'),
    ]
    for i, (en, fr) in enumerate(items, 1):
        print(f'  {i}. {tr(lang,en,fr)}')
    print()
    ui.note(tr(lang, 'Nothing has been installed or changed yet from this run.',
                      'Aucune installation ni modification effectuée pendant cette exécution.'), indent=2)
    if not interactive:
        print(tr(lang, '  Non-interactive mode: no external installs; local status may be updated.',
                       '  Mode non interactif : aucune installation externe ; état local mis à jour si nécessaire.'))
        return True
    response = input(ui.prompt_label(tr(lang, '  Type GO to start setup (anything else cancels): ',
                                      '  Tapez GO pour lancer la configuration (sinon annulation) : '))).strip()
    if response.lower() != 'go':
        print(tr(lang, '  Setup cancelled. No configuration was changed.',
                       '  Configuration annulée. Aucune modification effectuée.'))
        return False
    return True


def probe(argv, *, cwd=None, timeout=12):
    try:
        from platform_compat import prepare_subprocess
        command,env,_=prepare_subprocess(argv)
        return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None



def visible_run(argv, *, label, timeout, cwd=None, capture_output=False,
                inactivity_timeout=None, **kwargs):
    """Run one visible command, return its status, and clean up on timeout.

    Captured commands use a spinner. Commands with their own progress display
    retain that display. An optional inactivity limit is useful for large
    downloads: continued output keeps the installer running, subject to an
    overall maximum. Timeouts return exit status 124 instead of a traceback.
    """
    language = CURRENT_LANGUAGE.get()
    started = time.monotonic()
    terminal = bool(getattr(sys.stdout, 'isatty', lambda: False)())
    print('    ' + ui.paint(tr(language, 'Starting', 'Démarrage'), 'blue') + ': ' + label, flush=True)
    stop = threading.Event()
    worker = None
    result = None
    timed_out = False
    interrupted = False
    timeout_reason = ''
    proc = None

    if capture_output:
        def ticker():
            frames = ('|', '/', '-', '\\')
            index = 0
            last_notice = started
            while not stop.wait(0.2 if terminal else 0.5):
                elapsed = int(time.monotonic() - started)
                if terminal:
                    message = f'{label}  {frames[index % len(frames)]} {elapsed}s'
                    sys.stdout.write('\r    ' + message[:100].ljust(105))
                    sys.stdout.flush()
                    index += 1
                elif time.monotonic() - last_notice >= 8:
                    print(tr(language, f'    Working: {label} ({elapsed}s)',
                              f'    En cours : {label} ({elapsed}s)'), flush=True)
                    last_notice = time.monotonic()

        worker = threading.Thread(target=ticker, daemon=True)
        worker.start()

    def terminate_process_group():
        """Stop the shell *and* its download children, not only the shell."""
        if proc is None:
            return
        if os.name == 'nt':
            from platform_compat import stop_owned
            stop_owned(proc, isolated_group=True, grace_seconds=2)
            return
        if os.name == 'posix':
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if os.name == 'posix':
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    try:
        from platform_compat import prepare_subprocess
        argv,child_env,_=prepare_subprocess(argv,kwargs.pop('env',None))
        if child_env is not None:kwargs['env']=child_env
        # The process owns a separate group so a timed-out installer
        # cannot leave a download subprocess running in the background.
        use_stream = inactivity_timeout is not None and not capture_output
        proc = subprocess.Popen(
            argv, cwd=cwd,
            stdout=subprocess.PIPE if (capture_output or use_stream) else None,
            stderr=(subprocess.STDOUT if use_stream else
                    subprocess.PIPE if capture_output else None),
            text=capture_output,
            **__import__('platform_compat').popen_kwargs(), **kwargs)
        if use_stream:
            last_activity = time.monotonic()
            deadline = started + timeout
            if os.name=='nt':
                import queue
                chunks=queue.Queue()
                def stream_reader():
                    try:
                        while True:
                            chunk=os.read(proc.stdout.fileno(),8192)
                            chunks.put(chunk)
                            if not chunk:break
                    except OSError:
                        chunks.put(b'')
                threading.Thread(target=stream_reader,daemon=True).start()
                while True:
                    now=time.monotonic()
                    if now>=deadline:
                        timeout_reason='installation time limit reached'
                        raise subprocess.TimeoutExpired(argv,timeout)
                    if now-last_activity>=inactivity_timeout:
                        timeout_reason='installation stopped producing output'
                        raise subprocess.TimeoutExpired(argv,inactivity_timeout)
                    try:
                        chunk=chunks.get(timeout=max(.01,min(1,deadline-now,inactivity_timeout-(now-last_activity))))
                    except queue.Empty:
                        continue
                    if not chunk:break
                    last_activity=time.monotonic()
                    if hasattr(sys.stdout,'buffer'):
                        sys.stdout.buffer.write(chunk);sys.stdout.buffer.flush()
                    else:
                        sys.stdout.write(chunk.decode('utf8',errors='replace'));sys.stdout.flush()
            else:
              with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    now = time.monotonic()
                    if now >= deadline:
                        timeout_reason = tr(language, 'installation time limit reached',
                                            'durée maximale d’installation atteinte')
                        raise subprocess.TimeoutExpired(argv, timeout)
                    if now - last_activity >= inactivity_timeout:
                        timeout_reason = tr(language, 'installation stopped producing output',
                                            'installation sans activité')
                        raise subprocess.TimeoutExpired(argv, inactivity_timeout)
                    events = selector.select(min(1.0, deadline - now,
                                                 inactivity_timeout - (now - last_activity)))
                    for key, _ in events:
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        last_activity = time.monotonic()
                        if hasattr(sys.stdout, 'buffer'):
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                        else:
                            sys.stdout.write(chunk.decode('utf-8', errors='replace'))
                            sys.stdout.flush()
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            result = subprocess.CompletedProcess(argv, proc.returncode)
        elif capture_output:
            out, err = proc.communicate(timeout=timeout)
            result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
        else:
            proc.wait(timeout=timeout)
            result = subprocess.CompletedProcess(argv, proc.returncode)
        return result
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group()
        result = subprocess.CompletedProcess(argv, 124)
        return result
    except OSError as exc:
        result = subprocess.CompletedProcess(argv, 127, stderr=str(exc))
        return result
    except KeyboardInterrupt:
        interrupted = True
        terminate_process_group()
        raise SystemExit(130)
    finally:
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        stop.set()
        if worker is not None:
            worker.join(timeout=1)
            if terminal:
                sys.stdout.write('\r' + ' ' * 112 + '\r')
                sys.stdout.flush()
        elapsed = int(time.monotonic() - started)
        status = ('CANCELLED' if interrupted else 'TIMEOUT' if timed_out
                  else 'OK' if result is not None and result.returncode == 0
                  else 'FAILED')
        status_fr = {'OK': 'OK', 'FAILED': 'ÉCHEC', 'TIMEOUT': 'DÉLAI DÉPASSÉ',
                     'CANCELLED': 'ANNULÉ'}
        print('    ' + ui.paint(tr(language, status, status_fr[status]),
                         'green' if status == 'OK' else 'amber' if status == 'TIMEOUT' else 'red')
              + f': {label} ({elapsed}s)', flush=True)
        if timed_out:
            detail = timeout_reason or tr(language, 'command timed out', 'délai de commande dépassé')
            print(f'    {detail}', flush=True)
        if result is not None and result.returncode == 127:
            print('    ' + (result.stderr or ''), flush=True)
        if capture_output and result is not None and result.returncode not in (0, 124, 127):
            detail = (result.stderr or result.stdout or '').strip()
            if detail:
                print('    ' + detail[-3000:], flush=True)


def ok_bin(binary):
    return shutil.which(binary) is not None


def yesno(question, default=False, *, interactive=True, lang='en'):
    if not interactive: return False
    effective_lang = CURRENT_LANGUAGE.get() if lang == 'en' else lang
    suffix = (f'(oui/non) [défaut : {"Oui" if default else "Non"}]' if effective_lang == 'fr'
              else f'(yes/no) [default: {"Yes" if default else "No"}]')
    while True:
        response = input(ui.prompt_label(f'    {translate(question.strip(), effective_lang)} {suffix}: ', default=suffix)).strip().lower()
        if not response: return default
        if response in ('yes', 'y', 'oui', 'o'): return True
        if response in ('no', 'n', 'non'): return False
        print(tr(effective_lang, '    Please answer yes or no, or press Enter for the stated default.',
                                  '    Répondez oui ou non, ou appuyez sur Entrée pour la valeur par défaut.'))


def prompt(question, default='', *, interactive=True, optional=False):
    """Text prompt: show the actual Enter behavior, with one consistent default style."""
    if not interactive: return default
    language = CURRENT_LANGUAGE.get()
    if default:
        suffix = f'[défaut : {default}]' if language == 'fr' else f'[default: {default}]'
    elif optional:
        suffix = '[facultatif ; Entrée pour ignorer]' if language == 'fr' else '[optional; Enter to skip]'
    else:
        suffix = '[requis]' if language == 'fr' else '[required]'
    value = input(ui.prompt_label(f'    {translate(question).strip()} {suffix}: ', default=suffix)).strip()
    return value if value else default


def install_agent(agent, *, interactive, lang='en'):
    if ok_bin(agent): return True
    if not interactive:
        return False
    print(tr(lang, f'    {agent} is missing.', f'    {agent} est absent.'))
    if agent == 'codex':
        print(tr(lang,'    Official npm distribution: npm install -g @openai/codex',
                      '    Distribution npm officielle : npm install -g @openai/codex'))
        if not ok_bin('npm'):
            print(tr(lang,'    Install Node.js/npm first: https://nodejs.org/',
                          '    Installez d’abord Node.js/npm : https://nodejs.org/'))
            return False
        if yesno('Install Codex through npm now?',interactive=interactive):
            result = visible_run(['npm','install','-g','@openai/codex'],label='Install Codex',timeout=600)
            if result.returncode: print(tr(lang,'    Codex installation failed; inspect npm output.',
                                                   '    Installation de Codex échouée ; vérifiez les messages npm.'))
    else:
        if platform.system() == 'Windows':
            print(tr(lang,'    Native Windows distribution: winget install Anthropic.ClaudeCode',
                          '    Distribution native Windows : winget install Anthropic.ClaudeCode'))
            if not ok_bin('winget'):
                print('    Install Claude Code from https://code.claude.com/docs/en/setup and rerun dev init.')
                return False
            if yesno('Install native Claude Code with WinGet?',interactive=interactive):
                result=visible_run(['winget','install','--id','Anthropic.ClaudeCode','--exact',
                                    '--accept-package-agreements','--accept-source-agreements'],
                                   label='Install native Claude Code',timeout=600)
                if result.returncode:
                    print('    Claude Code WinGet installation failed; inspect output.')
            import host_setup
            host_setup.refresh_tool_path()
            return ok_bin('claude')
        print(tr(lang,'    Official Claude Code installer: https://claude.ai/install.sh',
                      '    Installateur officiel Claude Code : https://claude.ai/install.sh'))
        if not (ok_bin('curl') and ok_bin('bash')):
            print(tr(lang,'    curl and bash required for native installer; see official docs.',
                          '    curl et bash sont requis ; consultez la documentation officielle.'))
            return False
        if yesno('Download and execute the official Claude Code installer?',interactive=interactive):
            with tempfile.TemporaryDirectory(prefix='agentic-claude-') as folder:
                script = Path(folder)/'claude-install.sh'
                result = visible_run(['curl','-fLSs','https://claude.ai/install.sh','-o',str(script)],label='Download Claude installer',timeout=120,capture_output=True)
                if result.returncode or not script.is_file() or script.stat().st_size < 100:
                    print(tr(lang,'    Could not download official installer.',
                                  '    Téléchargement de l’installateur officiel impossible.'))
                    return False
                print(tr(lang,f'    Downloaded {script.stat().st_size} bytes from https://claude.ai/install.sh',
                              f'    {script.stat().st_size} octets téléchargés depuis https://claude.ai/install.sh'))
                if yesno('Execute this downloaded installer now?',interactive=interactive):
                    result = visible_run(['bash',str(script)],label='Install Claude Code',timeout=600)
                    if result.returncode: print(tr(lang,'    Claude installation failed; inspect the installer output.',
                                                       '    Échec de l’installation de Claude ; vérifiez la sortie.'))
    return ok_bin(agent)


def installed_java_major():
    result=probe(['java','-version']) if ok_bin('java') else None
    if not result:return None
    info=result.stdout+result.stderr
    m=re.search(r'\bversion ["\']?(\d+)',info) or re.search(r'\b(?:openjdk|java) (\d+)(?:[.\s]|$)',info)
    if not m: return None
    version=int(m.group(1))
    if version == 1:
        sub=re.search(r'1\.(\d+)',info)
        if sub: version=int(sub.group(1))
    return version


# Search project roots, not dependencies, generated outputs or tool configuration.
# A finite depth and directory budget keep setup responsive in large workspaces.
SKIP_SCAN_DIRS = {
    '.git', '.agentic', '.gaston', '.agents', '.claude', '.dart_tool', '.gradle',
    '.idea', '.vscode', '.venv', 'venv', 'node_modules', 'build',
    'target', 'out', 'dist', 'coverage', 'vendor', 'Pods', 'DerivedData',
    '__pycache__', 'examples', 'example', 'test', 'tests', '.next', '.pub-cache',
}


def discover_stacks(root, *, max_depth=7, max_directories=10000):
    """Return Java/Flutter candidate root paths relative to a workspace.

    Use sorted filesystem traversal, never follow symlinks, and skip generated
    or dependency directories. Android Gradle builds owned by Flutter are not
    Java backend candidates. Discovery never modifies the filesystem.
    """
    root = Path(root).resolve()
    java, flutter = set(), set()
    count = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current)
        relative = directory.relative_to(root)
        depth = len(relative.parts)
        count += 1
        if count > max_directories:
            print(f'    Project scan limited to {max_directories} directories; configure further roots manually.')
            break
        dirs[:] = sorted(d for d in dirs if d not in SKIP_SCAN_DIRS and
                         not (directory/d).is_symlink()) if depth < max_depth else []
        names = set(files)
        relative_name = relative.as_posix()
        if relative_name == '.': relative_name = '.'
        if any(f in names for f in ('pom.xml', 'build.gradle', 'build.gradle.kts')):
            java.add(relative_name)
        if 'pubspec.yaml' in names:
            try:
                pubspec = (directory / 'pubspec.yaml').read_text(encoding='utf-8')[:32768]
            except (OSError, UnicodeError):
                pubspec = ''
            if (re.search(r'(?m)^\s*sdk\s*:\s*flutter\s*(?:#.*)?$', pubspec)
                    or re.search(r'(?m)^flutter\s*:', pubspec)):
                flutter.add(relative_name)

    # Flutter applications contain Android/Gradle tooling, not Java backends.
    java = {p for p in java if not any(
        p.startswith(f + '/android/') or p == f + '/android'
        for f in flutter if f != '.')}
    if '.' in flutter:
        java = {p for p in java if p != 'android' and not p.startswith('android/')}
    return {'java': sorted(java, key=lambda x: (len(Path(x).parts), x)),
            'flutter': sorted(flutter, key=lambda x: (len(Path(x).parts), x))}


def stacks(root):
    """Compatibility helper: only select a root if unambiguous."""
    found = discover_stacks(root)
    return tuple(paths[0] if len(paths) == 1 else None
                 for paths in (found['java'], found['flutter']))


def choose_stack_root(kind, choices, configured, root, *, interactive, lang='en'):
    """Keep valid prior choices; auto-select unique match; ask only for ambiguity.

    The user can still provide a custom relative location if scanning missed it.
    Noninteractive setup never guesses between multiple candidates.
    """
    marker_names = ('pom.xml', 'build.gradle', 'build.gradle.kts') if kind == 'Java' else ('pubspec.yaml',)
    def valid(relative):
        if not isinstance(relative, str) or not relative or relative == '-': return False
        candidate = (root / relative).resolve()
        return candidate.is_relative_to(root) and candidate.is_dir() and any(
            (candidate / marker).is_file() for marker in marker_names)

    if valid(configured):
        ui.result(tr(lang, f'{kind} project: {configured}', f'Projet {kind} : {configured}'), 'OK')
        return configured
    if configured not in (None, '', '-'):
        ui.result(tr(lang, f'Previously configured {kind} path no longer exists: {configured}',
                            f'Ancien projet {kind} introuvable : {configured}'), 'WARNING')
    if len(choices) == 1:
        ui.result(tr(lang, f'{kind} project: {choices[0]}', f'Projet {kind} : {choices[0]}'), 'OK')
        return choices[0]
    if len(choices) > 1:
        print(tr(lang,f'    Multiple {kind} projects detected:',
                      f'    Plusieurs projets {kind} détectés :'))
        for i, item in enumerate(choices, 1): print(f'      {i}. {item}')
        if interactive:
            while True:
                answer = prompt(tr(lang,
                    f'Choose a {kind} project number or enter a relative path',
                    f'Choisissez un numéro de projet {kind} ou saisissez un chemin relatif'),
                    '', interactive=True, optional=True).strip()
                if not answer: return None
                numbered={str(i):item for i,item in enumerate(choices,1)}
                if answer in numbered: return numbered[answer]
                if valid(answer): return answer
                print(tr(lang,f'    Invalid {kind} directory; please choose again.',
                             f'    Répertoire {kind} invalide ; veuillez réessayer.'))
        else:
            print(tr(lang,f'    [MISSING] Ambiguous {kind} project; rerun dev init to choose.',
                          f'    [MANQUANT] Projet {kind} ambigu ; relancez dev init pour choisir.'))
        return None
    if kind == 'Flutter':
        ui.result(tr(lang, 'Flutter project not detected yet', 'Projet Flutter pas encore détecté'), 'INFO')
        return None
    ui.result(tr(lang, f'No {kind} project detected in this workspace',
                       f'Aucun projet {kind} détecté dans cet espace'), 'INFO')
    if interactive:
        while True:
            answer = prompt(tr(lang,
                f'{kind} project path', f'Chemin du projet {kind}'),
                '', interactive=True, optional=True).strip()
            if not answer: return None
            if valid(answer): return answer
            print(tr(lang,f'    Invalid {kind} directory; please choose again.',
                         f'    Répertoire {kind} invalide ; veuillez réessayer.'))
    return None


def project_test_commands(root, java, flutter, e2e, old):
    generated = {'backend_verify','flutter_analyze','flutter_tests','mobile_e2e'}
    checks=[c for c in old if c.get('name') not in generated]
    if java is not None:
        here=root/java
        if (here/'mvnw').is_file(): command=['./mvnw','-B','verify']
        elif (here/'pom.xml').is_file(): command=['mvn','-B','verify']
        elif (here/'gradlew').is_file(): command=['./gradlew','check']
        elif (here/'build.gradle').is_file() or (here/'build.gradle.kts').is_file(): command=['gradle','check']
        else: command=None
        if command:
            checks.insert(0,{'name':'backend_verify','cwd':java,'command':command,'timeout_seconds':1200})
    if flutter is not None:
        checks.extend([{'name':'flutter_analyze','cwd':flutter,'command':['flutter','analyze'],'timeout_seconds':600},
                       {'name':'flutter_tests','cwd':flutter,'command':['flutter','test'],'timeout_seconds':1200}])
        if e2e == 'maestro':
            # No when_path_exists: missing tests fail the readiness gate, not silently skipped.
            checks.append({'name':'mobile_e2e','cwd':flutter,'command':['maestro','test','.maestro'],'timeout_seconds':1200})
        elif e2e == 'patrol':
            checks.append({'name':'mobile_e2e','cwd':flutter,'command':['patrol','test'],'timeout_seconds':1800})
        elif e2e == 'integration_test':
            checks.append({'name':'mobile_e2e','cwd':flutter,'command':['flutter','test','integration_test'],'timeout_seconds':1800})
    return checks


def android_sdk_tools(name):
    """Look for Android SDK executables without requiring Flutter or PATH changes."""
    from_env = [os.environ.get('ANDROID_HOME'), os.environ.get('ANDROID_SDK_ROOT')]
    homes = [Path(p).expanduser() for p in from_env if p]
    homes.extend([Path.home() / 'Android/Sdk', Path.home() / 'Library/Android/sdk'])
    candidates = [Path(x) / sub / name for x in homes
                  for sub in (('platform-tools',) if name == 'adb' else ('emulator',))]
    found = shutil.which(name)
    if found:
        return str(found)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def android_avds():
    """Configured Android Studio AVDs, including when Flutter is absent."""
    binary = android_sdk_tools('emulator')
    if binary:
        result = probe([binary, '-list-avds'], timeout=20)
        if result and result.returncode == 0:
            return [s.strip() for s in result.stdout.splitlines() if s.strip()]
    # New Android CLI may be installed independently of the classic emulator CLI.
    if ok_bin('android'):
        result = probe(['android', 'emulator', 'list'], timeout=20)
        if result and result.returncode == 0:
            return [s.strip() for s in result.stdout.splitlines() if s.strip() and
                    not s.lower().startswith(('name ', 'avd ', 'configured '))]
    return []


def devices_running():
    binary = android_sdk_tools('adb')
    if not binary:
        return 0
    result=probe([binary,'devices'],timeout=15)
    return sum('\tdevice' in line for line in result.stdout.splitlines()[1:]) if result and result.returncode == 0 else 0


def mobile_device_running():
    if devices_running()>0: return True
    if platform.system()=='Darwin' and ok_bin('xcrun'):
        found=probe(['xcrun','simctl','list','devices','booted','--json'],timeout=20)
        if found and found.returncode==0:
            try:
                devices=json.loads(found.stdout).get('devices',{})
                return any(d.get('state')=='Booted' for group in devices.values() for d in group)
            except (ValueError,TypeError):pass
    return False


def lead_mcp_agents(preferences: dict) -> tuple[str, ...]:
    """Only the Lead gets interactive testing MCP registrations during init.

    Codex's `mcp add` is user-wide, not role-specific. Do not modify that
    global scope in a toolkit which promises enrolled-session isolation.
    Claude's --scope local is tied to the workspace.
    """
    return ('claude',) if preferences.get('reviewer') == 'claude' else ()


def registration_status(provider):
    if not ok_bin(provider):return False
    result=probe([provider,'mcp','get','maestro'],timeout=15)
    return bool(result and result.returncode==0)


def check_auth(provider):
    cmd=['codex','login','status'] if provider=='codex' else ['claude','auth','status']
    result=probe(cmd,timeout=12)
    if result is None:return None
    # Authentication CLI may change syntax. Treat unrecognized options as unknown.
    msg=(result.stderr+result.stdout).lower()
    if 'unknown command' in msg or 'unknown option' in msg or 'unrecognized' in msg:return None
    try:
        raw=json.loads(result.stdout)
        if isinstance(raw,dict) and raw.get('loggedIn') is False:return False
    except ValueError:pass
    if 'not logged in' in msg or 'not authenticated' in msg:return False
    return result.returncode==0



def validate_selected_models(current: dict, active_roles, mark):
    """Automatic model preflight invoked by dev init.

    A local CLI catalog verifies Codex model/effort support. Claude model IDs and
    effort syntax are validated locally, but account entitlements and whether
    the selected Advisor can be attached are only confirmed on a live enrolled session.
    """
    from model_menu import verify_codex_model_effort
    models = current.get('models', {})
    for role, provider in active_roles:
        key = provider + '_' + role
        model = models.get(key)
        title = f'{provider} {role} model/effort'
        if not isinstance(model, str) or not model.strip():
            mark(title, False, 'Choose a model in dev init')
            continue
        effort = current.get('reasoning', {}).get(key)
        if effort is None:
            mark(title, False, 'Choose a reasoning level in dev init')
            continue
        if provider == 'codex':
            ok, detail = verify_codex_model_effort(model, effort, verified=current.get('codex_verified_pairs'))
            mark(title, ok, detail)
        elif provider == 'claude':
            supported = effort in effort_choices('claude', model)
            mark(title, supported,
                 f'Claude effort {effort!r} is not a recognized CLI effort.')
            mark('Claude model ID',valid_model_id('claude',model), 'Enter an exact Claude model ID in dev models.')
            if role == 'reviewer':
                advisor = advisor_id(current)
                if advisor not in CLAUDE_ADVISORS and not valid_model_id('claude',advisor):
                    mark('Claude Lead Advisor setting', False,
                         'Unsupported Advisor choice; select an exact model ID or off in dev models.')
                elif advisor != 'off' and os.environ.get('CLAUDE_CODE_DISABLE_ADVISOR_TOOL') == '1':
                    mark('Claude Lead Advisor environment', False,
                         'CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1 blocks Advisor. Unset it, or turn Advisor off in dev models.')
                else:
                    mark('Claude Lead Advisor environment', True, '')
                if advisor != 'off' and os.environ.get('DISABLE_TELEMETRY'):
                    mark('Claude Advisor feature flags', False,
                         'DISABLE_TELEMETRY may prevent Advisor feature-flag retrieval.', optional=True)



def backend_test_readiness(root: Path, java: str | None) -> dict[str, bool]:
    """Check runnable backend tooling without modifying the application or host OS."""
    if java is None:
        return {}
    base = Path(root)/java
    runner = next((check for check in project_test_commands(Path(root),java,None,'none',[])
                   if check.get('name') == 'backend_verify'), None)
    command = runner['command'][0] if runner else None
    runner_ok = bool(command and
                     ((base/command).is_file() and os.access(base/command,os.X_OK)
                      if command.startswith('./') else ok_bin(command)))
    docker = host_setup.daemon_id(host_setup.local_docker()) if ok_bin('docker') else None
    return {'Java >= 17': (installed_java_major() or 0) >= 17,
            'Java test runner': runner_ok,
            'curl':ok_bin('curl'),
            'PostgreSQL client (psql)':ok_bin('psql') and host_setup.psql_ready(),
            'Docker engine (disposable PostgreSQL prerequisite)': bool(docker)}


def mobile_test_readiness(e2e: str, *, flutter: str | None, lead_agents=()) -> dict[str, bool]:
    """A testing environment may be installed before an application or device exists."""
    if e2e == 'none':
        return {}
    if e2e == 'maestro':
        from eng import detected_maestro
        return {'Java >= 17':(installed_java_major() or 0) >= 17,
                'Maestro CLI':bool(detected_maestro()),
                **{f'{agent} Maestro MCP':registration_status(agent) for agent in lead_agents},
                **({'Flutter SDK':ok_bin('flutter')} if flutter is not None else {})}
    if e2e == 'patrol':
        return {'Patrol CLI':ok_bin('patrol'),
                **({'Flutter SDK':ok_bin('flutter')} if flutter is not None else {})}
    return {'Flutter SDK':ok_bin('flutter')} if flutter is not None else {}


def testing_category_approval(name: str, checks: dict[str, bool], *, interactive: bool,
                              lang: str, resume_approved: bool = False,
                              on_approve=None) -> bool:
    """One approval per category, reusable after interruption in the same workspace."""
    if not checks:
        ui.result(tr(lang, name, name), 'SKIPPED', detail=tr(lang, 'Not applicable', 'Non applicable'))
        return False
    missing = [key for key, ready in checks.items() if not ready]
    if not missing:
        ui.result(name, 'OK', detail=tr(lang, 'Already configured', 'Déjà configuré'))
        return False
    ui.note(tr(lang, 'Missing: ', 'Manquant : ') + ', '.join(missing), tone='amber')
    if interactive and resume_approved:
        print(tr(lang, f'    {name}: previous approval saved; resuming incomplete setup.',
                       f'    {name} : autorisation conservée ; reprise de la configuration.'))
        return True
    approved = yesno(tr(lang,f'Set up {name} essentials for this workspace?',
                            f'Configurer les outils essentiels pour {name} dans cet espace de travail ?'),
                     default=True, interactive=interactive, lang=lang)
    if approved and on_approve is not None:
        on_approve()
    return approved


def prepare_backend_essentials(root: Path, java: str | None, *, approved: bool, lang: str):
    """Install only explicitly authorized system packages; never alter the app."""
    if java is None or not approved:
        return
    state = backend_test_readiness(root, java)
    missing = [key for name, key in [('Java >= 17','java'), ('curl','curl'),
                    ('PostgreSQL client (psql)','psql')] if not state.get(name)]
    if not state.get('Java test runner'):
        base = Path(root)/java
        # Wrappers belong to the app; do not chmod or replace them implicitly.
        if not (base/'mvnw').exists() and not (base/'gradlew').exists():
            if (base/'pom.xml').exists(): missing.append('maven')
            elif any((base/n).exists() for n in ('build.gradle','build.gradle.kts')):
                missing.append('gradle')
    if not state.get('Docker engine (disposable PostgreSQL prerequisite)'):
        if not ok_bin('docker'):
            missing.append('docker')
        else:
            print(tr(lang, '    Docker CLI exists but the local engine is not accessible. Start Docker or resolve socket permissions, then rerun init. No automatic privilege changes.',
                '    Le CLI Docker existe mais le moteur local est inaccessible. Démarrez Docker ou corrigez les permissions, puis relancez init. Aucun changement de privilèges automatique.'))
    host_setup.install(missing, interactive=True, ask=yesno, lang=lang)
    if 'docker' in missing:
        host_setup.offer_mac_docker_install(interactive=True,ask=yesno,lang=lang)
    if not host_setup.daemon_id(host_setup.local_docker()):
        host_setup.offer_docker_start(interactive=True,ask=yesno,lang=lang)
        if not host_setup.daemon_id(host_setup.local_docker()):
            print('    '+host_setup.manual_guidance(['docker']))


def postgres_setup_check(*, interactive, lang, progress, save_progress):
    """Return (passed, attempted, explanation); do not run containers silently."""
    prefix = host_setup.local_docker()
    if not prefix or not host_setup.daemon_id(prefix):
        return False, False, 'Local Docker engine unavailable; PostgreSQL smoke test not run.'
    previous = progress.get('postgres_smoke')
    current = host_setup.fingerprint(prefix)
    if current and isinstance(previous, dict) and previous.get('fingerprint') == current and previous.get('status') == 'pass':
        return True, False, 'Previously verified on this Docker engine and image; not rerun.'
    if not interactive or not sys.stdin.isatty():
        return False, False, 'PostgreSQL smoke test not run (non-interactive setup).'
    print(tr(lang, f'    Disposable PostgreSQL check: {host_setup.IMAGE}; may download the official image. No exposed ports or application data. Container removed after SQL check.',
                  f'    Test PostgreSQL jetable : {host_setup.IMAGE} ; téléchargement possible de l’image officielle. Aucun port exposé ni donnée applicative. Conteneur supprimé après le test SQL.'))
    if not yesno(tr(lang,'Run disposable PostgreSQL smoke test now?', 'Lancer maintenant le test PostgreSQL jetable ?'),
                 default=True, interactive=True, lang=lang):
        return False, False, 'PostgreSQL smoke test declined; no database verification claimed.'
    result = host_setup.postgres_smoke(prefix)
    result['fingerprint'] = host_setup.fingerprint(prefix) if result['status'] == 'pass' else None
    progress['postgres_smoke'] = result
    save_progress()
    return result['status'] == 'pass', True, result['reason']


def open_codex_hook_review(root: Path) -> int:
    """Open native Codex trust UI and return as soon as its approval is recorded.

    Only the child process created by this setup run is stopped. Existing
    sessions, saved IDs and provider trust records are never modified.
    If the provider changes its trust-record format, the user can exit the
    native UI normally; we explicitly report that trust is unverified.
    """
    from session_store import Store
    from native_hooks import session_active, codex_trust_status
    store = Store(root)
    command = ['codex']
    for role in ('builder', 'reviewer'):
        entry = store.get('codex', role)
        if entry and not session_active('codex', role):
            command = ['codex', 'resume', entry['id']]
            break
    # Native Codex must obtain consent. Gaston only observes the recorded
    # decision; it never writes to ~/.codex/config.toml or adds trust hashes.
    from platform_compat import prepare_subprocess
    command,child_env,_=prepare_subprocess(command)
    process = subprocess.Popen(command, cwd=root, env=child_env)
    try:
        while process.poll() is None:
            state = codex_trust_status(root)
            if state['status'] == 'trust_recorded':
                # The ephemeral UI was launched by this wizard for approval
                # only. End it after the decision is stored, avoiding a second
                # interactive chat and allowing init to resume automatically.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                return 0
            time.sleep(0.5)
        return process.returncode or 0
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return 130


def run_setup(root: Path, current: dict, *, interactive: bool, maestro_setup, playwright_setup, write_json,
              config_file: Path, local: Path, language: str | None = None):
    """Return readiness for core tools and project gates; no device boot during setup."""
    root=Path(root).resolve()
    existing_preferences = current.get('preferences', {})
    previously_configured = (existing_preferences.get('setup_language') in ('en', 'fr')
                             or existing_preferences.get('provider_mode') in ('codex', 'claude', 'both'))
    lang = choose_language(current, interactive=interactive, language=language)
    if not setup_preview(root, current, lang, interactive=interactive):
        return None  # GO remains mandatory; cancellation changes nothing.
    change_choices = bool(interactive and previously_configured and
        yesno(tr(lang, 'Change saved language or AI configuration?',
                       'Modifier la langue ou la configuration IA enregistrée ?'),
              default=False, interactive=True, lang=lang))
    if change_choices:
        lang = choose_language(current, interactive=interactive,
                               language=language, force_prompt=True)
    CURRENT_LANGUAGE.set(lang)
    pref = current.setdefault('preferences', {})
    pref['setup_language'] = lang
    local.mkdir(parents=True, exist_ok=True)
    progress = _load_progress(local, root)
    progress['state'] = 'in_progress'
    progress['language'] = lang
    def save_progress():
        progress['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        _atomic_json(local/'setup-progress.json', progress)
    def checkpoint(step):
        # Save selected settings immediately, not only after the final summary.
        _atomic_json(config_file, current)
        progress.setdefault('steps', {})[step] = 'saved'
        save_progress()
    normalize_models(current)
    checkpoint('language')
    _atomic_json(local/'setup-status.json', {
        'ready': False, 'project': str(root), 'language': lang, 'state': 'in_progress'})
    pending=[]; statuses=[]
    def mark(name, good, action, *, optional=False, deferred=False):
        label = 'OK' if good else ('DEFERRED' if deferred else ('OPTIONAL' if optional else 'MISSING'))
        statuses.append({'name': name, 'status': label, 'action': None if good else action})
        shown = ('INFO' if deferred and not good else label)
        ui.result(translate(name), shown, detail='' if good else translate(action))
        if not good and not (optional or deferred): pending.append(action)

    ui.heading(TOOL_NAME, tr(lang, 'Setup', 'Configuration'))
    ui.step(1, tr(lang, 'Project and prerequisites', 'Projets et prérequis'))
    host_setup.refresh_tool_path()
    ui.note(tr(lang, 'Host: ', 'Système : ')+host_setup.host_description(),indent=4)
    if host_setup.is_wsl() and str(root).startswith('/mnt/'):
        ui.note(tr(lang, 'WSL: prefer a workspace under your Linux home directory for private credentials and POSIX locks.', 'WSL : préférez un espace de travail dans votre dossier personnel Linux pour les identifiants et verrous POSIX.'),indent=4)
    if not ok_bin('git'):
        host_setup.install(['git'],interactive=interactive,ask=yesno,lang=lang)
    mark('Git installed', ok_bin('git'), 'Install Git')
    mark('Python >= 3.10',sys.version_info >= (3,10),'Install Python 3.10+')
    candidates=discover_stacks(root)
    pref=current.setdefault('preferences',{})
    existing_roots=current.get('project',{})
    java=choose_stack_root('Java', candidates['java'], existing_roots.get('java_root'), root,
                           interactive=interactive, lang=lang)
    flutter=choose_stack_root('Flutter', candidates['flutter'], existing_roots.get('flutter_root'), root,
                              interactive=interactive, lang=lang)
    current['project']={'java_root':java,'flutter_root':flutter}
    if len(candidates['java']) > 1 and java is None:
        mark('Java project selection', False, 'Choose which Java project to manage on the next dev init run')
    if len(candidates['flutter']) > 1 and flutter is None:
        mark('Flutter project selection', False, 'Choose which Flutter project to manage on the next dev init run')
    from workspace_git import tracked_roots, baseline_ready
    repos, missing_roots = tracked_roots(root, current['project'])
    if repos:
        print(tr(lang,'    Detected application Git repositories:',
                      '    Dépôts Git applicatifs détectés :'))
        for repo in repos: print('      -', repo.relative_to(root).as_posix())
    else:
        print(tr(lang,'    No Git repositories found inside this workspace.',
                      '    Aucun dépôt Git trouvé dans cet espace de travail.'))
    mark('Selected code roots tracked by Git', bool(repos) and not missing_roots,
         'Track the selected application directories with Git.'
         + (' Untracked code roots: ' + ', '.join(missing_roots) if missing_roots else ''))
    mark('Git baseline commits', baseline_ready(repos),
         'Create an initial commit inside each application Git repository; required for reliable diff review')
    for kind, value, filename in [('Java',java,'pom.xml'),('Flutter',flutter,'pubspec.yaml')]:
        if value is not None:
            path=(root/value).resolve()
            if not path.is_relative_to(root): raise ValueError('Stack root must be inside the project')
            exists=path.is_dir() and (any((path/name).exists() for name in ['pom.xml','build.gradle','build.gradle.kts']) if kind=='Java' else (path/filename).exists())
            mark(kind+' project',exists,f'Choose a valid {kind} root and rerun dev init')

    # AI-managed project configuration: no manual policy, PostgreSQL command,
    # or verification-command questionnaires in init. Preserve every existing
    # policy and custom check; only discover safe, deterministic test commands.
    e2e = pref.get('mobile_e2e', 'maestro')
    if e2e not in ('maestro', 'patrol', 'integration_test', 'none'):
        e2e = 'maestro'
    pref['mobile_e2e'] = e2e
    pref.pop('postgres_integration_required', None)  # drop obsolete setup toggle
    current.setdefault('policy_files', [])  # Never enable a domain pack without developer selection.
    # Initial scan only; future agent tasks re-discover new repos automatically.
    import workspace_checks
    current, _ = workspace_checks.refresh(root, current)
    current.setdefault('models', {'codex_builder':'', 'codex_reviewer':'',
                                  'claude_builder':'','claude_reviewer':''})
    current.setdefault('reasoning', {})
    checkpoint('project')

    ui.step(2, tr(lang, 'Development tools', 'Outils de développement'))
    # Testing environments are prepared after providers/roles are selected,
    # allowing a single role-aware approval per incomplete category.
    if java is not None:
        print(tr(lang,'    Java backend detected; test prerequisites checked in step 3.',
                      '    Backend Java détecté ; prérequis de test vérifiés à l’étape 3.'))
    if e2e != 'none':
        print(tr(lang,'    Mobile test tooling checked in step 3; no emulator is started.',
                      '    Outils de tests mobiles vérifiés à l’étape 3 ; aucun émulateur démarré.'))

    ui.step(3, tr(lang, 'AI assistants and sessions', 'Assistants IA et sessions'))
    stored_mode=pref.get('provider_mode')
    if stored_mode not in ('codex','claude','both'):
        # Infer the saved roles from earlier kit versions; do not silently change
        # provider mode merely because a CLI is temporarily unavailable.
        previous_builder=pref.get('preferred_builder',pref.get('builder','codex'))
        previous_reviewer=pref.get('preferred_reviewer',pref.get('reviewer','claude'))
        if previous_builder==previous_reviewer and previous_builder in ('codex','claude'):
            stored_mode=previous_builder
        else:
            stored_mode='both'
    mode=stored_mode
    options={'1':'codex','2':'claude','3':'both'}
    if interactive and (change_choices or not pref.get('provider_mode')):
        print(tr(lang, '    Select AI setup:', '    Choisissez la configuration IA :'))
        print(tr(lang, '      1. Codex only', '      1. Codex seul'))
        print(tr(lang, '      2. Claude only', '      2. Claude seul'))
        print(tr(lang, '      3. Both (recommended)', '      3. Les deux (recommandé)'))
        names={'codex':'Codex only','claude':'Claude only','both':'Both'}
        names_fr={'codex':'Codex seul','claude':'Claude seul','both':'Les deux'}
        current_name=(names_fr if lang=='fr' else names)[stored_mode]
        while True:
            selection=input(ui.prompt_label(tr(lang,
                f'    Select AI setup (1/2/3) [default: {current_name}]: ',
                f'    Choisissez la configuration IA (1/2/3) [défaut : {current_name}] : '),
                default='[défaut' if lang == 'fr' else '[default')).strip()
            if not selection or selection in options:
                mode=options.get(selection,stored_mode)
                break
            print(tr(lang,'    Invalid choice; please choose again.',
                          '    Choix invalide ; veuillez réessayer.'))
    pref['provider_mode']=mode
    pref['builder']='codex' if mode in ('both','codex') else 'claude'
    pref['reviewer']='claude' if mode in ('both','claude') else 'codex'
    checkpoint('ai_architecture')
    if interactive and not change_choices and previously_configured:
        mode_en = {'both': 'Both', 'codex': 'Codex only', 'claude': 'Claude only'}[mode]
        mode_fr = {'both': 'Les deux', 'codex': 'Codex seul', 'claude': 'Claude seul'}[mode]
        print(tr(lang, f'    AI setup: {mode_en} (saved)',
                       f'    Configuration IA : {mode_fr} (enregistrée)'))
    required={'codex','claude'} if mode=='both' else {mode}
    if 'codex' in required and not ok_bin('codex') and not ok_bin('npm'):
        host_setup.install(['node','npm'],interactive=interactive,ask=yesno,lang=lang)
    if 'claude' in required and not ok_bin('claude') and not ok_bin('curl'):
        host_setup.install(['curl'],interactive=interactive,ask=yesno,lang=lang)
    available={}
    for agent in ('codex','claude'):
        if agent not in required:
            available[agent]=False
            continue
        installed=ok_bin(agent) or install_agent(agent,interactive=interactive,lang=lang)
        mark(agent+' CLI',installed,f'Install {agent}',optional=False)
        auth=check_auth(agent) if installed else False
        if installed and auth is False and interactive and yesno(f'Open {agent} login flow now?',interactive=True):
            command=['codex','login'] if agent=='codex' else ['claude','auth','login']
            visible_run(command,label=f'{agent.capitalize()} sign-in',timeout=240)
            auth=check_auth(agent)
        available[agent]=bool(installed and auth is True)
        if installed:
            mark(agent+' authentication',auth is True,
                 f'Run {"codex login" if agent=="codex" else "claude auth login"}')

    # Setup selection determines actual permissions and automation. No implicit
    # switch to another provider without the developer choosing that setup.
    builder='codex' if mode in ('both','codex') else 'claude'
    reviewer='claude' if mode in ('both','claude') else 'codex'
    pref['builder']=builder
    pref['reviewer']=reviewer
    mark('AI builder available',available.get(builder,False),'Install and sign in to '+builder)
    mark('AI reviewer available',available.get(reviewer,False),'Install and sign in to '+reviewer)
    print(tr(lang,f'    Builder: {builder}  |  Lead: {reviewer}',
                  f'    Développement : {builder}  |  Pilotage : {reviewer}'))

    # Validate before accepting a selection or resuming a saved role. Only
    # persist a model after local validation; do not skip incompatible efforts.
    from model_menu import codex_catalog_details
    catalog=codex_catalog_details() if 'codex' in required else None
    if 'codex' in required:
        mark('Codex CLI model catalog', bool(catalog),
             'Catalog unavailable; exact IDs can be checked directly if Codex is installed.', optional=True)
    models=current.setdefault('models',{})
    reasoning=current.setdefault('reasoning',{})
    active_roles=(('builder',builder),('reviewer',reviewer))
    changed=set()
    direct_selected=set()
    def model_ready(agent,role):
        key=agent+'_'+role
        return not model_selection_error(agent,models.get(key),catalog=catalog,verified=current.get('codex_verified_pairs'))
    if interactive:
        if ('codex' in required and not any(levels and valid_model_id('codex',m) for m,levels in catalog.items())
                and not shutil.which('codex') and not current.get('codex_verified_pairs')):
            pref['models_configured']=False
            checkpoint('model_settings')
            mark('Codex model selection', False,
                 'No selectable Codex model has verified reasoning levels. Update Codex and rerun dev init.')
            progress['state']='incomplete'; save_progress()
            _atomic_json(local/'setup-status.json', {'ready':False,'project':str(root),
                'language':lang,'state':'incomplete','checks':statuses,'blockers':list(dict.fromkeys(pending))})
            return False
        for role,agent in active_roles:
            key=agent+'_'+role
            previous=models.get(key,'')
            if model_ready(agent,role) and not change_choices:
                continue
            role_label=tr(lang,'Builder' if role=='builder' else 'Engineering Lead',
                          'Développement' if role=='builder' else 'Pilotage technique')
            print(f'    {agent.capitalize()} — {role_label}:')
            selected,direct_effort=pick_role_model(agent,role,previous,catalog=catalog,config=current,lang=lang)
            models[key]=selected
            if selected!=previous:
                reasoning.pop(key,None)
                changed.add(key)
            if direct_effort is not None:
                reasoning[key]=direct_effort
                direct_selected.add(key)
            pref['models_configured']=False
            checkpoint(f'model_{key}')
        effort_roles=[]
        for role,agent in active_roles:
            key=agent+'_'+role
            levels=effort_choices(agent,models.get(key),catalog=catalog,verified=current.get('codex_verified_pairs'))
            if key in direct_selected:
                continue
            if change_choices or key in changed or reasoning.get(key) not in levels:
                effort_roles.append((role,agent,levels))
        if effort_roles:
            print(tr(lang,'    Reasoning effort:', '    Niveau de raisonnement :'))
        for role,agent,levels in effort_roles:
            key=agent+'_'+role
            label=tr(lang,'Engineering Lead' if role=='reviewer' else 'Builder',
                        'Pilotage technique' if role=='reviewer' else 'Développement')
            print('    '+label+' ('+agent+')')
            reasoning[key]=pick_effort(agent,role,models[key],reasoning.get(key,''),
                                       catalog=catalog,levels=levels,lang=lang)
            checkpoint(f'reasoning_{key}')
    pref['models_configured']=all(model_ready(agent,role) and
         reasoning.get(agent+'_'+role) in effort_choices(agent,models.get(agent+'_'+role),catalog=catalog,verified=current.get('codex_verified_pairs'))
         for role,agent in active_roles)
    checkpoint('model_settings')
    if reviewer=='claude':
        current.setdefault('advisor',{})['claude_reviewer']=advisor_id(current)
    checkpoint('advisor')
    validate_selected_models(current, active_roles, mark)
    if builder==reviewer:
        print(tr(lang,'    Two independent roles; Builder sessions are scoped to tasks.',
                      '    Deux sessions persistantes indépendantes seront utilisées.'))
    else:
        print(tr(lang,'    One independent role per provider; Builder sessions are scoped to tasks.',
                      '    Une session persistante par fournisseur.'))

    lead_agents=lead_mcp_agents(pref)
    ui.section(tr(lang, 'Testing essentials', 'Outils essentiels de test'))

    # Backend: only relevant if a Java project is detected. Do not assume a
    # Docker engine exists or silently modify system packages/application POMs.
    backend_before=backend_test_readiness(root, java)
    def approval_for(category):
        return f'{category}:{mode}:{java or "no-java"}'
    def remember_approval(category):
        progress.setdefault('approvals', {})[approval_for(category)] = True
        save_progress()
    backend_approved=testing_category_approval('Backend testing', backend_before,
                                                 interactive=interactive, lang=lang,
        resume_approved=bool(progress.get('approvals',{}).get(approval_for('backend'))),
        on_approve=lambda:remember_approval('backend'))
    prepare_backend_essentials(root,java,approved=backend_approved,lang=lang)
    backend_after=backend_test_readiness(root, java)
    for tool,ready in backend_after.items():
        mark('Backend '+tool,ready,
             (tr(lang,'Install the PostgreSQL command-line client (not the server); rerun dev init.',
                 'Installez le client en ligne de commande PostgreSQL (pas le serveur), puis relancez dev init.')
              if tool == 'PostgreSQL client (psql)' else f'Prepare {tool} for isolated backend verification'),
             optional=not backend_approved)

    if java is not None:
        smoke_ok, smoke_attempted, smoke_detail = postgres_setup_check(
            interactive=interactive and (backend_approved or all(backend_before.values())),
            lang=lang,progress=progress,save_progress=save_progress)
        mark('Disposable PostgreSQL smoke test',smoke_ok,smoke_detail,optional=not smoke_attempted)
        if smoke_ok:
            ui.note(smoke_detail,indent=6)
        # A development database is optional and independent of automated tests.
        # Do not provision it in non-interactive setup or modify app config.
        from development_db import probe, provision
        if interactive and smoke_ok:
            existing=probe(root)
            if existing.get('status')=='ready':
                db=existing
            else:
                db={'status':'not_configured'}
                if yesno(tr(lang,'Prepare a persistent local PostgreSQL development database (separate from tests)?','Préparer une base PostgreSQL locale persistante de développement (séparée des tests) ?'),default=True,interactive=True,lang=lang):
                    db=provision(root,host_setup.local_docker())
            mark('Persistent development PostgreSQL',db.get('status')=='ready',db.get('reason','Optional development database not configured'),optional=True)
            if db.get('status')=='ready':
                print(f"      pgAdmin / psql: {db['host']}:{db['port']} · database {db['database']} · user {db['development_user']}")
                print(f"      Private developer credentials: {db['credentials_file']}")
                print(tr(lang,'      Configure your application locally; automated tests remain isolated.','      Configurez votre application localement ; les tests restent isolés.'))

        gui=host_setup.offer_pgadmin(interactive=interactive,ask=yesno,lang=lang)
        mark('pgAdmin (optional desktop GUI)',gui=='ready',gui,optional=True)

    # Mobile: reuse already installed CLI/MCP and never start an emulator.
    mobile_before=mobile_test_readiness(e2e,flutter=flutter,lead_agents=lead_agents)
    checkpoint('backend_testing')
    mobile_approved=testing_category_approval('Mobile testing', mobile_before,
                                                interactive=interactive, lang=lang,
        resume_approved=bool(progress.get('approvals',{}).get(approval_for('mobile'))),
        on_approve=lambda:remember_approval('mobile'))
    if mobile_approved and e2e=='maestro':
        missing_host = []
        if not mobile_before.get('Java >= 17',True): missing_host.append('java')
        if not ok_bin('curl'): missing_host.append('curl')
        host_setup.install(missing_host,interactive=interactive,ask=yesno,lang=lang)
        maestro_setup(install=not mobile_before.get('Maestro CLI',False),
                      yes=True,language=lang,register_mcp=True,agents=lead_agents)
    elif mobile_approved and e2e not in ('maestro','none'):
        print(tr(lang,'    Selected mobile runner is project-specific; Builder will configure missing app tests.',
                      '    Le test mobile choisi dépend du projet ; sa configuration sera assurée lors du développement.'))
    if flutter is not None:
        mark('Flutter SDK',ok_bin('flutter'),'Install Flutter SDK and run flutter doctor')
    mobile_after=mobile_test_readiness(e2e,flutter=flutter,lead_agents=lead_agents)
    for tool,ready in mobile_after.items():
        mark('Mobile '+tool,ready,f'Prepare {tool} for mobile testing',
             optional=not mobile_approved)
    if reviewer=='codex':
        print(tr(lang,'    Codex Lead: user-scoped MCP registration skipped (not role-isolated).',
                      '    Pilotage Codex : MCP global ignoré (non isolé par rôle).'))

    # Web: one approval covers Playwright MCP, Test and Chromium. A previously
    # complete environment never triggers a prompt, even on repeated init.
    from playwright_tools import environment_ready, mcp_registered, package_installed, chromium_installed, browser_gate, node_major
    web_before={
        'Node.js 20+':(node_major() or 0)>=20,
        'npm':ok_bin('npm'), 'npx':ok_bin('npx'),
        'Playwright Test':package_installed(root),
        'Playwright Chromium':chromium_installed(root),
        **{f'{agent} Playwright MCP':mcp_registered(agent,root=root) for agent in lead_agents},
    }
    checkpoint('mobile_testing')
    web_approved=testing_category_approval('Web E2E',web_before,
                                             interactive=interactive,lang=lang,
        resume_approved=bool(progress.get('approvals',{}).get(approval_for('web'))),
        on_approve=lambda:remember_approval('web'))
    if web_approved:
        missing_host = []
        if not web_before.get('Node.js 20+',True): missing_host.append('node')
        if not web_before.get('npm',True) or not web_before.get('npx',True): missing_host.append('npm')
        host_setup.install(missing_host,interactive=interactive,ask=yesno,lang=lang)
        playwright_setup(root,active_agents=lead_agents,interactive=True,approved=True)
    web_after={
        'Node.js 20+':(node_major() or 0)>=20,
        'npm':ok_bin('npm'), 'npx':ok_bin('npx'),
        'Playwright Test':package_installed(root),
        'Playwright Chromium':chromium_installed(root),
        **{f'{agent} Playwright MCP':mcp_registered(agent,root=root) for agent in lead_agents},
    }
    for tool,ready in web_after.items():
        mark('Web '+tool,ready,f'Prepare {tool} for web E2E',optional=not web_approved)
    # Refresh after tooling installation. Do not replace real web-app gates
    # with a generic sandbox harness test command.
    current, _ = workspace_checks.refresh(root, current)

    checkpoint('web_testing')
    ui.section(tr(lang, 'Native chat integration', 'Intégration des chats natifs'))
    from native_hooks import (installed as hooks_installed, install_native_hooks,
                              codex_trust_status, refresh_central_provider_hooks)
    # Repair version-pinned hooks on every init/repair, even when the project
    # already has a registered hook configuration from an older engine.
    refresh_central_provider_hooks(root)
    # GO already authorizes local configuration. Hook installation is not
    # hook *trust*: the provider must record that separately.
    if interactive and all(available.get(a,False) for a in required) and not hooks_installed(root):
        try: install_native_hooks(root,approve=True)
        except (OSError,ValueError) as exc: print('    Hooks:',exc)
    mark('Registered-session hooks',hooks_installed(root),
         'Configure project hooks')
    if 'codex' in required and hooks_installed(root):
        trust=codex_trust_status(root)
        if interactive and trust['status'] != 'trust_recorded':
            ui.section(tr(lang, 'Codex hook approval', 'Autorisation des hooks Codex'), indent=4)
            ui.note(tr(lang,
                'Codex has four Gaston project hooks awaiting trust. They recognize registered sessions, enforce role permissions, and handle lifecycle events.',
                'Codex attend votre autorisation pour quatre hooks Gaston. Ils reconnaissent les sessions, protègent les rôles et gèrent leur cycle de vie.'), indent=6, tone=None)
            ui.note(tr(lang,
                'The wizard opens Codex for this one-time security decision. Check that the source is this workspace and the commands invoke .agentic/native_hooks.py codex. Approve only if they match.',
                'L’assistant ouvre Codex pour cette validation de sécurité. Vérifiez que la source est ce projet et que les commandes appellent .agentic/native_hooks.py codex. N’approuvez que si elles correspondent.'), indent=6)
            ui.note(tr(lang,
                'After Codex saves approval, setup returns automatically. If Codex stays open, exit its chat to resume; no extra Gaston commands are needed.',
                'Après enregistrement de l’accord dans Codex, l’installation reprend automatiquement. Si Codex reste ouvert, quittez son chat pour revenir ici.'), indent=6)
            if yesno(tr(lang, 'Open Codex to review these hooks?',
                            'Ouvrir Codex pour vérifier ces hooks ?'),
                     default=True, interactive=True, lang=lang):
                # Keep the trust choice within init. Codex records its own
                # approval; no bypass flag, writing provider trust or fake PASS.
                try: open_codex_hook_review(root)
                except (OSError,KeyboardInterrupt) as exc:
                    print(tr(lang, f'    Codex hook review not completed: {exc}',
                                  f'    Vérification des hooks Codex interrompue : {exc}'))
                trust=codex_trust_status(root)
        if trust['status']=='trust_recorded':
            ui.result(tr(lang, 'Codex hook trust', 'Validation des hooks Codex'), 'OK',
                      detail=tr(lang,'Provider trust decisions recorded; runtime activation unverified',
                                'Décisions enregistrées ; activation réelle non vérifiée'))
        else:
            ui.result(tr(lang, 'Codex hook trust', 'Validation des hooks Codex'), 'WARNING',
                      detail=tr(lang,'Pending or unverified in Codex; core MCP delegation does not depend on these hooks',
                                'En attente ou non vérifié dans Codex ; la délégation MCP reste indépendante'))
        # Do not claim hook activation from file presence or local trust hashes.

    from session_store import Store
    report_registered_sessions(Store(root), builder, reviewer, lang)

    checkpoint('native_sessions')
    ui.step(4, tr(lang, 'Project testing — current readiness',
                       'Tests des projets — état actuel'))
    # This is a snapshot, not a permanent test registry. Newly added repos
    # are discovered when the Lead starts work and before verification.
    projects = current.get('project', {}).get('discovered_roots', {})
    for kind, en, fr in [('java', 'Backend', 'Backend'),
                         ('flutter', 'Flutter', 'Flutter'),
                         ('web', 'Web', 'Web')]:
        roots = projects.get(kind, [])
        ui.row(tr(lang, en, fr), ', '.join(roots),
               state=tr(lang, 'Detected', 'Détecté') if roots
               else tr(lang, 'Not detected', 'Non détecté'))
    runnable = []
    unavailable = []
    for check in current.get('checks', []):
        command = check.get('command', [])
        if not command:
            continue
        cwd = (root / check.get('cwd', '.')).resolve()
        if not cwd.is_relative_to(root) or not cwd.is_dir():
            continue
        flag = check.get('when_path_exists')
        if flag and not (cwd / flag).exists():
            continue
        binary = command[0]
        ready = (cwd / binary).is_file() if binary.startswith('./') else bool(shutil.which(binary))
        if ready:
            ready, _ = workspace_checks.runner_preflight(root, check)
        (runnable if ready else unavailable).append(check['name'])
    print()
    ui.row(tr(lang, 'Runnable', 'Exécutable'), ', '.join(runnable) if runnable else tr(lang, 'None yet', 'Aucun pour le moment'))
    if unavailable:
        ui.row(tr(lang, 'Not ready', 'Non prêt'), ', '.join(unavailable))
    ui.note(tr(lang, 'New repositories and tests are discovered automatically during development.',
                    'Les nouveaux dépôts et tests sont détectés automatiquement pendant le développement.'), indent=4)

    import serena_tools
    semantic_ready=serena_tools.supported(serena_tools.executable(root))
    if not semantic_ready and shutil.which('uv') and interactive:
        if yesno(tr(lang,'Install optional workspace-local Serena semantic code tools?', 'Installer les outils sémantiques Serena facultatifs dans cet espace ?'),default=False,interactive=interactive,lang=lang):
            semantic_ready=serena_tools.setup(root,approved=True)
    mark('Serena semantic code tools',semantic_ready,'Optional: install uv then rerun dev init; native tools remain available',optional=True)

    ui.step(5, tr(lang, 'Summary', 'Résumé'))
    deferred_items=[s for s in statuses if s['status']=='DEFERRED']
    checkpoint('test_commands')
    report={'ready':not pending,'language':lang,'tool_name':TOOL_NAME,
            'checks':statuses,'blockers':list(dict.fromkeys(pending)),
            'deferred':deferred_items,'mobile_app_present':flutter is not None,
            'preferences':pref,'project':str(root)}
    local.mkdir(parents=True,exist_ok=True)
    _atomic_json(local/'setup-status.json', report)
    progress['state'] = 'ready' if not pending else 'incomplete'
    progress.setdefault('steps', {})['summary'] = 'saved'
    save_progress()
    if pending:
        ui.result(tr(lang, f'Setup incomplete — {len(report["blockers"])} action(s) remaining',
                           f'Configuration incomplète — {len(report["blockers"])} action(s) restante(s)'),
                  'WARNING', indent=2)
        for i, issue in enumerate(report['blockers'], 1):
            ui.note(f'{i}. {translate(issue)}', indent=4, tone=None)
        ui.note(tr(lang, 'Your progress is saved. Resolve these items, then rerun dev init.',
                        'Votre progression est enregistrée. Corrigez ces points, puis relancez dev init.'),
                indent=2)
    else:
        ui.result(tr(lang, 'Setup complete.', 'Configuration terminée.'), 'OK', indent=2)
        print('  ' + tr(lang, 'Run dev to start.', 'Lancez dev pour démarrer.'))
    print()
    return not pending
