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
