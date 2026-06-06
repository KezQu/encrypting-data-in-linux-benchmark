#!/usr/bin/env python3
"""Benchmark I/O przy użyciu dd dla filesystemów szyfrowanych i bazowego."""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import statistics
import subprocess
from datetime import datetime
import tempfile
from pathlib import Path

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"

TEST_FILE = "dd_benchmark_tmp.bin"
CSV_HEADER = ["Mechanizm", "Operacja", "Prędkość_MBs"]

SPEED_RE = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>MiB|MB|GiB|GB)/s")
FALLBACK_RE = re.compile(r"(?P<bytes>\d+)\s+bytes.*?,\s*(?P<seconds>\d+(?:[.,]\d+)?)\s+s,")


def run_command(command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark dd dla baseline, LUKS, eCryptfs i fscrypt."
    )
    parser.add_argument("--size", type=int, default=512, help="Rozmiar pliku testowego w MB")
    parser.add_argument("--block", default="1M", help="Rozmiar bloku I/O dla dd")
    parser.add_argument("--repeat", type=int, default=3, help="Liczba powtórzeń każdego testu")
    return parser.parse_args()


def print_header(title: str) -> None:
    print()
    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════╗{NC}")
    print(f"{BOLD}{CYAN}║  {title}{NC}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════╝{NC}")


def drop_caches() -> None:
    subprocess.run(["sync"], check=True)
    if os.geteuid() == 0:
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="utf-8")
        return

    subprocess.run(
        ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
        check=True,
    )


def invoking_user_command(command: list[str]) -> list[str]:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        return ["sudo", "-u", sudo_user, "--", *command]
    return command


def run_dd(command: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return subprocess.run(
        invoking_user_command(command),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def parse_dd_speed(output: str) -> float:
    unit_multipliers = {
        "MB": 1.0,
        "MiB": 1.048576,
        "GB": 1000.0,
        "GiB": 1073.741824,
    }

    matches = list(SPEED_RE.finditer(output))
    if matches:
        match = matches[-1]
        value = float(match.group("value").replace(",", "."))
        return round(value * unit_multipliers[match.group("unit")], 2)

    fallback = FALLBACK_RE.search(output)
    if fallback:
        bytes_written = float(fallback.group("bytes"))
        seconds = float(fallback.group("seconds").replace(",", "."))
        return round((bytes_written / seconds) / 1_000_000, 2)

    return 0.0


def average_speed(speeds: list[float]) -> float:
    return round(statistics.fmean(speeds), 2) if speeds else 0.0


def test_write(path: Path, label: str, file_size_mb: int, block_size: str, repeat: int, results_file: Path) -> None:
    target = path / TEST_FILE
    speeds: list[float] = []

    print(f"{GREEN}[WRITE]{NC} {BOLD}{label}{NC} – {file_size_mb} MB, blok {block_size}")

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
            print(f"  Próba {iteration}/{repeat}: {speed} MB/s")
    finally:
        target.unlink(missing_ok=True)

    avg = average_speed(speeds)
    print(f"  {GREEN}Średnia: {avg} MB/s{NC}")
    with results_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label, "WRITE", avg])


def test_read(path: Path, label: str, file_size_mb: int, block_size: str, repeat: int, results_file: Path) -> None:
    target = path / TEST_FILE
    speeds: list[float] = []

    print(f"{GREEN}[READ]{NC}  {BOLD}{label}{NC} – {file_size_mb} MB, blok {block_size}")

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
            print(f"  Próba {iteration}/{repeat}: {speed} MB/s")
    finally:
        target.unlink(missing_ok=True)

    avg = average_speed(speeds)
    print(f"  {GREEN}Średnia: {avg} MB/s{NC}")
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
        return False

    mapper_device = Path("/dev/mapper/luks_test")
    if not mapper_device.exists():
        source_device = Path("/dev/sdb")
        if not source_device.exists():
            print(f"{RED}[POMINIĘTO]{NC} LUKS – brak {source_device}")
            return False

        try:
            run_command(["sudo", "cryptsetup", "open", str(source_device), "luks_test"])
        except subprocess.CalledProcessError as exc:
            print(f"{RED}[POMINIĘTO]{NC} LUKS – nie udało się otworzyć {source_device}: {exc.stderr.strip() if exc.stderr else exc}")
            return False

    if not mapper_device.exists():
        print(f"{RED}[POMINIĘTO]{NC} LUKS – brak /dev/mapper/luks_test")
        return False

    mount_point.mkdir(parents=True, exist_ok=True)
    try:
        run_command(["sudo", "mount", str(mapper_device), str(mount_point)])
    except subprocess.CalledProcessError as exc:
        print(f"{RED}[POMINIĘTO]{NC} LUKS – nie udało się zamontować {mount_point}: {exc.stderr.strip() if exc.stderr else exc}")
        return False
    return True


