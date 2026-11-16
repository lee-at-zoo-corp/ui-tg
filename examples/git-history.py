#!/usr/bin/env python3
"""Build a ui-tg commit graph or open a selected commit's diff in Kakoune."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True)


def build(args):
    repo = Path(args.repo).resolve()
    base = git(repo, 'rev-parse', '--verify', args.base + '^{commit}').strip()
    head = git(repo, 'rev-parse', '--verify', args.head + '^{commit}').strip()
    commits = [base] + git(repo, 'rev-list', '--reverse', '--topo-order', f'{base}..{head}').splitlines()
    included = set(commits)
    nodes, edges = [], []
    for sha in commits:
        subject = git(repo, 'show', '-s', '--format=%s', sha).strip()
        author = git(repo, 'show', '-s', '--format=%an', sha).strip()
        references = []
        if sha == base:
            references.append(args.base)
        if sha == head:
            references.append(args.head)
        nodes.append({'id': sha, 'data': {'sha': sha, 'repo': str(repo), 'subject': subject,
                                        'author': author, 'ref': ', '.join(references)}})
        for parent in git(repo, 'show', '-s', '--format=%P', sha).split():
            if parent in included:
                edges.append([parent, sha])
    json.dump({'nodes': nodes, 'edges': edges}, sys.stdout, indent=2)
    print()


def selected(action):
    node = json.load(sys.stdin)
    if action == 'label':
        print(f"{node['sha'][:8]}  {node['ref']}")
        print(textwrap.fill(node['subject'], width=42, max_lines=5))
        print(node['author'])
        return
    sha = node['sha']
    if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', sha):
        raise ValueError('invalid commit SHA')
    repo = Path(node['repo']).resolve()
    diff = git(repo, 'show', '--format=fuller', '--stat', '--patch', '--first-parent',
               '--no-color', '--no-ext-diff', '--no-textconv', sha, '--')
    # Each activation gets its own file; existing Kakoune buffers stay unchanged.
    with tempfile.NamedTemporaryFile(mode='w', prefix=f'ui-tg-{sha[:8]}-', suffix='.diff',
                                     delete=False) as output:
        output.write(diff)
        path = output.name
    if action == 'diff':
        print(path)
        return
    subprocess.Popen(['zutty', '-quiet', '-title', f'Diff {sha[:8]}', '-e', 'kak', '-ro',
                      '-e', 'set-option buffer filetype diff', path], cwd=repo,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    print(f'Opened {sha[:8]} in Kakoune')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    graph = commands.add_parser('build', help='write a JSON graph, including the base commit')
    graph.add_argument('repo', nargs='?', default='.')
    graph.add_argument('--base', default='main')
    graph.add_argument('--head', default='HEAD')
    for name, help_text in [('label', 'format a commit node'),
                            ('diff', 'write a selected commit diff and print its path'),
                            ('open', 'open a selected commit diff in Zutty and Kakoune')]:
        commands.add_parser(name, help=help_text)
    args = parser.parse_args()
    if args.action == 'build':
        build(args)
    else:
        selected(args.action)


if __name__ == '__main__':
    main()
