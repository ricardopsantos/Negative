# summary: logging filter that drops /healthz lines from the gunicorn access log (PH95); loaded by gunicorn.conf.py.
import logging


class NoHealthz(logging.Filter):
    """The Docker healthcheck (/healthz every 20 s) is noise in the access log."""

    def filter(self, record):
        return "/healthz" not in record.getMessage()
