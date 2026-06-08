#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import typing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BS_RAND = "4k"
BS_SEQ = "1M"
ODIRECT_ERROR_RE = re.compile(
    r"(does not support direct=1/buffered=0|destination does not support O_DIRECT|not support O_DIRECT)",
    re.IGNORECASE,
)
TEST_DESCRIPTIONS = {
    "seq_write": f"Sequential write {BS_SEQ}",
    "seq_read": f"Sequential read {BS_SEQ}",
    "rand_write_4k": "Random write 4K",
    "rand_read_4k": "Random read 4K",
    "mixed_70r_30w": "Mixed 70%R/30%W 4K",
    "rand_write_64k": "Random write 64K",
    "rand_read_64k": "Random read 64K",
}

MOUNT_LUKS = Path("/mnt/luks_test")
LUKS_DEVICE = Path("/dev/sdb")
LUKS_MAPPER_DEVICE = Path("/dev/mapper/luks_test")

MOUNT_ECRYPTFS = Path("/mnt/ecryptfs_decrypted")

MOUNT_FSCRYPT_BASE = Path("/mnt/fscrypt_test")
MOUNT_FSCRYPT = Path("/mnt/fscrypt_test/private_data")
FSCRYPT_DEVICE = Path("/dev/sdc")

ECRYPTFS_ENCRYPTED = Path("/mnt/ecryptfs_encrypted")


@dataclass
class TestCase:
    name: str
    rw: str
    bs: str
    extra_params: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Advanced I/O benchmark using fio."
    )
    parser.add_argument(
        "--runtime", type=int, default=30, help="Test duration in seconds"
    )
    parser.add_argument("--size", default="512M", help="Test file size")
    parser.add_argument(
        "--iodepth", type=int, default=16, help="I/O queue depth"
    )
    parser.add_argument(
        "--numjobs", type=int, default=4, help="Number of parallel fio threads"
    )
    parser.add_argument(
        "--io-mode",
        choices=("auto", "direct", "buffered"),
        default="auto",
        help="fio I/O mode: auto = direct with buffered fallback, direct = O_DIRECT only, buffered = no O_DIRECT",
    )
    return parser.parse_args()


def check_deps() -> None:
    missing = [cmd for cmd in set(["fio"]) if shutil.which(cmd) is None]
    if missing:
        raise SystemExit(
            f"Missing tools: {' '.join(missing)}\nInstall with: sudo apt install fio"
        )


def print_header(title: str) -> None:
    print("-" * 50)
    print(f"|{title}|")
    print("-" * 50)
    print()


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


def invoking_user_command(command: list[str]) -> list[str]:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        print(f"Enter password for {sudo_user}")
        return ["su", sudo_user, "-c", shlex.join(command)]
    return command


def run_fio(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        invoking_user_command(command),
        check=True,
        text=True,
        capture_output=True,
    )


def fio_needs_buffered_retry(exc: subprocess.CalledProcessError) -> bool:
    output = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
    return bool(ODIRECT_ERROR_RE.search(output))


def drop_caches() -> None:
    subprocess.run(["sync"], check=True)
    subprocess.run(
        ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
        text=True,
        check=True,
    )


def prepare_rows(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "summary.csv"
    with summary_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Mechanism",
                "Test",
                "Description",
                "Read_IOPS",
                "Write_IOPS",
                "Read_BW_MBs",
                "Write_BW_MBs",
                "Read_Lat_us",
                "Write_Lat_us",
                "CPU_usr_%",
                "CPU_sys_%",
            ]
        )
    return summary_csv


