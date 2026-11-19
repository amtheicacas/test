import pytest


@pytest.fixture(autouse=True)
def _no_mfa_by_default(settings):
    """Older tests exercise the workflow; MFA behavior is tested explicitly in test_security.py."""
    settings.MFA_REQUIRED_ROLES = ()


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The sign-in throttles live in the cache; start every test with them empty."""
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()
