import asyncio
from datetime import UTC, datetime
from pathlib import Path

from reportlab.pdfgen import canvas
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.domain.entities.enums import DocumentStatus, UserRole
from app.infrastructure.persistence.models import DocumentModel, UserModel


def _create_demo_pdf(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(target))
    pdf.setFont("Helvetica", 16)
    pdf.drawString(72, 760, "Demo Agreement")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 730, "This is a seeded sample PDF for digital signature workflow.")
    pdf.drawString(72, 710, "Admin can create regions and signer can complete signature.")
    pdf.showPage()
    pdf.save()


async def seed() -> None:
    settings = get_settings()

    async with AsyncSessionLocal() as session:
        admin_result = await session.execute(select(UserModel).where(UserModel.email == "admin@example.com"))
        admin = admin_result.scalar_one_or_none()
        if not admin:
            admin = UserModel(
                name="Admin Demo",
                email="admin@example.com",
                password_hash=hash_password("Admin@123"),
                role=UserRole.ADMIN,
                created_at=datetime.now(UTC),
            )
            session.add(admin)

        signer_result = await session.execute(select(UserModel).where(UserModel.email == "signer@example.com"))
        signer = signer_result.scalar_one_or_none()
        if not signer:
            signer = UserModel(
                name="Signer Demo",
                email="signer@example.com",
                password_hash=hash_password("Signer@123"),
                role=UserRole.SIGNER,
                created_at=datetime.now(UTC),
            )
            session.add(signer)

        await session.flush()

        document_result = await session.execute(
            select(DocumentModel).where(
                DocumentModel.title == "Seeded Demo Contract",
                DocumentModel.uploaded_by == admin.id,
            )
        )
        document = document_result.scalar_one_or_none()
        if not document:
            sample_pdf_path = settings.original_storage_dir / "seeded_demo_contract.pdf"
            _create_demo_pdf(sample_pdf_path)
            document = DocumentModel(
                title="Seeded Demo Contract",
                uploaded_by=admin.id,
                original_path=str(sample_pdf_path),
                final_path=None,
                final_hash=None,
                total_pages=1,
                status=DocumentStatus.DRAFT,
                created_at=datetime.now(UTC),
            )
            session.add(document)

        await session.commit()
        print("Seed completed.")
        print("Admin: admin@example.com / Admin@123")
        print("Signer: signer@example.com / Signer@123")


if __name__ == "__main__":
    asyncio.run(seed())
