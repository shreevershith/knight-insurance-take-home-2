"""Build held-out submission #2 (D-19): a fictitious Tennessee new-venture corporation, written from scratch
after the held-out #1 fixes and run once on unchanged code, so its score is a measurement.

Different from the sample and from held-out #1 on purpose:
  - files inside a zip with misleading names ("Document (3).pdf" is a loss run), plus a loose copy of
    the application (the zipped "Application.pdf" must be caught as a DUPLICATE) and a .docx (UNSUPPORTED)
  - the driver and vehicle schedule as a PDF, not a spreadsheet
  - four IFTA returns in one PDF, the third page an image-only scan
  - Tennessee MVRs; a new-venture corporation; a commodity list that adds up to only 90%
Planted: a tow truck (APP-02 DECLINE), speeding 31-35 over (DRV-08 DECLINE), a 22-year-old driver,
an MVR 121 days old, an AL deductible, an APD request, under 3 years of coverage with a missing loss run,
"HAULAGE" vs "HAULING". Expected outcomes: eval/heldout/bluestem_expected.json (written first).

Usage: .venv/Scripts/python.exe scripts/make_heldout2.py
"""
import io
import random
import sys
import tempfile
import zipfile
from email.message import EmailMessage
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from make_heldout import render_page, scan, vin  # noqa: E402  (drawing helpers only)

OUT = ROOT / "eval" / "heldout" / "bluestem"
EML = ROOT / "eval" / "heldout" / "bluestem.eml"
random.seed(66218)
INSURED = "BLUESTEM HAULAGE INC"

UNITS = [  # unit, year, make, model, vin, type, stated value ("" = missing)
    ("1", "2022", "VOLVO", "VNL 760", vin("4V4NC9EH?NN302214"), "TRACTOR", "$138,000"),
    ("2", "2018", "PETERBILT", "389", vin("1XPXD49X?JD771045"), "TRACTOR", ""),
    ("3", "2015", "FORD", "F-550", vin("1FDUF5HT?FEA10552"), "TOW TRUCK (WRECKER)", "$61,000"),
]
DRIVERS = [  # name, DOB, TN license, class, hire
    ("MARCUS DELANEY", "05/18/1988", "104477218", "A", "11/01/2024"),
    ("KAYLA BROOKS", "03/02/2004", "118820457", "A", "06/10/2025"),
    ("ROBERTO SALINAS", "11/30/1979", "097733561", "A", "11/01/2024"),
]
STATES = ["Tennessee", "Arkansas", "Alabama", "Mississippi", "Kentucky", "Virginia"]
RATES = [0.27, 0.285, 0.29, 0.18, 0.246, 0.302]
QUARTERS = [  # (begin, end, due, miles per state)
    ("07/01/2025", "09/30/2025", "10/31/2025", [24480, 9180, 8568, 6732, 7344, 4896]),     # 61,200
    ("10/01/2025", "12/31/2025", "01/31/2026", [23520, 8820, 8232, 6468, 7056, 4704]),     # 58,800
    ("01/01/2026", "03/31/2026", "04/30/2026", [25200, 9450, 8820, 6930, 7560, 5040]),     # 63,000 (scanned)
    ("04/01/2026", "06/30/2026", "07/31/2026", [26400, 9900, 9240, 7260, 7920, 5280]),     # 66,000
]


def pdf_bytes(pages: list) -> bytes:
    """Pages are text (list of lines; '#' = heading) or a PIL image (an image-only, scanned page)."""
    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(False)
    for page in pages:
        pdf.add_page()
        if not isinstance(page, list):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "scan.png"
                page.save(path)
                pdf.image(str(path), x=0, y=0, w=215.9, h=279.4)
            continue
        y = 15
        for line in page:
            heading = line.startswith("#")
            pdf.set_font("Helvetica", "B" if heading else "", 12.5 if heading else 9.3)
            pdf.set_xy(12, y)
            pdf.cell(0, 5, line.lstrip("# "))
            y += 7 if heading else 5.1
    return bytes(pdf.output())


