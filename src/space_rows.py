"""Render Git row details from Herdr while keeping its native status colors.

Herdr 0.9 elides rows with no resolved tokens and has no minimum-row setting.
Publish a blank or explanatory placeholder when neither enabled Git token has a value.
Herdr's read-only worktree API resolves the workspace repository, including its
identity pane; do not guess from whichever pane is focused. This runs on the slow
collector tick, not on animation frames. It never fetches or changes Git state.
"""
import os
from pathlib import Path
import subprocess

from agent_info import PLUGIN, RpcError, rpc
from sidebar_settings import DEFAULTS, GIT_ICONS

SOURCE = PLUGIN + '.space-rows'
TOKEN = 'space_git_padding'
ICON_TOKEN = 'space_git_icon'
BRANCH_TOKEN = 'space_git_branch'
# An OpenRig team's branch, for rows without the Git icon: Herdr's own branch
# token is empty there, as the space's tiles run outside any repository.
TEAM_TOKEN = 'space_team_branch'
EMPTY = dict.fromkeys((TOKEN, ICON_TOKEN, BRANCH_TOKEN, TEAM_TOKEN))
GIT_ICON = GIT_ICONS[DEFAULTS['git_icon']][1]
PLACEHOLDER_COLOUR = '#585b70'
# Whitespace is normalized away by the API. This zero-width format character
# survives normalization, like the space color shade markers.
BLANK = '\u2063'


def upstream_differs(path):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    try:
        result = subprocess.run(
            ['git', '-C', path, 'rev-list', '--count', '--max-count=1', 'HEAD...@{upstream}'],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Git row check timed out') from error
    # A missing upstream/unborn HEAD has no native ahead/behind indicator.
    return result.returncode == 0 and result.stdout.strip() == '1'


class TeamBranch(str):
    """A branch read from an OpenRig team's checkouts rather than Herdr's own."""


def team_branch(endpoint, directories, settings, lookups=None):
    """(reason, branch) for a space holding an OpenRig team, from its seats' directories.

    The space's own tiles run outside any repository, so Herdr knows no branch for
    it; the seats' checkouts are looked up instead. Only the branch is shown: a
    status row would describe the tiles' directory, not the team's checkout.
    """
    if not settings['show_branch']:
        return 'No Git details', None
    lookups = {} if lookups is None else lookups
    branches = set()
    for directory in directories:
        if directory not in lookups:
            try:
                lookups[directory] = rpc(endpoint, 'worktree.list', {'cwd': directory})
            except RpcError as error:
                if error.code != 'not_git_worktree':
                    raise
                lookups[directory] = None
        result = lookups[directory]
        if result is None:
            continue
        # A seat may work in a subdirectory; its checkout is the deepest one containing it.
        target = Path(directory).resolve()
        found = [item for item in result['worktrees']
                 if target == Path(item['path']).resolve() or Path(item['path']).resolve() in target.parents]
        entry = max(found, key=lambda item: len(Path(item['path']).resolve().parts), default=None)
        if entry:
            branches.add(entry.get('branch') or ('detached' if entry.get('is_detached') else ''))
    branches.discard('')
    if not branches:
        return 'no git repo', None
    return None, TeamBranch(', '.join(sorted(branches)))


def git_details(endpoint, workspace, workspaces, settings, team_dirs=(), lookups=None):
    if not (settings['show_branch'] or settings['show_git']):
        return 'Git details hidden', None
    membership = workspace.get('worktree') or {}
    # Native grouped worktrees suppress both Git tokens in their child rows.
    if membership.get('is_linked_worktree') and any(
        (parent := other.get('worktree')) and not parent.get('is_linked_worktree')
        and parent.get('repo_key') == membership.get('repo_key') for other in workspaces
    ):
        return 'Git details in parent', None
    try:
        result = rpc(endpoint, 'worktree.list', {'workspace_id': workspace['workspace_id']})
    except RpcError as error:
        if error.code == 'not_git_worktree':
            return team_branch(endpoint, team_dirs, settings, lookups) if team_dirs else ('no git repo', None)
        raise
    entries = result['worktrees']
    entry = next((item for item in entries if item.get('open_workspace_id') == workspace['workspace_id']), None)
    if entry is None:
        path = membership.get('checkout_path') or result['source']['source_checkout_path']
        entry = next((item for item in entries if Path(item['path']).resolve() == Path(path).resolve()), None)
    if entry is None:
        raise RuntimeError('Workspace checkout was not returned by Herdr')
    branch = entry.get('branch') if settings['show_branch'] else None
    if branch or settings['show_git'] and upstream_differs(entry['path']):
        return None, branch
    return ('Detached HEAD' if entry.get('is_detached') else 'No Git details'), None


def row_tokens(reason, settings, branch=None):
    mode = settings['empty_git_row']
    glyph = GIT_ICONS[settings['git_icon']][1]
    if not reason or mode == 'hide':
        padding = None
    elif mode == 'placeholder':
        padding = reason
    else:
        padding = BLANK
    combined, icon, team = None, None, None
    if not reason and isinstance(branch, TeamBranch) and settings['show_branch'] and not settings['show_git_icon']:
        team = branch[:80]
    if settings['show_git_icon']:
        # Herdr inserts dots between text tokens. Keep icon + text in one token
        # for branch names and placeholders, as we already do for space names.
        if reason and mode=='placeholder':
            padding = glyph + ' ' + reason
        elif not reason:
            if branch and settings['show_branch']:
                combined = glyph + ' ' + branch[:78]
            else:
                icon = glyph  # status-only: Herdr uses a space before git_status
    return {TOKEN: padding, ICON_TOKEN: icon, BRANCH_TOKEN: combined, TEAM_TOKEN: team}


class SpaceRows:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def sync(self, settings, team_dirs=None):
        """`team_dirs` maps a space to the working directories of the OpenRig seats in it."""
        team_dirs = team_dirs or {}
        workspaces = rpc(self.endpoint, 'workspace.list')['workspaces']
        failed = False
        lookups = {}  # each team directory is looked up once per sync
        for workspace in workspaces:
            dirs = team_dirs.get(workspace['workspace_id'], ())
            try:
                if settings['empty_git_row']=='hide' and not settings['show_git_icon'] and not dirs:
                    values = dict(EMPTY)
                else:
                    reason, branch = git_details(self.endpoint, workspace, workspaces, settings,
                                                 dirs, lookups)
                    values = row_tokens(reason, settings, branch)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # Uncertain lookups must not leave a stray separator beside real
                # Git details. Other workspaces can still be refreshed.
                values = dict(EMPTY)
                failed = True
            if any(workspace.get('tokens', {}).get(key) != value for key,value in values.items()):
                rpc(self.endpoint, 'workspace.report_metadata', {
                    'workspace_id': workspace['workspace_id'], 'source': SOURCE,
                    'tokens': values})
        if failed:
            raise RuntimeError('Could not check one or more Git rows')
