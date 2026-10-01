from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import user_has_device

# URL names a signed-in but not-yet-verified user may still reach.
_VERIFY_EXEMPT = {"accounts:verify", "accounts:logout"}
_SETUP_EXEMPT = {"accounts:2fa_setup", "accounts:logout", "accounts:verify"}


class TwoFactorRequiredMiddleware:
    """
    Enforce the second factor after password login.

    * A user with a confirmed TOTP device must enter a code before using the site.
    * When REQUIRE_2FA is on, users without a device are sent to enroll.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return None
        match = request.resolver_match
        name = match.view_name if match else ""

        if user_has_device(user, confirmed=True):
            if not user.is_verified() and name not in _VERIFY_EXEMPT:
                return redirect(f"{reverse('accounts:verify')}?next={request.get_full_path()}")
            return None

        if settings.REQUIRE_2FA and name not in _SETUP_EXEMPT:
            return redirect("accounts:2fa_setup")
        return None


class SecurityHeadersMiddleware:
    """Content-Security-Policy and Permissions-Policy headers."""

    # Alpine.js evaluates attribute expressions with Function(), which needs 'unsafe-eval'.
    # Every script is still served from this origin; there are no inline <script> blocks.
    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", self.CSP)
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response
