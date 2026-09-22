"""
Standalone annotate router — fully isolated from the ESign signing flow.

A caller opens our page with a `ref` (HighlightRequests.HighlightGuid). We look up
that row, validate its token, decrypt the FileURL, and serve the PDF for markup
(highlight / draw / comment only). On save we burn the annotations into the PDF and
POST the resulting bytes back to CpaDesk (callback, same pattern as the sign flow).

No signing, no regions, no ESignRequests/ESignClients — nothing is persisted on our
side (stateless). Security is the HighlightGuid + HighlightToken pair.
"""

import base64
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from pypdf import PdfReader

from app.application.services.integration_service import _decrypt_esign_token, decrypt_path
from app.core.config import get_settings
from app.domain.entities.annotation import AnnotationEntity, AnnotationKind
from app.infrastructure.pdf_engine.signature_pdf_service import SignaturePdfService
from app.infrastructure.sqlserver.sqlserver_client import SqlServerUnavailableError
from app.presentation.routers.integration import _get_sqlserver_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotate", tags=["Annotate"])

# CpaDesk endpoint we POST the annotated PDF bytes to (option B, like the sign flow).
# Base URL is derived from the decrypted FileURL host; confirm the path with CPA.
CALLBACK_PATH = "/api/ESign/ProcessHighlightDocument"

# The frontend loads /meta and /file together on every page open (one to get the
# page count, one to get the bytes), which used to mean downloading the same PDF
# from CPA's file host twice, back to back — doubling wait time and load on a
# host that can already be slow. Cache a local copy briefly by ref so repeat
# calls reuse the first call's download instead of refetching remotely.
#
# Cached as a real file (not in-memory bytes) so /file can be served via
# FileResponse, which supports HTTP Range requests — letting pdf.js render the
# first page as soon as enough bytes arrive instead of waiting for the whole
# file, the same way the Document Preview page already works. `owned` tracks
# whether we created the file ourselves (safe to delete on eviction) or it's
# CPA's own local path (never delete something we didn't create).
_PDF_FETCH_CACHE: dict[str, tuple[float, Path, bool]] = {}
_PDF_FETCH_CACHE_TTL_SECONDS = 300.0


def _pdf_cache_get(ref: str) -> Path | None:
    entry = _PDF_FETCH_CACHE.get(ref)
    if not entry:
        return None
    cached_at, path, owned = entry
    if time.monotonic() - cached_at > _PDF_FETCH_CACHE_TTL_SECONDS or not path.exists():
        _PDF_FETCH_CACHE.pop(ref, None)
        if owned:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return None
    return path


def _pdf_cache_set(ref: str, path: Path, owned: bool) -> None:
    now = time.monotonic()
    # Opportunistic cleanup so this never grows unbounded across many distinct refs.
    for key, (cached_at, old_path, old_owned) in list(_PDF_FETCH_CACHE.items()):
        if now - cached_at > _PDF_FETCH_CACHE_TTL_SECONDS:
            _PDF_FETCH_CACHE.pop(key, None)
            if old_owned:
                try:
                    old_path.unlink(missing_ok=True)
                except OSError:
                    pass
    _PDF_FETCH_CACHE[ref] = (now, path, owned)


# ── Schemas ───────────────────────────────────────────────────────────────────


class AnnotateItem(BaseModel):
    page_number: int
    kind: str  # "highlight" | "drawing" | "text"
    x: float
    y: float
    width: float
    height: float
    color: str = "#fde047"
    text: str = ""
    paths: str = ""


class AnnotateSaveRequest(BaseModel):
    ref: str
    annotations: list[AnnotateItem]


# ── Helpers ─────────────────────────────────────────────────────────────────--


async def _get_highlight_row(ref: str) -> dict:
    """Look up the HighlightRequests row by guid and validate its token."""
    ss = _get_sqlserver_client()
    if ss is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SQL Server not configured")

    try:
        rows = await ss.execute_query(
            """
            SELECT TOP 1 HighlightRequestID, HighlightGuid, HighlightToken, FileName, FileURL
            FROM   HighlightRequests
            WHERE  HighlightGuid = :guid
              AND  IsActive  = 1
              AND  IsDeleted = 0
            """,
            {"guid": ref},
        )
    except SqlServerUnavailableError as exc:
        logger.warning("Annotate lookup failed: SQL Server temporarily unreachable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Temporarily unable to reach CpaDesk — please try again in a few seconds",
        ) from exc
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Highlight request not found or inactive")

    row = rows[0]
    token = row.get("HighlightToken") or ""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Highlight token not set")
    try:
        decrypted = _decrypt_esign_token(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Highlight token could not be decrypted") from exc
    # Case-insensitive: SQL Server stores the guid uppercase; the decrypted token is lowercase.
    if decrypted.strip().lower() != str(ref).strip().lower():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Highlight token does not match ref")
    return row


async def _fetch_pdf_path(ref: str, file_url_encrypted: str) -> Path:
    """Decrypt the FileURL and fetch the source PDF, returning a local file path.

    /meta, /file and /save all need this same file — cached by ref so repeat
    calls reuse the first's download instead of hitting CPA's (sometimes slow)
    file host again. A real file (not in-memory bytes) so /file can be served
    with proper Range support.
    """
    cached = _pdf_cache_get(ref)
    if cached is not None:
        return cached

    if not file_url_encrypted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No FileURL on highlight request")
    url = decrypt_path(file_url_encrypted)
    if url.startswith("http://") or url.startswith("https://"):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                data = resp.content
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch annotate PDF from %s: %s", url, exc)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not fetch source PDF") from exc
        settings = get_settings()
        temp_path = settings.original_storage_dir / f"annotate_stream_{uuid4().hex}.pdf"
        temp_path.write_bytes(data)
        _pdf_cache_set(ref, temp_path, owned=True)
        return temp_path

    path = Path(url)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source PDF not found")
    _pdf_cache_set(ref, path, owned=False)
    return path


