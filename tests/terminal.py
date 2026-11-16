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

        def wait_for_flash(start, activated_at):
            deadline = activated_at + 2
            saw_reverse = False
            while time.monotonic() < deadline:
                # Inspect complete rendered frames, excluding any preceding redraw.
                frames = bytes(transcript[start:]).split(b"\x1b[?2026l")
                for frame in frames[:-1]:
                    if b"\x1b[7m" in frame:
                        saw_reverse = True
                        reversed_cell = False
                        for token in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]|[^\x1b]", frame.decode()):
                            if token == "\x1b[7m":
                                reversed_cell = True
                            elif token in ("\x1b[0m", "\x1b[27m"):
                                reversed_cell = False
                            elif reversed_cell and len(token) == 1:
                                assert token not in "╔═╗║╚╝┌─┐│└┘", "node border inverted during activation"
                    elif saw_reverse:
                        elapsed = time.monotonic() - activated_at
                        assert 0.17 <= elapsed < 1.0, f"flash lasted {elapsed:.3f}s"
                        return
                if select.select([master], [], [], 0.02)[0]:
                    transcript.extend(os.read(master, 65536))
            raise AssertionError("activation must invert the node and restore it after 200ms")

        try:
            wait_for("Ready")
            assert all(f"\x1b[3;{column}H{letter}".encode() in transcript for column, letter in enumerate("ROOT", 5)), "formatter label was not displayed"
            assert not output.exists(), "focusing a node executed a command"
            flash_start, activated_at = len(transcript), time.monotonic()
            os.write(master, b"\r")
            wait_for("Done:")
            wait_for_flash(flash_start, activated_at)
            assert output.read_text().splitlines() == [raw], "activation changed raw JSON"
            # Right arrow focuses child, and does not activate it.
            os.write(master, b"\x1b[C")
            wait_for("[child]")
            assert output.read_text().splitlines() == [raw]
            # SGR mouse left click at the center of the root node.
            flash_start, activated_at = len(transcript), time.monotonic()
            os.write(master, b"\x1b[<0;5;3M\x1b[<0;5;3m")
            wait_for("Done:")
            wait_for_flash(flash_start, activated_at)
            assert output.read_text().splitlines() == [raw, raw]
            # Resize, Tab to child outside the narrow viewport, then activate.
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 24, 0, 0))
            os.kill(pid, signal.SIGWINCH)
            wait_for("\x1b[11;1H")
            os.write(master, b"\t")
            wait_for("[child]")
            os.write(master, b"\r")
            wait_for("Done:")
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
