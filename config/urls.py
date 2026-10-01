from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import HttpResponse
from django.urls import include, path


@login_not_required
def healthz(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


admin.site.site_header = "Grant Tracker administration"
admin.site.site_title = "Grant Tracker admin"

urlpatterns = [
    path("healthz", healthz),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("grants.urls")),
]

handler403 = "grants.views.errors.permission_denied"
handler404 = "grants.views.errors.not_found"
