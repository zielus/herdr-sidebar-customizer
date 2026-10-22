"""Validated sidebar preferences and transactional, narrowly scoped config edits.

The JSON stores preferences only. Herdr still owns key parsing and rendering;
Applying preferences validates Herdr config and records reversible ownership.
"""
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import string
import subprocess
import tempfile
import tomllib
import unicodedata

from agent_info import PLUGIN
from config_edit import assignment, command_chunks, section_span
from filecache import file_stamp
import runtime
from runtime import preferences_path


def bounce(frames):
    return frames + frames[-2:0:-1]


# Stable saved identifiers; durations are milliseconds at 1x.
# Names stay short enough for one line in the settings gallery.
ANIMATIONS = {
    '01': ('Braille', '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏', 100),
    '03': ('Half moon', '◐◓◑◒', 180),
    '04': ('Arc', '◜◝◞◟', 180),
    '19': ('Heartbeat', '·∙●∙', 220),
    '20': ('Rings', bounce('○◎●'), 160),
    '22': ('Starburst', bounce('✶✸✹✺'), 150),
    '23': ('Bar', bounce('▁▂▃▄▅▆▇█'), 90),
}
# Stable IDs from the reviewed Nerd Fonts gallery; labels stay human-readable.
GIT_ICONS = {
    '01': ('GitHub outline', '\uea84'),
    '03': ('GitHub alternate', '\ueb00'),
    '04': ('GitHub filled', '\ueba1'),
    '21': ('Forked repository', '\uea63'),
    '27': ('Repository', '\U000f0ccf'),
    '41': ('Branch (Material)', '\U000f062c'),
    '43': ('Branch (Powerline)', '\ue0a0'),
    '44': ('Git diamond (Devicons)', '\ue702'),
    '45': ('Git wordmark', '\uf1d3'),
    '47': ('Git square', '\uf1d2'),
    '48': ('Git diamond (Material)', '\U000f02a2'),
}
# Display names where title case is wrong, e.g. OpenCode.
AGENT_NAMES = {'opencode': 'OpenCode', 'pi': 'pi', 'omp': 'omp'}
DEFAULTS = {
    'animation': '01', 'speed': 1.0, 'animate': True, 'state_icons': True,
    'space_names': True, 'space_colours': True, 'name_bold': True,
    'collect_details': False, 'agent_text': True, 'show_task': False, 'show_agent': True, 'show_model': True,
    'show_effort': True, 'show_context': True, 'show_capacity': True,
    'context_mode': 'used', 'claude_label': 'Claude', 'codex_label': 'Codex',
    'opencode_label': 'OpenCode', 'pi_label': 'pi', 'omp_label': 'omp',
    'text_mode': 'automatic', 'text_template': '',
    'agent_colour': 'theme', 'tab_colour': 'theme', 'machine_colour': 'agent',
    'branch_colour': 'theme', 'neutral_colour': 'theme',
    'show_tab': True, 'show_machine': True, 'machine_position': 'name', 'detail_hints': True,
    'agent_row_shows': 'space', 'group_agents': False, 'groups_gap': 1,
    'tab_names': False, 'tab_name_format': '{topic}',
    'show_branch': True, 'show_git': True,
    'empty_git_row': 'blank', 'show_git_icon': False, 'git_icon': '41', 'spaces_gap': 0, 'agents_gap': 0,
    'default_hue': 'random', 'default_shade': 'medium',
    'idle_symbol': '○', 'working_symbol': '●', 'done_symbol': '●',
    'blocked_symbol': '⊘', 'unknown_symbol': '·',
    'shortcut_settings': 'prefix+shift+s',
    'shortcut_colours': 'prefix+shift+c', 'shortcut_spacing': '', 'shortcut_pin_tab': '',
}
SHORTCUTS = {
    'shortcut_settings': ('settings', 'Herdr Sidebar Customizer settings'),
    'shortcut_colours': ('space-color', 'choose space color'),
    'shortcut_spacing': ('toggle-spacing', 'toggle compact sidebar spacing'),
    'shortcut_pin_tab': ('pin-tab', "pin or unpin this tab's name"),
}
# Users could bind pin-tab by hand before it had a setting. Only a binding with
# this plugin's own description is managed, so Apply leaves theirs alone.
DESCRIBED_ONLY = {PLUGIN + '.pin-tab': SHORTCUTS['shortcut_pin_tab'][1]}
FIELDS = {'agent', 'model', 'effort', 'context', 'used', 'remaining', 'capacity',
          'used_pct', 'remaining_pct', 'task'}
