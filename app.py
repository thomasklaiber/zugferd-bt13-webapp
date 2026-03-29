from flask import Flask, request, send_file, render_template, jsonify
import pikepdf
import io
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
    """Gibt (filename, stream) des eingebetteten XML zurück."""
    try:
        ef_root = pdf.Root.Names.EmbeddedFiles
    except AttributeError:
        raise ValueError('Keine EmbeddedFiles im PDF gefunden. '
                         'Handelt es sich um eine ZUGFeRD/Factur-X Rechnung?')

    pairs = collect_ef_pairs(ef_root)
    if not pairs:
        raise ValueError('EmbeddedFiles-Namensbaum ist leer.')

    xml_pairs = [(fn, fs) for fn, fs in pairs if fn.lower().endswith('.xml')]
    if not xml_pairs:
        names_found = [fn for fn, _ in pairs]
        raise ValueError(f'Keine XML-Datei eingebettet. Gefundene Dateien: {names_found}')

    preferred = ['factur-x.xml', 'zugferd-invoice.xml', 'xrechnung.xml']
    for pref in preferred:
        for fn, fs in xml_pairs:
            if fn.lower() == pref.lower():
                return fn, _get_stream(fs)

    fn, fs = xml_pairs[0]
    return fn, _get_stream(fs)


def _get_stream(file_spec):
    for path in (('/EF', '/F'), ('/EF', '/UF')):
        try:
            node = file_spec
            for key in path:
                node = node[key]
            return node
        except (KeyError, TypeError):
            continue
    try:
        return file_spec.EF.F
    except AttributeError:
        raise ValueError('Kein EF/F-Stream im FileSpec.')


def parse_xml(raw: bytes) -> etree._Element:
    # BOM entfernen
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


def insert_bt13(xml_bytes: bytes, bt13_value: str) -> bytes:
    root = parse_xml(xml_bytes)

    ta = root.find(f'.//{{{NS_RAM}}}ApplicableHeaderTradeAgreement')
    if ta is None:
        raise ValueError('ApplicableHeaderTradeAgreement nicht gefunden. '
                         'Bitte prüfen Sie das ZUGFeRD-Profil (EN 16931 / EXTENDED).')

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

    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=False)


def process_pdf(pdf_bytes: bytes, bt13_value: str) -> bytes:
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    filename, stream = find_xml_stream(pdf)
    xml_bytes = stream.read_bytes()
    modified_xml = insert_bt13(xml_bytes, bt13_value)
    stream.write(modified_xml)
    out = io.BytesIO()
    pdf.save(out)
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
    """Diagnose-Endpunkt: zeigt alle eingebetteten Dateien im PDF."""
    if 'pdf' not in request.files:
        return jsonify({'error': 'Keine PDF-Datei'}), 400
    try:
        pdf = pikepdf.open(io.BytesIO(request.files['pdf'].read()))
        ef_root = pdf.Root.Names.EmbeddedFiles
        pairs = collect_ef_pairs(ef_root)
        result = []
        for fn, fs in pairs:
            try:
                s = _get_stream(fs)
                raw = s.read_bytes()
                for bom in (b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff'):
                    if raw.startswith(bom):
                        raw = raw[len(bom):]
                result.append({
                    'filename': fn,
                    'size_bytes': len(raw),
                    'preview': raw[:500].decode('utf-8', errors='replace'),
                })
            except Exception as e:
                result.append({'filename': fn, 'error': str(e)})
        return jsonify({'embedded_files': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
