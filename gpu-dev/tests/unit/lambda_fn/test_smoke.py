"""Smoke test: the lambda imports under the conftest env + boto3 mocks work."""

def test_lambda_imports(lambda_index):
    assert hasattr(lambda_index, "reconcile_warm_pool")
    assert hasattr(lambda_index, "try_claim_warm_pod")

def test_warm_targets_loaded(lambda_index):
    assert lambda_index.WARM_POOL_TARGETS  # from conftest env JSON

def test_aws_mocks_fixture(aws_mocks, lambda_index):
    assert "dynamodb" in aws_mocks
    assert lambda_index.dynamodb is aws_mocks["dynamodb"]
