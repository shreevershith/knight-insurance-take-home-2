"""Build the simulated agent email: the sample-submission zip attached to an .eml."""
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "candidate_materials" / "Case Study - Commercial Auto" / "Case Study - Commercial Auto (candidate pack)"
ZIP = PACK / "Sample Submission - Cobalt Mesa Transport.zip"
OUT = ROOT / "samples" / "cobalt_mesa_submission.eml"


def main() -> None:
    msg = EmailMessage()
    msg["From"] = "Daniel Okafor <dokafor@baylineinsurance.example>"
    msg["To"] = "submissions@knight-casestudy.example"
    msg["Subject"] = "New submission - COBALT MESA TRANSPORT INC - eff 12/15/2026"
    msg["Date"] = "Thu, 22 Oct 2026 09:14:00 -0700"
    msg.set_content(
        "Hi team,\n\nPlease find attached the submission for Cobalt Mesa Transport Inc, "
        "effective 12/15/2026. App, quote form, driver and vehicle lists, MVRs, loss runs "
        "and IFTAs are in the zip.\n\nThanks,\nDaniel Okafor\nBayline Insurance Agency Inc.\n")
    msg.add_attachment(ZIP.read_bytes(), maintype="application", subtype="zip", filename=ZIP.name)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(bytes(msg))
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
