#!/bin/bash
# GitHub Actions Runner - Container Entrypoint
# Purpose: Configure and start the GitHub Actions runner with optional feature installation
# Environment Variables:
#   GITHUB_URL: GitHub repository or organization URL (required)
#   RUNNER_TOKEN: Registration token from GitHub (required)
#   RUNNER_NAME: Custom name for this runner (optional)
#   RUNNER_LABELS: Comma-separated labels (optional)
#   RUNNER_FEATURES: Space-separated features to install (optional)

set -e  # Exit immediately on any error

# Log function for consistent output
log() {
    echo "[RUNNER] $(date '+%Y-%m-%d %H:%M:%S') $1"
}

# Validation function
validate_required_env() {
    local var_name=$1
    local var_value=${!var_name:-}
    
    if [ -z "$var_value" ]; then
        log "ERROR: Required environment variable $var_name is not set"
        log "Please set $var_name before starting the container"
        exit 1
    fi
}

log "Starting GitHub Actions Runner container..."

# Validate required environment variables
log "Validating environment variables..."
validate_required_env "GITHUB_URL"
validate_required_env "RUNNER_TOKEN"

# Set default values for optional variables
RUNNER_NAME=${RUNNER_NAME:-"container-runner-$(hostname)"}
RUNNER_LABELS=${RUNNER_LABELS:-"self-hosted,Linux,X64,container"}
RUNNER_WORKDIR=${RUNNER_WORKDIR:-"_work"}

log "Configuration:"
log "  GitHub URL: $GITHUB_URL"
log "  Runner Name: $RUNNER_NAME"
log "  Labels: $RUNNER_LABELS"
log "  Work Directory: $RUNNER_WORKDIR"

# Install features if requested
if [ -n "${RUNNER_FEATURES:-}" ]; then
    log "Features requested: $RUNNER_FEATURES"
    log "Installing features..."
    
    # Call our feature installation script
    if ! /usr/local/bin/install-features.sh; then
        log "ERROR: Feature installation failed"
        exit 1
    fi
    
    log "Feature installation completed successfully"
else
    log "No additional features requested"
fi

# Configure the GitHub Actions runner
log "Configuring GitHub Actions runner..."

# Remove any existing runner configuration (for container restarts)
if [ -f ".runner" ]; then
    log "Removing existing runner configuration..."
    ./config.sh remove --token "${RUNNER_TOKEN}" || true
fi

# Configure the runner with GitHub
log "Registering runner with GitHub..."
./config.sh \
    --url "${GITHUB_URL}" \
    --token "${RUNNER_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --work "${RUNNER_WORKDIR}" \
    --labels "${RUNNER_LABELS}" \
    --unattended \
    --replace

if [ $? -ne 0 ]; then
    log "ERROR: Failed to configure runner"
    log "Please check your GITHUB_URL and RUNNER_TOKEN"
    exit 1
fi

log "Runner configuration completed successfully"

# Cleanup function for graceful shutdown
cleanup() {
    log "Received shutdown signal, cleaning up..."
    log "Removing runner registration..."
    ./config.sh remove --token "${RUNNER_TOKEN}" || true
    log "Cleanup completed"
    exit 0
}

# Set up signal handlers for graceful shutdown
trap cleanup SIGTERM SIGINT

# Start the runner
log "Starting GitHub Actions runner..."
log "Runner is ready to receive jobs from: $GITHUB_URL"

# Execute the runner (this blocks until the runner stops)
exec ./run.sh