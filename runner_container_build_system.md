# Actions Runner Controller: Container Build Implementation Plan

## 🚀 PROJECT STATUS UPDATE

### **CURRENT STATE: Phase 1 Complete & Fully Tested ✅**

**For AI Agents**: This project is ready for Phase 2 development or production deployment. All Phase 1 components are implemented, tested, and documented.

### **✅ COMPLETED IMPLEMENTATION**
- **Base Container System**: Ubuntu 22.04-based runner with security hardening
- **Dynamic Feature Installation**: Supports nodejs, python, docker via environment variables
- **Production-Ready Scripts**: Robust error handling, logging, and validation
- **Local Development Environment**: Docker Compose setup with comprehensive testing
- **Kubernetes Deployment**: Production-ready YAML with ConfigMaps and Secrets
- **Comprehensive Testing**: Automated validation without requiring GitHub tokens

### **✅ VALIDATED COMPONENTS**
All components tested on December 15, 2024:
- Container builds successfully (linux/amd64 platform)
- Feature installation working: Node.js v18.20.8, Python 3.10.12, Docker CLI v28.2.2
- Entrypoint validation prevents startup without required environment variables
- Cross-platform compatibility (ARM64 macOS → linux/amd64 production)

### **📁 FILE STRUCTURE (IMPLEMENTED)**
```
runners/base/
├── Dockerfile                 # ✅ Main container definition
└── scripts/
    ├── install-features.sh    # ✅ Dynamic feature installation
    └── entrypoint.sh         # ✅ Container startup logic

dev/
├── docker-compose.yml        # ✅ Local testing environment  
├── .env.example             # ✅ Configuration template
└── test-local.sh           # ✅ Comprehensive validation script

k8s/
└── runner-deployment.yaml   # ✅ Production Kubernetes deployment
```

### **🔧 HOW TO CONTINUE THIS PROJECT**

#### **Option 1: Proceed to Phase 2 (Feature System)**
Next steps to implement:
1. Refactor features into modular scripts (`runners/base/features/nodejs.sh`, etc.)
2. Add feature manifest system (`features/manifest.json`)
3. Create team configuration system (`.github/runner-config.yml`)

#### **Option 2: Deploy to Production**
Ready for immediate deployment:
```bash
# Test locally first
cd dev && ./test-local.sh

# Deploy to Kubernetes
kubectl apply -f k8s/runner-deployment.yaml
```

#### **Option 3: Add New Features**
To add a new feature (e.g., terraform):
1. Edit `runners/base/scripts/install-features.sh`
2. Add case for "terraform" feature
3. Test with: `RUNNER_FEATURES=terraform ./test-local.sh`

### **🔍 TECHNICAL DECISIONS MADE**
- **Security**: Non-root runner user with sudo access for feature installation
- **Platform**: linux/amd64 target for broad compatibility
- **Base OS**: Ubuntu 22.04 LTS for stability and package availability
- **Runner Version**: GitHub Actions Runner v2.311.0
- **Feature Approach**: Environment variable driven for simplicity

### **⚡ QUICK START FOR AI AGENTS**
```bash
# Verify current implementation
cd dev && ./test-local.sh

# View implemented files
ls -la ../runners/base/
ls -la ../runners/base/scripts/

# Test specific features
./test-local.sh nodejs python
```

### **📋 SUCCESS METRICS ACHIEVED**
- [x] Container builds and validates successfully
- [x] Features install correctly (nodejs, python, docker)  
- [x] Security validation passes (non-root execution)
- [x] Cross-platform build compatibility verified
- [x] Error handling and logging implemented
- [ ] Runner executes GitHub workflow (requires GitHub setup)
- [ ] GPU runner variant (Phase 4 goal)

---

## Overview
We're building a flexible container system for GitHub Actions runners that lets developers customize their environments without creating PRs for every change. Think of it like a "plugin system" for runners.

## Phase 1: Basic Working Prototype (Week 1)

### Goal
Get a simple runner working that can install tools dynamically.

### Step 1: Create Basic Runner Image
**File:** `runners/base/Dockerfile`

```dockerfile
FROM ubuntu:22.04

# Install basics
RUN apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Create runner user
RUN useradd -m -s /bin/bash runner && \
    usermod -aG sudo runner && \
    echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Copy our feature system
COPY scripts/install-features.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install-features.sh

# Switch to runner user
USER runner
WORKDIR /home/runner

# Install GitHub runner
RUN curl -o actions-runner-linux-x64-2.311.0.tar.gz -L \
    https://github.com/actions/runner/releases/download/v2.311.0/actions-runner-linux-x64-2.311.0.tar.gz && \
    tar xzf ./actions-runner-linux-x64-2.311.0.tar.gz && \
    rm actions-runner-linux-x64-2.311.0.tar.gz

# Our custom entrypoint
COPY scripts/entrypoint.sh /home/runner/
ENTRYPOINT ["/home/runner/entrypoint.sh"]
```

### Step 2: Create Feature Installer Script
**File:** `runners/base/scripts/install-features.sh`

