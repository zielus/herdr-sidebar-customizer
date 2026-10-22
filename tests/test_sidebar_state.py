import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import sidebar_state as state
import space_colors as colors
import sidebar_settings as prefs

FRAMES = prefs.ANIMATIONS[prefs.DEFAULTS['animation']][1]
STILL = {status: prefs.DEFAULTS[status+'_symbol'] for status in ('idle','blocked','done','unknown')}
FRAME_SECONDS = prefs.frame_interval(prefs.DEFAULTS)

class SidebarStateTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(prefs,'preferences_path',return_value=Path(root)/'settings.json'))

    def test_all_palette_shades_and_states_match_config_rules(self):
        config = Path(__file__).parent / 'fixtures/config.toml'
        # Exercise the actual layout with deterministic preferences, independent
        # of whichever colors/visibility the user last saved in the panel.
        sidebar = tomllib.loads(prefs.render_config(config.read_text(),prefs.DEFAULTS))['ui']['sidebar']
        for section in ('spaces', 'agents'):
            row = sidebar[section]['rows'][0]
            self.assertLessEqual(len(row), 16)
            styles = {t['token']:t for t in row if isinstance(t, dict)}
            for key,_,color in colors.PALETTE:
                for status in (*STILL, 'working'):
                    for n in range(len(FRAMES)):
                        values = state.label_tokens(status,(n+.01)*FRAME_SECONDS,'Example',key)
                        populated = [(k,v) for k,v in values.items() if v is not None]
                        self.assertEqual(len(populated),1)
                        token,value = populated[0]
                        style = styles['$'+token]
                        self.assertLessEqual(len(style['rules']),16)
                        actual = style['fg']
                        for rule in style['rules']:
                            match = (value.startswith(rule['starts_with']) if 'starts_with' in rule
                                     else rule['contains'] in value)
                            if match:
                                actual = rule['fg']; break
                        self.assertEqual(actual,color)
                        visible = value.replace('\u2063','').replace('\u2062','')
                        glyph = FRAMES[n] if status == 'working' else STILL[status]
                        self.assertEqual(visible,glyph+' Example')

    def test_spinner_wraps_and_keeps_name_and_shade_steady(self):
        frames=[state.symbol('working',(n+.01)*FRAME_SECONDS) for n in range(len(FRAMES)+1)]
        self.assertEqual(frames[0],frames[-1])
        self.assertEqual(''.join(frames[:-1]),'⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
        labels=[state.label_tokens('working',(n+.01)*FRAME_SECONDS,'Example','red_light')['space_red']
                for n in range(len(FRAMES))]
        self.assertEqual(len({label[1:] for label in labels}),1)
        self.assertEqual(state.symbol('blocked',0),'⊘')
        self.assertEqual(state.symbol('done',0),'●')

    def test_live_transitions_moves_renames_and_ttl(self):
        agents=[{'pane_id':'p1','workspace_id':'w1','agent_status':'working'},
                {'pane_id':'p2','workspace_id':'w1','agent_status':'idle'}]
        spaces=[{'workspace_id':'w1','label':'One','agent_status':'working'},
                {'workspace_id':'w2','label':'Two','agent_status':'idle'}]
        palette={'w1':'red_light','w2':'blue_dark'}
        writes=[]
        def rpc(endpoint,method,params=None):
            if method=='agent.list':return {'agents':agents}
            if method=='workspace.list':return {'workspaces':spaces}
            writes.append((method,params));return {}
        with patch.object(state,'rpc',side_effect=rpc), patch.object(state.Animator,'palette',return_value=palette):
            animator=state.Animator('test-socket')
            animator.tick(0)
            writes.clear();animator.tick(.21)
            self.assertEqual(len(writes),2)
            agents[0]['agent_status']='done';spaces[0]['agent_status']='done'
            writes.clear();animator.tick(.5)
            self.assertEqual(len(writes),2)
            self.assertTrue(all(p['tokens']['space_red']=='●'+state.shade_marker('light')+' One' for _,p in writes))
            writes.clear();animator.tick(.75);self.assertEqual(writes,[])
            agents[0]['workspace_id']='w2';spaces[0]['label']='Renamed'
            writes.clear();animator.tick(1)
            pane=next(p for _,p in writes if p.get('pane_id')=='p1')
            self.assertIsNone(pane['tokens']['space_red'])
            self.assertEqual(pane['tokens']['space_blue'],'●'+state.shade_marker('dark')+' Two')
            writes.clear();animator.tick(7)
            # Grouping reports separately; these are the name tokens' renewals.
            self.assertEqual(len([p for _,p in writes if p['source']==state.SOURCE]),4)
            self.assertTrue(all('agent_status' not in p and 'ttl_ms' not in p for _,p in writes))
            agents.pop();animator.tick(8)
            self.assertNotIn(('pane','p2'),animator.previous)

    def test_palette_changes_are_reloaded(self):
        from agent_info import atomic_json
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'space-colors.json'
            animator=state.Animator('test',Path(root))
            self.assertEqual(animator.palette(),{})
            atomic_json(path,{'w1':'red_light'})
            self.assertEqual(animator.palette(),{'w1':'red_light'})
            atomic_json(path,{'w1':'blue_dark'})
            self.assertEqual(animator.palette(),{'w1':'blue_dark'})

    def test_run_survives_attribute_error_from_local_data(self):
        class Stop:
            waits = 0

            def is_set(self):
                return self.waits >= 2

            def wait(self, delay):
                self.waits += 1

        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            failure = AttributeError('malformed')
            with patch.object(state.Animator, 'tick', side_effect=failure) as tick:
                state.run('test', directory, Stop())
            self.assertEqual(tick.call_count, 2)
            status = json.loads((directory / 'animation-status.json').read_text())
            self.assertEqual(status['error'], 'AttributeError')

    def test_failed_report_is_retried(self):
        def rpc(endpoint,method,params=None):
            if method=='agent.list':return {'agents':[]}
            if method=='workspace.list':return {'workspaces':[{'workspace_id':'w1','label':'Example','agent_status':'idle'}]}
            raise RuntimeError('report failed')
        animator=state.Animator('test')
        with patch.object(state,'rpc',side_effect=rpc), patch.object(state.Animator,'palette',return_value={'w1':'red_light'}):
            with self.assertRaises(RuntimeError):animator.tick(0)
        self.assertEqual(animator.previous,{})
