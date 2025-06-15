use crate::{
    features::Feature,
    os::{OsFamily, OsInfo},
    package_managers::PackageManager,
};
use anyhow::Result;
use async_trait::async_trait;
use tracing::{debug, info, warn};

const UV_VERSION: &str = "0.4.29";

pub struct Uv {
    os_info: OsInfo,
}

impl Uv {
    pub fn new(os_info: OsInfo) -> Self {
        Self { os_info }
    }

    /// Get the path to the uv executable
    fn get_uv_command(&self) -> String {
        // Check common installation locations where standalone installer places uv
        let common_paths = vec![
            format!(
                "{}/.cargo/bin/uv",
                std::env::var("HOME").unwrap_or_default()
            ),
            "/usr/local/bin/uv".to_string(),
            "/usr/bin/uv".to_string(),
        ];

        for path in &common_paths {
            if std::path::Path::new(path).exists() {
                return path.clone();
            }
        }

        // Fallback to just "uv" (assume it's in PATH)
        "uv".to_string()
    }
}

#[async_trait]
impl Feature for Uv {
    fn name(&self) -> &str {
        "uv"
    }

    fn description(&self) -> &str {
        "uv - An extremely fast Python package installer and resolver"
    }

    async fn is_installed(&self) -> bool {
        // First check if uv is in PATH
        if tokio::process::Command::new("uv")
            .arg("--version")
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
        {
            debug!("uv found in PATH");
            return true;
        }

        // Check common installation locations where standalone installer places uv
        let common_paths = vec![
            format!(
                "{}/.cargo/bin/uv",
                std::env::var("HOME").unwrap_or_default()
            ),
            "/usr/local/bin/uv".to_string(),
            "/usr/bin/uv".to_string(),
        ];

        for path in common_paths {
            debug!("Checking for uv at: {}", path);
            if tokio::process::Command::new(&path)
                .arg("--version")
                .output()
                .await
                .map(|output| output.status.success())
                .unwrap_or(false)
            {
                debug!("uv found at: {}", path);
                return true;
            }
        }

        debug!("uv not found in any common paths");
        false
    }

    async fn install(&self, package_manager: &dyn PackageManager) -> Result<()> {
        if self.is_installed().await {
            info!("uv is already installed");
            return Ok(());
        }

        info!("Installing uv...");

        match &self.os_info.family {
            OsFamily::Linux => {
                match self.os_info.name.as_str() {
                    "ubuntu" | "debian" => {
                        debug!("Attempting to install uv on Ubuntu/Debian via package manager");
                        // Try package manager first, fall back to standalone installer
                        if let Err(e) = self.try_package_manager_install(package_manager).await {
                            warn!("Package manager installation failed: {}, falling back to standalone installer", e);
                            self.install_via_standalone().await?;
                        }
                    }
                    "alpine" => {
                        debug!("Installing uv on Alpine Linux via standalone installer");
                        // Alpine likely doesn't have uv in repos, use standalone installer
                        self.install_via_standalone().await?;
                    }
                    "centos" | "rhel" | "fedora" => {
                        debug!("Installing uv on CentOS/RHEL/Fedora via standalone installer");
                        // These distros likely don't have uv in repos, use standalone installer
                        self.install_via_standalone().await?;
                    }
                    _ => {
                        debug!("Installing uv via standalone installer (fallback)");
                        self.install_via_standalone().await?;
                    }
                }
            }
            OsFamily::Windows => {
                debug!("Attempting to install uv on Windows via package manager");
                // Try Chocolatey/Scoop first, fall back to standalone installer
                if let Err(e) = self
                    .try_windows_package_manager_install(package_manager)
                    .await
                {
                    warn!("Package manager installation failed: {}, falling back to standalone installer", e);
                    self.install_via_standalone_windows().await?;
                }
            }
            OsFamily::MacOs => {
                debug!("Installing uv on macOS via Homebrew");
                // Homebrew should have uv available
                let package_spec = format!("uv@{}", UV_VERSION);
                if let Err(e) = package_manager.install(&package_spec).await {
                    warn!(
                        "Homebrew installation failed: {}, falling back to standalone installer",
                        e
                    );
                    self.install_via_standalone().await?;
                }
            }
            OsFamily::Unknown => {
                return Err(anyhow::anyhow!("Unsupported OS for uv installation"));
            }
        }

        Ok(())
    }

