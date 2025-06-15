# Container Tests for runner-installer

This directory contains Dockerfiles for testing the runner-installer features in containerized environments.

## Available Images

### Ubuntu Jammy (22.04)
- **Dockerfile**: `Dockerfile.ubuntu-jammy`
- **Purpose**: Test runner-installer features on Ubuntu 22.04 LTS
- **Features**: Includes Rust toolchain, build dependencies, and the compiled runner-installer binary

## Running Container Tests

The container tests are located in `/tests/container_tests.rs` and use the bollard Docker API to:

1. Build Docker images for specific operating systems
2. Run containers using the built images
3. Execute commands within containers to test feature installation and verification
4. Clean up containers after testing

### Prerequisites

- Docker must be installed and running
- Rust toolchain for running tests

### Running the Tests

```bash
# Run all container tests
cargo test --test container_tests

# Run a specific container test
cargo test --test container_tests test_uv_installation_ubuntu_jammy

# Run with output
cargo test --test container_tests -- --nocapture
```

### Test Coverage

The container tests currently cover:

1. **UV Installation Test** (`test_uv_installation_ubuntu_jammy`):
   - Verifies uv is not initially installed
   - Installs uv using runner-installer
   - Verifies uv installation and basic functionality
   - Checks uv can be found in expected locations

2. **UV Already Installed Test** (`test_uv_already_installed_scenario`):
   - Tests the scenario where uv is already installed
   - Verifies runner-installer detects existing installation

3. **UV Verification Test** (`test_uv_verification`):
   - Tests uv's basic functionality after installation
   - Verifies uv can perform expected operations

## Docker Image Optimization

The Dockerfiles are optimized for build caching:

1. **Dependency Caching**: Cargo dependencies are built in a separate layer
2. **Source Code Layer**: Application source is copied and built separately
3. **Multi-stage Build**: Uses multi-stage builds where appropriate

## Adding New OS Support

To add support for a new operating system:

1. Create a new Dockerfile: `Dockerfile.<os-name>`
2. Follow the same pattern as existing Dockerfiles
3. Add corresponding test functions in `container_tests.rs`
4. Update this README with the new OS information

## Troubleshooting

### Docker Connection Issues
- Ensure Docker daemon is running
- Check Docker permissions for your user
- Verify Docker socket is accessible

### Build Failures
- Check Dockerfile syntax
- Ensure all required dependencies are installed in the base image
- Verify the build context includes all necessary files

### Test Failures
- Check container logs for detailed error information
- Verify the feature installation process works manually
- Ensure the test assertions match the expected behavior
