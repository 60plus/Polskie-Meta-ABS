# Storytel.pl-ADB

Provider metadanych dla [Audiobookshelf](https://www.audiobookshelf.org/) korzystający z publicznie dostępnego katalogu [Storytel Polska](https://www.storytel.com/pl).

Projekt jest przeznaczony przede wszystkim do wyszukiwania **polskich wydań audiobooków** i pobierania ich metadanych bezpośrednio ze Storytel. Nie korzysta z katalogu Lubimyczytać ani nie zakłada zgodności ze strukturą jego wyszukiwarki.

## Funkcje

- wyszukiwanie audiobooków po tytule;
- opcjonalne dopasowanie autora;
- wyszukiwanie bezpośrednio w polskim katalogu Storytel;
- obsługa stron renderowanych po stronie klienta dzięki Playwright;
- pobieranie metadanych potrzebnych Audiobookshelfowi;
- tytuł, autor, lektor, wydawca, opis, okładka, ISBN, rok wydania, język, czas trwania, gatunki i seria;
- ranking wyników na podstawie podobieństwa tytułu i autora;
- prosty cache w pamięci ograniczający liczbę powtarzanych zapytań;
- endpoint `/health` do sprawdzania stanu kontenera;
- gotowe pliki Docker i Docker Compose.

## Jak to działa

Storytel Polska korzysta z aplikacji internetowej renderowanej przez JavaScript. Zwykłe pobranie strony HTTP nie zawsze zawiera wyniki wyszukiwania widoczne w przeglądarce.

Dlatego provider korzysta z Playwright i prawdziwego silnika Chromium:

```text
Audiobookshelf
      │
      ▼
GET /search
      │
      ▼
Storytel Polska
      │
      ▼
wyszukiwanie renderowane w Chromium
      │
      ▼
strony książek /pl/books/...
      │
      ▼
metadane książki
      │
      ▼
format odpowiedzi Audiobookshelf
```

Dzięki temu wyszukiwanie opiera się na tym samym katalogu, który jest dostępny użytkownikowi w przeglądarce Storytel Polska.

## Instalacja przez Docker

Sklonuj repozytorium:

```bash
git clone https://github.com/60plus/Storytel.pl-ADB.git
cd Storytel.pl-ADB
```

Uruchom kontener:

```bash
docker compose up -d --build
```

Domyślnie provider nasłuchuje na porcie `3000`.

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

## Konfiguracja portu

Port można zmienić za pomocą zmiennej środowiskowej `PORT`.

Przykład:

```yaml
services:
  storytel-pl-abs:
    build: .
    ports:
      - "3000:3000"
    environment:
      PORT: 3000
```

## Integracja z Audiobookshelf

W Audiobookshelf dodaj provider metadanych jako własny / niestandardowy provider zgodnie z konfiguracją używanej wersji Audiobookshelf.

Adres providera powinien wskazywać na kontener lub host, na którym działa aplikacja, np.:

```text
http://adres-serwera:3000
```

Endpoint wyszukiwania providera:

```text
GET /search
```

Provider wymaga nagłówka `Authorization`. Wartość nagłówka nie jest obecnie używana do autoryzacji zewnętrznego konta Storytel — służy zgodności z interfejsem providera metadanych.

## API

### `GET /search`

Parametry:

- `query` — wymagany tytuł książki;
- `author` — opcjonalny autor.

Przykład:

```bash
curl -s \
  -H 'Authorization: test' \
  'http://localhost:3000/search?query=Wywy%C5%BCszenie%20Horusa&author=Dan%20Abnett'
```

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
      "cover": "https://covers.storytel.com/...",
      "isbn": "9788386758937",
      "genres": ["Fantasy"],
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

`duration` jest zwracane w minutach, zgodnie z wymaganiami Audiobookshelf.

### `GET /health`

Endpoint kontrolny:

```bash
curl http://localhost:3000/health
```

Odpowiedź:

```json
{"status":"ok"}
```

## Struktura projektu

```text
.
├── Dockerfile
├── compose.yml
├── requirements.txt
├── scraper.py
└── README.md
```

## Wymagania

Projekt korzysta z:

- Python 3;
- FastAPI;
- Uvicorn;
- Playwright;
- Chromium dostarczanego przez obraz Playwright.

Wszystkie wymagane zależności są zdefiniowane w `requirements.txt`, a środowisko przeglądarkowe jest dostarczane przez obraz Docker używany w `Dockerfile`.

## Dane i zakres działania

Źródłem danych jest **Storytel Polska** (`storytel.com/pl`). Provider jest nastawiony na polski katalog i polskie wydania audiobooków.

Projekt nie wymaga logowania do konta Storytel. Pobierane są informacje dostępne publicznie na stronach Storytel.

Storytel może w przyszłości zmienić sposób działania swojej strony, strukturę HTML lub mechanizm wyszukiwania. W takim przypadku selektory i sposób ekstrakcji metadanych mogą wymagać aktualizacji.

## Licencja

Projekt jest udostępniony zgodnie z licencją znajdującą się w repozytorium. Dane i materiały pobierane ze Storytel pozostają własnością odpowiednich właścicieli praw.
