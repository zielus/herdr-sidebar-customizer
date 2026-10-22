import curses
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import detail_hints
import settings_ui as ui
import sidebar_settings as prefs


class Screen:
    """Enough of a curses window to draw frames and replay keys."""

    def __init__(self, height=63, width=91, keys=()):
        self.height = height
        self.width = width
        self.keys = iter(keys)
        self.frames = []
        self.erase()

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows = [[' '] * self.width for _ in range(self.height)]

    def addnstr(self, y, x, text, n, style=0):
        for i, char in enumerate(text[:n]):
            if 0 <= y < self.height and 0 <= x + i < self.width:
                self.rows[y][x + i] = char

    def refresh(self):
        self.frames.append('\n'.join(''.join(row).rstrip() for row in self.rows))

    def get_wch(self):
        return next(self.keys)

    def keypad(self, *args):
        pass

    def timeout(self, *args):
        pass


def row_text(rows):
    return '\n'.join(''.join(segment[0] for segment in row) for row in rows)


def click(x, y, buttons=curses.BUTTON1_PRESSED):
    return (0, x, y, 0, buttons)


class SettingsUITests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(ui.settings, 'read_settings', return_value=dict(prefs.DEFAULTS)))
        self.enterContext(patch.object(ui, 'rpc', return_value={'workspaces': []}))
        self.session = ui.Session('test', self.root)
        self.enterContext(patch.object(ui, 'color_attr', return_value=0))

    def patch_curses(self):
        for name in ('curs_set', 'set_escdelay', 'mouseinterval', 'mousemask',
                     'start_color', 'use_default_colors'):
            self.enterContext(patch.object(curses, name))

    def run_ui(self, keys, height=63, width=91):
        self.patch_curses()
        screen = Screen(height, width, keys)
        ui.screen_main(screen, self.session)
        return screen.frames

    def frame_for(self, page, height=63, width=91):
        """Draw one page and return the Frame with its click targets."""
        self.patch_curses()
        view = ui.View(section=page)
        frame = None
        while frame is None:
            frame = ui.draw_settings(Screen(height, width), self.session, view)
        return frame

    # Previews

    def test_empty_session_gets_two_examples_without_creating_or_saving_spaces(self):
        for kind in ('spaces', 'agents'):
            rendered = row_text(ui.preview_rows(self.session, kind, 0))
            self.assertIn('Website', rendered)
            self.assertIn('Sandbox', rendered)
        self.session.set('@colour', 'red_light')
        self.assertEqual(self.session.value('@colour'), 'red_light')
        self.assertEqual(self.session.colours, {})
        self.assertFalse(self.session.dirty)
        ui.rpc.assert_called_once_with('test', 'workspace.list')

    def test_both_previews_respond_to_visibility_and_independent_spacing(self):
        before = {kind: ui.preview_rows(self.session, kind, 0) for kind in ('spaces', 'agents')}
        self.session.draft |= {'spaces_gap': 1}
        self.assertEqual(len(ui.preview_rows(self.session, 'spaces', 0)), len(before['spaces']) + 1)
        self.assertEqual(ui.preview_rows(self.session, 'agents', 0), before['agents'])
        self.session.draft |= {'agents_gap': 1}
        self.assertEqual(len(ui.preview_rows(self.session, 'agents', 0)), len(before['agents']) + 1)
        for field, missing in [('show_git', '↑1 ↓2'), ('show_branch', 'feature/ui')]:
            self.session.draft[field] = False
            self.assertNotIn(missing, str(ui.preview_rows(self.session, 'spaces', 0)))
        self.session.draft |= {'agent_text': False, 'show_tab': False}
        # After the name, the remote sample's label needs no details row.
        self.assertEqual(len(ui.preview_rows(self.session, 'agents', 0)), 3)
        self.assertIn('Build box', str(ui.preview_rows(self.session, 'agents', 0)[2]))
        # Leading the details row, it keeps that row even without details.
        self.session.draft['machine_position'] = 'details'
        self.assertEqual(len(ui.preview_rows(self.session, 'agents', 0)), 4)
        self.session.draft['show_machine'] = False
        self.assertEqual(len(ui.preview_rows(self.session, 'agents', 0)), 3)

    def test_previews_stack_agents_under_spaces_and_fit_their_reserved_rows(self):
        frame = self.run_ui(['q'])[-1].splitlines()
        spaces = next(i for i, line in enumerate(frame) if line.startswith('  SPACES'))
        agents = next(i for i, line in enumerate(frame) if line.startswith('  AGENTS'))
        self.assertGreater(agents, spaces + 2)
        self.assertIn('Website', frame[spaces + 1])
        self.session.draft |= {'spaces_gap': 1, 'agents_gap': 1, 'machine_position': 'details'}
        tallest = sum(len(ui.preview_rows(self.session, kind, 0)) + 1 for kind in ('spaces', 'agents'))
        self.assertLessEqual(tallest + 1, ui.PREVIEW_HEIGHT)

    def test_previews_show_only_on_pages_that_change_rows(self):
        for page, section in enumerate(ui.SECTIONS, 1):
            with self.subTest(page=section.name):
                frame = self.run_ui([str(page), 'q'])[-1]
                self.assertEqual('AGENTS' in frame, section.name != 'Shortcuts')

    def test_empty_git_row_choice_changes_space_height_but_keeps_gap_independent(self):
        before = ui.preview_rows(self.session, 'spaces', 0)
        self.assertEqual(len(before), 4)
        self.assertEqual(before[1], [])
        self.assertTrue(before[3])
        self.session.set('empty_git_row', 'hide')
        self.assertEqual(ui.preview_rows(self.session, 'spaces', 0), [before[0], before[2], before[3]])
        self.session.set('spaces_gap', 1)
        self.assertEqual(ui.preview_rows(self.session, 'spaces', 0), before)
        self.session.set('empty_git_row', 'blank')
        self.assertEqual(ui.preview_rows(self.session, 'spaces', 0), before[:2] + [[]] + before[2:])
        self.session.draft |= {'show_branch': False, 'show_git': False, 'spaces_gap': 0}
        self.assertEqual(ui.preview_rows(self.session, 'spaces', 0), [before[0], [], before[2], []])

    def test_placeholder_and_git_icon_preview_match_the_selected_modes(self):
        from space_rows import GIT_ICON
        self.session.set('empty_git_row', 'placeholder')
        self.session.set('@git_icon', prefs.DEFAULTS['git_icon'])
        rows = ui.preview_rows(self.session, 'spaces', 0)
        self.assertEqual(row_text([rows[1]]), GIT_ICON + ' no git repo')
        self.assertTrue(row_text([rows[3]]).startswith(GIT_ICON + ' feature/ui ↑1'))
        self.session.set('empty_git_row', 'blank')
        self.assertEqual(ui.preview_rows(self.session, 'spaces', 0)[1], [])
        self.session.set('empty_git_row', 'hide')
        self.assertEqual(len(ui.preview_rows(self.session, 'spaces', 0)), 3)
        self.session.draft |= {'empty_git_row': 'placeholder', 'show_branch': False, 'show_git': False}
        self.assertIn('Git details hidden', str(ui.preview_rows(self.session, 'spaces', 0)))

    # Merged controls

    def test_git_icon_picker_offers_none_then_every_icon_without_saving(self):
        field = ui.find_field('@git_icon')
        self.session.set('empty_git_row', 'placeholder')
        self.assertEqual(ui.display_value(self.session, '@git_icon'), 'None')
        seen = set()
        for _ in prefs.GIT_ICONS:
            self.session.cycle(field, 1)
            ident = self.session.value('@git_icon')
            label, glyph = prefs.GIT_ICONS[ident]
            seen.add(ident)
            self.assertTrue(self.session.draft['show_git_icon'])
            self.assertEqual(ui.display_value(self.session, '@git_icon'), glyph + ' ' + label)
            self.assertEqual(ui.preview_rows(self.session, 'spaces', 0)[1][0][0], glyph + ' no git repo')
        self.assertEqual(seen, set(prefs.GIT_ICONS))
        chosen = self.session.draft['git_icon']
        self.session.cycle(field, 1)
        self.assertEqual(self.session.value('@git_icon'), 'none')
        self.assertFalse(self.session.draft['show_git_icon'])
        self.assertEqual(self.session.draft['git_icon'], chosen)
        self.assertEqual(self.session.base['git_icon'], '41')
        self.assertIn('Nerd Font', ui.find_field('@git_icon').help)

    def test_space_row_shows_writes_both_saved_switches(self):
        field = ui.find_field('@row_shows')
        expected = [('name', False, True), ('symbol', True, False), ('both', True, True)]
        for choice, icons, names in expected:
            self.session.cycle(field, 1)
            self.assertEqual(self.session.value('@row_shows'), choice)
            self.assertEqual(self.session.draft['state_icons'], icons)
            self.assertEqual(self.session.draft['space_names'], names)
        self.assertEqual(ui.display_value(self.session, '@row_shows'), 'Symbol and name')

    def test_space_row_shows_reports_a_saved_empty_row_without_rewriting_it(self):
        saved = prefs.DEFAULTS | {'state_icons': False, 'space_names': False}
        with patch.object(ui.settings, 'read_settings', return_value=dict(saved)):
            self.session.reload()
        self.assertEqual(ui.display_value(self.session, '@row_shows'), 'Nothing')
        self.assertFalse(self.session.dirty)
        self.assertNotIn('neither', ui.find_field('@row_shows').options)
        self.session.cycle(ui.find_field('@row_shows'), 1)
        self.assertEqual(self.session.value('@row_shows'), 'both')

    def test_context_choice_merges_visibility_and_mode(self):
        field = ui.find_field('@context')
        self.assertEqual(ui.display_value(self.session, '@context'), 'Used %')
        self.session.cycle(field, 1)
        self.assertEqual(self.session.draft['context_mode'], 'remaining')
        self.assertEqual(ui.display_value(self.session, '@context'), 'Remaining %')
        self.session.cycle(field, 1)
        self.assertFalse(self.session.draft['show_context'])
        self.assertEqual(self.session.draft['context_mode'], 'remaining')
        self.assertEqual(ui.display_value(self.session, '@context'), 'Off')
        self.assertEqual(ui.inactive_reason(self.session, 'show_capacity'), 'Inactive: Context is Off.')
        self.session.cycle(field, 1)
        self.assertTrue(self.session.draft['show_context'])
        self.assertEqual(self.session.draft['context_mode'], 'used')

    def test_symbol_set_applies_presets_and_reports_custom_symbols(self):
        field = ui.find_field('@symbol_set')
        self.assertEqual(ui.display_value(self.session, '@symbol_set'), 'Circles')
        for name, symbols in ui.SYMBOL_SETS.items():
            for glyph in symbols:
                self.assertTrue(prefs.is_single_width_symbol(glyph), glyph)
        self.session.cycle(field, 1)
        self.assertEqual(ui.display_value(self.session, '@symbol_set'), 'Dots')
        self.session.cycle(field, 1)
        self.assertEqual(self.session.draft['blocked_symbol'], '!')
        self.assertEqual(ui.display_value(self.session, '@symbol_set'), 'Plain ASCII')
        self.session.set('idle_symbol', '~')
        self.assertEqual(ui.display_value(self.session, '@symbol_set'), 'Custom')
        self.session.cycle(field, 1)
        self.assertEqual(self.session.draft['idle_symbol'], '○')
        prefs.validate(self.session.draft)

    def test_state_symbols_show_a_sample_name(self):
        frame = self.run_ui(['5', 'q'])[-1]
        for state in ui.STATES:
            self.assertIn(prefs.DEFAULTS[state + '_symbol'] + ' Website', frame)

    def test_agent_names_row_edits_each_label_in_turn(self):
        self.assertEqual(self.session.value('@agent_names'), 'Claude, Codex, OpenCode, pi, omp')
        answers = iter(['Assistant', 'Coder', None])
        with patch.object(ui, 'edit_text', side_effect=lambda *args: next(answers)) as editor:
            ui.activate_field(None, self.session, ui.View(), ui.find_field('@agent_names'))
        self.assertEqual(editor.call_count, 3)
        self.assertIn('Claude (1 of 5)', editor.call_args_list[0].args[1])
        self.assertEqual(self.session.draft['claude_label'], 'Assistant')
        self.assertEqual(self.session.draft['codex_label'], 'Coder')
        self.assertEqual(self.session.draft['opencode_label'], 'OpenCode')
        self.assertEqual(self.session.value('@agent_names'), 'Assistant, Coder, OpenCode, pi, omp')
        self.session.set('show_agent', False)
        self.assertEqual(ui.inactive_reason(self.session, '@agent_names'), 'Inactive: turn on Agent name.')

    def test_cancelled_text_edit_keeps_the_value(self):
        self.session.set('tab_names', True)
        with patch.object(ui, 'edit_text', return_value=None):
            ui.activate_field(None, self.session, ui.View(), ui.find_field('tab_name_format'))
        self.assertEqual(self.session.draft['tab_name_format'], '{topic}')

    def test_human_values_replace_internal_names(self):
        expected = {'machine_position': 'After space name', 'default_hue': 'Random',
                    'default_shade': 'Medium', 'shortcut_spacing': 'Not set',
                    'shortcut_pin_tab': 'Not set', 'spaces_gap': 'Compact',
                    'empty_git_row': 'Blank line', 'text_mode': 'Pick fields'}
        for key, label in expected.items():
            self.assertEqual(ui.display_value(self.session, key), label, key)
        self.session.draft |= {'machine_position': 'details', 'default_hue': 'teal',
                               'empty_git_row': 'placeholder', 'text_mode': 'custom',
                               'agents_gap': 1}
        self.assertEqual(ui.display_value(self.session, 'machine_position'), 'In details row')
        self.assertEqual(ui.display_value(self.session, 'default_hue'), 'Teal')
        self.assertEqual(ui.display_value(self.session, 'empty_git_row'), 'Explain why')
        self.assertEqual(ui.display_value(self.session, 'text_mode'), 'Write a template')
        self.assertEqual(ui.display_value(self.session, 'agents_gap'), 'Spaced')
        self.assertEqual(ui.find_field('shortcut_pin_tab').help,
                         'Type a binding such as prefix+shift+g; clear it to remove.')

    # Pages and layout

    def test_pages_hold_the_approved_fields_in_order(self):
        keys = {section.name: [item.key for item in section.items if isinstance(item, ui.Field)]
                for section in ui.SECTIONS}
        self.assertEqual(list(keys), ['Spaces', 'Agents', 'Tabs', 'Colors', 'Motion & symbols', 'Shortcuts'])
        self.assertEqual(keys['Spaces'], ['@row_shows', 'name_bold', 'spaces_gap', 'show_branch',
                                          'show_git', 'empty_git_row', '@git_icon'])
        self.assertEqual(keys['Agents'][:4], ['collect_details', 'agent_text', 'detail_hints', 'text_mode'])
        self.assertEqual(keys['Agents'][-6:], ['show_machine', 'machine_position', 'agents_gap',
                                               'agent_row_shows', 'group_agents', 'groups_gap'])
        self.assertEqual(keys['Tabs'], ['show_tab', 'show_work', 'tab_names', 'tab_name_format'])
        self.assertEqual(keys['Shortcuts'][-1], 'shortcut_pin_tab')
        saved = {key for fields in keys.values() for key in fields if not key.startswith('@')}
        merged = {'state_icons', 'space_names', 'show_git_icon', 'git_icon', 'show_context',
                  'context_mode', *(label for label, _ in ui.AGENT_LABELS)}
        self.assertEqual(saved | merged, set(prefs.DEFAULTS))

    def test_text_modes_show_exclusive_controls_and_keep_drafts(self):
        self.session.set('claude_label', 'Assistant')
        self.session.set('show_model', False)
        self.session.set('text_mode', 'custom')
        self.assertEqual([f.key for f in ui.active_fields(self.session, 1)],
                         ['collect_details', 'agent_text', 'detail_hints', 'text_mode',
                          'text_template', 'show_machine', 'machine_position', 'agents_gap',
                          'agent_row_shows', 'group_agents', 'groups_gap'])
        self.assertEqual(self.session.draft['text_template'], prefs.DEFAULT_TEMPLATE)
        self.session.set('text_template', '{model} · {remaining_pct} left')
        self.session.set('text_mode', 'automatic')
        self.assertNotIn('text_template', [f.key for f in ui.active_fields(self.session, 1)])
        self.assertEqual(self.session.draft['claude_label'], 'Assistant')
        self.assertFalse(self.session.draft['show_model'])
        self.session.set('text_mode', 'custom')
        self.assertEqual(self.session.draft['text_template'], '{model} · {remaining_pct} left')

    def test_template_reference_is_inline_and_scrollable_in_small_pane(self):
        self.session.set('text_mode', 'custom')
        frames = self.run_ui(['2', *[curses.KEY_DOWN] * 4, 'q', 'q'])
        self.assertIn('AVAILABLE FIELDS', frames[-1])
        for field in prefs.FIELDS:
            self.assertIn('{' + field + '}', frames[-1])
        small = self.run_ui(['2', *[curses.KEY_NPAGE] * 4, 'q', 'q'], 28, 48)
        for field in prefs.FIELDS:
            self.assertIn('{' + field + '}', '\n'.join(small))
        for line in ui.template_help(48):
            self.assertLessEqual(len(line), 44)

    def test_help_appears_indented_under_the_selected_field_only(self):
        frame = self.run_ui([curses.KEY_DOWN, curses.KEY_DOWN, 'q'])[-1].splitlines()
        row = next(i for i, line in enumerate(frame) if line.startswith(' > Row spacing'))
        self.assertEqual(frame[row + 1].strip(), ui.find_field('spaces_gap').help)
        self.assertTrue(frame[row + 1].startswith(' ' * ui.HELP_INDENT))
        self.assertIn('Show branch', frame[row + 2])
        self.assertNotIn(ui.find_field('show_branch').help, '\n'.join(frame))

    def test_inactive_reason_replaces_help_under_the_field(self):
        self.session.set('tab_names', False)
        frame = self.run_ui(['3', curses.KEY_UP, 'q'])[-1]
        self.assertIn('Inactive: turn on Rename tabs from agent topic.', frame)

    def test_animation_gallery_names_fit_one_line_and_arrows_select(self):
        frames = self.run_ui(['5', curses.KEY_DOWN, curses.KEY_RIGHT, 'q', 'q'])
        self.assertEqual(self.session.draft['animation'], '03')
        self.assertEqual(ui.display_value(self.session, 'animation'), 'Half moon')
        for name, _, _ in prefs.ANIMATIONS.values():
            self.assertLess(len(name), ui.GALLERY_LABEL_MIN)
            self.assertIn(name, frames[-1])
        frame = self.frame_for(4)
        animation_hits = [action for _, _, _, action in frame.hits if action[0] == 'animation']
        self.assertEqual({action[1] for action in animation_hits}, set(prefs.ANIMATIONS))

    # Colors

    def test_text_palettes_include_neutrals_preserve_original_and_remain_stable(self):
        for key in ('neutral_colour', 'agent_colour', 'tab_colour', 'branch_colour'):
            choices = ui.color_options(self.session, key)
            self.assertIn(('theme', 'Follow theme', 'theme'), choices)
            self.assertTrue(any(label == 'White' for _, label, _ in choices))
            field = ui.find_field(key)
            self.session.cycle(field, 1)
            self.assertEqual(ui.color_options(self.session, key), choices)
            self.session.cycle(field, -1)
            self.assertEqual(self.session.value(key), self.session.base[key])
            self.assertNotIn('#', ui.display_value(self.session, key))
        self.assertEqual(len(ui.color_options(self.session, '@colour')), 36)

    def test_color_palette_is_embedded_and_keyboard_changes_only_draft(self):
        frames = self.run_ui(['4', *[curses.KEY_DOWN] * 6, '\n', curses.KEY_RIGHT, '\x1b', 'q', 'q'])
        self.assertIn('Agent details — choose a swatch', '\n'.join(frames))
        self.assertNotEqual(self.session.draft['agent_colour'], self.session.base['agent_colour'])
        self.assertEqual(self.session.base['agent_colour'], prefs.DEFAULTS['agent_colour'])
        self.assertTrue(any('████' in frame for frame in frames))
        self.assertIn('6 Shortcuts', frames[-1])
        for heading in (' SPACES', ' TEXT'):
            self.assertIn('\n' + heading + '\n', frames[-1])

    def test_highlighted_swatch_is_named_under_the_palette(self):
        self.session.set('@colour', 'teal_medium')
        self.assertEqual(ui.display_value(self.session, '@colour'), 'Teal · Medium')
        frame = self.run_ui(['4', 'q'])[-1]
        self.assertIn('\n Teal · Medium\n', frame)

    def test_agent_colour_stays_editable_without_details_row(self):
        # Machine labels follow agent_colour by default, even with the row hidden.
        self.session.set('agent_text', False)
        self.assertEqual(ui.inactive_reason(self.session, 'agent_colour'), '')
        self.session.cycle(ui.find_field('agent_colour'), 1)
        self.assertNotEqual(self.session.draft['agent_colour'], self.session.base['agent_colour'])
        self.assertEqual(prefs.machine_colour(self.session.draft), self.session.draft['agent_colour'])

    # Agent detail status

    def write_status(self, status):
        (self.root / 'status.json').write_text(json.dumps(status))

    def collecting(self):
        saved = prefs.DEFAULTS | {'collect_details': True}
        with patch.object(ui.settings, 'read_settings', return_value=dict(saved)):
            self.session.reload()

    def test_collection_status_summarizes_the_latest_worker_report(self):
        self.assertEqual(ui.collection_status(self.session), '')
        self.collecting()
        self.assertEqual(ui.collection_status(self.session), '')
        self.write_status({'checked_at': 1, 'agents': 5, 'models': 5, 'contexts': 4,
                           'missing_session': {'codex': 1}, 'integrations': {'codex': False},
                           'claude_hook': True})
        self.session.status_read_at = None
        self.assertEqual(ui.collection_status(self.session),
                         "Reading details for 5 of 6 agents · 1 needs Herdr's codex integration")
        frame = self.run_ui(['2', 'q'])[-1]
        self.assertIn('Reading details for 5 of 6 agents', frame)
        self.session.set('collect_details', False)
        self.assertEqual(ui.collection_status(self.session), '')

    def test_collection_status_before_apply_and_with_no_agents(self):
        self.session.set('collect_details', True)
        self.assertEqual(ui.collection_status(self.session), 'Collection starts when you apply.')
        self.collecting()
        self.write_status({'checked_at': 1, 'agents': 0, 'models': 0})
        self.assertEqual(ui.collection_status(self.session), 'No supported agents are running.')

    def test_detail_summaries_are_short_forms_of_each_issue(self):
        status = {'missing_session': {'codex': 2, 'pi': 1}, 'integrations': {'pi': True},
                  'unreadable_session': {'opencode': 1}, 'observe_errors': {'claude': 'KeyError'},
                  'missing_context': 1, 'claude_hook': False}
        self.assertEqual(detail_hints.summaries(status), [
            "2 need Herdr's codex integration", '1 needs a restart for details',
            '1 OpenCode session not readable', 'reading Claude details failed',
            '1 Claude needs the status-line hook'])
        self.assertEqual(len(detail_hints.summaries(status)), len(detail_hints.issues(status)))
        self.assertEqual(detail_hints.summaries({}), [])

    # Save state, status and actions

    def test_title_shows_one_save_state_and_counts_changes(self):
        frames = self.run_ui([curses.KEY_DOWN, curses.KEY_RIGHT, 'q', 'q'])
        self.assertIn('HERDR SIDEBAR CUSTOMIZER  Saved', frames[0])
        self.assertIn('1 unsaved change · A to apply', frames[2].splitlines()[0])
        self.assertNotIn('Draft preview only', '\n'.join(frames))
        self.assertNotIn('No pending changes', '\n'.join(frames))
        self.session.set('show_branch', False)
        self.assertEqual(ui.save_state(self.session, 91)[0], '2 unsaved changes · A to apply')
        self.assertEqual(ui.save_state(self.session, 48)[0], '2 unsaved · A to apply')

    def test_one_hint_line_is_readable_and_clickable_at_minimum_pane_size(self):
        frame = self.run_ui(['q'], 24, 48)[-1]
        footer = ' '.join(line.strip() for line in frame.splitlines()[-2:])
        for text, _ in ui.HINTS:
            self.assertIn(text, footer)
        self.assertNotIn('Reload', frame)
        for _, x, text, _ in ui.hint_layout(48):
            self.assertLessEqual(x + len(text), 47)
        wide = ui.hint_layout(91)
        self.assertEqual({line for line, *_ in wide}, {0})

    def test_undo_all_requires_confirmation_and_navigation_cancels_it(self):
        frames = self.run_ui([curses.KEY_RIGHT, 'u', curses.KEY_DOWN, 'u', 'q', 'q'], 24, 48)
        self.assertTrue(self.session.dirty)
        self.assertIn('Undo all unsaved changes?', frames[-2])
        self.assertIn('any other key cancels.', ' '.join(frames[-2].split()))
        frames = self.run_ui(['u', 'u', 'q'])
        self.assertFalse(self.session.dirty)
        self.assertEqual(self.session.draft, self.session.base)
        self.assertIn('Back to your saved settings.', frames[-1])

    def test_mouse_undo_all_and_page_click_follow_confirmation_rules(self):
        self.session.set('@row_shows', 'symbol')
        frame = self.frame_for(0)
        undo = next((y, left) for y, left, _, key in frame.button_hits if key == 'u')
        agents_tab = next((y, left) for y, left, _, index in frame.tab_hits if index == 1)
        clicks = [click(undo[1], undo[0]), click(agents_tab[1], agents_tab[0]), click(undo[1], undo[0])]
        with patch.object(curses, 'getmouse', side_effect=clicks):
            self.run_ui([curses.KEY_MOUSE] * 3 + ['q', 'q'])
        self.assertTrue(self.session.dirty)
        release = click(undo[1], undo[0], curses.BUTTON1_RELEASED)
        with patch.object(curses, 'getmouse', side_effect=[clicks[0], release, clicks[0]]):
            self.run_ui([curses.KEY_MOUSE] * 3 + ['q'])
        self.assertFalse(self.session.dirty)

    def test_failed_undo_preserves_unsaved_draft(self):
        self.session.set('name_bold', False)
        with patch.object(ui, 'rpc', side_effect=RuntimeError('Server unavailable')):
            frames = self.run_ui(['u', 'u', 'q', 'q'])
        self.assertTrue(self.session.dirty)
        self.assertFalse(self.session.draft['name_bold'])
        self.assertIn('Server unavailable', frames[-2])

    def test_apply_status_clears_after_next_edit_and_failure_keeps_draft(self):
        def save(*args):
            ui.settings.read_settings.return_value = dict(self.session.draft)
        with patch.object(prefs, 'apply_settings', side_effect=save):
            frames = self.run_ui([curses.KEY_RIGHT, 'a', curses.KEY_RIGHT, 'q', 'q'])
        self.assertIn('Applied.', frames[2])
        self.assertIn('Saved', frames[2].splitlines()[0])
        self.assertIn('unsaved changes · A to apply', frames[3].splitlines()[0])
        self.assertNotIn('Applied.', frames[3])
        with patch.object(prefs, 'apply_settings', side_effect=RuntimeError('Apply failed')):
            frames = self.run_ui(['a', 'q', 'q'])
        self.assertIn('Apply failed', frames[1])
        self.assertTrue(self.session.dirty)

    def test_inactive_controls_keep_values_and_explain_dependencies(self):
        for parent, value, child in [('animate', False, 'speed'),
                                     ('animate', False, 'animation'),
                                     ('@row_shows', 'name', '@symbol_set'),
                                     ('@row_shows', 'name', 'animate'),
                                     ('@context', 'off', 'show_capacity'),
                                     ('agent_text', False, 'text_mode'),
                                     ('agent_text', False, '@context'),
                                     ('show_machine', False, 'machine_position')]:
            with self.subTest(child=child):
                self.session.reload()
                self.session.set(parent, value)
                original = self.session.value(child)
                self.session.cycle(ui.find_field(child), 1)
                self.assertEqual(self.session.value(child), original)
                self.assertIn('Inactive:', self.session.message)
        # Collection remains an independent opt-in when the row is hidden.
        self.session.set('collect_details', True)
        self.assertTrue(self.session.draft['collect_details'])

    def test_manual_layout_marks_shortcuts_inactive_and_says_rows_need_updating(self):
        self.session.layout_mode = 'manual'
        self.assertIn('Inactive:', ui.inactive_reason(self.session, 'shortcut_settings'))
        self.assertIn('Inactive:', ui.inactive_reason(self.session, 'shortcut_pin_tab'))
        self.assertEqual(ui.inactive_reason(self.session, 'spaces_gap'), '')
        self.session.set('spaces_gap', 1)
        with patch.object(prefs, 'apply_settings'):
            self.session.apply()
        self.assertIn('Update your rows', self.session.message)
        self.session.layout_mode = 'managed'
        self.assertEqual(ui.inactive_reason(self.session, 'shortcut_settings'), '')

    def test_config_check_timeout_keeps_the_draft(self):
        self.session.set('spaces_gap', 1)
        timeout = subprocess.TimeoutExpired(['herdr', 'config', 'check'], 10)
        with patch.object(prefs, 'apply_settings', side_effect=timeout):
            frames = self.run_ui(['a', 'q', 'q'])
        self.assertEqual(self.session.draft['spaces_gap'], 1)
        self.assertTrue(self.session.dirty)
        self.assertIn('timed out', frames[1])

    def test_line_editor_keys(self):
        edit = ui.apply_edit_key
        self.assertEqual(edit('abc', 3, 'd'), ('abcd', 4))
        self.assertEqual(edit('abc', 1, '\x7f'), ('bc', 0))
        self.assertEqual(edit('abc', 0, curses.KEY_BACKSPACE), ('abc', 0))
        self.assertEqual(edit('abc', 1, curses.KEY_DC), ('ac', 1))
        self.assertEqual(edit('abc', 1, '\x15'), ('', 0))
        self.assertEqual(edit('abc', 1, curses.KEY_END), ('abc', 3))
        self.assertEqual(edit('abc', 2, '\x01'), ('abc', 0))
        self.assertEqual(edit('abc', 3, curses.KEY_RIGHT), ('abc', 3))
        self.assertEqual(edit('x' * 160, 160, 'y'), ('x' * 160, 160))

    def test_disabled_animation_rejects_mouse_selection_and_keyboard_changes(self):
        self.session.set('animate', False)
        frame = self.frame_for(4)
        y, x = next((y, left) for y, left, _, action in frame.hits
                    if action[0] == 'animation' and action[1] == '03')
        with patch.object(curses, 'getmouse', return_value=click(x, y)):
            frames = self.run_ui(['5', curses.KEY_MOUSE, curses.KEY_RIGHT, 'q', 'q'])
        self.assertEqual(self.session.draft['animation'], self.session.base['animation'])
        self.assertIn('Inactive: turn on Animate working agents.', frames[-2])

    def test_all_pages_remain_accessible_at_minimum_size(self):
        for page, section in enumerate(ui.SECTIONS, 1):
            with self.subTest(page=section.name):
                frames = self.run_ui([str(page), 'q'], 24, 48)
                self.assertIn(f'{page} {section.name}', frames[-1])
                self.assertIn(ui.active_fields(self.session, page - 1)[0].label, frames[-1])


if __name__ == '__main__':
    unittest.main()
