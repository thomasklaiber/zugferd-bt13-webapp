from flask import Flask, request, send_file, render_template, jsonify
import pikepdf
import io
import hashlib
from datetime import datetime, timezone
from lxml import etree

app = Flask(__name__)

NS_RAM = 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100'

BEFORE_BT13 = [
    f'{{{NS_RAM}}}SellerTradeParty',
    f'{{{NS_RAM}}}BuyerTradeParty',
    f'{{{NS_RAM}}}BuyerRequisitionerTradeParty',
    f'{{{NS_RAM}}}SellerTaxRepresentativeTradeParty',
    f'{{{NS_RAM}}}SellerOrderReferencedDocument',
]

AFTER_BT13 = [
    f'{{{NS_RAM}}}ContractReferencedDocument',
    f'{{{NS_RAM}}}AdditionalReferencedDocument',
    f'{{{NS_RAM}}}SpecifiedProcuringProject',
]


# ── PDF helper ─────────────────────────────────────────────────────────

def collect_ef_pairs(node):
    """Rekursiv EmbeddedFiles-Namensbaum (flat Array oder B-Tree) einlesen."""
    pairs = []
    if '/Names' in node:
        arr = node['/Names']
        for i in range(0, len(arr) - 1, 2):
            pairs.append((str(arr[i]), arr[i + 1]))
    if '/Kids' in node:
        for kid in node['/Kids']:
            pairs.extend(collect_ef_pairs(kid))
    return pairs


def find_xml_stream(pdf: pikepdf.Pdf):
    """Gibt (filename, file_spec, stream) des eingebetteten ZUGFeRD-XML zurück."""
    try:
        ef_root = pdf.Root.Names.EmbeddedFiles
    except AttributeError:
        raise ValueError(
            'Keine EmbeddedFiles im PDF gefunden. '
            'Bitte prüfen Sie, ob es sich um eine ZUGFeRD/Factur-X Rechnung handelt.'
        )

    pairs = collect_ef_pairs(ef_root)
    if not pairs:
        raise ValueError('EmbeddedFiles-Namensbaum ist leer.')

    xml_pairs = [(fn, fs) for fn, fs in pairs if fn.lower().endswith('.xml')]
    if not xml_pairs:
        raise ValueError(
            f'Keine XML-Datei eingebettet. Gefundene Dateien: {[fn for fn, _ in pairs]}'
        )

    preferred = ['factur-x.xml', 'zugferd-invoice.xml', 'xrechnung.xml']
    for pref in preferred:
        for fn, fs in xml_pairs:
            if fn.lower() == pref.lower():
                return fn, fs, _get_ef_stream(fs)

    fn, fs = xml_pairs[0]
    return fn, fs, _get_ef_stream(fs)


def _get_ef_stream(file_spec):
    for keys in (('/EF', '/F'), ('/EF', '/UF')):
        try:
            node = file_spec
            for k in keys:
                node = node[k]
            return node
        except (KeyError, TypeError):
            continue
    try:
        return file_spec.EF.F
    except AttributeError:
        raise ValueError('Kein EF/F-Stream im FileSpec gefunden.')


def _pdf_date(dt: datetime) -> str:
    """Formatiert Datum als PDF-Datumsstring D:YYYYMMDDHHmmSSOHH'mm'"""
    tz = dt.strftime('%z')
    if tz:
        sign = tz[0]
        h, m = tz[1:3], tz[3:5]
        tz_str = f"{sign}{h}'{m}'"
    else:
        tz_str = "Z"
    return dt.strftime(f"D:%Y%m%d%H%M%S") + tz_str


def update_ef_params(stream, new_content: bytes):
    """
    Aktualisiert /Params (Size, CheckSum, ModDate) nach Änderung des Stream-Inhalts.
    Pflicht für PDF/A-3 Konformität.
    """
    try:
        sd = stream.stream_dict
        if '/Params' not in sd:
            return

        params = sd['/Params']
        now_str = _pdf_date(datetime.now(timezone.utc))

        # /Size = unkomprimierte Bytegröße des Inhalts
        try:
            params['/Size'] = len(new_content)
        except Exception:
            pass

        # /CheckSum = MD5-Hash des unkomprimierten Inhalts
        try:
            params['/CheckSum'] = pikepdf.String(hashlib.md5(new_content).digest())
        except Exception:
            pass

        # /ModDate aktualisieren
        try:
            params['/ModDate'] = pikepdf.String(now_str)
        except Exception:
            pass

    except Exception:
        pass  # /Params-Update ist Best-Effort; fehlschlagen ist besser als Absturz


# ── XML helper ──────────────────────────────────────────────────────────

