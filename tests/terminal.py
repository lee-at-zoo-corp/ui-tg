#!/usr/bin/env python3
"""Exercise real terminal events with a PTY; run after cargo build."""
import fcntl
import json
import os
import pty
from pathlib import Path
import re
import select
import shlex
import signal
import struct
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/debug/ui-tg"


def exercise(piped):
    with tempfile.TemporaryDirectory(prefix="ui-tg-test-") as folder:
        output = Path(folder) / "selected.jsonl"
        raw = '{ "name": "Root", "number": 1.00, "literal": "$(false)" }'
        graph = '{"nodes":[{"id":"root","data":' + raw + '},{"id":"child","data":{"name":"Child"}}],"edges":[["root","child"]]}'
        source = Path(folder) / "graph.json"
        source.write_text(graph)
        command = f"cat >> {shlex.quote(str(output))}; printf '\\n' >> {shlex.quote(str(output))}"
        formatter = "python3 -c " + shlex.quote('import json,sys; print(json.load(sys.stdin)["name"].upper())')
        args = [str(BINARY), "-" if piped else str(source), "--on-selected", command, "--pretty", formatter]
        if piped:
            read_fd, write_fd = os.pipe()
            os.write(write_fd, graph.encode())
            os.close(write_fd)
        pid, master = pty.fork()
        if pid == 0:
            if piped:
                os.dup2(read_fd, 0)
                os.close(read_fd)
            os.execv(str(BINARY), args)
        if piped:
            os.close(read_fd)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        transcript = bytearray()

        def wait_for(text):
            deadline = time.monotonic() + 5
            start = len(transcript)
            while time.monotonic() < deadline:
                if text.encode() in transcript[start:]:
                    return
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    transcript.extend(data)
            raise AssertionError(f"missing {text!r}: {transcript.decode(errors='replace')}")

        def cells():
            # Read terminal attributes as well as glyphs, to check focus styling.
            screen = {}
            x = y = 0
            reversed_cell = False
            for token in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]|[^\x1b]", transcript.decode()):
                position = re.fullmatch(r"\x1b\[(\d+);(\d+)H", token)
                if position:
                    y, x = (int(part)-1 for part in position.groups())
                elif token == "\x1b[7m":
                    reversed_cell = True
                elif token in ("\x1b[0m", "\x1b[27m"):
                    reversed_cell = False
                elif len(token) == 1:
                    screen[(x,y)] = (token,reversed_cell)
                    x += 1
            return screen

        try:
            wait_for("Ready")
            assert cells()[(3,1)] == ('R',True), 'focused root must use reverse video'
            assert cells()[(13,1)] == ('C',False), 'other nodes must not be inverted'
            assert not any(c in transcript.decode() for c in '╔═╗║╚╝┌┐└┘'), 'node borders were rendered'
            assert not output.exists(), "focusing a node executed a command"
            os.write(master, b"\r")
            wait_for("Done:")
            assert output.read_text().splitlines() == [raw], "activation changed raw JSON"
            # Right arrow focuses child, and does not activate it.
            os.write(master, b"\x1b[C")
            wait_for("[child]")
            assert output.read_text().splitlines() == [raw]
            assert cells()[(3,1)] == ('R',False)
            assert cells()[(13,1)] == ('C',True)
            # SGR mouse left click at the center of the root node.
            os.write(master, b"\x1b[<0;5;2M\x1b[<0;5;2m")
            wait_for("Done:")
            assert output.read_text().splitlines() == [raw, raw]
            # Resize, Tab to child outside the narrow viewport, then activate.
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 12, 0, 0))
            os.kill(pid, signal.SIGWINCH)
            wait_for("\x1b[11;1H")
            os.write(master, b"\t")
            wait_for("[child]")
            os.write(master, b"\r")
            wait_for("[child] Done")
            assert json.loads(output.read_text().splitlines()[-1]) == {"name": "Child"}
            os.write(master, b"q")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                found, status = os.waitpid(pid, os.WNOHANG)
                if found:
                    assert os.waitstatus_to_exitcode(status) == 0
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("quit did not exit")
            attrs = termios.tcgetattr(master)
            assert attrs[3] & termios.ICANON and attrs[3] & termios.ECHO, "terminal was left in raw mode"
            assert all(code in (b"0", b"7", b"27") for code in re.findall(rb"\x1b\[([0-9;]*)m", transcript)), "unexpected color/style sequence emitted"
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(master)


for piped in (False, True):
    exercise(piped)
    print(f"PASS: {'piped stdin' if piped else 'file input'}, keyboard, mouse, raw payload, resize, cleanup, no colors")
