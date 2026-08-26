# Polskie Meta ABS

Jeden kontener Docker z niezależnymi providerami metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/), przygotowanymi z myślą o polskich katalogach książek i audiobooków.

Projekt łączy kilka polskich źródeł w jedną instalację. Każdy provider ma własny scraper, własne reguły dopasowania i może korzystać z innej techniki pobierania danych.

## Obsługiwane źródła

| Provider | Katalog | Port |
|---|---|---:|
| **Storytel Polska** | https://www.storytel.com/pl | `3000` |
| **Audioteka Polska** | https://audioteka.com/pl | `3001` |
| **Lubimyczytać Polska** | https://lubimyczytac.pl | `3002` |

Z punktu widzenia Audiobookshelf są to trzy osobne providery. Technicznie działają jednak w jednym kontenerze.

## Funkcje

- wyszukiwanie polskich wydań książek i audiobooków;
- wyszukiwanie po tytule i opcjonalnie autorze;
- dopasowanie wyników na podstawie tytułu i autora;
- pobieranie danych ze stron konkretnych produktów;
- obsługa audiobooków oraz cykli Audioteki;
- obsługa zarówno książek, jak i audiobooków z Lubimyczytać;
- okładki;
- opisy;
- autorzy;
- lektorzy i głosy, jeżeli źródło je udostępnia;
- wydawcy;
- rok publikacji/wydania;
- ISBN, jeżeli jest dostępny;
- czas trwania;
- gatunki;
- serie i informacje o cyklu;
- wspólny format odpowiedzi dla Audiobookshelf.

Priorytetem są **polskie wydania i polskie dane katalogowe**.

## Architektura

```text
                              ┌── :3000 ──► Storytel Polska
Audiobookshelf ── Nginx ──────┼── :3001 ──► Audioteka Polska
                              └── :3002 ──► Lubimyczytać Polska
                                         │
                                  wspólny kontener
                              FastAPI + providerzy
```

Wewnątrz kontenera providery działają jako osobne aplikacje FastAPI, a Nginx rozdziela ruch na podstawie portu:

```text
:3000 → 127.0.0.1:8000 → Storytel
:3001 → 127.0.0.1:8001 → Audioteka
:3002 → 127.0.0.1:8002 → Lubimyczytać
```

### Techniki pobierania danych

Providery nie muszą korzystać z jednego wspólnego mechanizmu.

- **Storytel** i **Audioteka** korzystają z Playwright/Chromium tam, gdzie wymagane jest renderowanie strony lub wykonanie JavaScriptu.
- **Lubimyczytać** korzysta z szybkiego klienta HTTP (`httpx`) oraz `BeautifulSoup`. Wyszukiwanie i strony szczegółowe są pobierane bez uruchamiania Playwrighta. Dzięki temu provider Lubimyczytać jest szybszy i nie jest zależny od nawigacji Chromium przy każdym wyniku.

Wspólne środowisko kontenera nadal zawiera Playwright/Chromium dla providerów, które go wymagają.

## Uruchomienie

Wymagany jest Docker oraz Docker Compose.

```bash
git clone https://github.com/60plus/Polskie-Meta-ABS.git
cd Polskie-Meta-ABS
docker compose up -d --build
```

`compose.override.yml` jest automatycznie dołączany przez Docker Compose i dodaje mapowanie portu `3002`.

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

### Lubimyczytać Polska

```text
http://SERWER:3002
```

Porty `3000`, `3001` i `3002` są przeznaczone do użycia przez Audiobookshelf. Porty `8000`, `8001` i `8002` są wewnętrzne dla kontenera i nie wymagają wystawiania na hosta.

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

### Lubimyczytać Polska

```text
Nazwa: Lubimyczytać Polska
URL: http://SERWER:3002
```

Dzięki temu Audiobookshelf może korzystać z trzech katalogów niezależnie, mimo że scraper działa w jednym kontenerze.

## API

Każdy provider udostępnia endpoint:

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

Nie jest wymagane logowanie do konta źródła.

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

### Przykład — Lubimyczytać

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3002/search?query=Rewizja&author=Remigiusz%20Mr%C3%B3z'
```

Lub dla audiobooka:

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3002/search?query=Siostry'
```

## Health check

```bash
curl http://localhost:3000/health
curl http://localhost:3001/health
curl http://localhost:3002/health
```

Przykładowa odpowiedź Lubimyczytać:

```json
{"status":"ok","provider":"lubimyczytac"}
```

## Lubimyczytać Polska

Provider Lubimyczytać działa na porcie `3002` i wyszukuje **zarówno książki, jak i audiobooki**.

Wyniki są pobierane z dwóch katalogów Lubimyczytać:

```text
https://lubimyczytac.pl/szukaj/ksiazki?phrase=...
https://lubimyczytac.pl/szukaj/audiobooki?phrase=...
```

Następnie provider otwiera strony szczegółowe znalezionych produktów, np.:

```text
https://lubimyczytac.pl/ksiazka/...
https://lubimyczytac.pl/audiobook/...
```

### Wydajne wyszukiwanie

Wyszukiwanie Lubimyczytać jest wykonywane bez Playwrighta. Provider używa jednego współdzielonego klienta `httpx`, keep-alive oraz cache wyników.

Książki i audiobooki są wyszukiwane równolegle. Następnie wyniki są deduplikowane, oceniane pod kątem zgodności z tytułem/autorem i dopiero najlepsze kandydatury są pobierane ze stron szczegółowych.