def parse_xml(raw: bytes) -> etree._Element:
    """Parst XML-Bytes; entfernt BOM und normalisiert Encoding."""
    for bom in (b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff'):
        if raw.startswith(bom):
            raw = raw[len(bom):]
            break
    try:
        return etree.fromstring(raw)
    except etree.XMLSyntaxError as e:
        try:
            return etree.fromstring(raw.decode('utf-8', errors='replace').encode('utf-8'))
        except Exception:
            raise ValueError(f'XML-Parsing fehlgeschlagen: {e}')


def serialize_xml(root: etree._Element) -> bytes:
    """
    Serialisiert zurück zu bytes.
    Wichtig: encoding='unicode' + manuelle Deklaration mit DOPPELTEN Anführungszeichen.
    lxml's xml_declaration=True verwendet einfache Anführungszeichen — das verletzt
    einige strikte ZUGFeRD-Validatoren.
    """
    body = etree.tostring(root, encoding='unicode', xml_declaration=False, pretty_print=False)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body.encode('UTF-8')


def insert_bt13(xml_bytes: bytes, bt13_value: str) -> bytes:
    root = parse_xml(xml_bytes)

    ta = root.find(f'.//{{{NS_RAM}}}ApplicableHeaderTradeAgreement')
    if ta is None:
        raise ValueError(
            'ApplicableHeaderTradeAgreement nicht gefunden. '
            'Bitte prüfen Sie das ZUGFeRD-Profil (mind. EN 16931 / EXTENDED).'
        )

    existing = ta.find(f'{{{NS_RAM}}}BuyerOrderReferencedDocument')
    if existing is not None:
        id_el = existing.find(f'{{{NS_RAM}}}IssuerAssignedID')
        if id_el is None:
            id_el = etree.SubElement(existing, f'{{{NS_RAM}}}IssuerAssignedID')
        id_el.text = bt13_value
    else:
        buyer_order = etree.Element(f'{{{NS_RAM}}}BuyerOrderReferencedDocument')
        issuer_id = etree.SubElement(buyer_order, f'{{{NS_RAM}}}IssuerAssignedID')
        issuer_id.text = bt13_value

        insert_idx = 0
        for i, child in enumerate(list(ta)):
            if child.tag in BEFORE_BT13:
                insert_idx = i + 1
            elif child.tag in AFTER_BT13:
                break

        ta.insert(insert_idx, buyer_order)

    return serialize_xml(root)


# ── Core processor ──────────────────────────────────────────────────────

def process_pdf(pdf_bytes: bytes, bt13_value: str) -> bytes:
    # preserve_pdfa=True (pikepdf default) hält PDF/A-Konformitätsmarkierungen
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))

    filename, file_spec, stream = find_xml_stream(pdf)
    xml_bytes = stream.read_bytes()
    modified_xml = insert_bt13(xml_bytes, bt13_value)

    # Stream-Inhalt schreiben
    stream.write(modified_xml)

    # /Params aktualisieren — KRITISCH für PDF/A-3-Konformität
    update_ef_params(stream, modified_xml)

    out = io.BytesIO()
    # object_stream_mode=preserve: minimiert strukturelle Änderungen am PDF
    # preserve_pdfa=True (default): behält XMP-/PDF-A-Metadaten
    pdf.save(out, object_stream_mode=pikepdf.ObjectStreamMode.preserve)
    return out.getvalue()


# ── Routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/process', methods=['POST'])
def process():
    if 'pdf' not in request.files:
        return jsonify({'error': 'Keine PDF-Datei hochgeladen.'}), 400

    pdf_file = request.files['pdf']
    bt13_value = request.form.get('bt13', '').strip()

    if not pdf_file.filename or not pdf_file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Nur PDF-Dateien (.pdf) sind erlaubt.'}), 400

    if not bt13_value:
        return jsonify({'error': 'BT-13 Bestellnummer darf nicht leer sein.'}), 400

    try:
        result = process_pdf(pdf_file.read(), bt13_value)
        out_name = pdf_file.filename.rsplit('.', 1)[0] + '_bt13.pdf'
        return send_file(
            io.BytesIO(result),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=out_name,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    except Exception as e:
        return jsonify({'error': f'Unbekannter Fehler: {e}'}), 500


@app.route('/debug', methods=['POST'])
def debug():
    """Diagnose: zeigt alle eingebetteten Dateien mit XML-Preview und /Params-Inhalt."""
    if 'pdf' not in request.files:
        return jsonify({'error': 'Keine PDF-Datei'}), 400
    try:
        pdf = pikepdf.open(io.BytesIO(request.files['pdf'].read()))
        ef_root = pdf.Root.Names.EmbeddedFiles
        pairs = collect_ef_pairs(ef_root)
        result = []
        for fn, fs in pairs:
            entry = {'filename': fn}
            try:
                s = _get_ef_stream(fs)
                raw = s.read_bytes()
                for bom in (b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff'):
                    if raw.startswith(bom):
                        raw = raw[len(bom):]
                entry['size_bytes'] = len(raw)
                entry['preview'] = raw[:300].decode('utf-8', errors='replace')
                # /Params auslesen
                if '/Params' in s.stream_dict:
                    p = s.stream_dict['/Params']
                    entry['params'] = {
                        'Size': int(p['/Size']) if '/Size' in p else None,
                        'CheckSum': bytes(p['/CheckSum']).hex() if '/CheckSum' in p else None,
                        'ModDate': str(p['/ModDate']) if '/ModDate' in p else None,
                    }
            except Exception as e:
                entry['error'] = str(e)
            result.append(entry)
        return jsonify({'embedded_files': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
