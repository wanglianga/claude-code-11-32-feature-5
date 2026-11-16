"""Service-level errors with machine-readable codes."""
from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def not_found(code: str, message: str, details: Any | None = None) -> ServiceError:
    return ServiceError(404, code, message, details)


def conflict(code: str, message: str, details: Any | None = None) -> ServiceError:
    return ServiceError(409, code, message, details)


def invalid(code: str, message: str, details: Any | None = None) -> ServiceError:
    return ServiceError(422, code, message, details)


def unprocessable(code: str, message: str, details: Any | None = None) -> ServiceError:
    return ServiceError(409, code, message, details)
