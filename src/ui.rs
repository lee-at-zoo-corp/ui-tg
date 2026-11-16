use crate::{
    command,
    layout::{Layout, clipped},
    model::Graph,
};
use anyhow::Result;
use crossterm::{
    cursor::{Hide, MoveTo, Show},
    event::{
        self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEventKind, KeyModifiers,
        MouseButton, MouseEventKind,
    },
    execute, queue,
    style::{Attribute, Print, SetAttribute},
    terminal::{
        self, BeginSynchronizedUpdate, Clear, ClearType, EndSynchronizedUpdate,
        EnterAlternateScreen, LeaveAlternateScreen,
    },
};
use std::{
    collections::BTreeMap,
    io::{self, Write},
    sync::mpsc,
    thread,
    time::Duration,
};
use unicode_width::UnicodeWidthChar;

struct Session;
impl Session {
    fn start() -> Result<Self> {
        terminal::enable_raw_mode()?;
        let guard = Self;
        execute!(io::stdout(), EnterAlternateScreen, EnableMouseCapture, Hide)?;
        Ok(guard)
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        let _ = execute!(
            io::stdout(),
            SetAttribute(Attribute::Reset),
            Show,
            DisableMouseCapture,
            LeaveAlternateScreen
        );
        let _ = terminal::disable_raw_mode();
    }
}

type Canvas = BTreeMap<(usize, usize), char>;
fn canvas(layout: &Layout, labels: &[String]) -> Canvas {
    let mut cells = layout.edges.clone();
    for (i, r) in layout.boxes.iter().enumerate() {
        for y in r.y..r.y + r.h {
            for x in r.x..r.x + r.w {
                cells.insert((x, y), ' ');
            }
        }
        for (row, line) in labels[i].lines().take(r.h).enumerate() {
            let mut x = r.x + 1;
            for c in clipped(line, r.w.saturating_sub(2)).chars() {
                let width = c.width().unwrap_or(0);
                if width > 0 {
                    cells.insert((x, r.y + row), c);
                    for continuation in x + 1..x + width {
                        cells.remove(&(continuation, r.y + row));
                    }
                    x += width;
                }
            }
        }
    }
    cells
}

