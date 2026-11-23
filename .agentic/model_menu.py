"""Model selection and per-model CLI reasoning capability checks.

Claude choices use explicit published IDs. Codex's local, experimental ``codex debug models``
metadata is the normal source of supported per-model efforts. Hidden catalog
entries remain available for exact ID input. If metadata is missing, a single
explicitly selected pair can be checked with an ephemeral Codex turn. Neither
metadata nor historical check evidence guarantees future account access.
"""
from __future__ import annotations
from functools import lru_cache
import json
import re
import shutil
import subprocess
import tempfile

CLAUDE_MODELS = [(model, model) for model in (
    'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5')]
CLAUDE_ADVISOR_DEFAULT = 'claude-opus-5-5'
CLAUDE_ADVISORS = (CLAUDE_ADVISOR_DEFAULT, 'claude-fable-5-2', 'off')
_LEGACY_CLAUDE = dict(zip(('sonnet', 'opus', 'haiku'), (m for m, _ in CLAUDE_MODELS)))


def claude_model_id(model):
    return _LEGACY_CLAUDE.get(model, model)


def advisor_id(config):
    saved = config.get('advisor', {}).get('claude_reviewer')
    # Old installations persisted the former default alias even if the user
    # never selected it. Migrate that alias to the new default; pinned IDs win.
    return CLAUDE_ADVISOR_DEFAULT if saved in (None, '', 'fable', 'opus') else saved


def normalize_models(config):
    for key, model in config.get('models', {}).items():
        if key.startswith('claude_') and isinstance(model, str):
            config['models'][key] = claude_model_id(model)
    if config.get('preferences', {}).get('reviewer') == 'claude' or 'advisor' in config:
        config.setdefault('advisor', {})['claude_reviewer'] = advisor_id(config)
    return config


def valid_model_id(provider, value):
    if not isinstance(value, str) or len(value) > 128:
        return False
    if provider == 'codex':
        return bool(_MODEL_ID.fullmatch(value))
    return provider == 'claude' and bool(re.fullmatch(r'claude-[a-z0-9]+(?:-[a-z0-9]+)+', value))

# These are *possible* levels, not a promise for every account/model.
REASONING_LEVELS = {
    'claude': ('low', 'medium', 'high', 'xhigh', 'max'),
    'codex': ('low', 'medium', 'high', 'xhigh', 'max'),
}
# Gaston's permitted Codex scale; catalog entries and direct checks use the same filter.
CODEX_EFFORT_ORDER = REASONING_LEVELS['codex']
CODEX_EXCLUDED_EFFORTS = ('none', 'minimal', 'ultra')
CODEX_FAMILIES = ('luna', 'sol', 'astra')
_MODEL_ID = re.compile(r'gpt-[\w.\-]+\Z')


class CodexCatalog(dict):
    """Full metadata with picker visibility kept separate."""
    def __init__(self):
        super().__init__()
        self.hidden = set()


def parse_codex_catalog(value, *, include_hidden=False):
    """Return {model_id: supported_effort_tuple} from the model entries ONLY.

    Accepts both the modern `supported_reasoning_levels` catalog field and the
    older `supported_reasoning_efforts` field. Never guesses from an API schema.
    Empty tuple means the CLI supplied no usable supported-effort information.
    """
    entries = value.get('models') if isinstance(value, dict) else value
    if not isinstance(entries, list):
        return {}
    catalog = CodexCatalog()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get('slug') or entry.get('model') or entry.get('id')
        if not isinstance(slug, str) or not _MODEL_ID.fullmatch(slug):
            continue
        hidden = entry.get('show_in_picker') is False or entry.get('visibility') in ('hide', 'hidden')
        if hidden:
            if not include_hidden:
                continue
            catalog.hidden.add(slug)
        if entry.get('supported_reasoning_levels') is not None:
            raw = entry['supported_reasoning_levels']
        else:
            raw = entry.get('supported_reasoning_efforts')
        levels = []
        if isinstance(raw, list):
            for choice in raw:
                if isinstance(choice, dict):
                    level = choice.get('effort') or choice.get('value') or choice.get('level')
                elif isinstance(choice, str):
                    level = choice
                else:
                    continue
                if level in CODEX_EFFORT_ORDER and level not in levels:
                    levels.append(level)
        catalog[slug] = tuple(sorted(levels, key=CODEX_EFFORT_ORDER.index))
    return catalog


