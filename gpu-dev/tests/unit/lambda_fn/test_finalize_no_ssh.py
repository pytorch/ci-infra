"""Unit tests for the slow-sshd finalize decision.

Regression for the orphaned-`preparing` bug: a persistent-disk reservation
restores its disk *before* sshd binds, so the readiness poll's log marker never
shows within the window. The main flow used to leave such reservations stuck in
`preparing` forever. It now finalizes anyway (routing is already stored, the SSH
proxy retries) and only fails when the pod itself reports errors.
"""


def test_finalize_when_pod_healthy_but_no_ssh_marker(lambda_index):
    # Running pod, no errors, sshd marker not seen -> finalize anyway.
    info = {"has_errors": False, "display_message": "🚀 Container running — starting SSH service…"}
    assert lambda_index.should_finalize_without_ssh_marker(info) is True


def test_do_not_finalize_when_pod_has_errors(lambda_index):
    info = {"has_errors": True, "display_message": "❌ ImagePullBackOff"}
    assert lambda_index.should_finalize_without_ssh_marker(info) is False


def test_missing_has_errors_key_defaults_to_finalize(lambda_index):
    # Defensive: a partial pod_info dict shouldn't strand the reservation.
    assert lambda_index.should_finalize_without_ssh_marker({}) is True
