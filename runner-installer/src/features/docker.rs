use async_trait::async_trait;
use anyhow::Result;
use tracing::{info, debug, warn};
use crate::{features::Feature, package_managers::PackageManager, os::{OsInfo, OsFamily}};

pub struct Docker {
    os_info: OsInfo,
}

impl Docker {
    pub fn new(os_info: OsInfo) -> Self {
        Self { os_info }
    }
}

#[async_trait]
impl Feature for Docker {
    fn name(&self) -> &str {
        "docker"
    }

    fn description(&self) -> &str {
        "Docker container runtime and CLI"
    }

    async fn is_installed(&self) -> bool {
        tokio::process::Command::new("docker")
            .arg("--version")
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn install(&self, package_manager: &dyn PackageManager) -> Result<()> {
        if self.is_installed().await {
            info!("Docker is already installed");
            return Ok(());
        }

        info!("Installing Docker...");

        match &self.os_info.family {
            OsFamily::Linux => {
                match self.os_info.name.as_str() {
                    "ubuntu" | "debian" => {
                        debug!("Installing Docker on Ubuntu/Debian via official repository");
                        self.install_docker_ubuntu_debian(package_manager).await?;
                    }
                    "alpine" => {
                        debug!("Installing Docker on Alpine Linux");
                        package_manager.install("docker").await?;
                        package_manager.install("docker-compose").await?;
                        
                        // Start Docker service
                        self.start_docker_service().await?;
                    }
                    "centos" | "rhel" | "fedora" => {
                        debug!("Installing Docker on CentOS/RHEL/Fedora via official repository");
                        self.install_docker_centos_rhel(package_manager).await?;
                    }
                    _ => {
                        // Fallback: use get.docker.com script
                        debug!("Installing Docker via get.docker.com script (fallback)");
                        self.install_docker_script().await?;
                    }
                }
            }
            OsFamily::Windows => {
                debug!("Installing Docker on Windows via Chocolatey");
                package_manager.install("docker-desktop").await?;
            }
            OsFamily::MacOs => {
                debug!("Installing Docker on macOS via Homebrew");
                package_manager.install("docker").await?;
            }
            OsFamily::Unknown => {
                return Err(anyhow::anyhow!("Unsupported OS for Docker installation"));
            }
        }

        // Add current user to docker group (Linux only)
        if matches!(self.os_info.family, OsFamily::Linux) {
            self.add_user_to_docker_group().await?;
        }

        Ok(())
    }

    async fn verify(&self) -> Result<()> {
        if !self.is_installed().await {
            return Err(anyhow::anyhow!("Docker installation verification failed"));
        }

        // Get Docker version
        let output = tokio::process::Command::new("docker")
            .arg("--version")
            .output()
            .await?;

        if output.status.success() {
            let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
            info!("Docker installed successfully: {}", version);
            
            // Test Docker functionality (if daemon is running)
            let hello_world = tokio::process::Command::new("docker")
                .args(&["run", "--rm", "hello-world"])
                .output()
                .await;
                
            match hello_world {
                Ok(output) if output.status.success() => {
                    info!("Docker functionality test: ✓");
                }
                _ => {
                    warn!("Docker installed but daemon may not be running");
                    info!("Note: You may need to start Docker daemon or log out/in for group changes");
                }
            }
            
            Ok(())
        } else {
            Err(anyhow::anyhow!("Docker verification failed"))
        }
    }
}

impl Docker {
    /// Install Docker on Ubuntu/Debian using official repository
    async fn install_docker_ubuntu_debian(&self, package_manager: &dyn PackageManager) -> Result<()> {
        // Update package list
        package_manager.update().await?;
        
        // Install prerequisites
        package_manager.install("ca-certificates").await?;
        package_manager.install("curl").await?;
        package_manager.install("gnupg").await?;
        package_manager.install("lsb-release").await?;

        // Add Docker's official GPG key
        let key_status = tokio::process::Command::new("sudo")
            .arg("bash")
            .arg("-c")
            .arg("curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg")
            .status()
            .await?;

        if !key_status.success() {
            return Err(anyhow::anyhow!("Failed to add Docker GPG key"));
        }

        // Add Docker repository
        let repo_status = tokio::process::Command::new("sudo")
            .arg("bash")
            .arg("-c")
            .arg("echo \"deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable\" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null")
            .status()
            .await?;

        if !repo_status.success() {
            return Err(anyhow::anyhow!("Failed to add Docker repository"));
        }

        // Update package list again
        package_manager.update().await?;

        // Install Docker
        package_manager.install("docker-ce").await?;
        package_manager.install("docker-ce-cli").await?;
        package_manager.install("containerd.io").await?;

        Ok(())
    }

    /// Install Docker on CentOS/RHEL using official repository
    async fn install_docker_centos_rhel(&self, package_manager: &dyn PackageManager) -> Result<()> {
        // Install prerequisites
        package_manager.install("yum-utils").await?;

        // Add Docker repository
        let repo_status = tokio::process::Command::new("sudo")
            .args(&["yum-config-manager", "--add-repo", "https://download.docker.com/linux/centos/docker-ce.repo"])
            .status()
            .await?;

        if !repo_status.success() {
            return Err(anyhow::anyhow!("Failed to add Docker repository"));
        }

        // Install Docker
        package_manager.install("docker-ce").await?;
        package_manager.install("docker-ce-cli").await?;
        package_manager.install("containerd.io").await?;

        // Start and enable Docker service
        self.start_docker_service().await?;

        Ok(())
    }

    /// Install Docker using get.docker.com script (fallback)
    async fn install_docker_script(&self) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .arg("bash")
            .arg("-c")
            .arg("curl -fsSL https://get.docker.com | sudo sh")
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!("Failed to install Docker via script"))
        }
    }

    /// Start Docker service
    async fn start_docker_service(&self) -> Result<()> {
        // Try systemctl first
        let systemctl_status = tokio::process::Command::new("sudo")
            .args(&["systemctl", "start", "docker"])
            .status()
            .await;

        if let Ok(status) = systemctl_status {
            if status.success() {
                debug!("Docker service started via systemctl");
                
                // Enable Docker to start on boot
                let _ = tokio::process::Command::new("sudo")
                    .args(&["systemctl", "enable", "docker"])
                    .status()
                    .await;
                
                return Ok(());
            }
        }

        // Try service command as fallback
        let service_status = tokio::process::Command::new("sudo")
            .args(&["service", "docker", "start"])
            .status()
            .await;

        if let Ok(status) = service_status {
            if status.success() {
                debug!("Docker service started via service command");
                return Ok(());
            }
        }

        warn!("Could not start Docker service automatically");
        Ok(())
    }

    /// Add current user to docker group
    async fn add_user_to_docker_group(&self) -> Result<()> {
        // Get current user
        let user_output = tokio::process::Command::new("whoami")
            .output()
            .await?;

        if !user_output.status.success() {
            return Err(anyhow::anyhow!("Failed to get current user"));
        }

        let username = String::from_utf8_lossy(&user_output.stdout).trim().to_string();

        // Add user to docker group
        let status = tokio::process::Command::new("sudo")
            .args(&["usermod", "-aG", "docker", &username])
            .status()
            .await?;

        if status.success() {
            debug!("Added user {} to docker group", username);
            info!("Note: You may need to log out and back in for group changes to take effect");
            Ok(())
        } else {
            warn!("Failed to add user to docker group");
            Ok(())
        }
    }
} 