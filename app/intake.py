"""Intake: .eml -> stored attachments -> unpacked archives -> pages.

Every file is stored by content hash. Zips are unpacked with a parent link, and a file
whose hash was already seen in this submission is marked DUPLICATE rather than processed twice.
"""
import email
import hashlib
import io
import mimetypes
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree
from email import policy
from pathlib import Path, PurePosixPath

import openpyxl
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageOps

from app.config import BLOB_DIR, PAGE_DIR, data_path, stored_path
from app.db import Document, Page, Submission, audit
from app.log import get_logger

log = get_logger(__name__)

MIN_TEXT_CHARS = 40          # below this a PDF page is treated as image-only
RENDER_SCALE = 150 / 72      # 150 dpi page images for the vision model and the UI
MAX_ZIP_DEPTH = 2
MAX_ZIP_MEMBERS = 200


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_blob(data: bytes, filename: str) -> Path:
    digest = sha256(data)
    path = BLOB_DIR / digest[:2] / f"{digest}{Path(filename).suffix.lower()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return path


def ingest_eml(db, raw: bytes) -> Submission:
    msg = email.message_from_bytes(raw, policy=policy.default)
    sub = Submission(sender=str(msg.get("From", "")), subject=str(msg.get("Subject", "")),
                     email_sha256=sha256(raw), status="RECEIVED")
    db.add(sub)
    db.flush()
    store_blob(raw, "message.eml")
    audit(db, sub.id, "system", "submission.received", f"submission:{sub.id}",
          after={"sender": sub.sender, "subject": sub.subject, "email_sha256": sub.email_sha256})

    count = _email_parts(db, sub, msg, None, {}, depth=0, body_name=EMAIL_BODY_NAME)
    log.info("email ingested", submission_id=sub.id, attachments=count)
    return sub


def _email_parts(db, sub, msg, parent, seen, depth, body_name) -> int:
    """An email's body (what the agent typed, read and classified like any text page) and its attachments.
    Used for the submission's own email and for emails found inside it."""
    body = email_body(msg)
    if body:
        _add_file(db, sub, body_name, body.encode("utf-8"), "text/plain", parent, seen, depth)
    count = 0
    for part in msg.iter_attachments():
        is_email = part.get_content_type() == "message/rfc822"
        filename = part.get_filename() or ("forwarded-email.eml" if is_email else "attachment.bin")
        _add_file(db, sub, filename, _attachment_bytes(part), part.get_content_type(), parent, seen, depth)
        count += 1
    return count


def _attachment_bytes(part) -> bytes:
    """An attachment's bytes. An email attached to an email ("forward as attachment") is a message object."""
    if part.get_content_type() == "message/rfc822":
        inner = part.get_content()
        return inner.as_bytes() if hasattr(inner, "as_bytes") else bytes(inner)
    return part.get_payload(decode=True) or b""


EMAIL_BODY_NAME = "email-body.txt"


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def email_body(msg) -> str:
    """The text the agent typed: the plain-text part, else the HTML part as text. Empty if neither."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        parser = _HTMLText()
        parser.feed(content)
        content = "".join(parser.parts)
    lines = [line.rstrip() for line in content.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


UPLOAD_SUBJECT = "Uploaded files"


def ingest_files(db, files: list[tuple[str, bytes]], sender: str = "", subject: str = UPLOAD_SUBJECT,
                 note: str = "") -> Submission:
    """Same path as email intake, for files uploaded directly (simulated email intake). sender and note are
    what the person uploading says came with the files; the note is read like an email body. An email found
    among the files supplies the sender and subject the upload doesn't have."""
    sub = Submission(sender=sender, subject=subject, status="RECEIVED")
    db.add(sub)
    db.flush()
    audit(db, sub.id, "system", "submission.received", f"submission:{sub.id}",
          after={"sender": sender, "subject": subject, "note": bool(note.strip()), "files": [f for f, _ in files]})
    seen: dict[str, Document] = {}
    if note.strip():
        _add_file(db, sub, EMAIL_BODY_NAME, note.strip().encode("utf-8"), "text/plain", None, seen, depth=0)
    for filename, data in files:
        _add_file(db, sub, filename, data, mimetypes.guess_type(filename)[0] or "", None, seen, depth=0)
    return sub


