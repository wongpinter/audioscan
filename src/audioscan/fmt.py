"""Human-readable formatting helpers."""

from __future__ import annotations

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def format_duration(seconds: float | None) -> str:
    """Render a duration as ``H:MM:SS`` (or ``MM:SS`` when under an hour)."""
    if seconds is None or seconds < 0:
        return "-"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bytes(size: int | float | None, *, precision: int = 1) -> str:
    """Render a byte count using binary units."""
    if size is None:
        return "-"
    value = float(size)
    for unit in _UNITS:
        if abs(value) < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{value:.{precision}f} {_UNITS[-1]}"


def format_bitrate(bits_per_second: int | None) -> str:
    """Render a bitrate as ``128 kbps``."""
    if not bits_per_second:
        return "-"
    if bits_per_second >= 1000:
        return f"{round(bits_per_second / 1000)} kbps"
    return f"{bits_per_second} bps"


def truncate(text: str | None, width: int) -> str:
    """Shorten ``text`` to ``width`` characters, adding an ellipsis when cut."""
    if not text:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "\u2026"
