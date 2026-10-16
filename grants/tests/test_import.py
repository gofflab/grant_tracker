from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from grants.models import Application, Tag

from .helpers import make_user

HEADER = "title,funder,status,probability,impact_score,requested_total,awarded_total,tags\n"


class ImportRobustnessTests(TestCase):
    """Bad rows are reported one by one; they never crash the import or roll back the good rows."""

    def setUp(self):
        self.client.force_login(make_user())

    def run_import(self, body):
        r = self.client.post("/import/", {"csv_file": SimpleUploadedFile("apps.csv", (HEADER + body).encode())})
        self.assertEqual(r.status_code, 200)
        return {row["title"]: row["errors"] for row in r.context["results"]}

    def test_row_with_more_values_than_the_header_is_reported(self):
        errors = self.run_import("Good,NIH,Submitted,,,,,\nToo wide,NIH,Submitted,,,,,,surprise\nTrailing comma,NIH,Submitted,,,,,,\n")
        self.assertEqual(errors["Good"], [])
        self.assertIn("1 more value than the header row", errors["Too wide"][0])
        self.assertEqual(errors["Trailing comma"], [])  # an empty extra value is harmless
        self.assertEqual(set(Application.objects.values_list("title", flat=True)), {"Good", "Trailing comma"})

    def test_out_of_range_numbers_are_reported_per_row(self):
        errors = self.run_import(
            "Good,NIH,Submitted,40,25,1.5M,,\n"
            "Negative chance,NIH,Submitted,-10,,,,\n"
            "Over 100,NIH,Submitted,150,,,,\n"
            "Huge score,NIH,Submitted,,12345.678,,,\n"
            "Huge award,NIH,Awarded,,,,99999999999999,\n"
            "Not a number,NIH,Submitted,,NaN,,,\n"
        )
        self.assertEqual(errors["Good"], [])
        self.assertIn("probability", errors["Negative chance"][0])
        self.assertIn("probability", errors["Over 100"][0])
        self.assertIn("impact_score", errors["Huge score"][0])
        self.assertIn("award", errors["Huge award"][0])
        self.assertIn("impact_score", errors["Not a number"][0])
        self.assertEqual(list(Application.objects.values_list("title", flat=True)), ["Good"])

    def test_extra_decimal_places_are_still_accepted(self):
        errors = self.run_import("Precise,NIH,Submitted,,25.555,,,\n")
        self.assertEqual(errors["Precise"], [])
        self.assertEqual(str(Application.objects.get().impact_score), "25.56")

    def test_long_tag_is_reused_not_duplicated(self):
        long_tag = "x" * 70
        self.run_import(f"First,NIH,Submitted,,,,,{long_tag}\nSecond,NIH,Submitted,,,,,{long_tag}\n")
        tag = Tag.objects.get(kind=Tag.Kind.TOPIC, name="x" * 60)
        self.assertEqual(tag.applications.count(), 2)
