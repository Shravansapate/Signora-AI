"""Display-only credentials shared by HTTP and WebSocket authentication."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.config import Principal


def authenticate(settings, sessions, token):
    if not token or len(token) > 512:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    identity = next(
        (p for key, p in settings.principals.items() if secrets.compare_digest(key, digest)),
        None,
    )
    if identity:
        if identity.expires_at <= datetime.now(UTC):
            return None
        if "display" in identity.roles:
            # Once a device has a managed credential, replacement must also revoke
            # any formerly configured static credential for that device.
            with sessions() as session:
                managed = session.scalar(
                    text("""SELECT EXISTS(
                    SELECT 1 FROM display_credentials c JOIN display_devices d
                    ON d.id=c.display_id WHERE d.subject=:subject)"""),
                    {"subject": identity.subject},
                )
            if managed:
                return None
        return identity
    if not token.startswith("sgd_"):
        return None
    with sessions() as session:
        row = (
            session.execute(
                text("""SELECT d.id,d.subject,d.station_id,c.expires_at
            FROM display_credentials c JOIN display_devices d ON d.id=c.display_id
            WHERE c.token_hash=:digest AND d.enabled AND c.expires_at>now()"""),
                {"digest": digest},
            )
            .mappings()
            .first()
        )
    if not row:
        return None
    return Principal(
        subject=row["subject"],
        roles={"display"},
        station_ids={row["station_id"]},
        expires_at=row["expires_at"],
        display_id=row["id"],
        credential_digest=digest,
    )


def issue_token(session, display_id, actor, days):
    """Caller holds the device/catalog transaction; raw token is returned once."""
    token = "sgd_" + secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=days)
    session.execute(
        text("""INSERT INTO display_credentials
        (display_id,token_hash,expires_at,issued_by) VALUES(:id,:hash,:expires,:actor)
        ON CONFLICT(display_id) DO UPDATE SET token_hash=:hash,expires_at=:expires,
        issued_at=now(),issued_by=:actor"""),
        {
            "id": display_id,
            "hash": hashlib.sha256(token.encode()).hexdigest(),
            "expires": expires,
            "actor": actor,
        },
    )
    return {"access_token": token, "token_expires_at": expires}
