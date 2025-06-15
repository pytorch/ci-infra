#!/bin/bash
# GitHub Actions Runner - Local Testing Script
# Purpose: Build and test runner container without connecting to GitHub
# Usage: ./test-local.sh [feature1] [feature2] ...

set -e

# Configuration
IMAGE_NAME="github-runner-test"
BUILD_CONTEXT="../runners/base"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Log functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Platform detection for cross-platform builds
PLATFORM=""
if [[ "$(uname -m)" == "arm64" ]] && [[ "$(uname -s)" == "Darwin" ]]; then
    log_warn "Detected ARM64 macOS - building for linux/amd64 platform"
    log_warn "Note: This may be slower due to emulation, but ensures production compatibility"
    PLATFORM="--platform linux/amd64"
fi


# Test features (default set if none provided)
TEST_FEATURES=${*:-"nodejs python docker"}

log_info "Starting local runner container test"
log_info "Testing features: $TEST_FEATURES"

# Step 1: Build the container image
log_info "Building runner container image..."
if ! docker build $PLATFORM -t "$IMAGE_NAME" "$BUILD_CONTEXT"; then
    log_error "Failed to build container image"
    if [[ -n "$PLATFORM" ]]; then
        log_error "Tip: Ensure Docker Desktop has 'Use cross-platform features' enabled"
        log_error "Or try without platform flag by commenting out platform detection"
    fi
    exit 1
fi
log_info "Container build successful"

# Step 2: Test feature installation (without GitHub connection)
log_info "Testing feature installation..."

# Create a test command that installs features and verifies them
TEST_CMD="
set -e
echo 'Testing feature installation...'

# Install features
RUNNER_FEATURES='$TEST_FEATURES' /usr/local/bin/install-features.sh

# Verify installations
echo 'Verifying installed features...'
"

# Add verification commands for each feature
for feature in $TEST_FEATURES; do
    case $feature in
        nodejs)
            TEST_CMD="$TEST_CMD
if command -v node >/dev/null 2>&1; then
    echo '✓ Node.js: \$(node --version)'
    echo '✓ npm: \$(npm --version)'
else
    echo '✗ Node.js installation failed'
    exit 1
fi"
            ;;
        python)
            TEST_CMD="$TEST_CMD
if command -v python3 >/dev/null 2>&1; then
    echo '✓ Python: \$(python3 --version)'
    echo '✓ pip: \$(pip3 --version)'
else
    echo '✗ Python installation failed'
    exit 1
fi"
            ;;
        docker)
            TEST_CMD="$TEST_CMD
if command -v docker >/dev/null 2>&1; then
    echo '✓ Docker: \$(docker --version)'
else
    echo '✗ Docker installation failed'
    exit 1
fi"
            ;;
    esac
done

TEST_CMD="$TEST_CMD
echo 'All feature tests passed!'
"

# Run the test
log_info "Running feature installation test..."
if docker run --rm $PLATFORM \
    --entrypoint bash \
    -e RUNNER_FEATURES="$TEST_FEATURES" \
    "$IMAGE_NAME" \
    -c "$TEST_CMD"; then
    log_info "Feature installation test passed!"
else
    log_error "Feature installation test failed"
    exit 1
fi

# Step 3: Test entrypoint script validation
log_info "Testing entrypoint parameter validation..."

# Test missing required environment variables
log_info "Testing missing GITHUB_URL (should fail)..."
if docker run --rm $PLATFORM "$IMAGE_NAME" bash -c "timeout 10 /home/runner/entrypoint.sh" 2>/dev/null; then
    log_error "Entrypoint should have failed without GITHUB_URL"
    exit 1
else
    log_info "✓ Correctly failed without GITHUB_URL"
fi

log_info "Testing missing RUNNER_TOKEN (should fail)..."
if docker run --rm $PLATFORM \
    -e GITHUB_URL="https://github.com/test/repo" \
    "$IMAGE_NAME" \
    bash -c "timeout 10 /home/runner/entrypoint.sh" 2>/dev/null; then
    log_error "Entrypoint should have failed without RUNNER_TOKEN"
    exit 1
else
    log_info "✓ Correctly failed without RUNNER_TOKEN"
fi

# Step 4: Cleanup
log_info "Cleaning up test image..."
docker rmi "$IMAGE_NAME" >/dev/null 2>&1 || true

# Summary
log_info "🎉 All tests passed!"
log_info ""
log_info "Next steps:"
log_info "1. Copy dev/.env.example to dev/.env"
log_info "2. Configure your GitHub URL and runner token"
log_info "3. Run: cd dev && docker-compose up --build"
log_info ""
log_info "The runner container is ready for deployment!"
