use crate::os::{OsFamily, OsInfo};
use anyhow::Result;
use async_trait::async_trait;

/// Trait for package managers
#[async_trait]
pub trait PackageManager: Send + Sync {
    /// Package manager name
    fn name(&self) -> &str;

    /// Update package lists
    async fn update(&self) -> Result<()>;

    /// Install a package
    async fn install(&self, package: &str) -> Result<()>;

    /// Check if a package is installed
    async fn is_installed(&self, package: &str) -> bool;

    /// Execute a command with appropriate privileges
    async fn execute_command(&self, command: &str, args: &[&str]) -> Result<()>;
}

/// Create appropriate package manager for the OS
pub fn create_package_manager(os_info: &OsInfo) -> Result<Box<dyn PackageManager>> {
    match &os_info.family {
        OsFamily::Linux => {
            match os_info.name.as_str() {
                "ubuntu" | "debian" => Ok(Box::new(AptPackageManager::new())),
                "centos" | "rhel" | "fedora" => Ok(Box::new(YumPackageManager::new())),
                "alpine" => Ok(Box::new(ApkPackageManager::new())),
                _ => Ok(Box::new(AptPackageManager::new())), // Default to apt
            }
        }
        OsFamily::Windows => Ok(Box::new(ChocolateyPackageManager::new())),
        OsFamily::MacOs => Ok(Box::new(BrewPackageManager::new())),
        OsFamily::Unknown => Err(anyhow::anyhow!("Unknown OS family")),
    }
}

/// APT package manager for Debian/Ubuntu
pub struct AptPackageManager;

impl Default for AptPackageManager {
    fn default() -> Self {
        Self::new()
    }
}

impl AptPackageManager {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl PackageManager for AptPackageManager {
    fn name(&self) -> &str {
        "apt"
    }

    async fn update(&self) -> Result<()> {
        self.execute_command("apt-get", &["update"]).await
    }

    async fn install(&self, package: &str) -> Result<()> {
        self.execute_command("apt-get", &["install", "-y", package])
            .await
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("dpkg")
            .args(["-l", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn execute_command(&self, command: &str, args: &[&str]) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .arg(command)
            .args(args)
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Command failed: {} {}",
                command,
                args.join(" ")
            ))
        }
    }
}

/// YUM package manager for CentOS/RHEL/Fedora
pub struct YumPackageManager;

impl Default for YumPackageManager {
    fn default() -> Self {
        Self::new()
    }
}

impl YumPackageManager {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl PackageManager for YumPackageManager {
    fn name(&self) -> &str {
        "yum"
    }

    async fn update(&self) -> Result<()> {
        self.execute_command("yum", &["update", "-y"]).await
    }

    async fn install(&self, package: &str) -> Result<()> {
        self.execute_command("yum", &["install", "-y", package])
            .await
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("rpm")
            .args(["-q", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn execute_command(&self, command: &str, args: &[&str]) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .arg(command)
            .args(args)
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Command failed: {} {}",
                command,
                args.join(" ")
            ))
        }
    }
}

/// APK package manager for Alpine Linux
pub struct ApkPackageManager;

impl Default for ApkPackageManager {
    fn default() -> Self {
        Self::new()
    }
}

impl ApkPackageManager {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl PackageManager for ApkPackageManager {
    fn name(&self) -> &str {
        "apk"
    }

    async fn update(&self) -> Result<()> {
        self.execute_command("apk", &["update"]).await
    }

    async fn install(&self, package: &str) -> Result<()> {
        self.execute_command("apk", &["add", "--no-cache", package])
            .await
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("apk")
            .args(["info", "-e", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn execute_command(&self, command: &str, args: &[&str]) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .arg(command)
            .args(args)
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Command failed: {} {}",
                command,
                args.join(" ")
            ))
        }
    }
}

/// Chocolatey package manager for Windows
pub struct ChocolateyPackageManager;

impl Default for ChocolateyPackageManager {
    fn default() -> Self {
        Self::new()
    }
}

impl ChocolateyPackageManager {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl PackageManager for ChocolateyPackageManager {
    fn name(&self) -> &str {
        "chocolatey"
    }

    async fn update(&self) -> Result<()> {
        self.execute_command("choco", &["upgrade", "all", "-y"])
            .await
    }

    async fn install(&self, package: &str) -> Result<()> {
        self.execute_command("choco", &["install", package, "-y"])
            .await
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("choco")
            .args(["list", "--local-only", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn execute_command(&self, command: &str, args: &[&str]) -> Result<()> {
        let status = tokio::process::Command::new(command)
            .args(args)
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Command failed: {} {}",
                command,
                args.join(" ")
            ))
        }
    }
}

/// Homebrew package manager for macOS
pub struct BrewPackageManager;

impl Default for BrewPackageManager {
    fn default() -> Self {
        Self::new()
    }
}

impl BrewPackageManager {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl PackageManager for BrewPackageManager {
    fn name(&self) -> &str {
        "homebrew"
    }

    async fn update(&self) -> Result<()> {
        self.execute_command("brew", &["update"]).await
    }

    async fn install(&self, package: &str) -> Result<()> {
        self.execute_command("brew", &["install", package]).await
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("brew")
            .args(["list", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn execute_command(&self, command: &str, args: &[&str]) -> Result<()> {
        let status = tokio::process::Command::new(command)
            .args(args)
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!(
                "Command failed: {} {}",
                command,
                args.join(" ")
            ))
        }
    }
}
