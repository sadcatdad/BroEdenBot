"""Shared limits for decoding images and fetching Discord-hosted media."""

from urllib.parse import urlsplit


MAX_IMAGE_PIXELS = 40_000_000
DISCORD_MEDIA_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


def is_discord_media_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in DISCORD_MEDIA_HOSTS
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError):
        return False


def check_image_dimensions(image, *, max_pixels: int = MAX_IMAGE_PIXELS) -> None:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise ValueError("Image dimensions are invalid or excessively large.")
