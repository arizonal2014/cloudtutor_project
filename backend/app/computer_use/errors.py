"""Shared exceptions for Computer Use backends."""


class ComputerUseDependencyError(RuntimeError):
    """Raised when optional runtime dependencies are unavailable."""


class ComputerUseConfigurationError(RuntimeError):
    """Raised when required environment configuration is missing."""
