"""Azure Document Intelligence client (Managed Identity, private endpoint).

Wraps ``azure-ai-documentintelligence`` so services never open the SDK directly.
Authentication uses ``DefaultAzureCredential`` (user-assigned Managed Identity)
over the resource's private endpoint. Calls are wrapped by the shared retry +
circuit-breaker policies (Requirements 4.5, 5.2); a normalized :class:`DIResult`
is returned so callers don't depend on the raw SDK response shape.
"""

from __future__ import annotations

from .client import DIResult, DocumentIntelligenceClient

__all__ = ["DIResult", "DocumentIntelligenceClient"]
