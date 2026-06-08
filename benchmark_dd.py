#!/usr/bin/env python3
import argparse
import csv
import os
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import typing
from datetime import datetime
from pathlib import Path

TEST_FILE = "dd_benchmark_tmp.bin"
CSV_HEADER = ["Mechanism", "Operation", "Speed (MBs)"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark dd for raw R/O operations, LUKS, eCryptfs and fscrypt."
    )
    parser.add_argument(
        "--size", type=int, default=512, help="Test file size in MB"
    )
    parser.add_argument(
        "--block",
        nargs="+",
        default=["4K", "512K", "1M", "4M"],
        metavar="BLOCK",
        help="Block size(s) for dd I/O (multiple values run separate benchmarks)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="Number of repetitions per test",
    )
    return parser.parse_args()


def print_header(title: str) -> None:
    print("-" * 50)
    print(f"|{title}|")
    print("-" * 50)
    print()


def run_cmd(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def drop_caches() -> None:
    subprocess.run(["sync"], check=True)
    if os.geteuid() == 0:
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="utf-8")
        return

    run_cmd(
        ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
    )


def invoking_user_command(command: list[str]) -> list[str]:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        print(f"Enter password for {sudo_user}")
        return ["su", sudo_user, "-c", shlex.join(command)]
    return command


def run_dd(command: list[str]) -> subprocess.CompletedProcess[str]:
    return run_cmd(
        invoking_user_command(command),
    )


def parse_dd_speed(output: str) -> float:
    SPEED_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(MB|GB)/s")
    UNIT_MUL = {
        "MB": 1.0,
        "GB": 1000.0,
    }

    matches = list(SPEED_RE.finditer(output))
    if matches:
        match = matches[-1]
        value = float(match.group(1).replace(",", "."))
        return round(value * UNIT_MUL[match.group(2)], 2)

    return 0.0


def average_speed(speeds: list[float]) -> float:
    return round(statistics.fmean(speeds), 2) if speeds else 0.0


def test_write(
    path: Path,
    label: str,
    file_size_mb: int,
    block_size: str,
    repeat: int,
    results_file: Path,
) -> None:
    target = path / TEST_FILE
    speeds: list[float] = []

    print(f"[WRITE] {label} - {file_size_mb} MB, block {block_size}")

    try:
        for iteration in range(1, repeat + 1):
            drop_caches()
            completed = run_dd(
                [
                    "dd",
                    "if=/dev/urandom",
                    f"of={target}",
                    f"bs={block_size}",
                    f"count={file_size_mb}",
                    "conv=fsync",
                ]
            )
            speed = parse_dd_speed(completed.stderr)
            speeds.append(speed)
            print(f"\tRun {iteration}/{repeat}: {speed} MB/s")
    finally:
        target.unlink(missing_ok=True)

    avg = average_speed(speeds)
    print(f"\tMean: {avg} MB/s")
    with results_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label, "WRITE", avg])


def test_read(
    path: Path,
    label: str,
    file_size_mb: int,
    block_size: str,
    repeat: int,
    results_file: Path,
) -> None:
    target = path / TEST_FILE
    speeds: list[float] = []

    print(f"[READ] {label} - {file_size_mb} MB, block {block_size}")

    try:
        run_dd(
            [
                "dd",
                "if=/dev/zero",
                f"of={target}",
                f"bs={block_size}",
                f"count={file_size_mb}",
                "conv=fsync",
            ]
        )

        for iteration in range(1, repeat + 1):
            drop_caches()
            completed = run_dd(
                [
                    "dd",
                    f"if={target}",
                    "of=/dev/null",
                    f"bs={block_size}",
                ]
            )
            speed = parse_dd_speed(completed.stderr)
            speeds.append(speed)
            print(f"\tRun {iteration}/{repeat}: {speed} MB/s")
    finally:
        target.unlink(missing_ok=True)

    avg = average_speed(speeds)
    print(f"\tMean: {avg} MB/s")
    with results_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label, "READ", avg])


def mountpoint_exists(path: Path) -> bool:
    try:
        return path.is_mount()
    except OSError:
        return False


def ensure_luks_mounted(mount_point: Path) -> bool:
    if mountpoint_exists(mount_point):
        return True

    mapper_device = Path("/dev/mapper/luks_test")
    if not mapper_device.exists():
        source_device = Path("/dev/sdb")
        if not source_device.exists():
            print(f"[SKIPPED] LUKS - missing {source_device}")
            return False

        try:
            run_cmd(
                ["sudo", "cryptsetup", "open", str(source_device), "luks_test"]
            )
        except subprocess.CalledProcessError as exc:
            print(
                f"[SKIPPED] LUKS - cannot open {source_device}: {exc.stderr.strip() if exc.stderr else exc}"
            )
            return False

    if not mapper_device.exists():
        print("[SKIPPED] LUKS - missing /dev/mapper/luks_test")
        return False

    mount_point.mkdir(parents=True, exist_ok=True)
    try:
        run_cmd(["sudo", "mount", str(mapper_device), str(mount_point)])
    except subprocess.CalledProcessError as exc:
        print(
            f"[SKIPPED] LUKS - cannot mount {mount_point}: {exc.stderr.strip() if exc.stderr else exc}"
        )
        return False
    return True


def ensure_ecryptfs_mounted(mount_point: Path, encrypted_dir: Path) -> bool:
    if mountpoint_exists(mount_point):
        return True

    if not encrypted_dir.exists():
        print(f"[SKIPPED] eCryptfs - encrypted dir missing {encrypted_dir}")
        return False

    mount_point.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "sudo",
                "mount",
                "-t",
                "ecryptfs",
                str(encrypted_dir),
                str(mount_point),
                "-o",
                "ecryptfs_cipher=aes,ecryptfs_key_bytes=32,ecryptfs_passthrough=n,ecryptfs_enable_filename_crypto=y",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[SKIPPED] eCryptfs - cannot mount {mount_point}: {exc}")
        return False
    return True


