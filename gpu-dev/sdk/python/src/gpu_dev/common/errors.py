"""Error types for the GPU Dev SDK."""


class GpuDevError(Exception):
    """Base error for all SDK operations."""
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


class GpuDevAuthError(GpuDevError):
    """Authentication or authorization failure."""


class GpuDevNotFoundError(GpuDevError):
    """Reservation or resource not found."""


class GpuDevTimeoutError(GpuDevError):
    """Operation timed out."""


class GpuDevValidationError(GpuDevError):
    """Invalid parameters."""


class GpuDevConnectionError(GpuDevError):
    """SSH or network connection failure."""


class GpuDevCapacityError(GpuDevError):
    """No GPU capacity available."""
