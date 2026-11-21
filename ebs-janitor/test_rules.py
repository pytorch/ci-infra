from datetime import UTC, datetime, timedelta

import pytest

from rules import is_leaked_runner_root

NOW = datetime(2026, 11, 3, tzinfo=UTC)
OLD = NOW - timedelta(days=30)
# Real tag sets and AMI names from the fbossci inventory (2026-11-03).
LEGACY_TAGS = {"Environment": "x", "InstanceManagement": "x", "Project": "x"}
LINUX_X86 = "al2023-ami-2023.10.20260302.1-kernel-6.1-x86_64"
LINUX_ARM = "al2023-ami-2023.12.20260803.3-kernel-6.1-arm64"
WINDOWS = "Windows 2019 GHA CI - 20261002003950"


@pytest.mark.parametrize(
    "tags,ami",
    [
        ({**LEGACY_TAGS, "Name": "gh-ci-action-runner"}, WINDOWS),  # pytorch Windows
        ({**LEGACY_TAGS, "Name": "meta-pytorch-action-runner"}, LINUX_X86),  # meta-pytorch Linux
        ({**LEGACY_TAGS, "Name": "gh-ci-action-runner"}, LINUX_ARM),  # leftover pytorch Linux
        ({}, LINUX_X86),  # untagged, like the backlog from before test-infra#7930
        ({}, WINDOWS),
    ],
)
def test_leaked_runner_roots_match(tags, ami):
    assert is_leaked_runner_root(tags, OLD, ami, NOW)


@pytest.mark.parametrize(
    "tags,created,ami",
    [
        ({}, NOW - timedelta(days=1), LINUX_X86),  # too new
        ({}, OLD, None),  # source AMI can't be resolved
        ({}, OLD, "my-custom-ami"),  # not a runner AMI
        ({"Name": "someones-scratch"}, OLD, LINUX_X86),  # runner AMI, not a runner volume
        ({"gpu-dev-user": "someone@example.com"}, OLD, LINUX_X86),  # tagged, no runner Name
    ],
)
def test_other_volumes_do_not_match(tags, created, ami):
    assert not is_leaked_runner_root(tags, created, ami, NOW)
