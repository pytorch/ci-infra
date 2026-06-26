"""Guard: the bin-pack-scheduler image minor must track clusters.yaml eks_version.

kube-scheduler tolerates only +/-1 minor skew from the API server (the EKS
control-plane version). This pure-logic test fails loudly if someone bumps
eks_version without bumping the scheduler image (or vice-versa), so the two
can't silently diverge across an EKS upgrade. Runs in `just test` — no cluster.
"""

from pathlib import Path

import yaml

_OSDC_ROOT = Path(__file__).resolve().parents[4]
_DEPLOYMENT = _OSDC_ROOT / "modules" / "bin-pack-scheduler" / "kubernetes" / "deployment.yaml"
_CLUSTERS = _OSDC_ROOT / "clusters.yaml"


def _scheduler_image_minor():
    """Return the major.minor of the kube-scheduler container image (e.g. '1.35')."""
    doc = yaml.safe_load(_DEPLOYMENT.read_text())
    containers = doc["spec"]["template"]["spec"]["containers"]
    image = next(c["image"] for c in containers if c["name"] == "kube-scheduler")
    tag = image.rsplit(":", 1)[1].lstrip("v")  # "v1.35.0" -> "1.35.0"
    return ".".join(tag.split(".")[:2])  # -> "1.35"


def test_scheduler_image_minor_matches_eks_version():
    eks_version = yaml.safe_load(_CLUSTERS.read_text())["defaults"]["eks_version"]
    image_minor = _scheduler_image_minor()
    msg = (
        f"bin-pack-scheduler image minor is {image_minor!r} but clusters.yaml "
        f"eks_version is {eks_version!r}; bump them together on EKS upgrade."
    )
    assert image_minor == eks_version, msg
