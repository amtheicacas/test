from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="dashboard"),
    path("violations/", views.violation_list, name="violation_list"),
    path("violations/new/", views.violation_create, name="violation_create"),
    path("violations/<int:pk>/", views.violation_detail, name="violation_detail"),
    path("violations/<int:pk>/<str:action>/", views.violation_action, name="violation_action"),
    path("notices/<int:pk>/mailed/", views.notice_mailed, name="notice_mailed"),
    path("notices/<int:pk>/pdf/", views.notice_pdf, name="notice_pdf"),
    path("board/", views.board_queue, name="board_queue"),
    path("meetings/", views.meetings, name="meetings"),
    path("meetings/<int:pk>/assign-all/", views.meeting_assign_all, name="meeting_assign_all"),
    path("committee/", views.committee_home, name="committee_home"),
    path("committee/meetings/<int:pk>/", views.committee_meeting, name="committee_meeting"),
    path("committee/cases/<int:pk>/", views.committee_case, name="committee_case"),
    path("committee/cases/<int:pk>/vote/", views.committee_vote, name="committee_vote"),
    path("photos/<int:pk>/", views.photo_file, name="photo"),
    path("me/", views.my_violations, name="my_violations"),
    path("me/<int:pk>/", views.my_violation, name="my_violation"),
    path("me/<int:pk>/state/", views.my_violation_state, name="my_violation_state"),
    path("map/", views.map_page, name="map"),
    path("api/map/", views.map_api, name="map_api"),
    path("r/<str:token>/", views.resident, name="resident"),
    path("r/<str:token>/photo/<int:pk>/", views.resident_photo, name="resident_photo"),
]
