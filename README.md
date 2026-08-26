# 🇵🇱 Polskie Meta ABS

### Polskie metadane dla Audiobookshelf — szybko, prosto i w jednym kontenerze.

urlDocker Hub — 60plus/polskie-meta-abshttps://hub.docker.com/r/60plus/polskie-meta-abs

**Polskie Meta ABS** dodaje do [Audiobookshelf](https://www.audiobookshelf.org/) polskie źródła metadanych dla książek i audiobooków.

Zamiast ręcznie poprawiać okładki, autorów, opisy czy lektorów — instalujesz jeden kontener i dodajesz źródła w Audiobookshelf.

### 📚 Obsługiwane źródła

- 🇸🇪 **Storytel Polska**
- 🎧 **Audioteka Polska**
- 📖 **Lubimyczytać Polska**
- 🎧 **BookBeat Polska**

Providerzy obsługują wyszukiwanie po tytule i autorze oraz pobierają dostępne dane wydania, m.in. **okładkę, opis, autora, lektora, wydawcę, ISBN, rok wydania, serię, gatunek i czas trwania**.

---

## 🚀 Instalacja z Docker Hub — polecana

Nie musisz klonować repozytorium ani budować obrazu samodzielnie.

### 1. Pobierz najnowszy obraz

```bash
docker pull 60plus/polskie-meta-abs:latest
```

### 2. Uruchom kontener

```bash
docker run -d \
  --name polskie-meta-abs \
  --restart unless-stopped \
  -p 3000:3000 \
  -p 3001:3001 \
  -p 3002:3002 \
  -p 3003:3003 \
  60plus/polskie-meta-abs:latest
```

### 3. Sprawdź

```bash
docker ps
```

Logi:

```bash
docker logs -f polskie-meta-abs
```

Gotowe. 🎉

---

# 🎧 Dodanie do Audiobookshelf

W Audiobookshelf przejdź do ustawień biblioteki / metadanych i dodaj **Custom Metadata Provider**.

Dodaj źródła, których chcesz używać:

| Źródło | URL providera |
|---|---|
| **Storytel Polska** | `http://IP_SERWERA:3000` |
| **Audioteka Polska** | `http://IP_SERWERA:3001` |
| **Lubimyczytać Polska** | `http://IP_SERWERA:3002` |
| **BookBeat Polska** | `http://IP_SERWERA:3003` |

Przykład dla serwera o adresie `192.168.1.100`:

```text
http://192.168.1.100:3002
```

### 💡 Jeśli Audiobookshelf działa na tym samym Docker hostcie

Jeżeli Audiobookshelf i Polskie Meta ABS są w tej samej sieci Docker, najlepiej użyć nazwy kontenera zamiast adresu IP, np.:

```text
http://polskie-meta-abs:3002
```

---

## ⭐ Dlaczego Polskie Meta ABS?

### 🇵🇱 Skupione na polskim katalogu

Źródła zostały dobrane przede wszystkim pod kątem polskich książek i audiobooków.

### 🖼️ Lepsze okładki i opisy

Providerzy pobierają dane bezpośrednio ze stron konkretnych wydań, a nie tylko z wyników wyszukiwania.

### 🎙️ Audiobooki

Dostępne są informacje takie jak lektor, czas trwania, wydawca czy ISBN — jeśli dane źródło je udostępnia.

### 📚 Lubimyczytać

Wyszukiwanie obejmuje zarówno książki, jak i audiobooki, dzięki czemu można znaleźć właściwe wydanie nawet wtedy, gdy występuje ono w obu katalogach.

### 🎧 BookBeat

Provider obsługuje również dane ukryte za **„Pokaż więcej”**, dzięki czemu może pobierać pełniejsze informacje z karty audiobooka.

### 🐳 Jeden kontener

Wszystkie źródła działają razem. Nie potrzebujesz osobnych kontenerów dla każdego providera.

---

# 🔄 Aktualizacja

Pobierz najnowszy obraz:

```bash
docker pull 60plus/polskie-meta-abs:latest
```

Następnie odtwórz kontener:

```bash
docker stop polskie-meta-abs
docker rm polskie-meta-abs

docker run -d \
  --name polskie-meta-abs \
  --restart unless-stopped \
  -p 3000:3000 \
  -p 3001:3001 \
  -p 3002:3002 \
  -p 3003:3003 \
  60plus/polskie-meta-abs:latest
```

Jeśli używasz Docker Compose, możesz również pobrać obraz i wykonać:

```bash
docker compose pull
docker compose up -d
```

---

# 🛠️ Instalacja z repozytorium

Jeżeli wolisz budować obraz samodzielnie:

```bash
git clone https://github.com/60plus/Polskie-Meta-ABS.git
cd Polskie-Meta-ABS
docker compose up -d --build
```

---

# 🔍 Diagnostyka

Sprawdzenie kontenera:

```bash
docker ps
```

Logi:

```bash
docker logs -f polskie-meta-abs
```

Test providerów:

```bash
curl http://localhost:3000/health
curl http://localhost:3001/health
curl http://localhost:3002/health
curl http://localhost:3003/health
```

---

# ❤️ Projekt

**Polskie Meta ABS** jest projektem społecznościowym dla użytkowników Audiobookshelf, którzy chcą wygodniej korzystać z polskich książek i audiobooków.

Jeżeli projekt jest dla Ciebie przydatny — ⭐ zostaw gwiazdkę na GitHubie i podziel się nim z innymi użytkownikami Audiobookshelf.

urlGitHub — 60plus/Polskie-Meta-ABShttps://github.com/60plus/Polskie-Meta-ABS

urlDocker Hub — 60plus/polskie-meta-abshttps://hub.docker.com/r/60plus/polskie-meta-abs

---

## ⚠️ Uwaga

Źródła zewnętrzne mogą zmieniać swoje strony i sposób udostępniania danych. W takim przypadku provider może wymagać aktualizacji.

Projekt korzysta z publicznie dostępnych danych katalogowych. Prawa do opisów, okładek i pozostałych materiałów należą do ich właścicieli.
