# Custom PyTorch GPU Development Server Image
# Based on pytorch/pytorch:2.12.0-cuda13.2-cudnn9-devel
FROM pytorch/pytorch:2.12.0-cuda13.2-cudnn9-devel

# Set environment variables for non-interactive installation
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Update apt and install base packages in a single layer so the package metadata
# is always fresh relative to the install. Splitting update from install causes
# Ubuntu to 404 on stale cached versions whenever security updates retire old .debs.
RUN for attempt in 1 2 3; do \
        echo "Package update attempt $attempt..." && \
        apt-get update -qq && break || \
        ([ $attempt -lt 3 ] && echo "Update failed, waiting 30s..." && sleep 30) \
    done && \
    apt-get install -y --no-install-recommends \
        openssh-server \
        sudo \
        curl \
        wget \
        vim \
        nano \
        neovim \
        git \
        coreutils \
        util-linux \
        procps \
        zsh \
        bash-completion \
        ca-certificates \
        gnupg \
        lsb-release \
        iproute2 \
        tmux \
        unzip \
        ccache \
        htop \
        tree \
        rsync \
        zstd \
        pigz \
        mold \
        bubblewrap
# Install Node.js 20 from NodeSource (required for Claude CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs

# Install older CUDA toolkits alongside base CUDA 13.2
# Base image already has NVIDIA repo configured, no need for cuda-keyring
RUN apt-get update && apt-get install -y --no-install-recommends \
        cuda-toolkit-12-8 \
        cuda-toolkit-12-9 \
        cuda-toolkit-13-0 \
        cuda-toolkit-13-1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# CUDA 13.2 is the default (PyTorch 2.12 compiled against it)
# All versions available at /usr/local/cuda-{12.8,12.9,13.0,13.1,13.2}/
# Switch with: export CUDA_HOME=/usr/local/cuda-12.8
ENV CUDA_HOME=/usr/local/cuda-13.2
ENV PATH=/usr/local/cuda-13.2/bin:${PATH}
ENV LD_LIBRARY_PATH=/usr/local/cuda-13.2/lib64:${LD_LIBRARY_PATH}

# Install EFA stack (prebuilt libfabric + OpenMPI + aws-ofi-nccl with GPU/RDMA support)
# Uses AWS EFA installer which bundles tested, compatible versions of all components
# -g: GPU support (enables CUDA HMEM / DMA-BUF for GPUDirect RDMA)
# --skip-kmod: skip kernel modules (host handles these)
# --skip-limit-conf: not needed in containers
# --no-verify: no EFA device at build time
ENV EFA_VERSION=1.47.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libhwloc-dev \
        pciutils \
        kmod \
        udev \
    && cd /tmp \
    && curl -sL https://efa-installer.amazonaws.com/aws-efa-installer-${EFA_VERSION}.tar.gz | tar xz \
    && cd aws-efa-installer \
    && ./efa_installer.sh -y -g --skip-kmod --skip-limit-conf --no-verify --mpi=openmpi4 \
    && cd / && rm -rf /tmp/aws-efa-installer* \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Clone and build NCCL tests with MPI support for multi-node benchmarking
RUN apt-get update && apt-get install -y --no-install-recommends libnccl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN cd /opt && \
    git clone https://github.com/NVIDIA/nccl-tests.git && \
    cd nccl-tests && \
    make MPI=1 MPI_HOME=/opt/amazon/openmpi CUDA_HOME=/usr/local/cuda NCCL_HOME=/usr -j$(nproc)

# Set environment variables for EFA and NCCL
ENV PATH=/opt/amazon/openmpi/bin:/opt/amazon/efa/bin:${PATH}
ENV LD_LIBRARY_PATH=/opt/amazon/ofi-nccl/lib:/opt/amazon/openmpi/lib:/opt/amazon/efa/lib:${LD_LIBRARY_PATH}
ENV FI_PROVIDER=efa
ENV FI_EFA_USE_DEVICE_RDMA=1
ENV NCCL_NET_GDR_LEVEL=SYS
ENV NCCL_DEBUG=INFO
ENV NCCL_ALGO=ring,tree
ENV NCCL_SOCKET_IFNAME=enp71s0
ENV NCCL_IB_HCA=^mlx
ENV NCCL_CROSS_NIC=0
ENV NCCL_IB_PCI_RELAXED_ORDERING=1
ENV NCCL_ASYNC_ERROR_HANDLING=1
ENV SUPPORTS_EFA=true

