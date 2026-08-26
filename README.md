# Polskie Meta dla Audiobookshelf

Jeden kontener z providerami metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/) opartymi o polskie katalogi audiobooków.

Obecnie obsługiwane są:

- **Storytel Polska**
- **Audioteka Polska**

Projekt korzysta z jednego wspólnego środowiska Playwright/Chromium. Każdy provider ma własną logikę wyszukiwania i ekstrakcji metadanych, ale nie wymaga osobnego kontenera.

## Obsługiwane providery

| Provider | Katalog | Port | Endpoint |
|---|---|---:|---|
| Storytel Polska | `storytel.com/pl` | `3000` | `/search?provider=storytel` |
| Audioteka Polska | `audioteka.com/pl` | `3001` | `/search?provider=audioteka` |

## Jak to działa

Storytel i Audioteka korzystają z nowoczesnych stron renderowanych przez JavaScript. Dlatego zwykłe pobranie HTML nie zawsze daje te same wyniki, które są widoczne w przeglądarce.

Providery wykorzystują wspólny Chromium uruchamiany przez Playwright:

```text
Audiobookshelf
      │
      ▼
Polskie Meta dla ABS
      │
      ├──────────────► Storytel Polska
      │                    port 3000
      │
      └──────────────► Audioteka Polska
                           port 3001
```

Wyniki są następnie zamieniane na format metadanych odpowiedni dla Audiobookshelf.

## Pobierane metadane

Providery starają się zwracać najważniejsze informacje o audiobooku:

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
- wynik podobieństwa.

Źródłem danych są bezpośrednio polskie katalogi Storytel i Audioteka. Priorytetem są **polskie wydania i polskie dane katalogowe**.

## Uruchomienie

Wymagany jest Docker z Docker Compose.

```bash
git clone https://github.com/60plus/Storytel.pl-ADB.git
cd Storytel.pl-ADB
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

## Storytel Polska

Provider korzysta z polskiego wyszukiwania Storytel oraz z indywidualnych stron książek.

Adres providera:

```text
http://adres-serwera:3000
```

Przykład:

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3000/search?provider=storytel&query=Wywy%C5%BCszenie%20Horusa&author=Dan%20Abnett'
```

## Audioteka Polska

Provider korzysta z polskiego katalogu Audioteka i z tego samego środowiska Chromium, które jest używane przez Storytel.

Adres providera:

```text
http://adres-serwera:3001
```

Przykład:

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3001/search?provider=audioteka&query=Mazurski%20przekr%C4%99t'
```

Parser Audioteki jest przygotowany również na pozycje będące audioserialami i seriami, dlatego nie ogranicza się wyłącznie do klasycznych książek.

## Wspólne API

### `GET /search`

Parametry:

- `provider` — `storytel` albo `audioteka`;
- `query` — wymagany tytuł;
- `author` — opcjonalny autor.

Wymagany jest nagłówek:

```http
Authorization: <dowolna-wartość>
```

Przykładowa odpowiedź:

```json
{
  "matches": [
    {
      "title": "...",
      "author": "...",
      "narrator": "...",
      "publisher": "...",
      "publishedYear": "2024",
      "description": "...",
      "cover": "https://...",
      "isbn": "...",
      "genres": ["..."],
      "series": [
        {
          "series": "...",
          "sequence": "1"
        }
      ],
      "language": "pol",
      "duration": 696,
      "type": "audiobook",
      "similarity": 1.0
    }
  ]
}
```

`duration` jest podawane w minutach.

### `GET /health`

```bash
curl http://localhost:3000/health
```

Odpowiedź:

```json
{"status":"ok"}
```

### `GET /providers`

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3000/providers'
```

Zwraca listę dostępnych providerów.

## Audiobookshelf

W Audiobookshelf możesz dodać każdy provider jako osobny niestandardowy provider metadanych.

### Storytel Polska

```text
Nazwa: Storytel Polska
URL: http://adres-serwera:3000
```

### Audioteka Polska

```text
Nazwa: Audioteka Polska
URL: http://adres-serwera:3001
```

Niestandardowe providery metadanych dodaje się w ustawieniach Audiobookshelf. citeturn883065search0

## Struktura projektu

```text
.
├── Dockerfile
├── compose.yml
├── requirements.txt
├── scraper.py
└── README.md
```

## Architektura

Jeden kontener współdzieli:

- FastAPI;
- Playwright;
- Chromium;
- cache;
- mechanizmy normalizacji i rankingu.

Każdy provider ma własne:

- adresy wyszukiwania;
- selektory stron;
- parser metadanych;
- zasady dopasowania wyników.

Dzięki temu dodanie kolejnego polskiego źródła nie wymaga uruchamiania kolejnego kontenera.

## Dodawanie kolejnego providera

Nowy provider powinien:

1. wyszukiwać pozycje w swoim katalogu;
2. pobierać stronę szczegółową;
3. wyciągać potrzebne metadane;
4. oceniać dopasowanie tytułu i autora;
5. zwracać wynik w formacie Audiobookshelf.

## Uwagi

Struktura stron Storytel i Audioteka może się zmieniać. W przypadku zmiany HTML, routingu lub sposobu renderowania strony odpowiedni parser może wymagać aktualizacji.

Projekt pobiera dane dostępne publicznie na stronach dostawców. Prawa do treści, opisów, okładek i innych materiałów źródłowych pozostają przy ich właścicielach.
