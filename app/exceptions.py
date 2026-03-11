from fastapi import HTTPException, status
from typing import Optional, Any


class InterviewSystemException(Exception):
    """Base exception for interview system"""
    def __init__(self, message: str, code: str = None, details: Any = None):
        self.message = message
        self.code = code or "system_error"
        self.details = details
        super().__init__(message)


class SessionNotFoundError(InterviewSystemException):
    def __init__(self, session_id: str):
        super().__init__(
            message=f"Interview session {session_id} not found",
            code="session_not_found",
            details={"session_id": session_id}
        )


class PodNotFoundError(InterviewSystemException):
    def __init__(self, pod_id: str):
        super().__init__(
            message=f"Pod {pod_id} not found",
            code="pod_not_found",
            details={"pod_id": pod_id}
        )


class PodAtCapacityError(InterviewSystemException):
    def __init__(self, pod_id: str, capacity: int):
        super().__init__(
            message=f"Pod {pod_id} is at maximum capacity ({capacity})",
            code="pod_at_capacity",
            details={"pod_id": pod_id, "capacity": capacity}
        )


class InterviewTimeoutError(InterviewSystemException):
    def __init__(self, session_id: str):
        super().__init__(
            message=f"Interview {session_id} has timed out",
            code="interview_timeout",
            details={"session_id": session_id}
        )


class OpenAIError(InterviewSystemException):
    def __init__(self, message: str, original_error: Optional[Exception] = None):
        super().__init__(
            message=message,
            code="openai_error",
            details={"original_error": str(original_error) if original_error else None}
        )


def http_exception_from_system_exception(exc: InterviewSystemException) -> HTTPException:
    """Convert system exception to HTTP exception"""
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    
    if isinstance(exc, SessionNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, PodNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, PodAtCapacityError):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(exc, InterviewTimeoutError):
        status_code = status.HTTP_408_REQUEST_TIMEOUT
    
    return HTTPException(
        status_code=status_code,
        detail={
            "message": exc.message,
            "code": exc.code,
            "details": exc.details
        }
    )