# Install Python packages (Jupyter and common ML packages)
# gpu-dev itself is bundled so users can run `gpu-dev submit` from inside their pod
# (combined with IRSA on the pod's service account, no manual aws sso login needed).
RUN pip install --no-cache-dir --break-system-packages \
        jupyterlab \
        ipywidgets \
        matplotlib \
        seaborn \
        pandas \
        numpy \
        scikit-learn \
        plotly \
        tensorboard \
        gpu-dev \
        aws-bedrock-token-generator

# Create dev user with UID 1081 to avoid conflicts with common base image users (e.g., ubuntu=1000)
RUN useradd -u 1081 -m -s /usr/bin/zsh dev && \
    usermod -aG sudo dev && \
    echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev && \
    echo 'Defaults lecture=never' >> /etc/sudoers.d/dev && \
    echo 'Defaults !lecture' >> /etc/sudoers.d/dev && \
    mkdir -p /home/dev/.ssh && \
    printf "Host *\n    StrictHostKeyChecking no\n    UserKnownHostsFile /dev/null\n    LogLevel ERROR\n" > /home/dev/.ssh/config && \
    chmod 700 /home/dev/.ssh && \
    chmod 600 /home/dev/.ssh/config && \
    chown -R 1081:1081 /home/dev/.ssh

# Configure SSH daemon
RUN mkdir -p /run/sshd /var/run/sshd && \
    ssh-keygen -A

# Create SSH config
COPY ssh_config /etc/ssh/sshd_config

# Install Claude Code SYSTEM-WIDE via the official native installer (as root).
# Lives in /opt/claude with a /usr/local/bin/claude symlink, survives every reservation
# regardless of persistent-disk state, and can't be shadowed by stale npm installs on
# user disks. Image rebuilds are the only update path — controlled, reproducible.
# Bump CLAUDE_CODE_BUILD to bust the layer cache and re-fetch the latest Claude Code
# (the installer always grabs latest; without a bump Docker reuses the cached layer).
USER root
ARG CLAUDE_CODE_BUILD=2026-05-29
RUN echo "Claude Code build marker: $CLAUDE_CODE_BUILD" && \
    curl -fsSL https://claude.ai/install.sh | HOME=/opt/claude bash || echo "Claude native install failed (non-fatal at build time)"
RUN if [ -e /opt/claude/.local/bin/claude ]; then \
        ln -sf /opt/claude/.local/bin/claude /usr/local/bin/claude; \
        chmod -R a+rX /opt/claude; \
    fi

# Set up npm global directory for dev user (kept for ad-hoc dev-installed CLIs).
USER dev
WORKDIR /home/dev
RUN mkdir -p ~/.npm-global && \
    npm config set prefix ~/.npm-global

