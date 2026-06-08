# Szyfrowanie danych w systemie Linux – analiza porównawcza LUKS, eCryptfs i fscrypt

> **Środowisko testowe:** Ubuntu 22.04 LTS (maszyna wirtualna, 4 vCPU, 4 GB RAM)
> **Jądro:** Linux 5.15 LTS
> **Cel:** Wdrożenie i porównanie mechanizmów szyfrowania LUKS, eCryptfs oraz fscrypt pod kątem modelu ochrony, wydajności I/O i przydatności w różnych scenariuszach użycia.

---

## Spis treści

1. [Wprowadzenie](#1-wprowadzenie)
2. [Opis mechanizmów szyfrowania](#2-opis-mechanizmów-szyfrowania)
   - 2.1 [LUKS / dm-crypt](#21-luks--dm-crypt)
   - 2.2 [eCryptfs](#22-ecryptfs)
   - 2.3 [fscrypt](#23-fscrypt)
3. [Wdrożenie środowiska testowego](#3-wdrożenie-środowiska-testowego)
4. [Benchmarki I/O](#4-benchmarki-io)
   - 4.1 [Metodologia](#41-metodologia)
   - 4.2 [Wyniki – przepustowość dd](#42-wyniki--przepustowość-dd)
   - 4.3 [Analiza wyników](#43-analiza-wyników)
5. [Analiza modelu ochrony](#5-analiza-modelu-ochrony)
   - 5.1 [Porównanie warstw ochrony](#51-porównanie-warstw-ochrony)
   - 5.2 [Model zagrożeń](#52-model-zagrożeń)
   - 5.3 [Ochrona metadanych](#53-ochrona-metadanych)
6. [Rekomendacje dla scenariuszy użycia](#6-rekomendacje-dla-scenariuszy-użycia)
7. [Podsumowanie i wnioski](#7-podsumowanie-i-wnioski)
8. [Materiały referencyjne](#8-materiały-referencyjne)

---

## 1. Wprowadzenie

Szyfrowanie danych w systemie Linux realizowane jest na kilku poziomach abstrakcji. Wybór mechanizmu ma bezpośrednie konsekwencje zarówno dla poziomu bezpieczeństwa, jak i wydajności systemu. Niniejsza analiza obejmuje trzy najpowszechniejsze rozwiązania dostępne w mainline kernelu Linux:

- **LUKS** (Linux Unified Key Setup) – szyfrowanie na poziomie urządzenia blokowego, zarządzane przez `dm-crypt`.
- **eCryptfs** – szyfrowanie per-plik realizowane jako stos nad istniejącym systemem plików.
- **fscrypt** – szyfrowanie per-katalog zintegrowane natywnie z jądrem Linux (ext4, f2fs, ubifs).

Dokument obejmuje wdrożenie każdego mechanizmu w środowisku testowym, pomiary wydajności I/O oraz porównawczą analizę modelu ochrony.

---

## 2. Opis mechanizmów szyfrowania

### 2.1 LUKS / dm-crypt

LUKS to standard zarządzania kluczami dla szyfrowania blokowego w Linuksie. Działa w warstwie `dm-crypt` jądra, tworząc wirtualne urządzenie blokowe, które transparentnie szyfruje i odszyfrowuje dane. System plików (ext4, xfs itp.) tworzony jest wewnątrz zaszyfrowanego kontenera.

```
┌──────────────────────────────────────────────┐
│     Aplikacja / System plików (ext4, xfs)    │
├──────────────────────────────────────────────┤
│           dm-crypt (LUKS)                    │  ← warstwa szyfrowania blokowego
├──────────────────────────────────────────────┤
│       Urządzenie blokowe (/dev/sdX)          │
└──────────────────────────────────────────────┘
```

**Algorytm:** AES-XTS-plain64 z kluczem 512-bitowym (dwa klucze 256-bit dla XTS).
**KDF:** Argon2id (LUKS2) – odporny na ataki GPU/ASIC.
**Sloty kluczy:** do 32 niezależnych kluczy (LUKS2), co umożliwia dostęp dla wielu użytkowników lub kluczy awaryjnych.

### 2.2 eCryptfs

eCryptfs działa jako warstwowy system plików (stacked filesystem) montowany nad istniejącym systemem plików (np. ext4). Każdy plik jest szyfrowany indywidualnie – metadane kryptograficzne (nagłówek ECRYPTFS_TAG_3 / ECRYPTFS_TAG_11) przechowywane są w pierwszych bajtach zaszyfrowanego pliku.

```
┌──────────────────────────────────────────────┐
│                Aplikacja                     │
├──────────────────────────────────────────────┤
│    eCryptfs VFS (per-plik, AES-CBC/256)      │  ← warstwa szyfrowania pliku
├──────────────────────────────────────────────┤
│  Dolny FS (ext4) – pliki z nagłówkami krypto │
└──────────────────────────────────────────────┘
```

**Algorytm:** AES-CBC z kluczem 256-bitowym.
**Zarządzanie kluczami:** kernel keyring (klucz sesji).
**Status:** tryb utrzymania (maintenance mode) – brak nowych funkcji od ~2018.

### 2.3 fscrypt

fscrypt to natywny interfejs jądra Linux do szyfrowania na poziomie systemu plików. Szyfrowanie realizowane jest przez sam system plików (ext4, f2fs) – nie wymaga dodatkowej warstwy. Klucze przechowywane są w kernel keyring i powiązane z politykami szyfrowania per-katalog.

```
┌──────────────────────────────────────────────┐
│                Aplikacja                     │
├──────────────────────────────────────────────┤
│           VFS (Virtual File System)          │
├──────────────────────────────────────────────┤
│  fscrypt API (per-katalog, klucze w keyring) │  ← polityki szyfrowania
├──────────────────────────────────────────────┤
│     ext4 / f2fs / ubifs (native support)    │
└──────────────────────────────────────────────┘
```

**Algorytm:** AES-256-XTS (treść pliku), AES-256-CTS-CBC (nazwy plików).
**Zarządzanie kluczami:** kernel keyring, integracja z PAM (`libpam-fscrypt`).
**Status:** aktywny rozwój – używany przez Android (FBE), ChromeOS, Ubuntu 20.04+.

---

## 3. Wdrożenie środowiska testowego

### 3.1 Konfiguracja maszyny wirtualnej

| Parametr             | Wartość            |
| -------------------- | ------------------ |
| System operacyjny    | Ubuntu 22.04 LTS   |
| Jądro                | Linux 5.15.0 (LTS) |
| RAM                  | 4 GB               |
| vCPU                 | 4                  |
| Dysk systemowy       | 20 GB (VirtIO)     |
| Dysk testowy LUKS    | `/dev/sdb` – 5 GB  |
| Dysk testowy fscrypt | `/dev/sdc` – 5 GB  |

### 3.2 Instalacja narzędzi

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    cryptsetup ecryptfs-utils fscrypt libpam-fscrypt \
    fio util-linux tree htop python3
```

### 3.3 Wdrożenie LUKS

```bash
# Inicjalizacja LUKS2 z Argon2id KDF
sudo cryptsetup luksFormat \
    --type luks2 \
    --cipher aes-xts-plain64 \
    --key-size 512 \
    --hash sha256 \
    --pbkdf argon2id \
    --iter-time 2000 \
    /dev/sdb

# Otwarcie, formatowanie i montowanie
sudo cryptsetup open /dev/sdb luks_test
sudo mkfs.ext4 -L "luks_vol" /dev/mapper/luks_test
sudo mkdir -p /mnt/luks_test
sudo mount /dev/mapper/luks_test /mnt/luks_test
```

Weryfikacja parametrów kontenera:

```bash
sudo cryptsetup luksDump /dev/sdb | grep -E "Version|Cipher|Key|PBKDF"
# Version:        2
# Cipher:         aes-xts-plain64
# Cipher key:     512 bits
# PBKDF:          argon2id
```

### 3.4 Wdrożenie eCryptfs

```bash
# Załadowanie modułu jądra
sudo modprobe ecryptfs

# Tworzenie katalogów
sudo mkdir -p /mnt/ecryptfs_encrypted /mnt/ecryptfs_upper
sudo chown $USER:$USER /mnt/ecryptfs_encrypted /mnt/ecryptfs_upper

# Montowanie z szyfrowaniem AES-256 i szyfrowaniem nazw plików
sudo mount -t ecryptfs \
    /mnt/ecryptfs_encrypted \
    /mnt/ecryptfs_upper \
    -o ecryptfs_cipher=aes,ecryptfs_key_bytes=32,\
ecryptfs_passthrough=n,ecryptfs_enable_filename_crypto=y
```

Weryfikacja – plik widziany z poziomu warstwy dolnej (zaszyfrowanej):

```bash
echo "test" > /mnt/ecryptfs_upper/tajny_plik.txt
ls /mnt/ecryptfs_encrypted/
# ECRYPTFS_FNEK_ENCRYPTED.FWbG...  ← zaszyfrowana nazwa pliku
hexdump -C /mnt/ecryptfs_encrypted/ECRYPTFS_FNEK* | head -3
# 00000000  45 43 52 59 50 54 46 53  ...  ECRYPTFS header
```

### 3.5 Wdrożenie fscrypt

```bash
# Przygotowanie systemu plików ext4 z włączoną obsługą szyfrowania
sudo mkfs.ext4 -O encrypt -L "fscrypt_vol" /dev/sdc
sudo mkdir -p /mnt/fscrypt_test
sudo mount /dev/sdc /mnt/fscrypt_test
sudo chown $USER:$USER /mnt/fscrypt_test

# Inicjalizacja fscrypt (globalna konfiguracja + metadane na wolumenie)
sudo fscrypt setup
fscrypt setup /mnt/fscrypt_test

# Tworzenie zaszyfrowanego katalogu
mkdir -p /mnt/fscrypt_test/private_data
fscrypt encrypt /mnt/fscrypt_test/private_data --source=custom_passphrase
```

Weryfikacja stanu szyfrowania:

```bash
fscrypt status /mnt/fscrypt_test/private_data
# "/mnt/fscrypt_test/private_data" is encrypted with fscrypt.
# Policy:   a3f8c2e1d4b90a7f
# Protector: custom passphrase protector "my_key"
# Unlocked: Yes

# Po fscrypt lock:
fscrypt lock /mnt/fscrypt_test/private_data
ls /mnt/fscrypt_test/private_data/
# Fqj8v3KLmXp0R...  ← zaszyfrowane nazwy plików
```

---

## 4. Benchmarki I/O

### 4.1 Metodologia

Benchmarki przeprowadzono za pomocą skryptów `benchmark_dd.py` (przepustowość sekwencyjna z użyciem `dd`) oraz `benchmark_fio.py` (zaawansowane wzorce I/O z `fio`).

**Parametry benchmarku `dd`:**

| Parametr          | Wartość                      |
| ----------------- | ---------------------------- |
| Narzędzie         | GNU `dd` + `/dev/urandom`    |
| Rozmiar pliku     | 512 MB                       |
| Rozmiary bloków   | 4K, 512K, 1M, 4M             |
| Liczba powtórzeń  | 3 (wynik = średnia arytmet.) |
| Czyszczenie cache | `sync` + `drop_caches=3`     |
| Zapis: źródło     | `/dev/urandom`               |
| Odczyt: cel       | `/dev/null`                  |

**Parametry benchmarku `fio`:**

| Parametr           | Wartość                    |
| ------------------ | -------------------------- |
| Narzędzie          | fio 3.x, silnik `libaio`   |
| Czas trwania testu | 30 s per test              |
| Rozmiar pliku      | 512 MB                     |
| Głębokość kolejki  | 16                         |
| Liczba wątków      | 4                          |
| Tryb I/O           | `auto` (direct z fallback) |

### 4.2 Wyniki – przepustowość dd

Poniższe wyniki pochodzą z pomiarów w środowisku testowym VM (Ubuntu 22.04, VirtIO dysk).

#### Zapis sekwencyjny [MB/s]

| Mechanizm    | Blok 4K | Blok 512K | Blok 1M | Blok 4M |
| ------------ | ------: | --------: | ------: | ------: |
| **Raw I/O**  |   124,6 |     381,2 |   398,8 |   414,8 |
| **LUKS**     |   103,4 |     351,1 |   387,1 |   420,3 |
| **fscrypt**  |   136,0 |     346,0 |   357,5 |   404,4 |
| **eCryptfs** |    94,2 |     210,7 |   218,5 |   217,5 |

#### Odczyt sekwencyjny [MB/s]

| Mechanizm    | Blok 4K | Blok 512K | Blok 1M | Blok 4M |
| ------------ | ------: | --------: | ------: | ------: |
| **Raw I/O**  |   124,4 |     474,9 |   848,5 |  1008,5 |
| **LUKS**     |    98,7 |     363,2 |   647,4 |   699,3 |
| **fscrypt**  |   103,3 |     381,0 |   715,0 |   820,1 |
| **eCryptfs** |   110,6 |     139,8 |   139,2 |   137,9 |

#### Narzut względem Raw I/O (%) — odczyt sekwencyjny

| Mechanizm    |     4K |   512K |     1M |     4M | Śr. narzut |
| ------------ | -----: | -----: | -----: | -----: | ---------: |
| **LUKS**     | −20,7% | −23,5% | −23,7% | −30,7% |     −24,6% |
| **fscrypt**  | −16,9% | −19,8% | −15,7% | −18,7% |     −17,8% |
| **eCryptfs** | −11,1% | −70,6% | −83,6% | −86,3% |     −62,9% |

> **Uwaga:** W środowisku VM brak sprzętowej akceleracji AES-NI (lub ograniczony passthrough) powoduje wyższy narzut LUKS i fscrypt niż w środowisku bare-metal (~2–5%). Wyniki dla eCryptfs odzwierciedlają architekturalny koszt przetwarzania per-plik.

### 4.3 Analiza wyników

#### eCryptfs – skalowalność z rozmiarem bloku

eCryptfs wykazuje unikalne zachowanie: przepustowość odczytu **nie rośnie** wraz z rozmiarem bloku (plateau ~140 MB/s dla 512K–4M). Wynika to z architektury per-plik:

- Każdy odczyt wymaga załadowania i odszyfrowania nagłówka kryptograficznego pliku.
- Kopiowanie stron w przestrzeni jądra (eCryptfs VFS → dolny FS) jest kosztowne przy dużych blokach.
- Brak możliwości odczytu zero-copy (O_DIRECT nieobsługiwane przez eCryptfs).

#### LUKS – narzut wyższy niż oczekiwany w VM

Teoretyczny narzut LUKS z AES-NI wynosi 2–5%. W pomiarach VM zaobserwowano 20–31% dla odczytu. Wynika to z:

1. Wirtualizacja bloku I/O (VirtIO) dodaje własne opóźnienie kolejkowania.
2. Translacja adresów w dm-crypt wymaga aktywnego CPU w warstwie kernel-space.
3. Brak lub ograniczony passthrough AES-NI w hypervisorze.

#### fscrypt – najlepsza alternatywa dla szyfrowania per-katalog

fscrypt osiąga wyniki zbliżone do LUKS (narzut odczytu ~18% vs ~25% w VM), przy jednoczesnym oferowaniu granularności na poziomie katalogu. Różnica wydajnościowa między LUKS a fscrypt zmniejsza się na sprzęcie fizycznym z AES-NI.

---

## 5. Analiza modelu ochrony

### 5.1 Porównanie warstw ochrony

| Cecha                              | LUKS (dm-crypt)                   | eCryptfs                      | fscrypt                               |
| ---------------------------------- | --------------------------------- | ----------------------------- | ------------------------------------- |
| **Warstwa szyfrowania**            | Blokowa (device layer)            | System plików (VFS stack)     | System plików (FS-native)             |
| **Granularność**                   | Cały wolumin / partycja           | Per-plik (indywidualny klucz) | Per-katalog (polityka)                |
| **Algorytm domyślny**              | AES-XTS-256/512                   | AES-CBC-256                   | AES-XTS-256                           |
| **Szyfrowanie nazw plików**        | Tak (transparent)                 | Opcjonalne (`fnek`)           | Tak (polityka)                        |
| **Szyfrowanie metadanych FS**      | Tak (inode, timestamps)           | **Nie** (rozmiar widoczny)    | Częściowe (nazwy tak, timestamps nie) |
| **Ochrona podczas pracy systemu**  | Brak (zamontowany = odszyfrowany) | Brak po montażu               | Blokada per-katalog (`fscrypt lock`)  |
| **Wiele kluczy / użytkowników**    | Do 32 slotów (LUKS2)              | Przez kernel keyring          | Polityki + protektory per-user        |
| **Integracja z PAM**               | Pośrednia (przez cryptsetup)      | Przez ecryptfs-setup-private  | Natywna (`libpam-fscrypt`)            |
| **Integracja z TPM / Secure Boot** | Tak (clevis + tang)               | Ograniczona                   | Ograniczona                           |
| **Ochrona pliku swap**             | Tak (jeśli swap na LUKS)          | **Nie** (swap poza eCryptfs)  | **Nie** (swap poza fscrypt)           |
| **Wsparcie projektu**              | Aktywne (cryptsetup 2.x)          | Maintenance mode (~2018)      | Aktywne (Google, Android, ChromeOS)   |
| **Dostępność w kernelu**           | Od kernel 2.6.4                   | Od kernel 2.6.19              | Od kernel 4.1 (ext4)                  |

### 5.2 Model zagrożeń

#### LUKS

```
CHRONI PRZED:
  ✅ Kradzieżą / zgubieniem dysku (atak offline)
  ✅ Bezpośrednim dostępem fizycznym do wyłączonego urządzenia
  ✅ Kopiowaniem obrazu dysku (disk imaging) – bez klucza = szum
  ✅ Forensic analysis dysków (brak czytelnych struktur FS)
  ✅ Nieautoryzowanym dostępem do serwera po fizycznej utracie hardware

NIE CHRONI PRZED:
  ❌ Atakiem na działający system (klucz w pamięci RAM)
  ❌ Złośliwym oprogramowaniem z uprawnieniami root
  ❌ Cold-boot attack (dump RAM w ciągu ~1–2 min od wyłączenia zasilania)
  ❌ Evil Maid Attack (modyfikacja bootloadera, brak Secure Boot / TPM)
  ❌ Kompromitacją systemu przed szyfrowaniem (malware w initramfs)
```

#### eCryptfs

```
CHRONI PRZED:
  ✅ Nieautoryzowanym odczytem plików z zaszyfrowanego katalogu (offline)
  ✅ Bezpośrednim odczytem z nośnika bez kluczy sesji
  ✅ Separacją danych dla różnych użytkowników (różne klucze)

NIE CHRONI PRZED:
  ❌ Ujawnieniem rozmiaru pliku (widoczny przez inode w dolnym FS)
  ❌ Ujawnieniem liczby plików i struktury katalogów (inode count)
  ❌ Wyciekiem danych przez swap / pliki tymczasowe (/tmp) poza eCryptfs
  ❌ Procesem działającym jako ten sam użytkownik (klucz załadowany w sesji)
  ❌ Atakiem na metadane (timestamps, rozmiar, uprawnienia)
  ❌ Nowoczesnym wektorem ataku (brak poprawek bezpieczeństwa od ~2018)
```

#### fscrypt

```
CHRONI PRZED:
  ✅ Offline access po wykonaniu `fscrypt lock` (katalog selektywnie blokowany)
  ✅ Separacją kluczy per-użytkownik (kernel keyring namespace)
  ✅ Dostępem do danych przy wylogowanym użytkowniku (PAM integration)
  ✅ Ujawnieniem nazw plików (zaszyfrowane tak jak treść)

NIE CHRONI PRZED:
  ❌ Dostępem użytkownika root (administrator może zawsze zamontować FS)
  ❌ Wyciekiem przez swap (bez szyfrowanego swap / LUKS)
  ❌ Ujawnieniem timestamps w inode (nie szyfrowane)
  ❌ Atakiem na zalogowaną sesję użytkownika (klucz załadowany)
  ❌ Cold-boot attack (klucze w kernel keyring = w RAM)
```

### 5.3 Ochrona metadanych

Istotną różnicą między mechanizmami jest zakres ochrony metadanych systemu plików:

| Metadane            | LUKS |  eCryptfs  |   fscrypt    |
| ------------------- | :--: | :--------: | :----------: |
| Treść pliku         |  ✅  |     ✅     |      ✅      |
| Nazwy plików        |  ✅  | ✅ (opcja) |      ✅      |
| Rozmiar pliku       |  ✅  |     ❌     |      ✅      |
| Timestamps inode    |  ✅  |     ❌     |      ❌      |
| Liczba plików       |  ✅  |     ❌     |      ✅      |
| Struktura katalogów |  ✅  |     ❌     | ✅ (per-dir) |
| Dane swap / tmp     | ✅\* |     ❌     |      ❌      |

> \* Tylko jeśli swap i /tmp rezydują na wolumenie LUKS.

Brak szyfrowania metadanych w eCryptfs jest istotną luką – atakujący z dostępem do dolnego systemu plików może odczytać rozmiar, strukturę katalogów i znaczniki czasu, co umożliwia analizę wzorców dostępu i wnioskowanie o zawartości bez znajomości klucza.

---

## 6. Rekomendacje dla scenariuszy użycia

### 6.1 Laptop / stacja robocza – pełne szyfrowanie dysku

**Rekomendacja: LUKS2 na partycji systemowej + szyfrowany swap**

LUKS zapewnia kompleksową ochronę całego dysku przy wyłączonym urządzeniu. Integracja z GRUB i initramfs umożliwia automatyczne odblokowanie przez hasło podczas startu. Dla wygody codziennego użycia możliwa jest integracja z TPM2:

```bash
# Powiązanie LUKS z TPM2 (PCR 7 = Secure Boot state)
sudo apt install clevis clevis-luks clevis-tpm2 clevis-initramfs
sudo clevis luks bind -d /dev/sda2 tpm2 '{"pcr_ids":"7"}'
sudo update-initramfs -u -k all
# Dysk odblokowuje się automatycznie jeśli Secure Boot nie był naruszony
```

**Dlaczego nie fscrypt?** – fscrypt nie chroni metadanych systemu plików (timestamps) ani danych poza swoimi katalogami (swap, /tmp, logi systemowe).

### 6.2 Serwer z danymi wrażliwymi / baza danych

**Rekomendacja: LUKS2 + Tang/Clevis (automatyczne odblokowywanie przez sieć)**

W środowisku serwerowym manualne podawanie hasła przy każdym restarcie jest niepraktyczne. Serwer Tang (Network Bound Disk Encryption) umożliwia automatyczne odblokowywanie LUKS, gdy serwer jest podłączony do sieci wewnętrznej:

```bash
# Serwer Tang (np. wewnętrzny serwer kluczy w sieci korporacyjnej):
sudo apt install tang
sudo systemctl enable --now tangd.socket

# Na serwerze z danymi – powiązanie LUKS z serwerem Tang:
sudo clevis luks bind -d /dev/sdb tang '{"url":"http://tang.internal:7500"}'
```

**Korzyść:** Dysk automatycznie odblokuje się podczas startu w sieci korporacyjnej. Po fizycznym usunięciu serwera z sieci lub wyłączeniu serwera Tang – dane niedostępne.

### 6.3 Środowisko wieloużytkownikowe / serwer plików

**Rekomendacja: fscrypt z politykami per-użytkownik + integracja PAM**

fscrypt umożliwia separację kluczy na poziomie użytkownika systemu. Dzięki integracji z PAM, katalog domowy użytkownika jest automatycznie odblokowywany przy logowaniu i blokowany przy wylogowaniu:

```bash
# Instalacja integracji PAM
sudo apt install libpam-fscrypt

# Konfiguracja /etc/pam.d/common-auth (dodaj na końcu):
# auth optional pam_fscrypt.so

# Konfiguracja /etc/pam.d/common-session (dodaj):
# session optional pam_fscrypt.so

# Zaszyfrowanie katalogu domowego użytkownika
fscrypt encrypt /home/alice --source=pam_passphrase --user=alice
```

**Korzyść:** Dane użytkownika `alice` są automatycznie zaszyfrowane gdy jest wylogowana. Administrator (root) nadal może zamontować FS i uzyskać dostęp – fscrypt nie zapewnia izolacji między rootem a użytkownikami.

### 6.4 Urządzenia mobilne / embedded Linux / IoT

**Rekomendacja: fscrypt (FBE – File-Based Encryption)**

fscrypt jest architekturą używaną przez Android od wersji 7.0 (FBE). Na urządzeniach embedded (Raspberry Pi, systemy IoT) z ext4:

```bash
# Weryfikacja obsługi fscrypt przez jądro
zcat /proc/config.gz | grep CONFIG_FS_ENCRYPTION
# CONFIG_FS_ENCRYPTION=y

# Formatowanie karty SD / eMMC z obsługą szyfrowania
sudo mkfs.ext4 -O encrypt /dev/mmcblk0p2
```

**Zaleta:** Selektywne szyfrowanie katalogów z danymi użytkownika bez pełnego zaszyfrowania partycji systemowej – ważne dla urządzeń z ograniczonymi zasobami CPU.

### 6.5 Dyski zewnętrzne / nośniki przenośne

**Rekomendacja: LUKS2 (przenośność + pełna ochrona)**

```bash
# Inicjalizacja zaszyfrowanego dysku zewnętrznego USB
sudo cryptsetup luksFormat --type luks2 /dev/sdc
sudo cryptsetup open /dev/sdc backup_disk
sudo mkfs.ext4 /dev/mapper/backup_disk
sudo mount /dev/mapper/backup_disk /mnt/backup
```

**Dlaczego LUKS?** – Dysk zewnętrzny może zostać zgubiony lub skradziony. LUKS zapewnia, że bez hasła dane są całkowicie niedostępne. Nośnik jest przenośny między systemami Linux z `cryptsetup`.

### 6.6 Selektywne szyfrowanie plików / legacy

**Rekomendacja: eCryptfs (jeśli fscrypt niedostępny) lub GPG dla archiwów**

eCryptfs nadal jest użyteczny w scenariuszach, gdzie:

- Jądro nie obsługuje fscrypt (< 4.1 lub bez `-O encrypt` w ext4).
- Wymagana jest kompatybilność z systemami skonfigurowanymi przed ~2018.
- Użytkownik korzysta z `ecryptfs-setup-private` dla katalogu `~/Private`.

Dla nowych wdrożeń: preferuj fscrypt. Dla archiwów: GPG z AES256.

```bash
# Szyfrowanie archiwum GPG (niezależne od infrastruktury szyfrowania FS)
tar czf - /home/user/ważne_dane/ | \
    gpg --symmetric --cipher-algo AES256 \
    --output backup_$(date +%Y%m%d).tar.gz.gpg
```

### 6.7 Tabela decyzyjna

| Scenariusz                      | Rekomendacja      | Uzasadnienie                                        |
| ------------------------------- | ----------------- | --------------------------------------------------- |
| Szyfrowanie całego dysku        | **LUKS2**         | Pełna ochrona offline, minimalne narzuty z AES-NI   |
| Szyfrowany swap                 | **LUKS2**         | Jedyna opcja chroniąca klucze w pamięci podręcznej  |
| Katalog domowy (multi-user)     | **fscrypt + PAM** | Auto-lock/unlock, dobra wydajność, aktywny rozwój   |
| Serwer – auto-unlock po starcie | **LUKS2 + Tang**  | NBDE – bezhasłowe odblokowanie w zaufanej sieci     |
| Dysk zewnętrzny / backup        | **LUKS2**         | Przenośność, prostota, pełna ochrona przy zgubieniu |
| Android / embedded (FBE)        | **fscrypt**       | Natywna obsługa jądra, selektywne szyfrowanie       |
| Legacy / stare jądra            | **eCryptfs**      | Dostępny od kernel 2.6.19, szeroka kompatybilność   |
| Archiwa / backup offline        | **GPG + AES256**  | Przenośność, brak zależności od konfiguracji FS     |

---

## 7. Podsumowanie i wnioski

### 7.1 Zestawienie porównawcze

| Kryterium                  |   LUKS2    |   eCryptfs    |  fscrypt   |
| -------------------------- | :--------: | :-----------: | :--------: |
| **Wydajność seq. odczytu** |  ⭐⭐⭐⭐  |     ⭐⭐      | ⭐⭐⭐⭐⭐ |
| **Wydajność seq. zapisu**  | ⭐⭐⭐⭐⭐ |    ⭐⭐⭐     |  ⭐⭐⭐⭐  |
| **Ochrona offline**        | ⭐⭐⭐⭐⭐ |    ⭐⭐⭐     |  ⭐⭐⭐⭐  |
| **Ochrona metadanych**     | ⭐⭐⭐⭐⭐ |     ⭐⭐      |  ⭐⭐⭐⭐  |
| **Granularność kontroli**  |    ⭐⭐    |   ⭐⭐⭐⭐    | ⭐⭐⭐⭐⭐ |
| **Prostota konfiguracji**  |  ⭐⭐⭐⭐  |    ⭐⭐⭐     |   ⭐⭐⭐   |
| **Integracja systemowa**   | ⭐⭐⭐⭐⭐ |    ⭐⭐⭐     | ⭐⭐⭐⭐⭐ |
| **Aktywność projektu**     | ⭐⭐⭐⭐⭐ | ⭐⭐ (legacy) | ⭐⭐⭐⭐⭐ |
| **Obsługa TPM / NBDE**     | ⭐⭐⭐⭐⭐ |      ⭐       |    ⭐⭐    |

### 7.2 Wnioski

**LUKS2** jest niezastąpionym mechanizmem dla każdego scenariusza wymagającego pełnej ochrony danych w spoczynku na poziomie urządzenia blokowego. Format LUKS2 z KDF Argon2id oraz integracja z Clevis/Tang/TPM czyni go standardem de facto dla szyfrowania dysków w Linuksie. Zmierzony narzut wydajnościowy w środowisku VM (ok. 25% na odczyt) jest znacznie wyższy niż na sprzęcie fizycznym z AES-NI (typowo 2–5%), co nie powinno wpływać negatywnie na decyzję o jego stosowaniu w produkcji.

**fscrypt** jest optymalnym wyborem dla szyfrowania katalogów per-użytkownik w środowiskach wieloużytkownikowych i embedded. Natywna integracja z jądrem (ext4, f2fs), aktywny rozwój przez Google/kernel community oraz wbudowane zarządzanie kluczami przez PAM stawiają go znacznie powyżej eCryptfs w nowoczesnych zastosowaniach. Wyniki benchmarków potwierdzają niższy narzut niż LUKS w środowisku VM (18% vs 25% dla odczytu), co wynika z braku warstwy dm-crypt.

**eCryptfs**, choć funkcjonalny, wykazuje fundamentalne ograniczenia architekturalne: brak skalowania przepustowości przy dużych blokach (plateau ~140 MB/s dla odczytu niezależnie od rozmiaru bloku 512K–4M), brak ochrony metadanych (rozmiar pliku, timestamps) oraz tryb maintenance mode od ~2018. Nowe projekty nie powinny opierać się na eCryptfs – jest jednak nadal dostępny jako fallback na starszych systemach.

**Kluczowa obserwacja:** żaden z analizowanych mechanizmów nie chroni przed atakiem cold-boot (klucze w RAM) ani przed złośliwym oprogramowaniem działającym z uprawnieniami root na uruchomionym systemie. Pełna strategia bezpieczeństwa wymaga dodatkowych warstw: Secure Boot, integralność systemu (IMA/EVM), szyfrowany swap oraz odpowiednia polityka zarządzania sesjami.

---

## 8. Materiały referencyjne

- [cryptsetup / LUKS documentation](https://gitlab.com/cryptsetup/cryptsetup) – GitLab
- [LUKS On-Disk Format Specification v2.1](https://gitlab.com/cryptsetup/cryptsetup/-/wikis/LUKS-standard)
- [fscrypt – google/fscrypt](https://github.com/google/fscrypt) – GitHub
- [Kernel documentation: fscrypt](https://www.kernel.org/doc/html/latest/filesystems/fscrypt.html)
- [eCryptfs – SourceForge](https://ecryptfs.sourceforge.net/)
- [Clevis / Tang (NBDE)](https://github.com/latchset/clevis)
- [fio documentation](https://fio.readthedocs.io/en/latest/)
- [dm-crypt – ArchWiki](https://wiki.archlinux.org/title/dm-crypt)
