"""All entry points construct the exact same execution class and provider adapters."""
from pathlib import Path
from ..engine import Engine
from ..provider import ResponsesProvider
from .images import ImageProvider
from .providers import ChatAPIProvider
from .retry import RetryProvider


def create_engine(state, store, provider, tokens, *, services=None, transport_manages_retries=False):
    wire = ImageProvider(provider, Path(state.workspace))
    if isinstance(provider, (ResponsesProvider, ChatAPIProvider)) and not transport_manages_retries:
        wire = RetryProvider(wire)
    engine = Engine(state, store, wire, tokens, services=services)
    if isinstance(wire, RetryProvider):
        wire.engine = engine
    return engine
