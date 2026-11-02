"""Build held-out submission #4 (D-46): a fictitious Texas carrier whose agent email reaches Knight forwarded
as an attachment by a CSR. Written after the answer key (eval/heldout/redriver_expected.json); run once.

Planted (see the answer key): lithium-ion batteries in the commodities, cross-border operations answered Yes,
a DUI 26 months before the application, a 67-year-old driver, Texas miles, miles in OK and LA. Edge case:
'Paper towels and tissue' among the commodities, which is not towing. Driver and vehicle schedules are two
sheets of one workbook.

Usage: .venv/Scripts/python.exe scripts/make_heldout4.py
"""
import io
import sys
from email.message import EmailMessage
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from make_heldout import vin  # noqa: E402
from make_heldout2 import pdf_bytes  # noqa: E402

OUT = ROOT / "eval" / "heldout" / "redriver"
EML = ROOT / "eval" / "heldout" / "redriver.eml"
INSURED = "RED RIVER CARTAGE LLC"
STATES = ["Texas", "Oklahoma", "Arkansas", "Louisiana"]
SHARES = [0.55, 0.20, 0.15, 0.10]
QUARTERS = [("07/01/2025", "09/30/2025", "10/31/2025", 198000), ("10/01/2025", "12/31/2025", "01/31/2026", 189000),
            ("01/01/2026", "03/31/2026", "04/30/2026", 204000), ("04/01/2026", "06/30/2026", "07/31/2026", 210000)]
YEAR_CODE = {2018: "J", 2019: "K", 2020: "L", 2021: "M", 2022: "N", 2023: "P"}
MAKES = [("FREIGHTLINER", "CASCADIA", "3AKJHHDR?{y}SL{s:05d}"), ("KENWORTH", "T680", "1XKYD49X?{y}J{s:06d}"),
         ("PETERBILT", "579", "1XPBD49X?{y}D{s:06d}"), ("VOLVO", "VNL 760", "4V4NC9EH?{y}N{s:06d}")]
UNITS = []
for i in range(8):
    year = 2018 + i % 6
    make, model, template = MAKES[i % 4]
    UNITS.append((str(i + 1), str(year), make, model, vin(template.format(y=YEAR_CODE[year], s=52081 + 1543 * i)),
                  112000 + 7000 * (year - 2018) + 2500 * (i % 2)))
# first, last, DOB, TX license, hire, original CDL issue, expiration, violations [(date, description, points)]
DRIVERS = [
    ("RAMON", "DELGADO", "03/15/1959", "062318447", "04/01/2015", "06/20/1988", "03/15/2030", []),
    ("TYLER", "BRANNON", "07/22/1990", "118204563", "02/14/2020", "05/10/2014", "07/22/2031",
     [("08/12/2024", "DUI - DRIVING WHILE INTOXICATED", "8")]),
    ("KEISHA", "MONROE", "11/03/1985", "093377120", "09/09/2018", "09/01/2011", "11/03/2029", []),
    ("JORGE", "AVILA", "01/19/1979", "077465902", "06/30/2016", "03/12/2002", "01/19/2032", []),
]
TERMS = [("Bluebonnet Casualty Company", "BC-6610238", "11/15/2022", "11/15/2023", 6),
         ("Bluebonnet Casualty Company", "BC-7720554", "11/15/2023", "11/15/2024", 7),
         ("Bluebonnet Casualty Company", "BC-8840091", "11/15/2024", "11/15/2025", 8),
         ("Bluebonnet Casualty Company", "BC-9930417", "11/15/2025", "11/15/2026", 8)]


def application() -> list:
    return [[
        "# COMMERCIAL AUTO INSURANCE APPLICATION - MOTOR CARRIER",
        "Agency: Prairie Wind Insurance Agency     Producer: Monica Reyes     (903) 555-0117",
        "Application date: 10/12/2026     Requested effective date: 11/15/2026",
        "",
        "# APPLICANT",
        f"Legal name: {INSURED}     Form of business: LLC",
        "FEIN: 75-4418290     USDOT No.: 2875510     MC No.: 984412",
        "Mailing address: 4100 W 7th St",
        "City: Texarkana     State: TX     ZIP: 75501",
        "Years in business: 12     New venture: No",
        "",
        "# COVERAGE",
        "Auto liability: $1,000,000 CSL     Auto liability deductible: None",
        "Physical damage (APD) quote requested: No     Motor truck cargo: Yes, $100,000",
        "",
        "# OPERATIONS",
        "Hazardous materials: No     Intermodal / containers: No",
        "Cross-border (Mexico) operations: Yes - occasional loads to Monterrey",
        "Towing or recovery operations: No     Hauls automobiles: No",
        "Radius: long haul 60%, intermediate 40%     States operated: TX, OK, AR, LA",
        "Power units: 8     Drivers: 4",
        "",
        "# COMMODITIES",
        "Commodity                        Percent",
        "Building materials               45%",
        "Paper towels and tissue          25%",
        "Canned foods                     20%",
        "Lithium-ion batteries            10%",
    ], [
        "# PRIOR INSURANCE (last 4 years)",
        "Insurer                       Policy number   Coverage         Effective    Expiration   Units",
        *[f"{c}   {p}      Auto liability   {s}   {e}   {u}" for c, p, s, e, u in TERMS],
        "",
        "# DRIVERS AND EQUIPMENT",
        "Per attached schedules (one workbook, two sheets).",
        "",
        "Signature: Dale Whitley, Managing Member     Date: 10/12/2026",
    ]]