def _add_file(db, sub, filename, data, media_type, parent, seen, depth) -> Document:
    digest = sha256(data)
    media_type = media_type if media_type and media_type != "application/octet-stream" else (
        mimetypes.guess_type(filename)[0] or "application/octet-stream")
    doc = Document(submission_id=sub.id, parent_id=parent.id if parent else None, filename=filename,
                   sha256=digest, blob_path=stored_path(store_blob(data, filename)), media_type=media_type,
                   size=len(data))
    db.add(doc)
    db.flush()

    if digest in seen:
        doc.status = "DUPLICATE"
        doc.duplicate_of_id = seen[digest].id
        log.info("duplicate file skipped", submission_id=sub.id, doc_id=doc.id, duplicate_of=seen[digest].id)
        return doc
    seen[digest] = doc

    suffix = Path(filename).suffix.lower()
    if suffix == ".zip" or media_type in ("application/zip", "application/x-zip-compressed"):
        _unpack_zip(db, sub, doc, data, seen, depth)
    elif suffix == ".eml" or media_type == "message/rfc822":
        _unpack_email(db, sub, doc, data, seen, depth)
    return doc


def _unpack_email(db, sub, doc, data, seen, depth):
    """An email inside the submission (forwarded as an attachment, zipped, or uploaded with other files): its
    body and attachments are read like the outer email's. If the submission has no email of its own (a
    direct upload), this one supplies the sender and subject; the uploader's own entries are kept."""
    doc.status = "CONTAINER"
    if depth >= MAX_ZIP_DEPTH:
        doc.status, doc.error = "UNSUPPORTED", "nested email or archive depth limit reached"
        return
    msg = email.message_from_bytes(data, policy=policy.default)
    if not sub.email_sha256:
        sub.sender = sub.sender or str(msg.get("From", ""))
        if not sub.subject or sub.subject == UPLOAD_SUBJECT:
            sub.subject = str(msg.get("Subject", "")) or sub.subject
        sub.email_sha256 = doc.sha256
    has_body = db.query(Document.id).filter_by(submission_id=sub.id, filename=EMAIL_BODY_NAME).first()
    body_name = f"{Path(doc.filename).stem}-body.txt" if has_body else EMAIL_BODY_NAME
    _email_parts(db, sub, msg, doc, seen, depth + 1, body_name)


def _unpack_zip(db, sub, doc, data, seen, depth):
    doc.status = "CONTAINER"
    if depth >= MAX_ZIP_DEPTH:
        doc.status, doc.error = "UNSUPPORTED", "nested archive depth limit reached"
        return
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        doc.status, doc.error = "UNREADABLE", f"bad zip: {exc}"
        return
    members = [i for i in zf.infolist() if not i.is_dir()][:MAX_ZIP_MEMBERS]
    for info in members:
        name = PurePosixPath(info.filename).name  # flatten paths, never write outside blob store
        if not name or name.startswith("."):
            continue
        _add_file(db, sub, name, zf.read(info), "", doc, seen, depth + 1)


