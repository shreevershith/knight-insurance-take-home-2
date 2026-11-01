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