To jest celowo zbliżone do lekkiej architektury stosowanej przez [lakafior/lubimyczytac-abs](https://github.com/lakafior/lubimyczytac-abs): wyszukiwanie HTTP zamiast uruchamiania przeglądarki dla każdego zapytania.

### Opis i pełne metadane

Dla stron książek i audiobooków provider próbuje pobrać opis z dedykowanego elementu:

```text
#book-description
```

Jeżeli opis nie jest dostępny w tym elemencie, używany jest `og:description`, a dodatkowe dane mogą zostać uzupełnione z JSON-LD strony.

Provider zbiera między innymi:

- tytuł;
- autora;
- lektora/czytającego, jeżeli jest podany;
- wydawcę;
- opis;
- okładkę;
- ISBN;
- rok wydania;
- język;
- czas trwania/czytania, jeżeli jest dostępny;
- cykl i numer tomu;
- typ wydania (`book` lub `audiobook`).

Dla audiobooków szczególnie ważne są dane pobierane ze **strony audiobooka**, a nie wyłącznie z karty wyników wyszukiwania. Dzięki temu audiobook może otrzymać opis oraz dodatkowe dane wydania, które nie są widoczne na samej liście wyników.

### URL-e audiobooków

Provider zachowuje prawidłową postać URL-a strony audiobooka. Nie wymusza automatycznie końcowego `/`, ponieważ Lubimyczytać może używać różnych wariantów routingu dla książek i audiobooków.

### Cache

Wyniki wyszukiwania są przechowywane w pamięci przez ograniczony czas (`CACHE_TTL`). Wspólny klient HTTP utrzymuje połączenia keep-alive, aby kolejne wyszukiwania nie musiały tworzyć nowych połączeń TCP.

## Metadane Audioteki

Scraper Audioteki korzysta ze strony wyszukiwania, a następnie przechodzi na stronę konkretnego produktu lub cyklu. Pozwala to pobierać dane z właściwej strony zamiast opierać wynik wyłącznie na karcie wyszukiwania.

### Audiobook

Dla stron:

```text
https://audioteka.com/pl/audiobook/...
```

opis jest pobierany z dedykowanego elementu:

```text
#audiobook-description
```

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
- dane pobrane ze strony szczegółowej;
- typ wyniku — książka lub audiobook;
- zgodność znalezionego produktu z zapytaniem.

Dla Lubimyczytać celowo pozostawiono oba typy wyników, ponieważ ten sam tytuł może występować jako zwykła książka oraz audiobook, a dane między wydaniami mogą się różnić.

## Wydajność i stabilność

Providerzy korzystają z różnych strategii zależnie od źródła. Nie ma potrzeby uruchamiania Playwrighta tam, gdzie katalog można pobrać bezpośrednio przez HTTP.

Lubimyczytać jest obsługiwane przez `httpx` + `BeautifulSoup`, z cache i współdzielonym klientem HTTP. Dzięki temu wyszukiwanie jest lekkie i szybkie, a pobieranie pełnych metadanych odbywa się dopiero dla wybranych kandydatów.

Storytel i Audioteka mogą korzystać z Playwright/Chromium, ponieważ ich strony wymagają renderowania lub wykonania JavaScriptu w odpowiednich miejscach.

## Architektura projektu

```text
.
├── Dockerfile
├── compose.yml
├── compose.override.yml
├── nginx.conf
├── requirements.txt
├── scraper.py
├── audioteka_provider.py
├── lubimyczytac_provider.py
└── README.md
```

### `scraper.py`

Provider Storytel Polska oraz warstwa aplikacyjna pierwszego providera.

### `audioteka_provider.py`

Niezależny scraper Audioteki Polska — wyszukiwanie, obsługa stron audiobooków i cykli oraz pobieranie metadanych z właściwych sekcji strony.

### `lubimyczytac_provider.py`

Niezależny scraper Lubimyczytać Polska — szybkie wyszukiwanie HTTP, wyszukiwanie książek i audiobooków, otwieranie stron szczegółowych oraz pobieranie pełnych metadanych.

Nie jest potrzebny osobny plik `lubimyczytac_provider_v2.py`.

### `nginx.conf`

Rozdziela ruch na podstawie portu:

```text
3000 → Storytel
3001 → Audioteka
3002 → Lubimyczytać
```

### `Dockerfile`

Buduje jeden obraz zawierający środowisko potrzebne do uruchomienia FastAPI, zależności providerów, Playwright/Chromium oraz Nginx.

### `compose.override.yml`

Dodaje mapowanie portu `3002` do hosta. Plik jest automatycznie uwzględniany przez Docker Compose przy standardowym `docker compose up`.

## Dodawanie kolejnych katalogów

Nowy provider powinien:

1. posiadać własny mechanizm wyszukiwania;
2. korzystać z Playwright tylko wtedy, gdy jest rzeczywiście potrzebny;
3. pobierać stronę szczegółową produktu;
4. wyciągać metadane potrzebne Audiobookshelf;
5. posiadać własne reguły dopasowania;
6. zwracać wspólny format `matches`;
7. otrzymać własny port, jeżeli ma być wystawiony jako osobny provider w Audiobookshelf.

Dodanie kolejnego katalogu nie wymaga tworzenia osobnego kontenera.

## Uwagi

Strony Storytel, Audioteka i Lubimyczytać mogą zmieniać HTML, routing, selektory lub sposób renderowania danych. W takim przypadku odpowiedni provider może wymagać aktualizacji.

Projekt korzysta z publicznie dostępnych danych katalogowych. Prawa do opisów, okładek, treści i innych materiałów pobieranych ze stron pozostają przy ich właścicielach.
