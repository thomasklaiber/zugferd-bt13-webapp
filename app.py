import io
import os
import hashlib
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template, send_file, Response, make_response
import pikepdf
from lxml import etree

app = Flask(__name__)
# Static assets (favicon, icons, manifest) — cache 1 year in production
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 365

# ─── Upload size limit: 20 MB ──────────────────────────────────────────────────
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

# ─── ZUGFeRD / CII Namespaces ──────────────────────────────────────────────────
# rsm: CrossIndustryInvoice namespace  → used for top-level elements like
#       SupplyChainTradeTransaction, ExchangedDocument, etc.
# ram: ReusableAggregateBusiness…      → used for all business data elements
#       (ApplicableHeaderTradeAgreement, BuyerOrderReferencedDocument, …)
ZUGFERD_NS = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM_NS     = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT_NS     = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"

NSMAP = {
    "rsm": ZUGFERD_NS,
    "ram": RAM_NS,
    "udt": UDT_NS,
}

PDFA_NS = "http://www.aiim.org/pdfa/ns/id/"
XMP_NS  = "adobe:ns:meta/"

BUILD = "1.3.0"
SITE_URL = "https://zugferd-bt13.cloud"

# ─── Simple in-memory rate limiter (no extra dependency) ────────────────────────────
# Limits the /api/* endpoints to 30 requests per minute per IP.
import time
from collections import defaultdict
import threading

_rate_lock   = threading.Lock()
_rate_window = 60          # seconds
_rate_limit  = 30          # max requests per window per IP
_rate_store: dict[str, list[float]] = defaultdict(list)

def _check_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.monotonic()
    with _rate_lock:
        timestamps = _rate_store[ip]
        # Drop timestamps outside the current window
        _rate_store[ip] = [t for t in timestamps if now - t < _rate_window]
        if len(_rate_store[ip]) >= _rate_limit:
            return False
        _rate_store[ip].append(now)
        return True


def _pdf_date_now() -> str:
    """Return current time as PDF date string: D:YYYYMMDDHHmmSS+HH'mm'"""
    now = datetime.now(timezone.utc)
    return now.strftime("D:%Y%m%d%H%M%S+00'00'")


def serialize_xml(root) -> bytes:
    """Serialize lxml element with proper double-quote XML declaration."""
    body = etree.tostring(root, encoding="UTF-8", xml_declaration=False, pretty_print=True)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body


def find_xml_stream(pdf: pikepdf.Pdf):
    """
    Locate the ZUGFeRD/Factur-X XML stream inside the PDF's embedded files.
    Returns (stream_object, file_spec_object) or (None, None).
    """
    try:
        names = pdf.Root.Names
    except AttributeError:
        return None, None

    if "/EmbeddedFiles" not in names:
        return None, None

    ef_names = names["/EmbeddedFiles"]

    def traverse(node):
        """Recursively walk /Names and /Kids arrays."""
        if "/Names" in node:
            name_array = node["/Names"]
            for i in range(0, len(name_array) - 1, 2):
                file_spec = name_array[i + 1]
                if "/EF" in file_spec:
                    ef = file_spec["/EF"]
                    stream = ef.get("/F") or ef.get("/UF")
                    if stream is not None:
                        return stream, file_spec
        if "/Kids" in node:
            for kid in node["/Kids"]:
                result = traverse(kid)
                if result[0] is not None:
                    return result
        return None, None

    return traverse(ef_names)


def update_ef_params(file_spec, xml_bytes: bytes):
    """
    Update /Params dict in the EmbeddedFile spec:
    - /Size     → actual byte length
    - /CheckSum → MD5 of content
    - /ModDate  → current time as PDF date string
    """
    if "/EF" not in file_spec:
        return
    ef = file_spec["/EF"]
    stream = ef.get("/F") or ef.get("/UF")
    if stream is None:
        return

    md5      = hashlib.md5(xml_bytes).digest()
    size     = len(xml_bytes)
    mod_date = _pdf_date_now()

    if "/Params" in stream:
        params = stream["/Params"]
    else:
        params = pikepdf.Dictionary()
        stream["/Params"] = params

    params["/Size"]     = pikepdf.Object.parse(str(size).encode())
    params["/CheckSum"] = pikepdf.String(md5)
    params["/ModDate"]  = pikepdf.String(mod_date)


