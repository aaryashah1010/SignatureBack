import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from app.domain.entities.annotation import AnnotationEntity, AnnotationKind
from app.domain.value_objects.signature_box import SignatureBox


_DEFAULT_ANNOTATION_COLOR = "#fde047"
# Blue used for the signature rule + "NAME (timestamp)" caption, matching e-sign conventions.
_SIGNATURE_CAPTION_COLOR = "#1a56db"
# Audit report page palette.
_AUDIT_BORDER_COLOR = "#2196c9"
_AUDIT_HEADING_COLOR = "#2b7cb9"


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
        signatures: list[tuple],
        annotations: list[AnnotationEntity] | None = None,
    ) -> None:
        """Stamp signatures onto the PDF.

        Each item is (box, signature_bytes) or (box, signature_bytes, caption).
        When a caption is given, a blue rule is drawn just under the box with the
        signer's name and signing timestamp beneath it (Adobe Sign style).
        """
        reader = PdfReader(str(source_pdf))
        writer = PdfWriter()

        for item in signatures:
            box, signature_bytes = item[0], item[1]
            caption = item[2] if len(item) > 2 else None

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
            if caption:
                self._draw_signature_caption(overlay, x=box_x, y_bottom=box_y, width=box_width, caption=caption)
            overlay.save()
            overlay_stream.seek(0)

            overlay_reader = PdfReader(overlay_stream)
            page.merge_page(overlay_reader.pages[0])

        # Burn admin annotations (highlights, free-draw, text notes) onto each page they belong to.
        if annotations:
            annotations_by_page: dict[int, list[AnnotationEntity]] = {}
            for ann in annotations:
                annotations_by_page.setdefault(ann.page_number, []).append(ann)

            for page_idx, source_page in enumerate(reader.pages):
                page_annotations = annotations_by_page.get(page_idx + 1)
                if not page_annotations:
                    continue
                page_width = float(source_page.mediabox.width)
                page_height = float(source_page.mediabox.height)

                overlay_stream = io.BytesIO()
                overlay = canvas.Canvas(overlay_stream, pagesize=(page_width, page_height))
                self._draw_annotations(overlay, page_width, page_height, page_annotations)
                overlay.save()
                overlay_stream.seek(0)

                overlay_reader = PdfReader(overlay_stream)
                source_page.merge_page(overlay_reader.pages[0])

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

    def render_audit_report(
        self,
        page_size: tuple[float, float],
        title: str,
        report_date: str = "",
        created_on: str = "",
        created_by: str = "",
        status: str = "",
        transaction_id: str = "",
        history: list[dict] | None = None,
    ) -> bytes:
        """Render a "Final Audit Report" page (or pages) as standalone PDF bytes.

        `history` is a list of {"text": str, "timestamp": str, "detail": str} events,
        rendered in order like an e-sign audit trail.
        """
        page_width, page_height = page_size
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=(page_width, page_height))

        border = HexColor(_AUDIT_BORDER_COLOR)
        heading = HexColor(_AUDIT_HEADING_COLOR)
        muted = HexColor("#555555")
        dark = HexColor("#1f2937")
        margin = 42.0

        def draw_border() -> None:
            c.saveState()
            c.setStrokeColor(border)
            c.setLineWidth(2)
            c.rect(24, 24, page_width - 48, page_height - 48, stroke=1, fill=0)
            c.restoreState()

        draw_border()
        y = page_height - 78

        # Title + report line
        c.setFillColor(heading)
        c.setFont("Helvetica", 20)
        c.drawString(margin, y, title or "Final Audit Report")
        y -= 16
        c.setFillColor(dark)
        c.setFont("Helvetica", 8)
        c.drawString(margin, y, "Final Audit Report")
        if report_date:
            c.drawRightString(page_width - margin, y, report_date)
        y -= 16

        # Metadata box
        rows = [
            ("Created:", created_on),
            ("By:", created_by),
            ("Status:", status),
            ("Transaction ID:", transaction_id),
        ]
        row_height = 14.0
        box_height = row_height * len(rows) + 10
        c.saveState()
        c.setFillColor(HexColor("#f3f3f3"))
        c.setStrokeColor(HexColor("#cccccc"))
        c.setLineWidth(0.6)
        c.rect(margin, y - box_height, page_width - 2 * margin, box_height, stroke=1, fill=1)
        c.restoreState()

        row_y = y - 16
        for label, value in rows:
            c.setFillColor(muted)
            c.setFont("Helvetica", 7.5)
            c.drawString(margin + 10, row_y, label)
            c.setFillColor(dark)
            c.setFont("Helvetica", 7.5)
            c.drawString(margin + 96, row_y, self._clip(str(value or ""), page_width - margin - 110, "Helvetica", 7.5))
            row_y -= row_height
        y -= box_height + 28

        # History heading
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin, y, f'"{title}" History' if title else "History")
        y -= 22

        for event in history or []:
            text = str(event.get("text") or "")
            timestamp = str(event.get("timestamp") or "")
            detail = str(event.get("detail") or "")

            needed = 14 + (10 if timestamp else 0) + (10 if detail else 0) + 8
            if y - needed < 60:  # start a new page
                c.showPage()
                draw_border()
                y = page_height - 78

            # bullet
            c.saveState()
            c.setFillColor(heading)
            c.circle(margin + 4, y + 3, 3, stroke=0, fill=1)
            c.restoreState()

            c.setFillColor(dark)
            c.setFont("Helvetica", 8)
            c.drawString(margin + 16, y, self._clip(text, page_width - margin - 30, "Helvetica", 8))
            y -= 11

            if timestamp:
                c.setFillColor(muted)
                c.setFont("Helvetica", 7)
                c.drawString(margin + 16, y, timestamp)
                y -= 10
            if detail:
                c.setFillColor(muted)
                c.setFont("Helvetica", 6.5)
                c.drawString(margin + 16, y, self._clip(detail, page_width - margin - 30, "Helvetica", 6.5))
                y -= 10
            y -= 8

        c.save()
        buffer.seek(0)
        return buffer.getvalue()

    @staticmethod
    def _clip(text: str, max_width: float, font_name: str, font_size: float) -> str:
        """Truncate with an ellipsis so a long value never runs off the page."""
        if stringWidth(text, font_name, font_size) <= max_width:
            return text
        ellipsis = "..."
        while text and stringWidth(text + ellipsis, font_name, font_size) > max_width:
            text = text[:-1]
        return text + ellipsis

    def _draw_signature_caption(
        self,
        overlay: canvas.Canvas,
        x: float,
        y_bottom: float,
        width: float,
        caption: str,
    ) -> None:
        """Draw a blue rule at the bottom of the signature box with the caption beneath it.

        Caption is the signer's name + signing timestamp, e.g.
        "JOHN DOE (Jul 8, 2026 20:56:19 CDT)".
        """
        overlay.saveState()
        color = HexColor(_SIGNATURE_CAPTION_COLOR)
        overlay.setStrokeColor(color)
        overlay.setLineWidth(0.8)
        overlay.line(x, y_bottom, x + width, y_bottom)

        font_name = "Helvetica"
        font_size = 6.5
        # Shrink until the caption fits the box width (never below 4pt).
        while font_size > 4.0 and stringWidth(caption, font_name, font_size) > width:
            font_size -= 0.25

        text_y = y_bottom - font_size - 1.5
        if text_y >= 0:
            overlay.setFillColor(color)
            overlay.setFont(font_name, font_size)
            overlay.drawString(x, text_y, caption)
        overlay.restoreState()

    def _draw_annotations(
        self,
        overlay: canvas.Canvas,
        page_width: float,
        page_height: float,
        annotations: list[AnnotationEntity],
    ) -> None:
        """Render highlight / drawing / text annotations onto a single overlay canvas.

        Annotations store normalized coords with top-left origin; PDF uses bottom-left.
        """
        for ann in annotations:
            ax = ann.x * page_width
            aw = ann.width * page_width
            ah = ann.height * page_height
            ay = page_height - ((ann.y + ann.height) * page_height)

            color = self._safe_hex_color(ann.color)

            if ann.kind == AnnotationKind.HIGHLIGHT:
                overlay.saveState()
                overlay.setFillColor(color)
                overlay.setFillAlpha(0.35)
                overlay.setStrokeAlpha(0)
                overlay.rect(ax, ay, aw, ah, stroke=0, fill=1)
                overlay.restoreState()

            elif ann.kind == AnnotationKind.DRAWING:
                try:
                    strokes = json.loads(ann.paths) if ann.paths else []
                except (ValueError, TypeError):
                    strokes = []
                overlay.saveState()
                overlay.setStrokeColor(color)
                overlay.setLineWidth(2.0)
                overlay.setLineCap(1)
                overlay.setLineJoin(1)
                for stroke in strokes:
                    if not isinstance(stroke, list) or len(stroke) < 2:
                        continue
                    path = overlay.beginPath()
                    started = False
                    for point in stroke:
                        if not isinstance(point, (list, tuple)) or len(point) < 2:
                            continue
                        # Points are normalized within the annotation bounding box (top-left origin).
                        px = ax + float(point[0]) * aw
                        py = ay + ah - float(point[1]) * ah
                        if not started:
                            path.moveTo(px, py)
                            started = True
                        else:
                            path.lineTo(px, py)
                    if started:
                        overlay.drawPath(path, stroke=1, fill=0)
                overlay.restoreState()

            elif ann.kind == AnnotationKind.TEXT:
                overlay.saveState()
                # Soft yellow sticky-note background to match the UI style.
                overlay.setFillColorRGB(1.0, 0.98, 0.78)
                overlay.setStrokeColor(color)
                overlay.setLineWidth(1.0)
                overlay.rect(ax, ay, aw, ah, stroke=1, fill=1)
                overlay.setFillColorRGB(0.12, 0.16, 0.22)
                font_name = "Helvetica"
                font_size = 9.0
                overlay.setFont(font_name, font_size)
                lines = self._wrap_text(
                    text=ann.text or "",
                    max_width=max(1.0, aw - 8.0),
                    font_name=font_name,
                    font_size=font_size,
                )
                line_height = font_size + 2.0
                text_y = ay + ah - font_size - 4.0
                for line in lines:
                    if text_y < ay + 4.0:
                        break
                    overlay.drawString(ax + 4.0, text_y, line)
                    text_y -= line_height
                overlay.restoreState()

    @staticmethod
    def _safe_hex_color(value: str):
        try:
            return HexColor(value or _DEFAULT_ANNOTATION_COLOR)
        except (ValueError, TypeError):
            return HexColor(_DEFAULT_ANNOTATION_COLOR)

    @staticmethod
    def _wrap_text(text: str, max_width: float, font_name: str, font_size: float) -> list[str]:
        lines: list[str] = []
        for paragraph in text.split("\n"):
            if not paragraph:
                lines.append("")
                continue
            words = paragraph.split(" ")
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if stringWidth(candidate, font_name, font_size) <= max_width:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                    # Hard-break very long single tokens character-by-character.
                    if stringWidth(word, font_name, font_size) > max_width:
                        chunk = ""
                        for ch in word:
                            test = chunk + ch
                            if stringWidth(test, font_name, font_size) <= max_width:
                                chunk = test
                            else:
                                if chunk:
                                    lines.append(chunk)
                                chunk = ch
                        current = chunk
                    else:
                        current = word
            if current:
                lines.append(current)
        return lines

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
