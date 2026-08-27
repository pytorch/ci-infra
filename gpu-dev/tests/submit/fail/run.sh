#!/usr/bin/env bash
# Failure test for `gpu-dev submit`: writes a partial output, then exits 7.
# Verifies the post-run rsync still pulls the partial files even on failure,
# the auto-cancel runs on non-zero exit, and the local exit code is preserved.
set -e

echo "=== host ==="
hostname
date -u

# Write a partial file so we can verify it was synced back
echo "step1 done at $(date -u)" > step1.txt
nvidia-smi -L > gpus-before-fail.txt

# Now error out
echo "About to fail..." > step2.txt
python3 -c "import sys; sys.exit(7)"

# Should not reach here
echo "should-not-appear" > step3.txt
