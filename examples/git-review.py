#!/usr/bin/env python3
"""Browse commits, their changed-file trees, and a shared Kakoune diff window."""
import argparse
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import textwrap
import uuid

SCRIPT = Path(__file__).resolve()
HISTORY = runpy.run_path(str(SCRIPT.with_name('git-history.py')))


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], timeout=15)


def hook(action):
    return shlex.join([sys.executable, str(SCRIPT), action])


def kak_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def changed_files(repo, sha):
    parents = git(repo, 'show', '-s', '--format=%P', sha).decode().split()
    options = ['--name-status', '-z', '--no-renames', '--no-ext-diff', '--no-textconv']
    if parents:
        raw = git(repo, 'diff', *options, parents[0], sha, '--')
    else:
        raw = git(repo, 'diff-tree', '--root', '--no-commit-id', '-r', *options, sha, '--')
    fields = raw.decode('utf-8').rstrip('\0').split('\0') if raw else []
    return list(zip(fields[0::2], fields[1::2]))


def file_graph(repo, sha):
    nodes = [{'id': 'dir:.', 'data': {'kind': 'directory', 'path': '.', 'label': f'{sha[:8]} changed files'}}]
    edges = []
    directories = {'.'}
    for status, name in sorted(changed_files(repo, sha), key=lambda pair: pair[1]):
        parts = PurePosixPath(name).parts
        parent = '.'
        for depth in range(1, len(parts)):
            directory = '/'.join(parts[:depth])
            if directory not in directories:
                directories.add(directory)
                nodes.append({'id': f'dir:{directory}', 'data': {'kind': 'directory', 'path': directory, 'label': parts[depth-1] + '/'}})
                edges.append([f'dir:{parent}', f'dir:{directory}'])
            parent = directory
        nodes.append({'id': f'file:{name}', 'data': {'kind': 'file', 'path': name, 'status': status, 'label': f'{status} {parts[-1]}'}})
        edges.append([f'dir:{parent}', f'file:{name}'])
    return {'nodes': nodes, 'edges': edges}


