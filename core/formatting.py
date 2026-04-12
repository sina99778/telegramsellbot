from __future__ import annotations


def format_volume_bytes(volume_bytes: int) -> str:
    if volume_bytes <= 0:
        return "0 GB"

    gigabytes = volume_bytes / (1024**3)
    if gigabytes.is_integer():
        return f"{int(gigabytes)} GB"
    return f"{gigabytes:.2f} GB"
