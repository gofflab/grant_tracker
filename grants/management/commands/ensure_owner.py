"""Create the first Owner account from environment variables if no users exist.

Reads GT_OWNER_USERNAME, GT_OWNER_EMAIL, GT_OWNER_PASSWORD. Safe to run on every start.
"""

import os

from django.core.management.base import BaseCommand

from accounts.models import User


class Command(BaseCommand):
    help = "Bootstrap the initial owner account."

    def handle(self, *args, **opts):
        if User.objects.exists():
            return
        username = os.environ.get("GT_OWNER_USERNAME")
        password = os.environ.get("GT_OWNER_PASSWORD")
        if not (username and password):
            self.stdout.write("No users yet. Set GT_OWNER_USERNAME and GT_OWNER_PASSWORD, or run `manage.py createsuperuser`.")
            return
        User.objects.create_superuser(
            username=username, email=os.environ.get("GT_OWNER_EMAIL", ""), password=password, role=User.Role.OWNER,
            first_name=os.environ.get("GT_OWNER_FIRST_NAME", ""), last_name=os.environ.get("GT_OWNER_LAST_NAME", ""),
        )
        self.stdout.write(self.style.SUCCESS(f"Created owner account '{username}'. Change the password after first sign-in."))
