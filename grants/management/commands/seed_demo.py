"""Load fictional demo data so you can explore the interface.

    python manage.py seed_demo --user <username>

Everything created is fictional and tagged "demo". Remove it with --remove.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from grants import services
from grants.models import (
    Application,
    ChecklistTemplate,
    Comment,
    CriterionScore,
    Document,
    Funder,
    Opportunity,
    Person,
    Personnel,
    ReviewFeedback,
    Tag,
    Task,
)

DEMO_TAG = "demo"


class Command(BaseCommand):
    help = "Create (or remove) fictional demo data."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Username to link as the demo PI (defaults to the first owner).")
        parser.add_argument("--remove", action="store_true", help="Delete all demo data.")

    def handle(self, *args, user=None, remove=False, **opts):
        if remove:
            return self.remove()
        if Tag.objects.filter(name=DEMO_TAG, kind=Tag.Kind.TOPIC).exists():
            raise CommandError("Demo data already loaded. Run with --remove first.")
        owner = User.objects.filter(username=user).first() if user else User.objects.filter(role=User.Role.OWNER).first()
        owner = owner or User.objects.filter(is_superuser=True).first()
        with transaction.atomic():
            self.create(owner)
        self.stdout.write(self.style.SUCCESS("Demo data created. Remove it any time with: manage.py seed_demo --remove"))

    def remove(self):
        tag = Tag.objects.filter(name=DEMO_TAG, kind=Tag.Kind.TOPIC).first()
        if not tag:
            self.stdout.write("No demo data found.")
            return
        with transaction.atomic():
            Application.objects.filter(tags=tag).delete()
            Opportunity.objects.filter(tags=tag).delete()
            Person.objects.filter(notes__startswith="[demo]").delete()
            tag.delete()
        self.stdout.write(self.style.SUCCESS("Demo data removed."))

    # ------------------------------------------------------------------

    def create(self, owner):
        today = timezone.localdate()
        d = lambda days: today + timedelta(days=days)  # noqa: E731
        demo = Tag.objects.create(name=DEMO_TAG, kind=Tag.Kind.TOPIC, color=Tag.Color.GRAY)
        topics = {
            n: Tag.objects.get_or_create(name=n, kind=Tag.Kind.TOPIC, defaults={"color": c})[0]
            for n, c in [("cephalopod", "purple"), ("neural stem cells", "blue"), ("spatial transcriptomics", "teal"),
                         ("cell fate", "green"), ("evo-devo", "amber")]
        }
        themes = {t.name: t for t in Tag.objects.filter(kind=Tag.Kind.CRITIQUE)}
        nih = Funder.objects.get_or_create(name="National Institutes of Health", defaults={"short_name": "NIH", "default_reporting": "nih_snap"})[0]
        nsf = Funder.objects.get_or_create(name="National Science Foundation", defaults={"short_name": "NSF"})[0]
        simons = Funder.objects.get_or_create(name="Simons Foundation", defaults={"short_name": "Simons", "funder_type": "foundation"})[0]
        whitehall = Funder.objects.get_or_create(name="Whitehall Foundation", defaults={"short_name": "Whitehall", "funder_type": "foundation"})[0]
        czi = Funder.objects.get_or_create(name="Chan Zuckerberg Initiative", defaults={"short_name": "CZI", "funder_type": "foundation"})[0]
        internal = Funder.objects.get_or_create(name="Internal / institutional funding", defaults={"short_name": "Internal", "funder_type": "institutional", "default_reporting": "final"})[0]

        def person(first, last, kind, position="", institution="", link=None):
            return Person.objects.create(first_name=first, last_name=last, kind=kind, position=position,
                                         institution=institution, user=link, notes="[demo] fictional")

        pi_link = owner if owner and not hasattr(owner, "person") else None
        pi = person("Jordan", "Lee", Person.Kind.LAB, "Associate Professor", "Example University", link=pi_link)
        postdoc = person("Sam", "Patel", Person.Kind.LAB, "Postdoctoral fellow", "Example University")
        grad = person("Riley", "Chen", Person.Kind.LAB, "Graduate student", "Example University")
        tech = person("Morgan", "Diaz", Person.Kind.LAB, "Research technician", "Example University")
        collab = person("Avery", "Nakamura", Person.Kind.COLLABORATOR, "Professor", "Coastal Marine Institute")
        po = person("Casey", "Morgan", Person.Kind.PROGRAM_OFFICER, "Program Director", "NICHD")
        admin = person("Taylor", "Brooks", Person.Kind.ADMIN, "Grants administrator", "Example University")

        def app(**kw):
            tags = kw.pop("tags", [])
            team = kw.pop("team", [])
            a = Application.objects.create(created_by=owner, program_officer=kw.pop("po", None), grants_admin=admin, **kw)
            a.tags.add(demo, *[topics[t] for t in tags])
            for p, role, pm in team:
                Personnel.objects.create(application=a, person=p, role=role, person_months=Decimal(str(pm)), is_key=role in ("pi", "co_i", "mpi"))
            return a

        # 1. Funded R01 (A1) with a not-funded A0 parent
        a0 = app(title="Developmental origins of neural stem cell diversity in the cephalopod brain", short_name="Cephalopod NSC R01",
                 funder=nih, sponsor_unit="NICHD", mechanism="R01", status=Application.Status.NOT_FUNDED,
                 sponsor_deadline=d(-1000), submitted_on=d(-1001), review_date=d(-880), decision_on=d(-800),
                 review_panel="DEV2", review_outcome="scored", impact_score=Decimal("38"), percentile=Decimal("27"),
                 requested_total=Decimal("3150000"), requested_direct_total=Decimal("1950000"), duration_years=5,
                 proposed_start=d(-760), proposed_end=d(1065), po=po,
                 lessons_learned="Reviewers wanted stronger preliminary lineage-tracing data and a clearer rationale for the model system.",
                 tags=["cephalopod", "neural stem cells"], team=[(pi, "pi", 2.4), (postdoc, "postdoc", 12)])
        fb = ReviewFeedback.objects.create(application=a0, source="reviewer", reviewer_label="Reviewer 1", overall_score="4",
                                           strengths="- Highly innovative system for comparative neurodevelopment\n- Strong investigator track record",
                                           weaknesses="- Limited preliminary data for Aim 2\n- Feasibility of in vivo lineage tracing unclear",
                                           response_plan="Add clonal labeling pilot; cite new embryo culture protocol.")
        fb.themes.add(*[themes[n] for n in ("Preliminary data", "Feasibility") if n in themes])
        for c, s in (("Factor 1: Importance of the Research", "3"), ("Factor 2: Rigor and Feasibility", "5"), ("Factor 3: Expertise and Resources", "Sufficient")):
            CriterionScore.objects.create(feedback=fb, criterion=c, score=s)
        fb2 = ReviewFeedback.objects.create(application=a0, source="reviewer", reviewer_label="Reviewer 2", overall_score="3",
                                            strengths="- Important gap in understanding of invertebrate neurogenesis",
                                            weaknesses="- Justify cephalopod over established models\n- Aim 3 overly ambitious")
        fb2.themes.add(*[themes[n] for n in ("Model system justification", "Overly ambitious") if n in themes])

        a1 = services.clone_application(a0, Application.SubmissionType.RESUBMISSION, owner)
        a1.sponsor_deadline, a1.submitted_on, a1.review_date = d(-880), d(-881), d(-760)
        a1.decision_on, a1.impact_score, a1.percentile = d(-650), Decimal("22"), Decimal("9")
        a1.review_outcome, a1.review_panel = "scored", "DEV2"
        a1.proposed_start, a1.proposed_end = d(-610), d(1215)
        a1.sponsor_id = "1R01HD000000-01A1"
        a1.status = Application.Status.AWARDED
        a1.major_goals = "Define the lineage relationships and molecular programs that generate neural stem cell diversity in the developing cephalopod brain."
        a1.save()
        a1.tags.add(demo)
        award = services.create_award(a1, owner)
        award.award_number, award.notice_date = "R01HD000000", d(-640)
        award.awarded_total, award.awarded_direct_total, award.fa_rate = Decimal("3080000"), Decimal("1900000"), Decimal("62")
        award.account_number = "90000001"
        award.save()
        services.generate_budget_periods(award, replace=True)
        for p in award.periods.filter(start_date__lte=today):
            p.status = "awarded"
            p.spent_to_date = (p.direct_costs or 0) * Decimal("0.9") if p.end_date < today else (p.direct_costs or 0) * Decimal("0.45")
            p.save()
        services.generate_reporting_tasks(award, owner)
        a1.tasks.filter(due_date__lt=today).update(status=Task.Status.DONE, completed_at=timezone.now())
        Personnel.objects.filter(application=a1, person=postdoc).update(person_months=Decimal("12"))
        Personnel.objects.create(application=a1, person=grad, role="grad", person_months=Decimal("12"))
        Personnel.objects.create(application=a1, person=tech, role="staff", person_months=Decimal("6"))

        # 2. NSF award
        a2 = app(title="A spatial transcriptomic atlas of the developing squid optic lobe", short_name="Optic lobe atlas (NSF)",
                 funder=nsf, sponsor_unit="BIO/IOS", mechanism="Standard Grant", status=Application.Status.AWARDED,
                 sponsor_deadline=d(-560), submitted_on=d(-562), decision_on=d(-380), requested_total=Decimal("950000"),
                 requested_direct_total=Decimal("620000"), proposed_start=d(-330), proposed_end=d(765), duration_years=3,
                 review_outcome="recommended", tags=["cephalopod", "spatial transcriptomics"],
                 team=[(pi, "pi", 1.0), (grad, "grad", 0), (collab, "co_pi", 0.6)])
        aw2 = services.create_award(a2, owner)
        aw2.award_number = "IOS-0000000"
        aw2.save()
        a2.tasks.filter(due_date__lt=today).exclude(title__startswith="Set up data").update(
            status=Task.Status.DONE, completed_at=timezone.now()
        )

        # 3. R21 under review
        app(title="Single-cell fate mapping of cephalopod retinal progenitors", short_name="Retina fate R21",
            funder=nih, sponsor_unit="NEI", mechanism="R21", status=Application.Status.IN_REVIEW,
            sponsor_deadline=d(-75), submitted_on=d(-76), review_date=d(20), council_date=d(110),
            requested_total=Decimal("420000"), requested_direct_total=Decimal("275000"), proposed_start=d(200), proposed_end=d(930),
            probability=25, review_panel="BVS", tags=["cell fate", "cephalopod"], po=po,
            team=[(pi, "pi", 1.2), (postdoc, "postdoc", 3)])

        # 4. Foundation proposal in preparation, with a checklist
        a4 = app(title="Neural circuits for adaptive camouflage: development of chromatophore motor neurons",
                 short_name="Camouflage circuits (Simons)", funder=simons, mechanism="Collaboration award",
                 status=Application.Status.DRAFTING, sponsor_deadline=d(24), internal_deadline=d(17),
                 requested_total=Decimal("1500000"), proposed_start=d(240), proposed_end=d(1335), probability=15,
                 priority="high", is_starred=True, tags=["cephalopod", "cell fate"],
                 team=[(pi, "mpi", 1.8), (collab, "mpi", 1.8)])
        fnd = ChecklistTemplate.objects.filter(name="Foundation proposal").first()
        if fnd:
            services.apply_checklist(a4, fnd, owner)
            a4.tasks.order_by("due_date").filter(due_date__lt=d(10)).update(status=Task.Status.DONE, completed_at=timezone.now())
            a4.tasks.update(assignee=owner)
        Comment.objects.create(application=a4, author=owner, body="Avery confirmed the imaging core letter. Budget draft due to Taylor next week.")
        services.store_upload(
            Document.objects.create(application=a4, category=Document.Category.AIMS, title="Specific Aims", version="v3",
                                    uploaded_by=owner),
            SimpleUploadedFile("aims-v3.md", b"# Specific Aims (draft v3)\n\nAim 1. Map chromatophore motor neuron birth order.\n"
                                             b"Aim 2. Identify transcription factors that specify motor neuron subtypes.\n"),
        )

        # 5. Closed internal pilot
        a5 = app(title="Pilot: single-nucleus multiome of hummingbird bobtail squid embryos", short_name="Multiome pilot",
                 funder=internal, mechanism="Pilot", status=Application.Status.AWARDED, submission_type="internal",
                 sponsor_deadline=d(-1300), submitted_on=d(-1300), decision_on=d(-1240), requested_total=Decimal("50000"),
                 proposed_start=d(-1200), proposed_end=d(-835), tags=["cephalopod"], team=[(pi, "pi", 0.6)])
        aw5 = services.create_award(a5, owner)
        aw5.status = "closed"
        aw5.save()
        a5.tasks.update(status=Task.Status.DONE, completed_at=timezone.now())

        # 6-7. Decisions and ideas
        app(title="Evolution of neural progenitor gene regulatory programs", short_name="Whitehall GRN",
            funder=whitehall, mechanism="Research grant", status=Application.Status.NOT_FUNDED,
            sponsor_deadline=d(-420), submitted_on=d(-421), decision_on=d(-300), requested_total=Decimal("225000"),
            tags=["evo-devo"], team=[(pi, "pi", 0.6)])
        app(title="Live imaging of neurogenesis in transparent cephalopod embryos", short_name="CZI imaging",
            funder=czi, mechanism="Imaging", status=Application.Status.PLANNING, sponsor_deadline=d(70),
            requested_total=Decimal("800000"), probability=20, tags=["cephalopod"], team=[(pi, "pi", 1.2)])
        app(title="Neural stem cell commitment atlas across molluscs", short_name="Mollusc atlas U01 idea",
            funder=nih, mechanism="U01", status=Application.Status.IDEA, tags=["evo-devo", "neural stem cells"])
        app(title="Cortical-like layering in the cephalopod vertical lobe", short_name="Vertical lobe R01 (2022)",
            funder=nih, mechanism="R01", sponsor_unit="NINDS", status=Application.Status.NOT_FUNDED,
            sponsor_deadline=d(-1500), submitted_on=d(-1501), decision_on=d(-1300), review_outcome="not_discussed",
            requested_total=Decimal("2600000"), tags=["cephalopod"], team=[(pi, "pi", 2.4)])

        # Opportunities
        for kw in (
            dict(title="Mechanisms of nervous system development in non-traditional models", funder=nsf,
                 sponsor_unit="BIO/IOS", mechanism="Standard Grant", deadline=d(45), fit_score=5,
                 status=Opportunity.Status.PLANNING, max_award=Decimal("1000000"), summary="Fictional demo solicitation."),
            dict(title="Brain development pilot awards (limited submission)", funder=internal, mechanism="Pilot",
                 internal_deadline=d(12), deadline=d(40), limited_submission=True, fit_score=4,
                 max_award=Decimal("100000"), summary="Fictional demo internal competition."),
            dict(title="Investigator awards in comparative neuroscience", funder=simons, mechanism="Investigator",
                 loi_deadline=d(95), deadline=d(160), fit_score=3, summary="Fictional demo program."),
        ):
            o = Opportunity.objects.create(added_by=owner, **kw)
            o.tags.add(demo, topics["cephalopod"])
