use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstallerConfig {
    /// Whether to stop on first failure or continue
    pub fail_fast: bool,
    /// Timeout for package operations in seconds
    pub timeout: u64,
    /// Whether to update package lists before installation
    pub update_packages: bool,
}

impl Default for InstallerConfig {
    fn default() -> Self {
        Self {
            fail_fast: true,
            timeout: 300, // 5 minutes
            update_packages: true,
        }
    }
}

impl InstallerConfig {
    /// Load configuration from file or use defaults
    pub fn load(config_path: Option<&str>) -> Result<Self> {
        match config_path {
            Some(path) => {
                if Path::new(path).exists() {
                    let content = std::fs::read_to_string(path)?;
                    let config: InstallerConfig = serde_yaml::from_str(&content)?;
                    Ok(config)
                } else {
                    tracing::warn!("Config file not found: {}, using defaults", path);
                    Ok(Self::default())
                }
            }
            None => Ok(Self::default()),
        }
    }
} 