@lru_cache(maxsize=1)
def codex_catalog_details():
    """Read the local Codex CLI catalog without making a model inference call."""
    if not shutil.which('codex'):
        return {}
    try:
        result = subprocess.run(['codex', 'debug', 'models'], capture_output=True,
                                text=True, timeout=25, check=False)
        if result.returncode:
            return {}
        return parse_codex_catalog(json.loads(result.stdout), include_hidden=True)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def codex_catalog():
    details = codex_catalog_details()
    return sorted(set(details) - getattr(details, 'hidden', set()))


def model_choices(provider, role, *, catalog=None):
    """Up to three discoverable model IDs; custom ID remains a separate option.

    Labels are *exact CLI model IDs*, never ambiguous family nicknames.
    """
    if provider == 'claude':
        return list(CLAUDE_MODELS)
    if provider != 'codex':
        raise ValueError('Unsupported provider')
    available = codex_catalog() if catalog is None else [m for m in catalog if m not in getattr(catalog, 'hidden', set())]
    families = ('luna', 'sol', 'astra') if role == 'builder' else ('sol', 'astra', 'luna')
    choices = []
    for family in families:
        matched = [slug for slug in available if isinstance(slug, str)
                   and _MODEL_ID.fullmatch(slug)
                   and re.search(r'(?<![a-z])' + family + r'(?![a-z])', slug)]
        if matched:
            def version_order(value):
                return (tuple(int(n) for n in re.findall(r'\d+', value)), value)
            selected = max(matched, key=version_order)
            choices.append((selected, selected))
    return choices


def recommended_model(provider, role, *, catalog=None):
    choices = model_choices(provider, role, catalog=catalog)
    return choices[0][0] if choices else None


def effort_choices(provider, model=None, *, catalog=None, verified=None):
    """Return model-specific options for Codex, documented levels for Claude.

    `catalog` can be a parsed {model: efforts} map in tests. Missing Codex
    metadata is NOT treated as support for generic options.
    """
    if provider == 'claude':
        return REASONING_LEVELS['claude']
    if provider != 'codex':
        raise ValueError('Unsupported provider')
    if model is None:  # Only for callers displaying the generic scale.
        return REASONING_LEVELS['codex']
    details = codex_catalog_details() if catalog is None else catalog
    return tuple(e for e in details.get(model, ()) if e in CODEX_EFFORT_ORDER) or verified_efforts(verified, model)


def configured_effort(config, provider, role, *, catalog=None):
    key = provider + '_' + role
    current = config.get('reasoning', {}).get(key)
    model = config.get('models', {}).get(key)
    levels = effort_choices(provider, model=model, catalog=catalog, verified=config.get('codex_verified_pairs')) if model else REASONING_LEVELS[provider]
    target = 'low' if role == 'builder' else 'medium'
    # Do not silently downgrade a saved choice after switching models. The
    # per-model preflight rejects incompatible choices with a concrete error.
    valid_values = CODEX_EFFORT_ORDER if provider == 'codex' else REASONING_LEVELS['claude']
    if current in valid_values or (provider == 'codex' and current in CODEX_EXCLUDED_EFFORTS):
        # Retain a removed saved value only to report a blocker, not to launch it.
        return current
    if target in levels or not levels:
        return target
    return levels[0]


def verify_codex_model_effort(model, effort, *, catalog=None, verified=None):
    """Return (ok, reason) using *installed CLI* metadata, not API guesses."""
    if effort not in CODEX_EFFORT_ORDER:
        return False, f'Reasoning {effort!r} is not enabled in Gaston. Choose a permitted level in ./dev models.'
    details = codex_catalog_details() if catalog is None else catalog
    levels = tuple(e for e in details.get(model, ()) if e in CODEX_EFFORT_ORDER)
    if not levels:
        if valid_model_id('codex', model) and effort in verified_efforts(verified, model):
            return True, 'Previously accepted by a direct Codex execution; current account access is checked on execution.'
        return False, f'No verified Codex metadata or execution evidence for {model!r} with reasoning {effort!r}.'
    if effort not in levels:
        return False, f'{model} does not list reasoning {effort!r} (supported: {", ".join(levels)}).'
    return True, 'CLI catalog supports this model/effort pair; actual account access remains untested.'


class ModelCatalogUnavailable(ValueError):
    """There is no model whose Codex reasoning capabilities can be verified."""


def model_selection_error(provider, model, *, catalog=None, verified=None):
    if not valid_model_id(provider, model):
        return 'Enter an exact model ID.'
    if provider == 'codex':
        details = codex_catalog_details() if catalog is None else catalog
        if not details.get(model) and not verified_efforts(verified, model):
            return f'No verified reasoning metadata for {model!r}.'
    return None


