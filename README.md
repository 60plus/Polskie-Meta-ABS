# Polskie Meta ABS

Jeden kontener z niezależnymi providerami metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/), skoncentrowanymi na polskich katalogach audiobooków.

## Obsługiwane źródła

| Provider | Źródło | Port |
|---|---|---:|
| Storytel Polska | `https://www.storytel.com/pl` | `3000` |
| Audioteka Polska | `https://audioteka.com/pl` | `3001` |

Oba providery działają w **jednym kontenerze Docker** i korzystają ze wspólnego środowiska Playwright/Chromium.

## Dlaczego Playwright?

Zarówno Storytel, jak i Audioteka korzystają z nowoczesnych stron internetowych renderowanych przez JavaScript. Zwykłe pobranie HTML może nie zawierać wyników, które użytkownik widzi w przeglądarce.

Provider uruchamia więc Chromium i wykonuje strony tak, jak robi to normalna przeglądarka:

```text
Audiobookshelf
      │
      ├── :3000 ──► Storytel Polska
      │
      └── :3001 ──► Audioteka Polska
                         │
                  wspólny kontener
                  Playwright + Chromium
```

Każdy katalog ma własny scraper, selektory i mechanizm dopasowania. Wspólne są infrastruktura, przeglądarka, cache i format odpowiedzi dla Audiobookshelf.

## Pobierane metadane

Provider zwraca informacje dostępne w katalogu danego źródła, w szczególności:

- tytuł;
- autor;
- lektor;
- wydawca;
- opis;
- okładka;
- ISBN;
- rok wydania;
- język;
- czas trwania w minutach;
- gatunki;
- seria i numer w serii;
- wynik dopasowania.

Priorytetem są **polskie wydania i polskie dane katalogowe**.

## Uruchomienie

Wymagany jest Docker z Docker Compose.

```bash
git clone https://github.com/60plus/Polskie-Meta-ABS.git
cd Polskie-Meta-ABS
docker compose up -d --build
```

Jeżeli repozytorium lokalnie ma jeszcze starą nazwę, po zmianie nazwy na GitHubie można używać dotychczasowego katalogu roboczego — nie ma potrzeby ponownego klonowania.

Sprawdzenie:

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

Porty są tylko wejściami do dwóch niezależnych providerów. Wewnątrz kontenera działa jeden serwer aplikacji i jeden wspólny Chromium.

## Test Storytel

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3000/search?query=Wywy%C5%BCszenie%20Horusa&author=Dan%20Abnett'
```

## Test Audioteka

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3001/search?query=Mazurski%20przekr%C4%99t'
```

## Health check

```bash
curl http://localhost:3000/health
curl http://localhost:3001/health
```

Przykładowa odpowiedź:

```json
{"status":"ok","provider":"storytel"}
```

lub:

```json
{"status":"ok","provider":"audioteka"}
```

## API wyszukiwania

Oba providery udostępniają ten sam endpoint:

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

Przykładowa odpowiedź:

```json
{
  "matches": [
    {
      "title": "Wywyższenie Horusa",
      "author": "Dan Abnett",
      "narrator": "Filip Kosior",
      "publisher": "Copernicus Corporation",
      "publishedYear": "2022",
      "description": "...",
      "cover": "https://...",
      "isbn": "9788386758937",
      "genres": ["..."],
      "series": [
        {
          "series": "Herezja Horusa",
          "sequence": "1"
        }
      ],
      "language": "pol",
      "duration": 757,
      "type": "audiobook",
      "similarity": 1.0
    }
  ]
}
```

`duration` jest zwracane w minutach.

## Audiobookshelf

Dodaj każdy provider osobno jako niestandardowy provider metadanych.

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

Dzięki osobnym portom Audiobookshelf traktuje je jako dwa niezależne źródła, mimo że fizycznie działają w jednym kontenerze.

## Architektura projektu

```text
.
├── Dockerfile
├── compose.yml
├── nginx.conf
├── requirements.txt
├── scraper.py
└── README.md
```

### `scraper.py`

Wspólna aplikacja FastAPI zawierająca oba providery oraz wspólną obsługę Playwright/Chromium.

### `nginx.conf`

Rozdziela ruch na podstawie portu:

```text
3000 → Storytel Polska
3001 → Audioteka Polska
```

Oba porty przekazują żądania do tej samej aplikacji FastAPI.

### `Dockerfile`

Buduje jeden obraz zawierający:

- Python;
- FastAPI;
- Uvicorn;
- Playwright;
- Chromium;
- Nginx.

## Dopasowanie wyników

Wyszukiwanie nie polega wyłącznie na prostym porównaniu tekstu.

Provider bierze pod uwagę między innymi:

- podobieństwo tytułu;
- podobieństwo autora, jeśli został podany;
- polski język wydania;
- dane ze strony konkretnego produktu.

Dzięki temu rekomendacje i przypadkowe pozycje o podobnym tytule mają mniejszą szansę zostać zwrócone jako właściwe dopasowanie.

## Dodawanie kolejnych polskich katalogów

Projekt został przygotowany jako baza pod kolejne źródła metadanych.

Nowy provider powinien:

1. korzystać ze wspólnego Playwright/Chromium;
2. posiadać własny mechanizm wyszukiwania;
3. pobierać stronę szczegółową produktu;
4. wyciągać tylko metadane potrzebne Audiobookshelf;
5. stosować własne reguły dopasowania;
6. zwracać ten sam format `matches`.

Kolejne źródła mogą dostać własne porty, bez tworzenia kolejnego kontenera.

## Uwagi

Strony Storytel i Audioteka mogą zmieniać HTML, routing lub sposób renderowania wyników. W takim przypadku odpowiedni scraper może wymagać aktualizacji.

Projekt korzysta z publicznie dostępnych danych katalogowych. Prawa do opisów, okładek, treści i innych materiałów pobieranych ze stron pozostają przy ich właścicielach.
