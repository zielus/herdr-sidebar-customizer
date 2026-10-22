import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_info import RpcError
import installation
import sidebar_settings as prefs
import space_rows as rows


class SpaceRowTests(unittest.TestCase):
    def setUp(self):
        self.workspace = {'workspace_id': 'w1'}
        self.reply = {'source': {'source_checkout_path': '/repo'},
                      'worktrees': [{'path': '/repo', 'branch': 'main'}]}

    def padding(self, **changes):
        return rows.git_details('socket', self.workspace, [self.workspace], prefs.DEFAULTS | changes)[0]

    def test_default_migrates_old_preferences_and_off_restores_native_layout(self):
        self.assertEqual(prefs.validate({'animation': '20'})['empty_git_row'], 'blank')
        self.assertEqual(prefs.validate({'keep_empty_git_row': True})['empty_git_row'], 'blank')
        self.assertEqual(prefs.validate({'keep_empty_git_row': False})['empty_git_row'], 'hide')
        self.assertEqual(prefs.validate({'keep_empty_git_row': True, 'empty_git_row': 'placeholder'})['empty_git_row'], 'placeholder')
        with self.assertRaises(ValueError): prefs.validate({'empty_git_row': 'other'})
        with self.assertRaises(ValueError):
            prefs.validate({'keep_empty_git_row': 'true'})
        for enabled in (True, False):
            settings = prefs.DEFAULTS | {'empty_git_row': 'blank' if enabled else 'hide'}
            config = tomllib.loads(prefs.render_config('', settings))['ui']['sidebar']
            self.assertEqual(any(isinstance(token,dict) and token.get('token')=='$'+rows.TOKEN for token in config['spaces']['rows'][1]), enabled)
            self.assertNotIn('$' + rows.TOKEN, str(config['agents']))
            settings |= {'show_branch': False, 'show_git': False}
            config = tomllib.loads(prefs.render_config('', settings))['ui']['sidebar']
            self.assertEqual(len(config['spaces']['rows']), 2 if enabled else 1)

    def test_missing_repo_branch_and_hidden_controls(self):
        with patch.object(rows, 'rpc', side_effect=RpcError('not_git_worktree')):
            self.assertTrue(self.padding())
        with patch.object(rows, 'rpc', return_value=self.reply), patch.object(rows, 'upstream_differs') as upstream:
            self.assertFalse(self.padding())
            upstream.assert_not_called()
        with patch.object(rows, 'rpc') as rpc:
            self.assertTrue(self.padding(show_branch=False, show_git=False))
            rpc.assert_not_called()

    def test_status_only_and_detached_head(self):
        with patch.object(rows, 'rpc', return_value=self.reply), patch.object(rows, 'upstream_differs') as upstream:
            for differs in (True, False):
                upstream.return_value = differs
                self.assertEqual(bool(self.padding(show_branch=False)), not differs)
            self.reply['worktrees'][0]['branch'] = None
            upstream.return_value = False
            self.assertTrue(self.padding())

    def test_grouped_worktree_has_no_native_git_row(self):
        child = {'workspace_id': 'w2', 'worktree': {'is_linked_worktree': True, 'repo_key': 'repo'}}
        parent = {'workspace_id': 'w1', 'worktree': {'is_linked_worktree': False, 'repo_key': 'repo'}}
        with patch.object(rows, 'rpc') as rpc:
            self.assertTrue(rows.git_details('socket', child, [parent, child], prefs.DEFAULTS)[0])
            rpc.assert_not_called()

    def test_an_openrig_team_space_shows_its_seats_branch(self):
        team = {'worktrees': [{'path': '/repo', 'branch': 'master'},
                              {'path': '/repo.worktrees/rig', 'branch': 'rig/main'}]}
        def rpc(endpoint, method, params):
            if params.get('workspace_id'):
                raise RpcError('not_git_worktree')  # the tiles run outside any repository
            if params['cwd'].startswith('/plain'):
                raise RpcError('not_git_worktree')
            return team
        def details(dirs, **changes):
            return rows.git_details('socket', self.workspace, [self.workspace], prefs.DEFAULTS | changes, dirs)
        with patch.object(rows, 'rpc', side_effect=rpc):
            self.assertEqual(details(['/repo.worktrees/rig', '/repo.worktrees/rig/src']), (None, 'rig/main'))
            self.assertEqual(details(['/repo', '/repo.worktrees/rig']), (None, 'master, rig/main'))
            self.assertEqual(details(['/plain']), ('no git repo', None))
            self.assertEqual(details([]), ('no git repo', None))
            self.assertEqual(details(['/repo'], show_branch=False), ('No Git details', None))

    def test_team_branch_shows_without_the_git_icon_and_dirs_are_looked_up_once(self):
        team = {'worktrees': [{'path': '/rig', 'branch': 'rig/main'}]}
        calls = []
        def rpc(endpoint, method, params=None):
            if method == 'workspace.list':
                return {'workspaces': [{'workspace_id': 'w1'}, {'workspace_id': 'w2'}]}
            if method == 'worktree.list':
                calls.append(params)
                if params.get('workspace_id'):
                    raise RpcError('not_git_worktree')
                return team
            return {}
        plain = prefs.DEFAULTS | {'show_git_icon': False, 'empty_git_row': 'hide'}
        writes = []
        with patch.object(rows, 'rpc', side_effect=lambda e, m, p=None: writes.append(p) or rpc(e, m, p)):
            rows.SpaceRows('socket').sync(plain, {'w1': ['/rig'], 'w2': ['/rig']})
        published = [p['tokens'] for p in writes if p and p.get('source') == rows.SOURCE]
        self.assertEqual([t[rows.TEAM_TOKEN] for t in published], ['rig/main', 'rig/main'])
        self.assertEqual(sum(1 for c in calls if c.get('cwd')), 1)
        # A space Herdr resolves itself never gets the team token, so its branch shows once.
        self.assertIsNone(rows.row_tokens(None, plain, 'main')[rows.TEAM_TOKEN])

    def test_native_workspace_match_beats_parent_checkout(self):
        self.reply['worktrees'].append({'path': '/child', 'branch': None, 'open_workspace_id': 'w1'})
        with patch.object(rows, 'rpc', return_value=self.reply), patch.object(rows, 'upstream_differs', return_value=False) as upstream:
            self.assertTrue(self.padding())
            upstream.assert_called_once_with('/child')

    def test_placeholder_labels_and_icon_modes(self):
        with patch.object(rows, 'rpc', side_effect=RpcError('not_git_worktree')):
            self.assertEqual(self.padding(), 'no git repo')
        with patch.object(rows, 'rpc', return_value=self.reply), patch.object(rows, 'upstream_differs', return_value=False):
            self.assertEqual(self.padding(show_branch=False), 'No Git details')
            self.reply['worktrees'][0].update(branch=None, is_detached=True)
            self.assertEqual(self.padding(), 'Detached HEAD')
        self.assertEqual(self.padding(show_branch=False, show_git=False), 'Git details hidden')
        for mode in ('hide','blank','placeholder'):
            for icon in (False,True):
                settings=prefs.DEFAULTS|{'empty_git_row':mode,'show_git_icon':icon}
                missing=rows.row_tokens('no git repo',settings)
                self.assertEqual(missing[rows.TOKEN],{'hide':None,'blank':rows.BLANK,'placeholder':(rows.GIT_ICON+' ' if icon else '')+'no git repo'}[mode])
                self.assertIsNone(missing[rows.ICON_TOKEN])
                present=rows.row_tokens(None,settings)
                self.assertIsNone(present[rows.TOKEN])
                self.assertEqual(present[rows.ICON_TOKEN],rows.GIT_ICON if icon else None)
        settings=prefs.DEFAULTS|{'empty_git_row':'placeholder','show_git_icon':True,'branch_colour':'#abcdef'}
        gitrow=tomllib.loads(prefs.render_config('',settings))['ui']['sidebar']['spaces']['rows'][1]
        self.assertEqual(gitrow[0]['token'],'$'+rows.BRANCH_TOKEN)
        combined=rows.row_tokens(None,settings,'main')
        self.assertEqual(combined[rows.BRANCH_TOKEN],rows.GIT_ICON+' main')
        self.assertIsNone(combined[rows.ICON_TOKEN])
        self.assertEqual(gitrow[-1]['fg'],rows.PLACEHOLDER_COLOUR)
        self.assertFalse(gitrow[-1]['dim'])

    def test_selected_git_icons_validate_migrate_and_render_all_row_types(self):
        self.assertEqual(prefs.validate({})['git_icon'],'41')
        self.assertEqual(set(prefs.GIT_ICONS),{'01','03','04','21','27','41','43','44','45','47','48'})
        with self.assertRaises(ValueError): prefs.validate({'git_icon':'42'})
        for ident,(_,glyph) in prefs.GIT_ICONS.items():
            settings=prefs.validate({'git_icon':ident,'show_git_icon':True,'empty_git_row':'placeholder'})
            self.assertEqual(rows.row_tokens(None,settings,'main')[rows.BRANCH_TOKEN],glyph+' main')
            self.assertEqual(rows.row_tokens(None,settings)[rows.ICON_TOKEN],glyph)
            self.assertEqual(rows.row_tokens('no git repo',settings)[rows.TOKEN],glyph+' no git repo')
            hidden=settings|{'show_git_icon':False}
            self.assertIsNone(rows.row_tokens(None,hidden,'main')[rows.BRANCH_TOKEN])
            self.assertEqual(rows.row_tokens('no git repo',hidden)[rows.TOKEN],'no git repo')

    def test_sync_updates_only_changed_metadata_and_clears_stale_padding_on_error(self):
        spaces = [self.workspace]
        writes = []
        def rpc(endpoint, method, params=None):
            if method == 'workspace.list': return {'workspaces': spaces}
            if method == 'worktree.list': raise RpcError('not_git_worktree')
            writes.append((method, params))
            self.workspace['tokens'] = params['tokens']
            return {}
        worker = rows.SpaceRows('socket')
        with patch.object(rows, 'rpc', side_effect=rpc):
            worker.sync(prefs.DEFAULTS)
            self.assertEqual(writes[0][1]['tokens'], {rows.TOKEN: rows.BLANK, rows.ICON_TOKEN: None, rows.BRANCH_TOKEN: None, rows.TEAM_TOKEN: None})
            self.assertEqual(writes[0][0], 'workspace.report_metadata')
            worker.sync(prefs.DEFAULTS)
            self.assertEqual(len(writes), 1)
            worker.sync(prefs.DEFAULTS | {'empty_git_row': 'hide'})
            self.assertEqual(writes[-1][1]['tokens'], {rows.TOKEN: None, rows.ICON_TOKEN: None, rows.BRANCH_TOKEN: None, rows.TEAM_TOKEN: None})
            self.workspace['tokens'] = {rows.TOKEN: rows.BLANK, rows.ICON_TOKEN: None, rows.BRANCH_TOKEN: None, rows.TEAM_TOKEN: None}
            with patch.object(rows, 'git_details', side_effect=RpcError('permission_denied')):
                with self.assertRaises(RuntimeError): worker.sync(prefs.DEFAULTS)
            self.assertEqual(writes[-1][1]['tokens'], {rows.TOKEN: None, rows.ICON_TOKEN: None, rows.BRANCH_TOKEN: None, rows.TEAM_TOKEN: None})

    def test_restore_clears_placeholder(self):
        writes = []
        def rpc(endpoint, method, params=None):
            if method == 'workspace.list': return {'workspaces': [self.workspace]}
            if method == 'agent.list': return {'agents': []}
            writes.append(params)
            return {}
        with patch.object(installation, 'rpc', side_effect=rpc):
            installation.clear_metadata('socket')
        self.assertIn({'workspace_id': 'w1', 'source': rows.SOURCE, 'tokens': {rows.TOKEN: None, rows.ICON_TOKEN: None, rows.BRANCH_TOKEN: None, rows.TEAM_TOKEN: None}}, writes)

    def test_local_git_comparison_unborn_missing_upstream_equal_ahead_and_detached(self):
        with tempfile.TemporaryDirectory() as root:
            def git(*args):
                return subprocess.run(['git', '-C', root, *args], check=True, capture_output=True, text=True).stdout.strip()
            git('init', '--initial-branch=main')
            self.assertFalse(rows.upstream_differs(root))
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'first')
            self.assertFalse(rows.upstream_differs(root))
            git('branch', 'upstream')
            git('branch', '--set-upstream-to=upstream')
            self.assertFalse(rows.upstream_differs(root))
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'ahead')
            self.assertTrue(rows.upstream_differs(root))
            git('checkout', '--detach')
            self.assertFalse(rows.upstream_differs(root))