# OpenAI Codex CLI on GPT-5.5 via AWS Bedrock (GA 2026-06-01). Installed system-wide
# (parallels Claude above), then /usr/local/bin/codex is replaced with a thin wrapper that
# auths via the pod IRSA — it mints a short-lived Bedrock bearer token (no per-user OpenAI
# key) and pins the bedrock-mantle provider + GPT-5.5 metadata. Reasoning effort is set with
# the CODEX_EFFORT env var (default high); the wrapper rewrites ~/.codex/config.toml each
# launch (home is ephemeral) so a /model picker mishap self-heals on restart. IAM is already
# in place (pod IRSA: bedrock-mantle:* + aws-marketplace:Subscribe).
USER root
RUN npm install -g --prefix /usr/local @openai/codex || echo "Codex CLI install failed (non-fatal at build time)"
# Bedrock wrapper, base64-embedded to avoid heredoc/quoting fragility. It execs the real
# launcher at /usr/local/lib/node_modules/@openai/codex/bin/codex.js. CRITICAL: `npm install`
# leaves /usr/local/bin/codex as a SYMLINK to that codex.js, so we must `rm -f` it first —
# writing through the symlink would clobber codex.js itself, making the wrapper exec itself
# (infinite recursion -> codex hangs on launch).
RUN rm -f /usr/local/bin/codex && echo 'IyEvdXNyL2Jpbi9lbnYgYmFzaAojIENvZGV4IHdpcmVkIHRvIEdQVC01LjUgb24gQVdTIEJlZHJvY2sgKHVzLWVhc3QtMiBtYW50bGUgZW5kcG9pbnQpLgojIEF1dGggdmlhIHRoZSBwb2QgSVJTQSAtPiBzaG9ydC1saXZlZCAofjEyaCkgQmVkcm9jayBiZWFyZXIgdG9rZW47IG5vIHBlci11c2VyIGtleS4KIyBSZWFzb25pbmcgZWZmb3J0IHZpYSBDT0RFWF9FRkZPUlQgZW52IChkZWZhdWx0IGhpZ2gpLiBUaGUgY29uZmlnIGlzIChyZSl3cml0dGVuIG9uCiMgZXZlcnkgbGF1bmNoOiAvaG9tZS9kZXYgaXMgZXBoZW1lcmFsLCBhbmQgdGhpcyBhbHNvIHNlbGYtaGVhbHMgYSAvbW9kZWwgbWlzaGFwCiMgKHRoZSBwaWNrZXIgY2FuIGNvcnJ1cHQgdGhlIG1vZGVsIGlkOyByZXN0YXJ0aW5nIGNvZGV4IHJlc2V0cyBpdCkuCnNldCArZQpSRUFMPS91c3IvbG9jYWwvbGliL25vZGVfbW9kdWxlcy9Ab3BlbmFpL2NvZGV4L2Jpbi9jb2RleC5qcwpFRkZPUlQ9IiR7Q09ERVhfRUZGT1JUOi1oaWdofSIKbWtkaXIgLXAgIiRIT01FLy5jb2RleCIgMj4vZGV2L251bGwKY2F0ID4gIiRIT01FLy5jb2RleC9jb25maWcudG9tbCIgPDxDRkcKbW9kZWwgPSAib3BlbmFpLmdwdC01LjUiCm1vZGVsX3Byb3ZpZGVyID0gImJlZHJvY2siCndlYl9zZWFyY2ggPSAiZGlzYWJsZWQiCm1vZGVsX2NvbnRleHRfd2luZG93ID0gMjcyMDAwCm1vZGVsX21heF9vdXRwdXRfdG9rZW5zID0gMTI4MDAwCm1vZGVsX3JlYXNvbmluZ19lZmZvcnQgPSAiJEVGRk9SVCIKClttb2RlbF9wcm92aWRlcnMuYmVkcm9ja10KbmFtZSA9ICJBV1MgQmVkcm9jayAoR1BULTUuNSkiCmJhc2VfdXJsID0gImh0dHBzOi8vYmVkcm9jay1tYW50bGUudXMtZWFzdC0yLmFwaS5hd3Mvb3BlbmFpL3YxIgplbnZfa2V5ID0gIk9QRU5BSV9BUElfS0VZIgp3aXJlX2FwaSA9ICJyZXNwb25zZXMiCkNGRwpUT0s9IiQocHl0aG9uMyAtYyAnZnJvbSBhd3NfYmVkcm9ja190b2tlbl9nZW5lcmF0b3IgaW1wb3J0IHByb3ZpZGVfdG9rZW47IHByaW50KHByb3ZpZGVfdG9rZW4ocmVnaW9uPSJ1cy1lYXN0LTIiKSknIDI+L2Rldi9udWxsKSIKWyAtbiAiJFRPSyIgXSAmJiBleHBvcnQgT1BFTkFJX0FQSV9LRVk9IiRUT0siCmV4ZWMgIiRSRUFMIiAiJEAiCg==' | base64 -d > /usr/local/bin/codex && chmod 0755 /usr/local/bin/codex

USER dev

# Install oh-my-zsh for dev user
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended

# Install useful zsh plugins
RUN git clone https://github.com/zsh-users/zsh-autosuggestions ~/.oh-my-zsh/custom/plugins/zsh-autosuggestions && \
    git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ~/.oh-my-zsh/custom/plugins/zsh-syntax-highlighting

# Create Jupyter config directory (token will be set at runtime)
RUN mkdir -p ~/.jupyter

