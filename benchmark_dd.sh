#!/usr/bin/env bash
# =============================================================================
# benchmark_dd.sh – prosty benchmark I/O przy użyciu dd
# Mierzy przepustowość zapisu i odczytu sekwencyjnego dla trzech mechanizmów
# szyfrowania oraz bazowego (niezaszyfrowanego) systemu plików.
#
# Użycie: sudo bash benchmark_dd.sh [--size <MB>] [--block <rozmiar>]
#
# Wymagania:
#   - LUKS zamontowany pod /mnt/luks_test
#   - eCryptfs zamontowany pod /mnt/ecryptfs_upper
#   - fscrypt zamontowany i odblokowany pod /mnt/fscrypt_test/private_data
#
# Wynik: plik CSV wyniki_dd_<timestamp>.csv w bieżącym katalogu
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Parametry domyślne (można nadpisać przez argumenty CLI)
# ---------------------------------------------------------------------------
BLOCK_SIZE="1M"       # Rozmiar bloku I/O dla dd
FILE_SIZE_MB=512      # Rozmiar pliku testowego w MB
REPEAT=3              # Liczba powtórzeń każdego testu (dla uśrednienia)
TEST_FILE="dd_benchmark_tmp.bin"

# Punkty montowania – dostosuj do swojego środowiska
MOUNT_LUKS="/mnt/luks_test"
MOUNT_ECRYPTFS="/mnt/ecryptfs_upper"
MOUNT_FSCRYPT="/mnt/fscrypt_test/private_data"
BASELINE_DIR="/tmp/dd_baseline_$$"   # $$ = PID, unikalny katalog tymczasowy

# ---------------------------------------------------------------------------
# Parsowanie argumentów CLI
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --size)   FILE_SIZE_MB="$2"; shift 2 ;;
        --block)  BLOCK_SIZE="$2";   shift 2 ;;
        --repeat) REPEAT="$2";       shift 2 ;;
        *) echo "Nieznana opcja: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Kolory ANSI dla czytelności wyjścia w terminalu
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'   # No Color (reset)

RESULTS_FILE="wyniki_dd_$(date +%Y%m%d_%H%M%S).csv"

# ---------------------------------------------------------------------------
# Funkcja: wydrukuj nagłówek sekcji
# ---------------------------------------------------------------------------
print_header() {
    echo -e "\n${BOLD}${CYAN}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}║  $1${NC}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${NC}"
}

