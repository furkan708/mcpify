"""mcpify — turn any OpenAPI REST API into an MCP server for AI agents."""

from .spec import SpecError, load_spec, resolve_ref, resolve_schema
from .tools import AuthConfig, RequestError, build_request, spec_to_tools

__version__ = "1.18.1"
__all__ = [
    "AuthConfig",
    "RequestError",
    "SpecError",
    "__version__",
    "build_request",
    "load_spec",
    "resolve_ref",
    "resolve_schema",
    "spec_to_tools",
]
