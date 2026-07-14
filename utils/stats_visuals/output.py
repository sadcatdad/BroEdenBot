import io
import logging
import os
import time
from typing import Callable, Iterable, List, Optional, Tuple

from PIL import Image

from .models import (
    RenderDiagnostics,
    RenderPage,
    RenderResult,
    RenderState,
    RenderWarning,
)
from .theme import LayoutProfile


logger = logging.getLogger(__name__)
DEFAULT_TARGET_BYTES = 8_000_000


class ImageSizeLimitError(RuntimeError):
    pass


def image_target_bytes() -> int:
    raw = os.getenv("STATS_IMAGE_TARGET_BYTES", str(DEFAULT_TARGET_BYTES))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid STATS_IMAGE_TARGET_BYTES=%r; using %s", raw, DEFAULT_TARGET_BYTES)
        return DEFAULT_TARGET_BYTES
    if value <= 0:
        logger.warning("STATS_IMAGE_TARGET_BYTES must be positive; using %s", DEFAULT_TARGET_BYTES)
        return DEFAULT_TARGET_BYTES
    return value


def encode_png(image: Image.Image, *, quantize: bool = False) -> bytes:
    prepared = image.convert("RGB")
    if quantize:
        prepared = prepared.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    output = io.BytesIO()
    prepared.save(output, "PNG", optimize=True, compress_level=9)
    return output.getvalue()


def build_render_result(
    *,
    graphic_type: str,
    profile: LayoutProfile,
    factories: Iterable[Callable[[int, int], Image.Image]],
    state: Optional[RenderState] = None,
    target_bytes: Optional[int] = None,
) -> RenderResult:
    started = time.perf_counter()
    state = state or RenderState()
    target = target_bytes or image_target_bytes()
    factories = list(factories)
    page_count = len(factories)
    pages = []  # type: List[RenderPage]
    warnings = []  # type: List[RenderWarning]

    for index, factory in enumerate(factories, start=1):
        width, height = profile.width, profile.height
        optimized = False
        actions = []
        while True:
            image = factory(width, height)
            png = encode_png(image)
            if len(png) <= target:
                break

            palette_png = encode_png(image, quantize=True)
            if len(palette_png) < len(png):
                png = palette_png
                optimized = True
                actions.append("palette quantization")
            if len(png) <= target:
                break

            next_width = max(profile.minimum_width, int(width * 0.9))
            next_height = max(profile.minimum_height, int(height * 0.9))
            if (next_width, next_height) == (width, height):
                raise ImageSizeLimitError(
                    "{} page {} is {:,} bytes after optimization; target is {:,} bytes".format(
                        graphic_type, index, len(png), target
                    )
                )
            width, height = next_width, next_height
            optimized = True
            actions.append("re-rendered at {}x{}".format(width, height))

        if actions:
            message = "Page {}: {}".format(index, ", ".join(actions))
            warnings.append(RenderWarning("image_optimized", message))
            logger.info("Stats graphic optimization %s: %s", graphic_type, message)
        pages.append(
            RenderPage(
                png=png,
                width=width,
                height=height,
                byte_size=len(png),
                page_number=index,
                page_count=page_count,
                profile=profile.name,
                optimized=optimized,
            )
        )

    duration_ms = (time.perf_counter() - started) * 1000
    diagnostics = RenderDiagnostics(
        graphic_type=graphic_type,
        profile=profile.name,
        render_duration_ms=duration_ms,
        page_count=page_count,
        truncated_text_count=state.truncated_text_count,
        avatar_fallback_count=state.avatar_fallback_count,
        overflow_warnings=tuple(state.overflow_warnings),
    )
    logger.info(
        "Rendered stats graphic type=%s profile=%s size=%sx%s pages=%s "
        "bytes=%s duration_ms=%.1f optimized=%s truncated=%s "
        "avatar_fallbacks=%s overflows=%s",
        graphic_type,
        profile.name,
        pages[0].width if pages else 0,
        pages[0].height if pages else 0,
        page_count,
        [page.byte_size for page in pages],
        duration_ms,
        any(page.optimized for page in pages),
        state.truncated_text_count,
        state.avatar_fallback_count,
        len(state.overflow_warnings),
    )
    return RenderResult(tuple(pages), tuple(warnings), diagnostics)
