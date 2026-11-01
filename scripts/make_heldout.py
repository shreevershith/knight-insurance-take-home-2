"""Build the held-out submission (D-19, D-13): a fictitious Arizona carrier, written from scratch.

Nothing here is copied or edited from the case-study sample. Layouts, header names, carriers and
the IFTA state are all different, on purpose, so the run measures generalization:
  - loose attachments (no zip) with misleading names (scan001.pdf is a loss run)
  - a loss run as a rotated, blurred phone photo (IMG_4471.jpg)                      (D-13)
  - an Arizona IFTA return as an image-only scan with abbreviated column headers
  - a corrupt PDF (Supplemental_Questionnaire.pdf)                                   (D-13)
  - a roster spreadsheet whose headers differ from the sample's ("Driver Name", "CDL #", "Body Type")
Planted: a DUI inside 5 years (DECLINE), a box truck (REFER), a stale loss run, a missing IFTA
quarter, a DOB mismatch. Expected outcomes: eval/heldout/desert_ridge_expected.json (written first).

Usage: .venv/Scripts/python.exe scripts/make_heldout.py
"""
import random
import sys
from email.message import EmailMessage
from pathlib import Path

import openpyxl
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.extract.normalize import vin_check  # noqa: E402

OUT = ROOT / "eval" / "heldout" / "desert_ridge"
EML = ROOT / "eval" / "heldout" / "desert_ridge.eml"
FONT = "C:/Windows/Fonts/arial.ttf"
random.seed(4471)

INSURED = "DESERT RIDGE FREIGHT LLC"
APP_DATE, EFF_DATE = "10/15/2026", "11/20/2026"

_TRANSLIT = dict(zip("ABCDEFGHJKLMNPRSTUVWXYZ", [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9]))
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def vin(template: str) -> str:
    """Fill position 9 ('?') with the ISO 3779 check digit."""
    total = sum((int(c) if c.isdigit() else _TRANSLIT.get(c, 0)) * w for c, w in zip(template, _WEIGHTS))
    r = total % 11
    v = template.replace("?", "X" if r == 10 else str(r))
    assert vin_check(v).get("check_digit"), v
    return v


UNITS = [  # unit, year, make, model, vin, body type, stated value
    ("1", "2021", "KENWORTH", "T680", vin("1XKYD49X?MJ439812"), "TRACTOR", "145000"),
    ("2", "2019", "FREIGHTLINER", "CASCADIA", vin("3AKJHHDR?KSKA4471"), "TRACTOR", "98000"),
    ("3", "2020", "ISUZU", "NPR HD", vin("JALC4W16?L7K00918"), "BOX TRUCK", "42000"),
]
DRIVERS = [  # roster name, roster DOB, CDL #, hire date, years exp
    ("HECTOR RAMOS", "04/11/1984", "D04417721", "03/01/2021", "18"),
    ("JAMES T WHITAKER", "12/02/1990", "D07739120", "06/15/2023", "12"),
    ("LUCIA FERNANDEZ", "07/19/2001", "D09921844", "02/10/2025", "3"),   # MVR says 07/09/2001
]
IFTA_STATES = ["Arizona", "New Mexico", "Texas", "Nevada", "Utah", "California", "Colorado", "Oklahoma"]
RATES = [0.26, 0.21, 0.20, 0.27, 0.379, 1.021, 0.325, 0.19]
QUARTERS = {  # period -> miles per state (totals 85,500 / 84,000 / 90,000)
    ("10/01/2025", "12/31/2025", "01/31/2026"): [38420, 9115, 6880, 7240, 5310, 12905, 3460, 2170],
    ("01/01/2026", "03/31/2026", "04/30/2026"): [36950, 8740, 7415, 6980, 4860, 13620, 2995, 2440],
    ("04/01/2026", "06/30/2026", "07/31/2026"): [40115, 9560, 7020, 7795, 5640, 14230, 3105, 2535],
}


