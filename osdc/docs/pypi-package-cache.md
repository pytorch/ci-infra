# PyPI Package Cache for CI Runners

## Problem

Every CI job creates a fresh virtualenv and pulls every Python package from remote PyPI. This causes:

- **Slow installs**: large packages (torch, numpy, etc.) take significant time to download and, in some cases, build from source
- **Rate limiting**: high concurrency across many runner pods triggers PyPI throttling
- **Build-from-source cost**: packages without pre-built wheels for the target platform are compiled on every job, wasting minutes of CPU

The git-cache-warmer DaemonSet already solves an analogous problem for git clones. This document evaluates equivalent strategies for Python packages.

## Requirements

1. Both `pip` and `uv` must work transparently (no workflow changes)
2. Pre-built wheels (compiled from source) must be cacheable and reusable across pods
3. Cache misses must fall back to upstream PyPI
4. Must fit the existing DaemonSet-on-NVMe architecture
5. Lightweight enough to run per-node

## Approaches Evaluated

### Approach 1: Wheelhouse DaemonSet (find-links)

A DaemonSet pre-downloads (and pre-builds) wheels to NVMe. Job pods mount the directory read-only and use `--find-links` to find packages locally.

**Mechanism:**
- DaemonSet runs `pip wheel -r requirements.txt -w /mnt/pypi-cache/wheelhouse/` and `pip download -r requirements.txt -d /mnt/pypi-cache/wheelhouse/`
- Job pods mount the wheelhouse at `/opt/pypi-cache` (read-only)
- Env vars: `PIP_FIND_LINKS=/opt/pypi-cache`, `UV_FIND_LINKS=/opt/pypi-cache`
- pip and uv check the local directory first, fall back to PyPI for misses

**Strengths:**
- Closest to the existing git-cache pattern (DaemonSet + hostPath + dual-slot rotation + startup taint)
- Read-only mount works fine for both pip and uv
- No proxy process running on the node
- Zero network overhead for cached packages

**Pitfalls:**
- Requires a **fully curated requirements list** — every package (including transitives) must be listed or it won't be in the cache
- Cache misses still hit PyPI directly (no on-demand caching)
- The requirements list must be maintained as dependencies evolve
- Platform-specific: wheels built for `linux_x86_64 + cp311` won't work on `aarch64` or `cp312`
- Pre-building source packages in the DaemonSet init can be slow (delays node readiness)

### Approach 2: nginx Caching Reverse Proxy (DaemonSet)

A DaemonSet runs nginx as a caching reverse proxy to `pypi.org` on each node.

**Mechanism:**
- nginx proxies requests to `pypi.org/simple/` and `files.pythonhosted.org`, caching responses on NVMe
- Job pods: `PIP_INDEX_URL=http://localhost:3141/simple/`, `UV_INDEX_URL=http://localhost:3141/simple/`
- First request for a package proxies to PyPI and caches; subsequent requests served from disk
- Optional pre-warming: `pip download -i http://localhost:3141/simple/ -r requirements.txt`

