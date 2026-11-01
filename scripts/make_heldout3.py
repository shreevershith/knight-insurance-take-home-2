"""Build held-out submission #3 (D-39): a CLEAN fictitious Tennessee fleet, written after the expected values
(eval/heldout/holston_expected.json) and run once, so its score is a measurement.

It tests what no earlier set reached: a submission that should pass every rule (QUOTE_AFTER_CHECKS: only the
three outside-data checks VERIFY) and Fleet routing (12 power units). New formats on purpose:
  - the application as a Word file (.docx) with tables; an HTML-only email; attachments sent loose, no zip
  - the vehicle schedule in a sheet named "Book1.xlsx"
  - a phone photo of the lead driver's CDL stored sideways with an EXIF rotation tag (as phones save them)
Nothing is planted: every value agrees across documents.

Usage: .venv/Scripts/python.exe scripts/make_heldout3.py
"""
import io
import sys
import zipfile
from email.message import EmailMessage
from pathlib import Path
from xml.sax.saxutils import escape

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from make_heldout import render_page, vin  # noqa: E402  (drawing and VIN helpers)
from make_heldout2 import pdf_bytes  # noqa: E402

OUT = ROOT / "eval" / "heldout" / "holston"
EML = ROOT / "eval" / "heldout" / "holston.eml"
INSURED = "HOLSTON VALLEY FREIGHT INC"
STATES = ["Tennessee", "Virginia", "West Virginia", "South Carolina", "Alabama", "Arkansas"]
SHARES = [0.35, 0.20, 0.10, 0.15, 0.12, 0.08]
QUARTERS = [("07/01/2025", "09/30/2025", "10/31/2025", 289800), ("10/01/2025", "12/31/2025", "01/31/2026", 276000),
            ("01/01/2026", "03/31/2026", "04/30/2026", 294400), ("04/01/2026", "06/30/2026", "07/31/2026", 303600)]
YEAR_CODE = {2019: "K", 2020: "L", 2021: "M", 2022: "N", 2023: "P", 2024: "R"}
MAKES = [("FREIGHTLINER", "CASCADIA", "3AKJHHDR?{y}SK{s:05d}"), ("VOLVO", "VNL 760", "4V4NC9EH?{y}N{s:06d}"),
         ("KENWORTH", "T680", "1XKYD49X?{y}J{s:06d}"), ("PETERBILT", "579", "1XPBD49X?{y}D{s:06d}")]
UNITS = []  # unit, year, make, model, vin, stated value
for i in range(12):
    year = 2019 + i % 6
    make, model, template = MAKES[i % 4]
    UNITS.append((str(i + 1), str(year), make, model, vin(template.format(y=YEAR_CODE[year], s=31407 + 977 * i)),
                  128000 + 6000 * (year - 2019) + 1500 * (i % 3)))
# name (first, last), DOB, TN license, hire date, original CDL issue, current card issued, license expiration
DRIVERS = [
    ("JAMES", "WHITAKER", "04/09/1979", "112204871", "03/01/2018", "06/12/2009", "03/14/2024", "04/09/2029"),
    ("CHIDI", "OKONKWO", "11/22/1986", "108833402", "05/15/2019", "08/03/2015", "11/02/2023", "11/22/2031"),
    ("LAURA", "PENNINGTON", "02/14/1990", "115560923", "01/10/2020", "07/20/2014", "02/01/2022", "02/14/2030"),
    ("DERRICK", "HOLLOWAY", "08/30/1975", "097718244", "09/01/2017", "04/11/2001", "08/15/2025", "08/30/2033"),
    ("MIGUEL", "SANTOS", "06/05/1984", "110027566", "02/20/2021", "10/02/2010", "06/01/2024", "06/05/2032"),
    ("TASHA", "REYNOLDS", "12/19/1992", "118840137", "07/06/2022", "03/18/2016", "12/10/2024", "12/19/2032"),
    ("WAYNE", "CARMICHAEL", "03/27/1970", "089924415", "04/04/2016", "05/09/1996", "03/20/2022", "03/27/2030"),
    ("BRANDON", "FOLEY", "10/11/1995", "121104588", "08/14/2023", "01/25/2019", "10/01/2023", "10/11/2031"),
    ("ANGELA", "MCCOY", "05/23/1981", "103367719", "06/01/2019", "09/14/2006", "05/15/2025", "05/23/2033"),
    ("TERRENCE", "BLAKE", "01/08/1988", "111938850", "03/12/2021", "12/01/2012", "01/05/2024", "01/08/2032"),
    ("KYLE", "DUNCAN", "07/16/1993", "119420633", "11/03/2022", "06/22/2017", "07/10/2025", "07/16/2033"),
    ("ROSA", "IBARRA", "09/02/1986", "107758291", "04/18/2020", "02/27/2011", "09/01/2022", "09/02/2030"),
]
TERMS = [  # carrier, policy, start, end, units
    ("Appalachian Mutual Insurance Co", "AM-220871", "11/01/2022", "11/01/2023", 9),
    ("Appalachian Mutual Insurance Co", "AM-231904", "11/01/2023", "11/01/2024", 10),
    ("Summit Ridge Casualty", "SRC-4410022", "11/01/2024", "11/01/2025", 11),
    ("Summit Ridge Casualty", "SRC-5520317", "11/01/2025", "11/01/2026", 12),
]


