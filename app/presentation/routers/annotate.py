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
import io
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel
from pypdf import PdfReader

from app.application.services.integration_service import _decrypt_esign_token, decrypt_path
from app.core.config import get_settings
from app.domain.entities.annotation import AnnotationEntity, AnnotationKind
from app.infrastructure.pdf_engine.signature_pdf_service import SignaturePdfService
from app.presentation.routers.integration import _get_sqlserver_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotate", tags=["Annotate"])

# CpaDesk endpoint we POST the annotated PDF bytes to (option B, like the sign flow).
# Base URL is derived from the decrypted FileURL host; confirm the path with CPA.
CALLBACK_PATH = "/api/ESign/ProcessHighlightDocument"


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


async def _fetch_pdf_bytes(file_url_encrypted: str) -> bytes:
    """Decrypt the FileURL and fetch the source PDF (remote URL or local path)."""
    if not file_url_encrypted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No FileURL on highlight request")
    url = decrypt_path(file_url_encrypted)
    if url.startswith("http://") or url.startswith("https://"):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                return resp.content
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch annotate PDF from %s: %s", url, exc)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not fetch source PDF") from exc
    path = Path(url)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source PDF not found")
    return path.read_bytes()


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
    pdf_bytes = await _fetch_pdf_bytes(row.get("FileURL") or "")
    total_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    return {"ref": ref, "file_name": row.get("FileName") or "document.pdf", "total_pages": total_pages}


@router.get("/{ref}/file")
async def annotate_file(ref: str) -> Response:
    """Stream the source PDF for annotation."""
    row = await _get_highlight_row(ref)
    pdf_bytes = await _fetch_pdf_bytes(row.get("FileURL") or "")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.post("/save")
async def annotate_save(payload: AnnotateSaveRequest) -> dict:
    """Burn the annotations into the PDF and POST the result back to CpaDesk."""
    row = await _get_highlight_row(payload.ref)
    pdf_bytes = await _fetch_pdf_bytes(row.get("FileURL") or "")

    settings = get_settings()
    storage = settings.original_storage_dir
    src = storage / f"annotate_src_{uuid4()}.pdf"
    out = storage / f"annotate_out_{uuid4()}.pdf"
    try:
        src.write_bytes(pdf_bytes)
        annotations = [_to_annotation_entity(a) for a in payload.annotations]
        SignaturePdfService().apply_signatures(
            source_pdf=src,
            target_pdf=out,
            signatures=[],
            annotations=annotations,
        )
        result_bytes = out.read_bytes()
    finally:
        for f in (src, out):
            try:
                f.unlink()
            except OSError:
                pass

    file_b64 = base64.b64encode(result_bytes).decode("utf-8")
    delivered = await _send_highlight_callback(row, file_b64)
    # Also return the bytes so the caller has them regardless of callback delivery.
    return {"ok": True, "callback_delivered": delivered, "file_bytes": file_b64}
