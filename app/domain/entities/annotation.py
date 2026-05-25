from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID


class AnnotationKind(str, Enum):
    """Type of admin annotation rendered on top of a document page."""

    HIGHLIGHT = "highlight"
    DRAWING = "drawing"
    TEXT = "text"


@dataclass(slots=True)
class AnnotationEntity:
    id: UUID
    document_id: UUID
    page_number: int
    kind: AnnotationKind
    # Normalized bounding box (0..1). For free-draw, this is the bounding rect of the strokes.
    x: float
    y: float
    width: float
    height: float
    color: str
    # Free-text comment for TEXT annotations. Empty otherwise.
    text: str
    # Serialized polyline data for DRAWING annotations (JSON array of normalized point lists).
    paths: str
    created_by: UUID
    created_at: datetime