def ensure_ecryptfs_mounted(mount_point: Path, lower_dir: Path) -> bool:
    if mountpoint_exists(mount_point):
        return False

    if not lower_dir.exists():
        print(f"{RED}[POMINIĘTO]{NC} eCryptfs – brak katalogu dolnego {lower_dir}")
        return False

    mount_point.mkdir(parents=True, exist_ok=True)
    try:
        run_command(
            [
                "mount",
                "-t",
                "ecryptfs",
                str(lower_dir),
                str(mount_point),
                "-o",
                "ecryptfs_cipher=aes,ecryptfs_key_bytes=32,ecryptfs_passthrough=n,ecryptfs_enable_filename_crypto=y",
            ],
            input_text="\n\n\n\n\n\nyes\n",
        )
    except subprocess.CalledProcessError as exc:
        print(f"{RED}[POMINIĘTO]{NC} eCryptfs – nie udało się zamontować {mount_point}: {exc.stderr.strip() if exc.stderr else exc}")
        return False
    return True


def unmount_if_needed(path: Path, mounted_here: bool) -> None:
    if not mounted_here:
        return

    subprocess.run(["umount", str(path)], check=False)


def print_results(results_file: Path) -> None:
    rows: list[list[str]] = []
    with results_file.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)

    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


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
    mount_ecryptfs = Path("/mnt/ecryptfs_upper")
    mount_fscrypt = Path("/mnt/fscrypt_test/private_data")
    baseline_dir = Path(tempfile.mkdtemp(prefix="dd_baseline_"))
    results_file = Path(f"wyniki_dd_{datetime.now():%Y%m%d_%H%M%S}.csv")

    with results_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)

    print_header("Benchmark dd – Porównanie szyfrowania Linux")
    print(f"  Plik CSV wyników: {YELLOW}{results_file}{NC}")
    print(f"  Rozmiar pliku testowego: {args.size} MB")
    print(f"  Rozmiar bloku: {args.block}")
    print(f"  Liczba powtórzeń: {args.repeat}")

    mounted_paths: list[Path] = []

    try:
        print_header("1. Baseline – bez szyfrowania")
        available_mb = available_space_mb(baseline_dir)
        if available_mb < args.size * 2:
            print(f"{RED}[OSTRZEŻENIE]{NC} Mało miejsca w /tmp ({available_mb} MB). Zmniejsz --size")
        test_write(baseline_dir, "Baseline", args.size, args.block, args.repeat, results_file)
        test_read(baseline_dir, "Baseline", args.size, args.block, args.repeat, results_file)
        shutil.rmtree(baseline_dir, ignore_errors=True)

        print_header("2. LUKS (szyfrowanie blokowe)")
        luks_mounted = ensure_luks_mounted(mount_luks)
        if mountpoint_exists(mount_luks):
            test_write(mount_luks, "LUKS", args.size, args.block, args.repeat, results_file)
            test_read(mount_luks, "LUKS", args.size, args.block, args.repeat, results_file)
            if luks_mounted:
                mounted_paths.append(mount_luks)
        else:
            print(f"{RED}[POMINIĘTO]{NC} LUKS – {mount_luks} nie jest zamontowany")
            append_na(results_file, "LUKS")

        print_header("3. eCryptfs (szyfrowanie per-plik)")
        ecryptfs_lower = Path("/mnt/ecryptfs_lower")
        ecryptfs_mounted = ensure_ecryptfs_mounted(mount_ecryptfs, ecryptfs_lower)
        if mountpoint_exists(mount_ecryptfs):
            test_write(mount_ecryptfs, "eCryptfs", args.size, args.block, args.repeat, results_file)
            test_read(mount_ecryptfs, "eCryptfs", args.size, args.block, args.repeat, results_file)
            if ecryptfs_mounted:
                mounted_paths.append(mount_ecryptfs)
        else:
            print(f"{RED}[POMINIĘTO]{NC} eCryptfs – {mount_ecryptfs} nie jest zamontowany")
            append_na(results_file, "eCryptfs")

        print_header("4. fscrypt (szyfrowanie per-katalog)")
        if mount_fscrypt.is_dir() and os.access(mount_fscrypt, os.W_OK):
            test_write(mount_fscrypt, "fscrypt", args.size, args.block, args.repeat, results_file)
            test_read(mount_fscrypt, "fscrypt", args.size, args.block, args.repeat, results_file)
        else:
            print(f"{RED}[POMINIĘTO]{NC} fscrypt – {mount_fscrypt} niedostępny lub zablokowany")
            append_na(results_file, "fscrypt")

        print_header("WYNIKI KOŃCOWE")
        print()
        print_results(results_file)
        print()
        print(f"{YELLOW}Wyniki zapisane w: {BOLD}{results_file}{NC}")
    finally:
        shutil.rmtree(baseline_dir, ignore_errors=True)
        for mount_point in reversed(mounted_paths):
            unmount_if_needed(mount_point, True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
