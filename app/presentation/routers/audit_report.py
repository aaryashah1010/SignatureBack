"""
Audit-report router.

CpaDesk calls this once a document is fully signed. They send the ESignRequestID plus
the audit trail; we take the signed PDF we already produced for that request, append a
"Final Audit Report" page at the end, and return the resulting PDF bytes.

Nothing is persisted — the appended PDF is returned to the caller, who stores it.
"""

import base64
import io
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.infrastructure.pdf_engine.signature_pdf_service import SignaturePdfService
from app.infrastructure.persistence.repositories import SqlAlchemyDocumentRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["Audit Report"])


class AuditHistoryItem(BaseModel):
    text: str = Field(default="", description="Event description, e.g. 'Document e-signed by John Doe'")
    timestamp: str = Field(default="", description="Free-text timestamp shown under the event")
    detail: str = Field(default="", description="Optional secondary line, e.g. 'Signature Date: ... Time Source: server'")


class AuditPageRequest(BaseModel):
    esign_request_id: int = Field(alias="ESignRequestID")
    title: str = Field(default="", alias="Title")
    report_date: str = Field(default="", alias="ReportDate")
    created_on: str = Field(default="", alias="CreatedOn")
    created_by: str = Field(default="", alias="CreatedBy")
    status: str = Field(default="", alias="Status")
    transaction_id: str = Field(default="", alias="TransactionId")
    history: list[AuditHistoryItem] = Field(default_factory=list, alias="History")

    model_config = {"populate_by_name": True}


@router.post("/audit-page")
async def append_audit_page(
    payload: AuditPageRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict:
    """Append a Final Audit Report page to the signed PDF for the given ESignRequestID."""
    doc_repo = SqlAlchemyDocumentRepository(session)
    document = await doc_repo.get_by_external_document_id(str(payload.esign_request_id))
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No document found for ESignRequestID {payload.esign_request_id}",
        )

    # Prefer the signed PDF; fall back to the original if nothing is signed yet.
    source_path = Path(document.final_path or document.original_path)
    if not source_path.exists():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source PDF not found on disk")

    reader = PdfReader(str(source_path))
    if not reader.pages:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source PDF has no pages")

    first_page = reader.pages[0]
    page_size = (float(first_page.mediabox.width), float(first_page.mediabox.height))

    audit_pdf_bytes = SignaturePdfService().render_audit_report(
        page_size=page_size,
        title=payload.title or document.title,
        report_date=payload.report_date,
        created_on=payload.created_on,
        created_by=payload.created_by,
        status=payload.status,
        transaction_id=payload.transaction_id,
        history=[item.model_dump() for item in payload.history],
    )

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for page in PdfReader(io.BytesIO(audit_pdf_bytes)).pages:
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    final_bytes = out.getvalue()

    logger.info(
        "Audit page appended for ESignRequestID=%s (pages %s -> %s)",
        payload.esign_request_id,
        len(reader.pages),
        len(writer.pages),
    )
    return {
        "ok": True,
        "ESignRequestID": payload.esign_request_id,
        "FileBytes": base64.b64encode(final_bytes).decode("utf-8"),
    }
