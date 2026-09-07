"""
Structured logging setup. Azure App Service captures stdout/stderr
automatically (Log Stream, and Application Insights if wired up later),
but scattered print() calls give an operator far less to work with than
leveled, timestamped, named-logger output when something actually needs
debugging in production.
"""
import logging
import sys


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,  # re-configure cleanly even if something else touched logging first (e.g. under pytest)
    )
