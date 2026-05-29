"""Exceptions shared by external providers (email, calendar).

Tool factories catch these and map them to stable ``ToolErrorCode`` values so
secrets and raw provider details never leak to the LLM.
"""


class ExternalProviderError(Exception):
    """Base class for external provider failures."""


class AuthMissingError(ExternalProviderError):
    """Provider is not authenticated / configured."""


class ProviderPermissionError(ExternalProviderError):
    """Provider rejected the request due to insufficient permissions/scopes."""


class ProviderNotFoundError(ExternalProviderError):
    """Requested resource was not found by the provider."""
