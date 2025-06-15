#!/bin/bash
# GitHub Actions Runner - Feature Installation Script
# Purpose: Install development tools dynamically based on RUNNER_FEATURES environment variable
# Usage: RUNNER_FEATURES="nodejs python docker" install-features.sh

set -e  # Exit on any error
set -u  # Exit on undefined variables

# Log function for consistent output
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Validate that RUNNER_FEATURES is set
if [ -z "${RUNNER_FEATURES:-}" ]; then
    log "No features requested. Set RUNNER_FEATURES environment variable."
    log "Example: RUNNER_FEATURES='nodejs python docker'"
    exit 0
fi

log "Installing features: ${RUNNER_FEATURES}"

# Process each requested feature
for feature in ${RUNNER_FEATURES}; do
    log "Installing feature: $feature"
    
    case $feature in
        nodejs)
            log "Installing Node.js 18.x LTS..."
            # Use NodeSource repository for latest stable version
            curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
            sudo apt-get install -y nodejs
            # Verify installation
            node --version
            npm --version
            log "Node.js installation complete"
            ;;
            
        python)
            log "Installing Python 3 with pip and venv..."
            sudo apt-get update
            sudo apt-get install -y python3 python3-pip python3-venv python3-dev
            # Verify installation
            python3 --version
            pip3 --version
            log "Python installation complete"
            ;;
            
        docker)
            log "Installing Docker CLI..."
            # Install Docker's official GPG key and repository
            curl -fsSL https://get.docker.com | sudo sh
            # Add runner user to docker group (requires container restart to take effect)
            sudo usermod -aG docker runner
            # Verify installation
            docker --version
            log "Docker CLI installation complete"
            log "Note: Container restart required for Docker socket access"
            ;;
            
        *)
            log "WARNING: Unknown feature '$feature'"
            log "Available features: nodejs, python, docker"
            log "Skipping unknown feature and continuing..."
            ;;
    esac
done

log "Feature installation complete!"
log "Installed features: ${RUNNER_FEATURES}"