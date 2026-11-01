"""What to extract from each document type. One catalogue drives the LLM schema, the prompt,
entity resolution and the UI. Adding a document type = adding an entry here, not a parser.

Each group: entity type, single or list, and {field: description}.
Values are extracted exactly as printed; normalization happens in code (normalize.py).
"""

APPLICANT = {
    "business_name": "Named insured / applicant / taxpayer legal name, exactly as printed",
    "dba": "DBA name if any",
    "entity_type": "Corporation, LLC, Sole Proprietorship, Partnership...",
    "fein": "FEIN / Tax ID (or SSN for individuals)",
    "dot_number": "USDOT number",
    "mc_number": "MC / docket number",
    "mailing_address": "Mailing street address",
    "mailing_city": "Mailing city",
    "mailing_state": "Mailing state (2-letter)",
    "mailing_zip": "Mailing ZIP",
    "garaging_address": "Physical / garaging address (full)",
    "phone": "Applicant phone",
    "email": "Applicant email",
    "contact_name": "Primary contact / owner name",
    "years_in_business": "Years in business / experience operating",
    "years_prior_insurance": "Years of prior insurance",
    "operation_type": "For hire / private / etc.",
    "description_of_operation": "Description of operations",
    "application_date": "Date of application",
    "effective_date": "Requested effective date",
    "new_venture": "Is this a new venture (yes/no)",
    "owner_is_driver": "Is the owner a driver (yes/no)",
    "agency_name": "Submitting agency name",
    "agency_phone": "Agency phone",
    "agency_contact": "Agency contact person",
    "signed": "Is the application signed (yes/no); empty if no signature block",
}

COVERAGE = {
    "al_limit": "Auto liability limit",
    "al_deductible": "Auto liability deductible requested ('Do not offer'/none if none)",
    "um_limit": "Uninsured/underinsured motorist limit",
    "apd_requested": "Is a physical damage (APD) quote requested (yes/no)",
    "apd_deductible": "Physical damage deductible",
    "cargo_requested": "Is a motor truck cargo quote requested (yes/no)",
    "cargo_limit": "Cargo limit requested",
    "reefer_breakdown": "Reefer breakdown coverage requested / reefer used (yes/no)",
    "trailer_interchange_limit": "Trailer interchange limit",
}

OPERATIONS = {
    "hazmat": "Hauls hazardous materials (yes/no)",
    "intermodal": "Hauls intermodal containers (yes/no)",
    "cross_border": "Cross-border operations (yes/no)",
    "doubles_triples": "Pulls doubles/triples (yes/no)",
    "oversize": "Oversize/overweight loads (yes/no)",
    "team_driving": "Team driving (yes/no)",
    "freight_broker": "Acts as freight broker / forwarder (yes/no)",
    "workers_comp": "Carries workers compensation (yes/no)",
    "sub_haulers": "Hires sub-haulers (yes/no)",
    "unlisted_vehicles": "Are there vehicles NOT listed on the application (yes/no)",
    "towing": "Towing / recovery operations (yes/no)",
    "auto_hauler": "Hauls automobiles (yes/no)",
    "telematics_pct": "Share of vehicles with telematics",
    "radius_summary": "Radius / operating range distribution exactly as printed (all buckets and %)",
    "farthest_state": "Farthest territory / state traveled",
    "states_of_operation": "States or area of operation as stated (e.g. '48 STATES')",
}

COMMODITY = {
    "commodity": "Commodity name",
    "percent": "Percent of loads",
    "average_value": "Average or typical value",
    "max_value": "Maximum value (if given)",
}

PRIOR_POLICY = {
    "term_start": "Policy term start date",
    "term_end": "Policy term end date",
    "carrier": "Carrier name",
    "coverage_lines": "Coverage lines (e.g. LIAB/PHD/MTC)",
    "policy_number": "Policy number",
    "units": "Number of units insured",
    "loss_runs_note": "What the row says about losses / loss runs, e.g. 'LOSS RUNS ATTACHED'",
}

DRIVER = {
    "full_name": "Driver full name as printed",
    "first_name": "First name (with middle name/initial if printed with it)",
    "last_name": "Last name",
    "dob": "Date of birth",
    "license_number": "Driver license number",
    "license_state": "License state",
    "license_class": "License class (A, B, C...)",
    "commercial_license": "Is it a commercial (CDL) license (yes/no)",
    "license_status": "License status (VALID, SUSPENDED, ...)",
    "license_issue_date": "License issue date",
    "license_expiration": "License expiration date",
    "first_licensed_year": "Approximate year first licensed",
    "cdl_issue_date": "Date the CDL was issued, if a history entry shows it",
    "hire_date": "Date of hire (or role such as OWNER if printed instead)",
    "years_experience": "Years of experience / class A experience as printed",
    "driver_type": "Company driver / owner / excluded",
    "mvr_order_date": "MVR order / report date",
    "mvr_marked_old": "Is this MVR marked as old/superseded on the page (yes/no)",
    "address": "Driver address",
}

VIOLATION = {
    "event_type": "VIOL / CONV / ACCD / suspension etc.",
    "violation_date": "Violation or accident date",
    "conviction_date": "Conviction date if given",
    "description": "Description",
    "points": "Points",
    "at_fault": "At fault (Y/N) if stated",
    "commercial_vehicle": "Commercial vehicle flag if stated",
}