# ---------------------------------------------------------------------------
# Funkcja: wyczyść page cache i dentries przed pomiarem
# Zapewnia, że dane są odczytywane z dysku (nie z RAM), co daje
# wiarygodne wyniki dla testów szyfrowania.
# ---------------------------------------------------------------------------
drop_caches() {
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Funkcja: uruchom polecenie jako użytkownik, który wywołał sudo
# Pozwala testować fscrypt z uprawnieniami właściciela odblokowanego katalogu.
# ---------------------------------------------------------------------------
run_as_invoking_user() {
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        sudo -u "$SUDO_USER" -- "$@"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Funkcja: wyodrębnij prędkość z wyjścia dd (w MB/s)
# dd wypisuje statystyki na stderr w formacie:
#   "X bytes (Y GB) copied, Z s, W MB/s"
# ---------------------------------------------------------------------------
parse_dd_speed() {
    # Wyodrębniamy ostatnią wartość liczbową przed "MB/s" lub "GB/s"
    local output="$1"
    local speed unit

    # Próba dopasowania MB/s
    speed=$(echo "$output" | grep -oP '[\d,.]+ MB/s' | grep -oP '[\d,.]+' | tr ',' '.')
    if [[ -n "$speed" ]]; then
        echo "$speed"
        return
    fi

    # Próba dopasowania GB/s → konwersja na MB/s
    speed=$(echo "$output" | grep -oP '[\d,.]+ GB/s' | grep -oP '[\d,.]+' | tr ',' '.')
    if [[ -n "$speed" ]]; then
        echo "$(echo "$speed * 1024" | bc)"
        return
    fi

    # Obliczenie ręczne: bajtów / czas
    local bytes time_s
    bytes=$(echo "$output" | grep -oP '^\d+')
    time_s=$(echo "$output" | grep -oP '[\d.]+ s,' | grep -oP '[\d.]+')
    if [[ -n "$bytes" && -n "$time_s" ]]; then
        echo "$(echo "scale=2; $bytes / $time_s / 1048576" | bc)"
        return
    fi

    echo "0"
}

# ---------------------------------------------------------------------------
# Funkcja: test zapisu dd
# Parametry:
#   $1 – ścieżka do katalogu testowego
#   $2 – etykieta mechanizmu (dla raportu i CSV)
# ---------------------------------------------------------------------------
test_write() {
    local path="$1"
    local label="$2"
    local target="${path}/${TEST_FILE}"
    local speeds=()

    echo -e "${GREEN}[WRITE]${NC} ${BOLD}${label}${NC} – ${FILE_SIZE_MB} MB, blok ${BLOCK_SIZE}"

    for (( i=1; i<=REPEAT; i++ )); do
        drop_caches

        # dd zapisuje losowe dane (/dev/urandom) do pliku testowego.
        # Używamy /dev/urandom (a nie /dev/zero) bo niektóre systemy szyfrowania
        # mogą optymalizować zapis samych zer – to byłoby nienaturalne obciążenie.
        local dd_output
        dd_output=$(run_as_invoking_user dd if=/dev/urandom \
                              of="$target" \
                              bs="$BLOCK_SIZE" \
                              count="$FILE_SIZE_MB" \
                              conv=fsync \
                              2>&1 | tail -1)

        local speed
        speed=$(parse_dd_speed "$dd_output")
        speeds+=("$speed")
        printf "  Próba %d/%d: %s MB/s\n" "$i" "$REPEAT" "$speed"
    done

    # Obliczenie średniej arytmetycznej
    local sum=0
    for s in "${speeds[@]}"; do
        sum=$(echo "$sum + $s" | bc)
    done
    local avg
    avg=$(echo "scale=2; $sum / $REPEAT" | bc)

    echo -e "  ${GREEN}Średnia: ${avg} MB/s${NC}"
    echo "${label},WRITE,${avg}" >> "$RESULTS_FILE"

    # Usuń plik testowy (zwolnienie miejsca przed testem odczytu)
    sudo rm -f "$target"
}

# ---------------------------------------------------------------------------
# Funkcja: test odczytu dd
# ---------------------------------------------------------------------------
test_read() {
    local path="$1"
    local label="$2"
    local target="${path}/${TEST_FILE}"
    local speeds=()

    echo -e "${GREEN}[READ]${NC}  ${BOLD}${label}${NC} – ${FILE_SIZE_MB} MB, blok ${BLOCK_SIZE}"

    # Najpierw utwórz plik testowy (stałe dane, nie losowe – szybszy zapis)
    run_as_invoking_user dd if=/dev/zero of="$target" bs="$BLOCK_SIZE" \
        count="$FILE_SIZE_MB" conv=fsync 2>/dev/null

    for (( i=1; i<=REPEAT; i++ )); do
        drop_caches   # Kluczowe: bez czyszczenia cache odczyt byłby z RAM!

        local dd_output
        dd_output=$(run_as_invoking_user dd if="$target" \
                              of=/dev/null \
                              bs="$BLOCK_SIZE" \
                              2>&1 | tail -1)

        local speed
        speed=$(parse_dd_speed "$dd_output")
        speeds+=("$speed")
        printf "  Próba %d/%d: %s MB/s\n" "$i" "$REPEAT" "$speed"
    done

    local sum=0
    for s in "${speeds[@]}"; do
        sum=$(echo "$sum + $s" | bc)
    done
    local avg
    avg=$(echo "scale=2; $sum / $REPEAT" | bc)

    echo -e "  ${GREEN}Średnia: ${avg} MB/s${NC}"
    echo "${label},READ,${avg}" >> "$RESULTS_FILE"

    sudo rm -f "$target"
}

# ---------------------------------------------------------------------------
# GŁÓWNA LOGIKA SKRYPTU
# ---------------------------------------------------------------------------

print_header "Benchmark dd – Porównanie szyfrowania Linux"
echo -e "  Plik CSV wyników: ${YELLOW}${RESULTS_FILE}${NC}"
echo -e "  Rozmiar pliku testowego: ${FILE_SIZE_MB} MB"
echo -e "  Rozmiar bloku: ${BLOCK_SIZE}"
echo -e "  Liczba powtórzeń: ${REPEAT}"

# Nagłówek CSV
echo "Mechanizm,Operacja,Prędkość_MBs" > "$RESULTS_FILE"

# --- Test bazowy (bez szyfrowania) ---
print_header "1. Baseline – bez szyfrowania"
mkdir -p "$BASELINE_DIR"
# Sprawdzenie miejsca na dysku w katalogu tymczasowym
AVAILABLE_MB=$(df -BM "$BASELINE_DIR" | awk 'NR==2 {gsub("M",""); print $4}')
if (( AVAILABLE_MB < FILE_SIZE_MB * 2 )); then
    echo -e "${RED}[OSTRZEŻENIE] Mało miejsca w /tmp (${AVAILABLE_MB} MB). Zmniejsz --size${NC}"
fi
test_write "$BASELINE_DIR" "Baseline"
test_read  "$BASELINE_DIR" "Baseline"
rm -rf "$BASELINE_DIR"

# --- Test LUKS ---
print_header "2. LUKS (szyfrowanie blokowe)"
if mountpoint -q "$MOUNT_LUKS" 2>/dev/null; then
    test_write "$MOUNT_LUKS" "LUKS"
    test_read  "$MOUNT_LUKS" "LUKS"
else
    echo -e "${RED}[POMINIĘTO]${NC} LUKS – ${MOUNT_LUKS} nie jest zamontowany"
    echo "LUKS,WRITE,N/A" >> "$RESULTS_FILE"
    echo "LUKS,READ,N/A"  >> "$RESULTS_FILE"
fi

# --- Test eCryptfs ---
print_header "3. eCryptfs (szyfrowanie per-plik)"
if mountpoint -q "$MOUNT_ECRYPTFS" 2>/dev/null; then
    test_write "$MOUNT_ECRYPTFS" "eCryptfs"
    test_read  "$MOUNT_ECRYPTFS" "eCryptfs"
else
    echo -e "${RED}[POMINIĘTO]${NC} eCryptfs – ${MOUNT_ECRYPTFS} nie jest zamontowany"
    echo "eCryptfs,WRITE,N/A" >> "$RESULTS_FILE"
    echo "eCryptfs,READ,N/A"  >> "$RESULTS_FILE"
fi

# --- Test fscrypt ---
print_header "4. fscrypt (szyfrowanie per-katalog)"
if [[ -d "$MOUNT_FSCRYPT" ]] && [[ -w "$MOUNT_FSCRYPT" ]]; then
    test_write "$MOUNT_FSCRYPT" "fscrypt"
    test_read  "$MOUNT_FSCRYPT" "fscrypt"
else
    echo -e "${RED}[POMINIĘTO]${NC} fscrypt – ${MOUNT_FSCRYPT} niedostępny lub zablokowany"
    echo "fscrypt,WRITE,N/A" >> "$RESULTS_FILE"
    echo "fscrypt,READ,N/A"  >> "$RESULTS_FILE"
fi

# ---------------------------------------------------------------------------
# Wyświetlenie tabeli wynikowej
# ---------------------------------------------------------------------------
print_header "WYNIKI KOŃCOWE"
echo ""
column -t -s',' "$RESULTS_FILE"
echo ""
echo -e "${YELLOW}Wyniki zapisane w: ${BOLD}${RESULTS_FILE}${NC}"
