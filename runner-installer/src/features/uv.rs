use crate::{
    features::Feature,
    os::{OsFamily, OsInfo},
    package_managers::PackageManager,
    utils::run_command,
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
        if run_command("uv", &["--version"])
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
            if run_command(&path, &["--version"])
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
                debug!("Installing uv via standalone installer");
                self.install_via_standalone().await?;
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
        let uv_output = run_command(&uv_cmd, &["--version"]).await?;

        if uv_output.status.success() {
            let uv_version = String::from_utf8_lossy(&uv_output.stdout)
                .trim()
                .to_string();
            info!("uv installed successfully: {}", uv_version);
        }

        // Test basic uv functionality
        let help_output = run_command(&uv_cmd, &["--help"]).await;

        if let Ok(help_out) = help_output {
            if help_out.status.success() {
                info!("uv basic functionality: ✓");
            }
        }

        Ok(())
    }
}

impl Uv {
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
        let curl_available = run_command("curl", &["--version"])
            .await
            .map(|output| output.status.success())
            .unwrap_or(false);

        let install_url = format!("https://astral.sh/uv/{}/install.sh", UV_VERSION);
        let status = if curl_available {
            debug!("Using curl for uv installation");
            let curl_cmd = format!("curl -LsSf {} | sh", install_url);
            run_command("sh", &["-c", &curl_cmd]).await?.status
        } else {
            debug!("Using wget for uv installation");
            let wget_cmd = format!("wget -qO- {} | sh", install_url);
            run_command("sh", &["-c", &wget_cmd]).await?.status
        };

        if !status.success() {
            return Err(anyhow::anyhow!(
                "Failed to install uv via standalone installer"
            ));
        }

        debug!("uv installed successfully via standalone installer");

        // After installation, ensure uv is accessible in PATH
        self.ensure_uv_in_path().await?;

        Ok(())
    }

    /// Ensure uv is accessible in PATH after installation
    async fn ensure_uv_in_path(&self) -> Result<()> {
        let home_dir = std::env::var("HOME").unwrap_or_default();
        let uv_path = format!("{}/.cargo/bin/uv", home_dir);

        // Check if uv was installed to ~/.cargo/bin
        if !std::path::Path::new(&uv_path).exists() {
            debug!("uv not found at expected location: {}", uv_path);
            return Ok(()); // Maybe it was installed elsewhere
        }

        debug!("Found uv at: {}", uv_path);

        // First, try to add ~/.cargo/bin to PATH by updating shell profile
        let bash_profile_updated = self.update_shell_profile(&home_dir).await;
        if bash_profile_updated {
            debug!("Updated shell profile to include ~/.cargo/bin in PATH");
            return Ok(());
        }

        // If profile update didn't work, try to create symlinks in system directories
        // Try to create a symlink in /usr/local/bin if it exists and is writable
        let usr_local_bin = "/usr/local/bin/uv";
        if std::path::Path::new("/usr/local/bin").exists() {
            // Check if we can write to /usr/local/bin (may need sudo)
            let symlink_result = tokio::fs::symlink(&uv_path, usr_local_bin).await;
            match symlink_result {
                Ok(_) => {
                    debug!("Created symlink: {} -> {}", usr_local_bin, uv_path);
                    return Ok(());
                }
                Err(e) => {
                    debug!("Failed to create symlink to /usr/local/bin: {}", e);
                    // Try with sudo if available
                    let sudo_result = run_command("sudo", &["ln", "-sf", &uv_path, usr_local_bin])
                        .await
                        .map(|output| output.status);

                    if let Ok(status) = sudo_result {
                        if status.success() {
                            debug!(
                                "Created symlink with sudo: {} -> {}",
                                usr_local_bin, uv_path
                            );
                            return Ok(());
                        }
                    }
                    debug!("Failed to create symlink even with sudo");
                }
            }
        }

        // Try to create ~/bin directory and symlink there
        let user_bin_dir = format!("{}/bin", home_dir);
        let user_bin_uv = format!("{}/uv", user_bin_dir);

        // Create ~/bin directory if it doesn't exist
        if let Err(e) = tokio::fs::create_dir_all(&user_bin_dir).await {
            debug!("Failed to create ~/bin directory: {}", e);
        } else {
            // Create symlink in ~/bin
            match tokio::fs::symlink(&uv_path, &user_bin_uv).await {
                Ok(_) => {
                    debug!("Created symlink: {} -> {}", user_bin_uv, uv_path);
                    // Also try to add ~/bin to PATH in shell profile
                    self.add_user_bin_to_path(&home_dir).await;
                    return Ok(());
                }
                Err(e) => {
                    debug!("Failed to create symlink in ~/bin: {}", e);
                }
            }
        }

        warn!("Could not create symlink for uv in any PATH directory. uv may not be accessible without using full path: {}", uv_path);
        Ok(())
    }

    /// Update shell profile to include ~/.cargo/bin in PATH
    async fn update_shell_profile(&self, home_dir: &str) -> bool {
        let cargo_env_path = format!("{}/.cargo/env", home_dir);
        let bashrc_path = format!("{}/.bashrc", home_dir);

        // First check if ~/.cargo/env exists (created by rustup)
        if std::path::Path::new(&cargo_env_path).exists() {
            // Add source ~/.cargo/env to .bashrc if not already there
            if let Ok(bashrc_contents) = tokio::fs::read_to_string(&bashrc_path).await {
                if !bashrc_contents.contains("source $HOME/.cargo/env") {
                    let source_line =
                        "\n# Added by runner-installer for cargo/uv\nsource $HOME/.cargo/env\n";
                    if tokio::fs::write(&bashrc_path, format!("{}{}", bashrc_contents, source_line))
                        .await
                        .is_ok()
                    {
                        debug!("Added source ~/.cargo/env to .bashrc");
                        return true;
                    }
                }
            } else {
                // Create .bashrc with cargo env source
                let bashrc_content =
                    "# Added by runner-installer for cargo/uv\nsource $HOME/.cargo/env\n";
                if tokio::fs::write(&bashrc_path, bashrc_content).await.is_ok() {
                    debug!("Created .bashrc with cargo env source");
                    return true;
                }
            }
        }

        false
    }

    /// Add ~/bin to PATH in shell profile
    async fn add_user_bin_to_path(&self, home_dir: &str) {
        let bashrc_path = format!("{}/.bashrc", home_dir);
        let export_line =
            "\n# Added by runner-installer for ~/bin\nexport PATH=\"$HOME/bin:$PATH\"\n";

        if let Ok(bashrc_contents) = tokio::fs::read_to_string(&bashrc_path).await {
            if !bashrc_contents.contains("export PATH=\"$HOME/bin:$PATH\"") {
                let _ =
                    tokio::fs::write(&bashrc_path, format!("{}{}", bashrc_contents, export_line))
                        .await;
                debug!("Added ~/bin to PATH in .bashrc");
            }
        } else {
            let _ = tokio::fs::write(&bashrc_path, export_line).await;
            debug!("Created .bashrc with ~/bin in PATH");
        }
    }

    /// Install via standalone installer for Windows
    async fn install_via_standalone_windows(&self) -> Result<()> {
        debug!("Installing uv via standalone installer on Windows");

        let install_url = format!("https://astral.sh/uv/{}/install.ps1", UV_VERSION);
        let powershell_cmd = format!("irm {} | iex", install_url);

        let status = run_command(
            "powershell",
            &["-ExecutionPolicy", "ByPass", "-c", &powershell_cmd],
        )
        .await?
        .status;

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
