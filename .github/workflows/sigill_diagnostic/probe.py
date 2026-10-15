#!/usr/bin/env python3
"""Private, fixed-revision native SIGILL comparison, with retained evidence."""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
from pathlib import Path

CANDIDATE = "58884e798d769b844449af94e0aac74456f26c58"
PARENT = "5767f0e852f8ee4788808d7aa3b1e8933736291d"
TESTS = ("test-sse2", "test-sse2-emulation", "test-float16", "test-copy-words")
TARGETS = ("testsse2", "testsse2_emulation", "testfloat16", "testcopywords")
TEST_PATTERN = "^(" + "|".join(TESTS) + ")$"
ARTIFACTS = Path("/diagnostic/artifacts")


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def hash_files(root, paths, output):
    entries = []
    for path in sorted(set(paths)):
        if path.is_file():
            entries.append(
                {"path": str(path.relative_to(root)), "sha256": digest(path)}
            )
    write_json(output, entries)


def command(argv, output, cwd=None, env=None, timeout=120):
    with output.open("w") as log:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            status = process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            status = process.wait()
            timed_out = True
    return {
        "argv": [str(arg) for arg in argv],
        "returncode": status,
        "timeout": timed_out,
        "signal": signal.Signals(-status).name if status < 0 else None,
        "passed": status == 0 and not timed_out,
    }


def require(result):
    if not result["passed"]:
        raise RuntimeError("Command failed: " + repr(result))


def packages():
    output = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\n"], text=True
    )
    return dict(line.split("=", 1) for line in output.splitlines())


def setup():
    before = packages()
    write_json(ARTIFACTS / "packages-before-debugger.json", before)
    if not shutil.which("gdb"):
        require(
            command(["apt-get", "update"], ARTIFACTS / "apt-update.log", timeout=180)
        )
        require(
            command(
                [
                    "apt-get",
                    "install",
                    "-y",
                    "--no-upgrade",
                    "--no-install-recommends",
                    "gdb",
                ],
                ARTIFACTS / "debugger-install.log",
                timeout=180,
            )
        )
    after = packages()
    write_json(ARTIFACTS / "packages-after-debugger.json", after)
    changed = {
        key: [value, after.get(key)]
        for key, value in before.items()
        if after.get(key) != value
    }
    write_json(ARTIFACTS / "existing-package-changes.json", changed)
    if changed:
        raise RuntimeError("Debugger installation changed existing package versions")
    for name, argv in (
        ("uname", ["uname", "-a"]),
        ("lscpu", ["lscpu"]),
        ("gcc-version", ["gcc", "-v"]),
        ("gxx-version", ["g++", "-v"]),
        ("gdb-version", ["gdb", "--version"]),
        ("cmake-version", ["cmake", "--version"]),
        ("ccache-version", ["ccache", "--version"]),
        ("native-features", ["gcc", "-march=native", "-Q", "--help=target"]),
        (
            "native-expansion",
            ["gcc", "-march=native", "-###", "-E", "-x", "c", "/dev/null"],
        ),
    ):
        require(command(argv, ARTIFACTS / (name + ".txt")))
    shutil.copy2("/proc/cpuinfo", ARTIFACTS / "cpuinfo.txt")
    shutil.copy2("/etc/os-release", ARTIFACTS / "os-release.txt")
    tool_paths = [
        Path(shutil.which(name)).resolve()
        for name in ("gcc", "g++", "ld", "ccache", "cmake", "gdb")
    ]
    hash_files(Path("/"), tool_paths, ARTIFACTS / "tool-hashes.json")


def cache_hits(path):
    text = path.read_text()
    matches = re.findall(r"cache hit \((?:direct|preprocessed)\)\s+(\d+)", text)
    return sum(map(int, matches)) if matches else None


def retain_build(build, output):
    if not build.exists():
        return
    for name in ("CMakeCache.txt", "compile_commands.json"):
        if (build / name).is_file():
            shutil.copy2(build / name, output / name)
    paths = [
        path
        for path in build.rglob("*")
        if path.suffix in (".o", ".a", ".gch", ".pch", ".h", ".hpp")
        or ".so" in path.name
        or path.name in ("flags.make", "link.txt")
    ]
    hash_files(build, paths, output / "build-file-hashes.json")
    binaries = output / "binaries"
    binaries.mkdir(exist_ok=True)
    for path in list(build.glob("libgdal.so*")) + [
        build / "autotest/cpp" / name for name in TARGETS
    ]:
        if path.is_file() and not path.is_symlink():
            shutil.copy2(path, binaries / path.name)
    hash_files(binaries, binaries.iterdir(), output / "binary-hashes.json")