def ensure_fscrypt_mounted(mount_point: Path) -> bool:
    encrypted_dir = mount_point / "private_data"
    source_device = Path("/dev/sdc")

    if not mountpoint_exists(mount_point):
        if not source_device.exists():
            print(f"[SKIPPED] fscrypt - missing {source_device}")
            return False

        mount_point.mkdir(parents=True, exist_ok=True)
        try:
            run_cmd(["sudo", "mount", str(source_device), str(mount_point)])
        except subprocess.CalledProcessError as exc:
            print(
                f"[SKIPPED] fscrypt - cannot mount {mount_point}: {exc.stderr.strip() if exc.stderr else exc}"
            )
            return False

    if not encrypted_dir.is_dir():
        print(f"[SKIPPED] fscrypt - missing {encrypted_dir}")
        return False

    try:
        subprocess.run(
            invoking_user_command(["fscrypt", "unlock", str(encrypted_dir)]),
            check=True,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        if "already unlocked" in stderr:
            return True
        print(
            f"[SKIPPED] fscrypt - cannot unlock {encrypted_dir}: {stderr or exc}"
        )
        return False

    if not os.access(encrypted_dir, os.W_OK):
        print(
            f"[SKIPPED] fscrypt - {encrypted_dir} still not available after unlock"
        )
        return False

    return True


def lock_fscrypt_directory() -> bool:
    encrypted_dir = Path("/mnt/fscrypt_test/private_data")

    try:
        run_cmd(invoking_user_command(["fscrypt", "lock", str(encrypted_dir)]))
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        if "already locked" not in stderr:
            print(f"[WARN] fscrypt - cannot lock {encrypted_dir}: {stderr}")
    return True


def unmount_if_needed(path: Path) -> bool:
    run_cmd(
        ["sudo", "umount", str(path)],
    )
    return True


def print_results(results_file: Path) -> None:
    rows: list[list[str]] = []
    with results_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)

    widths = [
        max(len(row[index]) for row in rows) for index in range(len(rows[0]))
    ]
    for row in rows:
        print(
            "  ".join(
                value.ljust(widths[index]) for index, value in enumerate(row)
            )
        )


def append_na(results_file: Path, mechanism: str) -> None:
    with results_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([mechanism, "WRITE", "N/A"])
        writer.writerow([mechanism, "READ", "N/A"])


def available_space_mb(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return usage.free // 1_000_000


def main() -> int:
    args = parse_args()

    mount_luks = Path("/mnt/luks_test")
    mount_ecryptfs = Path("/mnt/ecryptfs_decrypted")
    mount_fscrypt = Path("/mnt/fscrypt_test")
    results_file = Path(f"dd_results_{datetime.now():%Y%m%d_%H%M%S}.csv")

    with results_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)

    def run_test(
        label: str,
        work_dir: Path,
        test_name: str,
        set_up: typing.Callable[[], typing.Any],
        tear_down: typing.Callable[[], typing.Any],
    ):
        try:
            print_header(label)
            if not set_up():
                raise RuntimeError
            for block_size in args.block:
                test_write(
                    work_dir,
                    f"{test_name} [{block_size}]",
                    args.size,
                    block_size,
                    args.repeat,
                    results_file,
                )
                test_read(
                    work_dir,
                    f"{test_name} [{block_size}]",
                    args.size,
                    block_size,
                    args.repeat,
                    results_file,
                )
            tear_down()
        except Exception:
            print(f"[SKIPPED] {test_name} - {work_dir} not available")
            for block_size in args.block:
                append_na(results_file, f"{test_name} [{block_size}]")

    print_header("Benchmark dd")
    print(f"\tResults file: {results_file}")
    print(f"\tTest file size: {args.size} MB")
    print(f"\tBlock sizes: {', '.join(args.block)}")
    print(f"\tNumber of repetitions: {args.repeat}")

    raw_io_dir = Path(tempfile.mkdtemp(prefix="dd_raw_io_"))
    run_test(
        "1. Raw I/O - without any encryption",
        raw_io_dir,
        "Raw I/O",
        lambda: available_space_mb(raw_io_dir) >= args.size * 2,
        lambda: shutil.rmtree(raw_io_dir, ignore_errors=True),
    )
    run_test(
        "2. LUKS (Block encryption)",
        mount_luks,
        "LUKS",
        lambda: (
            ensure_luks_mounted(mount_luks) and mountpoint_exists(mount_luks)
        ),
        lambda: unmount_if_needed(mount_luks),
    )
    run_test(
        "3. eCryptfs (Per-file encryption)",
        mount_ecryptfs,
        "eCryptfs",
        lambda: (
            ensure_ecryptfs_mounted(
                mount_ecryptfs, Path("/mnt/ecryptfs_encrypted")
            )
            and mountpoint_exists(mount_ecryptfs)
        ),
        lambda: unmount_if_needed(mount_ecryptfs),
    )
    run_test(
        "4. fscrypt (Per-file encryption)",
        mount_fscrypt,
        "fscrypt",
        lambda: ensure_fscrypt_mounted(mount_fscrypt),
        lambda: (
            lock_fscrypt_directory() and unmount_if_needed(mount_fscrypt),
        ),
    )

    print_header("RESULTS:")
    print_results(results_file)
    print(f"\nSaved in: {results_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
