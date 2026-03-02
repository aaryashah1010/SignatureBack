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
            "classic": "arial.ttf",
            "script": "times.ttf",
            "formal": "cour.ttf",
        }

    def get_page_count(self, pdf_path: Path) -> int:
        reader = PdfReader(str(pdf_path))
        return len(reader.pages)

    def render_typed_signature(self, typed_name: str, typed_font: str) -> bytes:
        image = Image.new("RGBA", (800, 220), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        try:
            font_name = self.supported_typed_fonts.get(typed_font, "arial.ttf")
            font = ImageFont.truetype(font_name, 96)
        except OSError:
            font = ImageFont.load_default()
        draw.text((20, 60), typed_name, fill=(20, 20, 20, 255), font=font)

        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def apply_signature(
        self,
        source_pdf: Path,
        target_pdf: Path,
        page_number: int,
        box: SignatureBox,
        signature_bytes: bytes,
    ) -> None:
        reader = PdfReader(str(source_pdf))
        writer = PdfWriter()

        page = reader.pages[page_number - 1]
        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)

        box_width = box.width * page_width
        box_height = box.height * page_height
        box_x = box.x * page_width

        # Stored y is normalized from top-left in UI; PDF origin is bottom-left.
        box_y = page_height - ((box.y + box.height) * page_height)

        signature_image = Image.open(io.BytesIO(signature_bytes)).convert("RGBA")
        signature_image.thumbnail((int(box_width), int(box_height)))

        padded_image = Image.new("RGBA", (int(box_width), int(box_height)), (255, 255, 255, 0))
        padded_image.paste(signature_image, (0, 0), signature_image)
        buffered = io.BytesIO()
        padded_image.save(buffered, format="PNG")

        overlay_stream = io.BytesIO()
        overlay = canvas.Canvas(overlay_stream, pagesize=(page_width, page_height))
        overlay.drawImage(ImageReader(io.BytesIO(buffered.getvalue())), box_x, box_y, width=box_width, height=box_height, mask="auto")
        overlay.save()
        overlay_stream.seek(0)

        overlay_reader = PdfReader(overlay_stream)
        page.merge_page(overlay_reader.pages[0])

        for idx, source_page in enumerate(reader.pages):
            if idx == page_number - 1:
                writer.add_page(page)
            else:
                writer.add_page(source_page)

        with target_pdf.open("wb") as file_obj:
            writer.write(file_obj)