DEFAULT_TEMPLATE = '{agent} {model} · {effort} · {context}'
MANAGED_COMMANDS = frozenset(PLUGIN + '.' + action for action, _ in SHORTCUTS.values())


def is_managed(command):
    """Whether a parsed [[keys.command]] table is one this plugin writes and replaces."""
    name = command.get('command')
    if name not in MANAGED_COMMANDS:
        return False
    return name not in DESCRIBED_ONLY or command.get('description') == DESCRIBED_ONLY[name]


CHOICES = {
    'machine_position': ('name', 'details'),
    'agent_row_shows': ('space', 'agent', 'none'),
    'git_icon': GIT_ICONS,
    'empty_git_row': ('hide', 'blank', 'placeholder'),
    'animation': ANIMATIONS,
    'context_mode': ('used', 'remaining'),
    'text_mode': ('automatic', 'custom'),
    'default_shade': ('dark', 'medium', 'light'),
}
HEX_COLOR = re.compile(r'#[0-9a-fA-F]{6}')
# Written beside the managed binding by pre-release builds; removed with it.
OLD_SHORTCUT_COMMENT = '# Uppercase C preserves the built-in prefix+c new-tab shortcut.\n'
PREFIX_OWNER = 'Herdr prefix'


def migrate_empty_git_row(values):
    """Map the old keep_empty_git_row boolean onto the three-way control."""
    if not isinstance(values, dict) or 'keep_empty_git_row' not in values:
        return values
    values = dict(values)
    previous = values.pop('keep_empty_git_row')
    if not isinstance(previous, bool):
        raise ValueError('keep_empty_git_row must be on or off')
    values.setdefault('empty_git_row', 'blank' if previous else 'hide')
    return values


def check_type(key, value, default):
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(key + ' must be on or off')
    elif key == 'speed':
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not .25 <= value <= 4:
            raise ValueError('Speed must be between 0.25x and 4x')
    elif key.endswith('_gap'):
        if type(value) is not int or value not in (0, 1):
            raise ValueError('Spacing must be 0 or 1')
    elif not isinstance(value, str) or len(value) > 160 or any(not c.isprintable() for c in value):
        raise ValueError(key + ' must be short, printable text')


def is_single_width_symbol(value):
    return (len(value) == 1
            and unicodedata.east_asian_width(value) not in ('W', 'F')
            and not unicodedata.category(value).startswith(('M', 'C')))


def check_value(key, value, default):
    check_type(key, value, default)
    # Machine labels can follow the agent details color, their default.
    if key == 'machine_colour' and value == 'agent':
        return
    if key.endswith('_colour') and value != 'theme' and not HEX_COLOR.fullmatch(value):
        raise ValueError(key + ' must be theme or a six-digit hex color, e.g. #9399b2')
    if key.endswith('_symbol') and not is_single_width_symbol(value):
        raise ValueError(key + ' must be one single-width printable symbol')
    if key.endswith('_label') and len(value) > 32:
        raise ValueError('Agent labels must be 32 characters or fewer')


def check_choices(settings):
    from space_colors import HUES
    choices = CHOICES | {'default_hue': ('random', *(hue for hue, *_ in HUES))}
    for key, allowed in choices.items():
        if settings[key] not in allowed:
            raise ValueError('Invalid ' + key)


def check_template(template):
    for _, field, spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in FIELDS or spec or conversion):
            raise ValueError('Template fields: ' + ', '.join(sorted(FIELDS)))


def check_shortcut_names(settings):
    for key in SHORTCUTS:
        chord = settings[key]
        if chord and not re.fullmatch(r'[a-z0-9+_-]+', chord):
            raise ValueError('Use lowercase shortcut names, e.g. prefix+shift+s; empty disables')


def validate(values):
    values = migrate_empty_git_row(values)
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError('Unknown sidebar setting')
    result = DEFAULTS | values
    # Nonempty templates saved before text_mode existed stay custom.
    if 'text_mode' not in values and values.get('text_template'):
        result['text_mode'] = 'custom'
    for key, default in DEFAULTS.items():
        check_value(key, result[key], default)
    check_choices(result)
    check_template(result['text_template'])
    check_shortcut_names(result)
    return result


def read_settings(path=None):
    path = path or preferences_path()
    return validate(json.loads(path.read_text())) if path.exists() else dict(DEFAULTS)


