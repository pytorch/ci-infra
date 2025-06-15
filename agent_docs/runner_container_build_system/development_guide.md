# Development Guide

This guide covers local development workflows, testing procedures, and debugging techniques for the GitHub Actions Runner Container Build System.

## 🚀 Quick Start for Developers

### Prerequisites
- Docker and Docker Compose
- Rust toolchain (for Rust installer development)
- Git
- Basic understanding of containerization and GitHub Actions

### Initial Setup
```bash
# 1. Clone the repository
git clone https://github.com/yourorg/runner-containers
cd runner-containers

# 2. Set up environment
cp dev/.env.example dev/.env
# Edit .env with your GitHub token (optional for basic testing)

# 3. Build and test locally
cd dev
docker-compose up --build

# 4. Test without GitHub connection
./test-local.sh nodejs python docker
```

## 🦀 Rust Development Workflow

### Setting Up Rust Development Environment

```bash
# Install Rust if not already installed
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

# Navigate to Rust installer directory
cd runner-installer

# Install development dependencies
cargo build
cargo test

# Run the installer directly
cargo run -- --help
cargo run -- --features="nodejs" --verbose
```

### Development Commands

```bash
# Run tests
cargo test                          # Unit tests
cargo test --test integration_tests # Integration tests
cargo test -- --nocapture          # Show output during tests

# Format code
cargo fmt

# Lint code
cargo clippy

# Check without building
cargo check

# Build release version
cargo build --release

# Run specific binary
./target/release/runner-installer --features="nodejs,python" --verbose
```

### Testing Individual Features

```bash
# Test Node.js installation
cargo run -- --features="nodejs" --verbose

# Test multiple features
cargo run -- --features="nodejs,python,docker" --verbose

# Test with environment variable
RUNNER_FEATURES="python" cargo run

# Test configuration loading
cargo run -- --config=test-config.yml --verbose
```

## 🔧 Local Testing Environment

### Docker Compose Setup

The development environment provides multiple testing scenarios:

```bash
# Basic testing with features
cd dev
docker-compose up --build

# Test specific feature combinations
RUNNER_FEATURES="nodejs python" docker-compose up --build

# Interactive testing
docker-compose run --rm runner bash
```

### Test Script Usage

The `test-local.sh` script provides comprehensive validation without requiring GitHub setup:

```bash
# Test all features
./test-local.sh nodejs python docker

# Test individual features
./test-local.sh nodejs
./test-local.sh python

# Verbose testing
VERBOSE=1 ./test-local.sh nodejs python

# Test with custom image tag
IMAGE_TAG=custom ./test-local.sh nodejs
```

### Test Script Details

```bash
#!/bin/bash
# dev/test-local.sh - Comprehensive local testing

set -e

# Configuration
IMAGE_NAME="test-runner"
FEATURES="${*:-nodejs python docker}"
VERBOSE=${VERBOSE:-0}

log() {
    echo "[TEST] $(date '+%H:%M:%S') $1"
}

verbose_log() {
    if [ "$VERBOSE" = "1" ]; then
        echo "[DEBUG] $1"
    fi
}

# Build test image
log "Building test image with Rust installer..."
docker build -t "$IMAGE_NAME" ../runners/base/ --build-arg PLATFORM=linux/amd64

# Test feature installation
log "Testing feature installation: $FEATURES"
docker run --rm \
    -e RUNNER_FEATURES="$FEATURES" \
    "$IMAGE_NAME" \
    bash -c '
        set -e
        echo "=== Testing Rust Installer ==="
        runner-installer --features="$RUNNER_FEATURES" --verbose
        
        echo "=== Verifying Installations ==="
        for feature in $RUNNER_FEATURES; do
            case $feature in
                nodejs)
                    node --version && npm --version
                    echo "✓ Node.js verification passed"
                    ;;
                python)
                    python3 --version && pip3 --version
                    echo "✓ Python verification passed"
                    ;;
                docker)
                    docker --version
                    echo "✓ Docker verification passed"
                    ;;
                *)
                    echo "! Unknown feature: $feature"
                    ;;
            esac
        done
        
        echo "=== All tests passed! ==="
    '

log "Local testing completed successfully!"
```

## 🧪 Testing Strategies

### Unit Testing

Run unit tests for individual components:

```bash
cd runner-installer

# Test OS detection
cargo test test_os_detection

# Test package manager creation
cargo test test_package_manager_creation

# Test feature creation
cargo test test_feature_creation

# Test CLI parsing
cargo test test_cli_parsing
```

### Integration Testing