def pick_model(provider, role, previous='', *, catalog=None, lang='en', options=None, verified=None, manual_check=None):
    """Only return verified Codex IDs (or locally valid exact Claude IDs)."""
    def t(en, fr): return fr if lang == 'fr' else en
    if provider == 'codex':
        catalog = codex_catalog_details() if catalog is None else catalog
        usable = {m: levels for m, levels in catalog.items() if levels and valid_model_id('codex', m) and m not in getattr(catalog, 'hidden', set())}
        if not usable and not any(catalog.values()) and manual_check is None:
            raise ModelCatalogUnavailable('No selectable Codex models with verified reasoning. Update the Codex CLI and retry setup.')
        options = model_choices(provider,role,catalog=usable) if options is None else options
        options = [(m,l) for m,l in options if m in usable]
        if not options:
            options = [(m,m) for m in sorted(usable)[:3]]
    elif options is None:
        options = model_choices(provider,role)
    default = previous if not model_selection_error(provider,previous,catalog=catalog,verified=verified) else None
    default = default or (options[0][0] if options else None)
    if previous and model_selection_error(provider,previous,catalog=catalog,verified=verified):
        print('    '+model_selection_error(provider,previous,catalog=catalog,verified=verified))
    for i,(model,label) in enumerate(options,1):
        print(f'      {i}. {label}'+(t(' (recommended)',' (recommandé)') if i==1 else ''))
    manual = str(len(options)+1)
    numbers = {str(i): m for i,(m,_) in enumerate(options,1)}
    print(t(f'      {manual}. Custom model ID',f'      {manual}. Identifiant de modèle personnalisé'))
    suffix=t(f'[default: {default}]',f'[défaut : {default}]') if default else t('[required]','[requis]')
    question=t(f'    Choose model (1–{manual} or exact ID) {suffix}: ',f'    Choisissez le modèle (1–{manual} ou identifiant exact) {suffix} : ')
    while True:
        raw=input(question).strip()
        if raw==manual:
            if provider=='codex':
                print('    '+t('Available IDs: ','Identifiants disponibles : ')+', '.join(sorted(usable)))
            raw=input(t('    Enter exact model ID [required]: ','    Saisissez l’identifiant exact [requis] : ')).strip()
            # A blank custom ID is not consent to the default.
            candidate=raw
        else:
            candidate=numbers.get(raw,raw) if raw else default
        if (provider == 'codex' and valid_model_id(provider, candidate) and
                not catalog.get(candidate) and manual_check is not None):
            # Manual model/effort pairs must be accepted before the model is saved.
            error = manual_check(candidate)
        else:
            error=model_selection_error(provider,candidate,catalog=catalog,verified=verified)
        if not error:
            return candidate
        print('    '+error+' '+t('Please choose again.','Veuillez réessayer.'))


def pick_effort(provider, role, model, previous='', *, catalog=None, levels=None, lang='en', candidate_only=False):
    if levels is None:
        levels=effort_choices(provider,model,catalog=catalog)
    if provider == 'codex':
        levels = tuple(e for e in levels if e in CODEX_EFFORT_ORDER)
    if not levels:
        raise ModelCatalogUnavailable(f'No verified reasoning levels for {model}.')
    target='low' if role=='builder' else 'medium'
    default=previous if previous in levels else (target if target in levels else levels[0])
    if previous and previous not in levels:
        print(f'    Reasoning {previous!r} is not supported by {model}; choose a supported level.')
    for i,level in enumerate(levels,1):
        print(f'      {i}. {level.title()}'+((' (recommandé)' if lang=='fr' else ' (recommended)') if level==target and not candidate_only else ''))
    numbers={str(i):level for i,level in enumerate(levels,1)}
    question=(f'    Choisissez le raisonnement (1–{len(levels)} ou niveau exact) [défaut : {default}] : ' if lang=='fr' else
              f'    Choose reasoning (1–{len(levels)} or exact level) [default: {default}]: ')
    while True:
        raw=input(question).strip().lower()
        result=numbers.get(raw,raw) if raw else default
        if result in levels:
            return result
        print('    Niveau invalide ; réessayez.' if lang=='fr' else '    Invalid reasoning level; please choose again.')


def verified_efforts(verified, model):
    """Only the exact requested pairs previously accepted by Codex, not guessed levels."""
    values = verified.get(model, ()) if isinstance(verified, dict) else ()
    return tuple(e for e in CODEX_EFFORT_ORDER if isinstance(values, list) and e in values)


