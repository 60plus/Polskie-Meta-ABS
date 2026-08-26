# Polskie Meta ABS

Jeden kontener Docker z providerami metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/), przygotowanymi głównie z myślą o polskich książkach i audiobookach.

Projekt łączy cztery źródła w jednej instalacji:

| Provider | Źródło | Port |
|---|---|---:|
| **Storytel Polska** | https://www.storytel.com/pl | `3000` |
| **Audioteka Polska** | https://audioteka.com/pl | `3001` |
| **Lubimyczytać Polska** | https://lubimyczytac.pl | `3002` |
| **BookBeat Polska** | https://www.bookbeat.com/pl | `3003` |

Dla Audiobookshelf każdy z nich jest osobnym źródłem metadanych.

## Co potrafi

- wyszukiwać książki i audiobooki po tytule oraz autorze;
- dopasowywać właściwe wydanie do biblioteki;
- pobierać okładki i opisy;
- pobierać autorów i lektorów, jeśli są dostępni;
- pobierać wydawcę, rok wydania i ISBN, jeśli są dostępne;
- pobierać język, gatunek, serię i numer tomu, jeśli źródło je udostępnia;
- pobierać czas trwania audiobooka, jeśli jest dostępny;
- obsługiwać audiobooki i cykle Audioteki;
- obsługiwać zarówno książki, jak i audiobooki z Lubimyczytać;
- obsługiwać audiobooki z BookBeat.

Priorytetem są polskie wydania i dane katalogowe.

## Uruchomienie

Wymagany jest Docker oraz Docker Compose.

```bash
git clone https://github.com/60plus/Polskie-Meta-ABS.git
cd Polskie-Meta-ABS
docker compose up -d --build
```

Sprawdzenie działania:

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

### BookBeat Polska

```text
Nazwa: BookBeat Polska
URL: http://SERWER:3003
```

Porty `3000`–`3003` są przeznaczone do użycia przez Audiobookshelf. Porty wewnętrzne kontenera nie muszą być wystawiane na hosta.

## API

Każdy provider udostępnia:

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

Nie jest wymagane konto w serwisach źródłowych.

## Lubimyczytać Polska

Provider Lubimyczytać wyszukuje **zarówno książki, jak i audiobooki**.

Dla jednego zapytania sprawdzane są oba katalogi, a następnie najlepsze wyniki są sprawdzane na stronach konkretnych produktów.

Dzięki temu audiobook nie jest ograniczony wyłącznie do informacji widocznych na liście wyszukiwania.

Provider pobiera między innymi tytuł, autora, lektora, opis, okładkę, wydawcę, ISBN, rok wydania, język, gatunek, serię, numer tomu i czas trwania, jeśli dane są dostępne.

Lubimyczytać korzysta z bezpośredniego pobierania stron zamiast uruchamiania przeglądarki dla każdego wyniku. Książki i audiobooki są sprawdzane równolegle, a pełne dane są pobierane dopiero dla wybranych wyników.

## Audioteka Polska

Provider Audioteki obsługuje audiobooki oraz cykle/audioseriale.

Dane są pobierane ze strony właściwego produktu, dzięki czemu wynik może zawierać pełniejszy opis i informacje o wydaniu.

## BookBeat Polska

Provider BookBeat obsługuje polski katalog BookBeat i wyszukuje audiobooki po tytule oraz autorze. Wyniki są następnie sprawdzane na stronie konkretnego produktu.

Dane szczegółowe są pobierane z wyrenderowanej strony produktu, dzięki czemu provider może korzystać z metadanych, które nie są dostępne bezpośrednio w wynikach wyszukiwania.

Obsługiwane są między innymi:

- tytuł i autor;
- lektor;
- opis;
- okładka;
- wydawca;
- rok publikacji;
- ISBN audiobooka;
- język;
- gatunek;
- seria i numer tomu;
- czas trwania audiobooka.

BookBeat może udostępniać ten sam tytuł jako audiobook i e-book. Provider dla Audiobookshelf korzysta z danych wydania audiobookowego, jeśli są dostępne, dzięki czemu np. czas trwania, ISBN i wydawca dotyczą właściwego wydania.

### Rozwijanie pełnych metadanych

Na stronie produktu BookBeat część dodatkowych informacji jest domyślnie ukryta za przyciskiem **„Pokaż więcej”**. Provider automatycznie rozwija tę sekcję przed odczytem metadanych.

Obsługiwany jest również ekran zgody OneTrust. Provider próbuje zamknąć warstwę zgody przed dalszym przetwarzaniem strony, dzięki czemu overlay nie blokuje rozwijania metadanych.

Nie jest wykonywane drugie kliknięcie **„Pokaż więcej”** — zapobiega to ponownemu zwinięciu sekcji i regresji przy kolejnym żądaniu do Audiobookshelf.

Opis oraz szczegółowe informacje są pobierane ze strony produktu, a nie z samej listy wyszukiwania. Dzięki temu wynik może zawierać również pełniejsze informacje o obsadzie, lektorze, serii i wydaniu.

## Dopasowanie

Wyniki są oceniane na podstawie tytułu i autora, a następnie wybierane są najlepiej pasujące pozycje.

W Lubimyczytać pozostawiane są zarówno książki, jak i audiobooki, ponieważ ten sam tytuł może mieć różne wydania i różne dane.

## Sprawdzanie działania

Health check każdego providera:

```bash
curl http://localhost:3000/health
curl http://localhost:3001/health
curl http://localhost:3002/health
curl http://localhost:3003/health
```

Przykładowa odpowiedź BookBeat:

```json
{"status":"ok","provider":"bookbeat"}
```

## Struktura projektu

```text
.
├── Dockerfile
├── compose.yml
├── nginx.conf
├── requirements.txt
├── scraper.py
├── audioteka_provider.py
├── lubimyczytac_provider.py
├── bookbeat_provider.py
└── README.md
```

### `scraper.py`

Provider Storytel Polska.

### `audioteka_provider.py`

Provider Audioteki Polska — wyszukiwanie oraz pobieranie metadanych audiobooków i cykli.

### `lubimyczytac_provider.py`

Provider Lubimyczytać Polska — wyszukiwanie książek i audiobooków oraz pobieranie ich metadanych.

### `bookbeat_provider.py`

Provider BookBeat Polska — wyszukiwanie oraz pobieranie metadanych audiobooków ze stron produktów, z obsługą zgody OneTrust i rozwijania ukrytych metadanych.

### `nginx.conf`

Łączy cztery providery w jeden kontener i udostępnia je na osobnych portach.

### `Dockerfile` i `compose.yml`

Konfiguracja potrzebna do uruchomienia całego projektu w Dockerze.

## Dodawanie kolejnych źródeł

Nowy provider powinien mieć własne wyszukiwanie, pobierać dane właściwego produktu i zwracać je w formacie obsługiwanym przez Audiobookshelf.

Nie trzeba tworzyć osobnego kontenera dla każdego źródła.

## Uwagi

Serwisy źródłowe mogą zmieniać wygląd i sposób udostępniania danych. W takim przypadku odpowiedni provider może wymagać aktualizacji.

Projekt korzysta z publicznie dostępnych danych katalogowych. Prawa do opisów, okładek i pozostałych materiałów pozostają przy ich właścicielach.
