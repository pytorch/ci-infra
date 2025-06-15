# Multi-Platform Support

This document details the cross-platform architecture and implementation strategies for supporting multiple operating systems and Linux distributions in the GitHub Actions Runner Container Build System.

## 🌐 Multi-Platform Architecture Overview

The runner container system is designed with cross-platform compatibility as a core principle:

- **Unified API**: Single Rust-based installer with platform-specific implementations
- **Package Manager Abstraction**: Support for apt, yum, apk, chocolatey, homebrew
- **OS Detection**: Smart detection of Linux distributions, Windows, and macOS
- **Container Variants**: Multiple base images optimized for different platforms
- **Build System**: Multi-architecture container builds (linux/amd64, linux/arm64)

## 🦀 Rust Cross-Platform Implementation

### OS Detection System

The Rust installer provides comprehensive OS detection across platforms:

```rust
// runner-installer/src/os/mod.rs
#[derive(Debug, Clone)]
pub struct OsInfo {
    pub name: String,
    pub version: String,
    pub arch: String,
    pub family: OsFamily,
}

#[derive(Debug, Clone, PartialEq)]
pub enum OsFamily {
    Linux,
    Windows,
    MacOs,
    Unknown,
}

/// Detect the current operating system
pub fn detect_os() -> Result<OsInfo> {
    let arch = env::consts::ARCH.to_string();
    
    #[cfg(target_os = "linux")]
    return linux::detect_linux_info(arch);
    
    #[cfg(target_os = "windows")]
    return windows::detect_windows_info(arch);
    
    #[cfg(target_os = "macos")]
    return macos::detect_macos_info(arch);
    
    #[cfg(not(any(target_os = "linux", target_os = "windows", target_os = "macos")))]
    Err(anyhow!("Unsupported operating system"))
}
```

### Linux Distribution Detection

```rust
// runner-installer/src/os/linux.rs
pub fn detect_linux_info(arch: String) -> Result<OsInfo> {
    let (name, version) = if let Ok(os_release) = fs::read_to_string("/etc/os-release") {
        parse_os_release(&os_release)
    } else if let Ok(debian_version) = fs::read_to_string("/etc/debian_version") {
        ("debian".to_string(), debian_version.trim().to_string())
    } else if let Ok(redhat_release) = fs::read_to_string("/etc/redhat-release") {
        parse_redhat_release(&redhat_release)
    } else if fs::metadata("/etc/alpine-release").is_ok() {
        ("alpine".to_string(), get_alpine_version())
    } else if fs::metadata("/etc/arch-release").is_ok() {
        ("arch".to_string(), "rolling".to_string())
    } else {
        ("linux".to_string(), "unknown".to_string())
    };

    Ok(OsInfo {
        name,
        version,
        arch,
        family: OsFamily::Linux,
    })
}

fn parse_os_release(content: &str) -> (String, String) {
    let mut name = "linux".to_string();
    let mut version = "unknown".to_string();
    
    for line in content.lines() {
        if let Some(value) = line.strip_prefix("ID=") {
            name = value.trim_matches('"').to_string();
        } else if let Some(value) = line.strip_prefix("VERSION_ID=") {
            version = value.trim_matches('"').to_string();
        }
    }
    
    (name, version)
}
```

### Package Manager Abstraction

```rust
// runner-installer/src/package_managers/mod.rs
#[async_trait]
pub trait PackageManager: Send + Sync {
    fn name(&self) -> &str;
    async fn update(&self) -> Result<()>;
    async fn install(&self, package: &str) -> Result<()>;
    async fn is_installed(&self, package: &str) -> bool;
    async fn remove(&self, package: &str) -> Result<()>;
}

/// Create appropriate package manager for the OS  
pub fn create_package_manager(os_info: &OsInfo) -> Result<Box<dyn PackageManager>> {
    match &os_info.family {
        OsFamily::Linux => {
            match os_info.name.as_str() {
                "ubuntu" | "debian" => Ok(Box::new(apt::Apt::new())),
                "centos" | "rhel" | "fedora" | "rocky" | "almalinux" => {
                    Ok(Box::new(yum::Yum::new()))
                }
                "alpine" => Ok(Box::new(apk::Apk::new())),
                "arch" | "manjaro" => Ok(Box::new(pacman::Pacman::new())),
                "opensuse" | "sles" => Ok(Box::new(zypper::Zypper::new())),
                _ => {
                    // Fallback logic - try to detect available package managers
                    if which::which("apt").is_ok() {
                        Ok(Box::new(apt::Apt::new()))
                    } else if which::which("yum").is_ok() || which::which("dnf").is_ok() {
                        Ok(Box::new(yum::Yum::new()))
                    } else if which::which("apk").is_ok() {
                        Ok(Box::new(apk::Apk::new()))
                    } else {
                        Err(anyhow!("No supported package manager found"))
                    }
                }
            }
        }
        OsFamily::Windows => Ok(Box::new(chocolatey::Chocolatey::new())),
        OsFamily::MacOs => Ok(Box::new(brew::Brew::new())),
        OsFamily::Unknown => Err(anyhow!("No package manager available for unknown OS")),
    }
}
```

