from django.conf import settings

from . import accounts


def hoa(request):
    return {"HOA_NAME": settings.HOA_NAME, "MANAGER_PHONE": settings.OFFICE_PHONE, "MANAGER_EMAIL": settings.MANAGER_EMAIL,
            "ROLE": accounts.role_of(request.user)}
