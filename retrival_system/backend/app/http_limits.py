"""Enforce actual received bytes, including clients that understate Content-Length."""

from starlette.exceptions import HTTPException

from app.audio import MAX_AUDIO_BYTES


def asset_upload(path):
    return path == "/api/v1/admin/motions/stage" or (
        path.startswith("/api/v1/admin/signs/")
        and (path.endswith("/motions") or path.endswith("/metadata"))
    )


def request_limit(path):
    if asset_upload(path):
        return 160 * 1024 * 1024 + 65536
    if path == "/api/v1/voice/transcribe":
        return MAX_AUDIO_BYTES + 65536
    return 32768


class ReceivedBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await self.app(scope, receive, send)
        limit, received = request_limit(scope["path"]), 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "Request exceeds size limit")
            return message

        await self.app(scope, bounded_receive, send)
