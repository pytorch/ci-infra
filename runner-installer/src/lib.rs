//! GitHub Actions Runner Feature Installer
//!
//! A robust, cross-platform feature installation system for GitHub Actions runners.

pub mod config;
pub mod features;
pub mod os;
pub mod package_managers;

use anyhow::Result;
use tracing::{info, warn};

pub use config::InstallerConfig;

/// Main feature installer
pub struct FeatureInstaller {
    config: InstallerConfig,
    package_manager: Box<dyn package_managers::PackageManager>,
    os_info: os::OsInfo,
}

impl FeatureInstaller {
    /// Create a new feature installer
    pub fn new(config: InstallerConfig) -> Result<Self> {
        let os_info = os::detect_os()?;
        let package_manager = package_managers::create_package_manager(&os_info)?;

        info!(
            "Detected OS: {} {} ({})",
            os_info.name, os_info.version, os_info.arch
        );
        info!("Using package manager: {}", package_manager.name());

        Ok(Self {
            config,
            package_manager,
            os_info,
        })
    }

    /// Install the specified features
    pub async fn install_features(&self, feature_names: &[String]) -> Result<()> {
        info!(
            "Installing {} features: {}",
            feature_names.len(),
            feature_names.join(", ")
        );

        for feature_name in feature_names {
            match self.install_single_feature(feature_name).await {
                Ok(()) => info!("✓ Successfully installed: {}", feature_name),
                Err(e) => {
                    warn!("✗ Failed to install {}: {}", feature_name, e);
                    if self.config.fail_fast {
                        return Err(e);
                    }
                }
            }
        }

        Ok(())
    }

    async fn install_single_feature(&self, feature_name: &str) -> Result<()> {
        let feature = features::create_feature(feature_name, &self.os_info)?;
        feature.install(&*self.package_manager).await
    }
}
