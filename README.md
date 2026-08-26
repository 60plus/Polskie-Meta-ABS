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

Provider BookBeat wyszukuje polskie pozycje z katalogu BookBeat i pobiera dane ze stron konkretnych książek.

Obsługiwane są między innymi:

- wyszukiwanie po tytule;
- wyszukiwanie po serii;
- autor;
- lektor;
- opis;
- okładka;
- wydawca;
- rok wydania;
- ISBN;
- język;
- gatunek;
- seria i numer tomu;
- czas trwania.

BookBeat może udostępniać ten sam tytuł jako audiobook i e-book. Provider zwraca wynik jako audiobook, gdy jest używany jako źródło metadanych dla biblioteki audiobooków.

Dla przykładu BookBeat posiada stronę produktu `Operacja Mir`, a wyszukiwanie serii może znaleźć również pozycje należące do danego cyklu. Dane takie jak opis, autor, lektor i czas trwania są pobierane ze strony produktu, a nie tylko z listy wyszukiwania.

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

Provider BookBeat Polska — wyszukiwanie książek, wyszukiwanie po serii oraz pobieranie metadanych stron produktów.

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
