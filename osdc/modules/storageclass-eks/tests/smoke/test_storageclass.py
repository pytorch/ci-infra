"""Smoke tests for gp3 StorageClass."""

import pytest

pytestmark = [pytest.mark.live]


class TestStorageClass:
    """Verify the gp3 StorageClass is configured as default."""

    def test_gp3_storageclass_exists(self, all_storageclasses):
        sc_names = [sc["metadata"]["name"] for sc in all_storageclasses.get("items", [])]
        assert "gp3" in sc_names, f"StorageClass 'gp3' not found. Available: {sc_names}"

    def test_gp3_is_default(self, all_storageclasses):
        for sc in all_storageclasses.get("items", []):
            if sc["metadata"]["name"] == "gp3":
                annotations = sc.get("metadata", {}).get("annotations", {})
                is_default = annotations.get("storageclass.kubernetes.io/is-default-class", "false")
                assert is_default == "true", "StorageClass 'gp3' is not the default"
                return
        pytest.fail("StorageClass 'gp3' not found")
