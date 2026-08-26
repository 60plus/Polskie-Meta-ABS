# Polskie Meta ABS

Jeden kontener Docker z niezależnymi providerami metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/), przygotowanymi z myślą o polskich katalogach audiobooków.

Projekt łączy kilka polskich źródeł w jedną instalację. Każdy provider ma własny scraper i własne reguły dopasowania, ale wszystkie korzystają ze wspólnego środowiska Chromium/Playwright.

## Obsługiwane źródła

| Provider | Katalog | Port |
|---|---|---:|
| **Storytel Polska** | https://www.storytel.com/pl | `3000` |
| **Audioteka Polska** | https://audioteka.com/pl | `3001` |

Z punktu widzenia Audiobookshelf są to dwa osobne providery. Technicznie działają jednak w jednym kontenerze.

## Funkcje

- wyszukiwanie polskich wydań audiobooków;
- wyszukiwanie po tytule i opcjonalnie autorze;
- dopasowanie wyników na podstawie tytułu i autora;
- pobieranie danych ze stron konkretnych produktów;
- obsługa audiobooków oraz cykli Audioteki;
- okładki;
- opisy;
- autorzy;
- lektorzy i głosy;
- wydawcy;
- rok publikacji/wydania;
- ISBN, jeżeli jest dostępny;
- czas trwania;
- gatunki;
- serie i informacje o cyklu;
- wspólny format odpowiedzi dla Audiobookshelf.

Priorytetem są **polskie wydania i polskie dane katalogowe**.

## Jak to działa

Storytel i Audioteka korzystają z nowoczesnych stron renderowanych przez JavaScript. Dlatego scraper nie ogranicza się do zwykłego pobrania HTML.

Do obsługi katalogów wykorzystywany jest Chromium uruchamiany przez Playwright. Dzięki temu scraper może korzystać z danych pojawiających się dopiero po wykonaniu JavaScriptu.

```text
                         ┌── :3000 ──► Storytel Polska
Audiobookshelf ── Nginx ─┤
                         └── :3001 ──► Audioteka Polska
                                  │
                         wspólny kontener
                         Playwright + Chromium
```

Wewnątrz kontenera providery działają jako osobne aplikacje FastAPI, a Nginx rozdziela ruch na podstawie portu:

```text
:3000 → 127.0.0.1:8000 → Storytel
:3001 → 127.0.0.1:8001 → Audioteka
```

## Uruchomienie

Wymagany jest Docker oraz Docker Compose.

```bash
git clone https://github.com/60plus/Polskie-Meta-ABS.git
cd Polskie-Meta-ABS
docker compose up -d --build
```

Sprawdzenie kontenera:

```bash
docker compose ps
```

Logi:

```bash
docker compose logs -f
```

Zatrzymanie:

```bash
docker compose down
```

## Porty

### Storytel Polska

```text
http://SERWER:3000
```

### Audioteka Polska

```text
http://SERWER:3001
```

Porty `3000` i `3001` są przeznaczone do użycia przez Audiobookshelf. Porty `8000` i `8001` są wewnętrzne dla kontenera i nie wymagają wystawiania na hosta.

## Konfiguracja w Audiobookshelf

Dodaj każdy provider osobno jako niestandardowe źródło metadanych.

### Storytel Polska

```text
Nazwa: Storytel Polska
URL: http://SERWER:3000
```

### Audioteka Polska

```text
Nazwa: Audioteka Polska
URL: http://SERWER:3001
```

Dzięki temu Audiobookshelf może korzystać z obu katalogów niezależnie, mimo że scraper działa w jednym kontenerze.

## API

Oba providery udostępniają endpoint:

```text
GET /search
```

Parametry:

- `query` — tytuł lub fragment tytułu;
- `author` — opcjonalny autor.

Wymagany jest nagłówek:

```http
Authorization: <dowolna-wartość>
```

Nie jest wymagane logowanie do konta Storytel ani Audioteka.

### Przykład — Storytel

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3000/search?query=Wywy%C5%BCszenie%20Horusa&author=Dan%20Abnett'
```

### Przykład — Audioteka

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3001/search?query=Operacja%20Mir&author=Remigiusz%20Mr%C3%B3z'
```

## Health check

```bash
curl http://localhost:3000/health
curl http://localhost:3001/health
```