## 🐧 Linux Distribution Support

### Ubuntu/Debian (APT)

```rust
// runner-installer/src/package_managers/apt.rs
use async_trait::async_trait;
use anyhow::Result;
use crate::package_managers::PackageManager;

pub struct Apt;

impl Apt {
    pub fn new() -> Self {
        Self
    }
}

#[async_trait]
impl PackageManager for Apt {
    fn name(&self) -> &str {
        "apt"
    }

    async fn update(&self) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .args(&["apt-get", "update"])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to update package lists"))
        }
    }

    async fn install(&self, package: &str) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .args(&["apt-get", "install", "-y", package])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to install package: {}", package))
        }
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("dpkg")
            .args(&["-l", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn remove(&self, package: &str) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .args(&["apt-get", "remove", "-y", package])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to remove package: {}", package))
        }
    }
}
```

### CentOS/RHEL/Fedora (YUM/DNF)

```rust
// runner-installer/src/package_managers/yum.rs
pub struct Yum;

impl Yum {
    pub fn new() -> Self {
        Self
    }
    
    async fn get_package_manager(&self) -> &str {
        // Prefer dnf over yum on newer systems
        if which::which("dnf").is_ok() {
            "dnf"
        } else {
            "yum"
        }
    }
}

#[async_trait]
impl PackageManager for Yum {
    fn name(&self) -> &str {
        "yum"
    }

    async fn update(&self) -> Result<()> {
        let pm = self.get_package_manager().await;
        let status = tokio::process::Command::new("sudo")
            .args(&[pm, "update", "-y"])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to update package lists"))
        }
    }

    async fn install(&self, package: &str) -> Result<()> {
        let pm = self.get_package_manager().await;
        let status = tokio::process::Command::new("sudo")
            .args(&[pm, "install", "-y", package])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to install package: {}", package))
        }
    }

    async fn is_installed(&self, package: &str) -> bool {
        let pm = self.get_package_manager().await;
        tokio::process::Command::new(pm)
            .args(&["list", "installed", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }
}
```

### Alpine Linux (APK)

```rust
// runner-installer/src/package_managers/apk.rs
pub struct Apk;

#[async_trait]
impl PackageManager for Apk {
    fn name(&self) -> &str {
        "apk"
    }

    async fn update(&self) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .args(&["apk", "update"])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to update package index"))
        }
    }

    async fn install(&self, package: &str) -> Result<()> {
        let status = tokio::process::Command::new("sudo")
            .args(&["apk", "add", "--no-cache", package])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to install package: {}", package))
        }
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("apk")
            .args(&["info", "-e", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }
}
```

## 🪟 Windows Support

### Chocolatey Package Manager

```rust
// runner-installer/src/package_managers/chocolatey.rs
pub struct Chocolatey;

#[async_trait]
impl PackageManager for Chocolatey {
    fn name(&self) -> &str {
        "chocolatey"
    }

    async fn update(&self) -> Result<()> {
        let status = tokio::process::Command::new("choco")
            .args(&["upgrade", "all", "-y"])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to update chocolatey packages"))
        }
    }

    async fn install(&self, package: &str) -> Result<()> {
        let status = tokio::process::Command::new("choco")
            .args(&["install", package, "-y"])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to install package: {}", package))
        }
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("choco")
            .args(&["list", "--local-only", package])
            .output()
            .await
            .map(|output| {
                output.status.success() && 
                String::from_utf8_lossy(&output.stdout).contains(package)
            })
            .unwrap_or(false)
    }
}
```

