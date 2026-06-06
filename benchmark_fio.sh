#!/usr/bin/env bash
# =============================================================================
# benchmark_fio.sh – zaawansowany benchmark I/O przy użyciu fio
#
# fio (Flexible I/O Tester) pozwala symulować realistyczne wzorce obciążenia:
#   - zapis/odczyt sekwencyjny (backup, streaming)
#   - losowy odczyt/zapis 4K (bazy danych, systemowe)
#   - mieszany ruch (70% odczyt / 30% zapis) – typowe serwery aplikacyjne
#
# Użycie: sudo bash benchmark_fio.sh [opcje]
# Opcje:
#   --runtime <s>     Czas każdego testu w sekundach (domyślnie: 30)
#   --size <n>M|G     Rozmiar pliku testowego (domyślnie: 512M)
#   --iodepth <n>     Głębokość kolejki I/O (domyślnie: 16)
#   --numjobs <n>     Liczba równoległych wątków (domyślnie: 4)
#
# Wymagania:
#   - fio zainstalowane (apt install fio)
#   - python3 zainstalowane (do parsowania JSON)
#   - Zamontowane/odblokowane środowiska testowe
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Parametry domyślne
# ---------------------------------------------------------------------------
RUNTIME=30              # Czas testu w sekundach
FILE_SIZE="512M"        # Rozmiar pliku fio
BS_RAND="4k"            # Rozmiar bloku dla testów losowych (standardowy dla DB)
BS_SEQ="1M"             # Rozmiar bloku dla sekwencyjnych (backup, streaming)
IODEPTH=16              # Głębokość kolejki asynchronicznej (AIO)
NUMJOBS=4               # Liczba równoległych wątków fio
OUTPUT_DIR="fio_results_$(date +%Y%m%d_%H%M%S)"
SUMMARY_CSV="${OUTPUT_DIR}/summary.csv"
LOG_FILE="${OUTPUT_DIR}/benchmark.log"

# Punkty montowania
MOUNT_LUKS="/mnt/luks_test"
MOUNT_ECRYPTFS="/mnt/ecryptfs_upper"
MOUNT_FSCRYPT="/mnt/fscrypt_test/private_data"

# ---------------------------------------------------------------------------
# Parsowanie argumentów CLI
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime)  RUNTIME="$2";   shift 2 ;;
        --size)     FILE_SIZE="$2"; shift 2 ;;
        --iodepth)  IODEPTH="$2";   shift 2 ;;
        --numjobs)  NUMJOBS="$2";   shift 2 ;;
        *) echo "Nieznana opcja: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Kolory
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ---------------------------------------------------------------------------
# Sprawdzenie zależności
# ---------------------------------------------------------------------------
check_deps() {
    local missing=()
    for cmd in fio python3 bc; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "${RED}[BŁĄD] Brakujące narzędzia: ${missing[*]}${NC}" >&2
        echo "Zainstaluj: sudo apt install fio python3 bc" >&2
        exit 1
    fi
}

check_deps

# ---------------------------------------------------------------------------
# Przygotowanie katalogów wynikowych
# ---------------------------------------------------------------------------
mkdir -p "$OUTPUT_DIR"
mkdir -p "/tmp/fio_baseline_$$"   # Katalog bazowy z unikalnym PID

# Nagłówek CSV z wszystkimi mierzonymi parametrami
cat > "$SUMMARY_CSV" <<'EOF'
Mechanizm,Test,Opis,Read_IOPS,Write_IOPS,Read_BW_MBs,Write_BW_MBs,Read_Lat_us,Write_Lat_us,CPU_usr_%,CPU_sys_%
EOF

echo -e "${BOLD}${CYAN}fio Benchmark – start: $(date)${NC}" | tee "$LOG_FILE"

