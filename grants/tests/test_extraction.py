"""Opportunity import: rules, Grants.gov, AI, safe fetching and the review flow.

Fixtures in fixtures/ are synthetic pages modeled on NIH Guide, NSF and foundation layouts.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import User
from grants.extraction import ai, fetch, grantsgov, pipeline, rules
from grants.extraction.text import from_bytes
from grants.models import Funder, Opportunity

from .helpers import make_user

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 10, 1)


def fixture(name):
    return (FIXTURES / name).read_bytes()


def best(fields, name):
    c = fields.best(name)
    return c.value if c else None


def tiny_pdf(text):
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


class OfflineTestCase(TestCase):
    """Fail fast instead of reaching Grants.gov or the web; tests that need responses patch them."""

    def setUp(self):
        super().setUp()
        for target in (mock.patch.object(grantsgov, "post_json", side_effect=fetch.FetchError("offline in tests")),
                       mock.patch.object(pipeline, "fetch", side_effect=fetch.FetchError("offline in tests"))):
            target.start()
            self.addCleanup(target.stop)


class RuleExtractionTests(TestCase):
    def test_nih_nofo(self):
        f = rules.extract(from_bytes(fixture("nih_nofo.html"), "text/html"), "", TODAY)
        self.assertEqual(best(f, "title"), "Developmental Origins of Neural Cell Diversity (R01 Clinical Trial Not Allowed)")
        self.assertEqual(best(f, "number"), "PAR-26-123")
        self.assertEqual(best(f, "mechanism"), "R01")
        self.assertEqual(best(f, "sponsor_unit"), "NICHD, NINDS")
        self.assertEqual(best(f, "deadline"), date(2026, 10, 5))
        self.assertTrue(best(f, "recurring"))
        self.assertEqual(best(f, "recurrence_notes"), "Due dates: Oct 5, 2026; Feb 5, 2027; Jun 5, 2027")
        self.assertEqual(best(f, "expires_on"), date(2029, 9, 8))
        self.assertEqual(best(f, "max_award"), Decimal("500000"))
        self.assertIn("direct costs per year", best(f, "budget_notes"))
        self.assertEqual(best(f, "max_duration_years"), 5)
        self.assertIsNone(best(f, "limited_submission"))  # "may submit more than one application"
        self.assertEqual(best(f, "contact"), "Jane Q. Program <jane.program@nih.gov>")
        self.assertIn("neural stem and progenitor cells", best(f, "summary"))
        self.assertEqual(f.extra["Assistance listing"], "93.865, 93.853")
        self.assertEqual(f.extra["Clinical trials"], "Not allowed")
        self.assertIn("30 days", " ".join(f.notes))

    def test_nsf_solicitation(self):
        f = rules.extract(from_bytes(fixture("nsf_solicitation.txt"), "text/plain"), "", TODAY)
        self.assertEqual(best(f, "title"), "Mechanisms of Neural Development in Diverse Organisms (MNDDO)")
        self.assertEqual(best(f, "number"), "NSF 26-512")
        self.assertEqual(best(f, "sponsor_unit"), "BIO/IOS")
        self.assertEqual(best(f, "deadline"), date(2027, 1, 12))
        self.assertTrue(best(f, "recurring"))
        self.assertTrue(best(f, "limited_submission"))
        self.assertEqual(best(f, "max_duration_years"), 3)
        self.assertEqual(best(f, "contact"), "Avery Smith <asmith@nsf.gov>")
        self.assertEqual(f.extra["Expected awards"], "15 to 20")

    def test_free_form_foundation_page(self):
        f = rules.extract(from_bytes(fixture("foundation.html"), "text/html"), "", TODAY)
        self.assertEqual(best(f, "title"), "Investigator Awards in Neural Development")
        self.assertEqual(best(f, "loi_deadline"), date(2027, 1, 15))
        self.assertEqual(best(f, "deadline"), date(2027, 4, 1))
        self.assertEqual(best(f, "max_award"), Decimal("300000"))
        self.assertEqual(best(f, "max_duration_years"), 3)
        self.assertTrue(best(f, "limited_submission"))
        self.assertEqual(best(f, "funder_name"), "Example Brain Foundation")  # from the page title suffix
        self.assertLess(f.best("max_award").confidence, 0.6)  # loose pattern, flagged for checking

    def test_past_due_dates_are_flagged(self):
        f = rules.extract(from_bytes(fixture("nih_nofo.html"), "text/html"), "", date(2028, 1, 1))
        self.assertEqual(best(f, "deadline"), date(2027, 6, 5))
        self.assertIn("passed", " ".join(f.notes))

    def test_nih_standard_dates(self):
        self.assertEqual(rules.nih_standard_dates("R01", TODAY), [date(2026, 10, 5), date(2027, 2, 5), date(2027, 6, 5)])
        self.assertEqual(rules.nih_standard_dates("R21", TODAY)[0], date(2026, 10, 16))
        self.assertEqual(rules.nih_standard_dates("ZZ9", TODAY), [])


class PipelineTests(OfflineTestCase):
    def test_pasted_nih_page_builds_a_draft(self):
        result = pipeline.run(pasted=fixture("nih_nofo.html").decode(), use_ai=False, today=TODAY)
        init = result.initial
        nih = Funder.objects.get(short_name="NIH")
        self.assertEqual(init["funder"], nih.pk)
        self.assertEqual(init["number"], "PAR-26-123")
        self.assertEqual(init["deadline"], "2026-10-05")
        self.assertEqual(init["loi_deadline"], "2026-09-05")  # computed: 30 days before
        self.assertEqual(init["max_award"], "500000")
        self.assertEqual(init["status"], Opportunity.Status.WATCHING)
        self.assertIn("Program contact: Jane Q. Program <jane.program@nih.gov>", init["notes"])
        self.assertEqual(init["extra"]["Clinical trials"], "Not allowed")
        rows = {r["field"]: r for r in result.rows}
        self.assertEqual(rows["funder"]["value"], "National Institutes of Health")
        self.assertEqual(rows["loi_deadline"]["level"], "medium")
        json.dumps(result.as_session())  # must be session-serializable

    def test_link_with_grants_gov_and_domain_funder_match(self):
        page = fetch.Fetched(url="https://grants.nih.gov/grants/guide/pa-files/PAR-26-123.html",
                             content=fixture("nih_nofo.html"), content_type="text/html")
        gg = rules.Fields()
        gg.add("max_award", Decimal("750000"), "Grants.gov", 0.85)
        gg.add("deadline", date(2029, 6, 5), "Grants.gov close date", 0.5)
        with mock.patch.object(pipeline, "fetch", return_value=page), \
             mock.patch.object(grantsgov, "safe_lookup", return_value=(gg, None)) as lookup:
            result = pipeline.run(source=page.url, use_ai=False, today=TODAY)
        lookup.assert_called_once_with("PAR-26-123")
        self.assertEqual(result.initial["funder"], Funder.objects.get(short_name="NIH").pk)
        self.assertEqual(result.initial["max_award"], "750000")  # Grants.gov ceiling outranks the text pattern
        self.assertEqual(result.initial["deadline"], "2026-10-05")  # NOFO table outranks Grants.gov close date
        self.assertEqual(result.initial["url"], page.url)
        self.assertIn("Grants.gov lookup for PAR-26-123", result.sources)

    def test_announcement_number_looks_up_grants_gov_then_reads_nih_guide(self):
        page = fetch.Fetched(url="https://grants.nih.gov/grants/guide/pa-files/PAR-26-123.html",
                             content=fixture("nih_nofo.html"), content_type="text/html")
        with mock.patch.object(pipeline, "fetch", return_value=page) as fetched, \
             mock.patch.object(grantsgov, "safe_lookup", return_value=(None, None)):
            result = pipeline.run(source="par-26-123", use_ai=False, today=TODAY)
        fetched.assert_called_once_with("https://grants.nih.gov/grants/guide/pa-files/PAR-26-123.html")
        self.assertIn("Grants.gov has no listing for PAR-26-123.", result.notes)
        self.assertEqual(result.initial["title"], "Developmental Origins of Neural Cell Diversity (R01 Clinical Trial Not Allowed)")

    def test_unreachable_link_reports_and_suggests_upload(self):
        with mock.patch.object(pipeline, "fetch", side_effect=fetch.FetchError("The site returned an error (403).")):
            result = pipeline.run(source="https://example.org/rfa", use_ai=False, today=TODAY)
        self.assertEqual(result.rows, [])
        self.assertIn("uploading the PDF", " ".join(result.notes))

    def test_duplicate_detection(self):
        existing = Opportunity.objects.create(title="Already here", number="PAR-26-123")
        result = pipeline.run(pasted=fixture("nih_nofo.html").decode(), use_ai=False, today=TODAY)
        self.assertEqual(result.duplicate_id, existing.pk)

    def test_unknown_funder_gets_a_note(self):
        result = pipeline.run(pasted=fixture("foundation.html").decode(), use_ai=False, today=TODAY)
        self.assertNotIn("funder", result.initial)
        self.assertEqual(result.initial["title"], "Investigator Awards in Neural Development")
        self.assertEqual(result.new_funder, {"name": "Example Brain Foundation", "short_name": "",
                                             "funder_type": "foundation", "website": ""})

    def test_named_foundation_matches_existing_funder(self):
        funder = Funder.objects.create(name="Example Brain Foundation")
        result = pipeline.run(pasted=fixture("foundation.html").decode(), use_ai=False, today=TODAY)
        self.assertEqual(result.initial["funder"], funder.pk)

    def test_uploaded_pdf(self):
        pdf = tiny_pdf("Application Due Date: March 3, 2027")
        result = pipeline.run(upload=("call.pdf", pdf), use_ai=False, today=TODAY)
        self.assertEqual(result.initial["deadline"], "2027-03-03")
        self.assertIn("Uploaded file (call.pdf)", result.sources)


class GrantsGovMappingTests(TestCase):
    def test_lookup_maps_fields(self):
        search = {"errorcode": 0, "data": {"hitCount": 1, "oppHits": [
            {"id": "123456", "number": "PAR-26-123", "title": "Dev Origins", "agency": "National Institutes of Health", "closeDate": "06/05/2029"},
        ]}}
        detail = {"errorcode": 0, "data": {
            "opportunityNumber": "PAR-26-123", "opportunityTitle": "Developmental Origins of Neural Cell Diversity",
            "cfdas": [{"cfdaNumber": "93.865"}],
            "synopsis": {"agencyName": "National Institutes of Health", "responseDate": "Jun 05, 2029", "awardCeiling": "750000",
                         "synopsisDesc": "<p>Supports <b>developmental</b> neuroscience.</p>", "agencyContactEmail": "help@nih.gov",
                         "fundingDescLinkUrl": "https://grants.nih.gov/grants/guide/pa-files/PAR-26-123.html"},
        }}
        with mock.patch.object(grantsgov, "post_json", side_effect=[search, detail]) as post:
            f, link = grantsgov.lookup("PAR-26-123")
        self.assertEqual(post.call_args_list[1].args[1], {"opportunityId": 123456})
        self.assertEqual(best(f, "deadline"), date(2029, 6, 5))
        self.assertEqual(best(f, "max_award"), Decimal("750000"))
        self.assertEqual(best(f, "summary"), "Supports developmental neuroscience.")
        self.assertEqual(f.extra["Assistance listing"], "93.865")
        self.assertEqual(link, "https://grants.nih.gov/grants/guide/pa-files/PAR-26-123.html")

    def test_lookup_failure_is_a_note_not_an_error(self):
        with mock.patch.object(grantsgov, "post_json", side_effect=fetch.FetchError("API request failed (timed out).")):
            f, link = grantsgov.safe_lookup("PAR-26-123")
        self.assertIsNone(link)
        self.assertIn("Grants.gov lookup didn't work", f.notes[0])


class SafeFetchTests(TestCase):
    def test_refuses_internal_and_non_http_addresses(self):
        for url in ["http://127.0.0.1/admin", "http://localhost:8000/", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data",
                    "file:///etc/passwd", "ftp://example.org/x", "http://[::1]/"]:
            with self.assertRaises(fetch.FetchError, msg=url):
                fetch.fetch(url)


def fake_response(payload, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


AI_PAYLOAD = {
    "title": "Investigator Awards in Neural Development", "funder": "Example Brain Foundation", "sponsor_unit": "",
    "announcement_number": "", "mechanism": "Investigator award", "summary": "Funds early-career neurodevelopment research.",
    "eligibility": "Tenure-track faculty within six years of appointment.", "award_ceiling_usd": 300000,
    "budget_notes": "Up to $300,000 over three years", "max_duration_years": 3, "loi_due_date": "2027-01-15",
    "application_due_dates": ["2027-04-01"], "expiration_date": "", "limited_submission": True,
    "contact": "grants@examplebrain.org", "key_requirements": ["One nomination per institution", "Indirect costs capped at 10%"],
}


@override_settings(ANTHROPIC_API_KEY="sk-test", OPPORTUNITY_AI_MODEL="claude-opus-5-5")
class AIExtractionTests(OfflineTestCase):
    def test_request_shape_and_mapping(self):
        client = mock.MagicMock()
        client.beta.messages.create.return_value = fake_response(AI_PAYLOAD)
        with mock.patch("anthropic.Anthropic", return_value=client):
            f = ai.extract("Some announcement text", None, "https://example.org", TODAY)
        kwargs = client.beta.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", kwargs["betas"])
        self.assertEqual(kwargs["output_config"]["effort"], "low")
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("thinking", kwargs)
        self.assertEqual(best(f, "mechanism"), "Investigator award")
        self.assertEqual(best(f, "deadline"), date(2027, 4, 1))
        self.assertEqual(best(f, "requirements"), AI_PAYLOAD["key_requirements"])

    def test_pdf_is_sent_as_a_document(self):
        client = mock.MagicMock()
        client.beta.messages.create.return_value = fake_response(AI_PAYLOAD)
        with mock.patch("anthropic.Anthropic", return_value=client):
            ai.extract("", tiny_pdf("hello"), "call.pdf", TODAY)
        block = client.beta.messages.create.call_args.kwargs["messages"][0]["content"][0]
        self.assertEqual(block["type"], "document")
        self.assertEqual(block["source"]["media_type"], "application/pdf")

    def test_refusal_and_errors_fall_back_to_rules(self):
        client = mock.MagicMock()
        client.beta.messages.create.return_value = fake_response({}, stop_reason="refusal")
        with mock.patch("anthropic.Anthropic", return_value=client):
            f = ai.extract("text", None, "", TODAY)
        self.assertFalse(f.candidates)
        self.assertIn("declined", f.notes[0])

    def test_rules_beat_ai_where_the_announcement_labels_the_field(self):
        client = mock.MagicMock()
        payload = dict(AI_PAYLOAD, application_due_dates=["2026-12-01"], title="Something else")
        client.beta.messages.create.return_value = fake_response(payload)
        with mock.patch("anthropic.Anthropic", return_value=client):
            result = pipeline.run(pasted=fixture("nih_nofo.html").decode(), use_ai=True, today=TODAY)
        self.assertTrue(result.used_ai)
        self.assertEqual(result.initial["deadline"], "2026-10-05")
        self.assertEqual(result.initial["title"], "Developmental Origins of Neural Cell Diversity (R01 Clinical Trial Not Allowed)")
        self.assertIn("One nomination per institution", result.initial["notes"])

    def test_ai_fills_gaps_on_free_form_pages(self):
        client = mock.MagicMock()
        client.beta.messages.create.return_value = fake_response(AI_PAYLOAD)
        with mock.patch("anthropic.Anthropic", return_value=client):
            result = pipeline.run(pasted=fixture("foundation.html").decode(), use_ai=True, today=TODAY)
        self.assertEqual(result.initial["mechanism"], "Investigator award")
        self.assertEqual(result.initial["summary"], "Funds early-career neurodevelopment research.")


class ImportViewTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.client.force_login(self.user)

    def test_full_import_review_and_save_flow(self):
        self.assertContains(self.client.get("/opportunities/import/"), "Read announcement")
        r = self.client.post("/opportunities/import/", {"pasted": fixture("nih_nofo.html").decode()})
        self.assertRedirects(r, "/opportunities/new/?imported=1", fetch_redirect_response=False)
        r = self.client.get("/opportunities/new/?imported=1")
        self.assertContains(r, "Review imported opportunity")
        self.assertContains(r, 'value="PAR-26-123"')
        self.assertContains(r, "data-autofilled=")
        self.assertContains(r, "Where each value came from")
        form = r.context["form"]
        data = {k: v for k, v in form.initial.items() if v not in (None, "")}
        data["extra"] = json.dumps(data.get("extra", {}))
        data["imported"] = "1"
        for k in ("recurring", "limited_submission"):
            if data.get(k) is False:
                data.pop(k)
        r = self.client.post("/opportunities/new/?imported=1", data)
        opp = Opportunity.objects.get(number="PAR-26-123")
        self.assertRedirects(r, opp.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(opp.deadline, date(2026, 10, 5) if date.today() <= date(2026, 10, 5) else opp.deadline)
        self.assertEqual(opp.funder.short_name, "NIH")
        self.assertEqual(opp.extra["Assistance listing"], "93.865, 93.853")
        self.assertNotIn("opportunity_import", self.client.session)

    def test_upload_and_validation(self):
        r = self.client.post("/opportunities/import/", {})
        self.assertContains(r, "Paste a link")
        r = self.client.post("/opportunities/import/", {"upload": SimpleUploadedFile("x.exe", b"MZ")})
        self.assertContains(r, "Upload a PDF")
        r = self.client.post("/opportunities/import/", {"upload": SimpleUploadedFile("call.pdf", tiny_pdf("Application Due Date: March 3, 2027"))})
        self.assertEqual(r.status_code, 302)

    def test_new_funder_created_on_save(self):
        page = fetch.Fetched("https://www.examplebrain.org/awards", fixture("foundation.html"), "text/html")
        with mock.patch.object(pipeline, "fetch", return_value=page):
            self.client.post("/opportunities/import/", {"source": page.url})
        r = self.client.get("/opportunities/new/?imported=1")
        self.assertContains(r, "isn't in your list yet")
        self.assertContains(r, 'form="opportunity-form"')
        data = {"title": "Investigator Awards in Neural Development", "status": "watching", "extra": "{}",
                "imported": "1", "create_funder": "1"}
        self.client.post("/opportunities/new/?imported=1", data)
        opp = Opportunity.objects.get(title="Investigator Awards in Neural Development")
        self.assertEqual(opp.funder.name, "Example Brain Foundation")
        self.assertEqual(opp.funder.funder_type, "foundation")
        self.assertEqual(opp.funder.website, "https://examplebrain.org")

    def test_new_funder_skipped_when_unchecked(self):
        self.client.post("/opportunities/import/", {"pasted": fixture("foundation.html").decode()})
        self.client.post("/opportunities/new/?imported=1", {"title": "Call", "status": "watching", "extra": "{}", "imported": "1"})
        self.assertIsNone(Opportunity.objects.get(title="Call").funder)
        self.assertFalse(Funder.objects.filter(name="Example Brain Foundation").exists())

    def test_duplicate_warning(self):
        Opportunity.objects.create(title="Existing", number="PAR-26-123")
        self.client.post("/opportunities/import/", {"pasted": fixture("nih_nofo.html").decode()})
        self.assertContains(self.client.get("/opportunities/new/?imported=1"), "You already track this announcement")

    def test_viewers_cannot_import(self):
        viewer = make_user("viewer", User.Role.VIEWER)
        self.client.force_login(viewer)
        self.assertEqual(self.client.get("/opportunities/import/").status_code, 403)
