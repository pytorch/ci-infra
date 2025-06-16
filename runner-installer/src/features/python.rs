use crate::{
    features::Feature,
    os::{OsFamily, OsInfo},
    package_managers::PackageManager,
    utils::run_command,
};
use anyhow::Result;
use async_trait::async_trait;
use tracing::{debug, info};

pub struct Python {
    os_info: OsInfo,
}

impl Python {
    pub fn new(os_info: OsInfo) -> Self {
        Self { os_info }
    }
}

#[async_trait]
impl Feature for Python {
    fn name(&self) -> &str {
        "python"
    }

    fn description(&self) -> &str {
        "Python programming language with pip and venv"
    }

    async fn is_installed(&self) -> bool {
        // Check for Python 3
        let python3_check = run_command("python3", &["--version"])
            .await
            .map(|output| output.status.success())
            .unwrap_or(false);

        let pip_check = run_command("pip3", &["--version"])
            .await
            .map(|output| output.status.success())
            .unwrap_or(false);

        python3_check && pip_check
    }

    async fn install(&self, package_manager: &dyn PackageManager) -> Result<()> {
        if self.is_installed().await {
            info!("Python is already installed");
            return Ok(());
        }

        info!("Installing Python...");

        match &self.os_info.family {
            OsFamily::Linux => {
                match self.os_info.name.as_str() {
                    "ubuntu" | "debian" => {
                        debug!("Installing Python on Ubuntu/Debian");
                        package_manager.update().await?;
                        package_manager.install("python3").await?;
                        package_manager.install("python3-pip").await?;
                        package_manager.install("python3-venv").await?;
                        package_manager.install("python3-dev").await?;
                    }
                    "alpine" => {
                        debug!("Installing Python on Alpine Linux");
                        package_manager.install("python3").await?;
                        package_manager.install("py3-pip").await?;
                        package_manager.install("python3-dev").await?;
                    }
                    "centos" | "rhel" | "fedora" => {
                        debug!("Installing Python on CentOS/RHEL/Fedora");
                        package_manager.install("python3").await?;
                        package_manager.install("python3-pip").await?;
                        package_manager.install("python3-devel").await?;
                    }
                    _ => {
                        // Fallback: try common package names
                        debug!("Installing Python via fallback method");
                        package_manager.install("python3").await?;
                        package_manager.install("python3-pip").await?;
                    }
                }
            }
            OsFamily::Windows => {
                debug!("Installing Python on Windows via Chocolatey");
                package_manager.install("python").await?;
            }
            OsFamily::MacOs => {
                debug!("Installing Python on macOS via Homebrew");
                package_manager.install("python@3.11").await?;
            }
            OsFamily::Unknown => {
                return Err(anyhow::anyhow!("Unsupported OS for Python installation"));
            }
        }

        // Ensure pip is up to date
        self.upgrade_pip().await?;

        Ok(())
    }

    async fn verify(&self) -> Result<()> {
        if !self.is_installed().await {
            return Err(anyhow::anyhow!("Python installation verification failed"));
        }

        // Get Python version
        let python_output = run_command("python3", &["--version"]).await?;

        if python_output.status.success() {
            let python_version = String::from_utf8_lossy(&python_output.stdout)
                .trim()
                .to_string();
            info!("Python installed successfully: {}", python_version);
        }

        // Get pip version
        let pip_output = run_command("pip3", &["--version"]).await?;

        if pip_output.status.success() {
            let pip_version = String::from_utf8_lossy(&pip_output.stdout)
                .trim()
                .to_string();
            info!(
                "pip available: {}",
                pip_version.split_whitespace().nth(1).unwrap_or("unknown")
            );
        }

        // Test virtual environment creation
        let venv_test = run_command("python3", &["-m", "venv", "--help"]).await;

        if let Ok(venv_out) = venv_test {
            if venv_out.status.success() {
                info!("Virtual environment support: ✓");
            }
        }

        Ok(())
    }
}

impl Python {
    /// Upgrade pip to the latest version
    async fn upgrade_pip(&self) -> Result<()> {
        debug!("Upgrading pip to latest version");

        let output = run_command("python3", &["-m", "pip", "install", "--upgrade", "pip"]).await?;

        if output.status.success() {
            debug!("pip upgraded successfully");
            Ok(())
        } else {
            // Non-fatal error, pip might already be latest
            debug!("pip upgrade failed, but continuing...");
            Ok(())
        }
    }
}