class SettingsReader:
    """Hot reload with the last valid settings retained if a file is malformed."""
    def __init__(self, path=None):
        self.path = path or preferences_path()
        self.stamp, self.value, self.error = None, dict(DEFAULTS), None

    def load(self):
        try:
            stamp = file_stamp(self.path)
            if stamp != self.stamp:
                self.value = read_settings(self.path)
                self.stamp = stamp
            self.error = None
        except FileNotFoundError:
            self.value, self.stamp, self.error = dict(DEFAULTS), None, None
        except (OSError, ValueError, TypeError) as error:
            self.error = str(error)
        return self.value


def frame_interval(settings):
    return ANIMATIONS[settings['animation']][2] / 1000 / settings['speed']


def context_capacity(values):
    return (values.get('context_compact') or '').partition('/')[2]


def context_text(values, remaining, with_capacity):
    pct = values.get('context_left_pct' if remaining else 'context_used_pct')
    if not pct:
        return ''
    capacity = context_capacity(values)
    text = pct + '/' + capacity if with_capacity and capacity else pct
    return text + ' left' if remaining else text


def render_template(template, mapping):
    """Join the template's '·'-separated groups, skipping groups with no values.

    This avoids a dangling label or separator when an agent omits a field.
    """
    filled = {key: value or '' for key, value in mapping.items()}
    groups = []
    for group in template.split('·'):
        fields = [field for _, field, _, _ in string.Formatter().parse(group) if field]
        if fields and not any(mapping[field] for field in fields):
            continue
        rendered = ' '.join(group.format_map(filled).split())
        if rendered:
            groups.append(rendered)
    return ' · '.join(groups)


def custom_label(values, agent, template):
    # Custom templates own the whole label: the automatic-mode visibility
    # controls are kept for switching back but never suppress fields here.
    mapping = {
        'agent': AGENT_NAMES.get(agent, agent.title()),
        'model': values.get('model'),
        'effort': values.get('effort'),
        'context': context_text(values, remaining=False, with_capacity=True),
        'used': values.get('context_used'),
        'remaining': values.get('context_left'),
        'used_pct': values.get('context_used_pct'),
        'remaining_pct': values.get('context_left_pct'),
        'capacity': context_capacity(values),
        'task': values.get('agent_work'),
    }
    return render_template(template, mapping)


def automatic_label(values, agent, settings):
    harness = settings.get(agent + '_label', agent.title()) if settings['show_agent'] else ''
    model = values.get('model') if settings['show_model'] else ''
    effort = values.get('effort') if settings['show_effort'] else ''
    context = ''
    if settings['show_context']:
        context = context_text(values, settings['context_mode'] == 'remaining', settings['show_capacity'])
    first = ' '.join(part for part in (harness, model) if part)
    # The task leads, so a narrow sidebar cuts the details rather than the work.
    task = values.get('agent_work') if settings['show_task'] else ''
    return ' · '.join(part for part in (task, first, effort, context) if part)


def agent_label(values, agent, settings):
    if not settings['agent_text']:
        return None
    if settings['text_mode'] == 'custom':
        label = custom_label(values, agent, settings['text_template'])
    else:
        label = automatic_label(values, agent, settings)
    return label[:80] or None


def replace_section_rows(text, section, rows, gap):
    """Replace only rows/row_gap in an explicit section, preserving other keys."""
    table = 'ui.sidebar.' + section
    # A header on the last line needs its newline before rows can follow it.
    if text and not text.endswith('\n'):
        text += '\n'
    if not section_span(text, table):
        text = text.rstrip() + '\n\n[' + table + ']\n'
    old_rows = assignment(text, table, 'rows')
    if old_rows:
        start, end = old_rows[0], old_rows[1]
    else:
        start = end = section_span(text, table)[1]
    text = text[:start] + rows + '\n' + text[end:]
    _, body_start, body_end = section_span(text, table)
    body, count = re.subn(r'(?m)^([ \t]*row_gap[ \t]*=[ \t]*)\d+', lambda m: m[1] + str(gap),
                          text[body_start:body_end])
    if count == 0:
        body = f'row_gap = {gap}\n' + body
    return text[:body_start] + body + text[body_end:]


def inline_table(*pairs):
    """Format (key, raw TOML value) pairs as a one-line TOML inline table."""
    return '{ ' + ', '.join(key + ' = ' + value for key, value in pairs) + ' }'


