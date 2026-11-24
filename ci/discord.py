import json
import os
import sys
import urllib.error
import urllib.request


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        print("::warning::Discord notification unavailable: webhook is not configured.")
        return 1

    result = os.environ["CI_RESULT"]
    event = os.environ["EVENT_NAME"]
    ref = os.environ["REF"]
    sha = os.environ["SOURCE_SHA"]
    run_url = os.environ["RUN_URL"]

    publishes = ref == "refs/heads/main" and event != "pull_request"

    if result == "success":
        status = (
            "테스트 / Trivy 통과 및 ECR 게시 완료"
            if publishes
            else "테스트 / 빌드 / Trivy 검사 통과"
        )
    else:
        status = f"CI 결과: {result} / Actions 로그 확인 필요"

    message = (
        f"**ColdGuard CI**\n"
        f"{status}\n"
        f"브랜치: {ref}\n"
        f"커밋: {sha}\n"
        f"실행 결과: {run_url}\n"
        "이 알림은 CI 결과이며 실제 서비스 배포 완료 알림은 아닙니다."
    )

    request = urllib.request.Request(
        webhook,
        data=json.dumps({
            "content": message,
            "allowed_mentions": {"parse": []},
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ColdGuard-CI",
        },
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
