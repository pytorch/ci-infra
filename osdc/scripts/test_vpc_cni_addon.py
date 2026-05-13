"""Unit tests for the vpc-cni EKS addon terraform configuration (PR 7).

Defends:
- Env shape (4 required keys, JSON-string values, no HCL booleans/numbers)
- ENI_CONFIG_LABEL_DEF stays in sync with scripts/python/cni_constants.py:ENI_CONFIG_LABEL
  AND with base/scripts/bootstrap/eks-base-pre-nodeadm-az-label.sh
- resolve_conflicts_on_update = "OVERWRITE" (cutover gate)
- configuration_values uses literal jsonencode({...}) (no refactor-to-locals false-pass)
- addon_version on a major series that supports Custom Networking
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_TF = REPO_ROOT / "modules" / "eks" / "terraform" / "modules" / "eks" / "main.tf"
BOOTSTRAP_SH = REPO_ROOT / "base" / "scripts" / "bootstrap" / "eks-base-pre-nodeadm-az-label.sh"

# Make sibling helper module importable regardless of pytest invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))
from _tf_parse_helpers import (  # noqa: E402
    env_block as _env_block,
)
from _tf_parse_helpers import (  # noqa: E402
    resource_block as _resource_block,
)
from _tf_parse_helpers import (  # noqa: E402
    strip_double_quoted_strings as _strip_double_quoted_strings,
)
from cni_constants import ENI_CONFIG_LABEL  # noqa: E402

MAIN_TF_TEXT = MAIN_TF.read_text()
BOOTSTRAP_SH_TEXT = BOOTSTRAP_SH.read_text()

EXPECTED_ENV: dict[str, str] = {
    "AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG": "true",
    "ENABLE_PREFIX_DELEGATION": "true",
    "ENI_CONFIG_LABEL_DEF": ENI_CONFIG_LABEL,
    "WARM_PREFIX_TARGET": "1",
}


VPC_CNI_BLOCK = _resource_block(MAIN_TF_TEXT, "aws_eks_addon", "vpc_cni")
VPC_CNI_ENV_BLOCK = _env_block(VPC_CNI_BLOCK)


class TestVPCCNIAddonConfiguration:
    """The aws_eks_addon.vpc_cni resource is the cutover gate for Custom Networking (PR 7).

    Defends env shape, drift between cni_constants.py / bootstrap script / main.tf,
    and the OVERWRITE conflict-resolution mode. See INCREASE_IPV4.md PR 7.
    """

    block = VPC_CNI_BLOCK
    env_block = VPC_CNI_ENV_BLOCK

    def test_overwrite_conflict_mode(self) -> None:
        assert re.search(r'resolve_conflicts_on_update\s*=\s*"OVERWRITE"', self.block), (
            'aws_eks_addon.vpc_cni must set resolve_conflicts_on_update = "OVERWRITE" to force '
            "Custom Networking activation during the IPv4 cutover. See INCREASE_IPV4.md PR 7."
        )

    def test_uses_jsonencode_literal(self) -> None:
        assert re.search(r"configuration_values\s*=\s*jsonencode\(", self.block), (
            "aws_eks_addon.vpc_cni configuration_values must use a literal jsonencode({...}) call "
            "so the env-block content checks in this test file actually inspect the live spec. "
            "Refactoring to a local variable would silently bypass these guards."
        )

    def test_addon_version_supports_custom_networking(self) -> None:
        m = re.search(r'addon_version\s*=\s*"v1\.(\d+)\.', self.block)
        assert m, "aws_eks_addon.vpc_cni must declare a v1.x addon_version"
        minor = int(m.group(1))
        assert minor >= 20, (
            f"aws_eks_addon.vpc_cni addon_version must be >= v1.20 series. "
            f"Custom Networking floor is v1.18 but downgrades below v1.20 are "
            f"out of scope; declared v1.{minor}.x. See INCREASE_IPV4.md PR 7."
        )

    def test_all_four_env_keys_present(self) -> None:
        for key, value in EXPECTED_ENV.items():
            pattern = rf'{re.escape(key)}\s*=\s*"{re.escape(value)}"'
            assert re.search(pattern, self.env_block), (
                f"aws_eks_addon.vpc_cni env block missing required key/value: "
                f'{key} = "{value}". See INCREASE_IPV4.md PR 7.'
            )

    def test_env_values_are_json_strings(self) -> None:
        """Every env RHS must be a double-quoted string. HCL booleans / numbers
        leak through jsonencode as JSON booleans / numbers, which the AWS VPC CNI
        addon rejects (env values must be strings).
        """
        for line in self.env_block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$", stripped)
            if not m:
                continue
            key, rhs = m.group(1), m.group(2).rstrip(",")
            msg = (
                f"aws_eks_addon.vpc_cni env value for {key} must be a JSON string "
                f"(quoted), got `{rhs}`. HCL bool/number leaks through jsonencode and "
                "the addon rejects non-string env values. See INCREASE_IPV4.md PR 7."
            )
            assert rhs.startswith('"'), msg
            assert rhs.endswith('"'), msg

    def test_no_extra_env_keys(self) -> None:
        """Snapshot guard — fail loud if a 5th env key sneaks in unreviewed."""
        keys_found = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", self.env_block, re.MULTILINE))
        unexpected = keys_found - set(EXPECTED_ENV)
        assert not unexpected, (
            f"aws_eks_addon.vpc_cni env block has unexpected keys {sorted(unexpected)}. "
            f"Expected exactly {sorted(EXPECTED_ENV)}. Update EXPECTED_ENV in this test file "
            "if the new key is intentional. See INCREASE_IPV4.md PR 7."
        )
        missing = set(EXPECTED_ENV) - keys_found
        assert not missing, f"aws_eks_addon.vpc_cni env block missing expected keys {sorted(missing)}."

    def test_no_var_or_local_references_in_env(self) -> None:
        """Defend against a refactor that moves env values to var.* or local.* — that
        would silently bypass the literal-string assertions in this file.
        """
        scrubbed = _strip_double_quoted_strings(self.env_block)
        assert "var." not in scrubbed, (
            "aws_eks_addon.vpc_cni env block must not contain `var.` references. "
            "Hoisting env values to variables bypasses the literal-content guards "
            "in this test file. See INCREASE_IPV4.md PR 7."
        )
        assert "local." not in scrubbed, (
            "aws_eks_addon.vpc_cni env block must not contain `local.` references. "
            "Hoisting env values to locals bypasses the literal-content guards "
            "in this test file. See INCREASE_IPV4.md PR 7."
        )

    def test_eni_config_label_def_matches_python_constant(self) -> None:
        pattern = rf'ENI_CONFIG_LABEL_DEF\s*=\s*"{re.escape(ENI_CONFIG_LABEL)}"'
        assert re.search(pattern, self.env_block), (
            f"aws_eks_addon.vpc_cni ENI_CONFIG_LABEL_DEF must equal the value of "
            f"scripts/python/cni_constants.py:ENI_CONFIG_LABEL ({ENI_CONFIG_LABEL!r}). "
            "These two literals must move together to keep aws-node and the per-node "
            "kubelet labels referencing the same ENIConfig CRs. See INCREASE_IPV4.md PR 7."
        )


class TestENIConfigLabelLockstep:
    """The literal `ipam.osdc.internal/eni-config` must appear in lockstep across
    cni_constants.py, the bootstrap script, and main.tf. This class covers the
    bootstrap-script site; the Python <-> main.tf check is in the class above.
    """

    def test_label_in_bootstrap_script(self) -> None:
        # Strip whole-line comments so a comment-only mention of the label can't
        # silently satisfy this check while the actual --node-labels= line is
        # commented out or removed.
        non_comment_lines = "\n".join(line for line in BOOTSTRAP_SH_TEXT.splitlines() if not re.match(r"^\s*#", line))
        assert ENI_CONFIG_LABEL in non_comment_lines, (
            f"base/scripts/bootstrap/eks-base-pre-nodeadm-az-label.sh must contain the literal "
            f"{ENI_CONFIG_LABEL!r} OUTSIDE comments (the kubelet --node-labels= key written at "
            "first boot). Comment-only references are insufficient. If you changed "
            "cni_constants.py:ENI_CONFIG_LABEL, update the bootstrap script too. "
            "See INCREASE_IPV4.md PR 7."
        )
        # Stronger: assert the label appears in a --node-labels= context, not
        # just any non-comment line (e.g. a YAML key in an unrelated drop-in).
        assert f"--node-labels={ENI_CONFIG_LABEL}" in non_comment_lines, (
            f"base/scripts/bootstrap/eks-base-pre-nodeadm-az-label.sh must contain "
            f"--node-labels={ENI_CONFIG_LABEL}=... — the bootstrap script must apply "
            "this label via kubelet --node-labels (not just document or YAML-reference it). "
            "See INCREASE_IPV4.md PR 7."
        )
