from flask import Flask, request, send_file, render_template, jsonify
import pikepdf
import io
from lxml import etree

app = Flask(__name__)

NS_RAM = 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100'
NS_RSM = 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100'

ZUGFERD_FILENAMES = ['factur-x.xml', 'zugferd-invoice.xml', 'ZUGFeRD-invoice.xml',
                     'xrechnung.xml', 'invoice.xml']

# Tags that must appear BEFORE BuyerOrderReferencedDocument in ApplicableHeaderTradeAgreement
BEFORE_BT13 = [
    f'{{{NS_RAM}}}SellerTradeParty',
    f'{{{NS_RAM}}}BuyerTradeParty',
    f'{{{NS_RAM}}}BuyerRequisitionerTradeParty',
    f'{{{NS_RAM}}}SellerTaxRepresentativeTradeParty',
    f'{{{NS_RAM}}}SellerOrderReferencedDocument',
]


def insert_bt13(xml_bytes: bytes, bt13_value: str) -> bytes:
    root = etree.fromstring(xml_bytes)

    ta = root.find(f'''.//{{{NS_RAM}}}ApplicableHeaderTradeAgreement''')
    if ta is None:
        raise ValueError('Element ApplicableHeaderTradeAgreement nicht gefunden im XML')

    existing = ta.find(f'{{{NS_RAM}}}BuyerOrderReferencedDocument')
    if existing is not None:
        id_el = existing.find(f'{{{NS_RAM}}}IssuerAssignedID')
        if id_el is not None:
            id_el.text = bt13_value
        else:
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
            elif child.tag == f'{{{NS_RAM}}}ContractReferencedDocument':
                break

        ta.insert(insert_idx, buyer_order)

    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=False)


def process_pdf(pdf_bytes: bytes, bt13_value: str) -> bytes:
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))

    try:
        ef_names = pdf.Root.Names.EmbeddedFiles.Names
    except AttributeError:
        raise ValueError('Keine eingebetteten Dateien (EmbeddedFiles) in der PDF gefunden.')

    modified = False
    for i in range(0, len(ef_names), 2):
        filename = str(ef_names[i])
        file_spec = ef_names[i + 1]

        is_zugferd = (
            any(fn.lower() == filename.lower() for fn in ZUGFERD_FILENAMES)
            or filename.lower().endswith('.xml')
        )
        if not is_zugferd:
            continue

        try:
            ef_stream = file_spec.EF.F
            xml_bytes = ef_stream.read_bytes()
            modified_xml = insert_bt13(xml_bytes, bt13_value)
            ef_stream.write(modified_xml, filter=pikepdf.Name.FlateDecode)
            modified = True
            break
        except Exception as e:
            raise ValueError(f'Fehler beim Verarbeiten der XML ({filename}): {e}')

    if not modified:
        raise ValueError(
            'Keine ZUGFeRD/Factur-X XML-Einbettung gefunden. '            'Bitte prüfen Sie, ob die PDF eine eingebettete XML-Datei enthält.'
        )

    output = io.BytesIO()
    pdf.save(output)
    return output.getvalue()


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
        return jsonify({'error': 'Nur PDF-Dateien sind erlaubt.'}), 400

    if not bt13_value:
        return jsonify({'error': 'BT-13 Wert (Bestellnummer) darf nicht leer sein.'}), 400

    try:
        pdf_bytes = pdf_file.read()
        result_bytes = process_pdf(pdf_bytes, bt13_value)
        out_name = pdf_file.filename.rsplit('.', 1)[0] + '_bt13.pdf'
        return send_file(
            io.BytesIO(result_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=out_name,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    except Exception as e:
        return jsonify({'error': f'Unbekannter Fehler: {e}'}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
