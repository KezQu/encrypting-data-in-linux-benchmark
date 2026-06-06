#!/usr/bin/env python3
"""Zaawansowany benchmark I/O przy użyciu fio."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"

BS_RAND = "4k"
BS_SEQ = "1M"
ODIRECT_ERROR_RE = re.compile(
    r"(does not support direct=1/buffered=0|destination does not support O_DIRECT|not support O_DIRECT)",
    re.IGNORECASE,
)
TEST_DESCRIPTIONS = {
    "seq_write": f"Zapis sekwencyjny {BS_SEQ}",
    "seq_read": f"Odczyt sekwencyjny {BS_SEQ}",
    "rand_write_4k": "Losowy zapis 4K",
    "rand_read_4k": "Losowy odczyt 4K",
    "mixed_70r_30w": "Mieszany 70%R/30%W 4K",
    "rand_write_64k": "Losowy zapis 64K",
    "rand_read_64k": "Losowy odczyt 64K",
}

MOUNT_LUKS = Path("/mnt/luks_test")
MOUNT_ECRYPTFS = Path("/mnt/ecryptfs_upper")
MOUNT_FSCRYPT = Path("/mnt/fscrypt_test/private_data")
ECRYPTFS_LOWER = Path("/mnt/ecryptfs_lower")


@dataclass
class TestCase:
    name: str
    rw: str
    bs: str
    extra_params: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zaawansowany benchmark I/O przy użyciu fio.")
    parser.add_argument("--runtime", type=int, default=30, help="Czas testu w sekundach")
    parser.add_argument("--size", default="512M", help="Rozmiar pliku testowego")
    parser.add_argument("--iodepth", type=int, default=16, help="Głębokość kolejki I/O")
    parser.add_argument("--numjobs", type=int, default=4, help="Liczba równoległych wątków fio")
    parser.add_argument(
        "--io-mode",
        choices=("auto", "direct", "buffered"),
        default="auto",
        help="Tryb I/O fio: auto = direct z fallbackiem, direct = tylko O_DIRECT, buffered = bez O_DIRECT",
    )
    return parser.parse_args()


def check_deps() -> None:
    missing = [cmd for cmd in ("fio", "python3") if shutil.which(cmd) is None]
    if missing:
        raise SystemExit(
            f"Brakujące narzędzia: {' '.join(missing)}\nZainstaluj: sudo apt install fio python3"
        )


def print_header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}════════════════════════════════════════════════════════{NC}")
    print(f"{BOLD}{CYAN}  {title}{NC}")
    print(f"{BOLD}{CYAN}════════════════════════════════════════════════════════{NC}")


def is_available(path: Path, label: str) -> bool:
    if label == "Baseline":
        return True
    if label in {"LUKS", "eCryptfs"}:
        return path.is_mount()
    return path.is_dir() and os.access(path, os.W_OK)


def mountpoint_exists(path: Path) -> bool:
    try:
        return path.is_mount()
    except OSError:
        return False


def ensure_luks_mounted() -> bool:
    if mountpoint_exists(MOUNT_LUKS):
        return False

    mapper_device = Path("/dev/mapper/luks_test")
    if not mapper_device.exists():
        source_device = Path("/dev/sdb")
        if not source_device.exists():
            print(f"{RED}[POMINIĘTO]{NC} LUKS – brak {source_device}")
            return False

        try:
            subprocess.run(["sudo", "cryptsetup", "open", str(source_device), "luks_test"], check=True)
        except subprocess.CalledProcessError as exc:
            print(f"{RED}[POMINIĘTO]{NC} LUKS – nie udało się otworzyć {source_device}: {exc}")
            return False

    if not mapper_device.exists():
        print(f"{RED}[POMINIĘTO]{NC} LUKS – brak /dev/mapper/luks_test")
        return False

    MOUNT_LUKS.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["sudo", "mount", "/dev/mapper/luks_test", str(MOUNT_LUKS)], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"{RED}[POMINIĘTO]{NC} LUKS – nie udało się zamontować {MOUNT_LUKS}: {exc}")
        return False
    return True


def ensure_ecryptfs_mounted() -> bool:
    if mountpoint_exists(MOUNT_ECRYPTFS):
        return False
    if not ECRYPTFS_LOWER.exists():
        print(f"{RED}[POMINIĘTO]{NC} eCryptfs – brak katalogu dolnego {ECRYPTFS_LOWER}")
        return False

    MOUNT_ECRYPTFS.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "sudo",
                "mount",
                "-t",
                "ecryptfs",
                str(ECRYPTFS_LOWER),
                str(MOUNT_ECRYPTFS),
                "-o",
                "ecryptfs_cipher=aes,ecryptfs_key_bytes=32,ecryptfs_passthrough=n,ecryptfs_enable_filename_crypto=y",
            ],
            input="\n\n\n\n\n\nyes\n",
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"{RED}[POMINIĘTO]{NC} eCryptfs – nie udało się zamontować {MOUNT_ECRYPTFS}: {exc}")
        return False
    return True


def cleanup_mount(path: Path, mounted_here: bool) -> None:
    if mounted_here and mountpoint_exists(path):
        subprocess.run(["sudo", "umount", str(path)], check=False)


def invoking_user_command(command: list[str]) -> list[str]:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        return ["sudo", "-u", sudo_user, "--", *command]
    return command


def run_as_invoking_user(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return subprocess.run(
        invoking_user_command(command),
        check=True,
        text=True,
        capture_output=capture_output,
        env=env,
    )


def run_fio_command(command: list[str], *, run_as_user: bool) -> subprocess.CompletedProcess[str]:
    if run_as_user:
        return run_as_invoking_user(command, capture_output=True)

    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return subprocess.run(command, check=True, text=True, capture_output=True, env=env)


def write_fio_log(
    log_path: Path,
    attempt_name: str,
    completed: subprocess.CompletedProcess[str] | subprocess.CalledProcessError,
) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== {attempt_name} ===\n")
        if completed.stdout:
            handle.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                handle.write("\n")
        if completed.stderr:
            handle.write(completed.stderr)
            if not completed.stderr.endswith("\n"):
                handle.write("\n")


def fio_needs_buffered_retry(exc: subprocess.CalledProcessError) -> bool:
    output = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
    return bool(ODIRECT_ERROR_RE.search(output))


def drop_caches() -> None:
    subprocess.run(["sync"], check=True)
    if os.geteuid() == 0:
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="utf-8")
        return
    subprocess.run(
        ["sudo", "tee", "/proc/sys/vm/drop_caches"],
        input="3\n",
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def prepare_results(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "summary.csv"
    with summary_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Mechanizm",
                "Test",
                "Opis",
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
    *,
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
    run_as_user: bool,
    io_mode: str,
) -> None:
    description = TEST_DESCRIPTIONS.get(test_name, test_name)
    json_out = output_dir / f"{label}_{test_name}.json"
    fio_log = output_dir / f"{label}_{test_name}.log"
    filename = path / f"fio_testfile_{test_name}"

    print(f"\n{GREEN}[fio]{NC} {BOLD}{label}{NC} – {description}")
    print(f"  rw={test_case.rw}, bs={test_case.bs}, iodepth={iodepth}, numjobs={numjobs}, runtime={runtime}s")

    def build_fio_cmd(*, direct: bool) -> list[str]:
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
            f"--direct={1 if direct else 0}",
            "--group_reporting",
            "--randrepeat=0",
            "--norandommap",
            "--output-format=json",
            f"--output={json_out}",
            *test_case.extra_params,
        ]

    fio_log.write_text("", encoding="utf-8")

    if io_mode == "buffered":
        attempt_modes = ["buffered"]
    elif io_mode == "direct":
        attempt_modes = ["direct"]
    else:
        attempt_modes = ["direct", "buffered"]

    try:
        completed = None
        attempt_mode = attempt_modes[0]
        for attempt_mode in attempt_modes:
            try:
                completed = run_fio_command(build_fio_cmd(direct=(attempt_mode == "direct")), run_as_user=run_as_user)
                write_fio_log(fio_log, f"attempt: {attempt_mode}", completed)
                break
            except subprocess.CalledProcessError as exc:
                write_fio_log(fio_log, f"attempt: {attempt_mode}", exc)
                if io_mode != "auto" or attempt_mode != "direct" or not fio_needs_buffered_retry(exc):
                    raise

                print(f"{YELLOW}  [INFO]{NC} {label} – O_DIRECT nie jest obsługiwane, ponawiam w trybie buforowanym")

        if completed is None:
            raise subprocess.CalledProcessError(returncode=1, cmd=build_fio_cmd(direct=(attempt_modes[0] == "direct")))
    except subprocess.CalledProcessError:
        print(f"{RED}  [BŁĄD] fio nie powiódł się. Sprawdź: {fio_log}{NC}")
        with summary_csv.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([label, test_name, f"{description} [{io_mode}]", *("ERROR" for _ in range(8))])
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

    effective_mode = attempt_mode if io_mode != "auto" or attempt_mode != "buffered" else "buffered fallback"
    if effective_mode != "direct":
        description = f"{description} [{effective_mode}]"

    print(f"  Tryb I/O: {effective_mode}")
    print(f"  Odczyt:  {read_iops:>8.0f} IOPS  |  {read_bw_mbs:>7.1f} MB/s  |  lat: {read_lat_us:>8.1f} µs")
    print(f"  Zapis:   {write_iops:>8.0f} IOPS  |  {write_bw_mbs:>7.1f} MB/s  |  lat: {write_lat_us:>8.1f} µs")
    print(f"  CPU:     usr={cpu_usr:.1f}%  sys={cpu_sys:.1f}%")

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
    *,
    label: str,
    path: Path,
    file_size: str,
    runtime: int,
    iodepth: int,
    numjobs: int,
    output_dir: Path,
    summary_csv: Path,
    run_as_user: bool,
    io_mode: str,
) -> None:
    print_header(f"Testowanie: {label} ({path})")

    test_plan = [
        ("seq_write", TestCase("seq_write", "write", BS_SEQ, [])),
        ("seq_read", TestCase("seq_read", "read", BS_SEQ, [])),
        ("rand_write_4k", TestCase("rand_write_4k", "randwrite", BS_RAND, [])),
        ("rand_read_4k", TestCase("rand_read_4k", "randread", BS_RAND, [])),
        ("mixed_70r_30w", TestCase("mixed_70r_30w", "randrw", BS_RAND, ["--rwmixread=70"])),
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
            run_as_user=run_as_user,
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
            writer.writerow([label, test_name, TEST_DESCRIPTIONS.get(test_name, test_name), *(["N/A"] * 8)])


def print_summary(summary_csv: Path) -> None:
    with summary_csv.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    print_header("PODSUMOWANIE WYNIKÓW FIO")
    print()
    print(f"{BOLD}Przepustowość sekwencyjna (MB/s):{NC}")
    print("-------------------------------------------")
    print(f"{'Mechanizm':<12} {'Test':<16} {'Odczyt_MB/s':<12} {'Zapis_MB/s':<12}")
    for row in rows[1:]:
        if "seq_" in row[1]:
            print(f"{row[0]:<12} {row[2]:<16} {row[5]:<12} {row[6]:<12}")

    print()
    print(f"{BOLD}IOPS losowe 4K:{NC}")
    print("-------------------------------------------")
    print(f"{'Mechanizm':<12} {'Test':<20} {'Read_IOPS':<12} {'Write_IOPS':<12}")
    for row in rows[1:]:
        if "rand" in row[1] and "4k" in row[1]:
            print(f"{row[0]:<12} {row[2]:<20} {row[3]:<12} {row[4]:<12}")


def main() -> int:
    check_deps()
    args = parse_args()

    output_dir = Path(f"fio_results_{datetime.now():%Y%m%d_%H%M%S}")
    summary_csv = prepare_results(output_dir)
    baseline_dir = Path(tempfile.mkdtemp(prefix="fio_baseline_"))

    print(f"{BOLD}{CYAN}fio Benchmark – start: {datetime.now():%c}{NC}")

    mount_points = {
        "Baseline": baseline_dir,
        "LUKS": MOUNT_LUKS,
        "eCryptfs": MOUNT_ECRYPTFS,
        "fscrypt": MOUNT_FSCRYPT,
    }
    mounted_here: list[Path] = []

    try:
        luks_mounted = ensure_luks_mounted()
        if luks_mounted:
            mounted_here.append(MOUNT_LUKS)

        ecryptfs_mounted = ensure_ecryptfs_mounted()
        if ecryptfs_mounted:
            mounted_here.append(MOUNT_ECRYPTFS)

        for label in ("Baseline", "LUKS", "eCryptfs", "fscrypt"):
            path = mount_points[label]
            if is_available(path, label):
                run_all_tests(
                    label=label,
                    path=path,
                    file_size=args.size,
                    runtime=args.runtime,
                    iodepth=args.iodepth,
                    numjobs=args.numjobs,
                    output_dir=output_dir,
                    summary_csv=summary_csv,
                    run_as_user=(label == "fscrypt"),
                    io_mode=args.io_mode,
                )
            else:
                print(f"\n{RED}[POMINIĘTO]{NC} {label} – {path} niedostępny lub niezamontowany")
                fill_missing_rows(summary_csv, label)

        print_summary(summary_csv)
        print()
        print(f"{YELLOW}Pełne wyniki JSON i CSV: {BOLD}{output_dir}/{NC}")
        print(f"{YELLOW}Podsumowanie CSV: {BOLD}{summary_csv}{NC}")
        print(f"\n{GREEN}Benchmark zakończony: {datetime.now():%c}{NC}")
    finally:
        shutil.rmtree(baseline_dir, ignore_errors=True)
        for mount_point in reversed(mounted_here):
            cleanup_mount(mount_point, True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
