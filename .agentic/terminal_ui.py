"""Small dependency-free terminal presentation layer for Gaston setup.

ANSI is opt-in for actual terminals and never leaks into redirected output,
NO_COLOR, dumb terminals, or test logs. This module never changes setup state.
"""
from __future__ import annotations
import os
import shutil
import sys
import textwrap

CODES = {'blue': '94', 'green': '32', 'amber': '33', 'red': '31',
         'muted': '90', 'bold': '1'}


def supports_color(stream=None) -> bool:
    stream = sys.stdout if stream is None else stream
    return (not os.environ.get('NO_COLOR') and 'NO_COLOR' not in os.environ
            and os.environ.get('TERM', '') != 'dumb'
            and os.environ.get('CLICOLOR') != '0'
            and bool(getattr(stream, 'isatty', lambda: False)()))


def paint(text, tone: str, *, stream=None) -> str:
    value = str(text)
    if tone not in CODES or not supports_color(stream):
        return value
    return f'\x1b[{CODES[tone]}m{value}\x1b[0m'


def step(number: int, label: str) -> None:
    print('\n' + paint(f'STEP {number}/5', 'blue') + '  ' + paint(label, 'bold'))


def section(label: str, *, indent=2) -> None:
    print(' ' * indent + paint(label, 'blue'))


def heading(name: str, subheading: str = '') -> None:
    print('\n' + paint(name.upper(), 'bold'))
    if subheading:
        print('  ' + paint(subheading, 'blue'))


def result(label: str, status: str, *, detail: str = '', indent: int = 4) -> None:
    """Consistent machine-readable state labels, independently of ANSI color."""
    tone = {'OK': 'green', 'READY': 'green', 'MISSING': 'red',
            'FAILED': 'red', 'ERROR': 'red', 'WARNING': 'amber',
            'TIMEOUT': 'amber', 'SKIPPED': 'muted', 'INFO': 'muted',
            'OPTIONAL': 'muted', 'DEFERRED': 'muted'}.get(status, 'muted')
    prefix = paint(f'{status:<8}', tone)
    print(' ' * indent + prefix + '  ' + str(label))
    if detail:
        note(detail, indent=indent + 10, tone='muted' if status in ('INFO', 'OPTIONAL','SKIPPED') else None)


def note(message: str, *, indent: int = 4, tone: str | None = 'muted') -> None:
    """Wrap explanatory notes; never truncate useful failure information."""
    width = max(38, min(104, shutil.get_terminal_size(fallback=(90, 24)).columns) - indent)
    paragraphs = str(message).splitlines() or ['']
    for paragraph in paragraphs:
        for line in textwrap.wrap(paragraph, width=width, break_long_words=False,
                                  break_on_hyphens=False) or ['']:
            print(' ' * indent + (paint(line, tone) if tone else line))


def prompt_label(question: str, *, default: str | None = None) -> str:
    """Color only the question; never hide or obscure the explicit default."""
    if default:
        first, sep, tail = question.partition(default)
        if sep:
            return paint(first, 'bold') + paint(sep + tail, 'muted')
    return paint(question, 'bold')


def row(label: str, value: str, *, state: str = '', width: int = 11,
        indent: int = 4) -> None:
    suffix = paint(state, 'green' if state in ('Detected', 'Détecté') else 'muted') if state else ''
    shown = f'{label:<{width}}' + ('  ' + suffix if suffix else '')
    if value:
        shown += ('  ' if state else '  ') + str(value)
    print(' ' * indent + shown)