# ---------------------------------------------------------------- application (.docx)

def _p(text: str, bold: bool = False) -> str:
    run = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p><w:r>{run}<w:t xml:space='preserve'>{escape(text)}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{_p(c)}</w:tc>" for c in r) + "</w:tr>" for r in rows) + "</w:tbl>"


def application_docx() -> bytes:
    body = "".join([
        _p("COMMERCIAL AUTO INSURANCE APPLICATION - TRUCKING", bold=True),
        _p("Agency: Smoky Mountain Insurance Partners     Producer: Renee Calloway     (865) 555-0190"),
        _p("Application date: 10/05/2026     Requested effective date: 11/01/2026"),
        _p("1. APPLICANT", bold=True),
        _p(f"Legal name: {INSURED}"),
        _p("Form of business: Corporation     Years in business: 9     New venture: No"),
        _p("FEIN: 47-2219086     USDOT No.: 3388104     MC No.: 1187365"),
        _p("Mailing address: 2210 Sutherland Ave, Knoxville, TN 37919"),
        _p("Garaging address: 2210 Sutherland Ave, Knoxville, TN 37919"),
        _p("2. COVERAGE", bold=True),
        _p("Auto liability: $1,000,000 combined single limit     Auto liability deductible: None"),
        _p("Physical damage (APD) quote requested: No"),
        _p("Motor truck cargo: Yes, limit $100,000"),
        _p("3. OPERATIONS", bold=True),
        _p("Hazardous materials: No     Intermodal / containers: No     Cross-border (Mexico): No"),
        _p("Towing or recovery operations: No     Hauls automobiles: No"),
        _p("Radius: intermediate (to 500 miles) 70%, long haul 30%"),
        _p("States operated: TN, VA, WV, SC, AL, AR"),
        _p("Power units: 12     Drivers: 12"),
        _p("4. COMMODITIES", bold=True),
        _table([["Commodity", "Percent", "Average value"], ["Paper and packaging products", "40%", "$38,000"],
                ["Packaged foods / dry grocery", "35%", "$42,000"], ["Furniture (boxed)", "25%", "$31,000"]]),
        _p("5. PRIOR INSURANCE (last 4 years)", bold=True),
        _table([["Insurer", "Policy number", "Coverage", "Effective", "Expiration", "Units"]]
               + [[c, p, "Auto liability", s, e, str(u)] for c, p, s, e, u in TERMS]),
        _p("6. DRIVERS AND EQUIPMENT", bold=True),
        _p("Per attached driver and vehicle schedules."),
        _p("Signature: Gary Holston, President     Date: 10/05/2026"),
    ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
                   "<Default Extension='xml' ContentType='application/xml'/></Types>")
        z.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
                   f"<w:body>{body}</w:body></w:document>")
    return buf.getvalue()


# ---------------------------------------------------------------- schedules (.xlsx)

