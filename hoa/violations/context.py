from django.conf import settings


def hoa(request):
    return {"HOA_NAME": settings.HOA_NAME, "MANAGER_PHONE": settings.OFFICE_PHONE, "MANAGER_EMAIL": settings.MANAGER_EMAIL}
