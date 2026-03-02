from app.domain.entities.document import DocumentEntity
from app.domain.entities.signature_region import SignatureRegionEntity
from app.presentation.controllers.schemas import DocumentResponse, RegionResponse


def to_region_response(region: SignatureRegionEntity) -> RegionResponse:
    return RegionResponse(
        id=region.id,
        page_number=region.box.page_number,
        x=region.box.x,
        y=region.box.y,
        width=region.box.width,
        height=region.box.height,
        assigned_to=region.assigned_to,
        signed=region.signed,
        signed_at=region.signed_at,
    )


def to_document_response(document: DocumentEntity) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        title=document.title,
        uploaded_by=document.uploaded_by,
        status=document.status,
        total_pages=document.total_pages,
        final_hash=document.final_hash,
        created_at=document.created_at,
        regions=[to_region_response(region) for region in document.regions],
    )
