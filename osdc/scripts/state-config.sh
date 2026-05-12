#!/usr/bin/env bash
# shellcheck disable=SC2034
#
# Source this to obtain the OpenTofu remote-state backend region.
#
# Single source of truth for the AWS region that hosts the shared state
# bucket(s) and the DynamoDB lock table — independent of each cluster's
# own region. Centralizing it here means a future state-region migration
# is a one-file edit instead of a sprawling search-and-replace across
# every justfile recipe and module deploy script.
#
# Usage (from a justfile shebang recipe):
#   source "{{UPSTREAM}}/scripts/state-config.sh"
#
# Usage (from a standalone shell script that already knows OSDC_UPSTREAM):
#   source "$OSDC_UPSTREAM/scripts/state-config.sh"
#
# Variables exported:
#   STATE_REGION  — AWS region of the tofu state bucket + lock table
export STATE_REGION="us-west-2"
