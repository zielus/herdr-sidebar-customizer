#!/usr/bin/env python3
"""Interactive Herdr Sidebar Customizer settings, with draft previews and explicit Apply."""
import argparse
from collections import namedtuple
import curses
from dataclasses import dataclass, field as dataclass_field
import json
import math
import os
import subprocess
import textwrap
import time

from agent_info import PLUGIN, endpoint_state, read_json, rpc, state_root, tokens
import sidebar_settings as settings
import space_colors
from sidebar_state import symbol
from space_rows import PLACEHOLDER_COLOUR


# A field's options are None for free text, a tuple for a cyclic choice
# (booleans included), or an empty tuple for a picker such as a swatch grid.
# Keys starting with '@' are UI-only: they read and write saved settings
# but are never saved themselves.
Field = namedtuple('Field', 'key label options help')
Heading = namedtuple('Heading', 'title')
Section = namedtuple('Section', 'name items')

ON_OFF = (False, True)
GAPS = (0, 1)
SPEEDS = (.25, .5, .75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
STATES = ('idle', 'working', 'done', 'blocked', 'unknown')
AGENT_LABELS = (('claude_label', 'Claude'), ('codex_label', 'Codex'),
                ('opencode_label', 'OpenCode'), ('pi_label', 'pi'), ('omp_label', 'omp'))
# Single-width presets in STATES order; Circles matches the defaults.
SYMBOL_SETS = {
    'circles': ('○', '●', '●', '⊘', '·'),
    'dots': ('◦', '•', '•', '×', '·'),
    'ascii': ('-', '*', '+', '!', '?'),
}
STATE_SYMBOL_HELP = 'One single-width symbol; it overrides the symbol set.'
SHORTCUT_HELP = 'Type a binding such as prefix+shift+g; clear it to remove.'
CHOICE_LABELS = {
    '@row_shows': {'both': 'Symbol and name', 'name': 'Name only',
                   'symbol': 'Symbol only', 'neither': 'Nothing'},
    '@context': {'used': 'Used %', 'remaining': 'Remaining %', 'off': 'Off'},
    '@symbol_set': {'circles': 'Circles', 'dots': 'Dots', 'ascii': 'Plain ASCII',
                    'custom': 'Custom'},
    'empty_git_row': {'hide': 'Shorter row', 'blank': 'Blank line', 'placeholder': 'Explain why'},
    'machine_position': {'name': 'After space name', 'details': 'In details row'},
    'agent_row_shows': {'space': 'Space name', 'agent': 'Agent name', 'none': 'Nothing'},
    'text_mode': {'automatic': 'Pick fields', 'custom': 'Write a template'},
    'default_shade': {'dark': 'Dark', 'medium': 'Medium', 'light': 'Light'},
}


# Merged controls. Each reads a value from the draft and writes it back
# through the saved keys it stands for.
ROW_SHOWS = {'both': (True, True), 'name': (False, True), 'symbol': (True, False)}


def row_shows(draft):
    shown = (draft['state_icons'], draft['space_names'])
    return next((choice for choice, pair in ROW_SHOWS.items() if pair == shown), 'neither')


def set_row_shows(draft, choice):
    draft['state_icons'], draft['space_names'] = ROW_SHOWS[choice]


def git_icon_choice(draft):
    return draft['git_icon'] if draft['show_git_icon'] else 'none'


def set_git_icon_choice(draft, choice):
    draft['show_git_icon'] = choice != 'none'
    # The last icon stays saved while hidden, ready for next time.
    if choice != 'none':
        draft['git_icon'] = choice


def context_choice(draft):
    return draft['context_mode'] if draft['show_context'] else 'off'


def set_context_choice(draft, choice):
    draft['show_context'] = choice != 'off'
    if choice != 'off':
        draft['context_mode'] = choice


def symbol_set(draft):
    current = tuple(draft[state + '_symbol'] for state in STATES)
    return next((name for name, symbols in SYMBOL_SETS.items() if symbols == current), 'custom')


def set_symbol_set(draft, name):
    for state, glyph in zip(STATES, SYMBOL_SETS[name]):
        draft[state + '_symbol'] = glyph


MERGED = {
    '@row_shows': (row_shows, set_row_shows),
    '@git_icon': (git_icon_choice, set_git_icon_choice),
    '@context': (context_choice, set_context_choice),
    '@symbol_set': (symbol_set, set_symbol_set),
}

SECTIONS = [
    Section('Spaces', [
        Field('@row_shows', 'Space row shows', tuple(ROW_SHOWS),
              'The state symbol shows what the space\'s agents are doing.'),
        Field('name_bold', 'Bold space names', ON_OFF, 'Applies to the symbol and name together.'),
        Field('spaces_gap', 'Row spacing', GAPS,
              'Spaced adds a blank row between spaces. Agents has its own setting.'),
        Field('show_branch', 'Show branch', ON_OFF, 'Show the current Git branch under the space.'),
        Field('show_git', 'Show ahead/behind', ON_OFF,
              "Show Herdr's Git status, such as ↑1 ↓2 ahead and behind."),
        Field('empty_git_row', 'Spaces without Git', tuple(CHOICE_LABELS['empty_git_row']),
              'Shorter row drops the Git line. Blank line keeps every space the same height. '
              'Explain why says why no Git details show.'),
        Field('@git_icon', 'Git icon', ('none', *settings.GIT_ICONS),
              'Shown on Git rows. Needs a Nerd Font in your terminal; the preview shows your choice.'),
    ]),
    Section('Agents', [
        Field('collect_details', 'Collect agent details', ON_OFF,
              'Read model, effort and context from local Claude, Codex, OpenCode, pi and omp '
              'session data. Nothing is sent anywhere.'),
        Heading('DETAILS ROW'),
        Field('agent_text', 'Details row', ON_OFF, 'Show a row of agent details under each agent.'),
        Field('detail_hints', 'Missing-detail hints', ON_OFF,
              'Say in the details row why model or context is missing, e.g. needs herdr integration.'),
        Field('text_mode', 'Details text', tuple(CHOICE_LABELS['text_mode']),
              'Pick fields uses the switches below; Write a template gives full control. '
              'Each keeps its own settings.'),
        Field('show_agent', 'Agent name', ON_OFF, 'Show the agent name, as set in Agent names.'),
        Field('show_model', 'Model', ON_OFF, 'Show the model reported by the session.'),
        Field('show_effort', 'Reasoning effort', ON_OFF, 'Unknown effort is left out.'),
        Field('@context', 'Context', tuple(CHOICE_LABELS['@context']),
              'Context used or left, from the session itself; never an assumed size.'),
        Field('show_capacity', 'Show window size (/1M)', ON_OFF,
              'Add the context window, for example 25%/1M.'),
        Field('@agent_names', 'Agent names', None,
              'Enter edits the name shown for each agent; this does not change agent identity.'),
        Field('text_template', 'Template', None,
              'Enter edits the template. The fields you can use are listed below it.'),
        Heading('LABELS AND SPACING'),
        Field('show_machine', 'Machine labels', ON_OFF,
              'Show the saved SSH machine name on agents from other machines. '
              'Local agents are never labeled.'),
        Field('machine_position', 'Machine label position', tuple(CHOICE_LABELS['machine_position']),
              'After the space name on the first row, or leading the details row.'),
        Field('agents_gap', 'Row spacing', GAPS,
              'Spaced adds a blank row between agents. Spaces has its own setting.'),
        Heading('GROUPING'),
        Field('agent_row_shows', 'Agent row shows', tuple(CHOICE_LABELS['agent_row_shows']),
              "What follows the symbol on each agent row. Agent name is the pane's name "
              '(OpenRig names seats this way), else the herdr agent name, else the agent type.'),
        Field('group_agents', 'Group by space', ON_OFF,
              "Show each space's name once, above its agents. Needs Herdr's grouped order."),
        Field('groups_gap', 'Group spacing', GAPS, 'Spaced adds a blank row after each group.'),
    ]),
    Section('Tabs', [
        Field('show_tab', 'Show tab name', ON_OFF, 'Show the tab name after the space in the Agents list.'),
        Field('tab_names', 'Rename tabs from agent topic', ON_OFF,
              "Rename each agent's tab from its title, e.g. after /rename. A name you type stays "
              'until the title changes; pin a tab to keep it.'),
        Field('tab_name_format', 'Tab name format', None,
              'Use {topic} for the agent\'s topic and {n} for the tab number, e.g. "{n}· {topic}".'),
    ]),
    Section('Colors', [
        Heading('SPACES'),
        Field('space_colours', 'Use space colors', ON_OFF,
              'Off uses the color below for every name; saved space colors are kept.'),
        Field('@space', 'Space', (), 'Choose the space whose color to edit.'),
        Field('@colour', 'Color', (),
              'Choose from the swatches below. Enter focuses the palette; Escape returns to fields.'),
        Field('default_hue', 'New-space hue', ('random', *(hue for hue, *_ in space_colors.HUES)),
              'Applies only to new spaces; existing colors stay as they are.'),
        Field('default_shade', 'New-space shade', tuple(CHOICE_LABELS['default_shade']),
              'Applies only to new spaces.'),
        Heading('TEXT'),
        Field('neutral_colour', 'Names when colors are off', (),
              'Used for space names while Use space colors is off.'),
        Field('agent_colour', 'Agent details', (), 'Neutrals are included for text.'),
        Field('tab_colour', 'Tab name', (), 'Choose a swatch below.'),
        Field('machine_colour', 'Machine label', (),
              'By default labels match the agent details color, in bold.'),
        Field('branch_colour', 'Branch', (), 'Git status keeps its own colors.'),
    ]),
    Section('Motion & symbols', [
        Heading('MOTION'),
        Field('animate', 'Animate working agents', ON_OFF,
              'Off shows the static Working symbol below.'),
        Field('animation', 'Animation', tuple(settings.ANIMATIONS),
              'Left/Right selects an animation; click any preview to choose it.'),
        Field('speed', 'Animation speed', SPEEDS,
              "A multiplier of each animation's natural speed; 1x matches the preview."),
        Heading('SYMBOLS'),
        Field('@symbol_set', 'Symbol set', tuple(SYMBOL_SETS),
              'Sets all five symbols at once. Edit one below to make your own set.'),
        Field('idle_symbol', 'Idle', None, STATE_SYMBOL_HELP),
        Field('working_symbol', 'Working', None, STATE_SYMBOL_HELP),
        Field('done_symbol', 'Done', None, STATE_SYMBOL_HELP),
        Field('blocked_symbol', 'Blocked', None, STATE_SYMBOL_HELP),
        Field('unknown_symbol', 'Unknown', None, STATE_SYMBOL_HELP),
    ]),
    Section('Shortcuts', [
        Field('shortcut_settings', 'Open settings', None, SHORTCUT_HELP),
        Field('shortcut_colours', 'Space color picker', None, SHORTCUT_HELP),
        Field('shortcut_spacing', 'Toggle spacing', None, SHORTCUT_HELP),
        Field('shortcut_pin_tab', 'Pin tab name', None, SHORTCUT_HELP),
    ]),
]
# Pages whose settings change the sample rows; the others hide the previews.
PREVIEW_PAGES = ('Spaces', 'Agents', 'Tabs', 'Colors', 'Motion & symbols')
AUTOMATIC_ONLY = {'show_agent', 'show_model', 'show_effort', '@context', 'show_capacity',
                  '@agent_names'}
# These only shape the details row, so they mean nothing while it is hidden.
DETAILS_ROW_DEPENDENTS = AUTOMATIC_ONLY | {'detail_hints', 'text_mode', 'text_template'}
SYMBOL_DEPENDENTS = {'animate', 'animation', 'speed', '@symbol_set',
                     *(state + '_symbol' for state in STATES)}


def find_field(key):
    """The field for a key, wherever it appears."""
    return next(item for section in SECTIONS for item in section.items
                if isinstance(item, Field) and item.key == key)


def inactive_reason(session, key):
    """Explain inactive fields while keeping their saved choices intact."""
    draft = session.draft
    if key == 'tab_name_format' and not draft['tab_names']:
        return 'Inactive: turn on Rename tabs from agent topic.'
    if key in SYMBOL_DEPENDENTS and not draft['state_icons']:
        return 'Inactive: choose a symbol in Space row shows on Spaces.'
    if key in ('animation', 'speed') and not draft['animate']:
        return 'Inactive: turn on Animate working agents.'
    if key == 'working_symbol' and draft['animate']:
        return 'Inactive: turn off Animate working agents to use it.'
    if key == 'detail_hints' and not draft['collect_details']:
        return 'Inactive: turn on Collect agent details.'
    if key == 'machine_position' and not draft['show_machine']:
        return 'Inactive: turn on Machine labels.'
    if key == 'groups_gap' and not draft['group_agents']:
        return 'Inactive: turn on Group by space.'
    if key == 'machine_colour' and not draft['show_machine']:
        return 'Inactive: turn on Machine labels on Agents.'
    if key in DETAILS_ROW_DEPENDENTS and not draft['agent_text']:
        return 'Inactive: turn on Details row.'
    if key == 'show_capacity' and not draft['show_context']:
        return 'Inactive: Context is Off.'
    if key == '@agent_names' and not draft['show_agent']:
        return 'Inactive: turn on Agent name.'
    if key.startswith('shortcut_') and session.layout_mode == 'manual':
        return 'Inactive: manual layout leaves shortcuts to your own Herdr config.'
    return ''


def field_help(session, field):
    """Static help, plus the worker's latest missing-detail findings for collection."""
    if field.key != 'collect_details' or not session.draft['collect_details']:
        return field.help
    from detail_hints import issues
    found = issues(session.worker_status())
    return ' '.join(found) if found else field.help


def plural(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def collection_status(session):
    """One line on what the worker last found; empty when there is nothing to say."""
    if not session.draft['collect_details']:
        return ''
    if not session.base['collect_details']:
        return 'Collection starts when you apply.'
    status = session.worker_status()
    if 'checked_at' not in status:
        return ''
    from detail_hints import summaries
    unsessioned = sum((status.get('missing_session') or {}).values())
    unreadable = sum((status.get('unreadable_session') or {}).values())
    total = status.get('agents', 0) + unsessioned + unreadable
    if not total:
        return 'No supported agents are running.'
    text = f"Reading details for {status.get('models', 0)} of {plural(total, 'agent')}"
    found = summaries(status)
    return text + (' · ' + found[0] if found else '')


def default_space_color(draft, random_hue):
    """The color a new space would get; random_hue stands in when the hue is random."""
    hue = random_hue if draft['default_hue'] == 'random' else draft['default_hue']
    return f"{hue}_{draft['default_shade']}"


STATUS_REFRESH_SECONDS = 1


class Session:
    def __init__(self, endpoint, directory, layout_mode='managed'):
        self.endpoint = endpoint
        self.directory = directory
        self.layout_mode = layout_mode
        self.message = ''
        self.wid = os.environ.get('HERDR_WORKSPACE_ID')
        self.status = {}
        self.status_read_at = None
        self.reload()

    def reload(self):
        base = settings.read_settings()
        spaces = rpc(self.endpoint, 'workspace.list')['workspaces']
        path = self.directory / 'space-colors.json'
        base_colours = json.loads(path.read_text()) if path.exists() else {}
        valid = isinstance(base_colours, dict) and all(
            value in space_colors.KEYS for value in base_colours.values())
        if not valid:
            raise ValueError('Saved space colors are invalid')
        self.base = base
        self.spaces = spaces
        self.base_colours = base_colours
        self.draft = dict(base)
        if self.wid not in [space['workspace_id'] for space in spaces]:
            self.wid = spaces[0]['workspace_id'] if spaces else None
        self.colours = dict(base_colours)
        self.preview_colour = None

    def worker_status(self):
        """The worker's status.json, reread at most once a second while drawing."""
        now = time.monotonic()
        if self.status_read_at is None or now - self.status_read_at >= STATUS_REFRESH_SECONDS:
            self.status = read_json(self.directory / 'status.json')
            self.status_read_at = now
        return self.status

    @property
    def dirty(self):
        return self.draft != self.base or self.colours != self.base_colours

    @property
    def change_count(self):
        """Saved settings and space colors that differ from the draft."""
        changed = sum(self.draft[key] != self.base.get(key) for key in self.draft)
        recolored = sum(self.base_colours.get(wid) != colour for wid, colour in self.colours.items())
        return changed + recolored

    def value(self, key):
        if key == '@space':
            return self.wid
        if key == '@colour':
            fallback = self.preview_colour or default_space_color(self.draft, 'blue')
            return self.colours.get(self.wid, fallback)
        if key == '@agent_names':
            return ', '.join(self.draft[label] for label, _ in AGENT_LABELS)
        if key in MERGED:
            return MERGED[key][0](self.draft)
        return self.draft[key]

    def set(self, key, value):
        reason = inactive_reason(self, key)
        if reason:
            self.message = reason
            return
        if self.value(key) != value:
            self.message = ''
        if key == '@space':
            self.wid = value
        elif key == '@colour':
            self.set_space_colour(value)
        elif key in MERGED:
            MERGED[key][1](self.draft, value)
        else:
            self.draft[key] = value
            if key == 'text_mode' and value == 'custom' and not self.draft['text_template']:
                self.draft['text_template'] = settings.DEFAULT_TEMPLATE

    def set_space_colour(self, value):
        if self.wid:
            self.colours[self.wid] = value
        else:
            self.preview_colour = value

    def choices(self, field):
        if field.key == '@space':
            return tuple(space['workspace_id'] for space in self.spaces)
        if is_color(field.key):
            return tuple(value for value, _, _ in color_options(self, field.key))
        return field.options or ()

    def cycle(self, field, step):
        options = self.choices(field)
        if not options:
            return
        current = self.value(field.key)
        if current in options:
            index = options.index(current) + step
        else:
            # A value no choice names, such as custom symbols, steps onto the nearest end.
            index = 0 if step > 0 else -1
        self.set(field.key, options[index % len(options)])

    def apply(self):
        changes = {wid: color for wid, color in self.colours.items()
                   if self.base_colours.get(wid) != color}
        settings.apply_settings(self.endpoint, self.directory, self.draft, self.base,
                                changes, self.base_colours)
        self.reload()
        if self.layout_mode == 'manual':
            self.message = 'Saved. Update your rows to match layout changes.'
        else:
            self.message = 'Applied. Sidebar updates within three seconds.'

    def defaults(self):
        self.draft = dict(settings.DEFAULTS)
        self.colours = dict(self.base_colours)
        self.preview_colour = None
        self.message = ('Defaults previewed; Apply to save. Saved space colors kept. '
                        'Restore previous sidebar is a separate plugin action.')


def display_value(session, key):
    value = session.value(key)
    if key == '@space':
        return next((space['label'] for space in session.spaces if space['workspace_id'] == value),
                    'Sample space (preview only)')
    if is_color(key):
        return next(label for choice, label, _ in color_options(session, key) if choice == value)
    if key in CHOICE_LABELS:
        return CHOICE_LABELS[key][value]
    if isinstance(value, bool):
        return 'On' if value else 'Off'
    if key == 'animation':
        return settings.ANIMATIONS[value][0]
    if key == '@git_icon':
        return git_icon_label(value)
    if key == 'default_hue':
        return value.title()
    if key == 'speed':
        return f'{value:g}x'
    if key.endswith('_gap'):
        return 'Spaced' if value else 'Compact'
    if value == '':
        return 'Not set' if key.startswith('shortcut_') else 'Empty'
    return str(value)


def git_icon_label(ident):
    if ident == 'none':
        return 'None'
    label, glyph = settings.GIT_ICONS[ident]
    return glyph + ' ' + label


def put(screen, y, x, text, style=0):
    height, width = screen.getmaxyx()
    if 0 <= y < height and 0 <= x < width - 1:
        try:
            screen.addnstr(y, x, text, max(0, width - x - 1), style)
        except curses.error:
            pass


ESCAPE = '\x1b'
ENTER_KEYS = ('\n', '\r', curses.KEY_ENTER)
CLOSE_KEYS = ('q', 'Q', ESCAPE)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, '\x7f', '\b')
CTRL_A, CTRL_E, CTRL_U = '\x01', '\x05', '\x15'
MAX_TEXT_LENGTH = 160


def apply_edit_key(value, position, key):
    """Return the text and cursor position after one key in the line editor."""
    if key == CTRL_U:
        return '', 0
    if key in BACKSPACE_KEYS and position:
        return value[:position - 1] + value[position:], position - 1
    if key == curses.KEY_DC:
        return value[:position] + value[position + 1:], position
    if key == curses.KEY_LEFT:
        return value, max(0, position - 1)
    if key == curses.KEY_RIGHT:
        return value, min(len(value), position + 1)
    if key in (curses.KEY_HOME, CTRL_A):
        return value, 0
    if key in (curses.KEY_END, CTRL_E):
        return value, len(value)
    if isinstance(key, str) and key.isprintable() and len(value) < MAX_TEXT_LENGTH:
        return value[:position] + key + value[position:], position + 1
    return value, position


def edit_text(screen, title, value):
    """A one-line editor on the bottom rows; return the new text, or None when cancelled."""
    position = len(value)
    screen.timeout(-1)
    curses.curs_set(1)
    try:
        while True:
            height, width = screen.getmaxyx()
            if height < 5 or width < 12:
                return None
            y = height - 4
            for row in range(y, height):
                screen.move(row, 0)
                screen.clrtoeol()
            put(screen, y, 1, 'Edit ' + title, curses.A_BOLD)
            start = max(0, position - max(1, width - 5))
            put(screen, y + 1, 1, value[start:])
            put(screen, y + 2, 1, 'Enter: accept   Esc: cancel   Ctrl+U: clear', curses.A_DIM)
            screen.move(y + 1, min(width - 2, 1 + position - start))
            screen.refresh()
            key = screen.get_wch()
            if key in ENTER_KEYS:
                return value
            if key == ESCAPE:
                return None
            value, position = apply_edit_key(value, position, key)
    finally:
        curses.curs_set(0)
        screen.timeout(60)


def edit_agent_names(screen, session):
    """Edit each agent's visible name in turn; Escape stops at the current one."""
    for number, (key, agent) in enumerate(AGENT_LABELS, 1):
        title = f'name for {agent} ({number} of {len(AGENT_LABELS)})'
        value = edit_text(screen, title, session.value(key))
        if value is None:
            return
        session.set(key, value)


# Text colors use the 36-swatch space palette plus neutrals, and a saved custom
# color stays selectable without a hex editor.
NEUTRALS = tuple((f'#{n:02x}{n:02x}{n:02x}', name) for n, name in
                 [(0, 'Black'), (48, 'Charcoal'), (88, 'Dark gray'), (128, 'Gray'),
                  (168, 'Soft gray'), (208, 'Light gray'), (228, 'Pale gray'), (255, 'White')])
PALETTE_FOREGROUNDS = {key: fg for key, _, fg in space_colors.PALETTE}


def is_color(key):
    # Keys keep their original '_colour' spelling so existing installs still load.
    return key == '@colour' or key.endswith('_colour')


def swatch_name(value):
    """'teal_medium' -> 'Teal · Medium'."""
    hue, shade = value.rsplit('_', 1)
    return hue.title() + ' · ' + shade.title()


def color_options(session, key):
    """Return (value, label, foreground) choices for a color field."""
    palette = [(value, swatch_name(value), fg) for value, _, fg in space_colors.PALETTE]
    if key == '@colour':
        return palette
    options = [('theme', 'Follow theme', 'theme')]
    if key == 'machine_colour':
        options.insert(0, ('agent', 'Same as agent details', session.draft['agent_colour']))
    options += [(fg, label, fg) for _, label, fg in palette]
    options += [(fg, name, fg) for fg, name in NEUTRALS]
    known = {value for value, _, _ in options}
    # The saved color comes before the moving draft so arrow navigation stays stable.
    extras = ((session.base[key], 'Original color'),
              (settings.DEFAULTS[key], 'Default color'),
              (session.value(key), 'Current color'))
    for value, label in extras:
        if value not in known:
            options.append((value, label, value))
            known.add(value)
    return options


def active_items(session, section):
    """The page's headings and fields, without those the current text mode hides."""
    items = SECTIONS[section].items
    if SECTIONS[section].name != 'Agents':
        return items
    template = session.draft['text_mode'] == 'custom'
    hidden = {'text_template'} if not template else AUTOMATIC_ONLY
    return [item for item in items if isinstance(item, Heading) or item.key not in hidden]


def active_fields(session, section):
    return [item for item in active_items(session, section) if isinstance(item, Field)]


def template_help(width):
    paragraphs = [
        'AVAILABLE FIELDS',
        '{agent}         Agent name: Claude / Codex / OpenCode / pi / omp',
        '{model}         Model name',
        '{effort}        Reasoning effort',
        '{context}       Context used with capacity: 25%/1M',
        '{used}          Tokens used: 250k',
        '{remaining}     Tokens remaining: 750k',
        '{capacity}      Context window: 1M',
        '{used_pct}      Percentage used: 25%',
        '{remaining_pct} Percentage remaining: 75%',
        '',
        'Example: {agent} {model} · {effort} · {context}',
        'Remaining: {model} · {remaining_pct} left',
        'Pick-fields switches and agent names do not apply here. Separate optional groups '
        'with ·; a group with no available fields disappears.',
    ]
    lines = []
    for paragraph in paragraphs:
        wrapped = textwrap.wrap(paragraph, width=max(1, width - 4),
                                break_long_words=False, break_on_hyphens=False)
        lines.extend(wrapped or [''])
    return lines


# Two illustrative entries keep every layout control visible in empty sessions.
# The second runs on a fictional saved machine and has a Git branch.
PreviewSample = namedtuple('PreviewSample', 'name color status agent model tab branch remote')
SEPARATOR = ' · '
SAMPLE_MACHINE = 'Build box'
SAMPLE_GIT_STATUS = '↑1 ↓2'
GIT_STATUS_COLOR = '#afff87'


def preview_samples(session):
    first = next((space['label'] for space in session.spaces if space['workspace_id'] == session.wid),
                 'Website')
    return [
        PreviewSample(first, session.value('@colour'), 'working', 'claude', 'Opus 5',
                      'editor', None, False),
        PreviewSample('Sandbox', default_space_color(session.draft, 'teal'), 'done', 'codex',
                      'GPT-6 Astra', 'tests', 'feature/ui', True),
    ]


def append_segment(row, text, fg, bold=False):
    """Add a (text, fg, bold) segment, separated from any earlier one by a middle dot."""
    row.append(((SEPARATOR if row else '') + text, fg, bold))


def preview_rows(session, kind, now):
    """Sample rows for the 'spaces' or 'agents' list, as lists of (text, fg, bold)."""
    draft = session.draft
    sample_rows = space_sample_rows if kind == 'spaces' else agent_sample_rows
    # Each sample is its own space, so grouped samples are separate groups.
    group_gap = draft['groups_gap'] if kind == 'agents' and draft['group_agents'] else 0
    rows = []
    for index, sample in enumerate(preview_samples(session)):
        if index:
            rows.extend([] for _ in range(draft[kind + '_gap'] + group_gap))
        rows.extend(sample_rows(draft, sample, now))
    return rows


def name_color(draft, sample_color):
    return PALETTE_FOREGROUNDS[sample_color] if draft['space_colours'] else draft['neutral_colour']


def name_row(draft, sample, now, show_name=None):
    glyph = symbol(sample.status, now, draft) if draft['state_icons'] else ''
    show_name = draft['space_names'] if show_name is None else show_name
    name = sample.name if show_name else ''
    label = ' '.join(part for part in (glyph, name) if part)
    return [(label, name_color(draft, sample.color), draft['name_bold'])] if label else []


def space_sample_rows(draft, sample, now):
    rows = [name_row(draft, sample, now)]
    git_row = git_preview_row(draft, sample.branch)
    if git_row or draft['empty_git_row'] != 'hide':
        rows.append(git_row)
    return rows


def git_preview_row(draft, branch):
    icon = settings.GIT_ICONS[draft['git_icon']][1]
    show_branch = bool(branch) and draft['show_branch']
    show_git = bool(branch) and draft['show_git']
    row = []
    if draft['show_git_icon'] and (show_branch or show_git):
        label = icon + (' ' + branch if show_branch else '')
        row.append((label, draft['branch_colour'], False))
        if show_git:
            row.append((' ' + SAMPLE_GIT_STATUS, GIT_STATUS_COLOR, False))
    else:
        if show_git:
            row.append((SAMPLE_GIT_STATUS, GIT_STATUS_COLOR, False))
        if show_branch:
            append_segment(row, branch, draft['branch_colour'])
    if not row and draft['empty_git_row'] == 'placeholder':
        git_hidden = not (draft['show_git'] or draft['show_branch'])
        message = 'Git details hidden' if git_hidden else 'no git repo'
        if draft['show_git_icon']:
            message = icon + ' ' + message
        row = [(message, PLACEHOLDER_COLOUR, False)]
    return row


# Names the samples might be given with herdr agent rename.
SAMPLE_HANDLES = {'claude': 'builder', 'codex': 'reviewer'}
GROUP_INDENT = '  '


def agent_name_row(draft, sample, now):
    """The sample's first row: its symbol, then what Agent row shows names."""
    choice = draft['agent_row_shows']
    if choice == 'space':
        return name_row(draft, sample, now)
    name = SAMPLE_HANDLES.get(sample.agent, '') if choice == 'agent' else ''
    return name_row(draft, sample._replace(name=name), now, show_name=True)


def grouped_rows(draft, sample, rows):
    """The space's heading over its agent's rows, indented as Herdr lines them up."""
    heading = [(sample.name, name_color(draft, sample.color), draft['name_bold'])]
    indented = [[(GROUP_INDENT + row[0][0], *row[0][1:]), *row[1:]] if row else row for row in rows]
    return [heading, *indented]


def agent_sample_rows(draft, sample, now):
    remote = sample.remote and draft['show_machine']
    machine_color = settings.machine_colour(draft)
    title = agent_name_row(draft, sample, now)
    if remote and draft['machine_position'] == 'name':
        append_segment(title, SAMPLE_MACHINE, machine_color, bold=True)
    if draft['show_tab']:
        append_segment(title, sample.tab, draft['tab_colour'])
    rows = [title]
    details = []
    if remote and draft['machine_position'] == 'details':
        details.append((SAMPLE_MACHINE, machine_color, True))
    if draft['agent_text']:
        info = sample_agent_info(draft, sample)
        if info:
            append_segment(details, info, draft['agent_colour'])
    if details:
        rows.append(details)
    return grouped_rows(draft, sample, rows) if draft['group_agents'] else rows


def sample_agent_info(draft, sample):
    observation = {'model': sample.model, 'effort': 'medium',
                   'context': {'used': 250000, 'window': 1000000, 'percent': 25}}
    try:
        settings.validate(draft)
        return tokens(observation, sample.agent, draft)['agent_info'] or ''
    except ValueError:
        return 'Check the template syntax.'


# Curses color pair numbers, one per drawn cell so colors never clash:
# previews use 1-48 (2 lists x 24: up to 6 rows of 4 segments),
# form color fields 60-79 and palette swatches from 80 (at most 48 options).
PREVIEW_PAIRS = 1
PREVIEW_PAIRS_PER_PANEL = 24
PREVIEW_PAIRS_PER_ROW = 4
FIELD_PAIRS = 60
SWATCH_PAIRS = 80
# Each list has a heading and at most five rows (two samples, two rows each and
# a gap); grouped agents add a heading per sample and a group gap, eight rows.
# A blank row separates the lists. Reserving the full height keeps the form
# still while settings change the sample rows.
PREVIEW_HEIGHT = 16


def color_attr(fg, pair):
    try:
        color = space_colors.nearest_color(fg) if curses.COLORS >= 256 else curses.COLOR_CYAN
        curses.init_pair(pair, color, -1)
        return curses.color_pair(pair)
    except (ValueError, curses.error):
        return 0


def preview(screen, session, top):
    """Draw the Spaces list with the Agents list under it, as Herdr's sidebar stacks them."""
    _, width = screen.getmaxyx()
    now = time.monotonic()
    y = top
    for panel, kind in enumerate(('spaces', 'agents')):
        put(screen, y, 2, kind.upper(), curses.A_BOLD)
        if kind == 'spaces':
            put(screen, y, 10, 'samples · the first has no Git repo', curses.A_DIM)
        rows = preview_rows(session, kind, now)
        for row, segments in enumerate(rows):
            first_pair = PREVIEW_PAIRS + panel * PREVIEW_PAIRS_PER_PANEL + row * PREVIEW_PAIRS_PER_ROW
            draw_segments(screen, y + 1 + row, 2, width - 4, segments, first_pair)
        y += len(rows) + 2


def draw_segments(screen, y, left, width, segments, first_pair):
    x = left
    for index, (text, fg, bold) in enumerate(segments):
        available = left + width - x
        if available <= 0:
            break
        attr = color_attr(fg, first_pair + index)
        put(screen, y, x, text[:available], attr | (curses.A_BOLD if bold else 0))
        x += len(text)


GALLERY_LABEL_MIN = 10


def animation_cells(width):
    size = max(1, (width - 4) // len(settings.ANIMATIONS))
    return [(ident, 2 + i * size, size) for i, ident in enumerate(settings.ANIMATIONS)]


def gallery_height(width):
    """A glyph row, plus a name row when the cells are wide enough for names."""
    size = (width - 4) // len(settings.ANIMATIONS)
    return 2 if size >= GALLERY_LABEL_MIN else 1


def palette_cells(width, count):
    """(index, row, x) per swatch: three squares per hue, with a gutter between hues."""
    columns = space_colors.columns_for(width)
    cells = []
    for i in range(count):
        row = 3 + (i // columns) * 3
        x = 2 + ((i % columns) // 3) * 18 + (i % 3) * 5
        cells.append((i, row, x))
    return cells


HELP_INDENT = 5


def help_lines(session, field, width):
    """The selected field's help, or why it is inactive, wrapped under it."""
    text = inactive_reason(session, field.key) or field_help(session, field)
    return textwrap.wrap(text, max(1, width - HELP_INDENT - 1))


def status_lines(session, width):
    return textwrap.wrap(collection_status(session), max(1, width - HELP_INDENT - 1))


def template_lines(session, width):
    return textwrap.wrap(session.value('text_template') or '(empty)', width - 4)


def extra_height(session, field, width):
    """Rows a field draws below its label row and help."""
    if field.key == 'animation':
        return gallery_height(width)
    if field.key == 'text_template':
        return len(template_lines(session, width)) + 1 + len(template_help(width))
    if field.key == 'collect_details':
        return len(status_lines(session, width))
    return 0


@dataclass
class FormLayout:
    fields: list
    positions: list  # (row, height) per field; the selected one includes its help
    headings: list  # (row, title)
    total: int
    value_column: int


def form_layout(session, section, width, selected):
    """Place the page's headings and fields; only the selected field shows help."""
    fields, positions, headings = [], [], []
    row = 0
    for item in active_items(session, section):
        if isinstance(item, Heading):
            row += 1 if row else 0
            headings.append((row, item.title))
            row += 1
            continue
        help_rows = len(help_lines(session, item, width)) if len(fields) == selected else 0
        height = 1 + help_rows + extra_height(session, item, width)
        fields.append(item)
        positions.append((row, height))
        row += height
    return FormLayout(fields, positions, headings, row, value_column(fields, width))


def value_column(fields, width):
    """Line values up just past the page's longest label, leaving room for the value."""
    longest = max(len(field.label) for field in fields)
    return min(longest + 5, width - 16)


class FormCanvas:
    """Draws form rows, offset by scroll, into screen rows top..bottom-1 and
    records click targets in screen coordinates."""

    def __init__(self, screen, top, bottom, scroll):
        self.screen = screen
        self.top = top
        self.bottom = bottom
        self.scroll = scroll
        self.hits = []

    def screen_y(self, row):
        y = self.top + row - self.scroll
        return y if self.top <= y < self.bottom else None

    def draw(self, row, x, text, attr=0):
        y = self.screen_y(row)
        if y is not None:
            put(self.screen, y, x, text, attr)

    def hit(self, row, x, length, action):
        y = self.screen_y(row)
        if y is not None:
            self.hits.append((y, x, x + length, action))


def draw_form(screen, session, section, layout, view, top, bottom):
    """Draw a clipped, scrollable form; return its click targets and total height in rows."""
    _, width = screen.getmaxyx()
    canvas = FormCanvas(screen, top, bottom, view.scroll)
    for row, title in layout.headings:
        canvas.draw(row, 1, title, curses.A_BOLD | curses.A_DIM)
    for index, (field, (row, _)) in enumerate(zip(layout.fields, layout.positions)):
        selected = index == view.selected
        highlighted = selected and not view.palette_focus
        draw_field(canvas, session, field, index, row, layout.value_column, width,
                   selected, highlighted)
    total = layout.total
    if SECTIONS[section].name == 'Colors':
        total = draw_palette(canvas, session, layout.fields, view.color_target,
                             view.palette_focus, total + 1, width)
    return canvas.hits, total


def draw_field(canvas, session, field, index, row, column, width, selected, highlighted):
    inactive = bool(inactive_reason(session, field.key))
    attr = curses.A_REVERSE if highlighted else 0
    if inactive:
        attr |= curses.A_DIM
    label = ('> ' if selected else '  ') + field.label
    canvas.draw(row, 1, label[:column - 2], attr)
    canvas.hit(row, 1, width - 2, ('field', index))
    draw_value(canvas, session, field, index, row, column, attr, inactive)
    below = row + 1
    if selected:
        for line in help_lines(session, field, width):
            canvas.draw(below, HELP_INDENT, line, curses.A_DIM)
            below += 1
    draw_extras(canvas, session, field, index, below, width, attr, inactive)


def draw_value(canvas, session, field, index, row, column, attr, inactive):
    key = field.key
    if key == 'text_template':
        return
    if is_color(key):
        current = session.value(key)
        fg = next(fg for value, _, fg in color_options(session, key) if value == current)
        swatch_attr = curses.A_DIM if inactive else color_attr(fg, FIELD_PAIRS + index)
        canvas.draw(row, column, '██', swatch_attr)
        canvas.draw(row, column + 3, display_value(session, key), attr)
    elif key.endswith('_symbol'):
        draw_symbol_sample(canvas, session, key, index, row, column, inactive)
    else:
        canvas.draw(row, column, display_value(session, key), attr)


def draw_symbol_sample(canvas, session, key, index, row, column, inactive):
    """The symbol beside a sample name, in the color and weight the sidebar uses."""
    draft = session.draft
    sample = preview_samples(session)[0]
    style = curses.A_DIM
    if not inactive:
        style = color_attr(name_color(draft, sample.color), FIELD_PAIRS + index)
        style |= curses.A_BOLD if draft['name_bold'] else 0
    canvas.draw(row, column, draft[key] + ' ' + sample.name, style)


def draw_extras(canvas, session, field, index, top, width, attr, inactive):
    if field.key == 'animation':
        draw_animation_gallery(canvas, session, index, top, width, inactive)
    elif field.key == 'text_template':
        draw_template_field(canvas, session, index, top, width, attr)
    elif field.key == 'collect_details':
        for dy, line in enumerate(status_lines(session, width)):
            canvas.draw(top + dy, HELP_INDENT, line)


def draw_animation_gallery(canvas, session, index, top, width, inactive):
    """A clickable live preview of every animation, each named on one line."""
    now = 0 if inactive else time.monotonic()
    height = gallery_height(width)
    for ident, x, size in animation_cells(width):
        glyph = symbol('working', now, session.draft | {'animation': ident, 'animate': True})
        chosen = ident == session.draft['animation']
        if inactive:
            glyph_attr = curses.A_DIM
        else:
            glyph_attr = curses.A_REVERSE if chosen else 0
        cell = '[ ' + glyph + ' ]' if chosen else '  ' + glyph + '  '
        canvas.draw(top, x, cell, glyph_attr)
        if height > 1:
            label_attr = curses.A_BOLD if chosen and not inactive else curses.A_DIM
            canvas.draw(top + 1, x, settings.ANIMATIONS[ident][0][:size - 1], label_attr)
        for dy in range(height):
            canvas.hit(top + dy, x, size, ('animation', ident, index))


def draw_template_field(canvas, session, index, top, width, attr):
    lines = template_lines(session, width)
    for dy, line in enumerate(lines):
        canvas.draw(top + dy, 3, line, attr)
        canvas.hit(top + dy, 1, width - 2, ('field', index))
    help_top = top + len(lines) + 1
    for dy, line in enumerate(template_help(width)):
        canvas.draw(help_top + dy, 3, line, curses.A_DIM)


def draw_palette(canvas, session, fields, target, palette_focus, start, width):
    """Draw the swatch grid for the target color field; return the form's new height."""
    options = color_options(session, target)
    label = next(field.label for field in fields if field.key == target)
    canvas.draw(start, 1, label + ' — choose a swatch', curses.A_BOLD)
    if palette_focus:
        hint = 'Arrows: choose   Enter / Esc: back to fields'
    else:
        hint = 'Click a swatch, or select its field and press Enter.'
    canvas.draw(start + 1, 1, hint, curses.A_DIM)
    inactive = bool(inactive_reason(session, target))
    current = session.value(target)
    for index, row, x in palette_cells(width, len(options)):
        value, _, fg = options[index]
        attr = curses.A_DIM if inactive else color_attr(fg, SWATCH_PAIRS + index)
        for dy in (0, 1):
            canvas.draw(start + row + dy, x, '████', attr)
            canvas.hit(start + row + dy, x, 4, ('swatch', value))
        if value == current:
            canvas.draw(start + row + 1, x - 1, '>', curses.A_BOLD)
            canvas.draw(start + row + 1, x + 4, '<', curses.A_BOLD)
    grid_rows = math.ceil(len(options) / space_colors.columns_for(width))
    end = start + 3 + grid_rows * 3
    canvas.draw(end, 1, display_value(session, target), curses.A_BOLD)
    if target == '@colour' and session.wid is None:
        canvas.draw(end + 1, 1, 'Sample only; no real space is changed.', curses.A_DIM)
    return end + 2


TITLE = 'HERDR SIDEBAR CUSTOMIZER'
MIN_HEIGHT, MIN_WIDTH = 24, 48
PREVIEW_MIN_HEIGHT = 32
# Hints with a key are also clickable buttons.
HINTS = (('←→ change', None), ('↵ edit', None), ('Tab page', None), ('A apply', 'a'),
         ('U undo all', 'u'), ('D defaults', 'd'), ('Q close', 'q'))
HINT_SEPARATOR = ' · '
PAGE_KEYS = tuple(str(number) for number in range(1, len(SECTIONS) + 1))
ACTIVATE_KEYS = (*ENTER_KEYS, ' ')
ARROW_KEYS = (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN)
LEFT_CLICK = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
WHEEL_UP = curses.BUTTON4_PRESSED
WHEEL_DOWN = getattr(curses, 'BUTTON5_PRESSED', 0)
WHEEL_STEP = 3


@dataclass
class View:
    """Navigation state that survives between frames."""
    section: int = 0
    selected: int = 0
    scroll: int = 0
    color_target: str = '@colour'
    palette_focus: bool = False
    pending: str | None = None  # 'close' or 'undo' while awaiting confirmation
    reveal: bool = True  # scroll the selection into view on the next frame

    def open_page(self, section):
        self.section = section
        self.reset_page()

    def reset_page(self):
        self.selected = 0
        self.scroll = 0
        self.palette_focus = False


@dataclass
class Frame:
    """What one drawn frame needs for input handling."""
    height: int
    width: int
    fields: list
    total: int
    visible: int
    hits: list = dataclass_field(default_factory=list)
    tab_hits: list = dataclass_field(default_factory=list)
    button_hits: list = dataclass_field(default_factory=list)

    @property
    def max_scroll(self):
        return max(0, self.total - self.visible)


def init_curses(screen):
    curses.curs_set(0)
    curses.set_escdelay(30)
    screen.keypad(True)
    screen.timeout(60)
    curses.mouseinterval(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()


def screen_main(screen, session):
    init_curses(screen)
    view = View()
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        draw_title(screen, session)
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            if wait_in_small_pane(screen, session, view):
                return
            continue
        frame = draw_settings(screen, session, view)
        if frame is None:
            continue
        screen.refresh()
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        try:
            if handle_input(screen, session, view, frame, key):
                return
        # A failed or timed-out config check reports the error and keeps the draft.
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
            session.message = str(error)


def save_state(session, width):
    """'Saved', or how many changes await Apply, shortened to fit beside the title."""
    count = session.change_count
    if not count:
        return 'Saved', curses.A_DIM
    text = f"{count} unsaved {'change' if count == 1 else 'changes'} · A to apply"
    if len(TITLE) + 3 + len(text) > width - 2:
        text = f'{count} unsaved · A to apply'
    return text, curses.A_REVERSE | curses.A_BOLD


def draw_title(screen, session):
    _, width = screen.getmaxyx()
    put(screen, 0, 1, TITLE, curses.A_BOLD)
    text, attr = save_state(session, width)
    put(screen, 0, len(TITLE) + 3, text, attr)


def wait_in_small_pane(screen, session, view):
    """Show the resize notice and read one key; return True to close."""
    put(screen, 3, 1, 'Enlarge this pane (48 columns / 24 rows minimum).')
    put(screen, 4, 1, 'Q / Esc closes; unsaved changes need a second press.')
    screen.refresh()
    try:
        key = screen.get_wch()
    except curses.error:
        return False
    if key not in CLOSE_KEYS:
        view.pending = None
        return False
    if not session.dirty or view.pending == 'close':
        return True
    view.pending = 'close'
    return False


def draw_preview_area(screen, session, section, height, top):
    """Draw the previews on pages they affect; return the first free row."""
    if SECTIONS[section].name not in PREVIEW_PAGES:
        return top
    if height < PREVIEW_MIN_HEIGHT:
        put(screen, top, 1, 'Previews: enlarge the pane to see them.', curses.A_DIM)
        return top + 2
    preview(screen, session, top)
    return top + PREVIEW_HEIGHT + 1


def draw_settings(screen, session, view):
    """Draw a full frame. Return None when the scroll had to be clamped and the
    frame must be redrawn before reading input."""
    height, width = screen.getmaxyx()
    tab_hits, tab_end = draw_tabs(screen, view.section, width, 1)
    top = draw_preview_area(screen, session, view.section, height, tab_end + 2)
    view.selected = min(view.selected, len(active_fields(session, view.section)) - 1)
    layout = form_layout(session, view.section, width, view.selected)
    field = layout.fields[view.selected]
    if is_color(field.key):
        view.color_target = field.key
    status = textwrap.wrap(session.message, width - 2) or ['']
    hints = hint_layout(width)
    hint_rows = hints[-1][0] + 1
    bottom = height - hint_rows - len(status) - 1
    visible = max(1, bottom - top)
    if view.reveal:
        reveal_selection(session, view, layout, width, visible)
    hits, total = draw_form(screen, session, view.section, layout, view, top, bottom)
    frame = Frame(height, width, layout.fields, total, visible, hits, tab_hits)
    if view.scroll > frame.max_scroll:
        view.scroll = frame.max_scroll
        return None
    for y, line in enumerate(status, bottom + 1):
        put(screen, y, 1, line, curses.A_BOLD)
    frame.button_hits = draw_hints(screen, hints, height - hint_rows)
    return frame


def draw_tabs(screen, section, width, y):
    """Draw page tabs, wrapping as needed; return click targets and the last tab row."""
    hits = []
    x = 1
    for index, (name, _) in enumerate(SECTIONS):
        label = f' {index + 1} {name} '
        if x + len(label) > width - 1:
            x = 1
            y += 1
        put(screen, y, x, label, curses.A_REVERSE if index == section else curses.A_DIM)
        hits.append((y, x, x + len(label), index))
        x += len(label)
    return hits, y


def hint_layout(width):
    """(line, x, text, key) for each key hint, wrapping the hint line to the pane."""
    placed = []
    line, x = 0, 1
    for text, key in HINTS:
        if placed:
            if x + len(HINT_SEPARATOR) + len(text) > width - 1:
                line, x = line + 1, 1
            else:
                x += len(HINT_SEPARATOR)
        placed.append((line, x, text, key))
        x += len(text)
    return placed


def draw_hints(screen, hints, top):
    """Draw the key hints from row top; return (y, left, right, key) click targets."""
    hits = []
    for line, x, text, key in hints:
        y = top + line
        if x > 1:
            put(screen, y, x - len(HINT_SEPARATOR), HINT_SEPARATOR, curses.A_DIM)
        put(screen, y, x, text, curses.A_BOLD if key else curses.A_DIM)
        if key:
            hits.append((y, x, x + len(text), key))
    return hits


def reveal_selection(session, view, layout, width, visible):
    """Scroll so the selected field and its help, or the focused swatch, are on screen."""
    row, size = layout.positions[view.selected]
    if view.palette_focus:
        options = color_options(session, view.color_target)
        current = session.value(view.color_target)
        index = next(i for i, (value, _, _) in enumerate(options) if value == current)
        row = layout.total + 1 + palette_cells(width, len(options))[index][1]
        size = 2
    size = min(size, visible)
    if row < view.scroll:
        view.scroll = row
    elif row + size > view.scroll + visible:
        view.scroll = row + size - visible
    view.reveal = False


def handle_input(screen, session, view, frame, key):
    """Act on one key or mouse event; return True when the pane should close."""
    if key == curses.KEY_RESIZE:
        view.reveal = True
        return False
    if key == curses.KEY_MOUSE:
        _, x, y, _, buttons = curses.getmouse()
        if not buttons & (LEFT_CLICK | WHEEL_UP | WHEEL_DOWN):
            return False
    # Any handled event answers a pending confirmation, cancelling it unless it repeats.
    previous_pending = view.pending
    view.pending = None
    if previous_pending:
        session.message = ''
    if key == curses.KEY_MOUSE:
        key = handle_mouse(session, view, frame, x, y, buttons)
        if key is None:
            return False
    return handle_key(screen, session, view, frame, key, previous_pending)


def handle_mouse(session, view, frame, x, y, buttons):
    """Act on a click or wheel event; return a key to process next, or None."""
    if buttons & LEFT_CLICK:
        return handle_click(session, view, frame, x, y)
    if buttons & WHEEL_UP:
        view.scroll = max(0, view.scroll - WHEEL_STEP)
    elif buttons & WHEEL_DOWN:
        view.scroll = min(frame.max_scroll, view.scroll + WHEEL_STEP)
    return None


def hit_at(hits, x, y):
    # Later targets are drawn on top, so they win.
    return next((action for row, left, right, action in reversed(hits)
                 if y == row and left <= x < right), None)


def handle_click(session, view, frame, x, y):
    tab = hit_at(frame.tab_hits, x, y)
    if tab is not None:
        view.open_page(tab)
        return None
    button = hit_at(frame.button_hits, x, y)
    if button is not None:
        return button
    action = hit_at(frame.hits, x, y)
    if action is None:
        return None
    kind = action[0]
    if kind == 'animation':
        _, ident, index = action
        session.set('animation', ident)
        view.selected = index
    elif kind == 'swatch':
        session.set(view.color_target, action[1])
        view.palette_focus = True
        view.selected = next(i for i, field in enumerate(frame.fields)
                             if field.key == view.color_target)
    else:
        view.selected = action[1]
        view.palette_focus = False
        return '\n'
    return None


def handle_key(screen, session, view, frame, key, previous_pending):
    """Dispatch one key; return True when the pane should close."""
    field = frame.fields[view.selected]
    if view.palette_focus and key in (*ENTER_KEYS, ESCAPE):
        view.palette_focus = False
        view.reveal = True
        return False
    if key in CLOSE_KEYS:
        if not session.dirty or previous_pending == 'close':
            return True
        view.pending = 'close'
        session.message = 'Unsaved changes. Q / Esc again discards; A applies.'
        return False
    if key in ('a', 'A'):
        session.apply()
    elif key in ('u', 'U'):
        undo_all(session, view, previous_pending)
    elif key in ('d', 'D'):
        session.defaults()
        view.reset_page()
    elif key in ('\t', curses.KEY_BTAB):
        step = -1 if key == curses.KEY_BTAB else 1
        view.open_page((view.section + step) % len(SECTIONS))
    elif key in PAGE_KEYS:
        view.open_page(int(key) - 1)
    elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
        step = frame.visible if key == curses.KEY_NPAGE else -frame.visible
        view.scroll = max(0, min(frame.max_scroll, view.scroll + step))
    elif view.palette_focus and key in ARROW_KEYS:
        move_in_palette(session, field, frame.width, key)
        view.reveal = True
    elif key in (curses.KEY_UP, curses.KEY_DOWN):
        step = -1 if key == curses.KEY_UP else 1
        view.selected = (view.selected + step) % len(frame.fields)
        view.reveal = True
    elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
        session.cycle(field, -1 if key == curses.KEY_LEFT else 1)
        view.reveal = True
    elif key in ACTIVATE_KEYS:
        activate_field(screen, session, view, field)
    return False


def move_in_palette(session, field, width, key):
    columns = space_colors.columns_for(width)
    steps = {curses.KEY_LEFT: -1, curses.KEY_RIGHT: 1,
             curses.KEY_UP: -columns, curses.KEY_DOWN: columns}
    session.cycle(field, steps[key])


def undo_all(session, view, previous_pending):
    """Return to the saved settings, asking first when that would discard edits."""
    if session.dirty and previous_pending != 'undo':
        view.pending = 'undo'
        session.message = 'Undo all unsaved changes? U again undoes them; any other key cancels.'
        return
    session.reload()
    session.message = 'Back to your saved settings.'
    view.reset_page()
    view.reveal = True


def activate_field(screen, session, view, field):
    """Enter or Space: open the palette, edit text, or step a choice."""
    reason = inactive_reason(session, field.key)
    if reason:
        session.message = reason
        return
    if is_color(field.key):
        view.color_target = field.key
        view.palette_focus = True
    elif field.key == '@agent_names':
        edit_agent_names(screen, session)
    elif field.options is None:
        value = edit_text(screen, field.label, session.value(field.key))
        if value is not None:
            session.set(field.key, value)
    else:
        session.cycle(field, 1)
    view.reveal = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('open', 'edit'))
    parser.add_argument('--right', action='store_true', help='Open beside the calling pane')
    args = parser.parse_args()
    endpoint = os.environ.get('HERDR_SOCKET_PATH')
    if os.environ.get('HERDR_ENV') != '1' or not endpoint:
        parser.error('Run inside Herdr')
    from installation import record
    installed = record()
    if args.command == 'open':
        entrypoint = 'settings' if installed.get('enabled') else 'setup'
        params = {'plugin_id': PLUGIN, 'entrypoint': entrypoint}
        if args.right:
            params.update(placement='split', direction='right',
                          target_pane_id=os.environ['HERDR_PANE_ID'], focus=False)
        rpc(endpoint, 'plugin.pane.open', params)
    elif not installed.get('enabled'):
        from manage import setup_screen
        curses.wrapper(setup_screen, endpoint)
    else:
        directory = endpoint_state(state_root(), endpoint)
        session = Session(endpoint, directory, layout_mode=installed.get('mode'))
        curses.wrapper(screen_main, session)


if __name__ == '__main__':
    main()
