import asyncio
import io
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageOps


MAX_AVATAR_BYTES = 2_000_000
MAX_CACHE_ITEMS = 500
_CACHE = OrderedDict()  # type: OrderedDict[str, bytes]


@dataclass(frozen=True)
class AvatarFetchResult:
    data: Dict[str, bytes]
    failed_urls: Tuple[str, ...]


async def fetch_avatars(urls: Iterable[Optional[str]]) -> AvatarFetchResult:
    unique = {url for url in urls if url}
    data = {}
    for url in unique:
        if url in _CACHE:
            data[url] = _CACHE[url]
            _CACHE.move_to_end(url)
    pending = unique.difference(data)
    failed = []
    if pending:
        timeout = aiohttp.ClientTimeout(total=8, connect=4, sock_read=5)
        connector = aiohttp.TCPConnector(limit=10)
        semaphore = asyncio.Semaphore(10)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async def fetch(url: str):
                try:
                    async with semaphore:
                        async with session.get(url) as response:
                            if response.status != 200:
                                return url, None
                            if (
                                response.content_length
                                and response.content_length > MAX_AVATAR_BYTES
                            ):
                                return url, None
                            payload = await response.read()
                            if len(payload) > MAX_AVATAR_BYTES:
                                return url, None
                            return url, payload
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    return url, None

            results = await asyncio.gather(*(fetch(url) for url in pending))
        for url, payload in results:
            if payload:
                data[url] = payload
                _CACHE[url] = payload
                _CACHE.move_to_end(url)
            else:
                failed.append(url)
    while len(_CACHE) > MAX_CACHE_ITEMS:
        _CACHE.popitem(last=False)
    return AvatarFetchResult(data=data, failed_urls=tuple(sorted(failed)))


def prepare_avatar(
    data: Optional[bytes],
    size: int,
    shape: str = "circle",
) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)  # Animated avatars intentionally use their first frame.
            corrected = ImageOps.exif_transpose(source)
            avatar = ImageOps.fit(
                corrected.convert("RGBA"),
                (size, size),
                method=Image.Resampling.LANCZOS,
            )
        if shape != "square":
            mask = Image.new("L", (size, size), 0)
            if shape == "rounded":
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, size - 1, size - 1),
                    radius=max(2, size // 5),
                    fill=255,
                )
            else:
                ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            avatar.putalpha(mask)
        return avatar
    except (EOFError, OSError, ValueError):
        return None
