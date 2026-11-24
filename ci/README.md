# ColdGuard CI

PR to main / main push / workflow_dispatch trigger a GitHub-hosted Ubuntu runner.
Python 3.12 runs `python -m unittest discover -s tests -v` without production connections.
One shared image is built from the new root Dockerfile (none existed in the repository).
Override its command with `python -m collector.data_collector`,
`python -m controller.controller_svc`, or `python -m field_web.field_web`.
No main.py or simulator is started by the image. Runtime credentials, CA and writable state
volumes must be supplied by deployment; CI does not deploy or access any facility.

## Reports and failures
Actions -> ColdGuard CI -> run -> Artifacts -> ci-evidence-*.
Download full Actions logs from the run menu for scanner installation/network errors.
Artifacts include full unfiltered JSON when scan succeeded, gate decision, build/test logs,
source commit, run URL, timestamps, image ID and scanner version. A missing JSON report
is explicitly recorded and never means no vulnerabilities. Trivy uses exit-code 0 only
for report generation; scanner errors still fail the step. The separate Python gate
returns 1 for HIGH/CRITICAL and 2 for invalid inputs. Neither is swallowed.
No ECR or registry digest is claimed. Upload failure also fails CI.

## Exceptions
`ci/security/exceptions.json` starts empty. No example CVE is approved.
Each exception requires id, package, installed_version, target, image_id, approved_by,
approval_issue, reason, mitigation, expires_at (YYYY-MM-DD), services.
Services must contain collector, controller and field-web: this is one shared image.
Expiry begins at 00:00 UTC on the specified date. Changed image IDs require re-review.
Missing approval fields fail closed; expired/other-image entries never suppress findings.
Approval text is evidence, not authentication: security reviewers must approve changes
to workflow/gate/exception files before merge. Configure branch protection and reviewer
requirements with the repository administrator; CI does not change repository settings.

## Validation boundaries
`tests/test_ci_gate.py` uses synthetic findings, not actual CVEs or scanner output.
It tests normal decisions, HIGH/CRITICAL, valid/expired exceptions, scope and invalid reports.
A real Trivy image scan is a separate workflow step. Local unit success does not prove
Docker builds, database download failures or network timeouts have been tested on Actions.
The initial local validation passed 33 tests on Python 3.12; Docker was unavailable locally.
Actual Actions outcomes must be recorded after the PR runs. Do not disable the gate to
make a vulnerable image green. Request individual security approval or update dependencies.

Sources: https://github.com/aquasecurity/trivy-action
https://trivy.dev/docs/latest/configuration/filtering/

Scanner installation and scan stderr/stdout are archived as trivy-install.log and trivy-scan.log.
Trivy v0.76.0 is installed from its release archive with the published SHA256 checked.
Actions are pinned to resolved commit SHAs. The scanner process test uses a fake executable
returning 1 / 124; this does not simulate a real network outage or elapsed timeout.
The exception list is shared because all three services use one image; separate images
will require separate exception lists. Security review before merge is required.

Manual run: after merging the workflow, Actions -> ColdGuard CI -> Run workflow.
Before merge, opening a PR against main triggers validation.

## Image Scan / published GHCR images
Workflow: `.github/workflows/scan.yml` / Actions name: Image Scan.
Every six hours (UTC minute 17), discover all public tags for each of four images.
This re-scans existing tags too, to catch newly disclosed vulnerabilities; it does not
claim instantaneous publication detection or select the newest hash lexicographically.
Manual input is optional: empty scans all tags; a supplied tag must exist on all four images.
Different tags per service work in automatic mode. `ba2937` is accepted.
Discovery follows pagination and fails on network errors / missing tags; more than 256
matrix entries fails explicitly rather than silently skipping images. Maximum parallel jobs: 4.
Scheduled runs require merge to main and may be delayed by GitHub.
Each job records actual registry digest, local image ID and OCI labels and scans the ID.
Audit code commit is not the image source commit; image revision requires publisher labels.
No image publishing / deployment / AWS credentials are used. Exceptions start empty.
Security review is required for policy changes. The shared gate conservatively requires
all three facility-service reviews for exceptions; simulator exceptions require policy extension.
Download published-service-tag-run-attempt artifacts. Source-build CI remains separate.
Local tests: 36 passed. Real Actions build / scan / schedule execution remain unverified.


## 2026-11-24 검토 후 실행 정책 (위 초기 검증 기록보다 우선)
- 루트 Dockerfile의 공통 이미지를 한 번 빌드 / 검사합니다. 기본 CMD는 현장 웹입니다.
- collector: python -m collector.data_collector / controller: python -m controller.controller_svc 로 CMD를 재정의합니다.
- main push / main workflow_dispatch만 OIDC와 ECR 게시를 실행합니다. PR은 검사만 합니다.
- Variables: AWS_REGION / AWS_ACCOUNT_ID / AWS_ROLE_ARN. Secret: DISCORD_WEBHOOK_URL.
- Discord는 main의 신뢰된 스크립트만 사용합니다. 최초 PR에서는 main에 해당 스크립트가 없어 알림이 실패할 수 있습니다.
- Secret이 없거나 알림 전송이 실패하면 알림 job은 실패합니다. 원래 검사 job 결과는 유지합니다.
- ECR immutable SHA 태그가 있으면 manifest의 config digest와 검사한 로컬 image ID를 비교합니다.
  같은 이미지이면 재사용 / 다른 이미지이면 실패합니다. 임의 덮어쓰기나 latest 게시를 하지 않습니다.
  일부 저장소 게시 후 실패해도 동일 이미지 재실행은 나머지를 게시합니다.
  재빌드로 이미지 ID가 달라지면 새 소스 커밋으로 다시 검사해야 합니다. 기존 태그를 지우지 마세요.
- reports/published-images.csv와 published-*.json은 실제 ECR 조회 결과만 기록합니다.
- 정책 / workflow / 예외 파일 변경은 보안 담당 리뷰 및 main 보호 규칙으로 통제해야 합니다.
- 중앙 API / worker 코드가 없으므로 ECR 중앙 저장소 게시 / GitOps 쓰기 / Argo CD 연결은 미구현입니다.
  앱 경로 / 실행 명령 / health / targetPort / GitOps 쓰기 인증과 리뷰 정책 확정 후 연결합니다.
- Ansible 배포 및 운영 MQTT / MySQL 접속은 수행하지 않습니다.

로컬 검증 기록: Windows Python 3.15에서 40개 중 39개 통과 / Linux 전용 1개 건너뜀.
Python 3.12 Linux 컨테이너에서 네트워크 차단 후 40개 모두 통과.
루트 Dockerfile linux/amd64 빌드 성공 (coldguard-ci-review:validation).
실제 Trivy / Actions / ECR / Discord 전송은 미검증. 운영 연결 없음.
ECR 비교는 단일 플랫폼 manifest config digest 기준이며 CI build는 provenance=false를 사용합니다.
