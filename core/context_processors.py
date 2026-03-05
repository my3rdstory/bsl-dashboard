from .models import Member, ensure_superadmin_member


def nav_profile(request):
    display_name = ""
    is_backoffice_admin = False
    is_active_member = False
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        member = Member.objects.filter(user=user).only(
            "display_name",
            "is_backoffice_admin",
            "status",
        ).first()
        if member is None and bool(getattr(user, "is_superuser", False)):
            member = ensure_superadmin_member(user)
        if member is not None:
            display_name = str(member.display_name)
            is_backoffice_admin = bool(member.is_backoffice_admin)
            is_active_member = member.status == Member.STATUS_ACTIVE
    return {
        "nav_display_name": display_name,
        "nav_is_backoffice_admin": is_backoffice_admin,
        "nav_is_active_member": is_active_member,
        "nav_can_access_member_menus": is_active_member or is_backoffice_admin,
    }
