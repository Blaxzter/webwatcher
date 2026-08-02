# webwatcher

Rendert Webseiten mit Playwright (echtes Chromium, also auch JS-lastige Seiten),
vergleicht den Inhalt mit dem letzten Stand und schickt bei Änderungen eine
Telegram-Nachricht mit Diff und Screenshot.

Läuft eigenständig — kein Postgres, kein FastAPI. State liegt in einer SQLite-Datei.

```
🔔 Shop Produktseite hat sich geändert
+2 / -1 Zeilen
[Screenshot]

- Preis: 249,00 €
+ Preis: 199,00 €
+ Nur noch 3 auf Lager
```

## Begriffe vorweg

`webwatcher` ist das Programm, `run` / `check` / `chat-id` sind seine Unterbefehle
— wie bei `git status` oder `docker ps`. Du tippst sie wörtlich so ins Terminal,
es wird nichts ersetzt.

Läuft es im Docker-Container (der Normalfall auf dem Server), stellst du
`docker compose run --rm webwatcher` davor:

```bash
docker compose run --rm webwatcher list      # = "webwatcher list" im Container
```

## Schritt für Schritt auf dem Server

### 1. Bot anlegen — in der Telegram-App

[@BotFather](https://t.me/BotFather) anschreiben, `/newbot` senden, Namen
vergeben. Du bekommst einen Token wie `8123456789:AAHk3l-BeispielToken_xyz`.

### 2. Dem Bot einmal schreiben — in der Telegram-App

BotFather nennt dir den Benutzernamen deines Bots (`@mein_watcher_bot`). Such
ihn in Telegram und schick ihm **`/start`**.

Das ist keine Formalie: Telegram lässt Bots nur an Leute schreiben, die den
Chat selbst eröffnet haben. Ohne diesen Schritt gibt es auch keine Chat-ID.

### 3. Chat-ID holen — im Browser

Diese URL aufrufen, `<TOKEN>` durch deinen Token ersetzen:

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Das `bot` davor gehört dazu, also z.B.
`https://api.telegram.org/bot8123456789:AAHk3l-BeispielToken_xyz/getUpdates`

In der Antwort steht deine ID:

```json
{"ok":true,"result":[{"message":{"chat":{"id":123456789,"type":"private", ...
                                         ^^^^^^^^^ das ist sie
```

Kommt `{"ok":true,"result":[]}`, hast du Schritt 2 noch nicht gemacht (oder er
ist über 24 Stunden her — länger hebt Telegram das nicht auf).

### 4. Docker auf dem Server installieren

Falls noch nicht vorhanden (Ubuntu/Debian):

```bash
curl -fsSL https://get.docker.com | sh
```

### 5. Dateien auf den Server kopieren

Von deinem Rechner aus — PowerShell:

```powershell
.\deploy.ps1 -Remote benutzer@dein-server -RemoteDir /opt/webwatcher
```

Bash/macOS/Linux: `./deploy.sh benutzer@dein-server /opt/webwatcher`

Beide lassen `.venv`, `data/` und `.env` weg (siehe
[Updates auf den Server schieben](#updates-auf-den-server-schieben)). Benutze
**kein** `scp -r`: das schleppt die ~160 MB grosse `.venv` mit und legt beim
zweiten Aufruf ein verschachteltes Unterverzeichnis an.

Ab hier alles **auf dem Server**:

```bash
ssh benutzer@dein-server
cd /opt/webwatcher
```

### 6. Konfiguration anlegen

**Wichtig: vor dem ersten Docker-Befehl**, sonst legt Docker ein *Verzeichnis*
namens `config.yaml` an und nichts geht mehr.

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

`.env` bearbeiten (`nano .env`) — Token aus Schritt 1, ID aus Schritt 3:

```ini
TELEGRAM_BOT_TOKEN=8123456789:AAHk3l-BeispielToken_xyz
TELEGRAM_CHAT_ID=123456789
```

`config.yaml` bearbeiten (`nano config.yaml`) — die Beispielseiten unter
`sites:` durch deine ersetzen. Minimal reicht:

```yaml
sites:
  - name: Meine Seite
    url: https://example.com/das-was-dich-interessiert
    interval: 15m
```

### 7. Testen und starten

```bash
docker compose build                              # einmalig, dauert ein paar Minuten
docker compose run --rm webwatcher test-telegram  # kommt eine Nachricht an?
docker compose up -d                              # Daemon starten
docker compose logs -f                            # zuschauen (Strg+C beendet nur das Zuschauen)
```

Bei `test-telegram` sollte auf deinem Handy eine Nachricht ankommen. Kommt
stattdessen `HTTP 401: Unauthorized`, stimmt der Token nicht; bei
`HTTP 400: chat not found` die Chat-ID.

Beim ersten Durchlauf legt der Watcher pro Seite eine Baseline an und schickt
dir „wird jetzt beobachtet" mit Screenshot. Ab dann meldet er sich nur noch bei
Änderungen. Nach einem Server-Neustart läuft er dank `restart: unless-stopped`
automatisch wieder an.

## Updates auf den Server schieben

PowerShell:

```powershell
.\deploy.ps1                    # überträgt nach hetzner:~/docker/watcher
.\deploy.ps1 -Restart           # überträgt und startet neu
.\deploy.ps1 -IncludeEnv -Restart   # inklusive .env (neuer Token/neue Chat-ID)
.\deploy.ps1 -Remote meinserver -RemoteDir /srv/ww
.\deploy.ps1 -DryRun            # zeigt nur, was übertragen würde
```

Die `.env` bleibt standardmäßig **aus**, damit der Server seine eigenen
Zugangsdaten behält. Hast du lokal eine Chat-ID ergänzt oder den Token
gewechselt, brauchst du `-IncludeEnv` — sonst läuft der Server weiter mit den
alten Werten.

Und: **`docker compose restart` liest eine geänderte `.env` nicht neu ein**
(getestet). Nur `docker compose up -d` erzeugt den Container neu und übernimmt
sie — `deploy.ps1 -Restart` macht genau das.

Bash (macOS, Linux, Git Bash):

```bash
./deploy.sh                     # überträgt nach hetzner:~/docker/watcher
./deploy.sh --restart           # überträgt und startet neu
./deploy.sh meinserver /srv/ww  # anderer Host, anderes Verzeichnis
DRY_RUN=1 ./deploy.sh           # zeigt nur, was übertragen würde
```

`scp` ist dafür ungeeignet: es kennt kein `--exclude` und würde die ~160 MB
große `.venv` mitschleppen (der Rest ist zusammen ~165 KB). Ausserdem legt
`scp -r ordner ziel` beim **zweiten** Aufruf ein verschachteltes
`ziel/ordner` an, sobald das Ziel schon existiert.

`deploy.sh` packt stattdessen ein tar über SSH und lässt weg: `.venv`,
`__pycache__`, `data/`, `*.sqlite3`, `.env` und `.git`. Die `.env` bleibt
absichtlich aus — der Server behält seinen eigenen Token. Die `config.yaml`
geht mit, weil du sie lokal mit `webwatcher pick` pflegst.

Ohne Skript, als Einzeiler (funktioniert in PowerShell 7.4+ genauso wie in
Bash — PowerShell reicht Byte-Streams zwischen nativen Befehlen unverändert
durch). Voraussetzung ist Key-Authentifizierung: fragt ssh nach einem Passwort,
kann es das nicht, weil seine Standardeingabe vom tar-Stream belegt ist.

```powershell
tar czf - -C watcher --exclude=.venv --exclude=__pycache__ --exclude=data --exclude=.env . |
  ssh hetzner 'mkdir -p ~/docker/watcher && tar xzf - -C ~/docker/watcher'
```

Nach dem Übertragen auf dem Server aktivieren:

```bash
ssh hetzner 'cd ~/docker/watcher && docker compose up -d --build'
```

## Im Betrieb

Alle Befehle in `/opt/webwatcher` auf dem Server:

```bash
docker compose logs -f                              # Logs
docker compose run --rm webwatcher list             # Status aller Seiten
docker compose run --rm webwatcher check --show-diff  # sofort prüfen
docker compose restart                              # nach Änderung an config.yaml
docker compose down                                 # stoppen
```

Datenbank und Screenshots liegen im Docker-Volume `webwatcher-data`, nicht im
Projektordner — der Container läuft als unprivilegierter Benutzer und könnte in
ein bind-gemountetes `./data` nicht schreiben. Rankommen:

```bash
docker compose cp webwatcher:/app/data/screenshots ./screenshots
```

## Lokal ausprobieren (ohne Docker)

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
playwright install chromium

webwatcher chat-id                  # Alternative zu Schritt 3 oben
webwatcher pick https://example.com # Seite per Klick anlegen
webwatcher check --show-diff        # einmalig alle Seiten prüfen
webwatcher run                      # Daemon
```

Bash: `python -m venv .venv && source .venv/bin/activate`, der Rest identisch.

Ohne Docker kannst du die Chat-ID auch mit `webwatcher chat-id` holen — das
braucht nur den Token in der `.env`, die `TELEGRAM_CHAT_ID` darf noch leer sein.

## Mehrere Empfänger

Zwei Wege, je nachdem wie viele:

**Bis ~3 Personen: mehrere Chat-IDs.** Jede Person schreibt dem Bot einmal
`/start` (Telegram erlaubt Bots nur Nachrichten an Leute, die den Chat selbst
eröffnet haben — das lässt sich nicht umgehen), dann jeweils Schritt 3, und die
IDs kommen kommagetrennt in die `.env`:

```ini
TELEGRAM_CHAT_ID=123456789,987654321
```

In der `config.yaml` geht auch eine Liste:

```yaml
telegram:
  chat_id: [123456789, 987654321]
```

**Ab dann besser: eine Gruppe.** Gruppe anlegen, Bot einladen, dort etwas
schreiben, dann `webwatcher chat-id` — die Gruppen-ID ist negativ
(`-1001234567890`) und kommt als einzige ID in die `.env`. Vorteil: neue Leute
fügst du der Gruppe hinzu, ohne die Konfiguration anzufassen und ohne Neustart.
Sieht der Bot die Nachricht nicht (Privacy-Modus), schreib `/start@dein_bot`.

Screenshots werden nur **einmal** hochgeladen und an die weiteren Empfänger per
Telegram-`file_id` verteilt. Und blockiert einer der Empfänger den Bot, bekommen
die anderen ihre Meldung trotzdem — der Fehler landet nur im Log.
## Config per Klick bauen: `webwatcher pick`

Statt Selektoren aus den DevTools abzutippen:

```bash
webwatcher pick https://example.com/produkt/123 --name "Shop Produktseite" --interval 5m
```

Es öffnet sich ein sichtbares Chromium – **mit denselben Einstellungen, die der
Watcher später benutzt**. Fahre über die Seite, klicke das Element an, das
beobachtet werden soll, schalte auf „Ignorieren" und klicke Werbung und Banner
weg. Am Ende „Fertig", und der Block landet direkt in deiner `config.yaml`.

| Taste / Knopf | Wirkung |
| --- | --- |
| Klick | Element auswählen (je nach Modus beobachten oder ignorieren) |
| ↑ / ↓ | Eltern-/Kindelement wählen – ein Klick trifft immer das innerste Element |
| Tab | zwischen „nur dieses Element" und „alle gleichartigen" umschalten (siehe unten) |
| Seite bedienen | Picker pausieren, um Cookie-Banner wegzuklicken oder aufzuklappen |
| Vorschau | siehe unten |
| Esc | abbrechen, ohne etwas zu schreiben |

**„Vorschau" ist der eigentliche Trick.** Sie rendert die Seite zweimal
headless – also genau so, wie es der Server tut – und zeigt dir:

- den Text, der tatsächlich verglichen würde,
- ob dein Selektor headless überhaupt existiert (Cookie-Banner und Logins
  verändern die Seite oft, dann greift der Selektor aus dem sichtbaren Browser nicht),
- **alle Zeilen, die sich zwischen zwei Renders verändert haben.** Genau die
  produzieren später Fehlalarme. Ein Klick auf „Als ignore_patterns übernehmen"
  macht daraus fertige Regexes.

Selektoren, die nur über die Position funktionieren (`:nth-child`), werden mit
⚠ markiert – die brechen, sobald sich die Seitenstruktur ändert.

### Ganze Listen beobachten: Tab

Klickst du einen Eintrag einer Liste an, bekommst du standardmäßig genau diesen
einen (`li.item:nth-child(2)`). Meist willst du aber die **ganze** Liste – neue
Einträge sollen ja gerade auffallen. **Tab** schaltet auf den rein
klassenbasierten Selektor um (`li.item`): alle Treffer werden gleichzeitig
umrandet und das Label zeigt `li.item ×3`.

```yaml
- name: Neue Stellenangebote
  url: https://example.com/jobs
  selector: ".job-card"      # vergleicht alle Karten, nicht nur die erste
```

Der Watcher hängt den Text aller Treffer aneinander. Kommt eine Karte dazu,
fällt sie weg oder ändert sich, siehst du es im Diff. Elemente ohne brauchbaren
Klassennamen haben keine Tab-Variante – dann sagt der Picker das auch.

**Seiten hinter Login:**

```bash
webwatcher pick https://intern.example.com/status --profile ./browser-profil
```

Im Picker einloggen; die Session bleibt im Profilordner. Denselben Ordner auf
den Server kopieren, dann nutzt der Watcher dieselbe Anmeldung.

`pick` braucht einen Bildschirm, läuft also lokal, nicht auf dem Server. Der
übliche Weg: lokal picken, den erzeugten Block in die Server-`config.yaml`
kopieren (oder die Datei hochladen) und `docker compose restart`.

## Alternative: ohne Docker per systemd

Wer Chromium lieber direkt auf dem Server installiert statt im Container:

```bash
sudo useradd --system --create-home --home-dir /opt/webwatcher webwatcher
sudo -u webwatcher bash -c '
  cd /opt/webwatcher
  python3 -m venv .venv
  .venv/bin/pip install /pfad/zu/watcher
  .venv/bin/playwright install chromium
'
sudo /opt/webwatcher/.venv/bin/playwright install-deps chromium   # Systempakete für Chromium

sudo cp deploy/webwatcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now webwatcher
journalctl -u webwatcher -f
```

**RAM:** Chromium braucht ~300–500 MB pro paralleler Prüfung. Auf einem CX22
(4 GB) ist `concurrency: 2` entspannt; auf dem kleinsten CX11 lieber `1`.

## Konfiguration

Alles in `config.yaml`. Werte unter `defaults:` gelten für alle Seiten und
lassen sich pro Seite überschreiben. Secrets kommen per `${ENV_VAR}` rein.

```yaml
sites:
  - name: Shop Produktseite
    url: https://example.com/produkt/123
    interval: 5m
    selector: "#product-main"     # nur diesen Bereich vergleichen
    wait_for: ".price"            # warten bis das Element da ist
    ignore_selectors: [".ads", "#cookie-banner"]
    ignore_patterns: ['\d+ Besucher online']
    screenshot: full_page
```

| Option | Default | Bedeutung |
| --- | --- | --- |
| `interval` | `15m` | Prüfabstand (`30s`, `5m`, `2h`, `1d`) |
| `selector` | – | Nur dieser Bereich wird verglichen. Trifft der Selektor mehrere Elemente (z.B. `.produkt-karte`), werden **alle** verglichen |
| `ignore_selectors` | `[]` | Elemente, die vor dem Vergleich entfernt werden |
| `ignore_patterns` | `[]` | Regex; passende **Zeilen** fliegen raus |
| `track_attributes` | `[]` | z.B. `[class]` — erkennt Zustände, die nur im Attribut stehen (siehe unten) |
| `mode` | `text` | `text` = sichtbarer Text, `html` = Markup (erkennt auch Link-/Attributänderungen) |
| `wait_for` | – | CSS-Selektor, auf den vor dem Vergleich gewartet wird |
| `wait_until` | `networkidle` | Playwright-Ladezustand |
| `settle` | `0s` | Extra-Wartezeit nach dem Laden |
| `js` | – | JS, das vor dem Vergleich läuft (Cookie-Banner klicken, scrollen) |
| `screenshot` | `auto` | `auto` folgt dem `selector` (siehe unten). Sonst `full_page`, `viewport`, `element`, `none` |
| `min_changed_lines` | `1` | Kleinere Diffs werden still übernommen |
| `max_diff_lines` | `60` | Kürzt lange Diffs in der Nachricht |
| `notify_on_error_after` | `3` | Erst nach N Fehlern in Folge alarmieren |
| `block_resources` | `[]` | z.B. `[image, font, media]` — spart Traffic, wenn kein Screenshot nötig ist |
| `enabled` | `true` | Seite pausieren, ohne sie zu löschen |

### Wenn der Zustand nur in der CSS-Klasse steckt

Manche Seiten ändern bei einem Zustandswechsel gar keinen Text. Ein
Ticketkalender zeigt weiter die Tageszahl `8` — nur die Klasse springt von
`daySoldOut` auf `dayAvail`. Im Textmodus ist das **unsichtbar**.

```yaml
- name: Tickets August
  url: https://beispiel.de/kalender
  selector: div.calendar
  track_attributes: [class]     # <- macht Attributwechsel sichtbar
```

Damit steht pro Element mit diesem Attribut eine Zeile im Vergleich, und die
Meldung wird genau so knapp, wie man sie lesen will:

```
- class=daySoldOut | 8
+ class=dayAvail | 8
```

Mehrere Attribute gehen auch (`track_attributes: [class, href, title]`) — etwa
für Links, deren Ziel sich ändert, während der Linktext gleich bleibt.

`mode: html` würde solche Änderungen zwar auch erkennen, aber die Meldung wäre
unbrauchbar: eine Tabellenzeile ist *eine* Zeile Markup, die im Diff nach 300
Zeichen abgeschnitten wird. `track_attributes` ist dafür der gezieltere Weg.

### Was der Screenshot zeigt

Standardmäßig (`screenshot: auto`) zeigt das Bild das, was auch verglichen wird:

| Lage | Screenshot |
| --- | --- |
| `selector` trifft **ein** Element | nur dieses Element |
| `selector` trifft **mehrere** (Liste) | ganze Seite — eine einzelne Karte würde die Änderung ja verstecken |
| kein `selector` | sichtbarer Bereich |

Das ist wichtig, wenn der beobachtete Teil unterhalb des sichtbaren Bereichs
liegt: mit `viewport` bekämst du sonst das Hero-Bild der Seite statt der Tabelle,
auf die du wartest. Feste Werte (`full_page`, `viewport`, `element`, `none`)
haben weiterhin Vorrang, wenn du sie explizit setzt.

### Rauschen loswerden

Die häufigste Ursache für Fehlalarme sind Zeitstempel, Zähler und Werbung.
Drei Hebel, in dieser Reihenfolge:

1. **`selector`** — beobachte nur den Teil, der dich interessiert.
2. **`ignore_selectors`** — wirf bekannte Störer raus (Banner, „zuletzt angesehen").
3. **`ignore_patterns`** — Regex gegen dynamische Zeilen (`'^Stand: \d{2}\.'`).

Am schnellsten geht das über `webwatcher pick` und dessen Vorschau — die findet
instabile Zeilen automatisch. Manuell: `webwatcher check --show-diff --no-notify`
laufen lassen und schauen, was noch im Diff auftaucht.

## Kommandos

| Kommando | Zweck |
| --- | --- |
| `webwatcher pick <url>` | Elemente im Browser anklicken, Block in die Config schreiben |
| `webwatcher run` | Daemon; prüft jede Seite in ihrem Intervall |
| `webwatcher check [seite...]` | Einmalig prüfen; `--no-notify`, `--show-diff` |
| `webwatcher list` | Status aller Seiten |
| `webwatcher history [seite] -n 50` | Letzte Checks |
| `webwatcher reset [seite...]` | Baseline verwerfen (nächster Check ist wieder der erste) |
| `webwatcher test-telegram` | Verbindung prüfen |
| `webwatcher chat-id` | Chat-ID aus den letzten Bot-Nachrichten lesen |

Seiten werden über ihren Namen oder Key angesprochen: `webwatcher check "Shop Produktseite"`
oder `webwatcher check shop-produktseite`.

## Wie der Vergleich funktioniert

1. Seite mit Chromium laden, auf `wait_until`/`wait_for` warten, optional eigenes JS ausführen.
2. Screenshot machen (noch vom **unveränderten** Zustand).
3. `ignore_selectors` aus dem DOM entfernen, dann Text bzw. HTML des `selector` holen.
4. Whitespace normalisieren, leere Zeilen und `ignore_patterns` verwerfen, SHA-256 bilden.
5. Hash mit dem letzten Stand vergleichen; bei Unterschied einen zeilenbasierten Diff bauen.
6. Ab `min_changed_lines` Änderungen: Telegram-Nachricht mit Screenshot und Diff.

Der erste Check legt nur die Baseline an. Screenshots landen unter
`data/screenshots/<key>/`, es werden die letzten `keep_screenshots` behalten.
Sehr hohe `full_page`-Screenshots verschickt Telegram nicht als Foto — die
gehen dann automatisch als Datei raus.

## Fehlerbehebung

**„Timeout beim Laden"** — `wait_until: networkidle` ist streng; Seiten mit
Dauer-Polling werden nie idle. Dann `wait_until: load` plus `settle: 2s`.

**Ständige Änderungsmeldungen** — siehe „Rauschen loswerden".

**„Target closed" / Chromium stirbt** — zu wenig Shared Memory. In Docker ist
`shm_size: "1gb"` gesetzt; bare metal hilft weniger `concurrency`.

**Telegram meldet 429** — zu viele Nachrichten. Der Client wartet automatisch
die angegebene Zeit ab; bei vielen Seiten die Intervalle erhöhen.
