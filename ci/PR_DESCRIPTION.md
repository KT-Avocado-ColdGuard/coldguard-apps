# 공통 이미지 CI / Trivy 차단 정책

collector / controller / field-web의 공통 이미지를 한 번 빌드하고 예외 전 전체 등급 보고서 저장 후 HIGH / CRITICAL을 차단합니다. 미수정 취약점도 포함하며 예외 목록은 비어 있습니다. 검사 오류는 실패 상태를 유지하고 생성된 보고서 / 오류 로그를 artifact로 보관합니다.

- main 대상 PR / main push / 수동 실행
- Python 3.12 / 읽기 권한 / GitHub 제공 Ubuntu 실행기
- 기존 저장소에 Dockerfile이 없어 공통 실행 모듈을 유지하는 Dockerfile 추가
- AWS 인증 / ECR / GitOps / Argo CD / Ansible / 운영 VM 작업 없음
- 예외 변경과 차단 설정 변경은 보안 담당 검토 후 병합 필요

검증: 로컬 Python 3.12 단위 테스트 33개 통과 / bash 구문 검사 / git diff --check 통과.
모의 정책 시험: 정상 / HIGH 및 CRITICAL / 유효 예외 / 만료 예외 / 범위 불일치 / 잘못된 보고서 / 검사 실패 및 타임아웃 반환 코드.
미검증: 실제 Docker 빌드 / 실제 이미지 Trivy 스캔 / GitHub Actions 실행 및 artifact 업로드. 로컬에 Docker가 없고 GitHub 연결의 브랜치 생성 권한이 403으로 거부됨.