**Strengths:**
- No curated package list needed — caches on demand
- After a few builds on a node, the working set is fully cached
- Extremely lightweight (~10 MB RAM)
- Battle-tested at scale (1000+ downloads/min reported)
- Natural upstream fallback for cache misses
- Reference implementation: [hauntsaninja/nginx_pypi_cache](https://github.com/hauntsaninja/nginx_pypi_cache)

**Pitfalls:**
- **Cannot cache locally-built wheels** — only caches downloads from PyPI. Packages built from source are rebuilt every time on every node.
- Cold cache on fresh nodes (first build is slow)
- nginx config requires SNI (`proxy_ssl_server_name on`) and dual-host proxying (index host + file download host)
- Does not solve the build-from-source problem at all

### Approach 3: Shared uv Cache Directory (hostPath)

A DaemonSet warms uv's native cache on NVMe. Job pods share it via `UV_CACHE_DIR`.

**Mechanism:**
- DaemonSet runs `uv pip install -r requirements.txt` into a throwaway venv, populating the cache
- Job pods: `UV_CACHE_DIR=/opt/uv-cache`
- uv's cache is append-only and thread-safe for concurrent writes

**Strengths:**
- Simplest approach — no proxy process, no server
- uv's cache includes built wheels (compiled from source)
- Cache is deduplicated across venvs via hardlinks/reflinks

**Pitfalls:**
- **uv cannot use a read-only cache** — fails with `Permission denied (os error 13)`. Tracked as [astral-sh/uv#15934](https://github.com/astral-sh/uv/issues/15934), still open. Mount must be read-write.
- **uv only** — pip uses a different cache format. Jobs using pip get no benefit.
- No `uv pip download` command exists yet ([astral-sh/uv#2078](https://github.com/astral-sh/uv/issues/2078))
- Concurrent read-write from multiple pods to the same hostPath is risky (even though uv claims thread safety, multiple containers are not the same as multiple threads)

### Approach 4: Centralized devpi Proxy (Deployment)

A single devpi-server Deployment + PVC in the cluster, serving as both a PyPI proxy and a host for pre-built wheels.

**Mechanism:**
- devpi-server proxies PyPI and caches packages on first fetch
- Pre-built wheels can be uploaded via `devpi upload`
- All job pods: `PIP_INDEX_URL=http://devpi.pypi-cache:3141/root/pypi/+simple/`
- devpi-builder can batch-build and upload wheels from a requirements file

**Strengths:**
- Single cache instance — warm once, all nodes benefit immediately
- Supports uploading custom/pre-built wheels alongside PyPI proxy
- Mature project with a large user base
- Supports multiple indexes with inheritance (e.g., private packages + PyPI)

**Pitfalls:**
- **Heavyweight**: ~300 MB RAM minimum (without devpi-web), can grow to 2-4 GB with the web UI
- Single point of failure (unless replicated, which adds complexity)
- Network hop across nodes for every package fetch (latency for large wheels)
- Requires a PVC for persistent storage (not NVMe-local)
- Does not leverage per-node NVMe locality like the git-cache pattern
- SQLite-backed — potential bottleneck under high concurrency
- Overkill for a pure caching use case

### Approach 5: proxpi (Lightweight PyPI Caching Proxy)

A purpose-built lightweight PyPI caching proxy, designed for CI.

**Mechanism:**
- Single Python process, Docker image available
- Caches both index responses (30-min TTL) and package files (5 GB default)
- Config via env vars: `PROXPI_CACHE_DIR`, `PROXPI_CACHE_SIZE`, `PROXPI_INDEX_URL`

**Strengths:**
- Purpose-built for CI — understands PyPI's index format natively
- Lighter than devpi (~50-100 MB RAM)
- Simple configuration via environment variables

**Pitfalls:**
- **Cannot cache locally-built wheels** (same limitation as nginx)
- Heavier than nginx for what is essentially the same function
- Smaller community and less battle-tested than nginx or devpi
- No support for uploading/serving custom wheels

### Approach 6: bandersnatch (Selective PyPI Mirror)

Official PyPA mirroring tool. Syncs selected packages from PyPI to local storage.

**Mechanism:**
- Configure an allowlist of packages in `bandersnatch.conf`
- Run `bandersnatch mirror` to sync selected packages to local disk
- Serve the mirror with a separate web server (nginx, pypiserver)

**Strengths:**
- Official PyPA project, PEP 503/691 compliant
- Selective mirroring keeps storage manageable
- Full control over what is mirrored

**Pitfalls:**
- **Not a proxy** — no pass-through fallback for unlisted packages
- Requires a separate serving layer (two components to manage)
- Requires scheduled sync runs to pick up new versions
- Cannot serve locally-built wheels
- Operational complexity disproportionate to the problem

## Transparent Serving (No Workflow Changes Needed)

Both pip and uv respect environment variables for index URL. Setting these on job pods is sufficient to transparently route all package downloads through the local proxy — no workflow changes needed.

- `PIP_INDEX_URL` — affects all `pip install` commands in the container
- `UV_INDEX_URL` / `UV_DEFAULT_INDEX` — affects all `uv` commands
- uv also reads `PIP_INDEX_URL` for compatibility

**Precedence chain (pip):** CLI `--index-url` > requirements.txt `--index-url` > `PIP_INDEX_URL` env > `pip.conf` > pypi.org default

**Precedence chain (uv):** CLI `--index-url` > `UV_INDEX_URL` env > `pyproject.toml`/`uv.toml` > pypi.org default

### What bypasses the proxy (not captured)

- Workflows that explicitly set `--index-url` on CLI or in requirements.txt (overrides env var)
- Direct URL installs: `pip install https://some-url/package.whl` (bypasses index entirely)
- VCS installs: `pip install git+https://github.com/...` (cloned directly, not via index)
- `--no-index --find-links` (ignores all indexes)

Note: dependencies of direct-URL and VCS packages still resolve through the configured index, so transitive deps are captured.

**Coverage estimate:** Env vars alone capture ~85-90% of real-world pip/uv usage.

### Optional hardening with iptables

For bulletproof interception, an init container with `NET_ADMIN` can install iptables rules redirecting `pypi.org`/`files.pythonhosted.org` traffic to the local proxy by SNI. This catches even explicit `--index-url` overrides. But it adds complexity (`NET_ADMIN` capability, TLS handling, uv needs `UV_NATIVE_TLS=true` for custom CAs).

## Transparent Capture (Self-Learning Cache via Proxy Logs)

**Key insight: the proxy IS the telemetry.** If all pip/uv traffic flows through a local proxy (pypiserver, nginx, or devpi), the proxy access log captures everything needed — no `pip --report` flag required, no user cooperation needed.

What a single proxy access log line captures:

- **Package name, version, platform tags** — from the wheel filename in the URL path (e.g., `torch-2.5.1-cp311-cp311-manylinux_2_17_x86_64.whl`)
- **Python version, ABI** — from the wheel filename
- **Installer tool** — from User-Agent header (pip includes a rich JSON blob with pip version, Python version, OS, CPU arch, distro)
- **Timestamp** — when it was requested
- **Cache hit/miss** — HTTP status code (200 vs 304)
- **Transfer size** — bytes transferred
- **Whether it was an sdist** — URL ends in `.tar.gz` or `.zip` instead of `.whl`, meaning the client will build from source

This is **richer than `pip --report`** because:

- Tool-agnostic: captures pip, uv, poetry, conda, any HTTP client
- Zero user cooperation: developers write `pip install torch` and have no idea telemetry exists
- No overwrite problems: one log line per HTTP request, naturally appending
- Already available if running a cache proxy for performance

### pip --report: useful but not required

`pip install --report` (stable since pip 23.0) generates structured JSON with every package installed, including whether it was a wheel or sdist. However:

- Each invocation **overwrites** the file (no append mode)
- **uv has no equivalent** (issue #1442, still open)
- Requires setting `PIP_REPORT` env var or using a wrapper — opt-in, not transparent
- Only captures pip, not uv/poetry/other tools

The proxy log approach supersedes this for telemetry. `pip --report` remains useful for detailed dependency resolution debugging but is not needed for the cache learning loop.

### Why NOT pip wrappers/shims

Placing a wrapper script named `pip` earlier in PATH that injects flags is fragile:

- `python -m pip` bypasses the shim entirely (very common in CI)
- `pip3`, `pip3.11`, etc. need separate shims
- uv needs its own shim with different flags
- Virtualenv creation installs its own pip, bypassing shims
- More moving parts than the proxy approach with worse coverage

### Comparison of capture mechanisms

| Mechanism | pip | uv | poetry | No user action | Structured data | Covers sdist builds |
|-----------|-----|-----|--------|---------------|-----------------|---------------------|
| Proxy access log | Yes | Yes | Yes | Yes | Parse from URL | Yes (detects .tar.gz) |
| PIP_REPORT env var | Yes | No | No | Partial (env var) | JSON | Yes |
| PIP_LOG env var | Yes | No | No | Partial (env var) | Unstructured text | Yes |
| pip wrapper/shim | Partial | No | No | No | Depends | Yes |
| Network iptables | Yes | Yes | Yes | Yes | Parse from traffic | Yes |

**Recommended: proxy access log** as primary telemetry. Optionally add `PIP_LOG` env var for pip-specific debugging detail.

## Recommended Approach: Self-Learning pypiserver DaemonSet

**pypiserver** is a single-process, ~30 MB Python server that serves `.whl` files from a directory and falls through to upstream PyPI for anything not found locally. Combined with proxy log analysis, the cache becomes fully self-learning — no curated package list required, no workflow changes needed, the cache self-populates from actual usage.

### Architecture

```
Job pods (no changes needed):
  pip install torch  ──►  PIP_INDEX_URL=http://localhost:8080/simple/
  uv pip install numpy ──► UV_INDEX_URL=http://localhost:8080/simple/

DaemonSet per node (on NVMe):
┌───────────────────────────────────────────────────────────┐
│  pypiserver (or nginx reverse proxy)                      │
│  - serves pre-built wheels from per-CUDA wheelhouses      │
│  - falls back to pypi.org for cache misses                │
│  - access log captures ALL requests (the telemetry)       │
│                                                           │
│  Index paths:                                             │
│    /cpu/simple/   → wheelhouse-cpu/                       │
│    /cu118/simple/ → wheelhouse-cu118/                     │
│    /cu121/simple/ → wheelhouse-cu121/                     │
│    /cu124/simple/ → wheelhouse-cu124/                     │
│                                                           │
│  builder (periodic, e.g. every 6h):                       │
│  1. parse access log for .tar.gz downloads                │
│     (these are packages downloaded as sdists =             │
│      built from source by the client)                     │
│  2. for each CUDA version configured:                     │
│       pip wheel <pkg>==<ver> → wheelhouse-<cuda>/         │
│  3. next request for same pkg gets pre-built wheel        │
│                                                           │
│  NVMe hostPath: /mnt/pypi-cache/                          │
└───────────────────────────────────────────────────────────┘
```

### Request flow

1. Composite GitHub Action sets `PIP_INDEX_URL=http://localhost:8080/{cuda_slug}/simple/` for the job (see "CI integration" below)
2. pip/uv queries `localhost:8080/{cuda_slug}/simple/{package}/`
3. pypiserver checks the corresponding wheelhouse (`wheelhouse-{cuda_slug}/`) — if a matching `.whl` exists, serves it directly
4. If not found, pypiserver redirects to `pypi.org/simple/{package}/` (transparent fallback)
5. Access log records every request regardless of hit/miss

### Self-learning convergence

1. **Day 1:** job downloads `foo-1.0.tar.gz` via proxy (e.g., through `/cu121/simple/`) — builds from source — takes 5 minutes. Proxy logs the `.tar.gz` download.
2. **Builder** sees `.tar.gz` in logs — runs `pip wheel foo==1.0` for each configured CUDA version — places `foo-1.0+cu118-cp311-cp311-linux_x86_64.whl` in `wheelhouse-cu118/`, `foo-1.0+cu121-...` in `wheelhouse-cu121/`, etc.
3. **Day 2:** job requests `foo` via `/cu121/simple/` — pypiserver serves pre-built wheel from `wheelhouse-cu121/` — instant install.

No curated list. No workflow changes. Cache self-populates from actual usage.

### Cache aging

Delete wheels from the wheelhouse if the package hasn't appeared in access logs for N days (e.g., 30 days). Keeps cache size bounded without manual intervention.

### What to pre-build (optional heavy-packages.txt)

For faster Day-1 performance, optionally seed the wheelhouse with known heavy packages — those that **must be compiled from source** and lack pre-built wheels for the target platform. This list is:
- Small (typically 5-20 packages)
- Stable (doesn't change often)
- Discoverable: grep CI logs for `Building wheel` taking >30 seconds

The self-learning loop will discover and build these automatically, but seeding avoids the initial slow build on first request.

### What NOT to curate

- Pure-Python packages (fetched from PyPI via fallback)
- Packages that ship manylinux wheels on PyPI (fetched from PyPI via fallback)
- Anything that pip/uv can install directly from PyPI without building

### Dual-slot rotation

Same pattern as git-cache-warmer:
- Build wheels into inactive slot (`pypi-cache-b/`)
- Swap symlink atomically when build completes
- pypiserver serves from the symlink target
- Optional startup taint `pypi-cache-not-ready` cleared after first successful build

### Platform considerations

Wheels are platform-specific. The DaemonSet builds on the node it runs on, so architecture (x86_64 vs aarch64) is handled automatically. If multiple Python versions are in use, the build list must cover each version:

```bash
pip3.11 wheel -r heavy-packages.txt -w /mnt/pypi-cache/wheelhouse/
pip3.12 wheel -r heavy-packages.txt -w /mnt/pypi-cache/wheelhouse/
```

### CUDA variant handling

CUDA is fundamentally different from architecture and Python version — those are properties of the *environment* (detectable automatically), while CUDA version is a *build-time choice* that isn't encoded in standard wheel platform tags.

**The problem:** building flash-attn (or any CUDA extension) with three CUDA toolkits produces identically-named wheels:

```
flash_attn-2.5.6-cp311-cp311-linux_x86_64.whl  # built with CUDA 11.8
flash_attn-2.5.6-cp311-cp311-linux_x86_64.whl  # built with CUDA 12.1
flash_attn-2.5.6-cp311-cp311-linux_x86_64.whl  # built with CUDA 12.4
```

Same filename, different contents. A single wheelhouse directory can only hold one. pip has no way to distinguish them because the platform tags are identical.

**The solution:** separate index paths per CUDA version, following the same pattern PyTorch uses for `download.pytorch.org/whl/cu121/`. Each CUDA variant gets its own wheelhouse directory and index endpoint:

```
pypiserver (per node):
  /cu118/simple/  →  wheelhouse-cu118/
  /cu121/simple/  →  wheelhouse-cu121/
  /cu124/simple/  →  wheelhouse-cu124/
  /cpu/simple/    →  wheelhouse-cpu/    (fallthrough to pypi.org)
```

The builder uses PEP 440 local version segments to make filenames unique per CUDA variant (e.g., `flash_attn-2.5.6+cu121-cp311-cp311-linux_x86_64.whl`) and places each wheel in the corresponding wheelhouse directory.

**Builder configuration:** the builder pre-builds CUDA packages for all configured CUDA versions, even if not all are used by every CI job. The cost of extra builds is low (runs in background on NVMe), and it avoids the complexity of trying to auto-detect which CUDA versions are needed. The CUDA version matrix is static config maintained alongside the builder:

```bash
# Builder runs for each CUDA toolkit
for cuda in cu118 cu121 cu124; do
  # Activate the corresponding CUDA toolkit
  pip wheel flash-attn==2.5.6 -w /mnt/pypi-cache/wheelhouse-${cuda}/
done
```

**Self-learning interaction with CUDA:** the self-learning loop still discovers *which packages* need source builds (by detecting `.tar.gz` downloads in proxy access logs). But the CUDA version matrix is static config — access logs don't reveal which CUDA version a job needed, only that a source download occurred. The builder builds discovered packages for all configured CUDA versions.

### CI integration: composite GitHub Action

To make CUDA variant selection transparent for CI developers, a composite action sets the job-wide index URL. CI developers don't need to know about the cache internals — they just declare which CUDA version they need:

```yaml
# .github/actions/pip-cache-cuda/action.yml
name: 'PyPI Cache (CUDA)'
inputs:
  cuda-version:
    description: 'CUDA version (e.g., 11.8, 12.1, 12.4)'
    required: true

runs:
  using: composite
  steps:
    - shell: bash
      run: |
        CUDA_SLUG="cu$(echo '${{ inputs.cuda-version }}' | tr -d '.')"
        echo "PIP_INDEX_URL=http://localhost:8080/${CUDA_SLUG}/simple/" >> "$GITHUB_ENV"
        echo "UV_INDEX_URL=http://localhost:8080/${CUDA_SLUG}/simple/" >> "$GITHUB_ENV"
        echo "PIP_EXTRA_INDEX_URL=https://pypi.org/simple/" >> "$GITHUB_ENV"
```

Usage in workflows:

```yaml
jobs:
  test-cu121:
    steps:
      - uses: ./.github/actions/pip-cache-cuda
        with:
          cuda-version: '12.1'
      # every pip/uv install after this hits the cu121 index automatically
      - run: pip install flash-attn torch
```

`GITHUB_ENV` persists for all subsequent steps in the job — no wrapper scripts, no per-step repetition. Shorthand aliases (e.g., `.github/actions/pip-cache-cuda11`) can wrap the parameterized action for even simpler usage:

```yaml
# .github/actions/pip-cache-cuda11/action.yml
runs:
  using: composite
  steps:
    - uses: ./.github/actions/pip-cache-cuda
      with:
        cuda-version: '11.8'
```

For CPU-only jobs, either use the default `PIP_INDEX_URL` (set via pod env var to the `/cpu/simple/` path), or provide a matching `pip-cache-cpu` action.

### Comparison to git-cache-warmer

| Aspect | git-cache | pypi-cache (proposed) |
|--------|-----------|----------------------|
| DaemonSet | Yes | Yes |
| Storage | NVMe hostPath | NVMe hostPath |
| Dual-slot rotation | Yes | Yes |
| Startup taint | `git-cache-not-ready` | `pypi-cache-not-ready` |
| Transparency mechanism | `GIT_ALTERNATE_OBJECT_DIRECTORIES` | `PIP_INDEX_URL` / `UV_INDEX_URL` |
| Serving | Filesystem (read-only mount) | pypiserver process (localhost HTTP) |
| Fallback | GitHub (network fetch) | PyPI (redirect) |
| What's cached | Git objects | Wheel files (pre-built + downloaded) |
| Self-learning | N/A (repo list is static) | Yes (proxy logs drive builder) |

## Open Questions

1. ~~**Which packages need source builds?** Need to audit CI logs for `Building wheel` entries to populate `heavy-packages.txt`.~~ **Solved:** The self-learning builder detects `.tar.gz` downloads in proxy access logs and automatically builds wheels for them. An optional `heavy-packages.txt` can seed the cache for Day-1 performance but is not required.
2. **Multiple Python versions?** If jobs use different Python versions (3.11, 3.12, etc.), wheels must be built for each. The builder should detect the Python version from the wheel filename in the access log and build with the matching interpreter.
3. ~~**CUDA variants?** If torch or CUDA extensions are built from source, the build may need CUDA toolkit on the node (or use NVIDIA base images in the DaemonSet).~~ **Solved:** Separate index paths per CUDA version (`/cu118/simple/`, `/cu121/simple/`, etc.), each backed by its own wheelhouse directory. The builder pre-builds for all configured CUDA versions. CI jobs select the variant via a composite GitHub Action (`pip-cache-cuda`) that sets `PIP_INDEX_URL`/`UV_INDEX_URL` for the entire job via `GITHUB_ENV`. See "CUDA variant handling" and "CI integration" sections above.
4. ~~**Cache invalidation:** How often to rebuild? On a schedule (daily)? On requirements.txt changes?~~ **Solved:** The builder runs periodically (e.g., every 6h), and unused packages are aged out after N days without access log hits. The dual-slot rotation makes rebuilds safe (old cache serves until new one is ready).
5. ~~**Capture mechanism:** How to discover which packages are actually used without requiring workflow changes?~~ **Solved:** Proxy access logs provide complete, transparent telemetry. See "Transparent Capture" section above.
6. **Builder concurrency:** Should the builder run on every node independently, or should one node build and distribute wheels? Per-node is simpler and avoids cross-node transfer, but wastes CPU if many nodes build the same wheels.
7. **Access log rotation:** The builder parses access logs — need a retention/rotation policy so logs don't grow unbounded. Standard logrotate with the builder processing logs before rotation.

## References

- [pypiserver](https://github.com/pypiserver/pypiserver) — lightweight PyPI server with fallback
- [hauntsaninja/nginx_pypi_cache](https://github.com/hauntsaninja/nginx_pypi_cache) — nginx reverse proxy config for PyPI
- [proxpi](https://github.com/EpicWink/proxpi) — lightweight CI-focused PyPI proxy
- [devpi-server](https://pypi.org/project/devpi-server/) — full PyPI proxy + private index
- [pip caching docs](https://pip.pypa.io/en/stable/topics/caching/)
- [uv caching docs](https://docs.astral.sh/uv/concepts/cache/)
- [uv read-only cache issue #15934](https://github.com/astral-sh/uv/issues/15934)
- [uv pip download issue #2078](https://github.com/astral-sh/uv/issues/2078)