def xlsx(header: list[str], rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def drivers_xlsx() -> bytes:
    return xlsx(["First Name", "Last Name", "Date of Birth", "License #", "License State", "Date of Hire"],
                [[f, l, dob, lic, "TN", hire] for f, l, dob, lic, hire, *_ in DRIVERS])


def vehicles_xlsx() -> bytes:
    return xlsx(["Unit #", "Year", "Make", "Model", "VIN", "Type", "Stated Value"],
                [[u, y, mk, md, v, "TRACTOR", sv] for u, y, mk, md, v, sv in UNITS])


# ---------------------------------------------------------------- MVRs, loss runs, IFTA (text PDFs)

def mvrs() -> list:
    return [[
        "# TENNESSEE DEPARTMENT OF SAFETY AND HOMELAND SECURITY",
        "# MOTOR VEHICLE RECORD (DRIVER RECORD) - CERTIFIED",
        "Date ordered: 09/22/2026     Account: SMOKY MTN INS PARTNERS",
        "",
        f"Driver: {last}, {first}          DOB: {dob}",
        f"DL Number: {lic}     Class: A (COMMERCIAL)     Status: VALID     Expiration: {exp}",
        f"CDL Issue Date (original): {cdl}     Current card issued: {card}",
        "Endorsements: NONE     Restrictions: NONE     Medical certificate: ON FILE",
        "",
        "# MOVING VIOLATIONS AND CONVICTIONS",
        "NONE",
        "",
        "# ACCIDENTS",
        "NONE",
        "",
        "Total points (3 yr): 0",
    ] for first, last, dob, lic, _hire, cdl, card, exp in DRIVERS]


def loss_runs() -> list:
    return [[
        f"# {carrier.upper()}",
        "# LOSS RUN REPORT",
        "Valuation date: 09/24/2026",
        f"Named insured: {INSURED}",
        f"Policy number: {policy}     Line of business: Commercial Auto Liability",
        f"Policy period: {start} - {end}     Power units: {units}",
        "",
        "NO LOSSES REPORTED FOR THIS POLICY PERIOD",
        "",
        "Total claims: 0     Total paid: $0     Total reserve: $0     Total incurred: $0",
    ] for carrier, policy, start, end, units in TERMS]


def ifta() -> list:
    pages = []
    for begin, end, due, total in QUARTERS:
        miles = [round(total * s) for s in SHARES]
        assert sum(miles) == total, (total, miles)
        head = ["Jurisdiction", "Total Miles", "Taxable Miles", "Taxable Gallons", "Tax Paid Gallons", "Tax Due"]
        rows = [[st, f"{mi:,}", f"{mi:,}", f"{round(mi / 6.4):,}", f"{round(mi / 6.4):,}", "0.00"]
                for st, mi in zip(STATES, miles)]
        pages.append(["# TENNESSEE DEPARTMENT OF REVENUE", "# IFTA QUARTERLY FUEL TAX RETURN",
                      f"Licensee: {INSURED}     IFTA Lic. No.: TN-5520871",
                      f"Quarter: {begin} through {end}     Due: {due}", ""]
                     + [" | ".join(r) for r in [head] + rows + [["TOTAL", f"{total:,}", f"{total:,}", "", "", "0.00"]]])
    return pages


# ---------------------------------------------------------------- CDL photo (sideways, EXIF-rotated)

def cdl_photo() -> bytes:
    first, last, dob, lic, _hire, _cdl, card, exp = DRIVERS[0]
    upright = render_page(["# TENNESSEE", "COMMERCIAL DRIVER LICENSE", "", f"DL NO {lic}        CLASS A",
                           f"{last}", f"{first}", "2210 SUTHERLAND AVE KNOXVILLE TN 37919", "",
                           f"DOB {dob}", f"ISS {card}        EXP {exp}", "END NONE        REST NONE"],
                          width=1500, height=950, size=44)
    stored = upright.rotate(90, expand=True)   # pixels saved on their side, as a phone held in portrait does
    exif = stored.getexif()
    exif[0x0112] = 6                            # Orientation: rotate 90 degrees clockwise to view
    buf = io.BytesIO()
    stored.save(buf, "JPEG", quality=88, exif=exif.tobytes())
    return buf.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "Holston Valley - Application.docx": application_docx(),
        "Drivers.xlsx": drivers_xlsx(),
        "Book1.xlsx": vehicles_xlsx(),
        "MVRs.pdf": pdf_bytes(mvrs()),
        "LossRuns_2022-2026.pdf": pdf_bytes(loss_runs()),
        "IFTA_returns.pdf": pdf_bytes(ifta()),
        "IMG_20260921_142233.jpg": cdl_photo(),
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)

    msg = EmailMessage()
    msg["From"] = "Renee Calloway <rcalloway@smokymtnins.example>"
    msg["To"] = "submissions@knight-casestudy.example"
    msg["Subject"] = "New business - Holston Valley Freight Inc - 12 units - eff 11/01/2026"
    msg["Date"] = "Mon, 05 Oct 2026 10:12:00 -0400"
    msg.set_content("<p>Hi team,</p><p>New business for <b>Holston Valley Freight Inc.</b> (Knoxville, TN), 12 power units, "
                    "effective 11/01/2026. Application, driver and vehicle schedules, MVRs, four years of loss runs "
                    "(all clean) and the last four IFTA returns are attached, plus a photo of our lead driver's CDL.</p>"
                    "<p>Thanks,<br>Renee Calloway<br>Smoky Mountain Insurance Partners</p>", subtype="html")
    for name, data in files.items():
        kind = {"docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
                "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                "pdf": ("application", "pdf"), "jpg": ("image", "jpeg")}[name.rsplit(".", 1)[1]]
        msg.add_attachment(data, maintype=kind[0], subtype=kind[1], filename=name)
    EML.write_bytes(bytes(msg))
    print(f"wrote {len(files)} files to {OUT.relative_to(ROOT)} and {EML.relative_to(ROOT)} ({EML.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
