from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm

from .models import User


class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}))
    password = forms.CharField(
        strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )


class TokenForm(forms.Form):
    token = forms.CharField(
        label="Authentication code",
        max_length=16,
        widget=forms.TextInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True, "placeholder": "123456"}
        ),
        help_text="6-digit code from your authenticator app, or a backup code.",
    )

    def clean_token(self):
        return self.cleaned_data["token"].replace(" ", "").strip()


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "email_digest", "digest_days_ahead"]
        labels = {
            "email_digest": "Weekly email digest",
            "digest_days_ahead": "Digest look-ahead (days)",
        }


class UserCreateForm(forms.ModelForm):
    password = forms.CharField(
        label="Temporary password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Share this securely; the user can change it from their profile.",
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "role"]

    def clean_password(self):
        password = self.cleaned_data["password"]
        password_validation.validate_password(password)
        return password

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user


class UserUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "role", "is_active"]
        labels = {"is_active": "Active (can sign in)"}