def styled_token(token, color, bold=None, rules=None):
    pairs = [('token', json.dumps(token)), ('dim', 'false')]
    if bold is not None:
        pairs.append(('bold', json.dumps(bold)))
    if color != 'theme':
        pairs.append(('fg', json.dumps(color)))
    if rules is not None:
        pairs.append(('rules', '[' + ', '.join(rules) + ']'))
    return inline_table(*pairs)


def hidden_when(token, color, condition, bold=None):
    """A styled native token that hides itself when `condition` matches its value.

    `condition` is a (match kind, raw TOML value) pair, e.g. ('equals', '"Local"').
    """
    rule = inline_table(condition, ('hide', 'true'))
    return styled_token(token, color, bold, rules=[rule])


def machine_colour(settings):
    value = settings['machine_colour']
    return settings['agent_colour'] if value == 'agent' else value


def native_agent_tokens(settings):
    """Bold machine label for other machines, and fallback text for agents no worker marks."""
    from fallback import MARKER
    marked = ('contains', json.dumps(MARKER, ensure_ascii=True))
    machine = [hidden_when('machine', machine_colour(settings), ('equals', '"Local"'), bold=True)]
    fallback_text = [hidden_when('agent', settings['agent_colour'], marked),
                     hidden_when('state_text', settings['agent_colour'], marked)]
    return (machine if settings['show_machine'] else []), fallback_text


def first_row_extras(section, settings, machine):
    """Tokens after the space name in the first row."""
    if section != 'agents':
        return []
    tokens = list(machine) if settings['machine_position'] == 'name' else []
    if settings['show_tab']:
        tokens.append(styled_token('tab', settings['tab_colour']))
    return tokens


def git_row(settings):
    tokens = []
    if settings['show_git_icon']:
        if settings['show_branch']:
            tokens.append(styled_token('$space_git_branch', settings['branch_colour']))
        tokens.append(styled_token('$space_git_icon', settings['branch_colour']))
    if settings['show_git']:
        tokens.append('"git_status"')
    if settings['show_branch'] and not settings['show_git_icon']:
        tokens.append(styled_token('branch', settings['branch_colour']))
    if settings['empty_git_row'] != 'hide':
        from space_rows import PLACEHOLDER_COLOUR
        tokens.append(styled_token('$space_git_padding', PLACEHOLDER_COLOUR))
    return tokens


def details_row(settings, machine, fallback_text):
    tokens = list(machine) if settings['machine_position'] == 'details' else []
    if settings['agent_text']:
        # Grouped details carry their own indent under the agent's name.
        info = '$group_info' if settings['group_agents'] else '$agent_info'
        tokens.append(styled_token(info, settings['agent_colour']))
    return tokens + fallback_text


def render_rows(section, settings, block, native_tokens):
    """The TOML `rows` assignment for one sidebar section."""
    from space_colors import style_block
    machine, fallback_text = native_tokens
    grouped = section == 'agents' and settings['group_agents']
    if section == 'spaces':
        second = git_row(settings)
    else:
        second = details_row(settings, machine, fallback_text)
    lines = ['rows = [']
    if grouped:
        # Only each space's first agent fills the heading; Herdr hides empty rows.
        lines += ['  [', '    ' + style_block(settings, 'group'), '  ],']
    lines += ['  [', '    ' + block]
    lines += ['    ' + token + ',' for token in first_row_extras(section, settings, machine)]
    lines.append('  ],')
    if second:
        lines.append('  [' + ', '.join(second) + '],')
    if grouped and settings['groups_gap']:
        lines.append('  [' + styled_token('$group_gap', 'theme') + '],')
    lines.append(']')
    return '\n'.join(lines)


def render_shortcuts(text, settings):
    """Replace only this plugin's shortcut tables; other command tables stay intact."""
    text = text.replace(OLD_SHORTCUT_COMMENT, '')
    for start, end, raw in reversed(list(command_chunks(text))):
        if is_managed(tomllib.loads(raw)['keys']['command'][0]):
            text = text[:start] + text[end:]
    text = text.rstrip() + '\n'
    for key, (action, description) in SHORTCUTS.items():
        if settings[key]:
            text += ('\n[[keys.command]]\n'
                     'key = ' + json.dumps(settings[key]) + '\n'
                     'type = "plugin_action"\n'
                     'command = ' + json.dumps(PLUGIN + '.' + action) + '\n'
                     'description = ' + json.dumps(description) + '\n')
    return text


def compact_empty(value):
    """Drop empty tables and arrays, so an absent and an empty table compare equal."""
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        child = compact_empty(child)
        if child not in ({}, []):
            result[key] = child
    return result


