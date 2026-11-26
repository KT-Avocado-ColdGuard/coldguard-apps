import json
import os
import sys
import urllib.error
import urllib.request


def build_message(env):
    result = env["CI_RESULT"]
    publishes = env["REF"] == "refs/heads/main" and env["EVENT_NAME"] != "pull_request"
    if result == "success":
        title = "✅ CI 검사 완료"
        status = "테스트 / Trivy 통과 및 ECR 게시 완료" if publishes else "테스트 / 빌드 / Trivy 검사 통과 — PR에서는 ECR에 게시하지 않음"
    elif result == "cancelled":
        title, status = "⚪ CI 검사 취소", "실행이 취소되었습니다. Actions에서 취소 사유를 확인해 주세요."
    elif result == "skipped":
        title, status = "⚪ CI 검사 미실행", "실행 조건에 따라 검사가 생략되었습니다."
    else:
        title, status = "❌ CI 검사 실패", "검사 또는 게시·배포 버전 기록 단계가 실패했습니다. Actions 로그를 확인해 주세요."
    return (
        f"## {title}\n"
        f"**{status}**\n\n"
        f"- 브랜치: `{env['REF']}`\n"
        f"- 커밋 SHA: `{env['SOURCE_SHA']}`\n"
        f"- 실행 결과: <{env['RUN_URL']}>\n\n"
        "※ 이 알림은 CI 결과이며 실제 서비스 배포 완료 알림이 아닙니다."
    )


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        print("::warning::Discord notification unavailable: webhook is not configured.")
        return 1
    request = urllib.request.Request(
        webhook,
        data=json.dumps({
            "content": build_message(os.environ),
            "allowed_mentions": {"parse": []},
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "ColdGuard-CI"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print(f"Discord notification sent: HTTP {response.status}")
        return 0
    except urllib.error.HTTPError as exc:
        print(f"Discord notification failed: HTTP {exc.code}", file=sys.stderr)
    except urllib.error.URLError:
        print("Discord notification failed: network error", file=sys.stderr)
    except TimeoutError:
        print("Discord notification failed: timeout", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