    async fn verify(&self) -> Result<()> {
        if !self.is_installed().await {
            return Err(anyhow::anyhow!("uv installation verification failed"));
        }

        let uv_cmd = self.get_uv_command();

        // Get uv version
        let uv_output = tokio::process::Command::new(&uv_cmd)
            .arg("--version")
            .output()
            .await?;

        if uv_output.status.success() {
            let uv_version = String::from_utf8_lossy(&uv_output.stdout)
                .trim()
                .to_string();
            info!("uv installed successfully: {}", uv_version);
        }

        // Test basic uv functionality
        let help_output = tokio::process::Command::new(&uv_cmd)
            .arg("--help")
            .output()
            .await;

        if let Ok(help_out) = help_output {
            if help_out.status.success() {
                info!("uv basic functionality: ✓");
            }
        }

        Ok(())
    }
}

impl Uv {
    /// Try to install via package manager (for Ubuntu/Debian)
    async fn try_package_manager_install(
        &self,
        package_manager: &dyn PackageManager,
    ) -> Result<()> {
        // Most package managers don't have uv yet, but we can try common ones
        package_manager.update().await?;
        // Try to install specific version, may not be supported by all package managers
        let package_spec = format!("uv={}", UV_VERSION);
        package_manager.install(&package_spec).await
    }

    /// Try to install via Windows package managers
    async fn try_windows_package_manager_install(
        &self,
        package_manager: &dyn PackageManager,
    ) -> Result<()> {
        // Try the package manager (could be Chocolatey, Scoop, etc.)
        // For Windows package managers, version specification varies
        let package_spec = format!("uv --version {}", UV_VERSION);
        package_manager.install(&package_spec).await
    }

    /// Install via standalone installer for Unix-like systems
    async fn install_via_standalone(&self) -> Result<()> {
        debug!("Installing uv via standalone installer");

        // Check if curl is available, fallback to wget
        let curl_available = tokio::process::Command::new("curl")
            .arg("--version")
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false);

        let install_url = format!("https://astral.sh/uv/{}/install.sh", UV_VERSION);
        let status = if curl_available {
            debug!("Using curl for uv installation");
            let curl_cmd = format!("curl -LsSf {} | sh", install_url);
            tokio::process::Command::new("sh")
                .arg("-c")
                .arg(&curl_cmd)
                .status()
                .await?
        } else {
            debug!("Using wget for uv installation");
            let wget_cmd = format!("wget -qO- {} | sh", install_url);
            tokio::process::Command::new("sh")
                .arg("-c")
                .arg(&wget_cmd)
                .status()
                .await?
        };

        if status.success() {
            debug!("uv installed successfully via standalone installer");
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Failed to install uv via standalone installer"
            ))
        }
    }

    /// Install via standalone installer for Windows
    async fn install_via_standalone_windows(&self) -> Result<()> {
        debug!("Installing uv via standalone installer on Windows");

        let install_url = format!("https://astral.sh/uv/{}/install.ps1", UV_VERSION);
        let powershell_cmd = format!("irm {} | iex", install_url);

        let status = tokio::process::Command::new("powershell")
            .args(&["-ExecutionPolicy", "ByPass", "-c", &powershell_cmd])
            .status()
            .await?;

        if status.success() {
            debug!("uv installed successfully via standalone installer on Windows");
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Failed to install uv via standalone installer on Windows"
            ))
        }
    }
}
