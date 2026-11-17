import pytest


@pytest.fixture(autouse=True)
def _no_mfa_by_default(settings):
    """Older tests exercise the workflow; MFA behavior is tested explicitly in test_security.py."""
    settings.MFA_REQUIRED_ROLES = ()