# Create setup directory with all configs and user home items for copying to persistent disk after mount
USER root
RUN mkdir -p /devserver-setup && \
    cp -r /home/dev/.npm-global /devserver-setup/npm-global && \
    cp -r /home/dev/.oh-my-zsh /devserver-setup/oh-my-zsh && \
    cp -r /home/dev/.jupyter /devserver-setup/jupyter && \
    cp /home/dev/.npmrc /devserver-setup/.npmrc

COPY --chown=root:root shell_env /devserver-setup/.shell_env
COPY --chown=root:root zshrc /devserver-setup/.zshrc
COPY --chown=root:root zshrc_ext /devserver-setup/.zshrc_ext
COPY --chown=root:root bashrc /devserver-setup/.bashrc
COPY --chown=root:root bashrc_ext /devserver-setup/.bashrc_ext
COPY --chown=root:root bash_profile /devserver-setup/.bash_profile
COPY --chown=root:root profile /devserver-setup/.profile
COPY --chown=root:root zprofile /devserver-setup/.zprofile

# Don't copy to /home/dev since persistent disk will overwrite it
USER dev

# Switch back to root for final configuration
USER root

# Create custom MOTD  
COPY motd_script /usr/local/bin/motd_script
COPY motd_script /etc/update-motd.d/00-custom
RUN chmod +x /usr/local/bin/motd_script && \
    chmod +x /etc/update-motd.d/00-custom && \
    rm -f /etc/motd /etc/legal /usr/share/base-files/motd 2>/dev/null || true && \
    mkdir -p /etc/motd.d /etc/update-motd.d /var/lib/sudo/lectured && \
    touch /etc/motd.d/00-header && \
    chmod 644 /etc/motd.d/00-header && \
    touch /var/lib/sudo/lectured/dev && \
    echo "dev" > /var/lib/sudo/lectured/dev

# Add dotfiles persistence scripts
COPY setup-dotfiles-persistence /usr/local/bin/setup-dotfiles-persistence
COPY backup-dotfiles /usr/local/bin/backup-dotfiles
COPY restore-dotfiles /usr/local/bin/restore-dotfiles
COPY list-dotfile-versions /usr/local/bin/list-dotfile-versions
COPY restore-dotfiles-version /usr/local/bin/restore-dotfiles-version
COPY dotfiles-shutdown-handler /usr/local/bin/dotfiles-shutdown-handler
RUN chmod +x /usr/local/bin/setup-dotfiles-persistence && \
    chmod +x /usr/local/bin/backup-dotfiles && \
    chmod +x /usr/local/bin/restore-dotfiles && \
    chmod +x /usr/local/bin/list-dotfile-versions && \
    chmod +x /usr/local/bin/restore-dotfiles-version && \
    chmod +x /usr/local/bin/dotfiles-shutdown-handler

# Install nproc wrapper to report container CPU allocation instead of node CPUs
COPY nproc_wrapper /usr/local/bin/nproc_wrapper
RUN mv /usr/bin/nproc /usr/bin/nproc.real && \
    cp /usr/local/bin/nproc_wrapper /usr/bin/nproc && \
    chmod +x /usr/bin/nproc /usr/local/bin/nproc_wrapper

# Disable PAM MOTD and Ubuntu Pro ads
RUN sed -i 's/session    optional     pam_motd.so/#&/g' /etc/pam.d/sshd 2>/dev/null || true && \
    sed -i 's/session    optional     pam_motd.so  motd=/#&/g' /etc/pam.d/sshd 2>/dev/null || true && \
    rm -rf /etc/update-motd.d/00-header /etc/update-motd.d/10-help-text /etc/update-motd.d/80-esm /etc/update-motd.d/95-hwe-eol 2>/dev/null || true && \
    chmod -x /usr/sbin/update-motd 2>/dev/null || true

# Clean up
RUN apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Force rebuild for Ubuntu node compatibility
RUN echo "Built for Ubuntu nodes: $(date)" > /build-info.txt

# Set proper permissions
RUN chown -R dev:dev /home/dev && \
    chmod 700 /home/dev/.ssh 2>/dev/null || true

# Expose ports
EXPOSE 22 8888

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep sshd || exit 1

# Default command will be overridden by Kubernetes pod spec
CMD ["/usr/sbin/sshd", "-D", "-e"]