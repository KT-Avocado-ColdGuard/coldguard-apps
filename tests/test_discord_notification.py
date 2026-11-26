import importlib.util
from pathlib import Path
import unittest


def load_script():
    path = Path(__file__).resolve().parents[1] / SCRIPT_PATH
    spec = importlib.util.spec_from_file_location("notification_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT_PATH = "ci/discord.py"
app = load_script()


class NotificationTests(unittest.TestCase):
    def env(self, result="success", event="push"):
        return {"CI_RESULT": result, "EVENT_NAME": event, "REF": "refs/heads/main",
                "SOURCE_SHA": "a" * 40, "RUN_URL": "https://github.com/example/repo/actions/runs/1"}

    def test_main_success(self):
        message = app.build_message(self.env())
        self.assertIn("✅ CI 검사 완료", message)
        self.assertIn("ECR 게시 완료", message)
        self.assertIn("테스트 / 빌드 / Trivy 검사 및 ECR 게시 완료", message)
        self.assertIn("소스 커밋 SHA", message)
        self.assertNotIn("SHA256", message)

    def test_pr_never_claims_publication(self):
        message = app.build_message(self.env(event="pull_request"))
        self.assertNotIn("ECR 게시 완료", message)
        self.assertIn("게시하지 않음", message)

    def test_manual_non_main_run_does_not_claim_to_be_pr(self):
        env = self.env(event="workflow_dispatch")
        env["REF"] = "refs/heads/ci/discord-alerts"
        message = app.build_message(env)
        self.assertIn("이 실행에서는 ECR에 게시하지 않음", message)
        self.assertNotIn("PR에서는", message)
        self.assertNotIn("ECR 게시 완료", message)

    def test_workflow_displays_pr_head_sha_with_run_sha_fallback(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}", workflow)

    def test_failure_never_claims_deployment_success(self):
        message = app.build_message(self.env("failure"))
        self.assertIn("❌ CI 검사 실패", message)
        self.assertNotIn("✅", message)

    def test_cancelled_and_skipped_are_distinct(self):
        self.assertIn("검사 취소", app.build_message(self.env("cancelled")))
        self.assertIn("검사 미실행", app.build_message(self.env("skipped")))

    def test_link_preview_is_disabled(self):
        self.assertIn("<https://github.com/", app.build_message(self.env()))


if __name__ == "__main__":
    unittest.main()
