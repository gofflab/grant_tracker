import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models


def new_feed_token():
    return secrets.token_urlsafe(32)


class User(AbstractUser):
    """Portal account. Roles are deliberately coarse: a small lab does not need ACLs."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        EDITOR = "editor", "Editor"
        VIEWER = "viewer", "Viewer"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.EDITOR)
    calendar_token = models.CharField(max_length=64, default=new_feed_token, unique=True)
    email_digest = models.BooleanField(
        default=True, help_text="Receive a weekly email digest of upcoming deadlines and tasks."
    )
    digest_days_ahead = models.PositiveSmallIntegerField(default=21)

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def is_owner(self):
        return self.is_superuser or self.role == self.Role.OWNER

    @property
    def can_edit(self):
        return self.is_owner or self.role == self.Role.EDITOR

    @property
    def initials(self):
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return "".join(p[0] for p in parts).upper()[:2]
        return self.username[:2].upper()

    def rotate_calendar_token(self):
        self.calendar_token = new_feed_token()
        self.save(update_fields=["calendar_token"])
