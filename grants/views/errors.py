from django.shortcuts import render


def permission_denied(request, exception=None):
    message = str(exception) if exception and str(exception) else "You don't have permission to do that."
    return render(request, "errors/403.html", {"message": message}, status=403)


def not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)