def application() -> list:
    return [[
        "# TRUCKING NEW BUSINESS APPLICATION",
        "Agency: Rivergate Insurance Group     Agent: Dana Pruitt     (731) 555-0144",
        "Application date: 09/28/2026     Requested effective date: 10/30/2026",
        "",
        "# 1. APPLICANT",
        f"Legal name: {INSURED}          Form of business: Corporation",
        "FEIN: 62-3398154     USDOT No.: 4012775     MC No.: 1450932",
        "Address: 418 Industrial Pkwy, Jackson, TN 38301",
        "Years in business: 2          Is this a new venture?  Yes",
        "Principal: Gerald Whitcomb, President     Principal drives: No",
        "",
        "# 2. COVERAGE",
        "Auto liability: $1,000,000 combined single limit     AL deductible requested: $2,500",
        "Physical damage quote requested: Yes (stated values on schedule)",
        "Motor truck cargo: Yes, limit $100,000",
        "",
        "# 3. OPERATIONS",
        "Hazardous materials: No     Intermodal / containers: No     Cross-border: No",
        "Towing or recovery operations: No     Hauls automobiles: No",
        "Radius: local 30%, intermediate (to 500 mi) 50%, long haul 20%",
        "States operated: TN, AR, AL, MS, KY, VA",
        "Power units: 3",
        "",
        "# 4. COMMODITIES",
        "Commodity                        Percent     Average value",
        "Packaged foods / dry grocery     50%         $45,000",
        "Lumber and building supply       25%         $28,000",
        "Furniture (boxed)                15%         $30,000",
    ], [
        "# 5. PRIOR INSURANCE",
        "Insurer                   Policy number    Effective    Expiration    Units",
        "Hollis Casualty Company   HC-55102         10/15/2024   10/15/2025    2",
        "Hollis Casualty Company   HC-66218         10/15/2025   10/15/2026    3",
        "",
        "# 6. DRIVERS AND EQUIPMENT",
        "Per attached schedule.",
        "",
        "Signature: Gerald Whitcomb, President          Date: 09/28/2026",
    ]]


def schedule() -> list:
    drivers = ["# DRIVER SCHEDULE - BLUESTEM HAULAGE INC", "",
               "Name               | Date of Birth | License No. | State | Class | Date of Hire"]
    drivers += [f"{n:18} | {d}    | {lic}   | TN    | {c}     | {h}" for n, d, lic, c, h in DRIVERS]
    vehicles = ["# VEHICLE SCHEDULE - BLUESTEM HAULAGE INC", "",
                "Unit | Year | Make      | Model   | VIN               | Type                | Stated Value"]
    vehicles += [f"{u}    | {y} | {mk:9} | {md:7} | {v} | {t:19} | {sv or ''}" for u, y, mk, md, v, t, sv in UNITS]
    return [drivers, vehicles]


def mvrs() -> list:
    people = [
        ("DELANEY, MARCUS", "104477218", "05/18/1988", "09/20/2026", "03/01/2011", "05/18/2029", []),
        ("BROOKS, KAYLA", "118820457", "03/02/2004", "09/20/2026", "05/01/2024", "03/02/2030", []),
        ("SALINAS, ROBERTO", "097733561", "11/30/1979", "05/30/2026", "07/15/2003", "11/30/2027",
         ["04/22/2025   SPEEDING 31-35 MPH OVER LIMIT         5", "01/15/2026   IMPROPER LANE CHANGE                  2"]),
    ]
    pages = []
    for name, lic, dob, ordered, cdl, exp, viol in people:
        pages.append([
            "# TENNESSEE DEPARTMENT OF SAFETY AND HOMELAND SECURITY",
            "# MOTOR VEHICLE RECORD - CERTIFIED",
            f"Date ordered: {ordered}     Account: RIVERGATE INS GRP",
            "",
            f"Driver: {name}          DOB: {dob}",
            f"DL Number: {lic}     Class: A (COMMERCIAL)     Status: VALID     Expiration: {exp}",
            f"CDL Issue Date: {cdl}     Endorsements: NONE     Medical certificate: ON FILE",
            "",
            "# MOVING VIOLATIONS AND CONVICTIONS",
            "Date         Description                           Points",
            *(viol or ["NONE"]),
            "",
            "Total points (3 yr): " + str(sum(int(v.split()[-1]) for v in viol)),
        ])
    return pages


