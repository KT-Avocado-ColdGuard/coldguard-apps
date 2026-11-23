#!/usr/bin/env bash
set -euo pipefail
mkdir -p reports
date -u +%FT%TZ > reports/scan-started-at.txt
trivy --version > reports/trivy-version.txt 2>&1
# A timeout or scanner/DB error remains nonzero through tee.
timeout 15m trivy image --timeout 10m --scanners vuln --pkg-types os,library \
  --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL --ignore-unfixed=false \
  --ignorefile /dev/null --config /dev/null --format json \
  --output reports/trivy-full.json --exit-code 0 "$1" 2>&1 | tee reports/trivy-scan.log
