from dataclasses import dataclass, field
from typing import List, Sequence, Tuple


@dataclass(frozen=True)
class RenderWarning:
    code: str
    message: str


@dataclass(frozen=True)
class RenderPage:
    png: bytes
    width: int
    height: int
    byte_size: int
    page_number: int
    page_count: int
    profile: str
    optimized: bool = False


@dataclass(frozen=True)
class RenderDiagnostics:
    graphic_type: str
    profile: str
    render_duration_ms: float
    page_count: int
    truncated_text_count: int = 0
    avatar_fallback_count: int = 0
    overflow_warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderResult:
    pages: Tuple[RenderPage, ...]
    warnings: Tuple[RenderWarning, ...]
    diagnostics: RenderDiagnostics

    def attachment_names(self, filename: str) -> List[str]:
        stem, dot, suffix = filename.rpartition(".")
        if not dot:
            stem, suffix = filename, "png"
        if len(self.pages) == 1:
            return ["{}.{}".format(stem, suffix)]
        return [
            "{}_{}.{}".format(stem, page.page_number, suffix)
            for page in self.pages
        ]

    def attachments(self, filename: str) -> List[Tuple[str, bytes]]:
        names = self.attachment_names(filename)
        return [(name, page.png) for name, page in zip(names, self.pages)]


@dataclass
class RenderState:
    truncated_text_count: int = 0
    avatar_fallback_count: int = 0
    overflow_warnings: List[str] = field(default_factory=list)

