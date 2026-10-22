"""Set up and restore the plugin without owning unrelated user configuration."""
import json
import os
from pathlib import Path
import tempfile
import time

import fallback
import portable
import runtime
import setup_claude
import sidebar_settings as prefs
from agent_info import CAPTURE_NAME, SOURCE as AGENT_SOURCE, TOKEN_NAMES, rpc
from config_edit import owned_values, restore_owned
from sidebar_state import GROUP_SOURCE, GROUP_TOKENS, SOURCE as ANIMATION_SOURCE
from space_colors import HUES, KEYS as COLOR_KEYS
from space_rows import (
    BRANCH_TOKEN as ROWS_BRANCH_TOKEN,
    ICON_TOKEN as ROWS_ICON_TOKEN,
    SOURCE as ROWS_SOURCE,
    TOKEN as ROWS_TOKEN,
)

CLAUDE_CONFLICT = 'Claude settings.json (remove the status-line hook by hand)'


def read_text(path):
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def record():
    raw = read_text(runtime.installation_path())
    if raw is None:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('config_path') != str(runtime.config_path()):
        raise ValueError('Invalid setup record. Run the restore action before configuring again.')
    return value


def active():
    return record().get('enabled', False)


def replace(path, before, after):
    """Atomic replacement with optimistic concurrency checks and private new files."""
    # Write through symlinks, so a settings file kept in a dotfiles repo stays linked.
    path = Path(os.path.realpath(path))
    if read_text(path) != before:
        raise RuntimeError('A file changed during saving. Try again.')
    if after is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.sidebar-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        if before is not None:
            os.chmod(temporary, path.stat().st_mode & 0o777)
        if read_text(path) != before:
            raise RuntimeError('A file changed during saving. Try again.')
        portable.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reload_applied(endpoint):
    response = rpc(endpoint, 'server.reload_config')
    return response.get('status') == 'applied' and not response.get('diagnostics')


def transaction(changes, endpoint, reload):
    """Write every change, or undo the ones already written.

    Callers list the installation record first, so it is written first and
    undone last.
    """
    written = []
    try:
        for path, before, after in changes:
            if before != after:
                replace(path, before, after)
                written.append((path, before, after))
        if reload and not reload_applied(endpoint):
            raise RuntimeError('Herdr rejected the config; changes were rolled back.')
    except BaseException as error:
        stranded = roll_back(written)
        if reload and written:
            try:
                rpc(endpoint, 'server.reload_config')
            except (OSError, ValueError, RuntimeError):
                pass
        if stranded:
            names = ', '.join(str(path) for path in stranded)
            raise RuntimeError('Could not undo changes to ' + names +
                               '. Repair them by hand, then run Restore.') from error
        raise


def roll_back(written):
    """Undo writes newest first and return the files left changed.

    Rollback stops at the first file it cannot undo, such as one edited
    meanwhile. The earlier writes, including the record of the original
    config, then stay in place so Restore can still recover it.
    """
    for index in range(len(written) - 1, -1, -1):
        path, before, after = written[index]
        try:
            replace(path, after, before)
        except (OSError, RuntimeError):
            return [changed for changed, _, _ in written[:index + 1]]
    return []