### Windows Container Dockerfile

```dockerfile
# runners/windows/Dockerfile
# escape=`
FROM mcr.microsoft.com/windows/servercore:ltsc2022

# Install Chocolatey package manager
RUN powershell -Command `
    Set-ExecutionPolicy Bypass -Scope Process -Force; `
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; `
    iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

# Install essential tools
RUN choco install -y git curl jq

# Create runner user
RUN net user runner /add && `
    net localgroup administrators runner /add

# Install Rust toolchain
RUN choco install -y rust

# Copy and build Rust installer
COPY runner-installer/ C:/temp/runner-installer/
WORKDIR C:/temp/runner-installer
RUN cargo build --release && `
    copy target\release\runner-installer.exe C:\tools\ && `
    setx PATH "%PATH%;C:\tools"

# Clean up build dependencies
RUN rmdir /s /q C:\temp\runner-installer

# Copy Windows-specific scripts
COPY scripts/entrypoint-windows.ps1 C:/runner/
COPY scripts/install-features-windows.ps1 C:/scripts/

# Install GitHub runner for Windows
RUN powershell -Command `
    Invoke-WebRequest -Uri 'https://github.com/actions/runner/releases/download/v2.311.0/actions-runner-win-x64-2.311.0.zip' -OutFile 'C:\runner\actions-runner.zip'; `
    Expand-Archive -Path 'C:\runner\actions-runner.zip' -DestinationPath 'C:\runner'; `
    Remove-Item 'C:\runner\actions-runner.zip'

USER runner
WORKDIR C:/runner

ENTRYPOINT ["powershell", "-File", "C:/runner/entrypoint-windows.ps1"]
```

## 🍎 macOS Support

### Homebrew Package Manager

```rust
// runner-installer/src/package_managers/brew.rs
pub struct Brew;

#[async_trait]
impl PackageManager for Brew {
    fn name(&self) -> &str {
        "brew"
    }

    async fn update(&self) -> Result<()> {
        let status = tokio::process::Command::new("brew")
            .args(&["update"])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to update homebrew"))
        }
    }

    async fn install(&self, package: &str) -> Result<()> {
        let status = tokio::process::Command::new("brew")
            .args(&["install", package])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow!("Failed to install package: {}", package))
        }
    }

    async fn is_installed(&self, package: &str) -> bool {
        tokio::process::Command::new("brew")
            .args(&["list", package])
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }
}
```

## 🐳 Multi-Platform Container Images

### Docker Buildx Multi-Platform Setup

```bash
#!/bin/bash
# scripts/build-multiplatform.sh

set -e

# Create and use buildx builder
docker buildx create --name multiplatform --use --bootstrap

# Supported platforms
PLATFORMS="linux/amd64,linux/arm64,linux/arm/v7"

# Build variants
VARIANTS=("ubuntu" "alpine" "centos")

for variant in "${VARIANTS[@]}"; do
    echo "Building ${variant} for multiple platforms..."
    
    docker buildx build \
        --platform "$PLATFORMS" \
        --tag "ghcr.io/yourorg/runner-${variant}:latest" \
        --tag "ghcr.io/yourorg/runner-${variant}:$(date +%Y%m%d)" \
        --push \
        "./runners/${variant}/"
        
    echo "✓ Completed ${variant}"
done

# Windows build (separate due to platform requirements)
echo "Building Windows container..."
docker buildx build \
    --platform "windows/amd64" \
    --tag "ghcr.io/yourorg/runner-windows:latest" \
    --push \
    "./runners/windows/" || echo "! Windows build requires Windows build nodes"

echo "=== Multi-platform build completed ==="
```

### Ubuntu Multi-Platform Dockerfile

