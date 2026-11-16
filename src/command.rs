use anyhow::{Context, Result};
use std::{
    io::Write,
    process::{Command, Stdio},
    thread,
};

// Write concurrently with output collection so large payloads cannot deadlock a filter.
pub(crate) fn run(command: &str, id: &str, raw: &str) -> Result<String> {
    let mut child = Command::new("sh")
        .args(["-c", command])
        .env("UI_TG_NODE_ID", id)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("starting command")?;
    let mut stdin = child.stdin.take().context("opening command stdin")?;
    let bytes = raw.as_bytes().to_vec();
    let writer = thread::spawn(move || stdin.write_all(&bytes));
    let output = child.wait_with_output().context("waiting for command")?;
    let written = writer
        .join()
        .map_err(|_| anyhow::anyhow!("command input thread panicked"))?;
    anyhow::ensure!(
        output.status.success(),
        "command {}: {}",
        output.status,
        String::from_utf8_lossy(&output.stderr)
    );
    if let Err(error) = written {
        // A successful command may intentionally consume only part of stdin.
        if error.kind() != std::io::ErrorKind::BrokenPipe {
            return Err(error.into());
        }
    }
    String::from_utf8(output.stdout).context("command stdout must be UTF-8")
}

pub(crate) fn clean(text: &str) -> String {
    text.chars()
        .filter(|c| !c.is_control() || *c == '\n')
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shell_metacharacters_in_data_stay_literal() {
        let raw = r#"{ "value": "$(echo wrong); ' quotes", "n": 1.00 }"#;
        assert_eq!(run("cat", "node", raw).unwrap(), raw);
        assert_eq!(
            run("printf '%s' \"$UI_TG_NODE_ID\"", "a b;$", raw).unwrap(),
            "a b;$"
        );
    }
    #[test]
    fn reports_failed_command_and_removes_terminal_controls() {
        assert!(
            run("echo failure >&2; exit 3", "a", "null")
                .unwrap_err()
                .to_string()
                .contains("failure")
        );
        assert!(!clean("\x1b[31mhi\r\t").contains('\x1b'));
    }
}