def unowned_settings(data):
    """Parsed config without the rows, gaps and shortcuts this plugin manages."""
    sidebar = data.setdefault('ui', {}).setdefault('sidebar', {})
    for section in ('spaces', 'agents'):
        table = sidebar.setdefault(section, {})
        table.pop('rows', None)
        table.pop('row_gap', None)
    keymap = data.setdefault('keys', {})
    keymap['command'] = [c for c in keymap.get('command', []) if not is_managed(c)]
    return compact_empty(data)


def assert_only_owned_changes(before, after):
    if unowned_settings(tomllib.loads(before)) != unowned_settings(tomllib.loads(after)):
        raise ValueError('Settings edit would change unrelated Herdr configuration')


def render_config(text, settings):
    from space_colors import style_block
    block = style_block(settings)
    native_tokens = native_agent_tokens(settings)
    result = text
    for section in ('spaces', 'agents'):
        rows = render_rows(section, settings, block, native_tokens)
        result = replace_section_rows(result, section, rows, settings[section + '_gap'])
    result = render_shortcuts(result, settings)
    assert_only_owned_changes(text, result)
    return result


def check_config(text):
    # Ask the installed Herdr to validate syntax, unknown keys and bindings.
    with tempfile.TemporaryDirectory(prefix='sidebar-settings-') as root:
        path = Path(root) / 'config.toml'
        path.write_text(text)
        env = dict(os.environ, HERDR_CONFIG_PATH=str(path))
        result = subprocess.run([runtime.herdr_binary(), 'config', 'check'],
                                env=env, capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip()[:400])


@lru_cache(maxsize=1)
def default_keys():
    """Herdr's default key bindings, read from the commented --default-config output."""
    output = subprocess.check_output([runtime.herdr_binary(), '--default-config'], text=True, timeout=10)
    _, found, section = output.partition('[keys]\n')
    if not found:
        raise ValueError('Unexpected herdr --default-config output')
    section = section.split('\n[', 1)[0]
    section = section.split('\n# Custom commands', 1)[0]
    return {name: json.loads(value) for name, value in
            re.findall(r'(?m)^# ([a-z_]+) = ("[^"\n]*")', section)}


def chords(value):
    """Normalize one binding or a list of them into a set of comparable chords."""
    values = value if isinstance(value, list) else [value]
    result = set()
    for value in values:
        if not value:
            continue
        if '1..9' in value:
            expanded = [value.replace('1..9', str(n)) for n in range(1, 10)]
        else:
            expanded = [value]
        for item in expanded:
            parts = item.lower().replace('cmd+', 'super+').split('+')
            result.add('+'.join(sorted(parts[:-1]) + parts[-1:]))
    return result


def occupied_chords(keys):
    """Map each chord bound by Herdr or another command to a description of its owner."""
    native = default_keys() | {name: value for name, value in keys.items()
                               if isinstance(value, (str, list)) and name != 'command'}
    occupied = {}
    for name, value in native.items():
        if name.startswith('navigate_') or name == 'prefix':
            continue
        for chord in chords(value):
            occupied[chord] = 'Herdr ' + name
    # The prefix key itself must remain available even when a plugin chord is direct.
    for chord in chords(keys.get('prefix', native.get('prefix', 'ctrl+b'))):
        occupied[chord] = PREFIX_OWNER
    for command in keys.get('command', []):
        if not is_managed(command):
            for chord in chords(command['key']):
                occupied[chord] = command.get('description', 'another command')
    return occupied


def may_keep_shadowing(owner):
    """A saved plugin binding may keep overriding a native default, but never the prefix."""
    return owner.startswith('Herdr ') and owner != PREFIX_OWNER


def check_shortcuts(text, settings):
    keys = tomllib.loads(text).get('keys', {})
    saved = {command['command']: chords(command['key']) for command in keys.get('command', [])
             if is_managed(command)}
    occupied = occupied_chords(keys)
    for key, (action, description) in SHORTCUTS.items():
        kept = saved.get(PLUGIN + '.' + action, set())
        for chord in chords(settings[key]):
            if chord in occupied and not (chord in kept and may_keep_shadowing(occupied[chord])):
                raise ValueError(settings[key] + ' conflicts with ' + occupied[chord])
            occupied[chord] = description


def apply_settings(endpoint, directory, settings, expected, colour_changes=None,
                   expected_colours=None):
    """Save through the shared, reversible configuration transaction."""
    from installation import save
    return save(endpoint, directory, settings, expected, colour_changes, expected_colours)
