#!/usr/bin/env bash
set -euo pipefail
mkdir -p reports
exec > >(tee reports/pull.log) 2>&1
[[ "$IMAGE_TAG" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]] || { echo 'Invalid image tag'; exit 2; }
case "$SERVICE" in simulator|collector|controller|field-web) ;; *) exit 2;; esac
ref="ghcr.io/kt-avocado-coldguard/coldguard-$SERVICE:$IMAGE_TAG"
printf '%s\n' "$ref" > reports/requested-image.txt
docker pull --platform linux/amd64 "$ref"
docker image inspect "$ref" > reports/image-inspect.json
docker image inspect "$ref" --format '{{.Id}}' > reports/image-id.txt
docker image inspect "$ref" --format '{{json .RepoDigests}}' > reports/registry-digests.json
docker image inspect "$ref" --format '{{json .Config.Labels}}' > reports/image-labels.json