def split_pages(db, doc: Document) -> None:
    """Create Page rows: PDF pages, XLSX sheets, a Word or text document, or a single image page."""
    if doc.status != "OK":
        return
    path = data_path(doc.blob_path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            _split_pdf(db, doc, path)
        elif suffix in (".xlsx", ".xlsm"):
            _split_xlsx(db, doc, path)
        elif suffix == ".docx":
            _split_docx(db, doc, path)
        elif suffix == ".txt":
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            db.add(Page(document_id=doc.id, page_no=1, has_text_layer=bool(text), text=text, text_source="text_layer"))
        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            db.add(Page(document_id=doc.id, page_no=1, image_path=stored_path(_page_image(doc, path)), has_text_layer=False))
        else:
            doc.status, doc.error = "UNSUPPORTED", f"unsupported file type {suffix or doc.media_type}"
    except Exception as exc:  # corrupt file: explicit state, never a guess
        doc.status, doc.error = "UNREADABLE", f"{type(exc).__name__}: {exc}"
        log.warning("file unreadable", doc_id=doc.id, error=doc.error)


def _split_pdf(db, doc, path):
    pdf = pdfium.PdfDocument(str(path))
    with pdfplumber.open(str(path)) as plumber:
        for i, ppage in enumerate(plumber.pages):
            text = ppage.extract_text() or ""
            image_path = PAGE_DIR / f"{doc.sha256}_p{i + 1}.png"
            if not image_path.exists():
                pdf[i].render(scale=RENDER_SCALE).to_pil().save(image_path)
            has_text = len(text.strip()) >= MIN_TEXT_CHARS
            db.add(Page(document_id=doc.id, page_no=i + 1, has_text_layer=has_text,
                        text=text if has_text else "", text_source="text_layer" if has_text else "",
                        image_path=stored_path(image_path)))


MAX_IMAGE_EDGE = 2000           # px: phone photos are ~4000; the model reads 2000 fine, and the API caps size
MAX_IMAGE_BYTES = 3_500_000      # under the API's 5 MB image limit once base64-encoded


def _page_image(doc, path: Path) -> Path:
    """The image the model and the UI see. A photo that is already upright, small and light is used as-is
    (same bytes, so earlier runs' cached reads still apply). Otherwise: turned upright from its EXIF
    orientation (phones store rotation separately from the pixels) and scaled to MAX_IMAGE_EDGE."""
    with Image.open(path) as im:
        rotated = (im.getexif().get(0x0112, 1) or 1) != 1   # EXIF Orientation tag
        if not rotated and max(im.size) <= MAX_IMAGE_EDGE and path.stat().st_size <= MAX_IMAGE_BYTES:
            return path
        im = ImageOps.exif_transpose(im)
        im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        out = PAGE_DIR / f"{doc.sha256}_p1.jpg"
        im.convert("RGB").save(out, "JPEG", quality=90)
        return out


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_DOCX_XML = 20 * 1024 * 1024  # document.xml larger than this (a zip bomb, not a cover note) is refused


def docx_text(path) -> str:
    """A Word document's body as text, in reading order: one line per paragraph, one 'a | b | c' line per
    table row (the convention the PDF and spreadsheet paths use). Read in code, no model. Headers,
    footers and embedded images are not read."""
    with zipfile.ZipFile(path) as z:
        if z.getinfo("word/document.xml").file_size > MAX_DOCX_XML:
            raise ValueError("word/document.xml is too large")
        root = ElementTree.fromstring(z.read("word/document.xml"))

    def para(p) -> str:
        parts = []
        for el in p.iter():
            if el.tag == _W + "t":
                parts.append(el.text or "")
            elif el.tag == _W + "tab":
                parts.append("\t")
            elif el.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        return "".join(parts).strip()

    lines = []
    body = root.find(_W + "body")
    for block in (body if body is not None else []):
        if block.tag == _W + "p":
            lines.append(para(block))
        elif block.tag == _W + "tbl":
            for row in block.iter(_W + "tr"):
                cells = [" ".join(t for t in (para(p) for p in cell.iter(_W + "p")) if t) for cell in row.iter(_W + "tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
    return "\n".join(line for line in lines if line)


def _split_docx(db, doc, path):
    text = docx_text(path)
    if not text.strip():
        doc.status, doc.error = "UNSUPPORTED", "Word file has no text (images only are not read from .docx)"
        return
    db.add(Page(document_id=doc.id, page_no=1, has_text_layer=True, text=text, text_source="text_layer"))


def cell_text(cell, cached) -> str:
    """`B4='DEVRAJ'`; formula cells also show the saved result, or say there is none.
    Some tools write formulas without calculating them; we never recompute (that would be filling in)."""
    if hasattr(cell.value, "strftime"):  # Excel date serial (e.g. 46317) shown as the date it means
        return f"{cell.coordinate}={cell.value.strftime('%m/%d/%Y')} (date)"
    text = f"{cell.coordinate}={cell.value!r}"
    if isinstance(cell.value, str) and cell.value.startswith("="):
        text += f" [calculated: {cached!r}]" if cached is not None else " [formula; no calculated value saved in file]"
    return text


def _split_xlsx(db, doc, path):
    wb = openpyxl.load_workbook(str(path), data_only=False)
    values = openpyxl.load_workbook(str(path), data_only=True)
    for i, ws in enumerate(wb.worksheets):
        vs = values.worksheets[i]
        lines = []
        for row in ws.iter_rows():
            cells = [cell_text(c, vs[c.coordinate].value) for c in row if c.value not in (None, "")]
            if cells:
                lines.append(" | ".join(cells))
        label = ws.title + (" (hidden sheet)" if ws.sheet_state != "visible" else "")
        db.add(Page(document_id=doc.id, page_no=i + 1, label=label, has_text_layer=True,
                    text="\n".join(lines), text_source="xlsx"))