# ---------------------------------------------------------------- writers

def text_pdf(path: Path, pages: list[list[str]], mono: bool = False) -> None:
    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(False)
    for lines in pages:
        pdf.add_page()
        y = 15
        for line in lines:
            bold = line.startswith("#")
            text = line.lstrip("# ")
            pdf.set_font("Courier" if mono and not bold else "Helvetica", "B" if bold else "", 13 if bold else 9.5)
            pdf.set_xy(12, y)
            pdf.cell(0, 5, text)
            y += 7 if bold else 5.2
    pdf.output(str(path))


def render_page(lines: list[str], width=1700, height=2200, size=26) -> Image.Image:
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    font, bold = ImageFont.truetype(FONT, size), ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", size + 6)
    y = 110
    for line in lines:
        if line.startswith("#"):
            d.text((110, y), line.lstrip("# "), fill="black", font=bold)
            y += size + 26
        elif "\t" in line:  # a table row: fixed columns, no pipes (as printed)
            for i, cell in enumerate(line.split("\t")):
                d.text((110 + i * 190, y), cell, fill="black", font=font)
            y += size + 16
        else:
            d.text((110, y), line, fill="black", font=font)
            y += size + 16
    return img


def scan(img: Image.Image) -> Image.Image:
    """Photocopy look: grey, slightly rotated, a little blur and speckle."""
    img = img.convert("L").rotate(-1.1, expand=False, fillcolor=245).filter(ImageFilter.GaussianBlur(0.7))
    px = img.load()
    for _ in range(9000):
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = random.choice((120, 170, 210))
    return img