Full integration tests validate the complete workflow:

```bash
# Run all integration tests
cargo test --test integration_tests

# Run specific integration test
cargo test --test integration_tests test_cli_help

# Test with different environments
RUNNER_FEATURES="nodejs,python" cargo test --test integration_tests
```

### Container Testing

Test the complete container build and feature installation:

```bash
# Build and test container
docker build -t test-runner runners/base/
docker run --rm -e RUNNER_FEATURES="nodejs python" test-runner \
    bash -c "node --version && python3 --version"

# Test container with Rust installer
docker run --rm -e RUNNER_FEATURES="nodejs" test-runner \
    runner-installer --features="nodejs" --verbose

# Interactive container testing
docker run --rm -it -e RUNNER_FEATURES="nodejs python docker" test-runner bash
```

### Cross-Platform Testing

Test across different operating systems and architectures:

```bash
# Test on different platforms (requires buildx)
docker buildx build --platform linux/amd64,linux/arm64 runners/base/

# Test Alpine variant
docker build -t test-runner-alpine runners/alpine/
docker run --rm -e RUNNER_FEATURES="nodejs" test-runner-alpine

# Test multi-platform compose
docker-compose -f dev/docker-compose-multiplatform.yml --profile ubuntu up
docker-compose -f dev/docker-compose-multiplatform.yml --profile alpine up
```

## 🔍 Debugging Techniques

### Rust Debugging

```bash
# Debug builds with more information
cargo build
RUST_BACKTRACE=1 ./target/debug/runner-installer --features="nodejs" --verbose

# Use rust-gdb for debugging
rust-gdb ./target/debug/runner-installer
(gdb) run --features="nodejs" --verbose

# Debug with logging
RUST_LOG=debug cargo run -- --features="nodejs" --verbose
```

### Container Debugging

```bash
# Run container interactively
docker run --rm -it test-runner bash

# Check container logs
docker logs <container_id>

# Debug container build
docker build --no-cache --progress=plain runners/base/

# Debug feature installation
docker run --rm -e RUNNER_FEATURES="nodejs" test-runner \
    bash -c "set -x; runner-installer --features=nodejs --verbose"
```

### Common Issues & Solutions

#### Issue: Rust compilation fails
```bash
# Solution: Update Rust toolchain
rustup update stable
cargo clean
cargo build
```

#### Issue: Container build fails on ARM64 Mac
```bash
# Solution: Specify platform explicitly
docker build --platform linux/amd64 runners/base/
```

#### Issue: Feature installation fails
```bash
# Debug: Run with verbose logging
runner-installer --features="nodejs" --verbose
# or
RUST_LOG=debug runner-installer --features="nodejs"
```

#### Issue: Package manager not found
```bash
# Debug: Check OS detection
cargo run -- --verbose
# Look for "Detected OS:" and "Using package manager:" lines
```

## 🏗️ Adding New Features

### Step 1: Create Feature Implementation

Create a new file `runner-installer/src/features/terraform.rs`:

```rust
use async_trait::async_trait;
use anyhow::Result;
use tracing::{info, debug};
use crate::{features::Feature, package_managers::PackageManager, os::{OsInfo, OsFamily}};

pub struct Terraform {
    os_info: OsInfo,
}

impl Terraform {
    pub fn new(os_info: OsInfo) -> Self {
        Self { os_info }
    }
}

#[async_trait]
impl Feature for Terraform {
    fn name(&self) -> &str {
        "terraform"
    }

    fn description(&self) -> &str {
        "Infrastructure as Code tool"
    }

    async fn is_installed(&self) -> bool {
        tokio::process::Command::new("terraform")
            .arg("--version")
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    async fn install(&self, _package_manager: &dyn PackageManager) -> Result<()> {
        if self.is_installed().await {
            info!("Terraform is already installed");
            return Ok(());
        }

        info!("Installing Terraform...");
        
        // Install via HashiCorp's official method
        let download_url = "https://releases.hashicorp.com/terraform/1.5.0/terraform_1.5.0_linux_amd64.zip";
        
        let status = tokio::process::Command::new("curl")
            .args(&["-fsSL", download_url, "-o", "/tmp/terraform.zip"])
            .status()
            .await?;

        if !status.success() {
            return Err(anyhow::anyhow!("Failed to download Terraform"));
        }

        let status = tokio::process::Command::new("sudo")
            .args(&["unzip", "/tmp/terraform.zip", "-d", "/usr/local/bin/"])
            .status()
            .await?;

        if status.success() {
            Ok(())
        } else {
            Err(anyhow::anyhow!("Failed to install Terraform"))
        }
    }

    async fn verify(&self) -> Result<()> {
        if !self.is_installed().await {
            return Err(anyhow::anyhow!("Terraform installation verification failed"));
        }

        let output = tokio::process::Command::new("terraform")
            .arg("--version")
            .output()
            .await?;

        if output.status.success() {
            let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
            info!("Terraform installed successfully: {}", version);
        }

        Ok(())
    }
}
```