def _to_annotation_entity(item: AnnotateItem) -> AnnotationEntity:
    try:
        kind = AnnotationKind(item.kind)
    except ValueError:
        kind = AnnotationKind.HIGHLIGHT
    return AnnotationEntity(
        id=uuid4(),
        document_id=uuid4(),
        page_number=item.page_number,
        kind=kind,
        x=item.x,
        y=item.y,
        width=item.width,
        height=item.height,
        color=item.color,
        text=item.text,
        paths=item.paths,
        created_by=uuid4(),
        created_at=datetime.now(UTC),
    )


async def _mark_highlighted(ref: str) -> None:
    """Flip HighlightRequests.IsHighlight = 1 so CPA knows the doc was actually annotated.

    Rows left at 0 mean the user only viewed and closed — CPA can safely delete those.
    """
    ss = _get_sqlserver_client()
    if ss is None:
        return
    await ss.execute_non_query(
        """
        UPDATE HighlightRequests
        SET    IsHighlight = 1,
               UpdatedOn   = GETDATE()
        WHERE  HighlightGuid = :guid
        """,
        {"guid": ref},
    )


async def _send_highlight_callback(row: dict, file_b64: str) -> bool:
    """POST the annotated PDF bytes back to CpaDesk (base URL derived from FileURL host)."""
    decrypted = decrypt_path(row.get("FileURL") or "")
    parsed = urlparse(decrypted)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("Annotate callback skipped: cannot derive base URL from FileURL")
        return False
    base = f"{parsed.scheme}://{parsed.netloc}"
    payload = {
        "HighlightRequestID": row.get("HighlightRequestID"),
        "HighlightGuid": str(row.get("HighlightGuid")),
        "FileBytes": file_b64,
        "IsHighlight": True,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{base.rstrip('/')}{CALLBACK_PATH}", json=payload)
            resp.raise_for_status()
        logger.info("Highlight callback delivered for guid=%s", row.get("HighlightGuid"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Highlight callback failed for guid=%s: %s", row.get("HighlightGuid"), exc)
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/{ref}/meta")
async def annotate_meta(ref: str) -> dict:
    """Return file name + page count so the page can render the PDF."""
    row = await _get_highlight_row(ref)
    pdf_path = await _fetch_pdf_path(ref, row.get("FileURL") or "")
    total_pages = len(PdfReader(str(pdf_path)).pages)
    return {"ref": ref, "file_name": row.get("FileName") or "document.pdf", "total_pages": total_pages}


@router.get("/{ref}/file")
async def annotate_file(ref: str) -> Response:
    """Serve the source PDF for annotation, with Range support so pdf.js can
    render the first page as soon as enough bytes arrive instead of waiting
    for the whole file to transfer."""
    row = await _get_highlight_row(ref)
    pdf_path = await _fetch_pdf_path(ref, row.get("FileURL") or "")
    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.post("/save")
async def annotate_save(payload: AnnotateSaveRequest) -> dict:
    """Burn the annotations into the PDF and POST the result back to CpaDesk."""
    row = await _get_highlight_row(payload.ref)
    pdf_path = await _fetch_pdf_path(payload.ref, row.get("FileURL") or "")

    settings = get_settings()
    storage = settings.original_storage_dir
    out = storage / f"annotate_out_{uuid4()}.pdf"
    try:
        annotations = [_to_annotation_entity(a) for a in payload.annotations]
        SignaturePdfService().apply_signatures(
            source_pdf=pdf_path,
            target_pdf=out,
            signatures=[],
            annotations=annotations,
        )
        result_bytes = out.read_bytes()
    finally:
        try:
            out.unlink()
        except OSError:
            pass

    # Mark the request as actually annotated so CPA keeps the row (viewed-only stays 0).
    await _mark_highlighted(payload.ref)

    file_b64 = base64.b64encode(result_bytes).decode("utf-8")
    delivered = await _send_highlight_callback(row, file_b64)
    # Also return the bytes so the caller has them regardless of callback delivery.
    return {"ok": True, "is_highlight": True, "callback_delivered": delivered, "file_bytes": file_b64}
