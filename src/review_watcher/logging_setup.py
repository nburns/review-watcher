import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "review_watcher"
_LOG_FILE_NAME = "review-watcher.log"
_FORMAT = "ts=%(asctime)sZ level=%(levelname)s logger=%(name)s msg=%(message)s"


def configure_logging(log_dir: Path, *, verbose: bool) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)
    # launchd and an interactive shell can disagree on local time zone, so log in UTC.
    formatter.converter = time.gmtime
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / _LOG_FILE_NAME, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if sys.stderr.isatty():
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
