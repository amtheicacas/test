from django.conf import settings


def hoa(request):
    return {"HOA_NAME": settings.HOA_NAME}
