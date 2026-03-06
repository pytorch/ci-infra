# TODO List - Post-Testing Tasks

## Immediate Actions (Requires tf apply)

- **Fix git-cache service** ✅ DONE (deployed and tested)
  - FINAL SOLUTION: Replaced git-daemon with nginx HTTP server + pre-packaged tarballs
  - ARCHITECTURE: nginx serves pytorch-git.tar.gz (3.9GB), cache-updater refreshes hourly
  - PERFORMANCE: Main repo clone 36s (33% faster than 54s with git-daemon)
  - STATUS: Deployed and working. Optional: extend to submodule tarballs for even more speedup

- **Implement ccache_shared performance fix** ✅ DONE (elastic throughput)
  - ✅ COMPLETED: Switched to EFS Elastic Throughput in efs.tf (line 84)
  - TODO (optional): Add CCACHE_NOSTATS=1 environment variable to shell_env
  - ANALYSIS: See `/tmp/ccache-performance-analysis.md` for full recommendations

## High Priority

- [x] **Auto get-ssh-config in `gpu-dev connect`** ✅ DONE
  - Added to PR #50 (commit c9d0c9a)
  - Added to test/all-fixes-consolidated (commit 54b81af)
  - Auto-downloads SSH config if missing
  - Shows helpful error on auth failure with exact commands

- [x] **Debug /ccache_shared performance issues** ✅ ANALYSIS COMPLETE
  - Detailed analysis at `/tmp/ccache-performance-analysis.md`
  - ROOT CAUSE: EFS baseline throughput only 0.04 MiB/s (250x too slow for ccache)
  - IMMEDIATE FIX: Switch to EFS Elastic Throughput (1-line terraform change)
  - See analysis for full recommendations (NOSTATS, mount optimization, CloudWatch)

- [x] **Add EBS snapshot warm-up PR** ✅ ALREADY INCLUDED
  - PR #39 (commit 1c9f17f) - disk-warmer init container
  - Already in test/all-fixes-consolidated (lines 3704-3714 in Lambda)
  - Pre-warms metadata → critical dirs → remaining files

- [x] **Merge profiling timings PR** ✅ MERGED
  - PR #42: feat/reservation-timing-trace
  - Adds `--trace` flag to show detailed reservation timing
  - Merged into test/all-fixes-consolidated (commit 3db1bd3)
  - Also includes: chown skip optimization (30-40s speedup)

## Testing

- [x] **Test git clone with cache** ✅ TESTED (needs tf apply to fix git-daemon)
  - Created reservation and ran git clone
  - Cache miss detected - git-cache service has broken git-daemon container
  - Clone took 63.65s without cache (baseline established)
  - FIX: Updated git-cache.tf (init creates export-ok files, switched to ubuntu/git with git-daemon package)
  - NEXT: Run `tf apply` to deploy fix, then retest to verify cache works

- [x] **Monitor EFA speed benchmark** ✅ COMPLETED
  - Agent test completed: T4 nodes have EFA but NO RDMA support
  - EFA detected and initialized, but falls back to SENDRECV (copy-based, not RDMA)
  - Performance gain minimal on T4: EFA ~25 Gbps vs TCP ~10-20 Gbps (1.1-1.5x only)
  - Recommendation: Skip EFA on T4, use TCP; need H100+ for meaningful EFA RDMA (30-40x gain)
  - Full report: `/private/tmp/claude-501/-Users-wouterdevriendt-dev-osdc/tasks/a18c1a8332c02c597.output`

## Documentation

- [x] Add-user tested and approved ✅

## Completed
- [x] All 7 PRs tested ✅
- [x] Git-cache fixed and re-enabled ✅
- [x] Add-user test setup ✅
