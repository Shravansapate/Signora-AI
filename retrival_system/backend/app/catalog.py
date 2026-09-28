"""Short catalog transactions use one lock order across registry and playback.

Shared readers pin a consistent catalog; writers serialize the small initial library's
pointer/review changes. Inspection, upload and conversion never run under this lock.
"""

from sqlalchemy import text


def catalog_lock(session, *, write=False):
    function = "pg_advisory_xact_lock" if write else "pg_advisory_xact_lock_shared"
    session.execute(text(f"SELECT {function}(1397311310, 1)"))
