from datetime import date
from decimal import Decimal

from accounts.models import User
from grants.models import Application, Funder, Person, Personnel


def make_user(username="owner", role=User.Role.OWNER, **kw):
    return User.objects.create_user(username=username, password="Test-pass-12345", role=role, email=f"{username}@example.org", **kw)


def nih():
    return Funder.objects.get_or_create(name="National Institutes of Health", defaults={"short_name": "NIH", "default_reporting": "nih_snap"})[0]


def make_app(**kw):
    defaults = {"title": "Test application", "funder": nih(), "mechanism": "R01"}
    defaults.update(kw)
    return Application.objects.create(**defaults)


def add_person(app, first="Pat", last="Lee", pm="1.2", role="pi", **kw):
    person = Person.objects.create(first_name=first, last_name=last)
    return Personnel.objects.create(application=app, person=person, role=role, person_months=Decimal(pm), **kw)