```dockerfile
# runners/ubuntu/Dockerfile
FROM ubuntu:22.04

# Install build dependencies and Rust
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    build-essential \
    ca-certificates \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    && . ~/.cargo/env \
    && rm -rf /var/lib/apt/lists/*

# Set PATH for Rust
ENV PATH="/root/.cargo/bin:${PATH}"

# Create runner user
RUN useradd -m -s /bin/bash runner && \
    usermod -aG sudo runner && \
    echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Copy and build Rust installer
COPY runner-installer/ /tmp/runner-installer/
WORKDIR /tmp/runner-installer
RUN cargo build --release && \
    cp target/release/runner-installer /usr/local/bin/ && \
    chmod +x /usr/local/bin/runner-installer

# Clean up build dependencies
RUN rm -rf /tmp/runner-installer ~/.cargo

# Switch to runner user
USER runner
WORKDIR /home/runner

# Install GitHub runner (architecture-aware)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        RUNNER_ARCH="x64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        RUNNER_ARCH="arm64"; \
    elif [ "$ARCH" = "armv7l" ]; then \
        RUNNER_ARCH="arm"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -o actions-runner.tar.gz -L \
        "https://github.com/actions/runner/releases/download/v2.311.0/actions-runner-linux-${RUNNER_ARCH}-2.311.0.tar.gz" && \
    tar xzf actions-runner.tar.gz && \
    rm actions-runner.tar.gz

# Copy entrypoint
COPY scripts/entrypoint.sh /home/runner/
ENTRYPOINT ["/home/runner/entrypoint.sh"]
```

### Alpine Multi-Platform Dockerfile

```dockerfile
# runners/alpine/Dockerfile
FROM alpine:3.19

# Install build dependencies
RUN apk add --no-cache \
    curl \
    git \
    sudo \
    bash \
    build-base \
    ca-certificates

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Create runner user
RUN adduser -D -s /bin/bash runner && \
    echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Copy and build Rust installer
COPY runner-installer/ /tmp/runner-installer/
WORKDIR /tmp/runner-installer
RUN . ~/.cargo/env && cargo build --release && \
    cp target/release/runner-installer /usr/local/bin/ && \
    chmod +x /usr/local/bin/runner-installer

# Clean up
RUN rm -rf /tmp/runner-installer ~/.cargo
RUN apk del build-base

# Switch to runner user
USER runner
WORKDIR /home/runner

# Install GitHub runner (architecture-aware)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        RUNNER_ARCH="x64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        RUNNER_ARCH="arm64"; \
    elif [ "$ARCH" = "armv7l" ]; then \
        RUNNER_ARCH="arm"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -o actions-runner.tar.gz -L \
        "https://github.com/actions/runner/releases/download/v2.311.0/actions-runner-linux-${RUNNER_ARCH}-2.311.0.tar.gz" && \
    tar xzf actions-runner.tar.gz && \
    rm actions-runner.tar.gz

COPY scripts/entrypoint.sh /home/runner/
ENTRYPOINT ["/home/runner/entrypoint.sh"]
```

## 🧪 Multi-Platform Testing

### Test Matrix Configuration

```yaml
# .github/workflows/test-multiplatform.yml
name: Multi-Platform Testing

on: [push, pull_request]

jobs:
  test-rust:
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
        rust: [stable, beta]
    runs-on: ${{ matrix.os }}
    
    steps:
    - uses: actions/checkout@v4
    
    - name: Install Rust
      uses: dtolnay/rust-toolchain@master
      with:
        toolchain: ${{ matrix.rust }}
        
    - name: Cache cargo
      uses: actions/cache@v3
      with:
        path: ~/.cargo
        key: ${{ runner.os }}-cargo-${{ hashFiles('**/Cargo.lock') }}
        
    - name: Run tests
      run: cd runner-installer && cargo test
      
    - name: Test OS detection
      run: cd runner-installer && cargo run -- --verbose

  test-containers:
    strategy:
      matrix:
        variant: [ubuntu, alpine, centos]
        platform: [linux/amd64, linux/arm64]
        features: [nodejs, python, "nodejs,python", "nodejs,python,docker"]
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v4
    
    - name: Set up Docker Buildx
      uses: docker/setup-buildx-action@v3
      
    - name: Build and test
      run: |
        docker buildx build \
          --platform ${{ matrix.platform }} \
          --load \
          --tag test-runner \
          runners/${{ matrix.variant }}/
          
        docker run --rm \
          -e RUNNER_FEATURES="${{ matrix.features }}" \
          test-runner \
          runner-installer --features="${{ matrix.features }}" --verbose
```

## 📊 Platform Support Matrix

