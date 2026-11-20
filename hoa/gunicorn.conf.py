"""Settings the gunicorn web server picks up automatically from this folder (the live host runs `gunicorn config.wsgi`).

No access log: sign-in links and private case links carry a secret in the address (/accounts/link/<token>/, /r/<token>/), and an access
log would write every one of them to disk. Errors still go to the host's log."""
accesslog = None
errorlog = "-"
timeout = 60
graceful_timeout = 30
