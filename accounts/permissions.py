from functools import wraps

from django.core.exceptions import PermissionDenied


class EditorRequiredMixin:
    """Owners and editors may change data; viewers are read-only."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.can_edit:
            raise PermissionDenied("Your account is read-only.")
        return super().dispatch(request, *args, **kwargs)


class OwnerRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_owner:
            raise PermissionDenied("Only the portal owner can do this.")
        return super().dispatch(request, *args, **kwargs)


def editor_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.can_edit:
            raise PermissionDenied("Your account is read-only.")
        return view(request, *args, **kwargs)

    return wrapper


def owner_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_owner:
            raise PermissionDenied("Only the portal owner can do this.")
        return view(request, *args, **kwargs)

    return wrapper
