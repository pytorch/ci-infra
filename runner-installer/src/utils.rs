//! Utility functions for command execution with enhanced logging
//!
//! This module provides common utilities for executing system commands with detailed
//! logging that helps with debugging and local reproduction of issues.

use anyhow::Result;
use std::io;
use std::path::PathBuf;
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

/// Download content from a URL to a temporary file
///
/// This function:
/// - Downloads content using reqwest
/// - Creates a temporary file with optional executable permissions
/// - Returns the path to the temporary file
/// - Caller is responsible for cleanup (use `cleanup_file` helper)
///
/// # Arguments
/// * `url` - The URL to download from
/// * `filename` - The filename to use for the temporary file
/// * `executable` - Whether to set executable permissions (Unix only)
///
/// # Returns
/// * `Ok(PathBuf)` - Path to the created temporary file
/// * `Err(anyhow::Error)` - Download or file creation error
pub async fn download_file(url: &str, filename: &str, executable: bool) -> Result<PathBuf> {
    debug!("Downloading content from: {}", url);

    // Download the content using reqwest
    let client = reqwest::Client::new();
    let response = client.get(url).send().await?;

    if !response.status().is_success() {
        return Err(anyhow::anyhow!(
            "Failed to download from {}: HTTP {}",
            url,
            response.status()
        ));
    }

    let content = response.text().await?;

    // Create temporary file path
    let temp_dir = std::env::temp_dir();
    let file_path = temp_dir.join(filename);

    debug!("Writing downloaded content to: {}", file_path.display());

    // Write content to temporary file
    tokio::fs::write(&file_path, content).await?;

    // Set executable permissions on Unix systems
    #[cfg(unix)]
    if executable {
        use std::os::unix::fs::PermissionsExt;
        let mut perms = tokio::fs::metadata(&file_path).await?.permissions();
        perms.set_mode(0o755);
        tokio::fs::set_permissions(&file_path, perms).await?;
        debug!("Set executable permissions on: {}", file_path.display());
    }

    debug!(
        "Successfully created temporary file: {}",
        file_path.display()
    );
    Ok(file_path)
}

/// Clean up a temporary file
///
/// This is a helper function to clean up temporary files created by
/// `download_file`. It logs but doesn't return errors since
/// cleanup failures are typically non-critical.
///
/// # Arguments
/// * `file_path` - Path to the temporary file to remove
pub async fn cleanup_file(file_path: &PathBuf) {
    match tokio::fs::remove_file(file_path).await {
        Ok(_) => debug!("Cleaned up temporary file: {}", file_path.display()),
        Err(e) => debug!(
            "Failed to clean up temporary file {}: {}",
            file_path.display(),
            e
        ),
    }
}
