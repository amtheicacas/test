"""Settings the gunicorn web server picks up automatically from this folder (the live host runs `gunicorn config.wsgi`).

No access log: sign-in links and private case links carry a secret in the address (/accounts/link/<token>/, /r/<token>/), and an access
log would write every one of them to disk. Errors still go to the host's log."""
accesslog = None
errorlog = "-"
# NOTE: a host can override this file (Render sets its own GUNICORN_CMD_ARGS with --access-logfile -). The start command therefore ALSO says
# `--access-logfile /dev/null`, which wins over everything: gunicorn -c gunicorn.conf.py --access-logfile /dev/null config.wsgi:application
timeout = 60
graceful_timeout = 30