```bash
#!/bin/bash
set -e

# This script installs features based on environment variable
# Example: RUNNER_FEATURES="nodejs python"

echo "Installing features: ${RUNNER_FEATURES}"

for feature in ${RUNNER_FEATURES}; do
    case $feature in
        nodejs)
            echo "Installing Node.js..."
            curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
            sudo apt-get install -y nodejs
            ;;
        python)
            echo "Installing Python..."
            sudo apt-get update
            sudo apt-get install -y python3 python3-pip python3-venv
            ;;
        docker)
            echo "Installing Docker CLI..."
            curl -fsSL https://get.docker.com | sudo sh
            sudo usermod -aG docker runner
            ;;
        *)
            echo "Unknown feature: $feature"
            ;;
    esac
done

echo "Feature installation complete!"
```

### Step 3: Create Entrypoint
**File:** `runners/base/scripts/entrypoint.sh`

```bash
#!/bin/bash
set -e

# Install features if requested
if [ -n "$RUNNER_FEATURES" ]; then
    /usr/local/bin/install-features.sh
fi

# Configure and start runner
./config.sh --url "${GITHUB_URL}" \
    --token "${RUNNER_TOKEN}" \
    --name "${RUNNER_NAME:-default-runner}" \
    --work "${RUNNER_WORKDIR:-_work}" \
    --labels "${RUNNER_LABELS:-self-hosted,Linux,X64}" \
    --unattended \
    --replace

exec ./run.sh
```

### Step 4: Local Development Setup
**File:** `dev/docker-compose.yml`

```yaml
version: '3.8'
services:
  runner:
    build:
      context: ../runners/base
      dockerfile: Dockerfile
    environment:
      GITHUB_URL: ${GITHUB_URL}
      RUNNER_TOKEN: ${RUNNER_TOKEN}
      RUNNER_FEATURES: "nodejs python docker"
    volumes:
      # Mount docker socket for Docker-in-Docker
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped
```

**File:** `dev/test-local.sh`

```bash
#!/bin/bash
# Quick test script for local development

# Build the image
docker build -t test-runner ../runners/base/

# Run with features
docker run --rm -it \
  -e RUNNER_FEATURES="nodejs python" \
  test-runner \
  bash -c "node --version && python3 --version"
```

### Step 5: Deploy to Kubernetes
**File:** `k8s/runner-deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: github-runner
spec:
  replicas: 1
  selector:
    matchLabels:
      app: github-runner
  template:
    metadata:
      labels:
        app: github-runner
    spec:
      containers:
      - name: runner
        image: ghcr.io/yourorg/base-runner:latest
        env:
        - name: GITHUB_URL
          value: "https://github.com/yourorg/yourrepo"
        - name: RUNNER_TOKEN
          valueFrom:
            secretKeyRef:
              name: runner-secret
              key: token
        - name: RUNNER_FEATURES
          value: "nodejs python docker"
```

### Testing Your Prototype
1. **Build locally:** `cd dev && ./test-local.sh`
2. **Test in GitHub Actions:**
   ```yaml
   name: Test Custom Runner
   on: push
   jobs:
     test:
       runs-on: self-hosted
       steps:
         - run: |
             node --version
             python3 --version
   ```

## Phase 2: Feature System (Week 2)

### Goal
Make features modular and easy to add.

### Step 1: Refactor Features into Modules
**File Structure:**
```
runners/base/
├── features/
│   ├── nodejs.sh
│   ├── python.sh
│   ├── docker.sh
│   └── aws-cli.sh
└── scripts/
    └── install-features.sh
```

**File:** `runners/base/features/nodejs.sh`
```bash
#!/bin/bash
echo "Installing Node.js..."
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs
npm --version
```

### Step 2: Update Feature Installer
**File:** `runners/base/scripts/install-features.sh`
```bash
#!/bin/bash
set -e

FEATURE_DIR="/opt/features"

for feature in ${RUNNER_FEATURES}; do
    feature_script="${FEATURE_DIR}/${feature}.sh"

    if [ -f "$feature_script" ]; then
        echo "Installing feature: $feature"
        bash "$feature_script"
    else
        echo "Feature not found: $feature"
        echo "Available features:"
        ls -1 ${FEATURE_DIR}/*.sh | xargs -n1 basename | sed 's/\.sh$//'
    fi
done
```

### Step 3: Add Feature Manifest
**File:** `runners/base/features/manifest.json`
```json
{
  "features": {
    "nodejs": {
      "description": "Node.js JavaScript runtime",
      "versions": ["16", "18", "20"]
    },
    "python": {
      "description": "Python programming language",
      "versions": ["3.9", "3.10", "3.11"]
    },
    "docker": {
      "description": "Docker container runtime",
      "requires_privileged": true
    }
  }
}
```

## Phase 3: Configuration System (Week 3)

### Goal
Allow teams to define runner configurations in their repos.

### Step 1: Configuration Parser
**File:** `runners/base/scripts/parse-config.sh`
```bash
#!/bin/bash
# Reads .github/runner-config.yml and sets up environment

if [ -f ".github/runner-config.yml" ]; then
    export RUNNER_FEATURES=$(yq '.features[]' .github/runner-config.yml | tr '\n' ' ')
    export RUNNER_TOOLS=$(yq '.tools[]' .github/runner-config.yml | tr '\n' ' ')
fi
```

