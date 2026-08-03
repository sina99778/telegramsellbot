import io
import logging
import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent.parent / "core" / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"

DEFAULT_FONT_PATH = FONTS_DIR / "vazirmatn.ttf"


def reshape_text(text: str) -> str:
    """Reshape Persian text for Pillow. 
    Requires python-bidi and arabic-reshaper if we wanted perfect rendering,
    but we can try a basic fallback or leave it direct if the system supports it."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        reshaped_text = arabic_reshaper.reshape(text)
        bidi_text = get_display(reshaped_text)
        return bidi_text
    except ImportError:
        # Fallback if libraries are not installed (we didn't add them yet)
        return text


def _fit_font(draw, text: str, max_width: int, path: Path, start_size: int, min_size: int = 22):
    """Shrink a font until `text` fits `max_width`.

    The detail column sits left of the QR block, so a long value like
    "0.00 GB / Unlimited" would otherwise overlap the QR caption.
    """
    size = start_size
    while size > min_size:
        try:
            candidate = ImageFont.truetype(str(path), size)
        except IOError:
            return None
        if draw.textlength(text, font=candidate) <= max_width:
            return candidate
        size -= 2
    try:
        return ImageFont.truetype(str(path), min_size)
    except IOError:
        return None


def create_traffic_banner(
    config_name: str,
    user_id: int,
    status: str,
    used_gb: float,
    total_gb: float,
    days_left: Optional[int],
    is_active: bool,
    bot_username: str | None = None,
    vless_uri: str | None = None,
) -> io.BytesIO:
    """
    Generate a modern dark-mode visual banner displaying config status
    with a QR code of the vless URI.
    """
    # Make the banner wider to accommodate QR code
    width, height = 900, 420
    # Declared up front because the detail column widths depend on it.
    qr_size = 160
    
    # Create dark background
    image = Image.new("RGB", (width, height), "#111827")
    draw = ImageDraw.Draw(image)
    
    # Try to load font
    try:
        font_large = ImageFont.truetype(str(DEFAULT_FONT_PATH), 42)
        font_medium = ImageFont.truetype(str(DEFAULT_FONT_PATH), 28)
        font_small = ImageFont.truetype(str(DEFAULT_FONT_PATH), 20)
    except IOError:
        font_large = ImageFont.load_default(size=42)
        font_medium = ImageFont.load_default(size=28)
        font_small = ImageFont.load_default(size=20)

    # Decorate background - subtle gradients/shapes
    draw.ellipse((-100, -100, 200, 200), fill="#1f2937")
    draw.ellipse((width - 150, height - 150, width + 50, height + 50), fill="#1f2937")

    # Header section — truncate long config names so they don't bleed off
    # the canvas. Persian wider chars take more pixels than Latin, so we
    # cap aggressively.
    display_name = config_name or "(بدون نام)"
    max_name_len = 22
    if len(display_name) > max_name_len:
        display_name = display_name[:max_name_len - 1] + "…"
    draw.text((40, 30), reshape_text(f"Config: {display_name}"), fill="#f3f4f6", font=font_large)

    # Vazirmatn has no colour-emoji glyphs, so 🟢/🔴 rendered as blank tofu.
    # Draw the status dot as a real circle instead.
    #
    # A freshly bought config is "active" in the sense that it is usable, but
    # labelling it ACTIVE contradicts the caption's "activates on first
    # connection", so pending_activation gets its own amber label.
    if status == "pending_activation":
        status_text = "READY"
        status_color = "#fbbf24"
    elif is_active:
        status_text = "ACTIVE"
        status_color = "#34d399"
    else:
        status_text = status.upper()
        status_color = "#f87171"
    dot_r = 7
    dot_cx, dot_cy = 40 + dot_r, 80 + 18
    draw.ellipse(
        (dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r),
        fill=status_color,
    )
    draw.text((40 + dot_r * 2 + 10, 80), reshape_text(status_text), fill=status_color, font=font_medium)

    # Draw progress ring
    ring_x, ring_y, ring_r = 150, 250, 90
    draw.arc(
        (ring_x - ring_r, ring_y - ring_r, ring_x + ring_r, ring_y + ring_r),
        start=0, end=360, fill="#374151", width=15
    )
    
    # Calculate percentage — unlimited traffic (total_gb ≤ 0) renders as 0% used,
    # not 100%, because there is no cap to exceed.
    is_unlimited_traffic = total_gb <= 0
    if is_unlimited_traffic:
        percent = 0.0
    else:
        percent = (used_gb / total_gb) * 100
        percent = min(max(percent, 0), 100)
    
    # Draw active ring
    if is_unlimited_traffic:
        # A 0% arc would be invisible and read as a broken widget, so an
        # uncapped plan gets a deliberate full accent ring.
        draw.arc(
            (ring_x - ring_r, ring_y - ring_r, ring_x + ring_r, ring_y + ring_r),
            start=0, end=360, fill="#8b5cf6", width=15
        )
    else:
        end_angle = (percent / 100) * 360 - 90
        arc_color = "#3b82f6" if percent < 80 else "#ef4444"
        draw.arc(
            (ring_x - ring_r, ring_y - ring_r, ring_x + ring_r, ring_y + ring_r),
            start=-90, end=end_angle, fill=arc_color, width=15
        )
    
    # Center text for ring — measure so "0.0%" and "100.0%" are both centred
    # instead of drifting with a fixed offset.
    if is_unlimited_traffic:
        pct_label = "∞"
        used_label = "UNLIMITED"
    else:
        pct_label = f"{percent:.1f}%"
        used_label = "USED"
    pct_w = draw.textlength(pct_label, font=font_medium)
    draw.text((ring_x - pct_w / 2, ring_y - 24), pct_label, fill="#f3f4f6", font=font_medium)
    used_w = draw.textlength(used_label, font=font_small)
    draw.text((ring_x - used_w / 2, ring_y + 14), used_label, fill="#9ca3af", font=font_small)

    # Text details — keep values clear of the QR block on the right.
    details_x = 320
    details_max_w = (width - qr_size - 30) - details_x - 20
    draw.text((details_x, 150), reshape_text("Usage Data:"), fill="#9ca3af", font=font_small)
    
    if is_unlimited_traffic:
        # total_gb of 0 means no traffic cap (same convention as
        # format_plan_volume). Kept in English like every other label on this
        # canvas — mixing a Persian word into an LTR line makes the bidi
        # algorithm reorder the "/" separator.
        usage_text = f"{used_gb:.2f} GB / Unlimited ∞"
    else:
        usage_text = f"{used_gb:.2f} GB / {total_gb:.2f} GB"
    usage_shaped = reshape_text(usage_text)
    usage_font = _fit_font(draw, usage_shaped, details_max_w, DEFAULT_FONT_PATH, 42) or font_large
    draw.text((details_x, 180), usage_shaped, fill="#f3f4f6", font=usage_font)
    
    draw.text((details_x, 260), reshape_text("Time Remaining:"), fill="#9ca3af", font=font_small)
    
    # days_left is None only when the subscription has no expiry at all.
    # A plain 0 still means "expires today" and must not be shown as unlimited.
    if days_left is None:
        time_text = "Unlimited ∞"
    else:
        time_text = f"{days_left} Days"
    time_shaped = reshape_text(time_text)
    time_font = _fit_font(draw, time_shaped, details_max_w, DEFAULT_FONT_PATH, 42) or font_large
    draw.text((details_x, 290), time_shaped, fill="#f3f4f6", font=time_font)

    # ── QR Code ──────────────────────────────────────────────────────────
    # Generate a clearly visible QR code of the vless URI
    if vless_uri:
        try:
            import segno

            qr = segno.make_qr(vless_uri, error="L")

            # Render QR to a temporary PNG buffer, then load as PIL Image
            qr_buf = io.BytesIO()
            qr.save(
                qr_buf,
                kind="png",
                scale=6,
                border=2,
                dark="#111827",   # dark modules = banner background color
                light="#e5e7eb", # light modules = bright gray (visible!)
            )
            qr_buf.seek(0)
            qr_img = Image.open(qr_buf).convert("RGB")

            # Fit QR into its reserved box
            qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

            # Position: top-right corner with padding
            qr_x = width - qr_size - 30
            qr_y = 30

            # Draw a rounded border around QR
            border_pad = 8
            draw.rounded_rectangle(
                (qr_x - border_pad, qr_y - border_pad,
                 qr_x + qr_size + border_pad, qr_y + qr_size + border_pad),
                radius=10,
                fill="#1f2937",
                outline="#3b82f6",
                width=2,
            )

            # Paste QR code
            image.paste(qr_img, (qr_x, qr_y))

            # Label under QR
            label = "Scan to connect"
            label_w = draw.textlength(label, font=font_small)
            draw.text(
                (qr_x + (qr_size - label_w) // 2, qr_y + qr_size + border_pad + 4),
                label,
                fill="#9ca3af",
                font=font_small,
            )
        except ImportError:
            logger.warning("banner: segno not installed, skipping QR code")
        except Exception as exc:
            logger.warning("banner: QR generation failed: %s", exc)

    # Footer
    draw.text((40, height - 40), reshape_text(f"User ID: {user_id}"), fill="#6b7280", font=font_small)
    footer_tag = f"@{bot_username}" if bot_username else ""
    draw.text((width - 250, height - 40), reshape_text(footer_tag), fill="#6b7280", font=font_small)

    out_bio = io.BytesIO()
    image.save(out_bio, format="PNG")
    out_bio.seek(0)
    return out_bio
