//! Utility functions for command execution with enhanced logging
//!
//! This module provides common utilities for executing system commands with detailed
//! logging that helps with debugging and local reproduction of issues.

use std::io;
use std::process::Output;
use tracing::debug;

/// Shared logging setup for command execution
struct CommandLogger {
    current_dir: String,
    cmd_string: String,
}

impl CommandLogger {
    fn new(program: &str, args: &[&str]) -> Self {
        let current_dir = std::env::current_dir()
            .map(|d| d.display().to_string())
            .unwrap_or_else(|_| "unknown".to_string());

        let cmd_string = if args.is_empty() {
            program.to_string()
        } else {
            format!("{} {}", program, args.join(" "))
        };

        debug!("Executing command: {}", cmd_string);
        debug!("Working directory: {}", current_dir);

        Self {
            current_dir,
            cmd_string,
        }
    }

    fn log_success_with_output(&self, output: &Output) {
        debug!(
            "Command completed with exit code: {}",
            output.status.code().unwrap_or(-1)
        );
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            if !stderr.trim().is_empty() {
                debug!("Command stderr: {}", stderr.trim());
            }
            debug!(
                "Command failed. To reproduce locally, run: cd {} && {}",
                self.current_dir, self.cmd_string
            );
        }
    }

    fn log_error(&self, error: &io::Error) {
        debug!("Command failed to execute: {}", error);
        debug!(
            "To reproduce locally, run: cd {} && {}",
            self.current_dir, self.cmd_string
        );
    }
}

/// Execute a command with detailed logging for debugging
///
/// This function provides:
/// - Command line logging in copy-pasteable format
/// - Working directory context
/// - Exit code reporting
/// - stderr capture for failed commands
/// - Reproduction instructions for debugging
///
/// Returns the full command output including status, stdout, and stderr.
/// To check if the command succeeded: `result.status.success()`
/// To get exit code: `result.status.code().unwrap_or(-1)`
pub async fn run_command(program: &str, args: &[&str]) -> Result<Output, io::Error> {
    let logger = CommandLogger::new(program, args);

    let result = tokio::process::Command::new(program)
        .args(args)
        .output()
        .await;

    match &result {
        Ok(output) => logger.log_success_with_output(output),
        Err(e) => logger.log_error(e),
    }

    result
}
