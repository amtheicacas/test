"""Who is asking, and "too many requests" counters (shared by the sign-in, password-reset and private-link pages)."""
import logging
import time

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)


def client_ip(request):
    """The visitor's real address. Behind the host's load balancer (Render) every request arrives from the proxy, so the real address
    is the one the proxy appended to X-Forwarded-For: the TRUSTED_PROXY_COUNT-th entry from the right. Entries further left are
    supplied by the visitor and are ignored, so they can't be used to dodge a limit. With no proxy (your own PC) it is REMOTE_ADDR."""
    direct = request.META.get("REMOTE_ADDR", "")
    n = settings.TRUSTED_PROXY_COUNT
    if n > 0:
        hops = [h.strip() for h in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if h.strip()]
        if len(hops) >= n:
            return hops[-n]
    return direct


def hit(key, limit=3, window=3600):
    """Count one attempt for `key`; True once there have been `limit` earlier attempts within `window` seconds (this one is refused).
    The window starts at the FIRST attempt and later attempts do not extend it (before, every attempt, refused or not, started the whole hour again: someone who
    kept trying, or anyone who knew an owner's email address, could keep that owner locked out for ever). What is stored is (attempts, when the window began).
    If the counter store itself is broken this lets the request through: a broken counter must not lock owners out."""
    try:
        now = time.time()
        state = cache.get(key)
        if isinstance(state, tuple) and len(state) == 2 and now - state[1] < window:
            n, started = state
        else:       # no window yet, or it is over (a plain number saved by an older version counts as attempts made just now)
            n, started = (state if isinstance(state, int) and not isinstance(state, bool) else 0), now
        cache.set(key, (n + 1, started), max(1, int(window - (now - started))))        # it expires when the window does, not a whole window after this attempt
        return n >= limit
    except Exception:       # noqa: BLE001
        log.exception("throttle counter unavailable")
        return False
