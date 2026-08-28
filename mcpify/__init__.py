"""mcpify — turn any OpenAPI REST API into an MCP server for AI agents."""

from .spec import SpecError, load_spec, resolve_ref, resolve_schema
from .tools import AuthConfig, RequestError, build_request, spec_to_tools

__version__ = "1.0.4"
__all__ = [
    "SpecError",
    "load_spec",
    "resolve_ref",
    "resolve_schema",
    "AuthConfig",
    "RequestError",
    "build_request",
    "spec_to_tools",
    "__version__",
]
