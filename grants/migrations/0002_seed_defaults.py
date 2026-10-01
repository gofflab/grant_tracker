"""Starter reference data: common funders, checklist templates and critique themes.

Everything here is editable in Settings; nothing is required.
"""

from django.db import migrations

FUNDERS = [
    ("National Institutes of Health", "NIH", "federal", "https://grants.nih.gov", "nih_snap"),
    ("National Science Foundation", "NSF", "federal", "https://www.nsf.gov/funding", "annual"),
    ("Department of Defense, CDMRP", "DoD CDMRP", "federal", "https://cdmrp.health.mil", "annual"),
    ("Howard Hughes Medical Institute", "HHMI", "foundation", "https://www.hhmi.org", "annual"),
    ("Simons Foundation", "Simons", "foundation", "https://www.simonsfoundation.org", "annual"),
    ("Chan Zuckerberg Initiative", "CZI", "foundation", "https://chanzuckerberg.com/science/", "annual"),
    ("Whitehall Foundation", "Whitehall", "foundation", "https://www.whitehall.org", "annual"),
    ("Alfred P. Sloan Foundation", "Sloan", "foundation", "https://sloan.org", "annual"),
    ("McKnight Foundation", "McKnight", "foundation", "https://www.mcknight.org", "annual"),
    ("Internal / institutional funding", "Internal", "institutional", "", "final"),
]

# (title, category, anchor, offset_days)
NIH_RESEARCH = [
    ("Confirm NOFO, mechanism and eligibility; contact program officer", "admin", "sponsor", -90),
    ("Draft Specific Aims", "writing", "sponsor", -75),
    ("Get feedback on Specific Aims (colleagues / PO)", "writing", "sponsor", -60),
    ("Request letters of support from collaborators", "letters", "sponsor", -60),
    ("Open institutional proposal record with grants office", "admin", "sponsor", -45),
    ("Draft Research Strategy", "writing", "sponsor", -45),
    ("Draft budget and budget justification", "budget", "sponsor", -30),
    ("Update biosketches for all key personnel", "documents", "sponsor", -30),
    ("Data Management & Sharing Plan", "documents", "sponsor", -30),
    ("Facilities & resources and equipment", "documents", "sponsor", -21),
    ("Project Summary, Narrative and bibliography", "writing", "sponsor", -21),
    ("Other attachments (key resources, protocols, as applicable)", "documents", "sponsor", -21),
    ("Internal mock review of full draft", "writing", "sponsor", -14),
    ("Admin sections and final budget to grants office", "admin", "internal", 0),
    ("Final science sections to grants office", "admin", "sponsor", -3),
    ("Confirm submission; check for errors and warnings", "admin", "sponsor", 0),
    ("Check application image in eRA Commons (viewing window)", "admin", "sponsor", 2),
]

NSF_PROPOSAL = [
    ("Confirm solicitation, program and eligibility; contact program officer", "admin", "sponsor", -90),
    ("Draft Project Summary (overview, intellectual merit, broader impacts)", "writing", "sponsor", -60),
    ("Draft Project Description", "writing", "sponsor", -60),
    ("Request letters of collaboration", "letters", "sponsor", -45),
    ("Open institutional proposal record with grants office", "admin", "sponsor", -45),
    ("Budget and budget justification", "budget", "sponsor", -30),
    ("Biosketches and Current & Pending (SciENcv)", "documents", "sponsor", -30),
    ("Collaborators & Other Affiliations (COA)", "documents", "sponsor", -30),
    ("Data Management Plan", "documents", "sponsor", -21),
    ("Mentoring plan (if postdocs or grad students budgeted)", "documents", "sponsor", -21),
    ("Facilities, equipment and other resources", "documents", "sponsor", -21),
    ("Internal review of full draft", "writing", "sponsor", -14),
    ("Final package to grants office", "admin", "internal", 0),
    ("Confirm submission in Research.gov", "admin", "sponsor", 0),
]

FOUNDATION = [
    ("Review guidelines; confirm eligibility and any institutional nomination", "admin", "sponsor", -45),
    ("Draft proposal narrative", "writing", "sponsor", -30),
    ("Request letters of recommendation / support", "letters", "sponsor", -30),
    ("Budget", "budget", "sponsor", -21),
    ("Institutional approval and signatures", "admin", "internal", 0),
    ("Final proofread", "writing", "sponsor", -2),
    ("Submit and save confirmation", "admin", "sponsor", 0),
]

AWARD_SETUP = [
    ("Review Notice of Award terms and conditions", "admin", "award_start", 0),
    ("Upload Notice of Award to documents", "documents", "award_start", 3),
    ("Confirm account / cost center is set up", "admin", "award_start", 7),
    ("Confirm personnel effort allocations with department", "effort", "award_start", 14),
    ("Set up subawards and consultant agreements (if any)", "admin", "award_start", 14),
    ("Kick-off meeting with project team", "meeting", "award_start", 30),
    ("Set up data repositories per Data Management & Sharing Plan", "compliance", "award_start", 30),
    ("Plan renewal / competing continuation", "writing", "award_end", -540),
    ("Decide on no-cost extension", "admin", "award_end", -120),
    ("Close-out planning: final spending, equipment, final reports", "admin", "award_end", -60),
]

TEMPLATES = [
    ("NIH research grant (R01, R21, R03)", "Typical NIH research-grant preparation timeline", "submission", "R01, R21, R03, R15, U01", False, NIH_RESEARCH),
    ("NSF proposal", "Typical NSF full-proposal timeline", "submission", "", False, NSF_PROPOSAL),
    ("Foundation proposal", "Shorter timeline for private foundations", "submission", "", False, FOUNDATION),
    ("Award setup & close-out", "Applied automatically when an application is marked Awarded", "award", "", True, AWARD_SETUP),
]

CRITIQUE_THEMES = [
    ("Preliminary data", "amber"),
    ("Feasibility", "orange"),
    ("Overly ambitious", "red"),
    ("Significance", "blue"),
    ("Innovation", "purple"),
    ("Rigor / statistics", "teal"),
    ("Alternative approaches", "green"),
    ("Model system justification", "pink"),
    ("Investigator expertise", "gray"),
    ("Clarity / organization", "gray"),
]


def seed(apps, schema_editor):
    Funder = apps.get_model("grants", "Funder")
    Template = apps.get_model("grants", "ChecklistTemplate")
    Item = apps.get_model("grants", "ChecklistItem")
    Tag = apps.get_model("grants", "Tag")

    for name, short, kind, url, reporting in FUNDERS:
        Funder.objects.get_or_create(
            name=name, defaults={"short_name": short, "funder_type": kind, "website": url, "default_reporting": reporting}
        )
    for name, desc, applies, mechs, default, items in TEMPLATES:
        template, created = Template.objects.get_or_create(
            name=name, defaults={"description": desc, "applies_to": applies, "mechanisms": mechs, "is_default": default}
        )
        if created:
            Item.objects.bulk_create(
                Item(template=template, title=t, category=c, anchor=a, offset_days=o, order=i)
                for i, (t, c, a, o) in enumerate(items)
            )
    for name, color in CRITIQUE_THEMES:
        Tag.objects.get_or_create(name=name, kind="critique", defaults={"color": color})


class Migration(migrations.Migration):
    dependencies = [("grants", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