def phone_photo(img: Image.Image) -> Image.Image:
    """Phone photo of a page on a desk: paper on a dark background, tilted, soft focus, uneven light."""
    page = img.rotate(3.6, expand=True, fillcolor=(70, 62, 55)).filter(ImageFilter.GaussianBlur(1.1))
    canvas = Image.new("RGB", (page.width + 220, page.height + 260), (64, 56, 50))
    canvas.paste(page, (110, 140))
    shade = Image.linear_gradient("L").resize(canvas.size).point(lambda v: 70 + v // 3)
    canvas = Image.composite(canvas, Image.new("RGB", canvas.size, (30, 26, 22)), shade.point(lambda v: 255 - v // 6))
    return canvas.resize((canvas.width * 2 // 3, canvas.height * 2 // 3))


# ---------------------------------------------------------------- documents

def application() -> list[list[str]]:
    p1 = [
        "# COMMERCIAL TRUCKING APPLICATION",
        "Copperline Insurance Brokers  |  Producer: Maria Velasquez  |  (602) 555-0187",
        f"Date of application: {APP_DATE}          Proposed effective date: {EFF_DATE}",
        "",
        "# APPLICANT",
        f"Named insured: {INSURED}",
        "Business entity: Limited Liability Company (LLC)",
        "FEIN: 86-4417203          USDOT #: 3318842          MC #: 1129087",
        "Mailing address: 2250 W Buckeye Rd, Phoenix, AZ 85009",
        "Garaging address: same as mailing",
        "Years in business: 6          New venture: No",
        "Operation: For-hire motor carrier, general freight (dry van)",
        "Owner: Ricardo Ochoa          Owner drives: No",
        "",
        "# COVERAGES REQUESTED",
        "Auto liability limit: $1,000,000 CSL          AL deductible: None",
        "Physical damage (APD) quote requested: No",
        "Motor truck cargo requested: Yes          Cargo limit: $100,000",
        "",
        "# OPERATIONS",
        "Hazardous materials hauled: No          Intermodal containers: No",
        "Cross-border (Mexico/Canada): No          Towing / recovery: No",
        "Automobile hauling: No          Team drivers: No",
        "Radius: 0-250 mi 20%   251-500 mi 40%   over 500 mi 40%",
        "States of operation: AZ, NM, TX, NV, UT, CA, CO, OK",
        "Number of power units: 3",
        "",
        "# COMMODITIES HAULED",
        "Commodity                         % of loads     Avg value     Max value",
        "General freight / dry van          60%            $40,000       $90,000",
        "Building materials                 25%            $30,000       $60,000",
        "Paper products                     15%            $20,000       $35,000",
    ]
    p2 = [
        "# PRIOR INSURANCE (last 4 years)",
        "Carrier                          Policy No.     Term                        Lines     Units",
        "Saguaro Mutual Insurance Co      SMA-22-4471    11/20/2022 - 11/20/2023     AL/MTC    2",
        "Saguaro Mutual Insurance Co      SMA-23-4471    11/20/2023 - 11/20/2024     AL/MTC    3",
        "Mesa Specialty Insurance Co      MSP-240088     11/20/2024 - 11/20/2025     AL/MTC    3",
        "Mesa Specialty Insurance Co      MSP-250088     11/20/2025 - 11/20/2026     AL/MTC    3",
        "Loss runs for all terms attached.",
        "",
        "# DRIVERS AND VEHICLES",
        "See attached roster (Driver_Vehicle_Roster.xlsx).",
        "",
        "# SIGNATURE",
        "Signed: Ricardo Ochoa, Managing Member          Date: 10/15/2026",
    ]
    return [p1, p2]


def roster(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Drivers"
    ws.append(["Driver Name", "DOB", "CDL #", "CDL State", "Hire Date", "Years Exp"])
    for row in DRIVERS:
        ws.append(list(row[:3]) + ["AZ"] + list(row[3:]))
    units = wb.create_sheet("Units")
    units.append(["Unit #", "Year", "Make", "Model", "VIN", "Body Type", "Stated Value"])
    for u in UNITS:
        units.append(list(u))
    wb.save(path)


def mvr_pages() -> list[list[str]]:
    people = [
        ("RAMOS, HECTOR", "D04417721", "04/11/1984", "06/02/2022", "04/11/2030", "06/02/2008",
         ["05/03/2025 | 06/10/2025 | SPEEDING 16-20 MPH OVER LIMIT | 3"]),
        ("WHITAKER, JAMES T", "D07739120", "12/02/1990", "01/20/2022", "12/02/2027", "01/20/2014",
         ["02/14/2024 | 05/20/2024 | DUI - EXTREME (BAC .15+) | 8"]),
        ("FERNANDEZ, LUCIA", "D09921844", "07/09/2001", "03/15/2023", "07/09/2028", "03/15/2023", []),
    ]
    pages = []
    for name, lic, dob, issued, expires, cdl_orig, convictions in people:
        pages.append([
            "# ARIZONA MOTOR VEHICLE DIVISION - DRIVER RECORD (39 MONTH)",
            "Requested by: Copperline Insurance Brokers      Report Date: 10/02/2026      Order #: AZ-77120",
            "",
            f"Name: {name}",
            f"License #: {lic}     State: AZ     Class: A     Type: COMMERCIAL",
            f"Status: VALID     Issued: {issued}     Expires: {expires}",
            f"Date of Birth: {dob}",
            f"CDL Original Issue: {cdl_orig}",
            "Endorsements: N, T          Restrictions: NONE",
            "",
            "# CONVICTIONS / VIOLATIONS",
            "Viol Date | Conv Date | Description | Points",
            *(convictions or ["NO CONVICTIONS ON RECORD"]),
            "",
            f"*** END OF RECORD FOR {name} ***",
        ])
    return pages


def loss_run_lines(carrier, policy, term, run_date, claims, label="LOSS HISTORY REPORT", line="COMMERCIAL AUTO LIABILITY"):
    lines = [f"# {carrier}", f"# {label}", f"Valuation Date: {run_date}", f"Insured: {INSURED}",
             f"Policy No.: {policy}     Line of coverage: {line}", f"Policy Term: {term}", ""]
    if claims:
        lines += ["Claim No. | Date of Loss | Reported | Driver | Unit / VIN | Description | Status | Paid | Reserve | Incurred"]
        lines += claims
        lines += ["", "TOTALS: Claims 1 | Open 0 | Paid $8,400 | Reserve $0 | Incurred $8,400"]
    else:
        lines += ["TOTALS: Claims 0 | Open 0 | Paid $0 | Reserve $0 | Incurred $0",
                  "No losses reported for this policy term as of the valuation date."]
    return lines


def ifta_lines(period, miles, headers, scanned=False):
    begin, end, due = period
    sep = "\t" if scanned else " | "
    rows, tax_total, paid_total, taxable_total = [], 0.0, 0, 0
    for state, mi, rate in zip(IFTA_STATES, miles, RATES):
        taxable_gal = round(mi / 6.4)
        paid = round(taxable_gal * random.uniform(0.15, 1.3)) if state != "Arizona" else round(taxable_gal * 2.9)
        tax = round((taxable_gal - paid) * rate, 2)
        tax_total, paid_total, taxable_total = tax_total + tax, paid_total + paid, taxable_total + taxable_gal
        rows.append(sep.join([state, f"{mi:,}", f"{mi:,}", f"{taxable_gal:,}", f"{paid:,}", f"{taxable_gal - paid:,}", f"{rate:.4f}", f"${tax:,.2f}"]))
    total_mi = sum(miles)
    lines = ["# ARIZONA DEPARTMENT OF TRANSPORTATION", "# IFTA QUARTERLY FUEL TAX RETURN",
             f"Licensee: {INSURED}          IFTA Account: AZ-IFTA-118842",
             f"Reporting Period: {begin} - {end}          Due Date: {due}", "",
             sep.join(headers), *rows,
             sep.join(["TOTALS", f"{total_mi:,}", f"{total_mi:,}", f"{taxable_total:,}", f"{paid_total:,}",
                       f"{taxable_total - paid_total:,}", "", f"${tax_total:,.2f}"]),
             "", f"Net tax due: ${max(tax_total, 0):,.2f}     Penalty: $0.00     Interest: $0.00     Balance due: ${max(tax_total, 0):,.2f}"]
    return lines


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}

    files["Application_DesertRidge.pdf"] = OUT / "Application_DesertRidge.pdf"
    text_pdf(files["Application_DesertRidge.pdf"], application())

    files["Driver_Vehicle_Roster.xlsx"] = OUT / "Driver_Vehicle_Roster.xlsx"
    roster(files["Driver_Vehicle_Roster.xlsx"])

    files["Driver_Reports.pdf"] = OUT / "Driver_Reports.pdf"
    text_pdf(files["Driver_Reports.pdf"], mvr_pages(), mono=True)

    # loss runs: Saguaro as a PDF with a misleading name, valued 75 days before the application (stale)
    files["scan001.pdf"] = OUT / "scan001.pdf"
    text_pdf(files["scan001.pdf"], [
        loss_run_lines("SAGUARO MUTUAL INSURANCE CO.", "SMA-22-4471", "11/20/2022 to 11/20/2023", "08/01/2026", []),
        loss_run_lines("SAGUARO MUTUAL INSURANCE CO.", "SMA-23-4471", "11/20/2023 to 11/20/2024", "08/01/2026", []),
    ])
    # Mesa 24-25 as a phone photo, with one closed claim on a scheduled driver and unit
    claim = f"24-AL-1187 | 03/12/2025 | 03/13/2025 | HECTOR RAMOS | 2021 KENWORTH {UNITS[0][4]} | Rear-end collision, insured vehicle struck | CLOSED | $8,400 | $0 | $8,400"
    photo = phone_photo(render_page(loss_run_lines("MESA SPECIALTY INSURANCE COMPANY", "MSP-240088", "11/20/2024 - 11/20/2025",
                                                    "10/05/2026", [claim], label="LOSS RUN", line="AUTO LIABILITY (AL)"), size=24))
    files["IMG_4471.jpg"] = OUT / "IMG_4471.jpg"
    photo.save(files["IMG_4471.jpg"], quality=72)
    files["MesaSpecialty_LossRun_2025.pdf"] = OUT / "MesaSpecialty_LossRun_2025.pdf"
    text_pdf(files["MesaSpecialty_LossRun_2025.pdf"], [loss_run_lines(
        "MESA SPECIALTY INSURANCE COMPANY", "MSP-250088", "11/20/2025 - 11/20/2026", "10/05/2026", [],
        label="LOSS RUN", line="AUTO LIABILITY (AL)")])

    # IFTA: Q3 2025 deliberately missing; Q1 2026 is an image-only scan with abbreviated headers
    full = ["Jurisdiction", "Total Miles", "Taxable Miles", "Taxable Gallons", "Tax-Paid Gallons", "Net Taxable Gallons", "Rate", "Tax Due"]
    short = ["Juris.", "Total Mi.", "Taxable Mi.", "Taxable Gal.", "Tax Pd Gal.", "Net Gal.", "Rate", "Tax"]
    (q4, q1, q2) = QUARTERS.items()
    files["AZ_IFTA_Q4-2025.pdf"] = OUT / "AZ_IFTA_Q4-2025.pdf"
    text_pdf(files["AZ_IFTA_Q4-2025.pdf"], [ifta_lines(q4[0], q4[1], full)])
    files["fuel_tax_Q1.pdf"] = OUT / "fuel_tax_Q1.pdf"
    scan(render_page(ifta_lines(q1[0], q1[1], short, scanned=True), size=24)).save(files["fuel_tax_Q1.pdf"], resolution=200)
    files["IFTA_2026Q2.pdf"] = OUT / "IFTA_2026Q2.pdf"
    text_pdf(files["IFTA_2026Q2.pdf"], [ifta_lines(q2[0], q2[1], full)])

    # a corrupt attachment: a real PDF cut off part-way
    whole = OUT / "_whole.pdf"
    text_pdf(whole, [["# SUPPLEMENTAL QUESTIONNAIRE", "Safety program: weekly toolbox talks", "ELD provider: Motive"]])
    files["Supplemental_Questionnaire.pdf"] = OUT / "Supplemental_Questionnaire.pdf"
    files["Supplemental_Questionnaire.pdf"].write_bytes(whole.read_bytes()[: len(whole.read_bytes()) // 3])
    whole.unlink()

    msg = EmailMessage()
    msg["From"] = "Maria Velasquez <mvelasquez@copperline.example>"
    msg["To"] = "submissions@knight-casestudy.example"
    msg["Subject"] = "Submission: Desert Ridge Freight LLC - eff 11/20/2026"
    msg["Date"] = "Thu, 15 Oct 2026 10:02:00 -0700"
    msg.set_content("Hello,\n\nNew business submission for Desert Ridge Freight LLC (Phoenix, AZ), effective 11/20/2026: "
                    "application, roster, MVRs, loss runs (one is a phone photo, sorry) and IFTA returns attached.\n\n"
                    "Maria Velasquez\nCopperline Insurance Brokers\n")
    types = {".pdf": ("application", "pdf"), ".jpg": ("image", "jpeg"),
             ".xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    for name, path in files.items():
        main_t, sub_t = types[path.suffix]
        msg.add_attachment(path.read_bytes(), maintype=main_t, subtype=sub_t, filename=name)
    EML.write_bytes(bytes(msg))
    print(f"wrote {len(files)} files to {OUT.relative_to(ROOT)} and {EML.relative_to(ROOT)} ({EML.stat().st_size:,} bytes)")
    for u in UNITS:
        print("VIN", u[4], vin_check(u[4]))


if __name__ == "__main__":
    main()
