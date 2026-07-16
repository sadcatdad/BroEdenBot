"""BroEdenBot Visual Content Studio public API."""

from .registry import REGISTRY, AssetSlot, TemplateDefinition, template_registry
from .repository import (
    initialize_visual_studio_schema,
    resolve_published_configuration,
)

__all__ = [
    "REGISTRY",
    "AssetSlot",
    "TemplateDefinition",
    "initialize_visual_studio_schema",
    "resolve_published_configuration",
    "template_registry",
]
