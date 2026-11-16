# ui-tg

A colorless Rust terminal navigator for trees and directed graphs. Edges use
Manhattan routing (horizontal and vertical segments). Supports cycles, self-loops,
multiple parents, and disconnected components. Focus uses a double border. Enter or click briefly inverts only the activated node’s interior text and padding
for 200 ms, keeping the border unchanged, using the terminal’s existing foreground and background.

Requires Rust 1.85+, a Unix system with `sh`, and a UTF-8 terminal with mouse support.

```sh
cargo build --release
./target/release/ui-tg examples/tree.json \
  --pretty 'jq -r .name' \
  --on-selected 'jq . > /tmp/ui-tg-selected.json'
```

`jq` is only needed for these example commands. Commands may be any shell pipeline
or executable. To install elsewhere, run `cargo install --path .`.

## Input

Read JSON from a file, or use `-` / omit the path to read stdin:

```sh
cat examples/graph.json | ./target/release/ui-tg \
  --on-selected 'cat > /tmp/ui-tg-selected.json'
```

```json
{
  "nodes": [
    {"id": "root", "data": {"name": "Root", "path": "."}},
    {"id": "child", "data": {"name": "Child", "path": "src"}}
  ],
  "edges": [["root", "child"]]
}
```

Node IDs must be nonempty unique strings. Every edge is `[source_id, target_id]`
and must reference existing nodes. `edges` defaults to an empty list. A tree uses
this same format with parent-to-child edges. Empty graphs are rejected. Node order
determines Tab navigation and ordering within layout columns.

## Commands

`--on-selected 'command'` is required. Enter or a left click activates the focused
node. Arrow keys only move focus. Each activation runs `sh -c 'command'` with the
node's **original `data` JSON bytes** on stdin, including internal whitespace and
number spelling. The node wrapper and formatted label are not sent. No newline is
appended. `UI_TG_NODE_ID` contains the ID. Payload data is never inserted into shell
source. Only configure shell commands you intend to execute.

`--pretty 'command'` optionally runs a filter once per node before opening the TUI.
It receives exactly the same raw data and ID environment variable. UTF-8 stdout
becomes the label. Without a filter, labels are node IDs. Terminal control
characters are removed. Labels show up to eight lines, clipped to the node width;
`--node-width 40` changes the default width of 26 columns. Formatting never changes
the payload sent on activation. Formatter failure aborts with the affected node ID.

Selection commands run in a worker so navigation remains responsive. One command
runs at a time; activations while busy are ignored. The status line shows completion,
stdout, or a failure with stderr. Output is captured, so commands should be
noninteractive and produce finite output. There is no command timeout. Quitting
does not cancel an already started command. A hung formatter can be interrupted
with Ctrl+C before the TUI starts.

## Controls

| Input | Action |
| --- | --- |
| Arrows / h j k l | Focus nearest node in that direction |
| Tab / Shift+Tab | Next / previous node in input order |
| Enter / left click | Run selection command |
| Shift+arrows | Pan viewport |
| Mouse wheel | Scroll viewport |
| Home | Bring focused node into view |
| q / Esc / Ctrl+C | Quit |

The viewport follows keyboard focus and terminal resizing. `--direction north|east|south|west` sets the direction from source to target
(default: `east`). Labels remain upright in every direction. For Git history with
parent-to-child edges, use `--direction north` to place newer commits at the top.

Edges have arrowheads
at their destination. Crossings use line junctions; only arrowheads indicate actual
node connections. Long and backward edges use lanes beside the nodes in vertical layouts, or
beneath the nodes in horizontal layouts.

For a plain diagram without terminal control sequences:

```sh
./target/release/ui-tg examples/graph.json --on-selected cat --print
```

`--print` validates input and runs formatting, but never runs the selection command.

## Browse Git history

The Python example builds a graph from a base reference through a branch tip,
including the base commit. Enter or click opens the selected commit’s patch in a
new Zutty window with a read-only Kakoune buffer. Merge commits show the patch
against their first parent. This example requires Python 3, Git, Zutty, and Kakoune.

From this project directory:

```sh
python3 examples/git-history.py build /path/to/repo --base origin/main > /tmp/history.json
./target/release/ui-tg /tmp/history.json --node-width 46 --direction north \
  --pretty 'python3 examples/git-history.py label' \
  --on-selected 'python3 examples/git-history.py open'
```

Use `--head branch-name` to choose another tip. Generated diff files remain in the
system temporary directory so editor buffers remain usable. The graph contains the
repository’s absolute local path; regenerate it on another machine.

## Development

```sh
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
cargo build
python3 tests/terminal.py
```
