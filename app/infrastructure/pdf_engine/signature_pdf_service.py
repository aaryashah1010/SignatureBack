import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.domain.value_objects.signature_box import SignatureBox


class SignaturePdfService:
    def __init__(self) -> None:
        self.supported_typed_fonts = {
            "classic": [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "arial.ttf",
            ],
            "script": [
                "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
                "times.ttf",
            ],
            "formal": [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
                "cour.ttf",
            ],
        }

    def get_page_count(self, pdf_path: Path) -> int:
        reader = PdfReader(str(pdf_path))
        return len(reader.pages)

    def render_typed_signature(self, typed_name: str, typed_font: str) -> bytes:
        image = Image.new("RGBA", (1200, 340), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        font = self._resolve_typed_font(typed_font=typed_font, size=140)

        text = typed_name.strip()
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        x = max(20, (image.width - text_width) // 2)
        y = max(20, (image.height - text_height) // 2)
        draw.text((x, y), text, fill=(20, 20, 20, 255), font=font)

        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def _resolve_typed_font(self, typed_font: str, size: int) -> ImageFont.ImageFont:
        candidates = self.supported_typed_fonts.get(typed_font, self.supported_typed_fonts["classic"])
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        # Pillow 11+ supports sized default font, which is far more visible than bitmap fallback.
        return ImageFont.load_default(size=size)

    def apply_signature(
        self,
        source_pdf: Path,
        target_pdf: Path,
        page_number: int,
        box: SignatureBox,
        signature_bytes: bytes,
    ) -> None:
        self.apply_signatures(
            source_pdf=source_pdf,
            target_pdf=target_pdf,
            signatures=[(SignatureBox(page_number=page_number, x=box.x, y=box.y, width=box.width, height=box.height), signature_bytes)],
        )

    def apply_signatures(
        self,
        source_pdf: Path,
        target_pdf: Path,
        signatures: list[tuple[SignatureBox, bytes]],
    ) -> None:
        reader = PdfReader(str(source_pdf))
        writer = PdfWriter()

        for box, signature_bytes in signatures:
            page = reader.pages[box.page_number - 1]
            page_width = float(page.mediabox.width)
            page_height = float(page.mediabox.height)

            box_width = box.width * page_width
            box_height = box.height * page_height
            box_x = box.x * page_width

            # Stored y is normalized from top-left in UI; PDF origin is bottom-left.
            box_y = page_height - ((box.y + box.height) * page_height)
            overlay_png = self._prepare_overlay_png(signature_bytes=signature_bytes, box_width=box_width, box_height=box_height)

            overlay_stream = io.BytesIO()
            overlay = canvas.Canvas(overlay_stream, pagesize=(page_width, page_height))
            overlay.drawImage(ImageReader(io.BytesIO(overlay_png)), box_x, box_y, width=box_width, height=box_height, mask="auto")
            overlay.save()
            overlay_stream.seek(0)

            overlay_reader = PdfReader(overlay_stream)
            page.merge_page(overlay_reader.pages[0])

        for source_page in reader.pages:
            writer.add_page(source_page)

        with target_pdf.open("wb") as file_obj:
            writer.write(file_obj)

    def render_region_boxes(
        self,
        source_pdf: Path,
        target_pdf: Path,
        boxes: list[SignatureBox],
    ) -> None:
        """Draw empty dashed signature boxes on the PDF (for admin-prepared callback)."""
        reader = PdfReader(str(source_pdf))
        writer = PdfWriter()

        for page_idx, source_page in enumerate(reader.pages):
            page_width = float(source_page.mediabox.width)
            page_height = float(source_page.mediabox.height)

            page_boxes = [b for b in boxes if b.page_number - 1 == page_idx]
            if page_boxes:
                overlay_stream = io.BytesIO()
                c = canvas.Canvas(overlay_stream, pagesize=(page_width, page_height))
                c.setStrokeColorRGB(0.2, 0.4, 0.8)
                c.setLineWidth(1.5)
                c.setDash(4, 3)
                for box in page_boxes:
                    bx = box.x * page_width
                    bw = box.width * page_width
                    bh = box.height * page_height
                    by = page_height - ((box.y + box.height) * page_height)
                    c.rect(bx, by, bw, bh, stroke=1, fill=0)
                c.save()
                overlay_stream.seek(0)
                overlay_reader = PdfReader(overlay_stream)
                source_page.merge_page(overlay_reader.pages[0])

            writer.add_page(source_page)

        with target_pdf.open("wb") as f:
            writer.write(f)

    def _prepare_overlay_png(self, signature_bytes: bytes, box_width: float, box_height: float) -> bytes:
        render_scale = 3
        render_width = max(1, int(round(box_width * render_scale)))
        render_height = max(1, int(round(box_height * render_scale)))

        signature_image = Image.open(io.BytesIO(signature_bytes)).convert("RGBA")
        signature_image.thumbnail((render_width, render_height), Image.Resampling.LANCZOS)

        padded_image = Image.new("RGBA", (render_width, render_height), (255, 255, 255, 0))
        paste_x = max(0, (render_width - signature_image.width) // 2)
        paste_y = max(0, (render_height - signature_image.height) // 2)
        padded_image.paste(signature_image, (paste_x, paste_y), signature_image)

        buffered = io.BytesIO()
        padded_image.save(buffered, format="PNG")
        return buffered.getvalue()