def inspect_libraries(binary, build, output, env, require_gdal):
    expected = (build / "libgdal.so").resolve(strict=True)
    record = {
        "expected_gdal": {"path": str(expected), "sha256": digest(expected)},
        "status": "incomplete",
    }
    try:
        require(
            command(["readelf", "-d", str(binary)], output.with_suffix(".dynamic.txt"))
        )
        needed = re.findall(
            r"\(NEEDED\).*Shared library: \[([^\]]+)\]",
            output.with_suffix(".dynamic.txt").read_text(),
        )
        record["needed"] = needed
        needs_gdal = any(name.startswith("libgdal.so") for name in needed)
        require(command(["ldd", str(binary)], output.with_suffix(".ldd.txt"), env=env))
        linked = output.with_suffix(".ldd.txt").read_text()
        if "not found" in linked:
            raise RuntimeError("ldd reports an unresolved runtime dependency")
        paths = [Path(path) for path in re.findall(r"(?:=>\s+)?(/[^\s]+)\s+\(", linked)]
        hash_files(Path("/"), paths, output.with_suffix(".libraries.json"))
        gdal_paths = re.findall(r"libgdal\.so[^\s]*\s+=>\s+(/[^\s]+)", linked)
        observed = [
            {"path": str(Path(path).resolve(strict=True)), "sha256": digest(Path(path))}
            for path in gdal_paths
        ]
        record["observed_gdal"] = observed
        if (require_gdal or needs_gdal) and not observed:
            raise RuntimeError(
                "Required libgdal is absent from resolved runtime dependencies"
            )
        if any(item != record["expected_gdal"] for item in observed):
            raise RuntimeError(
                "Runtime libgdal path/hash differs from this phase's fresh build"
            )
        record["status"] = "verified" if observed else "no-libgdal-DT_NEEDED"
    except Exception as error:
        record.update(status="failed", error=repr(error))
        raise
    finally:
        write_json(output.with_suffix(".gdal-provenance.json"), record)


def debug_fault(test, cwd, env, output):
    commands = (
        "set pagination off",
        "set confirm off",
        "handle SIGILL stop print nopass",
        "run",
        "info program",
        "p $_siginfo",
        "info registers",
        "x/32xb $pc",
        "x/16i $pc",
        "disassemble /r $pc-32,$pc+64",
        "thread apply all bt full",
        "info sharedlibrary",
        "info proc mappings",
    )
    argv = ["gdb", "--batch", "--quiet", "--nx"]
    for value in commands:
        argv.extend(["-ex", value])
    argv.extend(["--args"] + test["command"])
    result = command(argv, output, cwd=cwd, env=env, timeout=90)
    result["sigill_observed"] = "received signal SIGILL" in output.read_text()
    mapped = [
        Path(line.split()[-1])
        for line in output.read_text().splitlines()
        if line.split() and line.split()[-1].startswith("/")
    ]
    hash_files(Path("/"), mapped, output.with_suffix(".mapped-files.json"))
    return result