def loss_run() -> list:
    return [[
        "# HOLLIS CASUALTY COMPANY",
        "# CERTIFIED LOSS RUN",
        "Prepared: 09/10/2026",
        "Named insured: BLUESTEM HAULING INC",
        "Policy: HC-66218     Coverage: Commercial Auto Liability",
        "Period: 10/15/2025 - 10/15/2026",
        "",
        "Claim       Loss date    Type                 Driver           Status   Paid    Reserve    Incurred",
        f"HC-C-2207   03/04/2026   Bodily injury        MARCUS DELANEY   OPEN     $0      $35,000    $35,000",
        f"            Unit: 2022 VOLVO VNL 760  VIN {UNITS[0][4]}",
        "",
        "Policy totals: 1 claim, 1 open. Paid $0. Reserve $35,000. Incurred $35,000.",
    ]]


def ifta() -> list:
    pages = []
    for i, (begin, end, due, miles) in enumerate(QUARTERS):
        rows, tax_total = [], 0.0
        for state, mi, rate in zip(STATES, miles, RATES):
            gal = round(mi / 6.2)
            paid = round(gal * (2.6 if state == "Tennessee" else random.uniform(0.2, 1.1)))
            tax = round((gal - paid) * rate, 2)
            tax_total += tax
            rows.append([state, f"{mi:,}", f"{mi:,}", f"{gal:,}", f"{paid:,}", f"{tax:,.2f}"])
        head = ["Jurisdiction", "Total Miles", "Taxable Miles", "Taxable Gallons", "Tax Paid Gallons", "Tax Due"]
        total = ["TOTAL", f"{sum(miles):,}", f"{sum(miles):,}", "", "", f"{tax_total:,.2f}"]
        top = ["# TENNESSEE DEPARTMENT OF REVENUE", "# IFTA QUARTERLY FUEL TAX RETURN",
               f"Licensee: {INSURED}     IFTA Lic. No.: TN-4471902",
               f"Quarter: {begin} through {end}     Due: {due}", ""]
        if i == 2:  # the third return was scanned
            pages.append(scan(render_page(top + ["\t".join(r) for r in [head] + rows + [total]], size=24)))
        else:
            pages.append(top + [" | ".join(r) for r in [head] + rows + [total]])
    return pages


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
        z.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
                   "<w:body><w:p><w:r><w:t>Agent notes: insured is growing fast, wants quote by 10/15.</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = pdf_bytes(application())
    members = {
        "Document (3).pdf": pdf_bytes(loss_run()),
        "Application.pdf": app,                       # same bytes as the loose attachment -> DUPLICATE
        "schedule.pdf": pdf_bytes(schedule()),
        "MVRs.pdf": pdf_bytes(mvrs()),
        "IFTA_returns_2025-2026.pdf": pdf_bytes(ifta()),
        "notes.docx": docx_bytes(),
    }
    for name, data in members.items():
        (OUT / name).write_bytes(data)
    (OUT / "Bluestem_App.pdf").write_bytes(app)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)

    msg = EmailMessage()
    msg["From"] = "Dana Pruitt <dpruitt@rivergate.example>"
    msg["To"] = "submissions@knight-casestudy.example"
    msg["Subject"] = "Bluestem Haulage - new business - eff 10/30"
    msg["Date"] = "Mon, 28 Sep 2026 15:40:00 -0500"
    msg.set_content("Hi,\n\nBluestem Haulage Inc. (Jackson TN), new venture, eff 10/30/2026. App attached; everything "
                    "else is in the zip.\n\nDana Pruitt\nRivergate Insurance Group\n")
    msg.add_attachment(app, maintype="application", subtype="pdf", filename="Bluestem_App.pdf")
    msg.add_attachment(zbuf.getvalue(), maintype="application", subtype="zip", filename="bluestem_docs.zip")
    EML.write_bytes(bytes(msg))
    print(f"wrote {len(members) + 1} files to {OUT.relative_to(ROOT)} and {EML.relative_to(ROOT)} ({EML.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
