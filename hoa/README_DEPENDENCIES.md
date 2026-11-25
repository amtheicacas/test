# Which package list is which

| File | Who uses it | What it is |
|---|---|---|
| `requirements.txt` | your PC (`start-site.bat`) | what the app needs; versions are loose ranges |
| `requirements-dev.txt` | running the automated tests | `requirements.txt` plus `pytest` and `pytest-django` (pinned) |
| `requirements-production.txt` | the *wanted* list for the live site | loose ranges; the source the lock is made from |
| `requirements-production.lock.txt` | **Render's build** (`build.sh`) | EXACT versions of every package, so a new release elsewhere can never change the live site by surprise |

## Updating the pinned versions (on purpose, e.g. for a security fix)
Do this in a fresh environment, then run all the tests, then commit the new lock file:
```
python -m venv lockvenv
lockvenv\Scripts\pip install -r requirements-production.txt
lockvenv\Scripts\pip freeze > new-lock.txt
```
Then copy the result over `requirements-production.lock.txt` (keep its comment header, leave out `pytest`, `pytest-django`, `iniconfig`, `pluggy`, `Pygments`, `pip`), run `python -m pytest -q`, and commit. A vulnerability scan (`pip-audit -r requirements-production.lock.txt`) should say none found.

Python version: the live site runs Python 3.13 (Render `PYTHON_VERSION`). The lock was made and tested on 3.13.
