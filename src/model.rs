use anyhow::Result;
use serde::Deserialize;
use serde_json::value::RawValue;

#[derive(Debug, Deserialize)]
pub(crate) struct Node {
    pub id: String,
    pub data: Box<RawValue>,
}
#[derive(Debug, Deserialize)]
pub(crate) struct Graph {
    pub nodes: Vec<Node>,
    #[serde(default)]
    pub edges: Vec<[String; 2]>,
}
impl Graph {
    pub fn parse(text: &str) -> Result<Self> {
        let graph: Self = serde_json::from_str(text)?;
        anyhow::ensure!(
            !graph.nodes.is_empty(),
            "graph must contain at least one node"
        );
        let mut ids = std::collections::HashSet::new();
        for node in &graph.nodes {
            anyhow::ensure!(!node.id.is_empty(), "node IDs cannot be empty");
            anyhow::ensure!(
                ids.insert(node.id.as_str()),
                "duplicate node ID: {}",
                node.id
            );
        }
        for [from, to] in &graph.edges {
            anyhow::ensure!(
                ids.contains(from.as_str()) && ids.contains(to.as_str()),
                "unknown endpoint: {from} -> {to}"
            );
        }
        Ok(graph)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn raw_payload_keeps_original_whitespace_and_number_spelling() {
        let graph =
            Graph::parse(r#"{"nodes":[{"id":"a","data":{ "number": 1.00, "text": "hi" }}]}"#)
                .unwrap();
        assert_eq!(
            graph.nodes[0].data.get(),
            r#"{ "number": 1.00, "text": "hi" }"#
        );
    }
    #[test]
    fn rejects_ambiguous_or_missing_endpoints() {
        for input in [
            r#"{"nodes":[]}"#,
            r#"{"nodes":[{"id":"a","data":null},{"id":"a","data":1}]}"#,
            r#"{"nodes":[{"id":"a","data":null}],"edges":[["a","missing"]]}"#,
        ] {
            assert!(Graph::parse(input).is_err(), "accepted {input}");
        }
    }
    #[test]
    fn accepts_cycles_self_loops_and_disconnected_nodes() {
        let input = r#"{"nodes":[{"id":"a","data":0},{"id":"b","data":1},{"id":"c","data":2}],"edges":[["a","b"],["b","a"],["a","a"]]}"#;
        assert!(Graph::parse(input).is_ok());
    }
}
