"""Settings for the automated tests: the normal settings, run as a development (test) site. A live site refuses to start without
real hosting settings, so the tests say up front that they are not one (see violations/tests/test_deploy_safety.py)."""
import os

os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("HOA_ENV_FILE", "no-local-env-file-while-testing")   # never read the developer's own local.env

from .settings import *  # noqa: E402,F401,F403
