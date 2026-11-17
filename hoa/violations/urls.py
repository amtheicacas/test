from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("violations/", views.violation_list, name="violation_list"),
    path("violations/new/", views.violation_create, name="violation_create"),
    path("violations/<int:pk>/", views.violation_detail, name="violation_detail"),
    path("violations/<int:pk>/<str:action>/", views.violation_action, name="violation_action"),
    path("notices/<int:pk>/mailed/", views.notice_mailed, name="notice_mailed"),
    path("notices/<int:pk>/pdf/", views.notice_pdf, name="notice_pdf"),
    path("board/", views.board_queue, name="board_queue"),
    path("photos/<int:pk>/", views.photo_file, name="photo"),
    path("r/<str:token>/", views.resident, name="resident"),
    path("r/<str:token>/photo/<int:pk>/", views.resident_photo, name="resident_photo"),
]
