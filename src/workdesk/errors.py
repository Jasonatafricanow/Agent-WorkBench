"""Structured business errors returned to the Host/Agent.

The `code` is stable and machine-readable; the message is for logs and must not
reveal whether a task outside the caller's visibility exists.
"""
from __future__ import annotations


class WorkbenchError(Exception):
    code = "ERROR"

    def __init__(self, message: str = "", **details):
        super().__init__(message or self.code)
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), **self.details}


class NotFound(WorkbenchError):
    """Missing OR not visible to the caller; the two are deliberately identical."""
    code = "NOT_FOUND"


class Forbidden(WorkbenchError):
    code = "FORBIDDEN"


class InvalidOperation(WorkbenchError):
    code = "INVALID"


class EvidenceRequired(WorkbenchError):
    code = "EVIDENCE_REQUIRED"


class VersionConflict(WorkbenchError):
    code = "VERSION_CONFLICT"


class IdempotencyConflict(WorkbenchError):
    code = "IDEMPOTENCY_CONFLICT"
