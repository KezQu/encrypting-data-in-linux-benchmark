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

MOUNT_LUKS = Path("/mnt/luks_test")
LUKS_DEVICE = Path("/dev/sdb")
LUKS_MAPPER_DEVICE = Path("/dev/mapper/luks_test")

MOUNT_ECRYPTFS = Path("/mnt/ecryptfs_upper")

MOUNT_FSCRYPT_BASE = Path("/mnt/fscrypt_test")
MOUNT_FSCRYPT = Path("/mnt/fscrypt_test/private_data")
FSCRYPT_DEVICE = Path("/dev/sdc")

ECRYPTFS_ENCRYPTED = Path("/mnt/ecryptfs_encrypted")


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


def drop_caches() -> None:
    subprocess.run(["sync"], check=True)
    subprocess.run(
        ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
        text=True,
        check=True,
    )


def invoking_user_command(command: list[str]) -> list[str]:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        print(f"Enter password for {sudo_user}")
        return ["su", sudo_user, "-c", shlex.join(command)]
    return command


def run_dd(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        invoking_user_command(command),
        check=True,
        text=True,
        capture_output=True,
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
    summary_file: Path,
) -> None:
    filename = path / TEST_FILE
    speeds: list[float] = []

    print(f"[WRITE] {label} - {file_size_mb} MB, block {block_size}")

    try:
        for iteration in range(1, repeat + 1):
            drop_caches()
            completed = run_dd(
                [
                    "dd",
                    "if=/dev/urandom",
                    f"of={filename}",
                    f"bs={block_size}",
                    f"count={file_size_mb}",
                    "conv=fsync",
                ]
            )
            speed = parse_dd_speed(completed.stderr)
            speeds.append(speed)
            print(f"\tRun {iteration}/{repeat}: {speed} MB/s")
    finally:
        filename.unlink(missing_ok=True)

    avg = average_speed(speeds)
    print(f"\tMean: {avg} MB/s")
    with summary_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label, "WRITE", avg])


def test_read(
    path: Path,
    label: str,
    file_size_mb: int,
    block_size: str,
    repeat: int,
    summary_file: Path,
) -> None:
    filename = path / TEST_FILE
    speeds: list[float] = []

    print(f"[READ] {label} - {file_size_mb} MB, block {block_size}")

    try:
        run_dd(
            [
                "dd",
                "if=/dev/zero",
                f"of={filename}",
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
                    f"if={filename}",
                    "of=/dev/null",
                    f"bs={block_size}",
                ]
            )
            speed = parse_dd_speed(completed.stderr)
            speeds.append(speed)
            print(f"\tRun {iteration}/{repeat}: {speed} MB/s")
    finally:
        filename.unlink(missing_ok=True)

    avg = average_speed(speeds)
    print(f"\tMean: {avg} MB/s")
    with summary_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label, "READ", avg])


