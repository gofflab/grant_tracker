from django.test import TestCase, override_settings
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.models import User

PASSWORD = "Test-pass-12345"


def totp_for(device):
    return str(TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift).token()).zfill(device.digits)


class LoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("pi", password=PASSWORD, role=User.Role.OWNER)

    def test_login_and_logout(self):
        r = self.client.post("/accounts/login/", {"username": "pi", "password": PASSWORD})
        self.assertRedirects(r, "/")
        r = self.client.post("/accounts/logout/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_lockout_after_repeated_failures(self):
        for _ in range(5):
            self.client.post("/accounts/login/", {"username": "pi", "password": "wrong"})
        r = self.client.post("/accounts/login/", {"username": "pi", "password": PASSWORD})
        self.assertContains(r, "Too many sign-in attempts", status_code=429)

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/accounts/logout/").status_code, 405)


class TwoFactorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("pi", password=PASSWORD, role=User.Role.OWNER)

    def enroll(self):
        self.client.force_login(self.user)
        self.client.get("/accounts/2fa/setup/")
        device = TOTPDevice.objects.get(user=self.user)
        r = self.client.post("/accounts/2fa/setup/", {"token": totp_for(device)})
        self.assertContains(r, "Save these one-time backup codes")
        device.refresh_from_db()
        self.assertTrue(device.confirmed)
        return device, r.context["codes"]

    def test_enrolled_user_must_verify_after_password(self):
        device, codes = self.enroll()
        self.client.logout()
        self.client.post("/accounts/login/", {"username": "pi", "password": PASSWORD})
        r = self.client.get("/applications/")
        self.assertRedirects(r, "/accounts/verify/?next=/applications/", fetch_redirect_response=False)
        r = self.client.post("/accounts/verify/?next=/applications/", {"token": "000000", "next": "/applications/"})
        self.assertContains(r, "not valid")
        device.refresh_from_db()
        self.assertEqual(device.throttling_failure_count, 1)  # django-otp throttles repeated guesses
        device.last_t, device.throttling_failure_count, device.throttling_failure_timestamp = -1, 0, None
        device.save()
        r = self.client.post("/accounts/verify/", {"token": totp_for(device), "next": "/applications/"})
        self.assertRedirects(r, "/applications/")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_backup_code_works_once(self):
        _, codes = self.enroll()
        self.client.logout()
        self.client.post("/accounts/login/", {"username": "pi", "password": PASSWORD})
        r = self.client.post("/accounts/verify/", {"token": codes[0]})
        self.assertEqual(r.status_code, 302)
        self.client.logout()
        self.client.post("/accounts/login/", {"username": "pi", "password": PASSWORD})
        r = self.client.post("/accounts/verify/", {"token": codes[0]})
        self.assertContains(r, "not valid")

    def test_verify_rejects_offsite_next(self):
        device, _ = self.enroll()
        self.client.logout()
        self.client.post("/accounts/login/", {"username": "pi", "password": PASSWORD})
        device.refresh_from_db()
        device.last_t = -1
        device.save()
        r = self.client.post("/accounts/verify/", {"token": totp_for(device), "next": "https://evil.example/"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)

    @override_settings(REQUIRE_2FA=True)
    def test_required_2fa_forces_enrollment(self):
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get("/"), "/accounts/2fa/setup/", fetch_redirect_response=False)


class UserManagementTests(TestCase):
    def test_last_owner_cannot_demote_self(self):
        owner = User.objects.create_user("pi", password=PASSWORD, role=User.Role.OWNER)
        self.client.force_login(owner)
        r = self.client.post(f"/accounts/users/{owner.pk}/", {"role": "editor", "is_active": "on"})
        self.assertContains(r, "only active owner")
        owner.refresh_from_db()
        self.assertEqual(owner.role, User.Role.OWNER)

    def test_owner_creates_user_with_strong_password(self):
        owner = User.objects.create_user("pi", password=PASSWORD, role=User.Role.OWNER)
        self.client.force_login(owner)
        r = self.client.post("/accounts/users/new/", {"username": "lm", "role": "editor", "password": "short"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/accounts/users/new/", {"username": "lm", "role": "editor", "password": "Lab-manager-2026!"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(User.objects.get(username="lm").check_password("Lab-manager-2026!"))