LICENSE_EVENT = {
    "date": "Event date",
    "event": "Event text, e.g. CDL DOWNGRADE NOTICE, CDL ISSUED, CDL TRANSFER FROM PA",
}

VEHICLE = {
    "unit_number": "Unit / vehicle number",
    "year": "Model year",
    "make": "Make",
    "model": "Model",
    "vin": "VIN exactly as printed",
    "vehicle_type": "Tractor, straight truck, box truck, trailer...",
    "gvw": "Gross vehicle weight",
    "cost_new": "Original cost new",
    "stated_value": "Stated value",
    "garaging_zip": "Garaging ZIP",
    "registered_state": "Registration state",
}

LOSS_RUN = {
    "carrier": "Insurance carrier / issuer",
    "insured_name": "Name of insured",
    "policy_number": "Policy number",
    "line": "Coverage line of this loss run: AL (auto liability), MTC (cargo) or APD",
    "term_start": "Policy period start",
    "term_end": "Policy period end",
    "run_date": "Run date / valuation date / letter date",
    "total_claims": "Total number of claims",
    "open_claims": "Open claims",
    "total_paid": "Total losses paid",
    "total_reserved": "Total reserved",
    "total_incurred": "Total incurred",
    "deductible": "Deductible",
    "no_loss_statement": "Exact no-loss / no-claims statement if present",
}

CLAIM = {
    "claim_number": "Claim number",
    "status": "Open/Closed",
    "date_of_loss": "Date of loss",
    "date_reported": "Date reported",
    "late_reported": "Late reported (Y/N)",
    "driver_name": "Driver name",
    "driver_dob": "Driver date of birth",
    "driver_reported": "Driver status (Reported/Unreported)",
    "vehicle": "Vehicle year/make",
    "vin": "VIN",
    "unit_reported": "Unreported unit flag as printed (Y/N)",
    "location": "Accident location",
    "description": "Loss description",
    "paid": "Claim total paid",
    "reserve": "Claim total reserves",
    "incurred": "Claim total incurred",
}

IFTA = {
    "taxpayer_name": "Taxpayer name",
    "account_number": "IFTA account number",
    "period_begin": "Period begin",
    "period_end": "Period end / filing period",
    "due_date": "Due date",
    "total_miles": "Total miles (table total row)",
    "total_taxable_gallons": "Total taxable gallons",
    "subtotal_due": "Subtotal amount due or credit",
    "penalty": "Penalty",
    "interest": "Interest",
    "total_due": "Total balance due",
}

IFTA_ROW = {
    "jurisdiction": "Jurisdiction (state/province) name",
    "surcharge": "Surcharge marker if any",
    "total_miles": "Total miles",
    "taxable_miles": "Taxable miles",
    "tax_paid_gallons": "Tax paid gallons",
    "total": "Row total amount",
}

# doc_type -> {group_name: (entity_type, is_list, fields)}
SPEC: dict[str, dict[str, tuple[str, bool, dict]]] = {
    "APPLICATION": {
        "applicant": ("applicant", False, APPLICANT),
        "coverage": ("coverage", False, COVERAGE),
        "operations": ("operations", False, OPERATIONS),
        "commodities": ("commodity", True, COMMODITY),
    },
    "QUOTE_FORM": {
        "applicant": ("applicant", False, APPLICANT),
        "coverage": ("coverage", False, COVERAGE),
        "operations": ("operations", False, OPERATIONS),
        "commodities": ("commodity", True, COMMODITY),
        "prior_policies": ("prior_policy", True, PRIOR_POLICY),
        "drivers": ("driver", True, DRIVER),
        "vehicles": ("vehicle", True, VEHICLE),
    },
    "DRIVER_SCHEDULE": {"drivers": ("driver", True, DRIVER), "vehicles": ("vehicle", True, VEHICLE)},
    "VEHICLE_SCHEDULE": {"drivers": ("driver", True, DRIVER), "vehicles": ("vehicle", True, VEHICLE)},
    "MVR": {
        "driver": ("driver", False, DRIVER),
        "violations": ("violation", True, VIOLATION),
        "license_events": ("license_event", True, LICENSE_EVENT),
    },
    "DRIVER_LICENSE": {"driver": ("driver", False, DRIVER)},
    "LOSS_RUN": {"loss_run": ("loss_run", False, LOSS_RUN), "claims": ("claim", True, CLAIM)},
    "IFTA_RETURN": {"ifta": ("ifta", False, IFTA), "jurisdictions": ("ifta_row", True, IFTA_ROW)},
}

# Fields compared across documents for conflicts (others are per-document facts).
COMPARABLE = {
    "applicant": {"business_name", "entity_type", "fein", "dot_number", "mc_number", "mailing_address",
                  "mailing_city", "mailing_zip", "phone", "years_in_business", "years_prior_insurance",
                  "owner_is_driver", "agency_name", "agency_phone", "effective_date"},
    "coverage": {"al_limit", "um_limit", "apd_requested", "cargo_requested", "cargo_limit", "reefer_breakdown"},
    "operations": {"hazmat", "intermodal", "cross_border", "doubles_triples", "oversize", "team_driving",
                   "freight_broker", "farthest_state"},
    "driver": {"full_name", "dob", "license_number", "license_state", "license_class", "commercial_license",
               "license_expiration", "hire_date"},
    "vehicle": {"year", "make", "vin"},
}