def ensure_luks_mounted() -> bool:
    if os.path.ismount(MOUNT_LUKS):
        return True

    if not LUKS_MAPPER_DEVICE.exists():
        if not LUKS_DEVICE.exists():
            print(f"[SKIPPED] LUKS - missing {LUKS_DEVICE}")
            return False

        try:
            subprocess.run(
                ["sudo", "cryptsetup", "open", str(LUKS_DEVICE), "luks_test"],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"[SKIPPED] LUKS - failed to open {LUKS_DEVICE}: {exc}")
            return False

    if not LUKS_MAPPER_DEVICE.exists():
        print("[SKIPPED] LUKS - missing /dev/mapper/luks_test")
        return False

    MOUNT_LUKS.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["sudo", "mount", "/dev/mapper/luks_test", str(MOUNT_LUKS)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[SKIPPED] LUKS - failed to mount {MOUNT_LUKS}: {exc}")
        return False
    return True


def ensure_ecryptfs_mounted() -> bool:
    if os.path.ismount(MOUNT_ECRYPTFS):
        return True

    if not ECRYPTFS_ENCRYPTED.exists():
        print(f"[SKIPPED] eCryptfs - encrypted dir missing {MOUNT_ECRYPTFS}")
        return False

    MOUNT_ECRYPTFS.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "sudo",
                "mount",
                "-t",
                "ecryptfs",
                str(ECRYPTFS_ENCRYPTED),
                str(MOUNT_ECRYPTFS),
                "-o",
                "ecryptfs_cipher=aes,ecryptfs_key_bytes=32,ecryptfs_passthrough=n,ecryptfs_enable_filename_crypto=y",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[SKIPPED] eCryptfs - failed to mount {MOUNT_ECRYPTFS}: {exc}")
        return False
    return True


def ensure_fscrypt_mounted() -> bool:
    if not os.path.ismount(MOUNT_FSCRYPT_BASE):
        if not FSCRYPT_DEVICE.exists():
            print(f"[SKIPPED] fscrypt - missing {FSCRYPT_DEVICE}")
            return False

        MOUNT_FSCRYPT_BASE.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["sudo", "mount", str(FSCRYPT_DEVICE), str(MOUNT_FSCRYPT_BASE)],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(
                f"[SKIPPED] fscrypt - failed to mount {MOUNT_FSCRYPT_BASE}: {exc.stderr.strip() if exc.stderr else exc}"
            )
            return False

    if not MOUNT_FSCRYPT.is_dir():
        print(f"[SKIPPED] fscrypt - missing {MOUNT_FSCRYPT}")
        return False

    try:
        subprocess.run(
            invoking_user_command(["fscrypt", "unlock", str(MOUNT_FSCRYPT)]),
            check=True,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        if "already unlocked" in stderr:
            return True
        print(
            f"[SKIPPED] fscrypt - failed to unlock {MOUNT_FSCRYPT}: {stderr or exc}"
        )
        return False

    if not os.access(MOUNT_FSCRYPT, os.W_OK):
        print(
            f"[SKIPPED] fscrypt - {MOUNT_FSCRYPT} still not available after unlock"
        )
        return False

    return True


def lock_fscrypt_directory() -> bool:
    try:
        subprocess.run(
            invoking_user_command(["fscrypt", "lock", str(MOUNT_FSCRYPT)]),
            check=True,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        if "already locked" not in stderr:
            print(f"[WARN] fscrypt - failed to lock {MOUNT_FSCRYPT}: {stderr}")
    return True


def unmount_if_needed(path: Path) -> bool:
    subprocess.run(["sudo", "umount", str(path)], check=False)
    return True


def print_summary(summary_file: Path) -> None:
    rows: list[list[str]] = []
    with summary_file.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    widths = [
        max(len(row[index]) for row in rows) for index in range(len(rows[0]))
    ]
    for row in rows:
        print(
            "  ".join(
                value.ljust(widths[index]) for index, value in enumerate(row)
            )
        )


def append_na(summary_file: Path, mechanism: str) -> None:
    with summary_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([mechanism, "WRITE", "N/A"])
        writer.writerow([mechanism, "READ", "N/A"])


def available_space_mb(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return usage.free // 1_000_000


def main() -> int:
    args = parse_args()

    summary_file = Path(f"dd_results_{datetime.now():%Y%m%d_%H%M%S}.csv")

    with summary_file.open("w", encoding="utf-8", newline="") as handle:
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
                    summary_file,
                )
                test_read(
                    work_dir,
                    f"{test_name} [{block_size}]",
                    args.size,
                    block_size,
                    args.repeat,
                    summary_file,
                )
            tear_down()
        except Exception:
            print(f"[SKIPPED] {test_name} - {work_dir} not available")
            for block_size in args.block:
                append_na(summary_file, f"{test_name} [{block_size}]")

    print_header("Benchmark dd")
    print(f"\tResults file: {summary_file}")
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
        MOUNT_LUKS,
        "LUKS",
        lambda: ensure_luks_mounted(),
        lambda: unmount_if_needed(MOUNT_LUKS),
    )
    run_test(
        "3. eCryptfs (Per-file encryption)",
        MOUNT_ECRYPTFS,
        "eCryptfs",
        lambda: ensure_ecryptfs_mounted(),
        lambda: unmount_if_needed(MOUNT_ECRYPTFS),
    )
    run_test(
        "4. fscrypt (Per-file encryption)",
        MOUNT_FSCRYPT,
        "fscrypt",
        lambda: ensure_fscrypt_mounted(),
        lambda: (
            lock_fscrypt_directory() and unmount_if_needed(MOUNT_FSCRYPT_BASE),
        ),
    )

    print_header("SUMMARY:")
    print_summary(summary_file)
    print(f"\nSaved in: {summary_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
