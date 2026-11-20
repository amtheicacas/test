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


@pytest.fixture(autouse=True)
def _files_in_a_temporary_folder(settings, tmp_path):
    """Photos and the saved copies of letters are written to MEDIA_ROOT: never into the project's own media folder while testing."""
    settings.MEDIA_ROOT = tmp_path / "media"
