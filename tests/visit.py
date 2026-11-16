#!/usr/bin/env python3
"""Check visit-triggered selection using real keyboard and mouse events."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import struct
import tempfile
import termios
import time

BINARY = Path(__file__).resolve().parents[1] / 'target/debug/ui-tg'

with tempfile.TemporaryDirectory(prefix='ui-tg-visit-test-') as folder:
    folder = Path(folder)
    output = folder / 'visits'
    graph = folder / 'graph.json'
    graph.write_text(json.dumps({'nodes': [{'id': name, 'data': name} for name in 'abc'],
                                 'edges': [['a', 'b'], ['b', 'c']]}))
    command = f"cat >> {shlex.quote(str(output))}; printf '\\n' >> {shlex.quote(str(output))}; sleep 0.2"
    pid, master = pty.fork()
    if pid == 0:
        os.execv(str(BINARY), [str(BINARY), str(graph), '--on-selected', command, '--select-on-visit'])
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 130, 0, 0))
    transcript = bytearray()

    def wait_for_visits(expected):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            lines = output.read_text().splitlines() if output.exists() else []
            if [json.loads(line) for line in lines] == expected:
                return
            if select.select([master], [], [], 0.02)[0]:
                try:
                    transcript.extend(os.read(master, 65536))
                except OSError:
                    break
        raise AssertionError(f'expected visits {expected}; terminal output: {transcript!r}')

    try:
        wait_for_visits(['a'])  # Initial focus is a visit.
        os.write(master, b'\t\t')  # Change focus while the first callback is busy.
        wait_for_visits(['a', 'c'])  # The latest visit must eventually be delivered.
        # Panning, resize and blocked movement must not retrigger selection.
        os.write(master, b'\x1b[1;2D\x1b[C')
        time.sleep(0.3)
        assert len(output.read_text().splitlines()) == 2
        # Hover must neither trigger a callback nor change keyboard focus.
        os.write(master, b'\x1b[<35;5;2M')
        time.sleep(0.3)
        assert len(output.read_text().splitlines()) == 2, 'hover triggered selection'
        os.write(master, b'\r')
        wait_for_visits(['a', 'c', 'c'])
        # A left click still focuses and activates the node under the pointer.
        os.write(master, b'\x1b[<0;5;2M\x1b[<0;5;2m')
        wait_for_visits(['a', 'c', 'c', 'a'])
        os.write(master, b'q')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                assert os.waitstatus_to_exitcode(status) == 0
                break
            time.sleep(0.02)
        else:
            raise AssertionError('visit mode did not quit')
        print('PASS: initial visit, latest busy visit, no duplicate visits, hover ignored, mouse click')
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(master)
