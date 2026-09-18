"""Controlled application errors.

Every error that can escape to the client must be one of these, so responses
never leak stack traces, prompts, or provider details (Section 08, SAFE FAILURE
and secret-handling rules).
"""


class GridWiseError(Exception):
    """Base class for controlled errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InterpretationError(GridWiseError):
    """The operator-note interpretation path failed in a controlled way."""


class OptimizationError(GridWiseError):
    """The optimizer failed or produced a schedule that failed final replay."""
