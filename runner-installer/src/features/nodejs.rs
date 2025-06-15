use async_trait::async_trait;
use anyhow::Result;
use tracing::{info, debug};
use crate::{features::Feature, package_managers::PackageManager, os::{OsInfo, OsFamily}};

pub struct NodeJs {
    os_info: OsInfo,
}

impl NodeJs {
    pub fn new(os_info: OsInfo) -> Self {
        Self { os_info }
    }
}

#[async_trait]
impl Feature for NodeJs {
    fn name(&self) -> &str {
        "nodejs"
    }

    fn description(&self) -> &str {
        "Node.js JavaScript runtime environment"
    }

    async fn is_installed(&self) -> bool {
        tokio::process::Command::new("node")
            .arg("--version")
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn install(&self, package_manager: &dyn PackageManager) -> Result<()> {
        if self.is_installed().await {
            info!("Node.js is already installed");
            return Ok(());
        }

        info!("Installing Node.js...");

        match &self.os_info.family {
            OsFamily::Linux => {
                match self.os_info.name.as_str() {
                    "ubuntu" | "debian" => {
                        debug!("Installing Node.js on Ubuntu/Debian via NodeSource repository");
                        
                        // Add NodeSource repository
                        let setup_script = tokio::process::Command::new("curl")
                            .args(&["-fsSL", "https://deb.nodesource.com/setup_18.x"])
                            .output()
                            .await?;

                        if !setup_script.status.success() {
                            return Err(anyhow::anyhow!("Failed to download NodeSource setup script"));
                        }

                        // Execute the setup script
                        let status = tokio::process::Command::new("sudo")
                            .arg("bash")
                            .arg("-c")
                            .arg("curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -")
                            .status()
                            .await?;

                        if !status.success() {
                            return Err(anyhow::anyhow!("Failed to setup NodeSource repository"));
                        }

                        // Install Node.js
                        package_manager.install("nodejs").await?;
                    }
                    "alpine" => {
                        debug!("Installing Node.js on Alpine Linux");
                        package_manager.install("nodejs").await?;
                        package_manager.install("npm").await?;
                    }
                    "centos" | "rhel" | "fedora" => {
                        debug!("Installing Node.js on CentOS/RHEL/Fedora via NodeSource repository");
                        
                        // Add NodeSource repository
                        let status = tokio::process::Command::new("sudo")
                            .arg("bash")
                            .arg("-c")
                            .arg("curl -fsSL https://rpm.nodesource.com/setup_18.x | sudo bash -")
                            .status()
                            .await?;

                        if !status.success() {
                            return Err(anyhow::anyhow!("Failed to setup NodeSource repository"));
                        }

                        package_manager.install("nodejs").await?;
                    }
                    _ => {
                        // Fallback: direct binary installation
                        debug!("Installing Node.js via binary download (fallback)");
                        self.install_binary().await?;
                    }
                }
            }
            OsFamily::Windows => {
                debug!("Installing Node.js on Windows via Chocolatey");
                package_manager.install("nodejs").await?;
            }
            OsFamily::MacOs => {
                debug!("Installing Node.js on macOS via Homebrew");
                package_manager.install("node").await?;
            }
            OsFamily::Unknown => {
                return Err(anyhow::anyhow!("Unsupported OS for Node.js installation"));
            }
        }

        Ok(())
    }

    async fn verify(&self) -> Result<()> {
        if !self.is_installed().await {
            return Err(anyhow::anyhow!("Node.js installation verification failed"));
        }

        // Get Node.js version
        let output = tokio::process::Command::new("node")
            .arg("--version")
            .output()
            .await?;

        if output.status.success() {
            let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
            info!("Node.js installed successfully: {}", version);
            
            // Also check npm
            let npm_output = tokio::process::Command::new("npm")
                .arg("--version")
                .output()
                .await;
                
            if let Ok(npm_out) = npm_output {
                if npm_out.status.success() {
                    let npm_version = String::from_utf8_lossy(&npm_out.stdout).trim().to_string();
                    info!("npm available: v{}", npm_version);
                }
            }
            
            Ok(())
        } else {
            Err(anyhow::anyhow!("Node.js verification failed"))
        }
    }
}

impl NodeJs {
    /// Install Node.js via binary download (fallback method)
    async fn install_binary(&self) -> Result<()> {
        let arch = match self.os_info.arch.as_str() {
            "x86_64" => "x64",
            "aarch64" => "arm64",
            "armv7l" => "armv7l",
            _ => return Err(anyhow::anyhow!("Unsupported architecture: {}", self.os_info.arch)),
        };

        let download_url = format!(
            "https://nodejs.org/dist/v18.20.8/node-v18.20.8-linux-{}.tar.xz",
            arch
        );

        debug!("Downloading Node.js binary from: {}", download_url);

        // Download and extract
        let status = tokio::process::Command::new("sudo")
            .arg("bash")
            .arg("-c")
            .arg(format!(
                "curl -fsSL {} | sudo tar -xJ -C /usr/local --strip-components=1",
                download_url
            ))
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!("Failed to install Node.js binary"))
        }
    }
} 