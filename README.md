# Polskie Meta ABS

Jeden kontener Docker z providerami metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/), przygotowanymi głównie z myślą o polskich książkach i audiobookach.

Projekt łączy trzy źródła w jednej instalacji:

| Provider | Źródło | Port |
|---|---|---:|
| **Storytel Polska** | https://www.storytel.com/pl | `3000` |
| **Audioteka Polska** | https://audioteka.com/pl | `3001` |
| **Lubimyczytać Polska** | https://lubimyczytac.pl | `3002` |

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
- obsługiwać zarówno książki, jak i audiobooki z Lubimyczytać.

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

Porty `3000`, `3001` i `3002` są przeznaczone do użycia przez Audiobookshelf. Porty wewnętrzne kontenera nie muszą być wystawiane na hosta.

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

Przykład:

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3002/search?query=Siostry&author=Amelia%20Malisz'
```

## Lubimyczytać Polska

Provider Lubimyczytać wyszukuje **zarówno książki, jak i audiobooki**.

Dla jednego zapytania sprawdzane są oba katalogi:

```text
https://lubimyczytac.pl/szukaj/ksiazki?phrase=...
https://lubimyczytac.pl/szukaj/audiobooki?phrase=...
```

Następnie najlepsze wyniki są sprawdzane na stronach konkretnych produktów:

```text
https://lubimyczytac.pl/ksiazka/...
https://lubimyczytac.pl/audiobook/...
```

Dzięki temu audiobook nie jest ograniczony wyłącznie do informacji widocznych na liście wyszukiwania.

### Metadane

Provider pobiera między innymi:

- tytuł;
- autora;
- lektora, jeśli jest podany;
- opis;
- okładkę;
- wydawcę;
- ISBN;
- rok wydania;
- język;
- gatunek;
- serię i numer tomu;
- czas trwania/czytania, jeśli jest dostępny.

W przypadku audiobooków dane są pobierane bezpośrednio ze strony audiobooka. Pozwala to uzyskać opis i pozostałe informacje dotyczące właściwego wydania, zamiast kopiować dane wyłącznie z książki o tym samym tytule.

### Szybkie wyszukiwanie

Lubimyczytać korzysta z bezpośredniego pobierania stron zamiast uruchamiania przeglądarki dla każdego wyniku. Książki i audiobooki są sprawdzane równolegle, a pełne dane są pobierane dopiero dla wybranych wyników.

Wyniki wyszukiwania są również przez krótki czas zapamiętywane, dzięki czemu kolejne wyszukiwanie tego samego tytułu jest szybsze.

## Audioteka Polska

Provider Audioteki obsługuje audiobooki oraz cykle/audioseriale.

Dane są pobierane ze strony właściwego produktu, dzięki czemu wynik może zawierać pełniejszy opis i informacje o wydaniu.

Dla Audioteki obsługiwane są między innymi:

- tytuł;
- autorzy;
- lektorzy;
- opis;
- okładka;
- wydawca;
- rok wydania;
- czas trwania;
- informacje o cyklu.

## Dopasowanie

Wyniki są oceniane na podstawie tytułu i autora, a następnie wybierane są najlepiej pasujące pozycje.

W Lubimyczytać pozostawiane są zarówno książki, jak i audiobooki, ponieważ ten sam tytuł może mieć różne wydania i różne dane.

## Sprawdzanie działania

Health check każdego providera:

```bash
curl http://localhost:3000/health
curl http://localhost:3001/health
curl http://localhost:3002/health
```

Dla Lubimyczytać przykładowa odpowiedź:

```json
{"status":"ok","provider":"lubimyczytac"}
```

## Struktura projektu

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

Provider Storytel Polska.

### `audioteka_provider.py`

Provider Audioteki Polska — wyszukiwanie oraz pobieranie metadanych audiobooków i cykli.

### `lubimyczytac_provider.py`

Provider Lubimyczytać Polska — wyszukiwanie książek i audiobooków oraz pobieranie ich metadanych.

### `nginx.conf`

Łączy trzy providery w jeden kontener i udostępnia je na osobnych portach.

### `Dockerfile` i `compose.yml`

Konfiguracja potrzebna do uruchomienia całego projektu w Dockerze.

## Dodawanie kolejnych źródeł

Nowy provider powinien mieć własne wyszukiwanie, pobierać dane właściwego produktu i zwracać je w formacie obsługiwanym przez Audiobookshelf.

Nie trzeba tworzyć osobnego kontenera dla każdego źródła.

## Uwagi

Serwisy źródłowe mogą zmieniać wygląd i sposób udostępniania danych. W takim przypadku odpowiedni provider może wymagać aktualizacji.

Projekt korzysta z publicznie dostępnych danych katalogowych. Prawa do opisów, okładek i pozostałych materiałów pozostają przy ich właścicielach.
