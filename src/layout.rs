use crate::model::Graph;
use std::collections::{BTreeMap, HashMap, VecDeque};
use unicode_width::UnicodeWidthChar;

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
pub(crate) enum Direction {
    North,
    East,
    South,
    West,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct Rect {
    pub x: usize,
    pub y: usize,
    pub w: usize,
    pub h: usize,
}
impl Rect {
    pub fn contains(self, x: usize, y: usize) -> bool {
        x >= self.x && x < self.x + self.w && y >= self.y && y < self.y + self.h
    }
    pub fn center(self) -> (usize, usize) {
        (self.x + self.w / 2, self.y + self.h / 2)
    }
}
#[derive(Debug)]
pub(crate) struct Layout {
    pub boxes: Vec<Rect>,
    pub edges: BTreeMap<(usize, usize), char>,
    pub width: usize,
    pub height: usize,
}

impl Layout {
    pub fn new(graph: &Graph, labels: &[String], width: usize, direction: Direction) -> Self {
        let ids: HashMap<_, _> = graph
            .nodes
            .iter()
            .enumerate()
            .map(|(i, n)| (n.id.as_str(), i))
            .collect();
        let mut outgoing = vec![Vec::new(); graph.nodes.len()];
        let mut incoming = vec![0usize; graph.nodes.len()];
        for [a, b] in &graph.edges {
            outgoing[ids[a.as_str()]].push(ids[b.as_str()]);
            incoming[ids[b.as_str()]] += 1;
        }
        // Longest-path ranks preserve parent ordering in DAGs, including merges.
        // Break cycles deterministically when no node with zero indegree remains.
        let mut ranks = vec![0; graph.nodes.len()];
        let mut visited = vec![false; graph.nodes.len()];
        let mut queue: VecDeque<_> = incoming
            .iter()
            .enumerate()
            .filter_map(|(i, &count)| (count == 0).then_some(i))
            .collect();
        loop {
            if queue.is_empty() {
                if let Some(i) = visited.iter().position(|&done| !done) {
                    queue.push_back(i);
                } else {
                    break;
                }
            }
            let i = queue
                .pop_front()
                .expect("queue contains a root or a cycle entry");
            if visited[i] {
                continue;
            }
            visited[i] = true;
            for &j in &outgoing[i] {
                if !visited[j] {
                    ranks[j] = ranks[j].max(ranks[i] + 1);
                    incoming[j] = incoming[j].saturating_sub(1);
                    if incoming[j] == 0 {
                        queue.push_back(j);
                    }
                }
            }
        }
        // Route in a canonical eastward space, exchanging axes for vertical flow.
        let vertical = matches!(direction, Direction::North | Direction::South);
        let heights: Vec<_> = labels
            .iter()
            .map(|label| label.lines().count().clamp(1, 8) + 2)
            .collect();
        let mut layer_sizes = vec![0; graph.nodes.len()];
        for (i, &rank) in ranks.iter().enumerate() {
            layer_sizes[rank] = layer_sizes[rank].max(if vertical { heights[i] } else { width });
        }
        let mut layer_positions = vec![2; graph.nodes.len()];
        for i in 1..layer_positions.len() {
            layer_positions[i] = layer_positions[i - 1] + layer_sizes[i - 1] + 8;
        }
        let mut rows = vec![1; graph.nodes.len()];
        let mut boxes = Vec::new();
        for (i, &h) in heights.iter().enumerate() {
            let rank = ranks[i];
            let (flow_size, cross_size) = if vertical { (h, width) } else { (width, h) };
            boxes.push(Rect {
                x: layer_positions[rank],
                y: rows[rank],
                w: flow_size,
                h: cross_size,
            });
            rows[rank] += cross_size + 3;
        }
        let bottom = boxes.iter().map(|r| r.y + r.h).max().unwrap_or(1) + 2;
        let mut wires = BTreeMap::new();
        let mut arrows = Vec::new();
        for (index, [a, b]) in graph.edges.iter().enumerate() {
            let source = boxes[ids[a.as_str()]];
            let target = boxes[ids[b.as_str()]];
            let sy = source.y + source.h / 2;
            let ty = target.y + target.h / 2;
            let sx = source.x + source.w;
            let tx = target.x - 1;
            let points = if ranks[ids[b.as_str()]] == ranks[ids[a.as_str()]] + 1 {
                vec![(sx, sy), (sx + 2, sy), (sx + 2, ty), (tx, ty)]
            } else {
                let lane = bottom + index * 2;
                vec![
                    (sx, sy),
                    (sx + 2, sy),
                    (sx + 2, lane),
                    (tx - 1, lane),
                    (tx - 1, ty),
                    (tx, ty),
                ]
            };
            for pair in points.windows(2) {
                segment(&mut wires, pair[0], pair[1]);
            }
            arrows.push((tx, ty));
        }
        let flow_extent = boxes.iter().map(|r| r.x + r.w + 3).max().unwrap_or(1);
        let cross_extent = wires
            .keys()
            .map(|p| p.1 + 2)
            .max()
            .unwrap_or(bottom)
            .max(bottom);
        let point = |(x, y): (usize, usize)| match direction {
            Direction::East => (x, y),
            Direction::West => (flow_extent - 1 - x, y),
            Direction::South => (y, x),
            Direction::North => (y, flow_extent - 1 - x),
        };
        let mut edges: BTreeMap<_, _> = wires
            .into_iter()
            .map(|(p, bits)| {
                let transformed = [(1, (0, -1)), (2, (1, 0)), (4, (0, 1)), (8, (-1, 0))]
                    .into_iter()
                    .filter(|(bit, _)| bits & bit != 0)
                    .fold(0, |mask, (_, (dx, dy))| {
                        let (dx, dy) = match direction {
                            Direction::East => (dx, dy),
                            Direction::West => (-dx, dy),
                            Direction::South => (dy, dx),
                            Direction::North => (dy, -dx),
                        };
                        mask | match (dx, dy) {
                            (0, -1) => 1,
                            (1, 0) => 2,
                            (0, 1) => 4,
                            _ => 8,
                        }
                    });
                (point(p), junction(transformed))
            })
            .collect();
        let arrow = match direction {
            Direction::East => '▶',
            Direction::West => '◀',
            Direction::South => '▼',
            Direction::North => '▲',
        };
        for p in arrows {
            edges.insert(point(p), arrow);
        }
        let boxes = boxes
            .into_iter()
            .map(|r| match direction {
                Direction::East => r,
                Direction::West => Rect {
                    x: flow_extent - r.x - r.w,
                    ..r
                },
                Direction::South => Rect {
                    x: r.y,
                    y: r.x,
                    w: r.h,
                    h: r.w,
                },
                Direction::North => Rect {
                    x: r.y,
                    y: flow_extent - r.x - r.w,
                    w: r.h,
                    h: r.w,
                },
            })
            .collect();
        let (width, height) = if vertical {
            (cross_extent, flow_extent)
        } else {
            (flow_extent, cross_extent)
        };
        Self {
            boxes,
            edges,
            width,
            height,
        }
    }
    pub fn hit(&self, x: usize, y: usize) -> Option<usize> {
        self.boxes.iter().position(|r| r.contains(x, y))
    }
    pub fn neighbor(&self, selected: usize, dx: isize, dy: isize) -> usize {
        let (x, y) = self.boxes[selected].center();
        self.boxes
            .iter()
            .enumerate()
            .filter_map(|(i, r)| {
                let (nx, ny) = r.center();
                let vx = nx as isize - x as isize;
                let vy = ny as isize - y as isize;
                let forward = vx * dx + vy * dy;
                let side = (vx * dy - vy * dx).unsigned_abs();
                (forward > 0).then_some((i, forward.unsigned_abs() + side * 3))
            })
            .min_by_key(|&(_, distance)| distance)
            .map_or(selected, |(i, _)| i)
    }
}
fn segment(wires: &mut BTreeMap<(usize, usize), u8>, a: (usize, usize), b: (usize, usize)) {
    let dx = (b.0 as isize - a.0 as isize).signum();
    let dy = (b.1 as isize - a.1 as isize).signum();
    let (forward, back) = match (dx, dy) {
        (1, 0) => (2, 8),
        (-1, 0) => (8, 2),
        (0, 1) => (4, 1),
        (0, -1) => (1, 4),
        _ => return,
    };
    let mut p = a;
    while p != b {
        let next = (p.0.wrapping_add_signed(dx), p.1.wrapping_add_signed(dy));
        *wires.entry(p).or_default() |= forward;
        *wires.entry(next).or_default() |= back;
        p = next;
    }
}
fn junction(bits: u8) -> char {
    match bits {
        1 | 4 | 5 => '│',
        2 | 8 | 10 => '─',
        6 => '┌',
        12 => '┐',
        3 => '└',
        9 => '┘',
        7 => '├',
        13 => '┤',
        14 => '┬',
        11 => '┴',
        _ => '┼',
    }
}
pub(crate) fn clipped(text: &str, limit: usize) -> String {
    let mut used = 0;
    text.chars()
        .take_while(|c| {
            used += c.width().unwrap_or(0);
            used <= limit
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cycles_and_long_edges_do_not_run_through_nodes() {
        let g = Graph::parse(r#"{"nodes":[{"id":"a","data":0},{"id":"b","data":0},{"id":"c","data":0}],"edges":[["a","b"],["b","c"],["c","a"],["a","a"],["a","c"]]}"#).unwrap();
        let l = Layout::new(
            &g,
            &["A".into(), "B\nB".into(), "C".into()],
            16,
            Direction::East,
        );
        for &(x, y) in l.edges.keys() {
            assert!(l.hit(x, y).is_none(), "wire intersects node at {x},{y}");
        }
        for (i, r) in l.boxes.iter().enumerate() {
            assert_eq!(l.hit(r.x, r.y), Some(i));
        }
        assert_eq!(l.neighbor(0, 1, 0), 1);
    }
    #[test]
    fn all_three_node_graphs_keep_wires_outside_boxes() {
        for mask in 0..512 {
            let nodes = (0..3)
                .map(|i| serde_json::json!({"id": i.to_string(), "data": i}))
                .collect::<Vec<_>>();
            let edges = (0..9)
                .filter(|bit| mask & (1 << bit) != 0)
                .map(|bit| [(bit / 3).to_string(), (bit % 3).to_string()])
                .collect::<Vec<_>>();
            let graph = Graph::parse(&serde_json::json!({"nodes":nodes,"edges":edges}).to_string())
                .unwrap();
            for direction in [
                Direction::North,
                Direction::East,
                Direction::South,
                Direction::West,
            ] {
                let layout = Layout::new(
                    &graph,
                    &["A\nB\nC".into(), "B".into(), "C\nD".into()],
                    12,
                    direction,
                );
                for &(x, y) in layout.edges.keys() {
                    assert!(
                        layout.hit(x, y).is_none(),
                        "wire intersects node in graph {mask}, direction {direction:?}, at {x},{y}"
                    );
                    assert!(x < layout.width && y < layout.height, "wire outside canvas");
                }
                for (i, rect) in layout.boxes.iter().enumerate() {
                    assert_eq!(layout.hit(rect.x, rect.y), Some(i));
                }
            }
        }
    }
    #[test]
    fn flow_direction_moves_children_without_rotating_their_labels() {
        let graph = Graph::parse(
            r#"{"nodes":[{"id":"old","data":0},{"id":"new","data":1}],"edges":[["old","new"]]}"#,
        )
        .unwrap();
        for (direction, dx, dy, arrow) in [
            (Direction::North, 0, -1, '▲'),
            (Direction::East, 1, 0, '▶'),
            (Direction::South, 0, 1, '▼'),
            (Direction::West, -1, 0, '◀'),
        ] {
            let layout = Layout::new(&graph, &["old\ncommit".into(), "new".into()], 20, direction);
            let old = layout.boxes[0];
            let new = layout.boxes[1];
            assert_eq!((old.w, old.h, new.w, new.h), (8, 2, 5, 1));
            assert_eq!(layout.neighbor(0, dx, dy), 1, "direction {direction:?}");
            assert!(layout.edges.values().any(|&c| c == arrow));
        }
    }
    #[test]
    fn merge_commit_is_north_of_every_parent_even_with_a_shortcut() {
        let graph = Graph::parse(r#"{"nodes":[{"id":"a","data":0},{"id":"b","data":0},{"id":"c","data":0},{"id":"d","data":0}],"edges":[["a","b"],["b","c"],["c","d"],["a","d"]]}"#).unwrap();
        let layout = Layout::new(&graph, &vec!["commit".into(); 4], 20, Direction::North);
        for [from, to] in &graph.edges {
            let source = graph.nodes.iter().position(|n| &n.id == from).unwrap();
            let target = graph.nodes.iter().position(|n| &n.id == to).unwrap();
            assert!(
                layout.boxes[target].y < layout.boxes[source].y,
                "{from} -> {to} must point north"
            );
        }
    }
    #[test]
    fn compact_tree_uses_one_row_per_label_and_side_connections() {
        let graph = Graph::parse(r#"{"nodes":[{"id":"root","data":0},{"id":"dir","data":0},{"id":"a","data":0},{"id":"b","data":0}],"edges":[["root","dir"],["dir","a"],["dir","b"]]}"#).unwrap();
        let layout = Layout::tree(
            &graph,
            &["root".into(), "dir/".into(), "a".into(), "b".into()],
            30,
            Direction::South,
        )
        .unwrap();
        for (i, r) in layout.boxes.iter().enumerate() {
            assert_eq!(r.y, i + 1);
            assert_eq!(r.h, 1);
            assert!(r.w <= 6, "short labels should not reserve 30 columns");
        }
        for &(x, y) in layout.edges.keys() {
            assert!(layout.hit(x, y).is_none());
        }
        assert!(
            Layout::tree(
                &graph,
                &["root".into(), "dir/".into(), "a".into(), "b".into()],
                30,
                Direction::North
            )
            .unwrap()
            .boxes[3]
                .y
                < layout.boxes[3].y
        );
    }
    #[test]
    fn tree_layout_rejects_cycles_and_multiple_parents() {
        for edges in [
            serde_json::json!([["a", "b"], ["b", "a"]]),
            serde_json::json!([["a", "b"], ["c", "b"]]),
        ] {
            let graph = Graph::parse(&serde_json::json!({"nodes":[{"id":"a","data":0},{"id":"b","data":0},{"id":"c","data":0}],"edges":edges}).to_string()).unwrap();
            assert!(
                Layout::tree(
                    &graph,
                    &["a".into(), "b".into(), "c".into()],
                    20,
                    Direction::South
                )
                .is_err()
            );
        }
    }
    #[test]
    fn down_in_an_outline_visits_children_before_the_next_directory() {
        let graph = Graph::parse(r#"{"nodes":[{"id":"r","data":0},{"id":"d","data":0},{"id":"a","data":0},{"id":"b","data":0},{"id":"s","data":0}],"edges":[["r","d"],["r","s"],["d","a"],["d","b"]]}"#).unwrap();
        let layout = Layout::tree(
            &graph,
            &[
                "root".into(),
                "dir".into(),
                "a".into(),
                "b".into(),
                "sibling".into(),
            ],
            20,
            Direction::South,
        )
        .unwrap();
        assert_eq!(layout.neighbor(1, 0, 1), 2);
        assert_eq!(layout.neighbor(2, 0, 1), 3);
        assert_eq!(layout.neighbor(3, 0, 1), 4);
    }
    #[test]
    fn vertical_history_with_different_label_widths_has_a_straight_spine() {
        let graph = Graph::parse(
            r#"{"nodes":[{"id":"old","data":0},{"id":"new","data":0}],"edges":[["old","new"]]}"#,
        )
        .unwrap();
        for direction in [Direction::North, Direction::South] {
            let layout = Layout::new(
                &graph,
                &[
                    "short\nold".into(),
                    "a much longer commit message\nnew".into(),
                ],
                46,
                direction,
            );
            assert_eq!(layout.boxes[0].center().0, layout.boxes[1].center().0);
            assert!(
                layout
                    .edges
                    .values()
                    .all(|&c| c == '│' || c == '▲' || c == '▼'),
                "linear history should not jog sideways"
            );
            assert_eq!(layout.boxes.iter().map(|r| r.y).min(), Some(1));
        }
    }
    #[test]
    fn clipping_uses_terminal_columns() {
        assert_eq!(clipped("a界b", 3), "a界");
    }
}
