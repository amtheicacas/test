#!/usr/bin/env bash
# Render build step: install what the live site needs and gather the CSS/JS/images. (Database changes run separately, as the pre-deploy command.)
set -o errexit
pip install --upgrade pip
pip install -r requirements-production.txt
python manage.py collectstatic --no-input