Przykładowe odpowiedzi:

```json
{"status":"ok","provider":"storytel"}
```

```json
{"status":"ok","provider":"audioteka"}
```

## Metadane Audioteki

Scraper Audioteki korzysta ze strony wyszukiwania, a następnie przechodzi na stronę konkretnego produktu lub cyklu. Pozwala to pobierać dane z właściwej strony zamiast opierać wynik wyłącznie na karcie wyszukiwania.

Dla różnych typów stron Audioteki wykorzystywane są odpowiednie sekcje strony.

### Audiobook

Dla stron:

```text
https://audioteka.com/pl/audiobook/...
```

opis jest pobierany z dedykowanego elementu:

```text
#audiobook-description
```

z fallbackami dla starszych wariantów HTML.

### Cykl / audioserial

Dla stron:

```text
https://audioteka.com/pl/cykl/...
```

scraper korzysta z dedykowanej sekcji opisu Audioteki, w tym klas zawierających:

```text
paragraph_description_
```

Dzięki temu opis nie jest przypadkowo pobierany z rekomendacji, stopki, recenzji lub innych elementów strony.

## Dopasowanie wyników

Wyniki nie są wybierane wyłącznie na podstawie pierwszego znalezionego adresu URL.

Provider bierze pod uwagę między innymi:

- podobieństwo tytułu;
- podobieństwo autora, jeśli został podany;
- typ strony — audiobook lub cykl;
- dane pobrane ze strony szczegółowej;
- zgodność znalezionego produktu z zapytaniem.

W przypadku Audioteki scraper rozróżnia właściwe strony produktów od stron katalogowych, takich jak listy nowości, bestsellery czy oferty specjalne.

## Wydajność i stabilność

Strony katalogów mogą wykonywać długotrwałe żądania w tle, dlatego scraper nie czeka na `networkidle`. Strona jest otwierana po załadowaniu DOM, a następnie scraper czeka tylko na potrzebne elementy.

Wspólne środowisko Chromium jest utrzymywane przez cały czas działania kontenera, a wyniki wyszukiwania mogą być obsługiwane z cache.

Ma to ograniczyć czas odpowiedzi i uniknąć sytuacji, w której Audiobookshelf musi wielokrotnie ponawiać to samo wyszukiwanie.

## Architektura projektu

```text
.
├── Dockerfile
├── compose.yml
├── nginx.conf
├── requirements.txt
├── scraper.py
├── storytel_provider.py
├── audioteka_provider.py
└── README.md
```

### `scraper.py`

Wspólna warstwa aplikacyjna i endpointy używane przez Audiobookshelf.

### `storytel_provider.py`

Niezależny scraper Storytel Polska — wyszukiwanie, pobieranie strony produktu i dopasowanie metadanych.

### `audioteka_provider.py`

Niezależny scraper Audioteki Polska — wyszukiwanie, obsługa stron audiobooków i cykli oraz pobieranie metadanych z właściwych sekcji strony.

### `nginx.conf`

Rozdziela ruch na podstawie portu:

```text
3000 → Storytel
3001 → Audioteka
```

### `Dockerfile`

Buduje jeden obraz zawierający środowisko potrzebne do uruchomienia FastAPI, Playwright/Chromium oraz Nginx.

## Dodawanie kolejnych katalogów

Projekt jest przygotowany jako baza dla kolejnych polskich źródeł metadanych.

Nowy provider powinien:

1. korzystać ze wspólnego środowiska Playwright/Chromium;
2. posiadać własny mechanizm wyszukiwania;
3. pobierać stronę szczegółową produktu;
4. wyciągać tylko metadane potrzebne Audiobookshelf;
5. posiadać własne reguły dopasowania;
6. zwracać wspólny format `matches`;
7. otrzymać własny port, jeżeli ma być wystawiony jako osobny provider w Audiobookshelf.

Dodanie kolejnego katalogu nie wymaga tworzenia osobnego kontenera.

## Uwagi

Strony Storytel i Audioteka mogą zmieniać HTML, routing, selektory lub sposób renderowania danych. W takim przypadku odpowiedni provider może wymagać aktualizacji.

Projekt korzysta z publicznie dostępnych danych katalogowych. Prawa do opisów, okładek, treści i innych materiałów pobieranych ze stron pozostają przy ich właścicielach.
