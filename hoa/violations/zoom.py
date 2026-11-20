"""Create the Zoom meeting for a committee hearing from inside the app, so nobody has to go to Zoom and paste a link.

Uses a Zoom *Server-to-Server OAuth* app (Zoom Marketplace -> Develop -> Build App -> Server-to-Server OAuth) with the scope to create
meetings. Set ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET and ZOOM_HOST_EMAIL (the email of the Zoom user who hosts: required).
The request shapes follow Zoom's documented REST API (v2) but have not been run against a live Zoom account: try one meeting and check it."""
import base64
import json
import time
import urllib.parse
import urllib.request

from django.conf import settings

_token = {"value": "", "expires": 0.0}


class ZoomError(Exception):
    pass


def configured():
    return bool(settings.ZOOM_ACCOUNT_ID and settings.ZOOM_CLIENT_ID and settings.ZOOM_CLIENT_SECRET)


def _request(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def access_token():
    if _token["value"] and _token["expires"] > time.time() + 60:
        return _token["value"]
    basic = base64.b64encode(f"{settings.ZOOM_CLIENT_ID}:{settings.ZOOM_CLIENT_SECRET}".encode()).decode()
    q = urllib.parse.urlencode({"grant_type": "account_credentials", "account_id": settings.ZOOM_ACCOUNT_ID})
    r = _request("POST", f"https://zoom.us/oauth/token?{q}", headers={"Authorization": f"Basic {basic}"})
    _token["value"], _token["expires"] = r["access_token"], time.time() + int(r.get("expires_in", 3600))
    return _token["value"]


def create_meeting(topic, starts_at, minutes=None):
    """Schedule a Zoom meeting; returns {link, meeting_id, passcode, dial_in, api_id}. Raises ZoomError with a plain message."""
    if not configured():
        raise ZoomError("Zoom is not connected yet.")
    from django.utils import timezone
    start = timezone.localtime(starts_at) if timezone.is_aware(starts_at) else starts_at
    body = {"topic": topic[:200], "type": 2, "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": settings.TIME_ZONE,
            "duration": int(minutes or settings.ZOOM_MEETING_MINUTES), "agenda": "Compliance Committee homeowner hearings",
            "settings": {"join_before_host": False, "waiting_room": True, "mute_upon_entry": True, "approval_type": 2}}
    host = settings.ZOOM_HOST_EMAIL
    if not host:   # a Server-to-Server app is not a person, so Zoom needs to be told whose meeting this is
        raise ZoomError("Set ZOOM_HOST_EMAIL to the email of the Zoom user who hosts the hearings.")
    try:
        r = _request("POST", f"https://api.zoom.us/v2/users/{urllib.parse.quote(host)}/meetings", body,
                     {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"})
    except Exception as e:   # network, bad credentials, no permission...
        raise ZoomError(f"Zoom did not create the meeting ({e}).")
    dial = []
    for n in (r.get("settings") or {}).get("global_dial_in_numbers") or []:
        dial.append(f"{n.get('number', '')} {n.get('city') or n.get('country_name') or ''}".strip())
    return {"link": r.get("join_url", ""), "meeting_id": str(r.get("id", "")), "passcode": r.get("password", ""),
            "dial_in": "\n".join(x for x in dial if x), "api_id": str(r.get("id", ""))}
