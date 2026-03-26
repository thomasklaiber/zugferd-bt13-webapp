# ZUGFeRD BT-13 Webapp

Eine kleine Webapp, die das Feld **BT-13 (BuyerOrderReference / Bestellnummer)** in den
eingebetteten XML-Teil einer ZUGFeRD/Factur-X PDF-Rechnung einfügt.

## Funktionsweise

1. PDF-Rechnung hochladen (ZUGFeRD 2.x / Factur-X, Profil EN 16931 / EXTENDED)
2. BT-13 Wert (Bestellnummer) eingeben
3. Geänderte PDF herunterladen – die eingebettete XML enthält nun:
   ```xml
   <ram:BuyerOrderReferencedDocument>
     <ram:IssuerAssignedID>IHRE_BESTELLNUMMER</ram:IssuerAssignedID>
   </ram:BuyerOrderReferencedDocument>
   ```
   Das PDF-Sichtbild bleibt unverändert.

## Lokale Entwicklung

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

## Docker (lokal)

```bash
docker build -t zugferd-bt13 .
docker run -p 5000:5000 zugferd-bt13
```

## Deployment auf Hostinger VPS

### Voraussetzungen
1. Hostinger VPS mit Docker & Docker Compose installiert
2. Verzeichnis `/opt/zugferd-bt13/` auf dem VPS anlegen:
   ```bash
   mkdir -p /opt/zugferd-bt13
   # docker-compose.yml dorthin kopieren
   ```
3. GitHub Repository Secrets setzen:
   | Secret | Beschreibung |
   |---|---|
   | `SSH_HOST` | IP-Adresse des Hostinger VPS |
   | `SSH_USER` | SSH-Benutzer (z. B. `root`) |
   | `SSH_KEY` | Privater SSH-Key (PEM-Format) |
   | `SSH_PORT` | SSH-Port (Standard: `22`) |

4. Bei jedem Push auf `main` baut GitHub Actions das Image, pusht es nach GHCR
   und deployt es automatisch auf dem VPS.

### Nginx Reverse Proxy (empfohlen)

```nginx
server {
    listen 80;
    server_name ihre-domain.de;

    location / {
        proxy_pass http://localhost:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 50M;
    }
}
```