# ---------------------------------------------------------------------------
# Asocjacyjna tablica: etykieta → ścieżka
# ---------------------------------------------------------------------------
declare -A MOUNT_POINTS=(
    ["Baseline"]="/tmp/fio_baseline_$$"
    ["LUKS"]="$MOUNT_LUKS"
    ["eCryptfs"]="$MOUNT_ECRYPTFS"
    ["fscrypt"]="$MOUNT_FSCRYPT"
)

# Opisy testów (do CSV)
declare -A TEST_DESCRIPTIONS=(
    ["seq_write"]="Zapis sekwencyjny ${BS_SEQ}"
    ["seq_read"]="Odczyt sekwencyjny ${BS_SEQ}"
    ["rand_write_4k"]="Losowy zapis 4K"
    ["rand_read_4k"]="Losowy odczyt 4K"
    ["mixed_70r_30w"]="Mieszany 70%R/30%W 4K"
    ["rand_write_64k"]="Losowy zapis 64K"
    ["rand_read_64k"]="Losowy odczyt 64K"
)

# ---------------------------------------------------------------------------
# Funkcja: uruchom pojedynczy test fio i sparsuj wyniki
#
# Parametry:
#   $1 – etykieta mechanizmu (np. "LUKS")
#   $2 – ścieżka do katalogu testowego
#   $3 – nazwa testu (klucz do TEST_DESCRIPTIONS)
#   $4 – typ I/O fio (write, read, randwrite, randread, randrw)
#   $5 – rozmiar bloku
#   $6 – (opcjonalnie) dodatkowe parametry fio
# ---------------------------------------------------------------------------
run_fio_test() {
    local label="$1"
    local path="$2"
    local test_name="$3"
    local rw="$4"
    local bs="$5"
    local extra_params="${6:-}"
    local description="${TEST_DESCRIPTIONS[$test_name]:-$test_name}"
    local json_out="${OUTPUT_DIR}/${label}_${test_name}.json"
    local fio_log="${OUTPUT_DIR}/${label}_${test_name}.log"

    echo -e "\n${GREEN}[fio]${NC} ${BOLD}${label}${NC} – ${description}"
    echo "  rw=${rw}, bs=${bs}, iodepth=${IODEPTH}, numjobs=${NUMJOBS}, runtime=${RUNTIME}s"

    # Pełna komenda fio z wyjaśnieniem parametrów:
    #
    # --ioengine=libaio   Asynchroniczne I/O Linuxa – realistyczne dla serwerów
    # --direct=1          O_DIRECT – pomija page cache, mierzy rzeczywisty I/O dysku
    #                     WAŻNE: bez tego wyniki byłyby zawyżone przez RAM caching
    # --time_based        Trzymaj runtime, nie zatrzymuj po przeczytaniu pliku
    # --group_reporting   Agreguj statystyki wszystkich numjobs w jeden raport
    # --randrepeat=0      Nie powtarzaj sekwencji losowej – realistyczniejsze obciążenie
    # --norandommap       Nie śledź odwiedzonych bloków – mniej pamięci RAM
    # --filename          Jeden plik testowy zamiast katalogu (spójność między testami)
    fio \
        --name="${label}_${test_name}" \
        --filename="${path}/fio_testfile_${test_name}" \
        --rw="$rw" \
        --bs="$bs" \
        --size="$FILE_SIZE" \
        --runtime="$RUNTIME" \
        --time_based \
        --iodepth="$IODEPTH" \
        --numjobs="$NUMJOBS" \
        --ioengine=libaio \
        --direct=1 \
        --group_reporting \
        --randrepeat=0 \
        --norandommap \
        --output-format=json \
        --output="$json_out" \
        $extra_params \
        > "$fio_log" 2>&1 || {
            echo -e "${RED}  [BŁĄD] fio nie powiódł się. Sprawdź: ${fio_log}${NC}"
            echo "${label},${test_name},${description},ERROR,ERROR,ERROR,ERROR,ERROR,ERROR,ERROR,ERROR" \
                >> "$SUMMARY_CSV"
            return 1
        }

    # Parsowanie JSON z wynikami fio przy użyciu Pythona
    # fio JSON zawiera zagnieżdżone struktury – python3 jest najwygodniejszy
    python3 - "$json_out" "$label" "$test_name" "$description" "$SUMMARY_CSV" <<'PYEOF'
import json, sys, os

json_file   = sys.argv[1]
label       = sys.argv[2]
test_name   = sys.argv[3]
description = sys.argv[4]
csv_file    = sys.argv[5]

try:
    with open(json_file) as f:
        data = json.load(f)
except Exception as e:
    print(f"  [BŁĄD] Nie można sparsować JSON: {e}")
    sys.exit(1)

job = data['jobs'][0]
r   = job['read']
w   = job['write']
cpu = job.get('usr_cpu', 0)
sys_cpu = job.get('sys_cpu', 0)

# Konwersje jednostek
read_iops    = r.get('iops', 0)
write_iops   = w.get('iops', 0)
read_bw_mbs  = r.get('bw', 0) / 1024          # KB/s → MB/s
write_bw_mbs = w.get('bw', 0) / 1024
read_lat_us  = r.get('lat_ns', {}).get('mean', 0) / 1000   # ns → µs
write_lat_us = w.get('lat_ns', {}).get('mean', 0) / 1000
cpu_usr      = job.get('usr_cpu', 0)
cpu_sys      = job.get('sys_cpu', 0)

# Wyświetlenie wyników w terminalu
print(f"  Odczyt:  {read_iops:>8.0f} IOPS  |  {read_bw_mbs:>7.1f} MB/s  |  lat: {read_lat_us:>8.1f} µs")
print(f"  Zapis:   {write_iops:>8.0f} IOPS  |  {write_bw_mbs:>7.1f} MB/s  |  lat: {write_lat_us:>8.1f} µs")
print(f"  CPU:     usr={cpu_usr:.1f}%  sys={cpu_sys:.1f}%")

# Zapis do CSV
with open(csv_file, 'a') as csv:
    csv.write(
        f"{label},{test_name},{description},"
        f"{read_iops:.0f},{write_iops:.0f},"
        f"{read_bw_mbs:.2f},{write_bw_mbs:.2f},"
        f"{read_lat_us:.1f},{write_lat_us:.1f},"
        f"{cpu_usr:.1f},{cpu_sys:.1f}\n"
    )
PYEOF

    # Usunięcie pliku testowego między scenariuszami (zwolnienie miejsca)
    rm -f "${path}/fio_testfile_${test_name}"
}