### Step 2: Register the Feature

Update `runner-installer/src/features/mod.rs`:

```rust
pub mod terraform; // Add this line

pub fn create_feature(name: &str, os_info: &OsInfo) -> Result<Box<dyn Feature>> {
    match name {
        "nodejs" => Ok(Box::new(nodejs::NodeJs::new(os_info.clone()))),
        "python" => Ok(Box::new(python::Python::new(os_info.clone()))),
        "docker" => Ok(Box::new(docker::Docker::new(os_info.clone()))),
        "terraform" => Ok(Box::new(terraform::Terraform::new(os_info.clone()))), // Add this line
        _ => Err(anyhow::anyhow!("Unknown feature: {}", name)),
    }
}
```

### Step 3: Test the New Feature

```bash
# Build and test
cargo build
cargo test

# Test the new feature
cargo run -- --features="terraform" --verbose

# Test in container
./dev/test-local.sh terraform

# Test with other features
./dev/test-local.sh nodejs terraform
```

## 📋 Code Quality Standards

### Formatting and Linting

```bash
# Format code
cargo fmt

# Check formatting
cargo fmt --check

# Lint code
cargo clippy

# Lint with all features
cargo clippy --all-features

# Fix lint issues automatically
cargo clippy --fix
```

### Documentation

```bash
# Generate documentation
cargo doc

# Generate and open documentation
cargo doc --open

# Test documentation examples
cargo test --doc
```

### Performance Testing

```bash
# Run benchmarks (if implemented)
cargo bench

# Profile performance
cargo build --release
perf record ./target/release/runner-installer --features="nodejs"
perf report
```

## 🚀 Continuous Integration

### Local CI Simulation

Run the same checks that CI will run:

```bash
#!/bin/bash
# scripts/ci-check.sh

set -e

echo "=== Running CI checks locally ==="

echo "1. Checking formatting..."
cargo fmt --check

echo "2. Running clippy..."
cargo clippy --all-features -- -D warnings

echo "3. Running tests..."
cargo test --all-features

echo "4. Running integration tests..."
cargo test --test integration_tests

echo "5. Building release..."
cargo build --release

echo "6. Testing container build..."
docker build -t ci-test runners/base/

echo "7. Testing feature installation..."
docker run --rm -e RUNNER_FEATURES="nodejs python" ci-test \
    bash -c "runner-installer --features=nodejs,python --verbose"

echo "=== All CI checks passed! ==="
```

### GitHub Actions Workflow

```yaml
# .github/workflows/ci.yml
name: CI

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    
    - name: Install Rust
      uses: dtolnay/rust-toolchain@stable
      
    - name: Cache cargo
      uses: actions/cache@v3
      with:
        path: ~/.cargo
        key: ${{ runner.os }}-cargo-${{ hashFiles('**/Cargo.lock') }}
        
    - name: Check formatting
      run: cd runner-installer && cargo fmt --check
      
    - name: Run clippy
      run: cd runner-installer && cargo clippy --all-features -- -D warnings
      
    - name: Run tests
      run: cd runner-installer && cargo test --all-features
      
    - name: Build release
      run: cd runner-installer && cargo build --release
      
    - name: Test container build
      run: docker build runners/base/
```

## 🐛 Troubleshooting Common Issues

### Build Issues

1. **Rust compilation errors**
   - Update toolchain: `rustup update`
   - Clean build: `cargo clean && cargo build`

2. **Docker build failures**
   - Check platform: `docker build --platform linux/amd64`
   - Clear cache: `docker builder prune`

3. **Permission errors**
   - Check user permissions in container
   - Verify sudo configuration

### Runtime Issues

1. **Feature installation fails**
   - Check internet connectivity
   - Verify package manager availability
   - Run with verbose logging

2. **OS not detected correctly**
   - Check `/etc/os-release` file
   - Verify OS detection logic

3. **Package manager not found**
   - Ensure package manager is installed
   - Check PATH environment variable

This development guide provides a comprehensive foundation for working with the runner container build system. The combination of Rust tooling, Docker containers, and comprehensive testing ensures a robust development experience. 