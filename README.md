# ZUGFeRD BT-13 Tool

Web-App zum Einfügen des Feldes **BT-13 (BuyerOrderReference / Bestellreferenz)** in bestehende ZUGFeRD- oder Factur-X-Rechnungen.

> Nur der eingebettete XML-Teil der PDF wird geändert. Das sichtbare PDF-Layout bleibt unverändert.

---

## Funktionen

| Endpunkt   | Methode | Beschreibung                                           |
|------------|---------|--------------------------------------------------------|
| `/`        | GET     | Web-Oberfläche (Drag & Drop)                           |
| `/process` | POST    | PDF hochladen → BT-13 einfügen → modifizierte PDF laden |
| `/check`   | POST    | Aktuellen BT-13-Wert auslesen (ohne Änderung)          |
| `/debug`   | POST    | Detailinfos: XML-Vorschau, /Params, XMP PDF/A-Status   |

---

## Lokaler Betrieb

### Voraussetzungen
- Python 3.12+
- `libqpdf-dev` (Ubuntu/Debian) oder `qpdf` (macOS via Homebrew)

```bash
pip install -r requirements.txt
python app.py
```

Aufruf: http://localhost:5000

---

## Docker (lokal)

```bash
docker compose up --build
```

---

## Deployment auf Hostinger VPS

### Einmalige VPS-Vorbereitung

```bash
# Docker installieren (falls noch nicht vorhanden)
curl -fsSL https://get.docker.com | sh

# Projektverzeichnis anlegen
mkdir -p /opt/zugferd-bt13
cd /opt/zugferd-bt13

# docker-compose.yml mit der richtigen Image-URL hochladen
# Ersetze GITHUB_USER und REPO_NAME:
cat > docker-compose.yml << 'EOF'
services:
  app:
    image: ghcr.io/GITHUB_USER/zugferd-bt13-webapp:latest
    ports:
      - "5000:5000"
    restart: unless-stopped
    environment:
      - FLASK_ENV=production
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
EOF
```

### GitHub Secrets setzen

Im GitHub-Repository unter **Settings → Secrets and variables → Actions**:

| Secret      | Wert                              |
|-------------|-----------------------------------|
| `SSH_HOST`  | IP-Adresse des VPS                |
| `SSH_USER`  | SSH-Benutzername (z. B. `root`)   |
| `SSH_KEY`   | Privater SSH-Key (PEM-Format)     |
| `SSH_PORT`  | SSH-Port (Standard: `22`)         |

### Manuelles erstes Deployment

```bash
# Auf dem VPS:
cd /opt/zugferd-bt13

# GHCR einloggen (einmalig, mit GitHub Personal Access Token)
echo "DEIN_GITHUB_PAT" | docker login ghcr.io -u GITHUB_USER --password-stdin

docker pull ghcr.io/GITHUB_USER/zugferd-bt13-webapp:latest
docker compose up -d
```

Ab jetzt deployed jeder `git push` auf `main` automatisch via GitHub Actions.

---

## Nginx Reverse Proxy (optional)

```nginx
server {
    listen 80;
    server_name erechnung.deinedomain.tld;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        client_max_body_size 20M;
    }
}
```

HTTPS mit Let's Encrypt:
```bash
certbot --nginx -d erechnung.deinedomain.tld
```

---

## Technische Details

- **Backend:** Python 3.12, Flask, pikepdf, lxml
- **XML-Patching:** `lxml` mit korrektem Namespace-Binding (`rsm:` für CII-Elemente, `ram:` für Geschäftsdaten)
- **PDF-Struktur:** `pikepdf` liest/schreibt den EmbeddedFiles-Baum ohne das sichtbare PDF anzufassen
- **PDF/A-Konformität:** XMP-Metadaten werden auf PDF/A-3B geprüft und ggf. korrigiert

---

## Changelog

### v1.0.0
- **Bugfix:** `get_bt13_value()` suchte fälschlich nach `ram:OrderReference` — korrekt ist `ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID`
- **Bugfix:** `insert_bt13()` suchte `SupplyChainTradeTransaction` im RAM-Namespace — korrekt ist der RSM-Namespace (CrossIndustryInvoice)
- **Bugfix:** `xml_stream.write()` mit explizitem `filter=/FlateDecode` erzeugte ungültige Streams — pikepdf komprimiert automatisch beim `save()`
- **Neu:** Upload-Größenlimit 20 MB mit sprechendem Fehler (HTTP 413)
- **Neu:** Non-root User im Docker-Container
- **Neu:** GitHub Actions CI/CD Workflow (`.github/workflows/deploy.yml`)
- **Neu:** `.dockerignore`
