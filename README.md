# Polskie Meta ABS

Jeden kontener Docker z niezależnymi providerami metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/), przygotowanymi z myślą o polskich katalogach książek i audiobooków.

Projekt łączy kilka polskich źródeł w jedną instalację. Każdy provider ma własny scraper i własne reguły dopasowania, ale wszystkie korzystają ze wspólnego środowiska Chromium/Playwright.

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

## Jak to działa

Storytel, Audioteka i Lubimyczytać korzystają z nowoczesnych stron internetowych, dlatego scraper nie ogranicza się do zwykłego pobrania HTML.

Do obsługi katalogów wykorzystywany jest Chromium uruchamiany przez Playwright. Dzięki temu scraper może korzystać z danych pojawiających się dopiero po wykonaniu JavaScriptu.

```text
                              ┌── :3000 ──► Storytel Polska
Audiobookshelf ── Nginx ──────┼── :3001 ──► Audioteka Polska
                              └── :3002 ──► Lubimyczytać Polska
                                         │
                                  wspólny kontener
                                  Playwright + Chromium
```

Wewnątrz kontenera providery działają jako osobne aplikacje FastAPI, a Nginx rozdziela ruch na podstawie portu:

```text
:3000 → 127.0.0.1:8000 → Storytel
:3001 → 127.0.0.1:8001 → Audioteka
:3002 → 127.0.0.1:8002 → Lubimyczytać
```

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

## Lubimyczytać

Provider Lubimyczytać działa niezależnie na porcie `3002` i wyszukuje **zarówno książki, jak i audiobooki**. Nie ogranicza wyników wyłącznie do audiobooków, ponieważ strona książki może zawierać lepszy lub bardziej kompletny opis oraz dane wydania.

Wyniki są następnie otwierane na stronach szczegółowych, np.:

```text
https://lubimyczytac.pl/audiobook/...
https://lubimyczytac.pl/ksiazka/...
```

### Opis

Dla obu typów stron opis jest pobierany z dedykowanego elementu:

```text
#book-description
```

To ważne, ponieważ opis może być zwinięty przyciskiem „więcej”, ale treść nadal znajduje się w tym elemencie i może zostać odczytana bez klikania.

### Dane książki

Provider wykorzystuje dane ze strony szczegółowej oraz JSON-LD i może zwrócić między innymi:

- tytuł;
- autora;
- lektora, jeśli jest podany;
- wydawcę;
- opis;
- okładkę;
- ISBN;
- datę/rok wydania;
- język;
- kategorię/gatunek;
- cykl i numer tomu;
- czas czytania lub trwania, jeżeli jest dostępny.

Dzięki temu zwykła książka może być użyta jako wartościowe źródło metadanych również wtedy, gdy użytkownik dopasowuje audiobook.

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

Strony katalogów mogą wykonywać długotrwałe żądania w tle, dlatego providerzy nie muszą czekać na `networkidle`. Strona jest otwierana po załadowaniu DOM, a następnie scraper czeka tylko na potrzebne elementy.

Wspólne środowisko Chromium jest utrzymywane przez cały czas działania kontenera, a wyniki wyszukiwania są obsługiwane z cache przez ograniczony czas.

Ma to ograniczyć czas odpowiedzi i zmniejszyć liczbę ponownych wyszukiwań wykonywanych przez Audiobookshelf.

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

Provider Storytel Polska oraz wspólna warstwa aplikacyjna dla pierwszego providera.

### `audioteka_provider.py`

Niezależny scraper Audioteki Polska — wyszukiwanie, obsługa stron audiobooków i cykli oraz pobieranie metadanych z właściwych sekcji strony.

### `lubimyczytac_provider.py`

Niezależny scraper Lubimyczytać Polska — wyszukiwanie książek i audiobooków, otwieranie stron szczegółowych oraz pobieranie metadanych z dedykowanych elementów strony.

### `nginx.conf`

Rozdziela ruch na podstawie portu:

```text
3000 → Storytel
3001 → Audioteka
3002 → Lubimyczytać
```

### `Dockerfile`

Buduje jeden obraz zawierający środowisko potrzebne do uruchomienia FastAPI, Playwright/Chromium oraz Nginx.

### `compose.override.yml`

Dodaje mapowanie portu `3002` do hosta. Plik jest automatycznie uwzględniany przez Docker Compose przy standardowym `docker compose up`.

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

Strony Storytel, Audioteka i Lubimyczytać mogą zmieniać HTML, routing, selektory lub sposób renderowania danych. W takim przypadku odpowiedni provider może wymagać aktualizacji.

Projekt korzysta z publicznie dostępnych danych katalogowych. Prawa do opisów, okładek, treści i innych materiałów pobieranych ze stron pozostają przy ich właścicielach.
