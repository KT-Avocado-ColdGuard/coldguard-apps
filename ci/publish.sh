#!/usr/bin/env bash
# Publish only the local image that passed this run's scan. Never overwrite a SHA tag.
set -euo pipefail
registry="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
scanned=$(cat reports/image-id.txt)
test "$(docker image inspect "coldguard:$GITHUB_SHA" --format '{{.Id}}')" = "$scanned"
trap 'docker logout "$registry" >/dev/null 2>&1 || true' EXIT
aws --version > reports/aws-version.txt 2>&1
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$registry"
printf 'service,image,digest,result\n' > reports/published-images.csv
for service in collector controller field-web central-worker; do
  repo="coldguard-prod-$service"
  image="$registry/$repo:$GITHUB_SHA"
  aws ecr batch-get-image --repository-name "$repo" --image-ids "imageTag=$GITHUB_SHA" > "reports/existing-$service.json"
  mode=$(python ci/ecr_check.py "reports/existing-$service.json" "$scanned")
  if [ "$mode" = missing ]; then
    docker tag "$scanned" "$image"
    docker push "$image" 2>&1 | tee "reports/push-$service.log"
  fi
  aws ecr batch-get-image --repository-name "$repo" --image-ids "imageTag=$GITHUB_SHA" > "reports/published-$service.json"
  test "$(python ci/ecr_check.py "reports/published-$service.json" "$scanned")" = identical
  digest=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["images"][0]["imageId"]["imageDigest"])' "reports/published-$service.json")
  printf '%s,%s,%s,%s\n' "$service" "$image" "$digest" "$mode" >> reports/published-images.csv
done
