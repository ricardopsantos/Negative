# summary: Photos gunicorn settings: one worker, 8 threads on :9556; access log to stdout and to DATA_DIR/logs/access.log (rotated), which survives a redeploy.
import os

bind = "0.0.0.0:9556"
workers = 1  # one process: the scan jobs, unlock state and sync acks live in memory
threads = 8
timeout = 120
accesslog = "-"
# The access log names the real client (Caddy's X-Forwarded-For), not the docker gateway.
access_log_format = '%({x-forwarded-for}i)s %(h)s "%(r)s" %(s)s %(b)s %(M)sms'

# PH67: the docker log starts empty after every reconcile (the container is recreated); the file in DATA_DIR keeps the
# history (10 MB x 5).
_logs = os.path.join(os.environ.get("PHOTOS_DATA_DIR", "./data"), "logs")
try:
    os.makedirs(_logs, exist_ok=True)
    _file = {"class": "logging.handlers.RotatingFileHandler", "filename": os.path.join(_logs, "access.log"),
             "maxBytes": 10 * 1024 * 1024, "backupCount": 5, "formatter": "plain"}
except OSError:
    _file = None
logconfig_dict = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(message)s"}, "generic": {"format": "%(asctime)s [%(process)d] [%(levelname)s] %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout", "formatter": "plain"},
                 "error_console": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "generic"},
                 **({"file": _file} if _file else {})},
    "loggers": {"gunicorn.access": {"handlers": ["console"] + (["file"] if _file else []), "level": "INFO", "propagate": False},
                "gunicorn.error": {"handlers": ["error_console"], "level": "INFO", "propagate": False}},
}


def post_worker_init(worker):
    import server
    server.start_background()
