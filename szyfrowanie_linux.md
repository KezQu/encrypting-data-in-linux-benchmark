# Szyfrowanie danych w systemie Linux: analiza porównawcza LUKS, eCryptfs i fscrypt

> **Środowisko testowe:** Ubuntu 22.04 LTS (maszyna wirtualna)
> **Cel:** Wdrożenie i porównanie mechanizmów szyfrowania LUKS, eCryptfs oraz fscrypt, benchmarki I/O, analiza modeli ochrony i rekomendacje użycia.

---

## Spis treści

1. [Przygotowanie środowiska testowego](#1-przygotowanie-środowiska-testowego)
2. [LUKS – szyfrowanie blokowe](#2-luks--szyfrowanie-blokowe)
3. [eCryptfs – szyfrowanie na poziomie systemu plików](#3-ecryptfs--szyfrowanie-na-poziomie-systemu-plików)
4. [fscrypt – szyfrowanie katalogów w ext4/f2fs](#4-fscrypt--szyfrowanie-katalogów-w-ext4f2fs)
5. [Benchmarki I/O](#5-benchmarki-io)
6. [Analiza modelu ochrony](#6-analiza-modelu-ochrony)
7. [Rekomendacje dla scenariuszy użycia](#7-rekomendacje-dla-scenariuszy-użycia)
8. [Podsumowanie](#8-podsumowanie)

---

## 1. Przygotowanie środowiska testowego

### 1.1 Konfiguracja maszyny wirtualnej Ubuntu

Zalecana konfiguracja VM (np. VirtualBox / VMware / QEMU):

| Parametr          | Wartość minimalna |
| ----------------- | ----------------- |
| System operacyjny | Ubuntu 22.04 LTS  |
| RAM               | 2 GB              |
| CPU               | 2 vCPU            |
| Dysk systemowy    | 20 GB             |
| Dysk testowy      | 5 GB (dla LUKS)   |

### 1.2 Aktualizacja systemu i instalacja narzędzi

```bash
# Aktualizacja listy pakietów i systemu – ważne przed instalacją nowych narzędzi,
# aby mieć pewność, że pobierane są aktualne wersje.
sudo apt update && sudo apt upgrade -y

# Instalacja wszystkich narzędzi potrzebnych w projekcie:
#   cryptsetup   – obsługa LUKS (szyfrowanie blokowe)
#   ecryptfs-utils – narzędzia eCryptfs (szyfrowanie na poziomie pliku)
#   fscrypt      – narzędzie CLI dla fscrypt (szyfrowanie katalogów)
#   fio          – elastyczne narzędzie do benchmarków I/O
#   hdparm       – testy przepustowości dysku (odczyt z cache i bez)
#   dd           – kopiowanie i testowanie bloków danych (wbudowane, ale upewniamy się)
#   util-linux   – zawiera lsblk, blkid itp.
#   pv           – monitorowanie przepływu danych w potoku
#   time         – mierzenie czasu wykonania poleceń
sudo apt install -y \
    cryptsetup \
    ecryptfs-utils \
    fscrypt \
    libpam-fscrypt \
    fio \
    hdparm \
    pv \
    util-linux \
    tree \
    htop

# Sprawdzenie zainstalowanych wersji narzędzi kryptograficznych
cryptsetup --version
ecryptfs-setup-private --help | head -5
fscrypt --version
```

### 1.3 Przygotowanie wirtualnego dysku testowego

```bash
# Tworzymy plik o rozmiarze 2 GB jako wirtualny dysk dla testów LUKS.
# Opcja /dev/urandom zapewnia, że dysk jest wypełniony losowymi danymi –
# utrudnia to atakującemu odróżnienie zaszyfrowanych danych od szumu.
sudo dd if=/dev/urandom of=/tmp/disk_test.img bs=1M count=2048 status=progress

# Mapujemy plik obrazu jako urządzenie blokowe za pomocą loop device.
# Polecenie zwróci nazwę urządzenia, np. /dev/loop0
sudo losetup --find --show /tmp/disk_test.img

# Weryfikacja – wylistuj wszystkie aktywne loop devices
sudo losetup -l

# Zapisujemy nazwę urządzenia do zmiennej dla wygody (dostosuj jeśli inna)
LOOP_DEV=$(sudo losetup --find --show /tmp/disk_test.img 2>/dev/null || echo "/dev/loop0")
echo "Urządzenie testowe: $LOOP_DEV"
```

---

## 2. LUKS – szyfrowanie blokowe

### Teoria

**LUKS** (Linux Unified Key Setup) to standard szyfrowania na poziomie urządzenia blokowego.
Działa w warstwie dm-crypt jądra Linux. Szyfruje całą partycję/dysk, system plików jest umieszczony wewnątrz zaszyfrowanego kontenera.

```
┌──────────────────────────────────────────────┐
│  Aplikacja / System plików (ext4, xfs, ...)  │
├──────────────────────────────────────────────┤
│            dm-crypt (LUKS)                   │  ← warstwa szyfrowania
├──────────────────────────────────────────────┤
│        Urządzenie blokowe (/dev/sdX)         │
└──────────────────────────────────────────────┘
```

### 2.1 Inicjalizacja kontenera LUKS

```bash
# Zmienna środowiskowa – dostosuj do swojego środowiska
LOOP_DEV="/dev/loop0"   # lub wynik z losetup --find --show

# Inicjalizacja LUKS2 na urządzeniu blokowym.
# --type luks2     – używamy nowszego formatu LUKS2 (lepsze KDF, większe możliwości)
# --cipher         – algorytm szyfrowania: AES w trybie XTS (standard dla dysków)
# --key-size 512   – 512 bitów klucza (XTS używa dwóch 256-bitowych kluczy)
# --hash sha256    – funkcja skrótu dla PBKDF
# --pbkdf argon2id – nowoczesna funkcja wyprowadzania klucza, odporna na GPU/ASIC
# --iter-time 2000 – czas iteracji PBKDF w ms (im więcej, tym bezpieczniej, ale wolniej)
# --verify-passphrase – dwukrotne podanie hasła podczas inicjalizacji
sudo cryptsetup luksFormat \
    --type luks2 \
    --cipher aes-xts-plain64 \
    --key-size 512 \
    --hash sha256 \
    --pbkdf argon2id \
    --iter-time 2000 \
    --verify-passphrase \
    "$LOOP_DEV"

# Wyświetlenie nagłówka LUKS – weryfikacja parametrów konfiguracji
sudo cryptsetup luksDump "$LOOP_DEV"
```

### 2.2 Otwieranie i montowanie kontenera

```bash
# Otwieramy kontener LUKS i mapujemy go jako urządzenie /dev/mapper/luks_test.
# Po tej operacji urządzenie /dev/mapper/luks_test zawiera odszyfrowane dane.
sudo cryptsetup open "$LOOP_DEV" luks_test
# Podaj hasło ustawione w luksFormat

# Weryfikacja – sprawdzamy czy urządzenie zostało zmapowane
ls -la /dev/mapper/luks_test
sudo cryptsetup status luks_test

# Tworzenie systemu plików ext4 wewnątrz zaszyfrowanego kontenera.
# -L "luks_vol" – etykieta wolumenu dla łatwej identyfikacji
sudo mkfs.ext4 -L "luks_vol" /dev/mapper/luks_test

# Montowanie zaszyfrowanego systemu plików
sudo mkdir -p /mnt/luks_test

# Ustawienie właściwości katalogów dla bieżącego użytkownika
sudo chown $USER:$USER /mnt/luks_test

sudo mount /dev/mapper/luks_test /mnt/luks_test

# Sprawdzenie zamontowanych wolumenów
df -h /mnt/luks_test
```

### 2.3 Testowanie odczytu/zapisu

```bash
# Podstawowy test zapisu – tworzymy plik testowy 100 MB
echo "Test zapisu LUKS:"
time sudo dd if=/dev/urandom of=/mnt/luks_test/testfile.bin bs=1M count=100 status=progress

# Podstawowy test odczytu – odczytujemy plik do /dev/null (pomijamy wyjście)
echo "Test odczytu LUKS:"
time sudo dd if=/mnt/luks_test/testfile.bin of=/dev/null bs=1M status=progress

# Czyszczenie cache systemu (page cache, dentries, inodes) przed kolejnym testem.
# Zapewnia to, że dane są odczytywane z dysku, a nie z pamięci RAM.
sync && echo 3 | sudo tee /proc/sys/vm/drop_caches
```

### 2.4 Zarządzanie kluczami LUKS

```bash
# LUKS obsługuje do 32 slotów na klucze (LUKS2) – możemy dodać klucz awaryjny.
# Dodanie drugiego hasła (np. awaryjnego) do slotu 1
sudo cryptsetup luksAddKey "$LOOP_DEV"
# Podaj istniejące hasło, potem nowe

# Listowanie aktywnych slotów kluczy
sudo cryptsetup luksDump "$LOOP_DEV" | grep -A2 "Keyslot"

# Usunięcie klucza ze slotu 1 (bezpieczne usunięcie dostępu)
# UWAGA: nie usuwaj ostatniego klucza – utracisz dostęp do danych!
sudo cryptsetup luksKillSlot "$LOOP_DEV" 1
```

### 2.5 Zamknięcie kontenera LUKS

```bash
# Odmontowanie systemu plików
sudo umount /mnt/luks_test

# Zamknięcie kontenera dm-crypt – dane są teraz zaszyfrowane i niedostępne
sudo cryptsetup close luks_test

# Weryfikacja – urządzenie nie powinno istnieć
ls /dev/mapper/ | grep luks_test || echo "Kontener poprawnie zamknięty"
```

---

## 3. eCryptfs – szyfrowanie na poziomie systemu plików

### Teoria

**eCryptfs** działa jako stos nad istniejącym systemem plików. Każdy plik jest szyfrowany indywidualnie – nagłówek kryptograficzny jest przechowywany w samym pliku. Umożliwia selektywne szyfrowanie katalogów.

```
┌──────────────────────────────────────────────┐
│              Aplikacja                        │
├──────────────────────────────────────────────┤
│   eCryptfs VFS (warstwa szyfrowania pliku)   │  ← szyfrowanie per-plik
├──────────────────────────────────────────────┤
│   Dolny system plików (ext4, xfs, ...)        │  ← zaszyfrowane pliki na dysku
└──────────────────────────────────────────────┘
```

### 3.1 Konfiguracja środowiska eCryptfs

```bash
# Załadowanie modułu jądra eCryptfs – wymagane przed pierwszym użyciem
sudo modprobe ecryptfs

# Weryfikacja załadowania modułu
lsmod | grep ecryptfs

# Tworzenie katalogu źródłowego (lower) i docelowego (upper/mountpoint)
# lower  – tu przechowywane są zaszyfrowane pliki na dysku
# upper  – tu aplikacje widzą odszyfrowane dane (mountpoint eCryptfs)
sudo mkdir -p /mnt/ecryptfs_lower
sudo mkdir -p /mnt/ecryptfs_upper

# Ustawienie właściwości katalogów dla bieżącego użytkownika
sudo chown $USER:$USER /mnt/ecryptfs_lower /mnt/ecryptfs_upper
```

### 3.2 Montowanie systemu eCryptfs

```bash
# Montowanie eCryptfs z opcjami kryptograficznymi:
# -t ecryptfs              – typ systemu plików
# ecryptfs_cipher=aes      – algorytm szyfrowania (AES)
# ecryptfs_key_bytes=32    – rozmiar klucza: 32 bajty = 256 bitów
# ecryptfs_passthrough=n   – nie przepuszczaj niezaszyfrowanych plików
# ecryptfs_enable_filename_crypto=y – szyfruj też nazwy plików
# ecryptfs_fnek_sig        – podpis klucza szyfrowania nazw plików (podawany automatycznie)
sudo mount -t ecryptfs \
    /mnt/ecryptfs_lower \
    /mnt/ecryptfs_upper \
    -o ecryptfs_cipher=aes,\
ecryptfs_key_bytes=32,\
ecryptfs_passthrough=n,\
ecryptfs_enable_filename_crypto=y

# Przy pierwszym montowaniu system pyta o hasło i potwierdza parametry.
# Wpisz hasło, a następnie potwierdź parametry odpowiadając 'yes'.

# Sprawdzenie zamontowanego systemu plików
mount | grep ecryptfs
df -h /mnt/ecryptfs_upper
```

### 3.3 Testowanie szyfrowania plików

```bash
# Tworzenie pliku testowego w katalogu eCryptfs (dane widoczne odszyfrowane)
echo "Tajne dane testowe eCryptfs" > /mnt/ecryptfs_upper/tajne.txt
cat /mnt/ecryptfs_upper/tajne.txt   # Powinien wyświetlić czytelny tekst

# Sprawdzenie jak plik wygląda w katalogu dolnym (zaszyfrowanym)
# Nazwa pliku powinna być zaszyfrowana (ciąg znaków base64-like)
ls -la /mnt/ecryptfs_lower/
# Próba odczytania zaszyfrowanego pliku wprost z lower dir – powinny być śmieci
sudo hexdump -C /mnt/ecryptfs_lower/$(ls /mnt/ecryptfs_lower/ | head -1) | head -20

# Test zapisu większego pliku dla benchmarku
time dd if=/dev/urandom of=/mnt/ecryptfs_upper/testfile.bin bs=1M count=100 status=progress

# Test odczytu
sync && echo 3 | sudo tee /proc/sys/vm/drop_caches
time dd if=/mnt/ecryptfs_upper/testfile.bin of=/dev/null bs=1M status=progress
```

### 3.4 Odmontowanie eCryptfs

```bash
# Odmontowanie eCryptfs – po tym dane są dostępne tylko w zaszyfrowanej formie w lower
sudo umount /mnt/ecryptfs_upper

# Weryfikacja – próba odczytu przez upper dir powinna się nie powieść
ls /mnt/ecryptfs_upper  # Katalog powinien być pusty lub niedostępny

# Dane zaszyfrowane nadal widoczne w lower (ale nieczytelne)
ls -la /mnt/ecryptfs_lower/
```

### 3.5 Ponowne montowanie istniejącego systemu eCryptfs

```bash
# Po odmontowaniu eCryptfs zaszyfrowane dane pozostają w /mnt/ecryptfs_lower.
# Aby ponownie uzyskać dostęp do plików, należy ponownie zamontować system.
# WAŻNE: należy użyć dokładnie tych samych parametrów kryptograficznych co przy
# pierwszym montowaniu (ten sam szyfr, rozmiar klucza, opcje).

# --- Metoda 1: Interaktywna (zalecana przy ręcznym użytkowaniu) ---
# System zapyta o hasło – podaj to samo hasło co przy pierwszym montowaniu.
# Przy pytaniu o parametry kryptograficzne zatwierdź te same wartości ('yes').
sudo mount -t ecryptfs \
    /mnt/ecryptfs_lower \
    /mnt/ecryptfs_upper \
    -o ecryptfs_cipher=aes,\
ecryptfs_key_bytes=32,\
ecryptfs_passthrough=n,\
ecryptfs_enable_filename_crypto=y

# --- Metoda 2: Nieinteraktywna (skrypty, automatyzacja) ---
# Krok 1: Dodaj hasło do keyringu jądra.
# --fnek powoduje dodanie zarówno klucza danych, jak i klucza szyfrowania nazw plików.
# Polecenie wypisuje dwa podpisy (sig): pierwszy to klucz danych, drugi to fnek_sig.
ecryptfs-add-passphrase --fnek
# Przykładowe wyjście:
#   Inserted auth tok with sig [aabbccdd11223344] into the user session keyring
#   Inserted auth tok with sig [eeff55667788aabb] into the user session keyring

# Krok 2: Montowanie z podpisami kluczy – bez interaktywnego pytania o hasło.
# Zastąp wartości ecryptfs_sig i ecryptfs_fnek_sig podpisami z poprzedniego kroku.
sudo mount -t ecryptfs \
    /mnt/ecryptfs_lower \
    /mnt/ecryptfs_upper \
    -o ecryptfs_cipher=aes,\
ecryptfs_key_bytes=32,\
ecryptfs_passthrough=n,\
ecryptfs_enable_filename_crypto=y,\
ecryptfs_sig=aabbccdd11223344,\
ecryptfs_fnek_sig=eeff55667788aabb

# Weryfikacja – pliki powinny być czytelne po ponownym zamontowaniu
mount | grep ecryptfs
ls /mnt/ecryptfs_upper/
```

### 3.6 Użycie ecryptfs-setup-private (integracja z katalogiem domowym)

```bash
# Wygodniejsza metoda – konfiguracja szyfrowanego katalogu prywatnego dla użytkownika.
# Ta metoda integruje się z PAM – katalog jest automatycznie montowany przy logowaniu.
# ecryptfs-setup-private tworzy ~/.Private (lower) i ~/Private (upper/mountpoint)
ecryptfs-setup-private
# Postępuj zgodnie z instrukcjami – podaj hasło logowania i hasło szyfrowania

# Ręczne montowanie katalogu prywatnego (zwykle robione automatycznie przez PAM)
ecryptfs-mount-private

# Sprawdzenie stanu
mount | grep ecryptfs

# Odmontowanie
ecryptfs-umount-private
```

---

## 4. fscrypt – szyfrowanie katalogów w ext4/f2fs

### Teoria

**fscrypt** to interfejs jądra Linux do szyfrowania na poziomie systemu plików, wspierany natywnie przez ext4 (od jądra 4.1), f2fs i ubifs. Klucze zarządzane są w keyring jądra. Narzędzie `fscrypt` to CLI do zarządzania politykami szyfrowania.

```
┌──────────────────────────────────────────────┐
│              Aplikacja                        │
├──────────────────────────────────────────────┤
│         VFS (Virtual File System)             │
├──────────────────────────────────────────────┤
│   fscrypt (szyfrowanie w jądrze, per-katalog) │  ← klucze w kernel keyring
├──────────────────────────────────────────────┤
│         ext4 / f2fs / ubifs                  │
└──────────────────────────────────────────────┘
```

### 4.1 Przygotowanie systemu plików ext4 z obsługą szyfrowania

```bash
# Tworzenie nowego obrazu dysku dla testów fscrypt (osobny od LUKS)
dd if=/dev/zero of=/tmp/fscrypt_test.img bs=1M count=1024 status=progress

# Mapowanie na loop device
FSCRYPT_LOOP=$(sudo losetup --find --show /tmp/fscrypt_test.img)
echo "fscrypt loop device: $FSCRYPT_LOOP"

# Tworzenie systemu plików ext4 z włączoną obsługą szyfrowania.
# Flaga -O encrypt aktywuje feature szyfrowania w superbloku ext4.
# Bez tej flagi fscrypt nie będzie działał na tym systemie plików.
sudo mkfs.ext4 -O encrypt -L "fscrypt_vol" "$FSCRYPT_LOOP"

# Weryfikacja – sprawdzamy czy feature encrypt jest włączony
sudo tune2fs -l "$FSCRYPT_LOOP" | grep -i encrypt

# Montowanie systemu plików
sudo mkdir -p /mnt/fscrypt_test
sudo mount "$FSCRYPT_LOOP" /mnt/fscrypt_test

# Ustawienie własności dla bieżącego użytkownika
sudo chown $USER:$USER /mnt/fscrypt_test
```

### 4.2 Inicjalizacja fscrypt

```bash
# Inicjalizacja fscrypt na poziomie systemu – tworzy plik konfiguracyjny /etc/fscrypt.conf
# Zawiera parametry algorytmów i PBKDF używane domyślnie.
sudo fscrypt setup

# Wyświetlenie konfiguracji fscrypt
cat /etc/fscrypt.conf

# Inicjalizacja fscrypt na konkretnym punkcie montowania.
# Tworzy katalog .fscrypt w korzeniu systemu plików do przechowywania metadanych polityk.
fscrypt setup /mnt/fscrypt_test

# Weryfikacja – powinien istnieć katalog .fscrypt
ls -la /mnt/fscrypt_test/.fscrypt/
```

### 4.3 Tworzenie zaszyfrowanego katalogu

```bash
# Tworzenie katalogu, który będzie szyfrowany
mkdir -p /mnt/fscrypt_test/private_data

# Szyfrowanie katalogu za pomocą fscrypt.
# Polecenie poprosi o wybranie lub stworzenie "protektora" (metody ochrony klucza):
#   - passphrase   – klucz chroniony hasłem użytkownika
#   - pam_passphrase – zintegrowane z hasłem logowania PAM
#   - raw_key      – klucz w postaci surowych bajtów (dla automatyzacji)
# Wybierz opcję 1 (passphrase) i podaj hasło.
fscrypt encrypt /mnt/fscrypt_test/private_data --source=custom_passphrase

# Sprawdzenie statusu szyfrowania katalogu
fscrypt status /mnt/fscrypt_test/private_data

# Wyświetlenie wszystkich zaszyfrowanych katalogów na wolumenie
fscrypt status /mnt/fscrypt_test
```

### 4.4 Testowanie szyfrowania

```bash
# Tworzenie pliku testowego w zaszyfrowanym katalogu
echo "Dane chronione przez fscrypt" > /mnt/fscrypt_test/private_data/tajne.txt
cat /mnt/fscrypt_test/private_data/tajne.txt   # Czytelne – klucz jest załadowany

# Test zapisu dla benchmarku
time dd if=/dev/urandom of=/mnt/fscrypt_test/private_data/testfile.bin \
    bs=1M count=100 status=progress

# Test odczytu
sync && echo 3 | sudo tee /proc/sys/vm/drop_caches
time dd if=/mnt/fscrypt_test/private_data/testfile.bin of=/dev/null bs=1M status=progress

# Zablokowanie katalogu – usunięcie klucza z keyring jądra
# Po tej operacji pliki są nieczytelne (nazwy i zawartość zaszyfrowane)
fscrypt lock /mnt/fscrypt_test/private_data

# Weryfikacja – pliki powinny mieć zaszyfrowane nazwy
ls /mnt/fscrypt_test/private_data/

# Odblokowanie – ponowne załadowanie klucza do keyring
fscrypt unlock /mnt/fscrypt_test/private_data
# Podaj hasło – pliki stają się ponownie czytelne

ls /mnt/fscrypt_test/private_data/
```

---

## 5. Benchmarki I/O

### 5.1 Skrypt benchmarku `dd` (prosty test przepustowości)

Zapisz jako `benchmark_dd.sh`:

```bash
#!/usr/bin/env bash
# =============================================================================
# benchmark_dd.sh – prosty benchmark I/O przy użyciu dd
# Mierzy przepustowość zapisu i odczytu sekwencyjnego dla trzech mechanizmów
# szyfrowania oraz bazowego (niezaszyfrowanego) systemu plików.
# Uruchomienie: sudo bash benchmark_dd.sh
# =============================================================================

set -euo pipefail

# --- Parametry konfiguracyjne ---
BLOCK_SIZE="1M"          # Rozmiar bloku I/O
FILE_SIZE_MB=512         # Rozmiar pliku testowego w MB
RESULTS_FILE="wyniki_dd_$(date +%Y%m%d_%H%M%S).csv"
TEST_FILE="benchmark_test.bin"

# Punkty montowania (ustaw zgodnie z wcześniej skonfigurowanymi wolumenami)
MOUNT_LUKS="/mnt/luks_test"
MOUNT_ECRYPTFS="/mnt/ecryptfs_upper"
MOUNT_FSCRYPT="/mnt/fscrypt_test/private_data"
MOUNT_BASELINE="/tmp/baseline_test"

# --- Kolory dla czytelności wyjścia ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# --- Funkcja pomocnicza: wyczyść cache przed testem ---
drop_caches() {
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
    echo -e "${YELLOW}[INFO] Cache wyczyszczony${NC}"
}

# --- Funkcja: test zapisu dd ---
# Argument $1: ścieżka do katalogu testowego
# Argument $2: etykieta mechanizmu (dla raportu)
test_write_dd() {
    local path="$1"
    local label="$2"
    local target="${path}/${TEST_FILE}"

    echo -e "${GREEN}[WRITE] Test zapisu: ${label}${NC}"

    # Wykonanie 3 pomiarów i obliczenie średniej
    local total_speed=0
    for i in 1 2 3; do
        drop_caches
        # dd zwraca prędkość na stderr – przechwytujemy do zmiennej
        local result
        result=$(dd if=/dev/urandom of="$target" bs="$BLOCK_SIZE" \
                    count="$FILE_SIZE_MB" 2>&1 | tail -1)
        local speed
        speed=$(echo "$result" | grep -oP '[\d.]+ [MGk]B/s' | tail -1)
        echo "  Próba $i: $speed"
        total_speed=$(echo "$total_speed + $(echo "$speed" | grep -oP '[\d.]+')" | bc)
    done

    local avg_speed
    avg_speed=$(echo "scale=2; $total_speed / 3" | bc)
    echo -e "${GREEN}  Średnia: ${avg_speed} MB/s${NC}"
    echo "${label},WRITE,${avg_speed}" >> "$RESULTS_FILE"
    rm -f "$target"
}

# --- Funkcja: test odczytu dd ---
test_read_dd() {
    local path="$1"
    local label="$2"
    local target="${path}/${TEST_FILE}"

    echo -e "${GREEN}[READ] Test odczytu: ${label}${NC}"

    # Najpierw zapisujemy plik testowy
    dd if=/dev/urandom of="$target" bs="$BLOCK_SIZE" count="$FILE_SIZE_MB" 2>/dev/null

    local total_speed=0
    for i in 1 2 3; do
        drop_caches
        local result
        result=$(dd if="$target" of=/dev/null bs="$BLOCK_SIZE" 2>&1 | tail -1)
        local speed
        speed=$(echo "$result" | grep -oP '[\d.]+ [MGk]B/s' | tail -1)
        echo "  Próba $i: $speed"
        total_speed=$(echo "$total_speed + $(echo "$speed" | grep -oP '[\d.]+')" | bc)
    done

    local avg_speed
    avg_speed=$(echo "scale=2; $total_speed / 3" | bc)
    echo -e "${GREEN}  Średnia: ${avg_speed} MB/s${NC}"
    echo "${label},READ,${avg_speed}" >> "$RESULTS_FILE"
    rm -f "$target"
}

# --- Przygotowanie pliku wynikowego CSV ---
echo "Mechanizm,Operacja,Prędkość_MBs" > "$RESULTS_FILE"

# --- Konfiguracja bazowego systemu plików (bez szyfrowania) ---
echo -e "\n${YELLOW}=== Przygotowanie systemu bazowego (bez szyfrowania) ===${NC}"
mkdir -p "$MOUNT_BASELINE"

# --- Uruchomienie testów ---
echo -e "\n${YELLOW}=== 1. Bazowy system plików (bez szyfrowania) ===${NC}"
test_write_dd "$MOUNT_BASELINE" "Baseline"
test_read_dd  "$MOUNT_BASELINE" "Baseline"

echo -e "\n${YELLOW}=== 2. LUKS ===${NC}"
# Upewnij się, że LUKS jest zamontowany przed uruchomieniem skryptu
if mountpoint -q "$MOUNT_LUKS"; then
    test_write_dd "$MOUNT_LUKS" "LUKS"
    test_read_dd  "$MOUNT_LUKS" "LUKS"
else
    echo -e "${RED}[BŁĄD] $MOUNT_LUKS nie jest zamontowany. Pomiń lub zamontuj ręcznie.${NC}"
fi

echo -e "\n${YELLOW}=== 3. eCryptfs ===${NC}"
if mountpoint -q "$MOUNT_ECRYPTFS"; then
    test_write_dd "$MOUNT_ECRYPTFS" "eCryptfs"
    test_read_dd  "$MOUNT_ECRYPTFS" "eCryptfs"
else
    echo -e "${RED}[BŁĄD] $MOUNT_ECRYPTFS nie jest zamontowany.${NC}"
fi

echo -e "\n${YELLOW}=== 4. fscrypt ===${NC}"
if [ -d "$MOUNT_FSCRYPT" ]; then
    test_write_dd "$MOUNT_FSCRYPT" "fscrypt"
    test_read_dd  "$MOUNT_FSCRYPT" "fscrypt"
else
    echo -e "${RED}[BŁĄD] $MOUNT_FSCRYPT nie istnieje.${NC}"
fi

# --- Wyświetlenie wyników ---
echo -e "\n${YELLOW}=== WYNIKI KOŃCOWE ===${NC}"
echo "Wyniki zapisane w: $RESULTS_FILE"
column -t -s',' "$RESULTS_FILE"

# --- Sprzątanie ---
rm -rf "$MOUNT_BASELINE"
```

### 5.2 Skrypt benchmarku `fio` (zaawansowany test I/O)

Zapisz jako `benchmark_fio.sh`:

```bash
#!/usr/bin/env bash
# =============================================================================
# benchmark_fio.sh – zaawansowany benchmark I/O przy użyciu fio
# fio (Flexible I/O Tester) umożliwia precyzyjne testowanie różnych wzorców
# dostępu: sekwencyjny, losowy, mieszany, z różnymi głębokościami kolejki.
# Uruchomienie: sudo bash benchmark_fio.sh
# =============================================================================

set -euo pipefail

# --- Parametry ---
RUNTIME=30          # Czas każdego testu w sekundach
FILE_SIZE="512M"    # Rozmiar pliku testowego
BLOCK_SIZE="4k"     # Rozmiar bloku dla testów losowych (typowy dla DB)
BS_SEQ="1M"         # Rozmiar bloku dla testów sekwencyjnych
IODEPTH=16          # Głębokość kolejki I/O (symulacja równoległych żądań)
NUMJOBS=4           # Liczba równoległych wątków
OUTPUT_DIR="fio_results_$(date +%Y%m%d_%H%M%S)"
SUMMARY_CSV="${OUTPUT_DIR}/summary.csv"

# Punkty testowe
declare -A MOUNT_POINTS=(
    ["Baseline"]="/tmp/fio_baseline"
    ["LUKS"]="/mnt/luks_test"
    ["eCryptfs"]="/mnt/ecryptfs_upper"
    ["fscrypt"]="/mnt/fscrypt_test/private_data"
)

# --- Przygotowanie ---
mkdir -p "$OUTPUT_DIR"
mkdir -p /tmp/fio_baseline
echo "Mechanizm,Test,Read_IOPS,Write_IOPS,Read_BW_MBs,Write_BW_MBs,Read_Lat_us,Write_Lat_us" \
    > "$SUMMARY_CSV"

# --- Funkcja uruchamiająca test fio i parsująca wyniki ---
run_fio_test() {
    local label="$1"
    local path="$2"
    local test_name="$3"
    local rw="$4"         # Typ I/O: read, write, randread, randwrite, randrw, rw
    local bs="$5"         # Rozmiar bloku
    local output="${OUTPUT_DIR}/${label}_${test_name}.json"

    echo "[fio] ${label} – ${test_name} (rw=${rw}, bs=${bs})..."

    # Uruchomienie fio z wyjściem JSON dla łatwego parsowania
    # --name         – nazwa testu (widoczna w raporcie)
    # --filename     – ścieżka do pliku testowego
    # --rw           – typ operacji I/O
    # --bs           – rozmiar bloku
    # --size         – rozmiar pliku testowego
    # --runtime      – czas trwania testu
    # --time_based   – trzymaj czas, nie rozmiar pliku
    # --iodepth      – głębokość kolejki asynchronicznej
    # --numjobs      – liczba równoległych zadań
    # --ioengine     – silnik I/O: libaio = asynchroniczne I/O Linuxa
    # --direct=1     – pomiń page cache (O_DIRECT) – test realnych danych dyskowych
    # --group_reporting – agreguj wyniki wszystkich wątków
    fio \
        --name="${label}_${test_name}" \
        --filename="${path}/fio_testfile" \
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
        --output-format=json \
        --output="$output" \
        2>/dev/null

    # Parsowanie kluczowych metryk z JSON (za pomocą python3)
    python3 - <<EOF
import json, sys

with open("$output") as f:
    data = json.load(f)

job = data['jobs'][0]
r = job['read']
w = job['write']

read_iops    = r.get('iops', 0)
write_iops   = w.get('iops', 0)
read_bw      = r.get('bw', 0) / 1024   # KB/s -> MB/s
write_bw     = w.get('bw', 0) / 1024
read_lat     = r.get('lat_ns', {}).get('mean', 0) / 1000  # ns -> us
write_lat    = w.get('lat_ns', {}).get('mean', 0) / 1000

print(f"  Odczyt:  {read_iops:.0f} IOPS | {read_bw:.1f} MB/s | lat: {read_lat:.1f} µs")
print(f"  Zapis:   {write_iops:.0f} IOPS | {write_bw:.1f} MB/s | lat: {write_lat:.1f} µs")

# Dopisz do CSV
with open("$SUMMARY_CSV", 'a') as csv:
    csv.write(f"$label,$test_name,{read_iops:.0f},{write_iops:.0f},"
              f"{read_bw:.2f},{write_bw:.2f},{read_lat:.1f},{write_lat:.1f}\n")
EOF

    # Usunięcie pliku testowego między testami
    rm -f "${path}/fio_testfile"
}

# --- Pętla główna: testy dla każdego mechanizmu ---
for label in "Baseline" "LUKS" "eCryptfs" "fscrypt"; do
    path="${MOUNT_POINTS[$label]}"

    # Sprawdzenie dostępności punktu montowania
    if [[ "$label" == "Baseline" ]]; then
        echo -e "\n>>> Testowanie: $label ($path)"
    elif mountpoint -q "$path" 2>/dev/null || [ -d "$path" ]; then
        echo -e "\n>>> Testowanie: $label ($path)"
    else
        echo "[POMINIĘTO] $label – $path niedostępny"
        continue
    fi

    # 1. Zapis sekwencyjny – dobry wskaźnik dla kopii zapasowych, dużych plików
    run_fio_test "$label" "$path" "seq_write"   "write"     "$BS_SEQ"

    # 2. Odczyt sekwencyjny – streaming, multimedia
    run_fio_test "$label" "$path" "seq_read"    "read"      "$BS_SEQ"

    # 3. Losowy zapis 4K – typowe obciążenie baz danych
    run_fio_test "$label" "$path" "rand_write"  "randwrite" "$BLOCK_SIZE"

    # 4. Losowy odczyt 4K – dostęp do małych plików, metadanych
    run_fio_test "$label" "$path" "rand_read"   "randread"  "$BLOCK_SIZE"

    # 5. Mieszany odczyt/zapis (70/30) – symulacja rzeczywistego obciążenia
    run_fio_test "$label" "$path" "mixed_rw"    "randrw"    "$BLOCK_SIZE"
done

# --- Wyświetlenie tabeli podsumowującej ---
echo -e "\n===== PODSUMOWANIE WYNIKÓW FIO ====="
column -t -s',' "$SUMMARY_CSV"
echo -e "\nPełne wyniki JSON: ${OUTPUT_DIR}/"

# Sprzątanie
rm -rf /tmp/fio_baseline
```

### 5.3 Uruchomienie benchmarków

```bash
# Nadanie uprawnień wykonania
chmod +x benchmark_dd.sh benchmark_fio.sh

# Uruchomienie benchmarku dd (wymaga sudo dla czyszczenia cache)
sudo bash benchmark_dd.sh

# Uruchomienie benchmarku fio (wymaga sudo dla direct I/O i czyszczenia cache)
sudo bash benchmark_fio.sh
```

---

## 6. Analiza modelu ochrony

### 6.1 Porównanie warstw ochrony

| Cecha                             | LUKS (dm-crypt)                   | eCryptfs                           | fscrypt                         |
| --------------------------------- | --------------------------------- | ---------------------------------- | ------------------------------- |
| **Warstwa szyfrowania**           | Blokowa (device)                  | System plików (VFS stack)          | System plików (jądro)           |
| **Granularność**                  | Cały wolumin/partycja             | Per-plik                           | Per-katalog                     |
| **Szyfrowanie nazw plików**       | Tak (transparent)                 | Opcjonalne (`fnek`)                | Tak (polityka)                  |
| **Szyfrowanie metadanych FS**     | Tak (inode, timestamps)           | Częściowe (rozmiar pliku widoczny) | Tak (nazwy tak, timestamps nie) |
| **Ochrona podczas pracy systemu** | Brak (zamontowany = odszyfrowany) | Brak po montażu                    | Blokada per-katalog (lock)      |
| **Hibernacja (suspend-to-disk)**  | Wymaga szyfrowanego swapu         | Wymaga szyfrowanego swapu          | Wymaga szyfrowanego swapu       |
| **Atak cold-boot**                | Podatny (klucz w RAM)             | Podatny (klucz w RAM)              | Podatny (klucz w keyring)       |
| **Wiele kluczy/użytkowników**     | Do 32 slotów (LUKS2)              | Przez keyring                      | Polityki + protektory           |
| **TPM / Secure Boot**             | Tak (clevis/tang)                 | Ograniczone                        | Ograniczone                     |
| **Pliki swap/tymczasowe**         | Chronione jeśli na LUKS           | Niezaszyfrowane (!)                | Niezaszyfrowane (!)             |

### 6.2 Model zagrożeń (Threat Model)

#### LUKS

```
✅ CHRONI PRZED:
  - Kradzieżą/zgubieniem dysku (offline attack)
  - Dostępem fizycznym do wyłączonego urządzenia
  - Kopiowaniem obrazu dysku (disk imaging)
  - Analizą sektorów dysku bez klucza

❌ NIE CHRONI PRZED:
  - Atakiem na działający system (zalogowany użytkownik)
  - Złośliwym oprogramowaniem (malware) z uprawnieniami root
  - Atakiem cold-boot (RAM dump po wyłączeniu zasilania < ~1-2 min)
  - Evil Maid Attack (jeśli brak Secure Boot / TPM)
```

#### eCryptfs

```
✅ CHRONI PRZED:
  - Nieautoryzowanym dostępem do plików w zaszyfrowanym katalogu
  - Bezpośrednim odczytem z dysku (ale nie przez zamontowany FS)
  - Różni użytkownicy – różne klucze dla różnych katalogów

❌ NIE CHRONI PRZED:
  - Procesem działającym jako ten sam użytkownik (klucz załadowany)
  - Ujawnieniem rozmiaru pliku (rozmiar widoczny przez inode)
  - Wyciekiem danych przez swap/tmp (poza eCryptfs)
  - Śladami metadanych (timestamps, inode numbers)
```

#### fscrypt

```
✅ CHRONI PRZED:
  - Offline access po wykonaniu `fscrypt lock`
  - Różni użytkownicy systemu (separacja kluczy przez keyring)
  - Selektywna blokada katalogów bez odmontowania FS

❌ NIE CHRONI PRZED:
  - Dostępem root (administrator może zawsze odczytać dane)
  - Wyciekiem przez swap (bez szyfrowanego swap)
  - Atakiem na zalogowaną sesję użytkownika
  - Częściowymi metadanymi (timestamps inode NIE są szyfrowane)
```

### 6.3 Analiza wydajności – oczekiwane wyniki

Szacowane spadki wydajności względem niezaszyfrowanego systemu plików (na CPU z AES-NI):

| Mechanizm | Zapis seq. | Odczyt seq. | IOPS 4K losowy | Opóźnienie    |
| --------- | ---------- | ----------- | -------------- | ------------- |
| LUKS      | ~2–5%      | ~2–5%       | ~3–8%          | bardzo niskie |
| fscrypt   | ~3–8%      | ~2–6%       | ~4–10%         | niskie        |
| eCryptfs  | ~15–35%    | ~10–25%     | ~20–50%        | wysokie       |

> **Uwaga:** Wyniki zależą od sprzętu. Na procesorach z akceleracją AES-NI (Intel Sandy Bridge+, AMD Ryzen) narzut LUKS i fscrypt jest minimalny.

```bash
# Sprawdzenie czy CPU obsługuje AES-NI (sprzętowe przyspieszenie AES)
grep -m1 'aes' /proc/cpuinfo && echo "AES-NI: WSPIERANE" || echo "AES-NI: BRAK"

# Benchmark samego algorytmu AES w jądrze
sudo cryptsetup benchmark --cipher aes-xts --key-size 512
```

---

## 7. Rekomendacje dla scenariuszy użycia

### 7.1 Laptop prywatny / stacja robocza

**Zalecenie: LUKS na partycji systemowej + szyfrowany swap**

```bash
# Dlaczego LUKS?
# - Pełna ochrona dysku przy wyłączonym urządzeniu
# - Minimalne obciążenie wydajnościowe (AES-NI)
# - Integracja z systemem (GRUB, initramfs, PAM)
# - Możliwość podpięcia TPM dla automatycznego odblokowania

# Konfiguracja szyfrowanego swapu (konieczna przy LUKS na systemie)
# Bez tego klucze szyfrowania mogą trafić do swapu podczas hibernacji!
sudo swapoff -a
sudo cryptsetup open --type plain --cipher aes-xts-plain64 \
    --key-file /dev/urandom /dev/sdX2 swap_crypt
sudo mkswap /dev/mapper/swap_crypt
sudo swapon /dev/mapper/swap_crypt

# Weryfikacja
swapon --show
```

**Integracja LUKS z TPM2 (automatyczne odblokowywanie przy starcie):**

```bash
# Instalacja clevis (narzędzie do automatycznego odblokowywania LUKS z TPM/sieci)
sudo apt install clevis clevis-luks clevis-tpm2 clevis-initramfs

# Powiązanie LUKS z TPM2 (PCR 7 = Secure Boot state)
sudo clevis luks bind -d /dev/sdX tpm2 '{"pcr_ids":"7"}'

# Aktualizacja initramfs
sudo update-initramfs -u -k all
```

### 7.2 Serwer z danymi wrażliwymi / baza danych

**Zalecenie: LUKS dla partycji danych + HSM lub klucz sieciowy (Tang/Clevis)**

```bash
# Dlaczego LUKS dla serwera?
# - Ochrona danych na dysku przy fizycznym dostępie do serwera
# - Zarządzanie kluczami przez infrastrukturę (Tang server)
# - Automatyczne odblokowywanie przez sieć bez manualnego hasła

# Konfiguracja automatycznego odblokowywania przez serwer Tang
sudo apt install clevis clevis-luks

# Powiązanie z serwerem Tang (adres serwera kluczy w sieci wewnętrznej)
sudo clevis luks bind -d /dev/sdb tang '{"url":"http://tang.internal.company.com"}'

# Test automatycznego odblokowywania
sudo clevis luks unlock -d /dev/sdb
```

### 7.3 Środowisko wieloużytkownikowe / chmura publiczna

**Zalecenie: fscrypt z politykami per-użytkownik + PAM integration**

```bash
# Dlaczego fscrypt?
# - Każdy użytkownik ma własne klucze (separacja w kernel keyring)
# - Automatyczne odblokowanie/blokowanie przy logowaniu/wylogowaniu przez PAM
# - Brak narzutu administracyjnego (vs LUKS per-user)

# Konfiguracja PAM dla automatycznego zarządzania kluczami fscrypt
# Edycja /etc/pam.d/common-auth – dodaj na końcu:
echo "auth optional pam_fscrypt.so" | sudo tee -a /etc/pam.d/common-auth

# Edycja /etc/pam.d/common-session – dodaj:
echo "session optional pam_fscrypt.so" | sudo tee -a /etc/pam.d/common-session

# Tworzenie katalogu domowego z szyfrowaniem per-użytkownik
sudo useradd -m -s /bin/bash nowy_uzytkownik
sudo fscrypt encrypt /home/nowy_uzytkownik \
    --source=pam_passphrase \
    --user=nowy_uzytkownik
```

### 7.4 Urządzenia mobilne / IoT

**Zalecenie: fscrypt (natywna integracja z Android) lub LUKS dla dysków zewnętrznych**

```bash
# fscrypt jest używany natywnie przez Android (FBE – File-Based Encryption)
# Na systemach Linux IoT (Raspberry Pi, embedded):

# Sprawdzenie obsługi fscrypt przez jądro
grep -r CONFIG_FS_ENCRYPTION /boot/config-$(uname -r) 2>/dev/null \
    || zcat /proc/config.gz | grep CONFIG_FS_ENCRYPTION

# Dla dysków zewnętrznych USB – LUKS jest naturalnym wyborem
# (pełna ochrona przy zgubieniu/kradzieży nośnika)
```

### 7.5 Kopia zapasowa / archiwizacja

**Zalecenie: LUKS lub szyfrowanie na poziomie archiwum (GPG)**

```bash
# LUKS dla zaszyfrowanego kontenera backupu na dysku zewnętrznym
BACKUP_DEV="/dev/sdc"   # Dysk przeznaczony na backup

sudo cryptsetup luksFormat --type luks2 "$BACKUP_DEV"
sudo cryptsetup open "$BACKUP_DEV" backup_volume
sudo mkfs.ext4 /dev/mapper/backup_volume
sudo mount /dev/mapper/backup_volume /mnt/backup

# Przykład rsync backup do zaszyfrowanego kontenera
sudo rsync -avz --delete /home/user/ /mnt/backup/user_home/

sudo umount /mnt/backup
sudo cryptsetup close backup_volume

# Alternatywnie: szyfrowanie archiwum tar przez GPG
# Tworzy zaszyfrowane archiwum bez konieczności oddzielnego kontenera
tar czf - /home/user/ | gpg --symmetric --cipher-algo AES256 \
    --output /mnt/external/backup_$(date +%Y%m%d).tar.gz.gpg
```

### 7.6 Tabela decyzyjna

| Scenariusz                      | Rekomendacja    | Powód                                            |
| ------------------------------- | --------------- | ------------------------------------------------ |
| Szyfrowanie całego dysku        | **LUKS**        | Pełna ochrona offline, minimalny narzut          |
| Katalog domowy użytkownika      | **fscrypt**     | Integracja PAM, dobra wydajność                  |
| Środowisko wieloużytkownikowe   | **fscrypt**     | Separacja kluczy per-użytkownik                  |
| Szyfrowanie dysków zewnętrznych | **LUKS**        | Prostota, przenośność                            |
| Selektywne szyfrowanie plików   | **eCryptfs**    | Per-plik, stary ale sprawdzony                   |
| Serwer w chmurze                | **LUKS + Tang** | Automatyczne odblokowywanie bez hasła manualnego |
| Android / embedded Linux        | **fscrypt**     | Natywna obsługa jądra, FBE                       |
| Backup na taśmie/chmurze        | **GPG + LUKS**  | Przenośność, niezależność od infrastruktury      |

---

## 8. Podsumowanie

### 8.1 Zestawienie porównawcze

| Kryterium                  | LUKS       | eCryptfs      | fscrypt    |
| -------------------------- | ---------- | ------------- | ---------- |
| **Wydajność**              | ⭐⭐⭐⭐⭐ | ⭐⭐⭐        | ⭐⭐⭐⭐   |
| **Bezpieczeństwo offline** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐      | ⭐⭐⭐⭐   |
| **Granularność kontroli**  | ⭐⭐       | ⭐⭐⭐⭐      | ⭐⭐⭐⭐⭐ |
| **Prostota konfiguracji**  | ⭐⭐⭐⭐   | ⭐⭐⭐        | ⭐⭐⭐     |
| **Integracja z systemem**  | ⭐⭐⭐⭐⭐ | ⭐⭐⭐        | ⭐⭐⭐⭐⭐ |
| **Wsparcie aktywne**       | ⭐⭐⭐⭐⭐ | ⭐⭐ (legacy) | ⭐⭐⭐⭐⭐ |
| **Szyfrowanie metadanych** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐        | ⭐⭐⭐⭐   |

### 8.2 Wnioski końcowe

- **LUKS** jest niezastąpiony dla szyfrowania całych partycji i dysków zewnętrznych. Jest standardem de facto w dystrybucjach Linux, posiada aktywny rozwój (LUKS2 + Argon2id + integracja z TPM). **Najwyższa rekomendacja dla ochrony danych w spoczynku.**

- **fscrypt** to nowoczesne rozwiązanie dla środowisk wieloużytkownikowych i integracji z PAM. Wspierany przez kernel mainline, aktywnie rozwijany (Android, ChromeOS). **Najlepsza opcja dla szyfrowania per-użytkownik na serwerach i stacjach roboczych.**

- **eCryptfs** – choć funkcjonalny, jest technologią w trybie utrzymania (maintenance mode). Nowsze projekty powinny preferować fscrypt. Nadal użyteczny w scenariuszach legacy i na starszych jądrach bez fscrypt.

### 8.3 Weryfikacja końcowa środowiska testowego

```bash
# Podsumowanie wszystkich zamontowanych wolumenów szyfrowanych
lsblk -o NAME,TYPE,SIZE,MOUNTPOINT,FSTYPE | grep -E "crypt|loop"
mount | grep -E "luks|ecryptfs|fscrypt"

# Status dm-crypt
sudo dmsetup status

# Sprawdzenie załadowanych modułów kryptograficznych
lsmod | grep -E "dm_crypt|ecryptfs|fscrypt"

# Weryfikacja aktywnych kluczy w keyring jądra (dla fscrypt)
keyctl show @u 2>/dev/null || echo "Brak kluczy użytkownika w keyring"

# Pełne sprzątanie środowiska testowego
sudo umount /mnt/luks_test 2>/dev/null || true
sudo umount /mnt/ecryptfs_upper 2>/dev/null || true
sudo umount /mnt/fscrypt_test 2>/dev/null || true
sudo cryptsetup close luks_test 2>/dev/null || true
sudo losetup -D   # Usuń wszystkie loop devices
rm -f /tmp/disk_test.img /tmp/fscrypt_test.img
```

---

### Materiały referencyjne

- [cryptsetup documentation – GitLab](https://gitlab.com/cryptsetup/cryptsetup)
- [fscrypt – github.com/google/fscrypt](https://github.com/google/fscrypt)
- [Kernel docs: fscrypt](https://www.kernel.org/doc/html/latest/filesystems/fscrypt.html)
- [eCryptfs – SourceForge](https://ecryptfs.sourceforge.net/)
- [LUKS On-Disk Format Specification v2.1](https://gitlab.com/cryptsetup/cryptsetup/-/wikis/LUKS-standard)
- [fio documentation](https://fio.readthedocs.io/en/latest/)