pub(crate) fn print(layout: &Layout, labels: &[String]) -> Result<()> {
    let cells = canvas(layout, labels);
    let mut out = io::BufWriter::new(io::stdout().lock());
    for y in 0..layout.height {
        let mut x = 0;
        while x < layout.width {
            let c = cells.get(&(x, y)).copied().unwrap_or(' ');
            write!(out, "{c}")?;
            x += c.width().unwrap_or(1).max(1);
        }
        writeln!(out)?;
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SelectionMode {
    Explicit,
    Visit,
}

pub(crate) fn run(
    graph: &Graph,
    labels: &[String],
    layout: &Layout,
    selected_command: &str,
    mode: SelectionMode,
) -> Result<()> {
    let previous_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = terminal::disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            SetAttribute(Attribute::Reset),
            Show,
            DisableMouseCapture,
            LeaveAlternateScreen
        );
        previous_hook(info);
    }));
    let _session = Session::start()?;
    let (tx, rx) = mpsc::channel::<Result<String>>();
    let mut selected = 0;
    let mut offset = (0usize, 0usize);
    let mut status = String::from("Ready");
    let mut busy = false;
    let mut follow = true;
    let mut dirty = true;
    let mut pending = (mode == SelectionMode::Visit).then_some(selected);
    loop {
        if let Ok(result) = rx.try_recv() {
            busy = false;
            status = match result {
                Ok(output) => format!("Done: {}", command::clean(&output).replace('\n', " | ")),
                Err(error) => format!("Error: {error:#}"),
            };
            dirty = true;
        }
        if let Some(node_index) = if busy { None } else { pending.take() } {
            let node = &graph.nodes[node_index];
            let cmd = selected_command.to_owned();
            let id = node.id.clone();
            let raw = node.data.get().to_owned();
            let tx = tx.clone();
            thread::spawn(move || {
                let _ = tx.send(command::run(&cmd, &id, &raw));
            });
            busy = true;
            status = "Running…".into();
            dirty = true;
        }
        let (width, height) = terminal::size()?;
        let view_h = usize::from(height.saturating_sub(2));
        if follow {
            let r = layout.boxes[selected];
            if r.w >= usize::from(width) || r.x < offset.0 {
                offset.0 = r.x.saturating_sub(1);
            }
            if r.w < usize::from(width) && r.x + r.w > offset.0 + usize::from(width) {
                offset.0 = (r.x + r.w + 1).saturating_sub(usize::from(width));
            }
            if r.h >= view_h || r.y < offset.1 {
                offset.1 = r.y.saturating_sub(1);
            }
            if r.h < view_h && r.y + r.h > offset.1 + view_h {
                offset.1 = (r.y + r.h + 1).saturating_sub(view_h);
            }
            follow = false;
        }
        if dirty {
            let mut out = io::stdout().lock();
            queue!(
                out,
                BeginSynchronizedUpdate,
                SetAttribute(Attribute::Reset),
                Clear(ClearType::All)
            )?;
            let mut reversed = false;
            for ((x, y), c) in canvas(layout, labels) {
                let cw = c.width().unwrap_or(1).max(1);
                if x >= offset.0
                    && y >= offset.1
                    && x + cw <= offset.0 + usize::from(width)
                    && y < offset.1 + view_h
                {
                    let reverse = layout.boxes[selected].contains(x, y);
                    if reverse != reversed {
                        queue!(
                            out,
                            SetAttribute(if reverse {
                                Attribute::Reverse
                            } else {
                                Attribute::NoReverse
                            })
                        )?;
                        reversed = reverse;
                    }
                    queue!(
                        out,
                        MoveTo((x - offset.0) as u16, (y - offset.1) as u16),
                        Print(c)
                    )?;
                }
            }
            queue!(out, SetAttribute(Attribute::Reset))?;
            let help = "Arrows/hjkl focus  Tab next  Enter/click run  Shift+arrows pan  Wheel scroll  Home center  q quit";
            if height >= 2 {
                queue!(
                    out,
                    MoveTo(0, height - 2),
                    Print(clipped(help, usize::from(width)))
                )?;
            }
            if height >= 1 {
                queue!(
                    out,
                    MoveTo(0, height - 1),
                    Print(clipped(
                        &command::clean(&format!("[{}] {}", graph.nodes[selected].id, status))
                            .replace('\n', " "),
                        usize::from(width)
                    ))
                )?;
            }
            queue!(out, EndSynchronizedUpdate)?;
            out.flush()?;
            dirty = false;
        }
        if !event::poll(Duration::from_millis(100))? {
            continue;
        }
        let mut activate = false;
        let previously_selected = selected;
        match event::read()? {
            Event::Key(key) if key.kind != KeyEventKind::Release => {
                if key.code == KeyCode::Char('q')
                    || key.code == KeyCode::Esc
                    || (key.code == KeyCode::Char('c')
                        && key.modifiers.contains(KeyModifiers::CONTROL))
                {
                    break;
                }
                let direction = match key.code {
                    KeyCode::Left | KeyCode::Char('h') => Some((-1, 0)),
                    KeyCode::Right | KeyCode::Char('l') => Some((1, 0)),
                    KeyCode::Up | KeyCode::Char('k') => Some((0, -1)),
                    KeyCode::Down | KeyCode::Char('j') => Some((0, 1)),
                    _ => None,
                };
                if let Some((dx, dy)) = direction {
                    if key.modifiers.contains(KeyModifiers::SHIFT) {
                        offset.0 = offset
                            .0
                            .saturating_add_signed(dx * 4)
                            .min(layout.width.saturating_sub(1));
                        offset.1 = offset
                            .1
                            .saturating_add_signed(dy * 3)
                            .min(layout.height.saturating_sub(1));
                    } else {
                        selected = layout.neighbor(selected, dx, dy);
                        follow = true;
                    }
                }
                match key.code {
                    KeyCode::Tab => {
                        selected = (selected + 1) % graph.nodes.len();
                        follow = true;
                    }
                    KeyCode::BackTab => {
                        selected = (selected + graph.nodes.len() - 1) % graph.nodes.len();
                        follow = true;
                    }
                    KeyCode::Home => follow = true,
                    KeyCode::Enter => activate = true,
                    _ => {}
                }
            }
            Event::Mouse(mouse) => match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) if usize::from(mouse.row) < view_h => {
                    if let Some(i) = layout.hit(
                        usize::from(mouse.column) + offset.0,
                        usize::from(mouse.row) + offset.1,
                    ) {
                        selected = i;
                        activate = true;
                    }
                }
                MouseEventKind::ScrollUp => offset.1 = offset.1.saturating_sub(3),
                MouseEventKind::ScrollDown => {
                    offset.1 = (offset.1 + 3).min(layout.height.saturating_sub(1))
                }
                MouseEventKind::ScrollLeft => offset.0 = offset.0.saturating_sub(4),
                MouseEventKind::ScrollRight => {
                    offset.0 = (offset.0 + 4).min(layout.width.saturating_sub(1))
                }
                _ => {}
            },
            Event::Resize(_, _) => follow = true,
            _ => {}
        }
        activate |= mode == SelectionMode::Visit && selected != previously_selected;
        if activate {
            if busy && mode == SelectionMode::Explicit {
                status = "Command still running".into();
            } else {
                // Coalesce fast visits so the last focused node is never lost.
                pending = Some(selected);
            }
        }
        dirty = true;
    }
    Ok(())
}
