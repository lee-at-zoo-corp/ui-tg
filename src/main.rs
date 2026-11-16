mod command;
mod layout;
mod model;
mod ui;

use anyhow::{Context, Result};
use clap::Parser;
use std::{
    io::{self, IsTerminal, Read},
    path::PathBuf,
};

#[derive(Debug, Parser)]
#[command(name = "ui-tg", version, about = "Colorless terminal trees and graphs", styles = clap::builder::Styles::plain())]
struct Args {
    /// JSON graph file; use - or omit to read stdin
    input: Option<PathBuf>,
    /// Shell command receiving the activated node's original data JSON on stdin
    #[arg(long)]
    on_selected: String,
    /// Shell filter receiving data JSON and returning plain-text node labels
    #[arg(long)]
    pretty: Option<String>,
    /// Node width in terminal columns (labels are clipped to fit)
    #[arg(long, default_value_t = 26, value_parser = clap::value_parser!(u16).range(8..=120))]
    node_width: u16,
    /// Graph layers or a compact indented tree (north/south only)
    #[arg(long, value_enum, default_value_t = LayoutKind::Graph)]
    layout: LayoutKind,
    /// Direction from parent/source toward child/target
    #[arg(long, value_enum, default_value_t = layout::Direction::East)]
    direction: layout::Direction,
    /// Run on-selected on initial focus and keyboard visits; mouse selection requires a click
    #[arg(long)]
    select_on_visit: bool,
    /// Validate, format and print a static diagram without starting the TUI
    #[arg(long)]
    print: bool,
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum LayoutKind {
    Graph,
    Tree,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let input = match args.input {
        Some(path) if path.as_os_str() != "-" => {
            std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?
        }
        _ => {
            anyhow::ensure!(
                !io::stdin().is_terminal(),
                "provide a graph file or pipe JSON on stdin; see --help"
            );
            let mut text = String::new();
            io::stdin().read_to_string(&mut text)?;
            text
        }
    };
    let graph = model::Graph::parse(&input).context("invalid graph")?;
    let labels: Vec<String> = graph
        .nodes
        .iter()
        .map(|node| {
            let text = match &args.pretty {
                Some(cmd) => command::run(cmd, &node.id, node.data.get())
                    .with_context(|| format!("formatting node {}", node.id))?,
                None => node.id.clone(),
            };
            let text = command::clean(text.trim_end());
            Ok(if text.is_empty() {
                node.id.clone()
            } else {
                text
            })
        })
        .collect::<Result<_>>()?;
    let layout = match args.layout {
        LayoutKind::Graph => layout::Layout::new(
            &graph,
            &labels,
            usize::from(args.node_width),
            args.direction,
        ),
        LayoutKind::Tree => layout::Layout::tree(
            &graph,
            &labels,
            usize::from(args.node_width),
            args.direction,
        )?,
    };
    if args.print {
        ui::print(&layout, &labels)?;
    } else {
        anyhow::ensure!(
            io::stdout().is_terminal(),
            "TUI output needs a terminal; use --print for redirected output"
        );
        let mode = if args.select_on_visit {
            ui::SelectionMode::Visit
        } else {
            ui::SelectionMode::Explicit
        };
        ui::run(&graph, &labels, &layout, &args.on_selected, mode)?;
    }
    Ok(())
}