# ---------------------------------------------------------------------------
# Funkcja: sprawdź dostępność punktu montowania
# ---------------------------------------------------------------------------
is_available() {
    local path="$1"
    local label="$2"

    if [[ "$label" == "Baseline" ]]; then
        return 0   # Baseline zawsze dostępny
    fi

    # Dla eCryptfs i LUKS – sprawdź czy punkt jest zamontowany
    if mountpoint -q "$path" 2>/dev/null; then
        return 0
    fi

    # Dla fscrypt – sprawdź czy katalog istnieje i jest zapisywalny
    if [[ -d "$path" ]] && [[ -w "$path" ]]; then
        return 0
    fi

    return 1
}

# ---------------------------------------------------------------------------
# Funkcja: uruchom komplet testów dla jednego mechanizmu
# ---------------------------------------------------------------------------
run_all_tests() {
    local label="$1"
    local path="$2"

    echo -e "\n${BOLD}${YELLOW}════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${YELLOW}  Testowanie: ${label} (${path})${NC}"
    echo -e "${BOLD}${YELLOW}════════════════════════════════════════════${NC}"

    # 1. Zapis sekwencyjny – symulacja kopiowania dużych plików, backup
    run_fio_test "$label" "$path" "seq_write" "write" "$BS_SEQ"

    # 2. Odczyt sekwencyjny – streaming mediów, duże transfery
    run_fio_test "$label" "$path" "seq_read" "read" "$BS_SEQ"

    # 3. Losowy zapis 4K – bazy danych (PostgreSQL, MySQL), systemowe logi
    run_fio_test "$label" "$path" "rand_write_4k" "randwrite" "$BS_RAND"

    # 4. Losowy odczyt 4K – systemy plików z małymi plikami, metadane
    run_fio_test "$label" "$path" "rand_read_4k" "randread" "$BS_RAND"

    # 5. Mieszany 70% odczyt / 30% zapis – typowe obciążenie serwera webowego
    run_fio_test "$label" "$path" "mixed_70r_30w" "randrw" "$BS_RAND" \
        "--rwmixread=70"

    # 6. Losowy zapis 64K – wirtualizacja (QEMU/KVM disk images), duże rekordy DB
    run_fio_test "$label" "$path" "rand_write_64k" "randwrite" "64k"

    # 7. Losowy odczyt 64K – odczyt stron HTML, pliki konfiguracyjne
    run_fio_test "$label" "$path" "rand_read_64k" "randread" "64k"
}

