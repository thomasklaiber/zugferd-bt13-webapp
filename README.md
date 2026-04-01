# ZUGFeRD BT-13 Tool

Web-App zum Einfügen des Feldes **BT-13 (BuyerOrderReference / Bestellreferenz)** in bestehende ZUGFeRD- oder Factur-X-Rechnungen.

> Nur der eingebettete XML-Teil der PDF wird geändert. Das sichtbare PDF-Layout bleibt unverändert.

**Produktiv-URL:** https://zugferd-bt13.srv1528010.hstgr.cloud

---

## Funktionen

| Endpunkt    | Methode | Beschreibung                                             |
|-------------|---------|----------------------------------------------------------|
| `/`         | GET     | Web-Oberfläche (Drag & Drop)                             |
| `/process`  | POST    | PDF hochladen → BT-13 einfügen → modifizierte PDF laden  |
| `/validate` | POST    | PDF prüfen ohne Änderung (JSON: `{ok:true}` oder Fehler) |
| `/check`    | POST    | Aktuellen BT-13-Wert auslesen                            |
| `/debug`    | POST    | Detailinfos: XML-Vorschau, /Params, XMP PDF/A-Status     |

---

## Lokaler Betrieb

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:5000
```

---

## Deployment auf Hostinger Docker Manager

Das Deployment läuft vollautomatisch über GitHub Actions + Hostinger Docker Manager.

### Einmalige Einrichtung (Reihenfolge beachten!)

#### 1. Traefik-Projekt deployen (Voraussetzung)

Im **Hostinger Docker Manager**:
1. „New Project" → Template **Traefik** auswählen
2. E-Mail für Let's Encrypt: `hallo@thomasklaiber.com`
3. Domain: `srv1528010.hstgr.cloud`
4. Deploy klicken

→ Erstellt das Docker-Netzwerk `traefik-proxy` und startet Traefik auf Port 80/443.

#### 2. ZUGFeRD-App deployen

Im **Hostinger Docker Manager**:
1. „New Project" → **Custom** (kein Template)
2. Repository: `ghcr.io/thomasklaiber/zugferd-bt13-webapp:latest`
3. Die `docker-compose.yml` aus diesem Repository verwenden
4. Deploy klicken

→ Die App ist erreichbar unter: **https://zugferd-bt13.srv1528010.hstgr.cloud**

### Automatisches Update bei Code-Änderungen

Bei jedem `git push` auf `main`:
1. GitHub Actions baut das Docker-Image und pushed es nach GHCR
2. Im Hostinger Docker Manager auf **„Redeploy"** klicken (oder Webhook einrichten)

---

## Technische Details

- **Backend:** Python 3.12, Flask, pikepdf, lxml
- **XML-Patching:** lxml mit korrektem Namespace-Binding
  - `rsm:` für CII-Top-Level-Elemente (`SupplyChainTradeTransaction`)
  - `ram:` für Geschäftsdaten (`BuyerOrderReferencedDocument`, `IssuerAssignedID`)
- **PDF-Struktur:** pikepdf liest/schreibt den EmbeddedFiles-Baum ohne das sichtbare PDF anzufassen
- **PDF/A-Konformität:** XMP-Metadaten werden auf PDF/A-3B geprüft und ggf. korrigiert
- **Upload-Limit:** 20 MB

---

## Changelog

### v1.0.0
- **Bugfix:** `get_bt13_value()` suchte fälschlich nach `ram:OrderReference` → korrekt: `ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID`
- **Bugfix:** `insert_bt13()` suchte `SupplyChainTradeTransaction` im RAM-Namespace → korrekt: RSM-Namespace
- **Bugfix:** `xml_stream.write()` mit explizitem `filter=/FlateDecode` erzeugte ungültige Streams → ohne Filter-Argument, pikepdf komprimiert automatisch
- **Bugfix:** Chrome „unsicherer Download"-Warnung: Two-Step-Ansatz mit `/validate` + Form-POST
- **Neu:** Upload-Größenlimit 20 MB
- **Neu:** Non-root User im Dockerfile
- **Neu:** GitHub Actions CI/CD (Build + Push nach GHCR)
- **Neu:** Traefik-Labels für Hostinger Docker Manager (HTTPS, Let's Encrypt, HTTP→HTTPS Redirect)
