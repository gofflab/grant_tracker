import base64
import io

import qrcode
import qrcode.image.svg
from axes.utils import reset as axes_reset
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_otp import devices_for_user, match_token
from django_otp import login as otp_login
from django_otp import user_has_device
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from grants.models import Person

from .forms import LoginForm, ProfileForm, TokenForm, UserCreateForm, UserUpdateForm
from .models import User
from .permissions import owner_required


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


def _safe_next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return fallback


def verify(request):
    """Second step of login for accounts with 2FA enabled."""
    if not user_has_device(request.user, confirmed=True):
        return redirect("grants:dashboard")
    if request.user.is_verified():
        return redirect(_safe_next(request, reverse("grants:dashboard")))
    form = TokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        device = match_token(request.user, form.cleaned_data["token"])
        if device:
            otp_login(request, device)
            if isinstance(device, StaticDevice):
                remaining = device.token_set.count()
                messages.warning(request, f"Backup code used. {remaining} backup codes remain.")
            return redirect(_safe_next(request, reverse("grants:dashboard")))
        form.add_error("token", "That code is not valid. Check your device clock and try again.")
    return render(request, "accounts/verify.html", {"form": form, "next": request.GET.get("next", "")})


def _qr_svg(uri):
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def _new_backup_codes(user, count=10):
    device, _ = StaticDevice.objects.get_or_create(user=user, name="backup")
    device.confirmed = True
    device.save()
    device.token_set.all().delete()
    codes = [StaticToken.random_token() for _ in range(count)]
    for code in codes:
        device.token_set.create(token=code)
    return codes


def two_factor_setup(request):
    user = request.user
    if TOTPDevice.objects.filter(user=user, confirmed=True).exists():
        messages.info(request, "Two-factor authentication is already enabled.")
        return redirect("accounts:profile")

    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=False)

    form = TokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["token"]):
            with transaction.atomic():
                device.confirmed = True
                device.save()
                codes = _new_backup_codes(user)
            otp_login(request, device)
            return render(request, "accounts/backup_codes.html", {"codes": codes, "first_time": True})
        form.add_error("token", "That code did not match. Scan the QR code again and enter the current code.")

    uri = device.config_url
    return render(
        request,
        "accounts/2fa_setup.html",
        {
            "form": form,
            "qr_svg": _qr_svg(uri),
            "secret": base64.b32encode(device.bin_key).decode().rstrip("="),
            "required": settings.REQUIRE_2FA,
        },
    )


@require_POST
def two_factor_disable(request):
    if settings.REQUIRE_2FA:
        messages.error(request, "Two-factor authentication is required on this portal and cannot be disabled.")
        return redirect("accounts:profile")
    if not request.user.check_password(request.POST.get("password", "")):
        messages.error(request, "Password incorrect; two-factor authentication is still on.")
        return redirect("accounts:profile")
    for device in devices_for_user(request.user, confirmed=None):
        device.delete()
    messages.success(request, "Two-factor authentication disabled.")
    return redirect("accounts:profile")


@require_POST
def regenerate_backup_codes(request):
    if not TOTPDevice.objects.filter(user=request.user, confirmed=True).exists():
        return redirect("accounts:profile")
    codes = _new_backup_codes(request.user)
    return render(request, "accounts/backup_codes.html", {"codes": codes, "first_time": False})


def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user, prefix="profile")
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile saved.")
        return redirect("accounts:profile")
    static = StaticDevice.objects.filter(user=request.user, name="backup").first()
    feed_url = settings.SITE_URL + reverse("grants:calendar_feed", args=[request.user.calendar_token])
    return render(
        request,
        "accounts/profile.html",
        {
            "form": form,
            "has_2fa": TOTPDevice.objects.filter(user=request.user, confirmed=True).exists(),
            "backup_remaining": static.token_set.count() if static else 0,
            "feed_url": feed_url,
            "require_2fa": settings.REQUIRE_2FA,
            "lab_pi": Person.lab_pi(),
        },
    )


def password_change(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, "Password changed.")
        return redirect("accounts:profile")
    return render(request, "accounts/password_change.html", {"form": form})


@require_POST
def rotate_calendar_token(request):
    request.user.rotate_calendar_token()
    messages.success(request, "Calendar feed link regenerated. Update any calendar app subscribed to the old link.")
    return redirect("accounts:profile")


# --- User management (owner only) -------------------------------------------


@owner_required
def user_list(request):
    users = User.objects.all().order_by("-is_active", "first_name", "username")
    totp_users = set(TOTPDevice.objects.filter(confirmed=True).values_list("user_id", flat=True))
    return render(request, "accounts/user_list.html", {"users": users, "totp_users": totp_users})


@owner_required
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        messages.success(request, f"Account created for {user}.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": "Add user"})


@owner_required
def user_update(request, pk):
    target = get_object_or_404(User, pk=pk)
    form = UserUpdateForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        if target == request.user and (
            form.cleaned_data["role"] != User.Role.OWNER or not form.cleaned_data["is_active"]
        ) and not User.objects.filter(role=User.Role.OWNER, is_active=True).exclude(pk=target.pk).exists():
            form.add_error(None, "You are the only active owner. Promote someone else first.")
        else:
            form.save()
            messages.success(request, f"Saved {target}.")
            return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": f"Edit {target}", "target": target})


@owner_required
@require_POST
def user_reset_2fa(request, pk):
    target = get_object_or_404(User, pk=pk)
    for device in devices_for_user(target, confirmed=None):
        device.delete()
    messages.success(request, f"Two-factor authentication reset for {target}. They will re-enroll at next sign-in if required.")
    return redirect("accounts:user_list")


@owner_required
@require_POST
def user_unlock(request, pk):
    target = get_object_or_404(User, pk=pk)
    axes_reset(username=target.username)
    messages.success(request, f"Sign-in lockout cleared for {target}.")
    return redirect("accounts:user_list")


# Password reset uses Django's views; exempt them from the login requirement.
password_reset = login_not_required(
    auth_views.PasswordResetView.as_view(
        template_name="accounts/password_reset.html",
        email_template_name="accounts/email/password_reset.txt",
        subject_template_name="accounts/email/password_reset_subject.txt",
        success_url=reverse_lazy("accounts:password_reset_done"),
    )
)
password_reset_done = login_not_required(
    auth_views.PasswordResetDoneView.as_view(template_name="accounts/password_reset_done.html")
)
password_reset_confirm = login_not_required(
    auth_views.PasswordResetConfirmView.as_view(
        template_name="accounts/password_reset_confirm.html",
        success_url=reverse_lazy("accounts:password_reset_complete"),
    )
)
password_reset_complete = login_not_required(
    auth_views.PasswordResetCompleteView.as_view(template_name="accounts/password_reset_complete.html")
)
