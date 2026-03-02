from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    SIGNER = "SIGNER"


class DocumentStatus(StrEnum):
    DRAFT = "Draft"
    PENDING = "Pending"
    PARTIALLY_SIGNED = "Partially Signed"
    COMPLETED = "Completed"


class SignatureMethod(StrEnum):
    DRAW = "draw"
    TYPE = "type"
    UPLOAD = "upload"
