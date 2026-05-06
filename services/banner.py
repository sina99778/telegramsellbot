import io
import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

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


def create_traffic_banner(
    config_name: str,
    user_id: int,
    status: str,
    used_gb: float,
    total_gb: float,
    days_left: int,
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

    # Header section
    draw.text((40, 30), reshape_text(f"Config: {config_name}"), fill="#f3f4f6", font=font_large)
    
    status_text = "🟢 ACTIVE" if is_active else ("🔴 " + status.upper())
    status_color = "#34d399" if is_active else "#f87171"
    # Place status text before QR area
    draw.text((40, 80), reshape_text(status_text), fill=status_color, font=font_medium)

    # Draw progress ring
    ring_x, ring_y, ring_r = 150, 250, 90
    draw.arc(
        (ring_x - ring_r, ring_y - ring_r, ring_x + ring_r, ring_y + ring_r),
        start=0, end=360, fill="#374151", width=15
    )
    
    # Calculate percentage
    percent = (used_gb / total_gb) * 100 if total_gb > 0 else 100
    percent = min(max(percent, 0), 100)
    
    # Draw active ring
    end_angle = (percent / 100) * 360 - 90
    arc_color = "#3b82f6" if percent < 80 else "#ef4444"
    draw.arc(
        (ring_x - ring_r, ring_y - ring_r, ring_x + ring_r, ring_y + ring_r),
        start=-90, end=end_angle, fill=arc_color, width=15
    )
    
    # Center text for ring
    draw.text((ring_x - 30, ring_y - 20), f"{percent:.1f}%", fill="#f3f4f6", font=font_medium)
    draw.text((ring_x - 20, ring_y + 15), "USED", fill="#9ca3af", font=font_small)

    # Text details
    details_x = 320
    draw.text((details_x, 150), reshape_text("Usage Data:"), fill="#9ca3af", font=font_small)
    draw.text((details_x, 180), reshape_text(f"{used_gb:.2f} GB / {total_gb:.2f} GB"), fill="#f3f4f6", font=font_large)
    
    draw.text((details_x, 260), reshape_text("Time Remaining:"), fill="#9ca3af", font=font_small)
    draw.text((details_x, 290), reshape_text(f"{days_left} Days"), fill="#f3f4f6", font=font_large)

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

            # Fit QR into a 160x160 box
            qr_size = 160
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
            # segno not installed — skip QR
            pass
        except Exception:
            # Any other QR failure — skip silently
            pass

    # Footer
    draw.text((40, height - 40), reshape_text(f"User ID: {user_id}"), fill="#6b7280", font=font_small)
    footer_tag = f"@{bot_username}" if bot_username else ""
    draw.text((width - 250, height - 40), reshape_text(footer_tag), fill="#6b7280", font=font_small)

    out_bio = io.BytesIO()
    image.save(out_bio, format="PNG")
    out_bio.seek(0)
    return out_bio