| Platform | OS Family | Package Manager | Container Support | Rust Support | Status |
|----------|-----------|-----------------|-------------------|--------------|---------|
| **Linux** | | | | | |
| Ubuntu 22.04 | Linux | apt | ✅ Full | ✅ Native | ✅ Production |
| Ubuntu 20.04 | Linux | apt | ✅ Full | ✅ Native | ✅ Production |
| Debian 11/12 | Linux | apt | ✅ Full | ✅ Native | ✅ Production |
| Alpine 3.19 | Linux | apk | ✅ Full | ✅ Native | ✅ Production |
| CentOS 8/9 | Linux | yum/dnf | ✅ Full | ✅ Native | ✅ Production |
| RHEL 8/9 | Linux | yum/dnf | ✅ Full | ✅ Native | ✅ Production |
| Fedora 38/39 | Linux | dnf | ✅ Full | ✅ Native | ✅ Production |
| Rocky Linux | Linux | dnf | ✅ Full | ✅ Native | ✅ Production |
| Arch Linux | Linux | pacman | 🚧 Planned | ✅ Native | 🚧 Development |
| openSUSE | Linux | zypper | 🚧 Planned | ✅ Native | 🚧 Development |
| **Windows** | | | | | |
| Windows Server 2022 | Windows | chocolatey | ✅ Full | ✅ Native | ✅ Production |
| Windows Server 2019 | Windows | chocolatey | ✅ Full | ✅ Native | ✅ Production |
| Windows 11 | Windows | chocolatey | 🚧 Testing | ✅ Native | 🚧 Development |
| **macOS** | | | | | |
| macOS 13+ | macOS | homebrew | ❌ N/A | ✅ Native | 🚧 Development |
| **Architecture** | | | | | |
| linux/amd64 | - | - | ✅ Full | ✅ Native | ✅ Production |
| linux/arm64 | - | - | ✅ Full | ✅ Native | ✅ Production |
| linux/arm/v7 | - | - | 🚧 Testing | ✅ Native | 🚧 Development |
| windows/amd64 | - | - | ✅ Full | ✅ Native | ✅ Production |

## 🔧 Advanced Multi-Platform Features

### Universal Binary Installation

For packages not available through package managers:

```rust
// Universal installation fallback
impl Feature for NodeJs {
    async fn install_universal_binary(&self) -> Result<()> {
        let (os, arch) = match (&self.os_info.family, self.os_info.arch.as_str()) {
            (OsFamily::Linux, "x86_64") => ("linux", "x64"),
            (OsFamily::Linux, "aarch64") => ("linux", "arm64"),
            (OsFamily::Linux, "armv7l") => ("linux", "armv7l"),
            (OsFamily::Windows, "x86_64") => ("win", "x64"),
            (OsFamily::MacOs, "x86_64") => ("darwin", "x64"),
            (OsFamily::MacOs, "aarch64") => ("darwin", "arm64"),
            _ => return Err(anyhow!("Unsupported platform for universal binary")),
        };
        
        let url = format!(
            "https://nodejs.org/dist/v18.20.8/node-v18.20.8-{}-{}.tar.xz",
            os, arch
        );
        
        // Download and install
        self.download_and_install(&url).await
    }
}
```

### Feature Platform Compatibility

```rust
// Feature-specific platform support
impl Feature for Docker {
    fn is_platform_supported(&self) -> bool {
        match &self.os_info.family {
            OsFamily::Linux => true,
            OsFamily::Windows => true,
            OsFamily::MacOs => false, // Docker Desktop not suitable for CI
            OsFamily::Unknown => false,
        }
    }
    
    async fn install(&self, package_manager: &dyn PackageManager) -> Result<()> {
        if !self.is_platform_supported() {
            return Err(anyhow!(
                "Docker is not supported on {} {}", 
                self.os_info.family, 
                self.os_info.name
            ));
        }
        
        // Platform-specific installation logic
        match &self.os_info.family {
            OsFamily::Linux => self.install_linux(package_manager).await,
            OsFamily::Windows => self.install_windows(package_manager).await,
            _ => unreachable!(),
        }
    }
}
```

This multi-platform architecture provides comprehensive support across different operating systems and architectures while maintaining a unified, type-safe interface through the Rust implementation. 