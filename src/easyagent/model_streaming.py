"""Compatibility import for the streaming API; implementation lives with model protocols."""
from .models.streaming import MODEL_OBSERVER, fold_sse

__all__ = ["MODEL_OBSERVER", "fold_sse"]
