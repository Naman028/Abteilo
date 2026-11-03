"""Safe, stable error codes that cross the API boundary."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"
    message = "The request could not be processed."

    def __init__(self, message: str | None = None, code: str | None = None):
        super().__init__(message or self.message)
        if message:
            self.message = message
        if code:
            self.code = code


class AuthRequired(AppError):
    status_code, code, message = 401, "AUTH_REQUIRED", "Sign in to continue."


class InvalidCredentials(AppError):
    status_code, code, message = 401, "AUTH_INVALID_CREDENTIALS", "Email or password is incorrect."


class RateLimited(AppError):
    status_code, code, message = 429, "AUTH_RATE_LIMITED", "Too many attempts. Try again later."


class CsrfRequired(AppError):
    status_code, code, message = 403, "CSRF_REQUIRED", "Missing request header."


class Forbidden(AppError):
    status_code, code, message = 403, "FORBIDDEN", "You are not allowed to do this."


class NotFound(AppError):
    status_code, code, message = 404, "NOT_FOUND", "Not found."


class Conflict(AppError):
    status_code, code, message = 409, "CONFLICT", "The request conflicts with the current state."


class Invalid(AppError):
    status_code, code, message = 422, "VALIDATION_FAILED", "The request is invalid."


class ServiceUnavailable(AppError):
    status_code, code, message = 503, "SERVICE_UNAVAILABLE", "A required service is unavailable."


def _body(request: Request, code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(_body(request, exc.code, exc.message), status_code=exc.status_code)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Field locations only; never echo the submitted values.
    fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()})
    return JSONResponse(
        _body(request, "VALIDATION_FAILED", "Invalid fields: " + ", ".join(fields)), status_code=422
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(_body(request, "INTERNAL_ERROR", "Unexpected error."), status_code=500)