def run_phase(label, revision, source, result):
    output = ARTIFACTS / label
    output.mkdir()
    result.update(
        label=label,
        revision=revision,
        status="incomplete",
        expected_tests=list(TESTS),
        tests=[],
    )
    write_json(output / "result.json", result)
    build = source / "build-ubuntu_20.04"
    cache = (
        Path("/.ccache")
        if label == "warm-candidate"
        else Path("/diagnostic") / (label + "-cache")
    )
    cache.mkdir(exist_ok=True)
    if label != "warm-candidate" and any(cache.iterdir()):
        raise RuntimeError("Cold cache must start empty")
    result["initial_cache_files"] = len(list(cache.rglob("*")))
    hash_files(cache, cache.rglob("*"), output / "initial-cache-hashes.json")
    env = dict(
        os.environ,
        CCACHE_DIR=str(cache),
        CCACHE_LOGFILE=str(output / "ccache.log"),
        DEBUGINFOD_URLS="",
    )
    for child in source.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    archive = Path("/diagnostic/inputs") / (revision + ".tar")
    result["archive_sha256"] = digest(archive)
    # These archives come only from the two fixed, locally verified Git commits.
    with tarfile.open(archive) as stream:
        stream.extractall(source)
    result["source_loaded"] = True
    hash_files(source, source.rglob("*"), output / "source-file-hashes.json")
    # Preserve the exact Git version/date generation without a second checkout.
    require(command(["git", "init", "--quiet", str(source)], output / "git-init.log"))
    (source / ".git/objects/info/alternates").write_text("/harness/.git/objects\n")
    require(
        command(
            ["git", "reset", "--mixed", revision], output / "git-reset.log", cwd=source
        )
    )
    require(
        command(
            ["git", "diff", "--quiet", "HEAD", "--"],
            output / "git-clean.log",
            cwd=source,
        )
    )
    build.mkdir()
    require(command(["ccache", "-M", "1G"], output / "cache-limit.txt", env=env))
    require(
        command(
            [
                "ccache",
                "--set-config",
                "sloppiness=pch_defines,time_macros,include_file_mtime,include_file_ctime",
            ],
            output / "cache-config-set.txt",
            env=env,
        )
    )
    require(command(["ccache", "-sp"], output / "cache-before.txt", env=env))
    require(command(["ccache", "-z"], output / "cache-reset.txt", env=env))
    configure_env = dict(
        env,
        CXXFLAGS="-march=native -O2 -Wodr -flto-odr-type-merging -Werror",
        CFLAGS="-O2 -march=native -Werror",
    )
    argv = [
        "cmake",
        str(source),
        "-DUSE_CCACHE=ON",
        "-DUSE_PRECOMPILED_HEADERS=ON",
        "-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON",
        "-DCMAKE_INSTALL_PREFIX=/tmp/install-gdal",
        "-DGDAL_USE_TIFF_INTERNAL=OFF",
        "-DGDAL_USE_GEOTIFF_INTERNAL=OFF",
        "-DECW_ROOT=/opt/libecwj2-3.3",
        "-DMRSID_ROOT=/usr/local",
        "-DFileGDB_ROOT=/usr/local/FileGDB_API",
        "-DSQLite3_INCLUDE_DIR=/usr/local/install-sqlite-trusted-schema-off/include",
        "-DSQLite3_LIBRARY=/usr/local/install-sqlite-trusted-schema-off/lib/libsqlite3.so",
        "-DCSHARP_BINDINGS=OFF",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    result["configure"] = command(
        argv, output / "configure.log", cwd=build, env=configure_env, timeout=180
    )
    require(result["configure"])
    result["build"] = command(
        ["cmake", "--build", ".", "--parallel", str(os.cpu_count()), "--target"]
        + list(TARGETS),
        output / "build.log",
        cwd=build,
        env=env,
        timeout=1500,
    )
    require(result["build"])
    require(
        command(
            ["ctest", "--show-only=json-v1", "-R", TEST_PATTERN],
            output / "ctest-selection.json",
            cwd=build,
            env=env,
        )
    )
    selected = json.loads((output / "ctest-selection.json").read_text())["tests"]
    if sorted(test["name"] for test in selected) != sorted(TESTS):
        raise RuntimeError(
            "CTest selection must contain exactly the four expected native tests"
        )
    result["discovered_test_count"] = len(selected)
    # Replay CTest's exact commands and properties directly to retain POSIX signals.
    for test in selected:
        properties = {
            item["name"]: item["value"] for item in test.get("properties", [])
        }
        test_env = dict(env)
        for assignment in properties.get("ENVIRONMENT", []):
            key, value = assignment.split("=", 1)
            test_env[key] = value
        cwd = properties.get("WORKING_DIRECTORY", str(build / "autotest/cpp"))
        prefix = output / test["name"]
        inspect_libraries(
            Path(test["command"][0]),
            build,
            prefix,
            test_env,
            require_gdal=test["name"] not in ("test-sse2", "test-sse2-emulation"),
        )
        observed = command(
            test["command"],
            prefix.with_suffix(".log"),
            cwd=cwd,
            env=test_env,
            timeout=60,
        )
        observed["name"] = test["name"]
        result["tests"].append(observed)
        write_json(output / "result.json", result)
        if observed["signal"] == "SIGILL" and not observed["timeout"]:
            observed["gdb"] = debug_fault(
                test, cwd, test_env, prefix.with_suffix(".gdb.txt")
            )
        write_json(output / "result.json", result)
    result["status"] = (
        "passed"
        if len(result["tests"]) == len(TESTS)
        and all(test["passed"] for test in result["tests"])
        else "failed"
    )


def main():
    source = Path(os.environ["SIGILL_SOURCE_ROOT"])
    if not source.samefile("/diagnostic/source"):
        raise RuntimeError(
            "Source root must be the disposable /diagnostic/source bind mount"
        )
    results = {
        "harness_commit": os.environ["SIGILL_HARNESS_COMMIT"],
        "cache_restore_hit": os.environ.get("SIGILL_CACHE_HIT"),
        "cache_matched_key": os.environ.get("SIGILL_CACHE_MATCHED_KEY"),
        "scope": "Four native CTest commands, no Python/Zarr suite or product fix claim.",
        "hypotheses": [
            "Warm candidate SIGILL plus cold candidate success suggests cached state only if relevant object cache hits occurred.",
            "A faulting opcode unsupported by the captured CPU features suggests an ISA mismatch.",
            "Cold candidate SIGILL plus cold parent success suggests a candidate-specific difference in this environment.",
        ],
        "environment_limits": [
            "Pinned pulled dependency manifest, not the unavailable original locally rebuilt image manifest.",
            "New runner may differ from original znver3; comparisons control only this job's environment.",
            "Additive gdb installation precedes all arms; existing package versions must remain unchanged.",
            "Container runs as root; original build ran as the host UID.",
            "Restored cache without observed hits does not test the original cached objects.",
            "Passing phases do not exonerate the original environment or prove the source fix.",
        ],
        "phases": [],
        "expected_phase_count": 3,
        "expected_tests_per_phase": len(TESTS),
        "status": "incomplete",
    }
    write_json(ARTIFACTS / "results.json", results)
    try:
        setup()
        for label, revision in (
            ("warm-candidate", CANDIDATE),
            ("cold-candidate", CANDIDATE),
            ("cold-parent", PARENT),
        ):
            result = {}
            results["phases"].append(result)
            try:
                run_phase(label, revision, source, result)
            except Exception as error:
                result.update(status="failed", error=repr(error))
            finally:
                output = ARTIFACTS / label
                output.mkdir(exist_ok=True)
                cache = (
                    "/.ccache"
                    if label == "warm-candidate"
                    else "/diagnostic/" + label + "-cache"
                )
                try:
                    require(
                        command(
                            ["ccache", "-s"],
                            output / "cache-after.txt",
                            env=dict(os.environ, CCACHE_DIR=cache),
                        )
                    )
                    result["observed_cache_hits"] = cache_hits(
                        output / "cache-after.txt"
                    )
                    if result.get("source_loaded"):
                        retain_build(source / "build-ubuntu_20.04", output)
                except Exception as error:
                    result.update(status="failed", evidence_error=repr(error))
                write_json(output / "result.json", result)
                write_json(ARTIFACTS / "results.json", results)
        results["status"] = (
            "passed"
            if len(results["phases"]) == 3
            and all(phase["status"] == "passed" for phase in results["phases"])
            else "failed"
        )
    except Exception as error:
        results.update(status="failed", error=repr(error))
    write_json(ARTIFACTS / "results.json", results)
    return 0 if results["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--driver-status", type=int)
    parser.add_argument("--workflow-outcome")
    args = parser.parse_args()
    if args.manifest:
        args.manifest.mkdir(parents=True, exist_ok=True)
        if args.driver_status is not None:
            write_json(
                args.manifest / "driver-status.json",
                {"exit_status": args.driver_status},
            )
        if args.workflow_outcome is not None:
            write_json(
                args.manifest / "workflow-outcome.json",
                {"outcome": args.workflow_outcome},
            )
        manifest = args.manifest / "artifact-manifest.json"
        paths = [
            path
            for path in args.manifest.rglob("*")
            if path.is_file() and path != manifest
        ]
        hash_files(args.manifest, paths, manifest)
    else:
        raise SystemExit(main())
