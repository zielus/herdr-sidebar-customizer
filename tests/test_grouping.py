import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import sidebar_settings as prefs
import sidebar_state as state
import settings_ui as ui
from sidebar_state import INDENT, shade_marker

AGENTS = [
    {'pane_id': 'p1', 'workspace_id': 'w1', 'agent_status': 'idle', 'agent': 'claude', 'name': 'lead',
     'tokens': {'agent_info': 'Opus 5.5 · high'}},
    {'pane_id': 'p3', 'workspace_id': 'w2', 'agent_status': 'idle', 'agent': 'codex'},
    {'pane_id': 'p2', 'workspace_id': 'w1', 'agent_status': 'idle', 'agent': 'codex', 'name': 'advisor'},
]
SPACES = [{'workspace_id': 'w1', 'label': 'boxbox', 'agent_status': 'idle'},
          {'workspace_id': 'w2', 'label': 'Projects', 'agent_status': 'idle'}]
PALETTE = {'w1': 'red_light', 'w2': 'blue_dark'}
IDLE = prefs.DEFAULTS['idle_symbol']


class GroupingTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.settings_path = Path(root) / 'settings.json'
        self.enterContext(patch.object(prefs, 'preferences_path', return_value=self.settings_path))

    def tick(self, **changes):
        writes = {}
        def rpc(endpoint, method, params=None):
            if method == 'agent.list': return {'agents': AGENTS}
            if method == 'workspace.list': return {'workspaces': SPACES}
            if method == 'pane.report_metadata':
                # Herdr refuses a report that updates more than 16 tokens.
                self.assertLessEqual(len(params['tokens']), 16)
                writes.setdefault(params['pane_id'], {}).update(params['tokens'])
            return {}
        animator = state.Animator('test-socket')
        with patch.object(state, 'rpc', side_effect=rpc), \
                patch.object(state.Animator, 'palette', return_value=PALETTE), \
                patch.object(animator.settings, 'load', return_value=prefs.DEFAULTS | changes):
            animator.tick(0)
        return writes

    def test_positions_follow_space_order_not_list_order(self):
        self.assertEqual(state.group_positions(AGENTS, SPACES),
                         {'p1': (True, False, False), 'p2': (False, True, False), 'p3': (True, True, True)})

    def test_off_keeps_the_ungrouped_rows_and_clears_group_tokens(self):
        writes = self.tick()
        self.assertEqual(writes['p2']['space_red'], IDLE + shade_marker('light') + ' boxbox')
        self.assertTrue(all(v is None for k, v in writes['p1'].items() if not k.startswith('space_')))

    def test_grouped_heading_indent_details_and_gap(self):
        writes = self.tick(group_agents=True, agent_row_shows='agent')
        first, second, last = writes['p1'], writes['p2'], writes['p3']
        self.assertEqual(first['group_red'], 'b' + shade_marker('light') + 'oxbox')
        self.assertIsNone(second['group_red'])
        # The heading is the first agent's own first row, so its name row needs no indent.
        self.assertEqual(first['space_red'], IDLE + shade_marker('light') + ' lead')
        self.assertEqual(second['space_red'], INDENT[0] + shade_marker('light') + INDENT[1:] + IDLE + ' advisor')
        self.assertEqual(first['group_info'], INDENT + 'Opus 5.5 · high')
        self.assertIsNone(second['group_info'])
        self.assertIsNone(first['group_gap'])
        self.assertEqual(second['group_gap'], '⠀')
        # The last group gets no gap; an unnamed agent shows its type.
        self.assertIsNone(last['group_gap'])
        self.assertEqual(last['space_blue'], IDLE + shade_marker('dark') + ' Codex')

    def test_agent_row_can_show_nothing_and_gap_can_be_off(self):
        writes = self.tick(group_agents=True, agent_row_shows='none', groups_gap=0)
        self.assertEqual(writes['p1']['space_red'], IDLE + shade_marker('light'))
        self.assertIsNone(writes['p2']['group_gap'])

    def test_agent_name_ignores_the_spaces_name_switch(self):
        writes = self.tick(agent_row_shows='agent', space_names=False)
        self.assertEqual(writes['p1']['space_red'], IDLE + shade_marker('light') + ' lead')
        self.assertEqual(self.tick(space_names=False)['p1']['space_red'], IDLE + shade_marker('light'))

    def test_grouped_config_adds_heading_details_and_gap_rows(self):
        config = (Path(__file__).parent / 'fixtures/config.toml').read_text()
        settings = prefs.DEFAULTS | {'group_agents': True, 'collect_details': True}
        rows = tomllib.loads(prefs.render_config(config, settings))['ui']['sidebar']['agents']['rows']
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(len(row) <= 16 for row in rows))
        self.assertEqual({t['token'] for t in rows[0]}, {'$group_' + hue for hue, *_ in state_hues()})
        self.assertEqual(rows[2][0]['token'], '$group_info')
        self.assertEqual(rows[3][0]['token'], '$group_gap')
        ungrouped = tomllib.loads(prefs.render_config(config, settings | {'groups_gap': 0}))
        self.assertEqual(len(ungrouped['ui']['sidebar']['agents']['rows']), 3)
        plain = tomllib.loads(prefs.render_config(config, prefs.DEFAULTS))
        self.assertEqual(plain['ui']['sidebar']['agents']['rows'][1][0]['token'], '$agent_info')

    def test_settings_validate_the_new_choices(self):
        self.assertEqual(prefs.validate({'agent_row_shows': 'agent', 'groups_gap': 0})['groups_gap'], 0)
        with self.assertRaises(ValueError):
            prefs.validate({'agent_row_shows': 'tab'})
        with self.assertRaises(ValueError):
            prefs.validate({'group_agents': 'yes'})

    def test_preview_groups_each_sample_under_its_space(self):
        session = type('S', (), {})()
        session.draft = prefs.DEFAULTS | {'group_agents': True, 'agent_row_shows': 'agent'}
        session.spaces, session.wid = [], None
        session.value = lambda key: 'red_light'
        rows = ui.preview_rows(session, 'agents', 0)
        texts = [''.join(text for text, *_ in row) for row in rows]
        self.assertEqual(texts[0], 'Website')
        self.assertTrue(texts[1].startswith(ui.GROUP_INDENT) and 'builder' in texts[1])
        self.assertIn([], rows)
        self.assertLessEqual(len(rows) + 6 + 2, ui.PREVIEW_HEIGHT)
        self.assertEqual(ui.inactive_reason(session, 'groups_gap'), '')
        session.draft = session.draft | {'group_agents': False}
        self.assertTrue(ui.inactive_reason(session, 'groups_gap'))


def state_hues():
    from space_colors import HUES
    return HUES


if __name__ == '__main__':
    unittest.main()