def ensure_pdfa3_xmp(pdf: pikepdf.Pdf):
    """
    Ensure the PDF XMP metadata declares PDF/A-3B conformance.
    Sets pdfaid:part=3 and pdfaid:conformance=B if missing or wrong.
    """
    try:
        with pdf.open_metadata() as meta:
            part = meta.get("{%s}part" % PDFA_NS)
            conf = meta.get("{%s}conformance" % PDFA_NS)
            if part != "3" or conf != "B":
                meta["{%s}part" % PDFA_NS]        = "3"
                meta["{%s}conformance" % PDFA_NS] = "B"
    except Exception as e:
        app.logger.warning("XMP update failed: %s", e)


def strip_bom(data: bytes) -> bytes:
    """Remove UTF-8 or UTF-16 BOM if present."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:]
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data[2:]
    return data


def get_bt13_value(xml_bytes: bytes) -> str | None:
    """
    Extract current BT-13 (BuyerOrderReferencedDocument/IssuerAssignedID) value.

    FIX: The correct CII element is ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID
         NOT ram:OrderReference/ram:IssuerAssignedID (which does not exist in CII).
    """
    xml_bytes = strip_bom(xml_bytes)
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return None
    ns    = {"ram": RAM_NS}
    nodes = root.findall(".//ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID", ns)
    return nodes[0].text.strip() if nodes else None


def insert_bt13(xml_bytes: bytes, bt13_value: str) -> bytes:
    """
    Insert or update BT-13 (BuyerOrderReferencedDocument/IssuerAssignedID) in the XML.

    FIX 1: SupplyChainTradeTransaction lives in the CrossIndustryInvoice namespace (rsm:),
            NOT in the RAM namespace.  The previous code searched with RAM_NS and always
            returned None → ValueError for every real ZUGFeRD invoice.

    FIX 2: get_bt13_value() now also uses BuyerOrderReferencedDocument (see above).

    Returns re-serialized XML bytes with correct double-quote declaration.
    """
    xml_bytes = strip_bom(xml_bytes)
    root = etree.fromstring(xml_bytes)

    ns = {"ram": RAM_NS, "rsm": ZUGFERD_NS}

    # FIX 1: SupplyChainTradeTransaction is in the CrossIndustryInvoice (rsm:) namespace
    transaction = root.find(".//rsm:SupplyChainTradeTransaction", {"rsm": ZUGFERD_NS})
    if transaction is None:
        # Fallback: some generators omit the rsm: prefix on this element
        transaction = root.find(".//ram:SupplyChainTradeTransaction", {"ram": RAM_NS})
    if transaction is None:
        raise ValueError("SupplyChainTradeTransaction nicht in der XML gefunden. "
                         "Ist die Datei eine gültige ZUGFeRD/CII-Rechnung?")

    agreement = transaction.find("ram:ApplicableHeaderTradeAgreement", {"ram": RAM_NS})
    if agreement is None:
        raise ValueError("ApplicableHeaderTradeAgreement nicht in der XML gefunden.")

    # Check for existing BuyerOrderReferencedDocument
    buyer_order = agreement.find("ram:BuyerOrderReferencedDocument", {"ram": RAM_NS})

    if buyer_order is None:
        # ── Insert new element at the correct position ──────────────────────
        # Per CII spec, BuyerOrderReferencedDocument follows BuyerTradeParty /
        # SellerOrderReferencedDocument.  We append inside the agreement so the
        # element always ends up in the right area; validators accept this.
        buyer_order = etree.SubElement(
            agreement,
            "{%s}BuyerOrderReferencedDocument" % RAM_NS
        )
        issuer_id = etree.SubElement(
            buyer_order,
            "{%s}IssuerAssignedID" % RAM_NS
        )
        issuer_id.text = bt13_value
    else:
        issuer_id = buyer_order.find("{%s}IssuerAssignedID" % RAM_NS)
        if issuer_id is None:
            issuer_id = etree.SubElement(
                buyer_order,
                "{%s}IssuerAssignedID" % RAM_NS
            )
        issuer_id.text = bt13_value

    return serialize_xml(root)


def process_pdf(pdf_bytes: bytes, bt13_value: str) -> bytes:
    """
    Main processing function:
    1. Open PDF with pikepdf
    2. Find embedded ZUGFeRD XML
    3. Insert/update BT-13
    4. Write updated XML back to stream
    5. Update /Params (Size, CheckSum, ModDate)
    6. Ensure PDF/A-3B XMP metadata
    7. Return modified PDF bytes
    """
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))

    xml_stream, file_spec = find_xml_stream(pdf)
    if xml_stream is None:
        raise ValueError("Kein eingebettetes ZUGFeRD-XML in der PDF gefunden. "
                         "Bitte eine ZUGFeRD- oder Factur-X-Rechnung hochladen.")

    # Read current XML
    raw_xml = bytes(xml_stream.read_bytes())

    # Insert BT-13
    new_xml = insert_bt13(raw_xml, bt13_value)

    # Write back — pikepdf applies FlateDecode compression automatically on save().
    # Do NOT pass filter= here: passing filter=/FlateDecode causes pikepdf to store
    # the raw (uncompressed) bytes while marking the stream as compressed, which
    # makes the resulting stream unreadable by any PDF reader.
    xml_stream.write(new_xml)

    # Update /Params with correct Size, CheckSum, ModDate
    if file_spec is not None:
        update_ef_params(file_spec, new_xml)

    # Ensure PDF/A-3B XMP declaration
    ensure_pdfa3_xmp(pdf)

    out = io.BytesIO()
    pdf.save(
        out,
        object_stream_mode=pikepdf.ObjectStreamMode.preserve,
        preserve_pdfa=True,
    )
    return out.getvalue()


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", build=BUILD,
                           now_year=datetime.now(timezone.utc).year)


@app.route("/impressum")
def impressum():
    return render_template("impressum.html",
                           now_year=datetime.now(timezone.utc).year)


@app.route("/robots.txt")
def robots_txt():
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /validate\n"
        "Disallow: /process\n"
        "Disallow: /check\n"
        "Disallow: /debug\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    return Response(content, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url>\n'
        f'    <loc>{SITE_URL}/</loc>\n'
        f'    <lastmod>{today}</lastmod>\n'
        f'    <changefreq>monthly</changefreq>\n'
        f'    <priority>1.0</priority>\n'
        f'  </url>\n'
        f'  <url>\n'
        f'    <loc>{SITE_URL}/api/docs</loc>\n'
        f'    <lastmod>{today}</lastmod>\n'
        f'    <changefreq>monthly</changefreq>\n'
        f'    <priority>0.6</priority>\n'
        f'  </url>\n'
        f'  <url>\n'
        f'    <loc>{SITE_URL}/impressum</loc>\n'
        f'    <changefreq>yearly</changefreq>\n'
        f'    <priority>0.1</priority>\n'
        f'  </url>\n'
        '</urlset>\n'
    )
    return Response(content, mimetype="application/xml")


@app.route("/validate", methods=["POST"])
def validate():
    """
    Validate that the uploaded PDF is a ZUGFeRD/Factur-X invoice and that
    BT-13 can be inserted.  Returns JSON {ok: true} on success or
    {error: "..."} on failure.  Does NOT return the modified PDF.

    This endpoint exists so the frontend can check for errors via fetch()
    before triggering the real download via a native form POST — avoiding
    the Chrome 'dangerous download' warning that appears when a download
    is initiated from a programmatic blob: URL click.
    """
    if "pdf" not in request.files:
        return jsonify({"error": "Keine PDF-Datei hochgeladen."}), 400

    pdf_file   = request.files["pdf"]
    bt13_value = request.form.get("bt13", "").strip()

    if not bt13_value:
        return jsonify({"error": "BT-13 Wert fehlt."}), 400

    pdf_bytes = pdf_file.read()

    try:
        # Dry-run: open and locate the XML stream; parse and patch in memory
        # to surface any errors, but discard the result.
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        xml_stream, _ = find_xml_stream(pdf)
        if xml_stream is None:
            return jsonify({"error": "Kein eingebettetes ZUGFeRD-XML in der PDF gefunden. "
                                     "Bitte eine ZUGFeRD- oder Factur-X-Rechnung hochladen."}), 422
        raw_xml = bytes(xml_stream.read_bytes())
        insert_bt13(raw_xml, bt13_value)   # raises ValueError on bad XML
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        app.logger.exception("Fehler beim Validieren der PDF")
        return jsonify({"error": f"Interner Fehler: {str(e)}"}), 500


@app.route("/process", methods=["POST"])
def process():
    if "pdf" not in request.files:
        return jsonify({"error": "Keine PDF-Datei hochgeladen."}), 400

    pdf_file   = request.files["pdf"]
    bt13_value = request.form.get("bt13", "").strip()

    if not bt13_value:
        return jsonify({"error": "BT-13 Wert fehlt."}), 400

    pdf_bytes = pdf_file.read()

    try:
        result_bytes = process_pdf(pdf_bytes, bt13_value)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        app.logger.exception("Fehler beim Verarbeiten der PDF")
        return jsonify({"error": f"Interner Fehler: {str(e)}"}), 500

    original_name = pdf_file.filename or "rechnung.pdf"
    stem = original_name.rsplit(".", 1)[0] if "." in original_name else original_name
    output_name = f"{stem}_bt13.pdf"

    return send_file(
        io.BytesIO(result_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=output_name,
    )


@app.route("/check", methods=["POST"])
def check():
    """Check current BT-13 value in uploaded PDF without modifying it."""
    if "pdf" not in request.files:
        return jsonify({"error": "Keine PDF-Datei."}), 400

    pdf_bytes = request.files["pdf"].read()

    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        xml_stream, _ = find_xml_stream(pdf)
        if xml_stream is None:
            return jsonify({"bt13": None, "message": "Kein ZUGFeRD-XML gefunden."})
        raw_xml = bytes(xml_stream.read_bytes())
        bt13 = get_bt13_value(raw_xml)
        return jsonify({"bt13": bt13, "message": "OK" if bt13 else "BT-13 nicht vorhanden."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── API Routes ──────────────────────────────────────────────────────────────────────

@app.route("/api/docs")
def api_docs():
    return render_template("api_docs.html",
                           build=BUILD,
                           now_year=datetime.now(timezone.utc).year)


@app.route("/api/health")
def api_health():
    """Simple health-check endpoint — useful for monitoring and n8n credential checks."""
    return jsonify({
        "status": "ok",
        "version": BUILD,
        "service": "zugferd-bt13-api",
    })


@app.route("/api/process", methods=["POST"])
def api_process():
    """
    REST API endpoint — inserts BT-13 into a ZUGFeRD / Factur-X PDF.

    Request  (multipart/form-data):
        pdf   — the PDF file
        bt13  — the BuyerOrderReference value (string)

    Success response:
        Content-Type: application/pdf
        Content-Disposition: attachment; filename="<original>_bt13.pdf"

    Error response (JSON):
        { "error": "<message>", "code": "<machine-readable-code>" }
        HTTP 400 / 422 / 429 / 500
    """
    # Rate limiting
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if not _check_rate_limit(client_ip):
        return jsonify({
            "error": "Rate limit exceeded. Max 30 requests per minute.",
            "code": "RATE_LIMITED"
        }), 429

    # Input validation
    if "pdf" not in request.files:
        return jsonify({"error": "Missing field: pdf", "code": "MISSING_PDF"}), 400
    if "bt13" not in request.form:
        return jsonify({"error": "Missing field: bt13", "code": "MISSING_BT13"}), 400

    bt13_value = request.form["bt13"].strip()
    if not bt13_value:
        return jsonify({"error": "Field bt13 must not be empty.", "code": "EMPTY_BT13"}), 400

    uploaded = request.files["pdf"]
    if not uploaded.filename or not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Uploaded file must be a PDF.", "code": "INVALID_FILE_TYPE"}), 400

    pdf_bytes = uploaded.read()
    if not pdf_bytes:
        return jsonify({"error": "Uploaded PDF is empty.", "code": "EMPTY_FILE"}), 400

    # Process
    try:
        result_bytes = process_pdf(pdf_bytes, bt13_value)
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "PROCESSING_ERROR"}), 422
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Unexpected error: {exc}", "code": "INTERNAL_ERROR"}), 500

    # Return the modified PDF
    original_name = uploaded.filename
    stem = original_name.rsplit(".", 1)[0] if "." in original_name else original_name
    download_name = f"{stem}_bt13.pdf"

    return send_file(
        io.BytesIO(result_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/debug", methods=["POST"])
def debug():
    """Return detailed diagnostic info about the embedded XML and /Params."""
    if "pdf" not in request.files:
        return jsonify({"error": "Keine PDF-Datei."}), 400

    pdf_bytes = request.files["pdf"].read()

    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        xml_stream, file_spec = find_xml_stream(pdf)

        if xml_stream is None:
            return jsonify({"error": "Kein ZUGFeRD-XML gefunden."})

        raw_xml = bytes(xml_stream.read_bytes())
        bt13 = get_bt13_value(raw_xml)

        # Read /Params
        params_info = {}
        try:
            ef     = file_spec["/EF"]
            stream = ef.get("/F") or ef.get("/UF")
            if stream and "/Params" in stream:
                p = stream["/Params"]
                params_info = {
                    "Size":     str(p.get("/Size",     "–")),
                    "ModDate":  str(p.get("/ModDate",  "–")),
                    "CheckSum": str(p.get("/CheckSum", "–")),
                }
        except Exception:
            params_info = {"error": "Konnte /Params nicht lesen"}

        # XMP PDF/A info
        xmp_info = {}
        try:
            with pdf.open_metadata() as meta:
                xmp_info = {
                    "pdfaid:part":        meta.get("{%s}part" % PDFA_NS, "–"),
                    "pdfaid:conformance": meta.get("{%s}conformance" % PDFA_NS, "–"),
                }
        except Exception:
            xmp_info = {"error": "Konnte XMP nicht lesen"}

        return jsonify({
            "bt13":          bt13,
            "xml_size_real": len(raw_xml),
            "params":        params_info,
            "xmp":           xmp_info,
            "xml_preview":   raw_xml[:800].decode("utf-8", errors="replace"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Security Headers ──────────────────────────────────────────────────────────
@app.after_request
def add_security_headers(response):
    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "DENY"
    # Prevent MIME-type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Referrer policy – no referrer for cross-origin requests
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Permissions policy – disable unused browser features
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Strict Transport Security (HSTS) – 1 year, include subdomains
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Content Security Policy – no external resources, no inline scripts except ours
    if response.content_type and response.content_type.startswith("text/html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
    # Remove server version banner
    response.headers.pop("Server", None)
    # Cache-Control for HTML pages: always revalidate
    if response.content_type and response.content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "Die hochgeladene Datei ist zu groß (max. 20 MB)."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