# ---------------------------------------------------------------------------
# GŁÓWNA PĘTLA: iteracja po wszystkich mechanizmach szyfrowania
# ---------------------------------------------------------------------------
LABELS=("Baseline" "LUKS" "eCryptfs" "fscrypt")

for label in "${LABELS[@]}"; do
    path="${MOUNT_POINTS[$label]}"

    if is_available "$path" "$label"; then
        run_all_tests "$label" "$path"
    else
        echo -e "\n${RED}[POMINIĘTO]${NC} ${label} – ${path} niedostępny lub niezamontowany"
        # Zapisz puste wyniki do CSV żeby tabela była kompletna
        for test_name in "seq_write" "seq_read" "rand_write_4k" "rand_read_4k" \
                         "mixed_70r_30w" "rand_write_64k" "rand_read_64k"; do
            desc="${TEST_DESCRIPTIONS[$test_name]:-$test_name}"
            echo "${label},${test_name},${desc},N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A" \
                >> "$SUMMARY_CSV"
        done
    fi
done

# ---------------------------------------------------------------------------
# Wyświetlenie zbiorczego podsumowania
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}${CYAN}════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}${CYAN}  PODSUMOWANIE WYNIKÓW FIO${NC}"
echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════════${NC}"
echo ""

# Tabela przepustowości (zapis + odczyt sekwencyjny)
echo -e "${BOLD}Przepustowość sekwencyjna (MB/s):${NC}"
echo "-------------------------------------------"
printf "%-12s %-6s %-12s %-12s\n" "Mechanizm" "Test" "Odczyt_MB/s" "Zapis_MB/s"
grep "seq_" "$SUMMARY_CSV" | awk -F',' '{printf "%-12s %-16s %-12s %-12s\n", $1, $3, $6, $7}'

echo ""
echo -e "${BOLD}IOPS losowe 4K:${NC}"
echo "-------------------------------------------"
printf "%-12s %-20s %-12s %-12s\n" "Mechanizm" "Test" "Read_IOPS" "Write_IOPS"
grep "rand.*4k" "$SUMMARY_CSV" | awk -F',' '{printf "%-12s %-20s %-12s %-12s\n", $1, $3, $4, $5}'

echo ""
echo -e "${YELLOW}Pełne wyniki JSON i CSV: ${BOLD}${OUTPUT_DIR}/${NC}"
echo -e "${YELLOW}Podsumowanie CSV: ${BOLD}${SUMMARY_CSV}${NC}"

# ---------------------------------------------------------------------------
# Sprzątanie
# ---------------------------------------------------------------------------
rm -rf "/tmp/fio_baseline_$$"

echo -e "\n${GREEN}Benchmark zakończony: $(date)${NC}" | tee -a "$LOG_FILE"
