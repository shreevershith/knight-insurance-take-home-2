"""The agent's email body is part of the submission; phone photos are turned upright and sized for the model."""
from email.message import EmailMessage

from PIL import Image

from app.analyst import agent_email, build_context
from app.db import Document, SessionLocal, init_db
from app.intake import EMAIL_BODY_NAME, MAX_IMAGE_EDGE, _page_image, email_body, ingest_eml, split_pages


def _eml(plain=None, html=None) -> bytes:
    m = EmailMessage()
    m["From"], m["Subject"] = "agent@example.com", "New business - Test Trucking"
    if plain:
        m.set_content(plain)
    if html:
        m.add_alternative(html, subtype="html") if plain else m.set_content(html, subtype="html")
    m.add_attachment(b"%PDF-1.4 not really", maintype="application", subtype="pdf", filename="app.pdf")
    return m.as_bytes()


def test_plain_body_preferred_html_body_read_as_text():
    from email import message_from_bytes, policy
    msg = message_from_bytes(_eml(plain="Eff 12/15/2026.\nMVR to follow.", html="<p>ignored</p>"), policy=policy.default)
    assert email_body(msg) == "Eff 12/15/2026.\nMVR to follow."
    msg = message_from_bytes(_eml(html="<div>Hi team,</div><p>5 units, <b>pls quote</b> by Fri</p>"), policy=policy.default)
    assert email_body(msg) == "Hi team,\n5 units, pls quote by Fri"


def test_body_becomes_a_text_page_and_reaches_the_analyst():
    init_db()
    with SessionLocal() as db:
        sub = ingest_eml(db, _eml(plain="Eff 12/15/2026. One loss run is a phone photo, sorry."))
        db.commit()
        body = db.query(Document).filter_by(submission_id=sub.id, filename=EMAIL_BODY_NAME).one()
        split_pages(db, body)
        db.commit()
        page = body.pages[0]
        assert page.has_text_layer and page.text_source == "text_layer" and "phone photo" in page.text
        assert "phone photo" in agent_email(db, sub.id)
        context, _ = build_context(db, sub)
        assert context["agent_email"]["from"] == "agent@example.com" and "12/15/2026" in context["agent_email"]["body"]


def _photo(path, size, orientation=1):
    im = Image.new("RGB", size, "white")
    exif = im.getexif()
    exif[0x0112] = orientation
    im.save(path, "JPEG", exif=exif.tobytes())
    return path


def test_sideways_phone_photo_is_turned_upright_and_large_one_is_scaled(tmp_path):
    from types import SimpleNamespace as NS
    rotated = _page_image(NS(sha256="rot"), _photo(tmp_path / "rot.jpg", (1200, 800), orientation=6))
    with Image.open(rotated) as im:
        assert im.size == (800, 1200)                       # EXIF 6 = rotate 90: now portrait, as the person saw it
    big = _page_image(NS(sha256="big"), _photo(tmp_path / "big.jpg", (4032, 3024)))
    with Image.open(big) as im:
        assert max(im.size) == MAX_IMAGE_EDGE
    small = _photo(tmp_path / "small.jpg", (1600, 1204))
    assert _page_image(NS(sha256="small"), small) == small  # already fine: same bytes, cached reads still apply


def _inner_email() -> EmailMessage:
    m = EmailMessage()
    m["From"], m["Subject"] = "Dana Pruitt <dana@agency.example>", "Bluestem Haulage - new business"
    m.set_content("Eff 10/30/2026. Loss runs to follow.")
    m.add_attachment(b"%PDF-1.4 not really", maintype="application", subtype="pdf", filename="app.pdf")
    return m


def _docs(db, sub):
    db.commit()
    return {d.filename: d for d in db.query(Document).filter_by(submission_id=sub.id)}


def test_an_email_forwarded_as_an_attachment_is_opened():
    from app.intake import ingest_eml
    outer = EmailMessage()
    outer["From"], outer["Subject"] = "csr@knight.example", "FW: Bluestem"
    outer.set_content("Forwarding the agent's email below.")
    outer.add_attachment(_inner_email())                       # message/rfc822, as mail clients forward
    init_db()
    with SessionLocal() as db:
        sub = ingest_eml(db, outer.as_bytes())
        docs = _docs(db, sub)
        assert docs["forwarded-email.eml"].status == "CONTAINER"
        assert "app.pdf" in docs and docs["app.pdf"].parent_id == docs["forwarded-email.eml"].id
        assert "forwarded-email-body.txt" in docs                # the outer body keeps email-body.txt
        assert sub.sender == "csr@knight.example"                # the submission's own email wins


def test_an_email_inside_a_zip_or_among_uploads_supplies_the_sender():
    import io
    import zipfile
    from app.intake import UPLOAD_SUBJECT, ingest_files
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("original email.eml", _inner_email().as_bytes())
    init_db()
    with SessionLocal() as db:
        sub = ingest_files(db, [("package.zip", buf.getvalue())])
        docs = _docs(db, sub)
        assert sub.sender.startswith("Dana Pruitt") and sub.subject == "Bluestem Haulage - new business"
        assert "app.pdf" in docs and EMAIL_BODY_NAME in docs     # its body is the submission's email text
        own = ingest_files(db, [("x.eml", _inner_email().as_bytes())], sender="Rivergate CSR", note="Agent called: eff 10/30.")
        docs = _docs(db, own)
        assert own.sender == "Rivergate CSR" and own.subject == "Bluestem Haulage - new business"
        from app.config import data_path
        assert "Agent called" in data_path(docs[EMAIL_BODY_NAME].blob_path).read_text(encoding="utf-8")
        assert "x-body.txt" in docs                              # the uploader's note keeps email-body.txt
    assert UPLOAD_SUBJECT == "Uploaded files"
