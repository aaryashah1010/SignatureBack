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
# Audit report page palette — emerald/mint theme.
_AUDIT_BORDER_COLOR = "#2196c9"
_AUDIT_HEADING_COLOR = "#059669"
_AUDIT_MINT_BG = "#ecfdf5"
_AUDIT_MINT_BORDER = "#a7f3d0"
_AUDIT_INDIGO = "#6366f1"
_AUDIT_AVATAR_COLORS = ("#2563eb", "#7c3aed", "#db2777", "#d97706", "#0891b2", "#16a34a")


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
        transaction_id: str = "",  # noqa: ARG002 — kept for API compatibility, no longer rendered
        history: list[dict] | None = None,
        signers: list[dict] | None = None,
    ) -> bytes:
        """Render a "Final Audit Report" page (or pages) as standalone PDF bytes.

        `history` is a list of {"text": str, "timestamp": str, "detail": str} events,
        rendered as a colored, icon-based timeline. `signers` is an optional list of
        {"name": str, "email": str, "status": str, "timestamp": str} — when the caller
        doesn't send it, it is best-effort derived from the "e-signed by NAME (email)"
        phrasing already present in `history`.

        Uses a two-pass render so the footer can show "Page X of N" (the first pass
        only measures how many pages the content needs; nothing from it is kept).
        """
        history = history or []
        resolved_signers = signers if signers else self._derive_signers_from_history(history)

        scratch = io.BytesIO()
        scratch_canvas = canvas.Canvas(scratch, pagesize=page_size)
        total_pages = self._draw_audit_report(
            scratch_canvas, page_size, title, report_date, created_on, created_by,
            status, history, resolved_signers, total_pages=None,
        )
        scratch_canvas.save()

        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=page_size)
        self._draw_audit_report(
            c, page_size, title, report_date, created_on, created_by,
            status, history, resolved_signers, total_pages=total_pages,
        )
        c.save()
        buffer.seek(0)
        return buffer.getvalue()

    @staticmethod
    def _derive_signers_from_history(history: list[dict]) -> list[dict]:
        """Best-effort: extract a distinct signer list from the free-text history
        lines when the caller doesn't send a structured `signers` array. Looks for
        the "e-signed by NAME (email)" phrasing used in the callback contract."""
        import re

        pattern = re.compile(r"e-signed by ([^(]+?)\s*\(([^)]+@[^)]+)\)", re.IGNORECASE)
        detail_pattern = re.compile(r"Signature Date:\s*(.+?)(?:\s*-\s*Time Source|$)", re.IGNORECASE)
        seen: dict[str, dict] = {}
        for event in history or []:
            text = str(event.get("text") or "")
            match = pattern.search(text)
            if not match:
                continue
            name = match.group(1).strip()
            email = match.group(2).strip()
            detail_match = detail_pattern.search(str(event.get("detail") or ""))
            timestamp = detail_match.group(1).strip() if detail_match else str(event.get("timestamp") or "")
            seen[email.lower()] = {"name": name, "email": email, "status": "Signed", "timestamp": timestamp}
        return list(seen.values())

    @staticmethod
    def _icon_image(kind: str, color_hex: str, px: int = 64) -> Image.Image:
        """Procedurally draws a small line-icon glyph (transparent background)."""
        img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        lw = max(2, px // 11)
        pad = px * 0.16

        if kind == "document":
            d.rounded_rectangle([pad, pad * 0.6, px - pad, px - pad * 0.6], radius=px * 0.08, outline=color_hex, width=lw)
            for i in range(3):
                ly = px * 0.42 + i * px * 0.15
                d.line([pad + px * 0.12, ly, px - pad - px * 0.12, ly], fill=color_hex, width=max(1, lw - 1))
        elif kind == "calendar":
            top = px * 0.24
            d.rounded_rectangle([pad, top, px - pad, px - pad * 0.5], radius=px * 0.06, outline=color_hex, width=lw)
            d.line([pad, top + px * 0.16, px - pad, top + px * 0.16], fill=color_hex, width=lw)
            d.rounded_rectangle([px * 0.28, px * 0.08, px * 0.35, top + px * 0.05], radius=px * 0.02, fill=color_hex)
            d.rounded_rectangle([px * 0.65, px * 0.08, px * 0.72, top + px * 0.05], radius=px * 0.02, fill=color_hex)
        elif kind == "person":
            d.ellipse([px * 0.32, px * 0.14, px * 0.68, px * 0.5], outline=color_hex, width=lw)
            d.pieslice([px * 0.16, px * 0.48, px * 0.84, px * 1.16], 180, 360, outline=color_hex, width=lw)
        elif kind == "shield":
            pts = [(px * 0.5, px * 0.08), (px * 0.86, px * 0.24), (px * 0.86, px * 0.56),
                   (px * 0.5, px * 0.94), (px * 0.14, px * 0.56), (px * 0.14, px * 0.24)]
            d.polygon(pts, outline=color_hex, width=lw)
        elif kind == "clock":
            d.ellipse([pad, pad, px - pad, px - pad], outline=color_hex, width=lw)
            cx = cy = px / 2
            d.line([cx, cy, cx, cy - px * 0.24], fill=color_hex, width=lw)
            d.line([cx, cy, cx + px * 0.18, cy + px * 0.06], fill=color_hex, width=lw)
        elif kind == "envelope":
            d.rounded_rectangle([pad, pad * 1.3, px - pad, px - pad * 1.3], radius=px * 0.05, outline=color_hex, width=lw)
            d.line([pad, pad * 1.3, px / 2, px * 0.55], fill=color_hex, width=lw)
            d.line([px / 2, px * 0.55, px - pad, pad * 1.3], fill=color_hex, width=lw)
        elif kind == "eye":
            d.ellipse([pad * 0.5, px * 0.32, px - pad * 0.5, px * 0.68], outline=color_hex, width=lw)
            r = px * 0.09
            d.ellipse([px / 2 - r, px / 2 - r, px / 2 + r, px / 2 + r], fill=color_hex)
        elif kind == "check":
            d.line([px * 0.2, px * 0.52, px * 0.42, px * 0.74], fill=color_hex, width=lw + 1)
            d.line([px * 0.42, px * 0.74, px * 0.82, px * 0.26], fill=color_hex, width=lw + 1)
        return img

    def _draw_icon(self, c: canvas.Canvas, kind: str, color_hex: str, cx: float, cy: float, size: float) -> None:
        img = self._icon_image(kind, color_hex)
        c.drawImage(ImageReader(img), cx - size / 2, cy - size / 2, width=size, height=size, mask="auto")

    def _event_style(self, text: str) -> tuple[str, str]:
        """Returns (icon_kind, hex_color) based on the event's kind."""
        t = text.lower()
        if "e-signed" in t or "agreement completed" in t or t.strip().startswith("completed"):
            return "check", "#16a34a"
        if "mailed" in t or "sent" in t:  # matches "emailed" and CPA's "e-mailed"
            return "envelope", "#2563eb"
        if "viewed" in t or "opened" in t:
            return "eye", "#94a3b8"
        if "created" in t:
            return "document", _AUDIT_INDIGO
        return "document", _AUDIT_HEADING_COLOR

    @staticmethod
    def _status_color(value: str) -> str:
        v = (value or "").lower()
        if "complet" in v or "sign" in v:
            return "#16a34a"
        if "pending" in v:
            return "#d97706"
        if "reject" in v or "declin" in v or "cancel" in v:
            return "#dc2626"
        return "#64748b"

    def _draw_pill(self, c: canvas.Canvas, x: float, y_baseline: float, text: str, color_hex: str) -> float:
        c.saveState()
        c.setFont("Helvetica-Bold", 7.5)
        text_w = stringWidth(text, "Helvetica-Bold", 7.5)
        pad_x, h = 8.0, 14.0
        w = text_w + pad_x * 2
        c.setFillColor(HexColor(color_hex))
        c.roundRect(x, y_baseline - 3.5, w, h, h / 2, stroke=0, fill=1)
        c.setFillColor(HexColor("#ffffff"))
        c.drawString(x + pad_x, y_baseline, text)
        c.restoreState()
        return w

    def _draw_audit_report(  # noqa: PLR0915 — one cohesive layout routine, deliberately linear
        self,
        c: canvas.Canvas,
        page_size: tuple[float, float],
        title: str,
        report_date: str,
        created_on: str,
        created_by: str,
        status: str,
        history: list[dict],
        signers: list[dict],
        total_pages: int | None,
    ) -> int:
        """Draws the full report onto `c`. Returns the total number of pages used.

        When `total_pages` is None (measuring pass), the footer omits "of N" — this
        does not affect layout, so page counts match between the measuring and the
        real pass.
        """
        page_width, page_height = page_size
        margin = 40.0
        page_num = 1

        frame_color = HexColor("#e2e8f0")
        muted = HexColor("#64748b")
        dark = HexColor("#0f172a")
        white = HexColor("#ffffff")
        heading = HexColor(_AUDIT_HEADING_COLOR)

        HEADER_H = 88.0

        def draw_page_frame() -> None:
            c.saveState()
            c.setStrokeColor(frame_color)
            c.setLineWidth(1)
            c.roundRect(16, 16, page_width - 32, page_height - 32, 8, stroke=1, fill=0)
            c.restoreState()

        def draw_footer() -> None:
            c.saveState()
            c.setStrokeColor(frame_color)
            c.setLineWidth(0.75)
            c.line(margin, 32, page_width - margin, 32)
            self._draw_icon(c, "shield", "#94a3b8", margin + 5, 24, 9)
            c.setFillColor(muted)
            c.setFont("Helvetica", 6.5)
            c.drawString(margin + 14, 21, "Signing audit trail - generated automatically")
            page_label = f"Page {page_num} of {total_pages}" if total_pages else f"Page {page_num}"
            c.drawRightString(page_width - margin, 21, page_label)
            c.restoreState()

        def new_page(continuation_header: bool = True) -> float:
            nonlocal page_num
            draw_footer()
            c.showPage()
            page_num += 1
            draw_page_frame()
            if continuation_header:
                c.saveState()
                c.setFillColor(heading)
                c.roundRect(16, page_height - 46, page_width - 32, 30, 6, stroke=0, fill=1)
                c.setFillColor(white)
                c.setFont("Helvetica-Bold", 11)
                c.drawString(margin, page_height - 37, self._clip(title or "Final Audit Report", page_width - 2 * margin, "Helvetica-Bold", 11))
                c.restoreState()
                return page_height - 62.0
            return page_height - 32.0

        def draw_header() -> float:
            top = page_height - 16
            c.saveState()
            path = c.beginPath()
            path.roundRect(16, top - HEADER_H, page_width - 32, HEADER_H, 8)
            c.clipPath(path, stroke=0, fill=0)
            c.setFillColor(HexColor(_AUDIT_MINT_BG))
            c.rect(16, top - HEADER_H, page_width - 32, HEADER_H, stroke=0, fill=1)
            # Soft decorative blobs, clipped to the header's rounded rect.
            c.setFillColor(HexColor(_AUDIT_MINT_BORDER))
            c.setFillAlpha(0.55)
            c.circle(page_width - 60, top - 10, 70, stroke=0, fill=1)
            c.setFillAlpha(0.35)
            c.circle(page_width - 140, top - HEADER_H + 15, 50, stroke=0, fill=1)
            c.restoreState()

            # Logo tile.
            logo_size = 40.0
            logo_x, logo_y = margin, top - HEADER_H / 2 - logo_size / 2
            c.saveState()
            c.setFillColor(HexColor(_AUDIT_MINT_BG))
            c.setStrokeColor(heading)
            c.setLineWidth(1.2)
            c.roundRect(logo_x, logo_y, logo_size, logo_size, 8, stroke=1, fill=1)
            c.restoreState()
            self._draw_icon(c, "document", _AUDIT_HEADING_COLOR, logo_x + logo_size / 2, logo_y + logo_size / 2, 20)

            text_x = logo_x + logo_size + 14
            c.saveState()
            c.setFillColor(heading)
            c.setFont("Helvetica-Bold", 7.5)
            c.drawString(text_x, top - 26, "FINAL AUDIT REPORT")
            c.setFillColor(dark)
            c.setFont("Helvetica-Bold", 17)
            c.drawString(text_x, top - 46, self._clip(title or "Final Audit Report", page_width - margin - text_x - 90, "Helvetica-Bold", 17))
            c.restoreState()

            if report_date:
                self._draw_icon(c, "calendar", _AUDIT_HEADING_COLOR, page_width - margin - 60, top - 22, 12)
                c.saveState()
                c.setFillColor(dark)
                c.setFont("Helvetica-Bold", 8.5)
                c.drawString(page_width - margin - 52, top - 25, report_date)
                c.restoreState()

            return top - HEADER_H - 16

        def draw_info_bar(top: float) -> float:
            bar_h = 62.0
            c.saveState()
            c.setFillColor(white)
            c.setStrokeColor(frame_color)
            c.setLineWidth(0.75)
            c.roundRect(margin, top - bar_h, page_width - 2 * margin, bar_h, 8, stroke=1, fill=1)
            c.restoreState()

            usable = page_width - 2 * margin
            sections = [
                ("calendar", "#059669", "CREATED", created_on or "N/A"),
                ("person", _AUDIT_INDIGO, "PREPARED BY", created_by or "N/A"),
                ("shield", "#16a34a", "STATUS", None),
            ]
            seg_w = usable / 3
            for i, (icon, color, label, value) in enumerate(sections):
                sx = margin + seg_w * i
                icon_cx = sx + 26
                icon_cy = top - bar_h / 2
                c.saveState()
                c.setFillColor(HexColor(color))
                c.setFillAlpha(0.15)
                c.circle(icon_cx, icon_cy, 15, stroke=0, fill=1)
                c.restoreState()
                self._draw_icon(c, icon, color, icon_cx, icon_cy, 16)

                tx = sx + 48
                c.setFillColor(muted)
                c.setFont("Helvetica-Bold", 6.5)
                c.drawString(tx, top - bar_h / 2 + 10, label)
                if value is not None:
                    c.setFillColor(dark)
                    c.setFont("Helvetica-Bold", 9.5)
                    c.drawString(tx, top - bar_h / 2 - 4, self._clip(str(value), seg_w - 55, "Helvetica-Bold", 9.5))
                elif status:
                    self._draw_pill(c, tx, top - bar_h / 2 - 6, status.upper(), self._status_color(status))

                if i < len(sections) - 1:
                    c.saveState()
                    c.setStrokeColor(frame_color)
                    c.setLineWidth(0.75)
                    c.line(sx + seg_w, top - bar_h + 10, sx + seg_w, top - 10)
                    c.restoreState()
            return top - bar_h - 22

        def measure_timeline_row(event: dict, col_w: float) -> tuple[list[str], float]:
            """Returns (wrapped title lines, row height) for the given column width."""
            text = str(event.get("text") or "")
            detail = str(event.get("detail") or "")
            row_w = col_w - 34
            title_lines = self._wrap_2_lines(text, row_w - 90, "Helvetica-Bold", 8.5)
            title_h = 11.0 * len(title_lines)
            detail_h = 9.0 if detail else 0.0
            row_h = max(16 + title_h + detail_h, 40.0)
            return title_lines, row_h

        def draw_timeline_row(event: dict, col_x: float, col_w: float, top_y: float, prev_bullet_y: float | None) -> tuple[float, float]:
            text = str(event.get("text") or "")
            timestamp = str(event.get("timestamp") or "")
            detail = str(event.get("detail") or "")
            date_part, time_part = "", ""
            if " - " in timestamp:
                date_part, time_part = (p.strip() for p in timestamp.split(" - ", 1))
            elif timestamp:
                date_part = timestamp

            title_lines, row_h = measure_timeline_row(event, col_w)
            row_top = top_y
            row_bottom = row_top - row_h
            bullet_y = row_top - row_h / 2 + 4

            bullet_x = col_x + 12
            text_x = col_x + 30
            row_w = col_w - 34

            if prev_bullet_y is not None:
                c.saveState()
                c.setStrokeColor(frame_color)
                c.setLineWidth(1.2)
                c.line(bullet_x, prev_bullet_y - 9, bullet_x, bullet_y + 9)
                c.restoreState()

            icon_kind, color_hex = self._event_style(text)
            c.saveState()
            c.setFillColor(HexColor("#f8fafc"))
            c.roundRect(text_x - 6, row_bottom + 3, row_w, row_h - 6, 6, stroke=0, fill=1)
            c.restoreState()

            c.saveState()
            c.setFillColor(HexColor(color_hex))
            c.circle(bullet_x, bullet_y, 9, stroke=0, fill=1)
            c.restoreState()
            self._draw_icon(c, icon_kind, "#ffffff", bullet_x, bullet_y, 10)

            ty = row_top - 13
            c.setFillColor(dark)
            c.setFont("Helvetica-Bold", 8.5)
            for line in title_lines:
                c.drawString(text_x, ty, line)
                ty -= 11.0
            if detail:
                c.setFillColor(muted)
                c.setFont("Helvetica-Oblique", 6.5)
                c.drawString(text_x, ty, self._clip(detail, row_w - 20, "Helvetica-Oblique", 6.5))

            if date_part or time_part:
                c.setFillColor(muted)
                c.setFont("Helvetica-Bold", 6.8)
                c.drawRightString(col_x + col_w, row_top - 13, date_part)
                if time_part:
                    c.drawRightString(col_x + col_w, row_top - 23, time_part)

            return row_bottom - 6, bullet_y

        def draw_details_card(top: float, x: float, w: float) -> float:
            fields = [("Document Name", title), ("Created On", created_on), ("Prepared By", created_by)]
            row_h = 30.0
            card_h = row_h * len(fields) + 42.0
            c.saveState()
            c.setFillColor(white)
            c.setStrokeColor(frame_color)
            c.setLineWidth(0.75)
            c.roundRect(x, top - card_h, w, card_h, 8, stroke=1, fill=1)
            c.restoreState()

            hy = top - 20
            self._draw_icon(c, "document", _AUDIT_HEADING_COLOR, x + 16, hy, 13)
            c.setFillColor(dark)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(x + 28, hy - 4, "Document Details")

            fy = top - 44
            for label, value in fields:
                c.setFillColor(muted)
                c.setFont("Helvetica-Bold", 6.5)
                c.drawString(x + 16, fy, label.upper())
                c.setFillColor(dark)
                c.setFont("Helvetica-Bold", 9)
                c.drawString(x + 16, fy - 12, self._clip(str(value or "N/A"), w - 32, "Helvetica-Bold", 9))
                fy -= row_h

            c.setFillColor(muted)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawString(x + 16, fy, "STATUS")
            if status:
                self._draw_pill(c, x + 16, fy - 13, status.upper(), self._status_color(status))
            return top - card_h - 16

        def draw_signers_card(top: float, x: float, w: float) -> float:
            if not signers:
                return top
            row_h = 40.0
            card_h = row_h * len(signers) + 34.0
            c.saveState()
            c.setFillColor(white)
            c.setStrokeColor(frame_color)
            c.setLineWidth(0.75)
            c.roundRect(x, top - card_h, w, card_h, 8, stroke=1, fill=1)
            c.restoreState()

            c.setFillColor(dark)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(x + 16, top - 20, "Signers")

            ry = top - 30
            for idx, signer in enumerate(signers):
                name = str(signer.get("name") or "Unknown")
                email = str(signer.get("email") or "")
                s_status = str(signer.get("status") or "Signed")
                s_time = str(signer.get("timestamp") or "")
                initials = "".join(p[0].upper() for p in name.split()[:2]) or "?"
                avatar_color = HexColor(_AUDIT_AVATAR_COLORS[idx % len(_AUDIT_AVATAR_COLORS)])

                cy = ry - 12
                c.saveState()
                c.setFillColor(avatar_color)
                c.circle(x + 26, cy, 13, stroke=0, fill=1)
                c.setFillColor(white)
                c.setFont("Helvetica-Bold", 8.5)
                c.drawCentredString(x + 26, cy - 3, initials)
                c.restoreState()

                c.setFillColor(dark)
                c.setFont("Helvetica-Bold", 8.5)
                c.drawString(x + 46, cy + 2, self._clip(name, w - 66, "Helvetica-Bold", 8.5))
                c.setFillColor(muted)
                c.setFont("Helvetica", 6.8)
                c.drawString(x + 46, cy - 8, self._clip(email, w - 66, "Helvetica", 6.8))

                self._draw_icon(c, "check", "#16a34a", x + 46 + 5, cy - 18, 8)
                c.setFillColor(HexColor("#16a34a"))
                c.setFont("Helvetica-Bold", 6.8)
                c.drawString(x + 46 + 12, cy - 20, s_status)
                if s_time:
                    c.setFillColor(muted)
                    c.setFont("Helvetica", 6.3)
                    c.drawRightString(x + w - 12, cy - 20, s_time)
                ry -= row_h
            return top - card_h - 16

        def draw_success_note(top: float, x: float, w: float) -> None:
            note_h = 44.0
            c.saveState()
            c.setFillColor(HexColor(_AUDIT_MINT_BG))
            c.setStrokeColor(HexColor(_AUDIT_MINT_BORDER))
            c.setLineWidth(0.75)
            c.roundRect(x, top - note_h, w, note_h, 8, stroke=1, fill=1)
            c.restoreState()
            self._draw_icon(c, "shield", "#059669", x + 20, top - note_h / 2, 16)
            c.setFillColor(HexColor("#065f46"))
            c.setFont("Helvetica", 6.8)
            lines = self._wrap_text(
                "This document has been successfully signed and completed through a secure audit trail.",
                max_width=w - 44, font_name="Helvetica", font_size=6.8,
            )
            ty = top - note_h / 2 + (len(lines) - 1) * 4.5
            for line in lines[:3]:
                c.drawString(x + 34, ty, line)
                ty -= 9

        # ── Page 1 ───────────────────────────────────────────────────────────────
        draw_page_frame()
        y = draw_header()
        y = draw_info_bar(y)

        content_w = page_width - 2 * margin
        gap = 16.0
        left_w = content_w * 0.6
        right_x = margin + left_w + gap
        right_w = content_w - left_w - gap

        left_top = y
        c.saveState()
        self._draw_icon(c, "clock", _AUDIT_HEADING_COLOR, margin + 6, left_top - 2, 13)
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 11.5)
        c.drawString(margin + 16, left_top, "Signing Activity")
        c.restoreState()
        ly = left_top - 22

        right_y = draw_details_card(left_top + 4, right_x, right_w)
        right_y = draw_signers_card(right_y, right_x, right_w)
        if signers:
            draw_success_note(right_y, right_x, right_w)

        prev_bullet_y: float | None = None
        on_first_page = True
        floor_y = 55.0
        for event in history:
            active_col_w = left_w if on_first_page else content_w
            _, est_row_h = measure_timeline_row(event, active_col_w)
            if ly - (est_row_h + 6) < floor_y:
                ly = new_page(continuation_header=True)
                prev_bullet_y = None
                on_first_page = False

            col_x = margin
            col_w = left_w if on_first_page else content_w
            ly, prev_bullet_y = draw_timeline_row(event, col_x, col_w, ly, prev_bullet_y)

        draw_footer()
        return page_num

    def _wrap_2_lines(self, text: str, max_width: float, font_name: str, font_size: float) -> list[str]:
        """Wraps text to at most 2 lines, ellipsis-clipping the 2nd line if more remains."""
        lines = self._wrap_text(text, max_width, font_name, font_size)
        if len(lines) <= 2:
            return lines
        second = self._clip(" ".join(lines[1:]), max_width, font_name, font_size)
        return [lines[0], second]

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