def direct_codex_check(model, effort):
    """One ephemeral read-only inference, only for a selected undocumented pair.

    A completed turn shows acceptance for that request, not future entitlement
    or independent proof of the server's internal reasoning implementation.
    """
    if not valid_model_id('codex', model) or effort not in CODEX_EFFORT_ORDER:
        return False, 'Invalid model or reasoning level.'
    if not shutil.which('codex'):
        return False, 'Codex CLI is not available for the direct check.'
    cmd = ['codex', 'exec', '--json', '--ephemeral', '--skip-git-repo-check',
           '--ignore-user-config', '--sandbox', 'read-only', '-c', 'approval_policy="never"',
           '--model', model, '-c', f'model_reasoning_effort={json.dumps(effort)}',
           'Reply with only GASTON_MODEL_OK. Do not use tools or inspect files.']
    try:
        with tempfile.TemporaryDirectory(prefix='gaston-model-check-') as tmp:
            result = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                                    timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, 'Codex direct check could not complete (CLI error or timeout).'
    events = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict): events.append(event)
        except ValueError:
            continue
    failures = [e for e in events if e.get('type') in ('error', 'turn.failed')]
    warnings = result.stderr.lower() + ' ' + ' '.join(str(e.get('message','')) for e in events if e.get('type') in ('warning','error')).lower()
    # Generic *metadata* fallback is normal for an unlisted model. Actual
    # model/effort replacement is not evidence for the requested pair.
    substitution = re.search(r'(?:fall(?:ing)? back to|switch(?:ing|ed) to) (?:the )?(?:default |fallback )?(?:model(?! metadata)|reasoning|gpt-)|using (?:the )?(?:default|fallback) (?:model(?! metadata)|reasoning)|(?:clamp(?:ed|ing)|downgrad(?:ed|ing)).{0,60}(?:effort|model)|(?:effort|model).{0,60}(?:clamp(?:ed|ing)|downgrad(?:ed|ing)|was adjusted)|reasoning effort.{0,80}(?:unsupported|not supported|adjusted)', warnings)
    if result.returncode or failures or substitution:
        detail = ' '.join(str(e.get('message') or e.get('error') or '') for e in failures)
        # Do not echo arbitrary CLI diagnostics (may contain account details).
        if re.search(r'reasoning|effort', detail+' '+warnings, re.I):
            return False, 'Codex rejected or could not verify the requested reasoning level.'
        return False, 'Codex did not accept the requested model/effort (model, account, or CLI error).'
    completed = any(e.get('type') == 'turn.completed' for e in events)
    answer = any(e.get('type') == 'item.completed' and isinstance(e.get('item'), dict)
                 and e['item'].get('type') == 'agent_message'
                 and str(e['item'].get('text', '')).strip() == 'GASTON_MODEL_OK' for e in events)
    if not (completed and answer):
        return False, 'Codex did not return a completed verification turn.'
    return True, 'Codex completed a direct check of the requested model/effort.'


def pick_role_model(provider, role, previous='', *, catalog=None, config=None, lang='en'):
    """Select a model, validating an undocumented pair before returning it.

    Returns (model, already-selected effort or None). Standard catalog choices
    retain the wizard's usual separate reasoning step. Evidence is not saved
    until the caller checkpoints its configuration.
    """
    config = config if config is not None else {}
    verified = config.get('codex_verified_pairs', {})
    manual_effort = None
    def check(candidate):
        nonlocal manual_effort
        if not shutil.which('codex'):
            return 'No verified reasoning metadata; Codex CLI unavailable for direct validation.'
        print('    No published reasoning metadata. Select a candidate level (unverified levels require one short Codex turn using model allowance).')
        old = config.get('reasoning', {}).get(provider+'_'+role, '')
        choice = pick_effort(provider, role, candidate, old, levels=CODEX_EFFORT_ORDER, lang=lang, candidate_only=True)
        if choice not in verified_efforts(verified, candidate):
            print(f'    Checking {candidate} / {choice} directly in Codex...', flush=True)
            ok, reason = direct_codex_check(candidate, choice)
            if not ok: return reason
            if not isinstance(config.get('codex_verified_pairs'), dict):
                config['codex_verified_pairs'] = {}
            pairs = config['codex_verified_pairs']
            pairs[candidate] = list(dict.fromkeys((*verified_efforts(pairs, candidate), choice)))
            print('    '+reason)
        else:
            print('    Reusing previously accepted model/effort.')
        manual_effort = choice
        return None
    selected = pick_model(provider, role, previous, catalog=catalog, lang=lang,
                          verified=verified, manual_check=check if provider=='codex' else None)
    return selected, manual_effort