class DiffViewer:
    """Own just this explorer's Kakoune server and terminal."""
    def __init__(self, directory, repo, sha):
        self.directory, self.repo, self.sha = Path(directory), repo, sha
        self.session = 'ui-tg-' + uuid.uuid4().hex
        self.server = self.terminal = None
        self.log = (self.directory / 'editor.log').open('ab')

    def pipe(self, command):
        subprocess.run(['kak', '-p', self.session], input=command + '\n', text=True,
                       stdout=self.log, stderr=self.log, check=True, timeout=5)

    def wait_until(self, ready, description, process):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if ready():
                return
            if process.poll() is not None:
                raise RuntimeError(f'{description} exited; see {self.directory / "editor.log"}')
            time.sleep(0.02)
        raise RuntimeError(f'timed out waiting for {description}')

    def visit(self, node):
        if node['kind'] != 'file':
            return 'Directory: ' + node['path']
        name = node['path']
        diff = git(self.repo, 'show', '--format=fuller', '--stat', '--patch', '--first-parent',
                   '--no-renames', '--no-color', '--no-ext-diff', '--no-textconv', self.sha,
                   '--', ':(literal)' + name)
        path = self.directory / (hashlib.sha256(name.encode()).hexdigest() + '.diff')
        path.write_bytes(diff)
        if self.server is None or self.server.poll() is not None:
            self.server = subprocess.Popen(['kak', '-d', '-s', self.session], stdin=subprocess.DEVNULL,
                                           stdout=self.log, stderr=self.log)
            self.wait_until(lambda: self.session in subprocess.check_output(['kak', '-l'], text=True, timeout=2).splitlines(),
                            'Kakoune server', self.server)
        edit = f'edit {kak_quote(path)}; set-option buffer filetype diff; set-option buffer readonly true; select 1.1,1.1'
        if self.terminal is None or self.terminal.poll() is not None:
            ready = self.directory / 'editor-ready'
            ready.unlink(missing_ok=True)
            command = f'rename-client ui-tg-diff; {edit}; echo -to-file {kak_quote(ready)} ready'
            active = None
            if shutil.which('xdotool'):
                focused = subprocess.run(['xdotool', 'getactivewindow'], capture_output=True, text=True, timeout=2)
                if focused.returncode == 0:
                    active = focused.stdout.strip()
            self.terminal = subprocess.Popen(['zutty', '-quiet', '-title', f'ui-tg diff {self.sha[:8]}',
                                             '-e', 'kak', '-c', self.session, '-e', command], cwd=self.repo,
                                            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
            self.wait_until(ready.exists, 'Kakoune client', self.terminal)
            if active:
                subprocess.run(['xdotool', 'windowactivate', active], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2)
        else:
            self.pipe('evaluate-commands -client ui-tg-diff ' + kak_quote(edit))
        return 'Diff: ' + name

    def close(self):
        if self.server is not None and self.server.poll() is None:
            try:
                self.pipe('kill!')
                self.server.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                stop(self.server)
        stop(self.terminal)
        self.log.close()


class VisitServer:
    def __init__(self, directory, on_visit):
        self.path = Path(directory) / 'visit.sock'
        self.on_visit = on_visit
        self.stopping = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.path))
        self.socket.listen(4)
        self.socket.settimeout(0.1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(2)
                try:
                    request = bytearray()
                    while b'\n' not in request:
                        chunk = connection.recv(65536)
                        if not chunk:
                            raise ValueError('incomplete visit request')
                        request.extend(chunk)
                        if len(request) > 1024 * 1024:
                            raise ValueError('visit request too large')
                    message = self.on_visit(json.loads(request))
                    response = {'ok': True, 'message': message}
                except Exception as error:
                    response = {'ok': False, 'message': str(error)}
                try:
                    connection.sendall(json.dumps(response).encode() + b'\n')
                except OSError:
                    pass

    def close(self):
        self.stopping.set()
        self.thread.join()
        self.socket.close()
        self.path.unlink(missing_ok=True)


def send_selection(socket_variable):
    node = json.load(sys.stdin)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(25)
        client.connect(os.environ[socket_variable])
        client.sendall(json.dumps(node).encode() + b'\n')
        with client.makefile('r') as response:
            result = json.loads(response.readline())
    if not result['ok']:
        raise RuntimeError(result['message'])
    print(result['message'])


def explore(directory):
    directory = Path(directory)
    config = json.loads((directory / 'config.json').read_text())
    graph = directory / 'files.json'
    graph.write_text(json.dumps(file_graph(config['repo'], config['sha'])))
    viewer = DiffViewer(directory, config['repo'], config['sha'])
    (directory / 'session').write_text(viewer.session)
    server = VisitServer(directory, viewer.visit)
    process = None
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        env = dict(os.environ, UI_TG_REVIEW_SOCKET=str(server.path))
        process = subprocess.Popen([config['ui_tg'], str(graph), '--layout', 'tree', '--direction', 'south',
                                    '--node-width', '34', '--select-on-visit',
                                    '--pretty', hook('label'), '--on-selected', hook('visit')], env=env)
        return process.wait()
    finally:
        stop(process)
        server.close()
        viewer.close()
        shutil.rmtree(directory)


class FileExplorer:
    """Replace the previous tree only after its editor has been cleaned up."""
    def __init__(self):
        self.terminal = None
        self.directory = None

    def close(self):
        stop(self.terminal)
        self.terminal = None
        if self.directory is not None:
            deadline = time.monotonic() + 8
            while self.directory.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            if self.directory.exists():
                raise RuntimeError('previous file explorer did not finish closing')
            self.directory = None

    def visit(self, node):
        self.close()
        self.directory = Path(tempfile.mkdtemp(prefix='ui-tg-files-'))
        (self.directory / 'config.json').write_text(json.dumps(node))
        try:
            self.terminal = subprocess.Popen(['zutty', '-quiet', '-title', f"ui-tg files {node['sha'][:8]}",
                                             '-geometry', '110x36', '-e', sys.executable, str(SCRIPT),
                                             'explore', str(self.directory)], cwd=node['repo'],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
        except OSError:
            shutil.rmtree(self.directory)
            self.directory = None
            raise
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (self.directory / 'visit.sock').exists():
                return 'Opened changed files for ' + node['sha'][:8]
            if self.terminal.poll() is not None:
                shutil.rmtree(self.directory, ignore_errors=True)
                self.directory = None
                raise RuntimeError('file explorer failed to start')
            time.sleep(0.02)
        # Preserve the owned process so close() can still clean it up.
        raise RuntimeError('file explorer startup timed out')


def branch(args):
    binary = args.ui_tg or shutil.which('ui-tg') or str(SCRIPT.parents[1] / 'target/release/ui-tg')
    binary = str(Path(binary).resolve())
    if not os.access(binary, os.X_OK):
        raise RuntimeError('build ui-tg first with cargo build --release, or pass --ui-tg')
    with tempfile.TemporaryDirectory(prefix='ui-tg-history-') as folder:
        graph = Path(folder) / 'history.json'
        with graph.open('w') as output, redirect_stdout(output):
            HISTORY['build'](args)
        data = json.loads(graph.read_text())
        for node in data['nodes']:
            node['data']['ui_tg'] = binary
        graph.write_text(json.dumps(data))
        explorer = FileExplorer()
        server = VisitServer(folder, explorer.visit)
        process = None
        def interrupted(signum, _frame):
            raise SystemExit(128 + signum)
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, interrupted)
        try:
            env = dict(os.environ, UI_TG_BRANCH_SOCKET=str(server.path))
            process = subprocess.Popen([binary, str(graph), '--direction', 'north', '--node-width', '46',
                                        '--pretty', hook('label'), '--on-selected', hook('commit')], env=env)
            return process.wait()
        finally:
            stop(process)
            server.close()
            explorer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    history = commands.add_parser('branch', help='browse a branch, its changed files, and their diffs')
    history.add_argument('repo', nargs='?', default='.')
    history.add_argument('--base', default='main')
    history.add_argument('--head', default='HEAD')
    history.add_argument('--ui-tg')
    explorer = commands.add_parser('explore', help=argparse.SUPPRESS)
    explorer.add_argument('directory')
    for action in ('commit', 'label', 'visit'):
        commands.add_parser(action, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.action == 'branch':
        return branch(args)
    if args.action == 'commit':
        send_selection('UI_TG_BRANCH_SOCKET')
    elif args.action == 'explore':
        return explore(args.directory)
    elif args.action == 'visit':
        send_selection('UI_TG_REVIEW_SOCKET')
    else:
        node = json.load(sys.stdin)
        if 'kind' in node:
            print(node['label'])
        else:
            print(f"{node['sha'][:8]}  {node['ref']}")
            print('\n'.join(textwrap.wrap(node['subject'], width=42, max_lines=5)))
            print(node['author'])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        print(f'git-review: {error}', file=sys.stderr)
        sys.exit(1)
