from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("verify/", views.verify, name="verify"),
    path("profile/", views.profile, name="profile"),
    path("password/", views.password_change, name="password_change"),
    path("2fa/setup/", views.two_factor_setup, name="2fa_setup"),
    path("2fa/disable/", views.two_factor_disable, name="2fa_disable"),
    path("2fa/backup-codes/", views.regenerate_backup_codes, name="2fa_backup_codes"),
    path("calendar-token/rotate/", views.rotate_calendar_token, name="rotate_calendar_token"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/", views.user_update, name="user_update"),
    path("users/<int:pk>/reset-2fa/", views.user_reset_2fa, name="user_reset_2fa"),
    path("users/<int:pk>/unlock/", views.user_unlock, name="user_unlock"),
    path("password-reset/", views.password_reset, name="password_reset"),
    path("password-reset/sent/", views.password_reset_done, name="password_reset_done"),
    path("password-reset/<uidb64>/<token>/", views.password_reset_confirm, name="password_reset_confirm"),
    path("password-reset/complete/", views.password_reset_complete, name="password_reset_complete"),
]
