from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import User
from grants import services
from grants.forms import PersonForm
from grants.models import Application, Person, Personnel

from .helpers import make_app, make_user

HX = {"HTTP_HX_REQUEST": "true"}


def person_data(**kw):
    data = {"first_name": "Ada", "last_name": "Byron", "kind": "lab", "is_active": "on"}
    data.update(kw)
    return data


class PersonTypeTests(TestCase):
    def test_principal_investigator_type_is_separate_from_collaborator(self):
        values = Person.Kind.values
        self.assertIn("pi", values)
        self.assertIn("collaborator", values)
        self.assertIn("lab_pi", values)

    def test_only_one_lab_pi_in_the_database(self):
        Person.objects.create(first_name="A", last_name="One", kind=Person.Kind.LAB_PI)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Person.objects.create(first_name="B", last_name="Two", kind=Person.Kind.LAB_PI)
        Person.objects.create(first_name="C", last_name="Three", kind=Person.Kind.PI)
        Person.objects.create(first_name="D", last_name="Four", kind=Person.Kind.PI)

    def test_form_rejects_second_lab_pi_and_non_owner_link(self):
        owner = make_user("owner")
        editor = make_user("editor", User.Role.EDITOR)
        Person.objects.create(first_name="Lab", last_name="Head", kind=Person.Kind.LAB_PI)
        form = PersonForm(person_data(kind="lab_pi"), user=owner)
        self.assertFalse(form.is_valid())
        self.assertIn("already the Lab PI", str(form.errors))
        Person.objects.all().delete()
        form = PersonForm(person_data(kind="lab_pi", user=editor.pk), user=owner)
        self.assertFalse(form.is_valid())
        self.assertIn("Owner account", str(form.errors))
        self.assertTrue(PersonForm(person_data(kind="lab_pi", user=owner.pk), user=owner).is_valid())

    def test_editors_cannot_assign_or_change_lab_pi(self):
        editor = make_user("editor", User.Role.EDITOR)
        form = PersonForm(user=editor)
        self.assertNotIn("lab_pi", [c[0] for c in form.fields["kind"].choices])
        head = Person.objects.create(first_name="Lab", last_name="Head", kind=Person.Kind.LAB_PI)
        form = PersonForm(person_data(kind="lab"), instance=head, user=editor)
        self.assertTrue(form.is_valid())
        form.save()
        head.refresh_from_db()
        self.assertEqual(head.kind, Person.Kind.LAB_PI)  # field is disabled for editors
        self.client.force_login(editor)
        self.assertEqual(self.client.post(f"/settings/people/{head.pk}/delete/", **HX).status_code, 403)

    def test_new_person_shortcut_on_team_form_excludes_lab_pi(self):
        from grants.forms import PersonnelForm

        self.assertNotIn("lab_pi", [c[0] for c in PersonnelForm().fields["new_kind"].choices])


class LabPiBehaviourTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner", first_name="Loyal", last_name="Goff")
        self.client.force_login(self.owner)

    def test_claim_from_profile(self):
        self.assertContains(self.client.get("/accounts/profile/"), "This is me")
        r = self.client.post("/settings/lab-pi/claim/")
        self.assertRedirects(r, "/accounts/profile/", fetch_redirect_response=False)
        head = Person.lab_pi()
        self.assertEqual((head.full_name, head.user), ("Loyal Goff", self.owner))
        self.assertContains(self.client.get("/accounts/profile/"), "This is you")
        # Claiming again is harmless; a second owner cannot take over.
        self.client.post("/settings/lab-pi/claim/")
        self.assertEqual(Person.objects.filter(kind=Person.Kind.LAB_PI).count(), 1)
        other = make_user("owner2")
        person, error = services.claim_lab_pi(other)
        self.assertIsNone(person)
        self.assertIn("already the Lab PI", error)

    def test_claim_reuses_existing_person_and_requires_owner(self):
        mine = Person.objects.create(first_name="Loyal", last_name="Goff", kind=Person.Kind.LAB, user=self.owner)
        services.claim_lab_pi(self.owner)
        mine.refresh_from_db()
        self.assertTrue(mine.is_lab_pi)
        editor = make_user("editor", User.Role.EDITOR)
        self.client.force_login(editor)
        self.assertEqual(self.client.post("/settings/lab-pi/claim/").status_code, 403)

    def test_new_application_adds_lab_pi_with_matching_role_and_follows_role_changes(self):
        services.claim_lab_pi(self.owner)
        r = self.client.post("/applications/new/", {
            "title": "MPI R01", "initial_status": "planning", "submission_type": "new", "priority": "medium", "role": "mpi",
        })
        app = Application.objects.get(title="MPI R01")
        line = app.personnel.get()
        self.assertEqual((line.person, line.role, line.is_key, line.person_months), (Person.lab_pi(), Personnel.Role.MPI, True, None))
        self.client.post(f"/applications/{app.pk}/edit/", {
            "title": "MPI R01", "submission_type": "new", "priority": "medium", "role": "co_i",
        })
        line.refresh_from_db()
        self.assertEqual(line.role, Personnel.Role.CO_I)

    def test_role_sync_leaves_manual_changes_alone(self):
        services.claim_lab_pi(self.owner)
        app = make_app(role=Application.Role.PI)
        line = services.add_lab_pi(app)
        line.role = Personnel.Role.OSC
        line.save()
        app.role = Application.Role.CO_I
        app.save()
        self.assertEqual(services.sync_lab_pi_role(app, Application.Role.PI), 0)

    def test_no_lab_pi_means_no_auto_add(self):
        app = make_app()
        self.assertIsNone(services.add_lab_pi(app))
        self.assertFalse(app.personnel.exists())

    def test_resubmission_and_reports_default_to_lab_pi(self):
        head, _ = services.claim_lab_pi(self.owner)
        a0 = make_app(status=Application.Status.NOT_FUNDED)
        a1 = services.clone_application(a0, Application.SubmissionType.RESUBMISSION, self.owner)
        self.assertTrue(a1.personnel.filter(person=head).exists())
        Person.objects.create(first_name="Zed", last_name="Other", kind=Person.Kind.PI)
        r = self.client.get("/current-pending/")
        self.assertEqual(r.context["person"], head)
        r = self.client.get("/effort/")
        self.assertEqual(r.context["selected"], head)

    def test_people_list_shows_lab_pi_first(self):
        Person.objects.create(first_name="Aaron", last_name="Aardvark", kind=Person.Kind.PI)
        services.claim_lab_pi(self.owner)
        r = self.client.get("/settings/?tab=people")
        self.assertEqual(list(r.context["items"])[0], Person.lab_pi())
        self.assertContains(r, "Lab PI")
