"""Shared exception types."""


class ControllerError(Exception):
    """An operation the controller cannot perform now (maps to an HTTP status)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status
