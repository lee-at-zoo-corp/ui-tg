#!/usr/bin/env python3
"""Git tree and owned Kakoune lifecycle checks; GUI tests require a test display."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import time
import unittest

REVIEW = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'examples/git-review.py'))


class GitReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ui-tg-review-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/a.txt').write_text('before\n')
        (self.repo / 'removed.txt').write_text('delete me\n')
        self.commit('initial')
        self.initial = self.git('rev-parse', 'HEAD').strip()
        (self.repo / 'src/a.txt').write_text('after\n')
        (self.repo / 'removed.txt').unlink()
        self.special = "src/[x] $;' file.txt"
        (self.repo / self.special).write_text('literal path\n')
        self.commit('changes')
        self.sha = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.DEVNULL)

    def commit(self, subject):
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', subject)

    def test_tree_preserves_file_paths_and_deletion_status(self):
        graph = REVIEW['file_graph'](str(self.repo), self.sha)
        files = {node['data']['path']: node['data']['status'] for node in graph['nodes'] if node['data']['kind'] == 'file'}
        self.assertEqual(files, {'src/a.txt': 'M', self.special: 'A', 'removed.txt': 'D'})
        parents = {target: source for source, target in graph['edges']}
        self.assertEqual(parents['file:' + self.special], 'dir:src')
        self.assertEqual(parents['dir:src'], 'dir:.')
        root_files = REVIEW['changed_files'](str(self.repo), self.initial)
        self.assertEqual(set(root_files), {('A','src/a.txt'),('A','removed.txt')})
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'empty')
        empty = REVIEW['file_graph'](str(self.repo), self.git('rev-parse', 'HEAD').strip())
        self.assertEqual(len(empty['nodes']), 1)
        self.assertEqual(empty['edges'], [])

    @unittest.skipUnless(os.environ.get('UI_TG_GUI_TESTS') == '1', 'requires a dedicated Xephyr display')
    def test_file_visits_reuse_kakoune_and_close_only_the_owned_session(self):
        directory = self.root / 'viewer'
        directory.mkdir()
        viewer = REVIEW['DiffViewer'](directory, str(self.repo), self.sha)
        try:
            viewer.visit({'kind': 'directory', 'path': 'src'})
            self.assertIsNone(viewer.server)
            viewer.visit({'kind': 'file', 'path': 'src/a.txt'})
            server, terminal = viewer.server, viewer.terminal
            first = self.current_file(viewer)
            self.assertIn('+after', first.read_text())
            viewer.visit({'kind': 'file', 'path': self.special})
            second = self.current_file(viewer)
            self.assertNotEqual(first, second)
            self.assertIn('+literal path', second.read_text())
            self.assertNotIn('+after', second.read_text())
            self.assertIs(viewer.server, server)
            self.assertIs(viewer.terminal, terminal)
            # Closing the editor manually must not break subsequent visits.
            REVIEW['stop'](terminal)
            viewer.visit({'kind': 'file', 'path': 'removed.txt'})
            self.assertIs(viewer.server, server)
            self.assertIsNot(viewer.terminal, terminal)
            self.assertIn('-delete me', self.current_file(viewer).read_text())
        finally:
            viewer.close()
        self.assertIsNotNone(viewer.server.poll())
        self.assertIsNotNone(viewer.terminal.poll())
        sessions = subprocess.check_output(['kak', '-l'], text=True).splitlines()
        self.assertNotIn(viewer.session, sessions)

    @unittest.skipUnless(os.environ.get('UI_TG_GUI_TESTS') == '1', 'requires a dedicated Xephyr display')
    def test_closing_the_explorer_closes_its_editor(self):
        for action in ('quit', 'close-window'):
            with self.subTest(action=action):
                directory = self.root / action
                directory.mkdir()
                binary = Path(__file__).resolve().parents[1] / 'target/debug/ui-tg'
                script = Path(__file__).resolve().parents[1] / 'examples/git-review.py'
                (directory / 'config.json').write_text(json.dumps({'repo': str(self.repo), 'sha': self.sha, 'ui_tg': str(binary)}))
                title = 'ui-tg lifecycle ' + action
                with (self.root / (action + '.log')).open('ab') as log:
                    frontend = subprocess.Popen(['zutty','-quiet','-title',title,'-geometry','110x36','-e',
                                                 'python3',str(script),'explore',str(directory)],
                                                stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                try:
                    self.await_condition(lambda: (directory / 'visit.sock').exists())
                    session = (directory / 'session').read_text()
                    window = subprocess.check_output(['xdotool','search','--name','^'+title+'$'],text=True).splitlines()[-1]
                    subprocess.run(['xdotool','key','--window',window,'Tab'],check=True)
                    self.await_condition(lambda: (directory / 'editor-ready').exists())
                    self.assertIn(session, subprocess.check_output(['kak','-l'],text=True).splitlines())
                    if action == 'quit':
                        subprocess.run(['xdotool','keydown','--window',window,'q'],check=True)
                        self.assertEqual(frontend.wait(timeout=5),0)
                    else:
                        frontend.terminate()
                        frontend.wait(timeout=5)
                    self.await_condition(lambda: not directory.exists())
                    self.assertNotIn(session, subprocess.check_output(['kak','-l'],text=True).splitlines())
                finally:
                    REVIEW['stop'](frontend)

    @unittest.skipUnless(os.environ.get('UI_TG_GUI_TESTS') == '1', 'requires a dedicated Xephyr display')
    def test_selecting_another_commit_replaces_the_tree_and_its_editor(self):
        explorer = REVIEW['FileExplorer']()
        binary = Path(__file__).resolve().parents[1] / 'target/debug/ui-tg'
        node = {'repo': str(self.repo), 'sha': self.sha, 'ui_tg': str(binary)}
        try:
            explorer.visit(node)
            previous = explorer.directory
            session = (previous / 'session').read_text()
            window = subprocess.check_output(['xdotool','search','--name','^ui-tg files '+self.sha[:8]+'$'],text=True).splitlines()[-1]
            subprocess.run(['xdotool','key','--window',window,'Tab'],check=True)
            self.await_condition(lambda: (previous / 'editor-ready').exists())
            explorer.visit(dict(node, sha=self.initial))
            self.assertFalse(previous.exists())
            self.assertNotIn(session, subprocess.check_output(['kak','-l'],text=True).splitlines())
            self.assertNotEqual(explorer.directory, previous)
            self.assertEqual(json.loads((explorer.directory / 'config.json').read_text())['sha'], self.initial)
            current = explorer.directory
        finally:
            explorer.close()
        self.assertFalse(current.exists())

    def await_condition(self, condition):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail('timed out waiting for explorer/editor lifecycle')

    def current_file(self, viewer):
        marker = viewer.directory / 'current-buffer'
        marker.unlink(missing_ok=True)
        viewer.pipe('evaluate-commands -client ui-tg-diff ' + REVIEW['kak_quote'](
            'echo -to-file ' + REVIEW['kak_quote'](marker) + ' %val{buffile}'))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if marker.exists():
                return Path(marker.read_text().strip())
            time.sleep(0.02)
        self.fail('Kakoune did not display the visited diff')


if __name__ == '__main__':
    unittest.main()
