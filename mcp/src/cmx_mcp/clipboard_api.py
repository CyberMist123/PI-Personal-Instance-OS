"""Starlette routes for /clipboard-api.

Policy lives in clipboard_store; this module only parses input, enforces the
request-level boundary (identity + Origin) and shapes responses. Every response
is no-store, and every download is attachment + nosniff.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import clipboard_search as search
from .clipboard_db import (
    ENTRY_BYTE_LIMIT,
    FILE_LIMIT,
    VIEW_TEMPORARY,
    ClipboardError,
)
from .clipboard_files import ClipboardFiles
from .clipboard_store import ClipboardStore
from .web_auth import WebIdentity, verify_web_identity

NO_STORE = {"Cache-Control": "no-store"}
MUTATING = frozenset({"POST", "PATCH", "DELETE", "PUT"})
MAX_FORM_FIELDS = 8


def _json(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth[7:].strip()


class ClipboardRoutes:
    def __init__(
        self,
        *,
        store: ClipboardStore,
        files: ClipboardFiles,
        base_url: str,
        allowed_origins: Iterable[str],
    ) -> None:
        self.store = store
        self.files = files
        self.base_url = base_url
        self.allowed_origins = {o.rstrip("/") for o in allowed_origins}

    # ---------- boundary ----------

    async def _identity(self, request: Request) -> WebIdentity:
        token = _bearer(request)
        if not token:
            raise ClipboardError("unauthorized", status=401)
        identity = await run_in_threadpool(verify_web_identity, self.base_url, token)
        if identity is None:
            raise ClipboardError("unauthorized", status=401)
        return identity

    def _check_origin(self, request: Request) -> None:
        if request.method not in MUTATING:
            return
        # Fail closed: a missing Origin on a state-changing request is exactly
        # the shape a cross-site form post would have.
        origin = request.headers.get("origin", "").rstrip("/")
        if not origin or origin not in self.allowed_origins:
            raise ClipboardError("invalid_origin", status=403)

    # ---------- routes ----------

    async def list_entries(self, request: Request) -> Response:
        identity = await self._identity(request)
        params = request.query_params
        view = params.get("view") or VIEW_TEMPORARY
        query = search.normalize_query(params.get("q"))
        allowed: set[str] | None = None
        if query:
            rows = await run_in_threadpool(self.store.search_rows, identity.account_id)
            allowed = search.matching_entry_ids(rows, query)
        entries, truncated = await run_in_threadpool(
            lambda: self.store.list_entries(
                identity.account_id,
                view=view,
                topic=params.get("topic") or None,
                kind=params.get("type") or None,
                allowed_ids=allowed,
            )
        )
        return _json({"entries": entries, "truncated": truncated, "view": view})

    async def usage(self, request: Request) -> Response:
        identity = await self._identity(request)
        return _json(await run_in_threadpool(self.store.usage, identity.account_id))

    async def create_entry(self, request: Request) -> Response:
        identity = await self._identity(request)
        self._check_origin(request)
        form = await request.form(max_files=FILE_LIMIT + 1, max_fields=MAX_FORM_FIELDS)
        try:
            text = str(form.get("text") or "")
            uploads = [u for u in form.getlist("files") if hasattr(u, "filename")]
            if len(uploads) > FILE_LIMIT:
                raise ClipboardError("too_many_files", max_files=FILE_LIMIT)
            entry = await run_in_threadpool(self._store_upload, identity.account_id, text, uploads)
        finally:
            await form.close()
        return _json(entry, status=201)

    def _store_upload(self, owner: str, text: str, uploads: list[Any]) -> dict[str, Any]:
        """Blocking half of create: stage, insert, promote, or clean up.

        Staging is capped by the per-entry limit so a caller cannot make us
        write unbounded bytes. The account quota is checked authoritatively
        inside create_entry's transaction, where it cannot race another upload.
        """
        budget = ENTRY_BYTE_LIMIT - 1 - len(text.encode("utf-8"))
        batch = self.files.new_batch()
        try:
            staged = [
                self.files.stage(
                    batch,
                    upload.file,
                    filename=str(getattr(upload, "filename", "") or ""),
                    content_type=str(getattr(upload, "content_type", "") or ""),
                    budget=budget,
                )
                for upload in uploads
            ]
            return self.store.create_entry(
                owner, text=text, staged=staged,
                promote=lambda entry_id: self.files.promote(batch, entry_id),
            )
        except BaseException:
            # Any failure — validation, quota, disk, or a rollback inside the
            # transaction — must leave nothing behind in staging.
            self.files.discard(batch)
            raise

    async def patch_entry(self, request: Request) -> Response:
        identity = await self._identity(request)
        self._check_origin(request)
        entry_id = str(request.path_params.get("entry_id") or "")
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            raise ClipboardError("invalid_json")
        if not isinstance(body, dict):
            raise ClipboardError("invalid_json")
        unknown = set(body) - {"favorite", "topic"}
        if unknown:
            raise ClipboardError("unsupported_field", fields=sorted(unknown))
        if "favorite" in body:
            if not isinstance(body["favorite"], bool):
                raise ClipboardError("invalid_favorite")
            await run_in_threadpool(
                self.store.set_favorite, identity.account_id, entry_id, body["favorite"]
            )
        if "topic" in body:
            topic = body["topic"]
            if topic is not None and not isinstance(topic, str):
                raise ClipboardError("invalid_topic")
            await run_in_threadpool(self.store.set_topic, identity.account_id, entry_id, topic)
        entry = await run_in_threadpool(self.store.get_entry, identity.account_id, entry_id)
        return _json(entry)

    async def delete_entry(self, request: Request) -> Response:
        identity = await self._identity(request)
        self._check_origin(request)
        entry_id = str(request.path_params.get("entry_id") or "")
        removed = await run_in_threadpool(
            self.store.delete_entries, identity.account_id, [entry_id]
        )
        if not removed:
            raise ClipboardError("not_found", status=404)
        for eid in removed:
            self.files.delete_entry(eid)
        return _json({"removed": len(removed)})

    async def delete_many(self, request: Request) -> Response:
        identity = await self._identity(request)
        self._check_origin(request)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            raise ClipboardError("invalid_json")
        ids = body.get("entry_ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            # No "empty means everything": the client must name what it deletes.
            raise ClipboardError("entry_ids_required")
        removed = await run_in_threadpool(self.store.delete_entries, identity.account_id, ids)
        for eid in removed:
            self.files.delete_entry(eid)
        return _json({"removed": len(removed)})

    async def delete_file(self, request: Request) -> Response:
        identity = await self._identity(request)
        self._check_origin(request)
        entry_id = str(request.path_params.get("entry_id") or "")
        file_id = str(request.path_params.get("file_id") or "")
        result = await run_in_threadpool(
            self.store.delete_file, identity.account_id, entry_id, file_id
        )
        self.files.delete_file(entry_id, file_id)
        if result["entry_removed"]:
            self.files.delete_entry(entry_id)
        return _json({"removed": 1, "entry_removed": result["entry_removed"]})

    async def download(self, request: Request) -> Response:
        identity = await self._identity(request)
        entry_id = str(request.path_params.get("entry_id") or "")
        file_id = str(request.path_params.get("file_id") or "")
        row = await run_in_threadpool(
            self.store.get_file, identity.account_id, entry_id, file_id
        )
        path = self.files.object_path(entry_id, file_id)
        if not path.is_file():
            raise ClipboardError("not_found", status=404)
        return FileResponse(
            path,
            filename=str(row["original_name"]),
            media_type="application/octet-stream",
            headers={
                **NO_STORE,
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    def sweep(self) -> int:
        removed = self.store.purge_expired()
        for entry_id in removed:
            self.files.delete_entry(entry_id)
        return len(removed)


async def _guard(request: Request, handler: Callable) -> Response:
    try:
        return await handler(request)
    except ClipboardError as exc:
        return _json(exc.payload(), status=exc.status)


def build_clipboard_routes(
    *,
    runtime: Path,
    base_url: str,
    allowed_origins: Iterable[str],
) -> tuple[list[Route], ClipboardRoutes]:
    store = ClipboardStore(runtime / "clipboard.sqlite3")
    store.initialize()
    files = ClipboardFiles(runtime / "clipboard")
    files.cleanup_staging()
    api = ClipboardRoutes(
        store=store, files=files, base_url=base_url, allowed_origins=allowed_origins
    )

    def route(path: str, handler: Callable, methods: list[str]) -> Route:
        async def endpoint(request: Request) -> Response:
            return await _guard(request, handler)

        return Route(path, endpoint, methods=methods)

    routes = [
        route("/clipboard-api/usage", api.usage, ["GET"]),
        # Must precede the templated entry routes so "delete-many" is never
        # parsed as an entry_id.
        route("/clipboard-api/entries/delete-many", api.delete_many, ["POST"]),
        route("/clipboard-api/entries", api.list_entries, ["GET"]),
        route("/clipboard-api/entries", api.create_entry, ["POST"]),
        route("/clipboard-api/entries/{entry_id}/files/{file_id}", api.download, ["GET"]),
        route("/clipboard-api/entries/{entry_id}/files/{file_id}", api.delete_file, ["DELETE"]),
        route("/clipboard-api/entries/{entry_id}", api.patch_entry, ["PATCH"]),
        route("/clipboard-api/entries/{entry_id}", api.delete_entry, ["DELETE"]),
    ]
    return routes, api