def run_fio_test(
    label: str,
    path: Path,
    test_name: str,
    test_case: TestCase,
    file_size: str,
    runtime: int,
    iodepth: int,
    numjobs: int,
    output_dir: Path,
    summary_csv: Path,
    io_mode: str,
) -> None:
    description = TEST_DESCRIPTIONS.get(test_name, test_name)
    safe_label = label.replace("/", "_").replace(" ", "_")
    json_out = output_dir / f"{safe_label}_{test_name}.json"
    filename = path / f"fio_testfile_{test_name}"

    print(f"\n[FIO] {label} - {description}")
    print(
        f"\trw={test_case.rw}, bs={test_case.bs}, iodepth={iodepth}, numjobs={numjobs}, runtime={runtime}s"
    )

    def build_fio_cmd(attempt_mode: str) -> list[str]:
        return [
            "fio",
            f"--name={label}_{test_name}",
            f"--filename={filename}",
            f"--rw={test_case.rw}",
            f"--bs={test_case.bs}",
            f"--size={file_size}",
            f"--runtime={runtime}",
            "--time_based",
            f"--iodepth={iodepth}",
            f"--numjobs={numjobs}",
            "--ioengine=libaio",
            f"--direct={1 if attempt_mode == 'direct' else 0}",
            "--group_reporting",
            "--randrepeat=0",
            "--norandommap",
            "--output-format=json",
            f"--output={json_out}",
            *test_case.extra_params,
        ]

    if io_mode == "buffered":
        attempt_modes = ["buffered"]
    elif io_mode == "direct":
        attempt_modes = ["direct"]
    else:
        attempt_modes = ["direct", "buffered"]

    completed = None
    attempt_mode = attempt_modes[0]
    for attempt_mode in attempt_modes:
        try:
            drop_caches()
            completed = run_fio(build_fio_cmd(attempt_mode=attempt_mode))
            break
        except subprocess.CalledProcessError as exc:
            if not (
                io_mode == "auto"
                and attempt_mode == "direct"
                and fio_needs_buffered_retry(exc)
            ):
                raise

            print(
                f"\t[INFO] {label} - O_DIRECT not supported, retrying in buffered mode"
            )

    if completed is None:
        with summary_csv.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    label,
                    test_name,
                    f"{description} [{io_mode}]",
                    *("ERROR" for _ in range(8)),
                ]
            )
        return

    with json_out.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    job = data["jobs"][0]
    read = job["read"]
    write = job["write"]
    cpu_usr = job.get("usr_cpu", 0)
    cpu_sys = job.get("sys_cpu", 0)

    read_iops = read.get("iops", 0)
    write_iops = write.get("iops", 0)
    read_bw_mbs = read.get("bw", 0) / 1024
    write_bw_mbs = write.get("bw", 0) / 1024
    read_lat_us = read.get("lat_ns", {}).get("mean", 0) / 1000
    write_lat_us = write.get("lat_ns", {}).get("mean", 0) / 1000

    effective_mode = (
        attempt_mode
        if not (io_mode == "auto" and attempt_mode == "buffered")
        else "buffered fallback"
    )
    if effective_mode != "direct":
        description = f"{description} [{effective_mode}]"

    print(f"\tI/O mode: {effective_mode}")
    print(f"\tCPU: usr={cpu_usr:.1f}%  sys={cpu_sys:.1f}%")
    print(
        f"\tRead:  {read_iops:>8.0f} IOPS | {read_bw_mbs:>7.1f} MB/s | lat: {read_lat_us:>8.1f} us"
    )
    print(
        f"\tWrite: {write_iops:>8.0f} IOPS | {write_bw_mbs:>7.1f} MB/s | lat: {write_lat_us:>8.1f} us"
    )

    with summary_csv.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                label,
                test_name,
                description,
                f"{read_iops:.0f}",
                f"{write_iops:.0f}",
                f"{read_bw_mbs:.2f}",
                f"{write_bw_mbs:.2f}",
                f"{read_lat_us:.1f}",
                f"{write_lat_us:.1f}",
                f"{cpu_usr:.1f}",
                f"{cpu_sys:.1f}",
            ]
        )

    filename.unlink(missing_ok=True)


