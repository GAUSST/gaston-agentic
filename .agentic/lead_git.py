"""Purpose-built Git operations for the enrolled Engineering Lead.

Read-only inspection needs no Builder. Local commits require the developer's
explicit request and exact, individual file paths; no shell or remote Git verbs.
"""
from __future__ import annotations
from project_layout import project_state, project_state_name, project_root

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import workspace_git

MAX_FILES = 250
MAX_DIFF = 48000


def _git(repo: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Status/diff/read commands cannot create Git index lock files.
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_PAGER'] = 'cat'
    return subprocess.run(['git', '-c', 'color.ui=false', *args], cwd=repo, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False)


def _text(b: bytes, limit: int = MAX_DIFF) -> str:
    return b[:limit].decode('utf-8', 'replace')


def _repo(root: Path, project: dict | None, repo_path: str) -> Path:
    if not isinstance(repo_path, str) or not repo_path or repo_path.startswith('/'):
        raise ValueError('Choose a workspace-relative repository returned by git_inspect')
    if repo_path != '.' and ('..' in Path(repo_path).parts or Path(repo_path).is_absolute()):
        raise ValueError('Repository must be workspace-relative')
    root = root.resolve()
    repos = workspace_git.repositories(root, project)
    chosen = (root / repo_path).resolve()
    if chosen not in repos:
        raise ValueError('Repository not discovered under this workspace')
    return chosen


def _status_entries(repo: Path) -> list[dict]:
    status = _git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--', '.', *workspace_git.git_exclusion_pathspecs())
    if status.returncode != 0:
        raise ValueError('Git status failed: ' + _text(status.stderr, 500))
    fields = status.stdout.split(b'\0')
    entries = []
    i = 0
    while i < len(fields):
        item = fields[i]
        i += 1
        if not item:
            continue
        if len(item) < 4 or item[2:3] != b' ':
            raise ValueError('Malformed Git porcelain record')
        xy = item[:2].decode('ascii', 'replace')
        name = os.fsdecode(item[3:])
        entry = {'path': name, 'index': xy[0], 'worktree': xy[1],
                 'untracked': xy == '??'}
        # With -z, rename/copy destination is first, origin is next.
        if 'R' in xy or 'C' in xy:
            if i >= len(fields) or not fields[i]:
                raise ValueError('Missing Git rename/copy origin')
            entry['previous_path'] = os.fsdecode(fields[i])
            i += 1
        entries.append(entry)
    return entries


def _path_categories(entry: dict) -> list[str]:
    code = entry['index'] + entry['worktree']
    if entry['untracked']:
        return ['untracked']
    names = []
    for letter, label in [('A', 'added'), ('M', 'modified'), ('D', 'deleted'),
                          ('R', 'renamed'), ('C', 'copied'), ('U', 'unmerged')]:
        if letter in code:
            names.append(label)
    if entry['index'] not in (' ', '?'):
        names.append('staged')
    if entry['worktree'] not in (' ', '?'):
        names.append('unstaged')
    return names


def inspect(root: Path, project: dict | None = None, *, repository: str | None = None,
            diff: bool = False, task_id: str | None = None) -> dict:
    root = root.resolve()
    if not shutil.which('git'):
        return {'status': 'blocked', 'reason': 'Git CLI not installed'}
    try:
        repos = workspace_git.repositories(root, project)
        if repository is not None:
            repos = [_repo(root, project, repository)]
        output = []
        for repo in repos:
            label = repo.relative_to(root).as_posix()
            branch = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD')
            head = _git(repo, 'rev-parse', '--verify', 'HEAD')
            changes = _status_entries(repo)
            counts = {key: sum(key in _path_categories(e) for e in changes)
                      for key in ('untracked','added','modified','deleted','renamed','staged','unstaged','unmerged')}
            item = {'repository': label,
                    'branch': _text(branch.stdout, 160).strip() if branch.returncode == 0 else None,
                    'head_exists': head.returncode == 0,
                    'head': _text(head.stdout, 80).strip() if head.returncode == 0 else None,
                    'counts': counts, 'changed_files_total': len(changes),
                    'changes': changes[:MAX_FILES], 'truncated': len(changes) > MAX_FILES}
            if diff:
                patch = _git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--', '.', *workspace_git.git_exclusion_pathspecs()) if head.returncode == 0 else None
                if patch is not None and patch.returncode == 0:
                    item['diff'] = _text(patch.stdout)
                    item['diff_truncated'] = len(patch.stdout) > MAX_DIFF
                else:
                    item['diff'] = None
                    item['diff_note'] = 'No HEAD or diff failed; use changed files list to inspect sources'
                item['diff_note'] = 'Git diff excludes untracked file content; their paths are listed above.'
            output.append(item)
        result = {'status': 'ready', 'repositories': output, 'repository_count': len(output)}
        if not output:
            result['note'] = 'No Git roots found inside this workspace; not evidence that Git is absent elsewhere.'
        if task_id is not None:
            if not isinstance(task_id,str) or not re.fullmatch('[a-z0-9-]{8,120}',task_id):
                raise ValueError('Invalid task ID')
            folder = project_state(root) / 'runs' / task_id
            if folder.is_symlink() or not (folder / 'task.json').is_file():
                raise ValueError('Unknown task')
            def _read(path: str) -> dict:
                try:
                    value = json.loads((folder / path).read_text(encoding='utf-8'))
                    return value if isinstance(value,dict) else {}
                except (OSError,ValueError):
                    return {}
            build = _read('build.json')
            reported = build.get('changed_files', [])
            if not isinstance(reported,list):
                reported = []
            visible = {str(Path(item['repository']) / entry['path']).removeprefix('./')
                       for item in output for entry in item['changes']}
            result['task'] = {'task_id':task_id,
                              'builder_reported_paths':reported[:MAX_FILES],
                              'currently_visible_as_uncommitted':sorted(set(reported) & visible),
                              'not_currently_visible_as_uncommitted':sorted(set(reported) - visible),
                              'preexisting_change_overlaps':_read('existing-change-overlaps.json').get('changed_by_builder',[]),
                              'attribution_note':'Builder reports intent; Git shows current tree, not guaranteed historical ownership. A missing diff may be committed, removed, or outside Git.'}
        return result
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return {'status':'blocked','reason':str(exc)[:650]}


def _literal_paths(repo: Path, paths: list[str]) -> list[str]:
    if not isinstance(paths,list) or not 1<=len(paths)<=75:
        raise ValueError('Provide 1–75 explicit file paths, not directories or globs')
    result=[]
    for raw in paths:
        if not isinstance(raw,str) or not raw or len(raw)>500 or '\0' in raw or '\n' in raw:
            raise ValueError('Invalid file path')
        path = Path(raw)
        if path.is_absolute() or '..' in path.parts or str(path) in ('.', ''):
            raise ValueError('Paths must be repository-relative and cannot traverse directories')
        candidate=(repo / path).resolve()
        if not candidate.is_relative_to(repo.resolve()):
            raise ValueError('Path escapes the selected repository')
        if candidate.is_dir():
            raise ValueError('Provide individual file paths; directory-wide commits are not allowed')
        if workspace_git.is_excluded_path(path.as_posix()):
            raise ValueError('Private Gaston/runtime and generated paths are excluded from application Git operations')
        result.append(path.as_posix())
    if len(set(result))!=len(result):
        raise ValueError('Duplicate commit paths')
    return result


def commit(root: Path, project: dict | None = None, *, repository: str,
           paths: list[str], message: str, explicit_user_request: bool,
           requested_by_user: str) -> dict:
    """Scoped local commit only. Never a push, reset, branch change or arbitrary shell.

    explicit_user_request is an auditable Lead claim; the actual conversation and
    host tool permissions remain the authority for approving a Git write.
    """
    if explicit_user_request is not True or not isinstance(requested_by_user,str) or len(requested_by_user.strip())<8:
        return {'status':'blocked','reason':'A direct developer Git-commit request must exist; no autonomous Git writes'}
    if not isinstance(message,str) or not 5<=len(message.strip())<=160 or '\n' in message:
        return {'status':'blocked','reason':'Provide a meaningful one-line commit message'}
    try:
        repo = _repo(root.resolve(), project, repository)
        selected = _literal_paths(repo, paths)
        entries = _status_entries(repo)
        unrelated_staged=[e['path'] for e in entries
                          if e['index'] not in (' ', '?') and e['path'] not in set(selected)]
        if unrelated_staged:
            return {'status':'blocked','reason':'Unrelated staged files detected; refusing to include them in the commit',
                    'staged_paths':unrelated_staged[:75]}
        changed = {e['path'] for e in entries}
        # Deletions, modifications and untracked files must be explicit.
        unexpected = [p for p in selected if p not in changed]
        if unexpected:
            return {'status':'blocked','reason':'Selected paths are not currently changed', 'paths':unexpected}
        selected_specs = [':(literal)' + name for name in selected]
        # Avoid git hooks executing arbitrary scripts behind a benign commit tool.
        args = ['-c', 'core.hooksPath=/dev/null']
        add = _git(repo, *args, 'add', '-A', '--', *selected_specs)
        if add.returncode != 0:
            return {'status':'blocked','reason':'Git add failed', 'error':_text(add.stderr,600)}
        stage = _git(repo, 'diff', '--cached', '--name-only', '-z')
        staged = sorted(os.fsdecode(x) for x in stage.stdout.split(b'\0') if x)
        if stage.returncode or not staged or set(staged) != set(selected):
            # Do not commit anything not explicitly selected.
            return {'status':'blocked','reason':'Staging was not exactly the selected file set; nothing committed',
                    'staged_files':staged, 'manual_review_required':True}
        check = _git(repo, 'diff', '--cached', '--check')
        if check.returncode:
            return {'status':'blocked','reason':'Staged patch failed whitespace/conflict check; nothing committed',
                    'evidence':_text(check.stdout+check.stderr,900)}
        before = _git(repo, 'rev-parse', '--verify', 'HEAD')
        done = _git(repo, *args, 'commit', '-m', message.strip(), timeout=60)
        if done.returncode:
            return {'status':'blocked','reason':'Git commit failed; staged paths may remain',
                    'evidence':_text(done.stdout+done.stderr,1300)}
        after = _git(repo, 'rev-parse', '--verify', 'HEAD')
        committed = _git(repo, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z', 'HEAD')
        actual = sorted(os.fsdecode(x) for x in committed.stdout.split(b'\0') if x)
        return {'status':'pass' if after.returncode == 0 and actual == staged else 'blocked',
                'repository':repo.relative_to(root.resolve()).as_posix(),
                'previous_head':_text(before.stdout,60).strip() if before.returncode == 0 else None,
                'commit':_text(after.stdout,60).strip() if after.returncode == 0 else None,
                'committed_files':actual,
                'note':'Local commit only; never pushed. Commit hooks disabled for bounded execution. Developer authorization is enforced by policy/host permission, not inferred from this argument.'}
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return {'status':'blocked','reason':str(exc)[:650]}
