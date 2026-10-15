#!/usr/bin/env bash
set -euo pipefail

task_root="${RUNNER_TEMP:?}/sigill-diagnostic"
image='ghcr.io/osgeo/gdal-deps@sha256:8acd01bda60d97c80dabec704f2e06fc1a6d8b7a301b165248e560d51525dc98'
candidate='58884e798d769b844449af94e0aac74456f26c58'
parent='5767f0e852f8ee4788808d7aa3b1e8933736291d'
container_name="sigill-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}"
mkdir -p "$task_root/artifacts" "$task_root/inputs" "$task_root/source" \
    "$PWD/.ccache"

finalize() {
    local exit_status=$?
    trap - EXIT
    docker rm --force "$container_name" > "$task_root/artifacts/container-cleanup.log" 2>&1 || true
    python3 .github/workflows/sigill_diagnostic/probe.py \
        --manifest "$task_root/artifacts" --driver-status "$exit_status"
    exit "$exit_status"
}
trap finalize EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

printf '%s\n' "$image" > "$task_root/artifacts/requested-image.txt"
git rev-parse HEAD > "$task_root/artifacts/harness-commit.txt"
git diff --binary > "$task_root/artifacts/harness-working-tree.patch"
uname -a > "$task_root/artifacts/host-uname.txt"
lscpu > "$task_root/artifacts/host-lscpu.txt"
cc -march=native -### -E - < /dev/null \
    > "$task_root/artifacts/host-native-target.txt" 2>&1
for revision in "$candidate" "$parent"; do
    test "$(git rev-parse "$revision^{commit}")" = "$revision"
    git archive --format=tar "$revision" > "$task_root/inputs/$revision.tar"
    git rev-parse "$revision^{tree}" > "$task_root/artifacts/$revision.tree"
done

docker pull "$image" > "$task_root/artifacts/image-pull.log" 2>&1
docker image inspect "$image" > "$task_root/artifacts/image-inspect.json"

# One container retains the same CPU, compiler, dependencies, and paths for all arms.
# gdb is added before any build. The probe rejects upgrades of existing packages.
docker run --name "$container_name" --rm --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    --mount "type=bind,source=$PWD,target=/harness,readonly" \
    --mount "type=bind,source=$task_root,target=/diagnostic" \
    --mount "type=bind,source=$task_root/source,target=$PWD" \
    --mount "type=bind,source=$PWD/.ccache,target=/.ccache" \
    --workdir "$PWD" \
    -e "SIGILL_SOURCE_ROOT=$PWD" \
    -e "SIGILL_CACHE_HIT=${SIGILL_CACHE_HIT:-}" \
    -e "SIGILL_CACHE_MATCHED_KEY=${SIGILL_CACHE_MATCHED_KEY:-}" \
    -e "SIGILL_HARNESS_COMMIT=$(git rev-parse HEAD)" \
    -e DEBUGINFOD_URLS= \
    "$image" python3 /harness/.github/workflows/sigill_diagnostic/probe.py \
    > "$task_root/artifacts/probe-console.log" 2>&1
