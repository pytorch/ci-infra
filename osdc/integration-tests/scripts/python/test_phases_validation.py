"""Unit tests for phases_validation.py (print_report, etc.)."""


class TestPrintReport:
    def setup_method(self):
        from phases_validation import print_report

        self.print_report = print_report

    def test_all_pass(self, capsys):
        workflow_results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "test-cpu", "conclusion": "success"},
                    {"name": "test-gpu", "conclusion": "success"},
                ],
            },
        ]
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "passed"},
        }
        observability = [
            {"name": "Mimir: metrics", "status": "pass", "detail": "up metric found"},
            {"name": "Loki: logs", "status": "pass"},
        ]

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation, observability)

        assert result is True
        out = capsys.readouterr().out
        assert "PASSED" in out
        assert "FAILED" not in out

    def test_workflow_failure_sets_overall_fail(self, capsys):
        workflow_results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "test-cpu", "conclusion": "success"},
                    {"name": "test-gpu", "conclusion": "failure"},
                ],
            },
        ]
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "passed"},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation, [])

        assert result is False

    def test_validation_failure_sets_overall_fail(self, capsys):
        workflow_results = []
        validation = {
            "smoke": {"status": "failed"},
            "compactor": {"status": "skipped"},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation, [])

        assert result is False

    def test_observability_failure_sets_overall_fail(self, capsys):
        workflow_results = []
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "passed"},
        }
        observability = [
            {"name": "Mimir: metrics", "status": "fail"},
        ]

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation, observability)

        assert result is False

    def test_skipped_validation_does_not_fail(self, capsys):
        workflow_results = []
        validation = {
            "smoke": {"status": "skipped"},
            "compactor": {"status": "skipped"},
        }
        observability = [
            {"name": "Loki: logs", "status": "skip"},
        ]

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation, observability)

        assert result is True

    def test_failure_log_printed(self, capsys):
        workflow_results = [
            {
                "run_id": 42,
                "failure_log": "Error: something broke\nLine 2",
                "jobs": [{"name": "build", "conclusion": "failure"}],
            },
        ]
        validation = {"smoke": {"status": "passed"}, "compactor": {"status": "passed"}}

        self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation, [])

        out = capsys.readouterr().out
        assert "something broke" in out
        assert "run 42" in out
