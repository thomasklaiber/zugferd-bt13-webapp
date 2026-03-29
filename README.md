# ZUGFeRD BT-13 Tool

Webapp zum Einfügen von **BT-13 (BuyerOrderReference / Bestellnummer)** in den
eingebetteten XML-Teil einer ZUGFeRD / Factur-X PDF-Rechnung.

Das Sichtbild der PDF bleibt unverändert – nur die eingebettete CII-XML wird ergänzt:

```xml
<ram:BuyerOrderReferencedDocument>
  <ram:IssuerAssignedID>IHRE_BESTELLNUMMER</ram:IssuerAssignedID>
</ram:BuyerOrderReferencedDocument>
```

## Unterstützte Formate

- ZUGFeRD 2.x (EN 16931 / EXTENDED / COMFORT)
- Factur-X (alle Profile)
- X-Rechnung (CII-Variante mit eingebetteter XML)

## Lokale Entwicklung

```bash
pip install -r requirements.txt
python app.py
# http://localhost:5000
```

## Docker

```bash
docker compose up --build
# http://localhost:5000
```

## Deployment auf Hostinger VPS

### Einmalig auf dem VPS einrichten

```bash
# Repository klonen
git clone https://github.com/DEIN-USER/DEIN-REPO.git /opt/zugferd-bt13
cd /opt/zugferd-bt13

# Ersten Build starten
docker compose up --build -d
```

### GitHub Secrets konfigurieren

| Secret      | Beschreibung                          |
|-------------|---------------------------------------|
| `SSH_HOST`  | IP-Adresse des Hostinger VPS          |
| `SSH_USER`  | SSH-Benutzer (z. B. `root`)           |
| `SSH_KEY`   | Privater SSH-Key (PEM-Format)         |
| `SSH_PORT`  | SSH-Port (Standard: `22`)             |

Jeder Push auf `main` deployt automatisch.

## Diagnose

Falls die XML-Erkennung fehlschlägt, den `/debug`-Endpunkt nutzen:

```bash
curl -X POST http://DEINE-IP:5000/debug \
     -F "pdf=@rechnung.pdf" | python3 -m json.tool
```

Zeigt alle eingebetteten Dateien mit Dateinamen, Größe und XML-Preview.

## Nginx Reverse Proxy (empfohlen)

```nginx
server {
    listen 80;
    server_name ihre-domain.de;

    client_max_body_size 50M;

    location / {
        proxy_pass         http://localhost:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }
}
```
