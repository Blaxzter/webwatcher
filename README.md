# webwatcher

Rendert Webseiten mit Playwright (echtes Chromium, also auch JS-lastige Seiten),
vergleicht den Inhalt mit dem letzten Stand und schickt bei Änderungen eine
Telegram-Nachricht mit Diff und Screenshot.

Läuft eigenständig — kein Postgres, kein Build-Schritt. Alles liegt in einer
SQLite-Datei: die beobachteten Seiten, ihr Zustand und die Historie. Gepflegt
werden sie im Terminal oder in einer [Weboberfläche](#weboberfläche), die im
selben Prozess mitläuft.

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

In der `config.yaml` stehen Telegram, Speicherorte und Voreinstellungen. Die
beobachteten **Seiten stehen dort nicht** — die liegen in der Datenbank und
werden über die [Weboberfläche](#weboberfläche) oder `webwatcher pick`
gepflegt. Für den Anfang reicht die Datei also so, wie sie ist.

Hast du schon einen `sites:`-Block (oder willst ihn aus dem Beispiel
übernehmen), holst du ihn einmalig in die Datenbank:

```bash
docker compose run --rm webwatcher import-config
```

Danach wird der Block nicht mehr gelesen und kann raus.

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
.\deploy.ps1                    # überträgt nach hetzner:~/docker/watcher/watcher
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
./deploy.sh                     # überträgt nach hetzner:~/docker/watcher/watcher
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
  ssh hetzner 'mkdir -p ~/docker/watcher/watcher && tar xzf - -C ~/docker/watcher/watcher'
```

Nach dem Übertragen auf dem Server aktivieren:

```bash
ssh hetzner 'cd ~/docker/watcher/watcher && docker compose up -d --build'
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

## Weboberfläche

Seiten anlegen, den zu beobachtenden Bereich direkt auf der Seite anklicken,
Verlauf und Screenshots ansehen, sofort prüfen lassen — alles im Browser. Die
Oberfläche läuft im selben Prozess wie der Daemon, Änderungen greifen ohne
Neustart.

> **Sie bringt keine eigene Anmeldung mit.** Wer sie erreicht, kann eigenes
> JavaScript auf beliebigen Seiten ausführen lassen (`js:` pro Seite) — das ist
> Codeausführung auf deinem Server. Sie gehört deshalb hinter einen Reverse
> Proxy, der die Authentifizierung übernimmt, oder auf `127.0.0.1`.

### Hinter Traefik

Die mitgelieferte `docker-compose.yml` veröffentlicht bewusst **keinen**
Host-Port — es gibt kein `ports:`, also erreicht nur Traefik das UI über dessen
Docker-Netz. Das `expose:` darin ist reine Dokumentation; für Traefik zählt das
Label `loadbalancer.server.port`.

An der `docker-compose.yml` selbst ist nichts zu ändern, alles steht in der
`.env`:

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `WEBWATCHER_WEB` | `false` | Oberfläche überhaupt starten |
| `WEBWATCHER_HOST` | `webwatcher.localhost` | Domain, unter der sie läuft |
| `WEBWATCHER_PORT` | `8080` | Port im Container — gilt für App **und** Traefik-Label |
| `WEBWATCHER_AUTH` | – | BasicAuth-Hash, siehe unten |
| `TRAEFIK_NETWORK` | `traefik-net` | vorhandenes Docker-Netz des Proxy |
| `TRAEFIK_ENTRYPOINT` | `websecure` | Entrypoint aus deinem Traefik-Compose |
| `TRAEFIK_CERTRESOLVER` | `letsencrypt` | Resolver aus deinem Traefik-Compose |

`WEBWATCHER_PORT` steht absichtlich nur an dieser einen Stelle: die
`config.yaml` liest ihn über `${WEBWATCHER_PORT:-8080}`, das Traefik-Label
ebenso. Sonst zeigt der Router irgendwann auf einen Port, auf dem niemand
lauscht.

```bash
htpasswd -nB frederic          # Hash für die BasicAuth erzeugen
nano .env                      # WEBWATCHER_WEB und WEBWATCHER_AUTH eintragen
docker compose up -d --build
```

Der Hash gehört in **einfache** Anführungszeichen:

```ini
WEBWATCHER_AUTH='frederic:$2y$05$oIxYc0dTbCTFOwqfoTZi5u...'
```

Ohne sie hält Compose `$05$…` für Variablennamen und setzt sie leer ein — der
Hash wird stillschweigend abgeschnitten, und die Anmeldung geht nie, ohne dass
irgendwo ein Fehler auftaucht. Doppelte Anführungszeichen helfen nicht; die
einzige Alternative ist, jedes `$` zu verdoppeln (`$$`). Das betrifft genauso
die `TRAEFIK_DASHBOARD_AUTH` deines Traefik-Compose.

**Zwei Schichten, mit Absicht.** Cloudflare Access allein genügt nicht: Traefik
routet nach `Host`-Header, also kommt

```bash
curl -H "Host: $WEBWATCHER_HOST" https://<server-ip>/ -k
```

am Access-Login vorbei, wenn jemand die Server-IP kennt. Entweder die Firewall
auf die Cloudflare-IP-Bereiche einschränken — oder, einfacher, die BasicAuth
aus dem Compose stehen lassen. Meldet Cloudflare Access an, liest die
Oberfläche zusätzlich `Cf-Access-Authenticated-User-Email` und schreibt bei
jeder Änderung mit, wer sie gemacht hat (`web.user_header` für andere Header).

**Zertifikat per DNS-Challenge.** Der Router nutzt `certresolver: cfdns`, nicht
`letsencrypt`. Sobald der DNS-Eintrag bei Cloudflare proxied ist (orange Wolke,
Voraussetzung für Access), geht die HTTP-01-Challenge durch Cloudflare und wird
unzuverlässig; die DNS-Challenge über `CF_DNS_API_TOKEN` nicht.

An den Timeouts ist nichts zu drehen: Traefiks `writeTimeout` ist per Default
`0`, und die 60 s `readTimeout` gelten für das *Lesen des Requests*. Die langen
Antworten von Picker und Vorschau (30–90 s) laufen also durch.

Ein Nebeneffekt von `traefik-net`: der Container hängt im selben Netz wie die
anderen Dienste hinter dem Proxy, und webwatcher lädt beliebige URLs. Wer die
Oberfläche bedienen kann, kann sie damit auf interne Dienste richten — was
gegenüber dem `js:`-Feld (beliebiger Code) aber keine neue Eskalation ist.

### Ohne Proxy, nur zum Ausprobieren

```bash
webwatcher run --web --web-host 127.0.0.1 --web-port 8080
```

Die vier Reiter im Editor:

| Reiter | wofür |
| --- | --- |
| Einstellungen | alle Optionen als Formular, mit den Voreinstellungen als Platzhalter |
| Auswählen | die Seite wird auf dem Server geladen, ein Klick ins Bild setzt den Selektor |
| Verlauf | die letzten Prüfungen mit Dauer, HTTP-Status und Fehlern |
| Inhalt | der Text, der tatsächlich verglichen wird, plus letzter Screenshot |

**Vorschau prüfen** rendert die Seite zweimal hintereinander und zeigt, was sich
zwischen zwei Läufen von allein ändert — Uhrzeiten, Zähler, wechselnde Banner.
Genau das würde sonst bei jedem Intervall eine Meldung auslösen. Ein Klick
übernimmt die Vorschläge als `ignore_patterns`.

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

## Seite per Klick anlegen: `webwatcher pick`

Statt Selektoren aus den DevTools abzutippen:

```bash
webwatcher pick https://example.com/produkt/123 --name "Shop Produktseite" --interval 5m
```

Es öffnet sich ein sichtbares Chromium – **mit denselben Einstellungen, die der
Watcher später benutzt**. Fahre über die Seite, klicke das Element an, das
beobachtet werden soll, schalte auf „Ignorieren" und klicke Werbung und Banner
weg. Am Ende „Fertig", und die Seite wird gespeichert.

Das braucht einen Bildschirm, läuft also auf deinem Rechner, nicht auf dem
Server. Auf dem Server macht der Reiter **Auswählen** in der
[Weboberfläche](#weboberfläche) dasselbe – nur mit Screenshots statt eines
echten Fensters.

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

Auf dem Server geht dasselbe ohne Profilordner über den Reiter **Auswählen** in
der [Weboberfläche](#weboberfläche) — nur für Seiten hinter einem Login hilft
der lokale Picker mit `--profile` weiter.

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

Zwei Orte, mit klarer Aufteilung:

- **`config.yaml`** — Telegram, Speicher, `defaults:` für alle Seiten und die
  Weboberfläche. Secrets kommen per `${ENV_VAR}` rein. Änderungen hier brauchen
  einen Neustart (`docker compose restart`).
- **Datenbank** — die beobachteten Seiten. Gepflegt über die Weboberfläche oder
  `webwatcher pick`; Änderungen greifen sofort, ohne Neustart.

Jede Option unten steht in beiden Welten zur Verfügung: unter `defaults:` als
Vorgabe für alle Seiten, und pro Seite als Feld im Formular, das die Vorgabe
überschreibt. Auch in der Datenbank bleibt `${ENV_VAR}` unaufgelöst stehen und
wird erst beim Prüfen eingesetzt — ein Token landet also nie im Klartext dort.

Als YAML sieht eine Seite so aus (das erzeugt `import-config` bzw. das Formular):

```yaml
name: Shop Produktseite
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
| `interval_windows` | `[]` | Zeitfenster mit engerem Takt, z.B. jede Minute rund um die Ticketfreigabe (siehe unten) |
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

### Enger takten, wenn etwas passiert

Viele Seiten sind den ganzen Tag langweilig und für zehn Minuten spannend: neue
Tickets kommen nachts um 1 ins System, Rückläufer werden gegen 9 freigegeben.
Dauerhaft jede Minute zu prüfen wäre unhöflich und teuer — also nur dann:

```yaml
defaults:
  interval: 15m                 # der normale Takt
  interval_windows:
    - from: "00:45"             # neue Kontingente
      to: "01:30"
      interval: 1m
    - from: "08:45"             # Rückläufer, nur werktags
      to: "09:30"
      interval: 1m
      days: [mo, di, mi, do, fr]
```

- **Uhrzeiten in Anführungszeichen.** YAML liest ein nacktes `9:30` als Zahl;
  ohne Quotes gibt es dafür eine Fehlermeldung statt eines stillen Fehlers.
- Gilt die **`timezone`** der Seite (Default `Europe/Berlin`), nicht UTC und
  nicht die Serverzeit. Eine Zeitumstellung mitten im Fenster verschiebt es
  nicht.
- **`to` ist exklusiv**, und ein Fenster darf über Mitternacht gehen:
  `from: "23:30"` / `to: "00:15"`. Mit `days` zählt dabei der Starttag — das
  Fenster läuft in den nächsten Tag hinein.
- **`days`** versteht `mo`–`so`, `mon`–`sun` und `montag`–`sonntag`. Ohne
  `days` gilt das Fenster täglich.
- Überlappen sich Fenster, **gewinnt das kürzeste Intervall**.
- Unter `defaults:` gesetzt, gelten die Fenster für alle Seiten. Eine Seite
  kann sie mit einer eigenen Liste ersetzen oder mit `interval_windows: []`
  abschalten.

Der Watcher wacht am Fensteranfang auf, statt den laufenden 15-Minuten-Takt
abzuwarten: Beginnt um 08:45 ein Fenster, ist der nächste Check um 08:45 und
nicht erst um 08:57. `webwatcher list` zeigt den Takt, der gerade gilt, mit
`*` für „kommt aus einem Fenster"; `webwatcher -v list` zeigt alle Fenster.

```
KEY                       INTERVAL  LETZTER CHECK        STATUS
kilmainham-gaol-august          1m* 2026-08-03 09:02:11  ok
```

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

### Hetzner-Cloud-Server wieder bestellbar?

Ist ein Servertyp ausverkauft, sagt die Hetzner Console beim Bestellen nur
„Preselected server type is not available". Dafür braucht es keine Webseite und
keinen Login: `kind: hetzner_stock` fragt die öffentliche Cloud-API, die genau
diese Information pro Standort liefert.

**Token anlegen** — einmalig in der [Hetzner Console](https://console.hetzner.com/):
Projekt öffnen → **Sicherheit** → **API-Tokens** → **API-Token generieren**,
Berechtigung **Lesen**. Der Token wird nur ein einziges Mal angezeigt. Welches
Projekt ist egal, die Verfügbarkeit ist überall gleich. Er läuft nicht ab und
braucht keine 2FA. Mit „Lesen“ kann er nichts bestellen und nichts löschen.
Dann in die `.env`:

```bash
HCLOUD_TOKEN=dein-token
```

und den Container neu erstellen (`docker compose up -d`; ein bloßes `restart`
liest die `.env` nicht neu ein). Der Token bleibt in der Umgebung — er landet
weder in der Datenbank noch im Web-UI.

**Einrichten** — im Web-UI *Neue Seite* → Art `hetzner_stock`, Servertyp `cx53`,
Standorte anhaken. Als Mapping:

```yaml
- name: Hetzner CX53
  kind: hetzner_stock
  server_type: cx53          # wie in der Console, klein oder groß
  locations: [nbg1, fsn1]    # leer = alle Standorte
  interval: 5m
```

Standorte: `nbg1` Nürnberg, `fsn1` Falkenstein, `hel1` Helsinki, `ash` Ashburn,
`hil` Hillsboro, `sin` Singapur.

Verglichen wird eine Zeile pro Standort. Die Meldung kommt ohne Diff, nur der Wechsel steht drin:

```
🟢 CX53 Nürnberg (nbg1) ist wieder bestellbar
Hetzner Console öffnen

CX53 Nürnberg (nbg1): verfügbar
CX53 Falkenstein (fsn1): ausverkauft
```

Wird er wieder ausverkauft, kommt eine 🔴-Nachricht **ohne Ton**. Ein falscher
oder fehlender Token zählt als normaler Fehlversuch und wird nach
`notify_on_error_after` gemeldet. Die API erlaubt 3600 Anfragen pro Stunde und
Projekt, also wird selbst ein `interval: 1m` nicht ausgebremst. Läuft im selben
Projekt viel Automatisierung (Terraform o.ä.), den Token lieber in einem leeren
eigenen Projekt anlegen, damit sich beide das Limit nicht teilen.

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
| `webwatcher pick <url>` | Elemente im Browser anklicken und als Seite speichern |
| `webwatcher run` | Daemon; prüft jede Seite in ihrem Intervall |
| `webwatcher run --web` | Daemon samt Weboberfläche (`--web-host`, `--web-port`) |
| `webwatcher import-config` | `sites:` aus der config.yaml einmalig in die Datenbank holen |
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