def schedules() -> bytes:
    wb = openpyxl.Workbook()
    drivers = wb.active
    drivers.title = "Drivers"
    drivers.append(["First Name", "Last Name", "Date of Birth", "License #", "License State", "Date of Hire"])
    for first, last, dob, lic, hire, *_ in DRIVERS:
        drivers.append([first, last, dob, lic, "TX", hire])
    units = wb.create_sheet("Units")
    units.append(["Unit #", "Year", "Make", "Model", "VIN", "Type", "Stated Value"])
    for u, y, mk, md, v, sv in UNITS:
        units.append([u, y, mk, md, v, "TRACTOR", sv])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def mvrs() -> list:
    pages = []
    for first, last, dob, lic, _hire, cdl, exp, violations in DRIVERS:
        pages.append([
            "# TEXAS DEPARTMENT OF PUBLIC SAFETY",
            "# DRIVER RECORD - CERTIFIED ABSTRACT",
            "Date ordered: 09/28/2026     Requested by: PRAIRIE WIND INS AGENCY",
            "",
            f"Driver: {last}, {first}          DOB: {dob}",
            f"DL Number: {lic}     Class: A (COMMERCIAL)     Status: VALID     Expiration: {exp}",
            f"CDL Issue Date: {cdl}     Endorsements: NONE     Restrictions: NONE",
            "",
            "# VIOLATIONS/CONVICTIONS",
            "Date         Description                              Points",
            *([f"{d}   {desc:40} {pts}" for d, desc, pts in violations] or ["NONE"]),
            "",
            f"Total points (3 yr): {sum(int(p) for _, _, p in violations)}",
        ])
    return pages


def loss_runs() -> list:
    return [[
        f"# {carrier.upper()}",
        "# LOSS RUN REPORT",
        "Valuation date: 09/30/2026",
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
        rows = [[st, f"{mi:,}", f"{mi:,}", f"{round(mi / 6.3):,}", f"{round(mi / 6.3):,}", "0.00"]
                for st, mi in zip(STATES, miles)]
        pages.append(["# TEXAS COMPTROLLER OF PUBLIC ACCOUNTS", "# IFTA QUARTERLY FUEL TAX RETURN",
                      f"Licensee: {INSURED}     IFTA Lic. No.: TX-8840212",
                      f"Quarter: {begin} through {end}     Due: {due}", ""]
                     + [" | ".join(r) for r in [head] + rows + [["TOTAL", f"{total:,}", f"{total:,}", "", "", "0.00"]]])
    return pages


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "Red River - Application.pdf": pdf_bytes(application()),
        "Red River schedules.xlsx": schedules(),
        "MVRs.pdf": pdf_bytes(mvrs()),
        "Loss runs 2022-2026.pdf": pdf_bytes(loss_runs()),
        "IFTA 2025-2026.pdf": pdf_bytes(ifta()),
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)

    agent = EmailMessage()   # the agent's original email, as it reached the CSR
    agent["From"] = "Monica Reyes <mreyes@prairiewind.example>"
    agent["To"] = "pnguyen@knight-casestudy.example"
    agent["Subject"] = "Red River Cartage LLC - new business - eff 11/15/2026"
    agent["Date"] = "Mon, 12 Oct 2026 09:05:00 -0500"
    agent.set_content("Hi Pat,\n\nNew business for Red River Cartage LLC (Texarkana, TX), 8 units, eff 11/15/2026. "
                      "Application, schedules, MVRs, 4 years of loss runs and IFTAs attached. They run some loads "
                      "into Mexico, noted on the app.\n\nMonica Reyes\nPrairie Wind Insurance Agency\n")
    kinds = {"pdf": ("application", "pdf"), "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    for name, data in files.items():
        kind = kinds[name.rsplit(".", 1)[1]]
        agent.add_attachment(data, maintype=kind[0], subtype=kind[1], filename=name)

    forward = EmailMessage()  # the CSR forwards it to the submissions inbox as an attachment
    forward["From"] = "Pat Nguyen <pnguyen@knight-casestudy.example>"
    forward["To"] = "submissions@knight-casestudy.example"
    forward["Subject"] = "FW: Red River Cartage LLC - new business"
    forward["Date"] = "Mon, 12 Oct 2026 10:20:00 -0500"
    forward.set_content("Forwarding a submission that came to my inbox instead of submissions@. Original attached.\n\nPat\n")
    forward.add_attachment(agent)
    EML.write_bytes(bytes(forward))
    print(f"wrote {len(files)} files to {OUT.relative_to(ROOT)} and {EML.relative_to(ROOT)} ({EML.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