def encoded(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def check_no_other_installation():
    for other in (runtime.state_root() / 'installations').glob('*.json'):
        if other == runtime.installation_path():
            continue
        if json.loads(other.read_text()).get('enabled'):
            raise RuntimeError('Another config already uses these preferences. '
                               'Restore it first or use separate plugin directories.')


def layout_mode(old, setup_mode):
    if not setup_mode and not old.get('enabled'):
        raise RuntimeError('Run the Sidebar Customizer setup action first.')
    mode = setup_mode or old['mode']
    if mode not in ('managed', 'manual'):
        raise ValueError('Choose managed or manual layout.')
    if old.get('enabled') and mode != old['mode']:
        raise RuntimeError('Restore the existing setup before changing layout mode.')
    return mode


def managed_config(before, settings, old):
    if old.get('enabled'):
        if owned_values(before or '', runtime.PLUGIN) != owned_values(old['applied'] or '', runtime.PLUGIN):
            raise RuntimeError('Sidebar rows or shortcuts changed outside the plugin. '
                               'Restore or resolve those edits first.')
    prefs.check_shortcuts(before or '', settings)
    after = prefs.render_config(before or '', settings)
    prefs.check_config(after)
    return after


def restoration_text(old, before, mode):
    """The config text Restore will write back.

    On a later Apply the stored original is rebased onto the current file, so
    edits the user made since the last Apply survive Restore.
    """
    if not old.get('enabled'):
        return before
    original = old.get('original')
    if mode == 'managed' and before is not None and before != old['applied']:
        original, _ = restore_owned(before, original, old['applied'] or '', runtime.PLUGIN)
    return original


def palette_change(directory, endpoint, colour_changes, expected_colours):
    path = directory / 'space-colors.json'
    raw = read_text(path)
    palette = json.loads(raw) if raw else {}
    live = {space['workspace_id'] for space in rpc(endpoint, 'workspace.list')['workspaces']}
    for workspace_id, colour in colour_changes.items():
        if workspace_id not in live or colour not in COLOR_KEYS:
            raise ValueError('Space or color no longer exists.')
        if palette.get(workspace_id) != (expected_colours or {}).get(workspace_id):
            raise RuntimeError('Space color changed elsewhere. Undo all (U) to load it, then apply again.')
        palette[workspace_id] = colour
    return path, raw, encoded(palette)


def save(endpoint, directory, settings, expected, colour_changes=None,
         expected_colours=None, setup_mode=None, claude_hook=False):
    settings = prefs.validate(settings)
    with runtime.config_lock(), runtime.palette_lock(directory):
        old = record()
        check_no_other_installation()
        mode = layout_mode(old, setup_mode)
        if prefs.read_settings() != expected:
            raise RuntimeError('Settings changed elsewhere. Undo all (U) to load them, then apply again.')
        target = runtime.config_path()
        before = read_text(target)
        after = managed_config(before, settings, old) if mode == 'managed' else before
        current_record = {
            'config_path': str(target), 'enabled': True, 'mode': mode,
            'original': restoration_text(old, before, mode),
            'applied': after, 'updated_at': int(time.time()),
        }
        record_path = runtime.installation_path()
        changes = [(record_path, read_text(record_path), encoded(current_record)),
                   (target, before, after)]
        if colour_changes:
            changes.append(palette_change(directory, endpoint, colour_changes, expected_colours))
        preferences = prefs.preferences_path()
        changes.append((preferences, read_text(preferences), encoded(settings)))
        if claude_hook:
            changes.extend(setup_claude.prepare(setup_claude.settings_path(), runtime.state_root()))
        transaction(changes, endpoint, reload=mode == 'managed' and after != before)
    return settings


def configure(endpoint, settings, mode='managed', claude_hook=False):
    from agent_info import endpoint_state, start
    result = save(endpoint, endpoint_state(runtime.state_root(), endpoint), settings,
                  prefs.read_settings(), setup_mode=mode, claude_hook=claude_hook)
    start(endpoint, runtime.state_root())
    return result


def restored_config(old, before):
    """Return the config text to write back and the later edits kept instead."""
    if not old.get('enabled'):
        # Already restored. CLAUDE_CONFLICT is checked again on this run.
        previous = old.get('restore_conflicts', [])
        return before, [conflict for conflict in previous if conflict != CLAUDE_CONFLICT]
    if old['mode'] != 'managed':
        return before, []
    if before is None:
        return None, ['config deleted after setup']
    after, conflicts = restore_owned(before, old['original'], old['applied'] or '', runtime.PLUGIN)
    if after is not None:
        prefs.check_config(after)
    return after, conflicts


def restore(endpoint):
    with runtime.config_lock():
        old = record()
        if not old:
            return []
        target = runtime.config_path()
        before = read_text(target)
        after, conflicts = restored_config(old, before)
        try:
            hook_changes = setup_claude.prepare(setup_claude.settings_path(), runtime.state_root(), remove=True)
        except (ValueError, RuntimeError):
            # An unreadable Claude settings file must not block restoring the sidebar.
            hook_changes = []
            conflicts.append(CLAUDE_CONFLICT)
        # Disable the record first so workers stop before the layout goes away.
        # Their current reads are metadata-only and cannot rewrite the config.
        disabled = old | {'enabled': False, 'restore_conflicts': conflicts}
        record_path = runtime.installation_path()
        changes = [(record_path, read_text(record_path), encoded(disabled)),
                   (target, before, after), *hook_changes]
        transaction(changes, endpoint, reload=before != after)
    wait_for_workers()
    clear_metadata(endpoint)
    remove_captures()
    return conflicts + refresh_other_sessions(endpoint, old['mode'])


def refresh_other_sessions(endpoint, mode):
    """Clear metadata in other Herdr sessions that use this config, and reload them."""
    conflicts = []
    for directory in config_state_dirs():
        other = read_text(directory / 'endpoint')
        if not other or other == endpoint:
            continue
        try:
            clear_metadata(other)
            if mode == 'managed' and not reload_applied(other):
                conflicts.append('reload another Herdr session manually')
        except (OSError, ValueError, RuntimeError):
            # Dead sessions have no live metadata. Live ones need a reload.
            if Path(other).exists():
                conflicts.append('refresh another Herdr session manually')
    return conflicts


def config_state_dirs():
    """State directories of every Herdr session that uses this config."""
    config = str(runtime.config_path())
    for directory in runtime.state_root().iterdir():
        if directory.is_dir() and read_text(directory / 'config-path') == config:
            yield directory


def remove_captures():
    """Delete the status-line captures the worker kept for Claude sessions."""
    for directory in config_state_dirs():
        for path in directory.glob('*.json'):
            if not CAPTURE_NAME.fullmatch(path.name):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A leftover capture is harmless; it must not abort the restore.
                pass


def clear_metadata(endpoint):
    """Clear each source's keys. Never report an agent lifecycle transition."""
    animation = dict.fromkeys('space_' + hue for hue, *_ in HUES)
    grouping = dict.fromkeys(['group_' + hue for hue, *_ in HUES] + list(GROUP_TOKENS))
    for agent in rpc(endpoint, 'agent.list')['agents']:
        pane_id = agent['pane_id']
        rpc(endpoint, 'pane.report_metadata',
            {'pane_id': pane_id, 'source': ANIMATION_SOURCE, 'tokens': animation})
        rpc(endpoint, 'pane.report_metadata',
            {'pane_id': pane_id, 'source': GROUP_SOURCE, 'tokens': grouping})
        rpc(endpoint, 'pane.report_metadata',
            {'pane_id': pane_id, 'source': AGENT_SOURCE, 'tokens': dict.fromkeys(TOKEN_NAMES)})
        fallback.clear(endpoint, pane_id)
    rows = dict.fromkeys([ROWS_TOKEN, ROWS_ICON_TOKEN, ROWS_BRANCH_TOKEN])
    for space in rpc(endpoint, 'workspace.list')['workspaces']:
        workspace_id = space['workspace_id']
        rpc(endpoint, 'workspace.report_metadata',
            {'workspace_id': workspace_id, 'source': ANIMATION_SOURCE, 'tokens': animation})
        rpc(endpoint, 'workspace.report_metadata',
            {'workspace_id': workspace_id, 'source': ROWS_SOURCE, 'tokens': rows})


def wait_for_workers(timeout=10):
    deadline = time.monotonic() + timeout
    for directory in config_state_dirs():
        with (directory / 'watch.lock').open('a') as handle:
            while not portable.try_lock(handle):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Worker is still stopping. Wait, then run Restore again before uninstalling.')
                time.sleep(0.1)
