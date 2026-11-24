"""Compare ECR manifest config digest with the exact scanned Docker image ID."""
import json
import sys


def check(data, scanned):
    images = data.get("images", [])
    failures = data.get("failures", [])
    if not images:
        if len(failures) == 1 and failures[0].get("failureCode") == "ImageNotFound":
            return "missing"
        raise ValueError("ECR lookup failed; cannot treat as absent")
    if len(images) != 1 or failures:
        raise ValueError("Unexpected ECR response")
    manifest = json.loads(images[0]["imageManifest"])
    if manifest.get("config", {}).get("digest") != scanned:
        raise ValueError("Immutable SHA tag contains a different image; preserve it and use a new source commit")
    return "identical"


if __name__ == "__main__":
    print(check(json.load(open(sys.argv[1])), sys.argv[2]))
