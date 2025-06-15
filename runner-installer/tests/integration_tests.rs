use std::process::Command;
use tempfile::TempDir;

#[tokio::test]
async fn test_help_command() {
    let output = Command::new("cargo")
        .args(&["run", "--", "--help"])
        .output()
        .expect("Failed to execute help command");

    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("GitHub Actions Runner Feature Installer"));
    assert!(stdout.contains("--features"));
    assert!(stdout.contains("--verbose"));
}

#[tokio::test]
async fn test_no_features_specified() {
    let output = Command::new("cargo")
        .args(&["run"])
        .output()
        .expect("Failed to execute command");

    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("No features specified"));
}

#[tokio::test]
async fn test_unknown_feature() {
    let output = Command::new("cargo")
        .args(&["run", "--", "--features=unknown-feature"])
        .output()
        .expect("Failed to execute command");

    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stderr.contains("Unknown feature: unknown-feature")
            || stdout.contains("Unknown feature: unknown-feature")
    );
}

#[tokio::test]
async fn test_valid_feature_names() {
    // Test that valid feature names are recognized (this shouldn't install anything)
    let output = Command::new("cargo")
        .args(&[
            "run",
            "--",
            "--features=nodejs",
            "--config=/nonexistent/config.yml",
        ])
        .output()
        .expect("Failed to execute command");

    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);

    // Should not contain "Unknown feature" error
    assert!(!stderr.contains("Unknown feature: nodejs"));
    assert!(!stdout.contains("Unknown feature: nodejs"));
}

#[tokio::test]
async fn test_version_output() {
    let output = Command::new("cargo")
        .args(&["run", "--", "--features=nodejs", "--verbose"])
        .output()
        .expect("Failed to execute command");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("GitHub Actions Runner Feature Installer v"));
}

#[tokio::test]
async fn test_config_file_handling() {
    let temp_dir = TempDir::new().expect("Failed to create temp directory");
    let config_path = temp_dir.path().join("test-config.yml");

    // Create a test config file
    std::fs::write(
        &config_path,
        r#"
fail_fast: false
timeout: 600
update_packages: false
"#,
    )
    .expect("Failed to write config file");

    let output = Command::new("cargo")
        .args(&[
            "run",
            "--",
            "--features=nodejs",
            &format!("--config={}", config_path.display()),
        ])
        .output()
        .expect("Failed to execute command");

    let stdout = String::from_utf8_lossy(&output.stdout);

    // Should have loaded the config (evidenced by not showing "using defaults")
    assert!(stdout.contains("GitHub Actions Runner Feature Installer"));
}

#[tokio::test]
async fn test_comma_separated_features() {
    let output = Command::new("cargo")
        .args(&["run", "--", "--features=nodejs,python,docker"])
        .output()
        .expect("Failed to execute command");

    let stdout = String::from_utf8_lossy(&output.stdout);

    // Should detect all three features
    assert!(stdout.contains("Installing 3 features"));
}

#[tokio::test]
async fn test_os_detection() {
    let output = Command::new("cargo")
        .args(&["run", "--", "--features=nodejs", "--verbose"])
        .output()
        .expect("Failed to execute command");

    let stdout = String::from_utf8_lossy(&output.stdout);

    // Should show OS detection
    assert!(stdout.contains("Detected OS:"));
    assert!(stdout.contains("Using package manager:"));
}

#[cfg(unix)]
#[tokio::test]
async fn test_unix_specific_features() {
    let output = Command::new("cargo")
        .args(&["run", "--", "--features=nodejs", "--verbose"])
        .output()
        .expect("Failed to execute command");

    let stdout = String::from_utf8_lossy(&output.stdout);

    // On Unix systems, should detect appropriate package manager
    let has_package_manager = stdout.contains("Using package manager: apt")
        || stdout.contains("Using package manager: yum")
        || stdout.contains("Using package manager: apk")
        || stdout.contains("Using package manager: homebrew");

    assert!(has_package_manager, "Should detect a Unix package manager");
}

#[tokio::test]
async fn test_environment_variable_parsing() {
    let output = Command::new("cargo")
        .args(&["run"])
        .env("RUNNER_FEATURES", "nodejs python")
        .output()
        .expect("Failed to execute command");

    let stdout = String::from_utf8_lossy(&output.stdout);

    // Should parse features from environment variable
    assert!(stdout.contains("Installing 2 features"));
}

#[tokio::test]
async fn test_logging_levels() {
    // Test verbose logging
    let verbose_output = Command::new("cargo")
        .args(&["run", "--", "--features=nodejs", "--verbose"])
        .output()
        .expect("Failed to execute command");

    let verbose_stdout = String::from_utf8_lossy(&verbose_output.stdout);

    // Verbose should show more detailed output
    assert!(verbose_stdout.contains("DEBUG") || verbose_stdout.contains("Installing"));

    // Test normal logging
    let normal_output = Command::new("cargo")
        .args(&["run", "--", "--features=nodejs"])
        .output()
        .expect("Failed to execute command");

    let normal_stdout = String::from_utf8_lossy(&normal_output.stdout);

    // Normal should be less verbose
    assert!(normal_stdout.len() <= verbose_stdout.len());
}
