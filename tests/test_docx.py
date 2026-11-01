"""Word attachments (.docx) are read at intake like any text page; a cover note typed OTHER counts as read."""
import io
import zipfile
from types import SimpleNamespace as NS

from app import pipeline
from app.db import Document, SessionLocal, Submission, init_db
from app.intake import docx_text, ingest_files, split_pages

W = "xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'"


def _docx(body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_paragraphs_and_table_rows_in_reading_order(tmp_path):
    table = ("<w:tbl>" + "".join(f"<w:tr><w:tc>{_p(a)}</w:tc><w:tc>{_p(b)}</w:tc></w:tr>" for a, b in
                                 (("Driver", "DOB"), ("Maria Lopez", "04/12/1998"))) + "</w:tbl>")
    path = tmp_path / "supplement.docx"
    path.write_bytes(_docx(_p("Supplemental questionnaire") + table + _p("Signed: Dave")))
    assert docx_text(path) == "Supplemental questionnaire\nDriver | DOB\nMaria Lopez | 04/12/1998\nSigned: Dave"


def test_a_docx_becomes_a_text_page_and_a_broken_one_is_unreadable():
    init_db()
    with SessionLocal() as db:
        sub = ingest_files(db, [("notes.docx", _docx(_p("Agent notes: insured wants a quote by 10/15."))),
                                ("broken.docx", b"PK not really a zip")], sender="t", subject="docx")
        db.commit()
        docs = {d.filename: d for d in db.query(Document).filter_by(submission_id=sub.id)}
        for d in docs.values():
            split_pages(db, d)
        db.commit()
        notes, broken = docs["notes.docx"], docs["broken.docx"]
        assert notes.status == "OK" and notes.pages[0].has_text_layer and "10/15" in notes.pages[0].text
        assert broken.status == "UNREADABLE"


def test_a_page_confidently_typed_other_is_read_not_unclassified():
    init_db()
    with SessionLocal() as db:
        sub = Submission(subject="other")
        db.add(sub)
        db.commit()
        doc = NS(filename="notes.docx")
        for confidence, status in ((0.9, "OK"), (0.3, "UNCLASSIFIED")):
            page = NS(document=doc, page_no=1, label="", status="PENDING", error="")
            result = NS(doc_type="OTHER", classify_confidence=confidence, classify_reason="agent cover note",
                        classify_method="llm_text", status="PENDING", error="", text="x", text_source="text_layer")
            pipeline._classify(db, sub, page, result)
            assert page.status == status