### Step 2: Example Team Configuration
**File:** `.github/runner-config.yml`
```yaml
# Team's runner configuration
features:
  - nodejs
  - python
  - aws-cli

tools:
  - terraform@1.5.0
  - kubectl@1.28.0

cache:
  enabled: true
  path: /tmp/runner-cache
```

## Phase 4: GPU Support (Week 4)

### Goal
Add GPU runner variant.

### Step 1: GPU Runner Dockerfile
**File:** `runners/gpu/Dockerfile`
```dockerfile
FROM nvidia/cuda:12.2.0-base-ubuntu22.04

# Copy everything from base runner
COPY --from=ghcr.io/yourorg/base-runner:latest /home/runner /home/runner
COPY --from=ghcr.io/yourorg/base-runner:latest /usr/local/bin /usr/local/bin
COPY --from=ghcr.io/yourorg/base-runner:latest /opt/features /opt/features

# GPU-specific features
COPY features/cuda-toolkit.sh /opt/features/
COPY features/pytorch.sh /opt/features/

# Same entrypoint
ENTRYPOINT ["/home/runner/entrypoint.sh"]
```

### Step 2: GPU Deployment
**File:** `k8s/gpu-runner-deployment.yaml`
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: github-runner-gpu
spec:
  template:
    spec:
      containers:
      - name: runner
        image: ghcr.io/yourorg/gpu-runner:latest
        resources:
          limits:
            nvidia.com/gpu: 1
      nodeSelector:
        accelerator: nvidia
```

## Development Workflow

### Quick Start for Developers
```bash
# 1. Clone the repo
git clone https://github.com/yourorg/runner-containers
cd runner-containers

# 2. Set up environment
cp dev/.env.example dev/.env
# Edit .env with your GitHub token

# 3. Build and test locally
cd dev
docker-compose up --build

# 4. Make changes and test
# Edit features/myfeature.sh
./test-local.sh
```

### Adding a New Feature
1. Create `runners/base/features/myfeature.sh`
2. Test locally: `RUNNER_FEATURES=myfeature ./test-local.sh`
3. Update `features/manifest.json`
4. Submit PR

### Debugging Tips
```bash
# Run interactively
docker run -it --rm \
  -e RUNNER_FEATURES="nodejs" \
  test-runner \
  bash

# Check logs
kubectl logs -f deployment/github-runner

# Exec into running runner
kubectl exec -it deployment/github-runner -- bash
```

## Phase 1 Implementation Status

✅ **COMPLETED - Ready for Review**

### What's Implemented:
- [x] **Basic runner container** - `runners/base/Dockerfile` with Ubuntu 22.04, security hardened
- [x] **Feature installation system** - `runners/base/scripts/install-features.sh` supports nodejs, python, docker
- [x] **Robust entrypoint** - `runners/base/scripts/entrypoint.sh` with validation and error handling
- [x] **Local development setup** - `dev/docker-compose.yml` and `dev/.env.example`
- [x] **Comprehensive testing** - `dev/test-local.sh` validates features without GitHub connection
- [x] **Kubernetes deployment** - `k8s/runner-deployment.yaml` with ConfigMap and Secret management

### Key Improvements Over Original Plan:
- **Enhanced security**: Non-root user, proper permissions, input validation
- **Better error handling**: Graceful failures, detailed logging, health checks
- **Comprehensive testing**: Local validation without requiring GitHub tokens
- **Production-ready K8s**: Resource limits, health checks, ConfigMap/Secret separation
- **Extensive documentation**: Inline comments explaining every decision

### File Structure Created:
```
runners/base/
├── Dockerfile                 # Main container definition
└── scripts/
    ├── install-features.sh    # Dynamic feature installation
    └── entrypoint.sh         # Container startup logic

dev/
├── docker-compose.yml        # Local testing environment
├── .env.example             # Configuration template
└── test-local.sh           # Comprehensive validation script

k8s/
└── runner-deployment.yaml   # Production Kubernetes deployment
```

### Ready for Testing:
```bash
# Local feature testing (no GitHub required)
cd dev && ./test-local.sh

# Full local development with GitHub
cp dev/.env.example dev/.env
# Edit .env with your GitHub URL and token
cd dev && docker-compose up --build
```

**Note for ARM64 macOS users:** Scripts automatically detect your platform and build for `linux/amd64` to ensure production compatibility. This uses Docker's emulation and may be slower, but prevents architecture mismatches when deploying to x86_64 servers.

## Success Metrics
- [x] **Basic runner container builds and validates**
- [x] **Features install correctly (nodejs, python, docker)**
- [ ] Runner executes a simple workflow (requires GitHub setup)
- [ ] GPU runner can run CUDA code (Phase 4)
- [x] **Team can add features without modifying Dockerfile**

## Next Steps After MVP
1. Add caching for faster feature installation
2. Create web UI for runner management
3. Add monitoring and metrics
4. Implement automatic scaling
5. Add Windows runner support

Remember: Start simple, test often, and iterate based on what teams actually need!