def run_all_tests(
    label: str,
    path: Path,
    file_size: str,
    runtime: int,
    iodepth: int,
    numjobs: int,
    output_dir: Path,
    summary_csv: Path,
    io_mode: str,
) -> None:
    print_header(f"Testing: {label} ({path})")

    test_plan = [
        ("seq_write", TestCase("seq_write", "write", BS_SEQ, [])),
        ("seq_read", TestCase("seq_read", "read", BS_SEQ, [])),
        ("rand_write_4k", TestCase("rand_write_4k", "randwrite", BS_RAND, [])),
        ("rand_read_4k", TestCase("rand_read_4k", "randread", BS_RAND, [])),
        (
            "mixed_70r_30w",
            TestCase("mixed_70r_30w", "randrw", BS_RAND, ["--rwmixread=70"]),
        ),
        ("rand_write_64k", TestCase("rand_write_64k", "randwrite", "64k", [])),
        ("rand_read_64k", TestCase("rand_read_64k", "randread", "64k", [])),
    ]

    for test_name, test_case in test_plan:
        run_fio_test(
            label=label,
            path=path,
            test_name=test_name,
            test_case=test_case,
            file_size=file_size,
            runtime=runtime,
            iodepth=iodepth,
            numjobs=numjobs,
            output_dir=output_dir,
            summary_csv=summary_csv,
            io_mode=io_mode,
        )


def fill_missing_rows(summary_csv: Path, label: str) -> None:
    with summary_csv.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for test_name in (
            "seq_write",
            "seq_read",
            "rand_write_4k",
            "rand_read_4k",
            "mixed_70r_30w",
            "rand_write_64k",
            "rand_read_64k",
        ):
            writer.writerow(
                [
                    label,
                    test_name,
                    TEST_DESCRIPTIONS.get(test_name, test_name),
                    *(["N/A"] * 8),
                ]
            )


def print_summary(summary_csv: Path) -> None:
    with summary_csv.open("r", encoding="utf-8", newline="") as handle:
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


def main() -> int:
    check_deps()
    args = parse_args()

    output_dir = Path(f"fio_results_{datetime.now():%Y%m%d_%H%M%S}")
    summary_csv = prepare_rows(output_dir)
    raw_io_dir = Path(tempfile.mkdtemp(prefix="fio_raw_io_"))

    print(f"fio Benchmark - start: {datetime.now():%c}")

    def run_test(
        label: str,
        path: Path,
        set_up: typing.Callable[[], typing.Any],
        tear_down: typing.Callable[[], typing.Any],
    ) -> None:
        try:
            if not set_up():
                raise RuntimeError
            run_all_tests(
                label=label,
                path=path,
                file_size=args.size,
                runtime=args.runtime,
                iodepth=args.iodepth,
                numjobs=args.numjobs,
                output_dir=output_dir,
                summary_csv=summary_csv,
                io_mode=args.io_mode,
            )
            tear_down()
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            stdout = exc.stdout.strip() if exc.stdout else ""
            detail = stderr or stdout or str(exc)
            print(f"[SKIPPED] {label} - {path} not available: {detail}")
            fill_missing_rows(summary_csv, label)
        except Exception as exc:
            print(f"[SKIPPED] {label} - {path} not available: {exc}")
            fill_missing_rows(summary_csv, label)

    run_test(
        "Raw I/O",
        raw_io_dir,
        lambda: True,
        lambda: shutil.rmtree(raw_io_dir, ignore_errors=True),
    )
    run_test(
        "LUKS",
        MOUNT_LUKS,
        lambda: ensure_luks_mounted() and os.path.ismount(MOUNT_LUKS),
        lambda: unmount_if_needed(MOUNT_LUKS),
    )
    run_test(
        "eCryptfs",
        MOUNT_ECRYPTFS,
        lambda: ensure_ecryptfs_mounted() and os.path.ismount(MOUNT_ECRYPTFS),
        lambda: unmount_if_needed(MOUNT_ECRYPTFS),
    )
    run_test(
        "fscrypt",
        MOUNT_FSCRYPT,
        lambda: ensure_fscrypt_mounted(),
        lambda: (
            lock_fscrypt_directory() and unmount_if_needed(MOUNT_FSCRYPT_BASE)
        ),
    )

    print_header("SUMMARY:")
    print_summary(summary_csv)
    print(f"Saved in: {output_dir}/")
    print(f"Summary CSV: {summary_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
