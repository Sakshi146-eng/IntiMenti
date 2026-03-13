from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import logging
import time

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    """Middleware to log all incoming requests"""
    
    async def __call__(self, request: Request, call_next):
        start_time = time.time()
        
        # Log request
        logger.info(
            f"Request: {request.method} {request.url.path} "
            f"from {request.client.host if request.client else 'unknown'}"
        )
        
        try:
            response = await call_next(request)
        except Exception as e:
            logger.error(f"Unhandled error: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "message": "An unexpected error occurred"}
            )
        
        # Log response
        duration = time.time() - start_time
        logger.info(
            f"Response: {request.method} {request.url.path} "
            f"status={response.status_code} duration={duration:.3f}s"
        )
        
        # Add timing header
        response.headers["X-Response-Time"] = f"{duration:.3f}s"
        
        return response


class RateLimitMiddleware:
    """Simple in-memory rate limiter"""
    
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict = {}  # ip -> list of timestamps
    
    async def __call__(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        
        # Clean old entries
        if client_ip in self._requests:
            self._requests[client_ip] = [
                t for t in self._requests[client_ip]
                if now - t < self.window_seconds
            ]
        else:
            self._requests[client_ip] = []
        
        # Check rate limit
        if len(self._requests[client_ip]) >= self.max_requests:
            logger.warning(f"Rate limit exceeded for {client_ip}")
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limit_exceeded", "message": "Too many requests"}
            )
        
        self._requests[client_ip].append(now)
        
        return await call_next(request)


def setup_middleware(app: FastAPI):
    """Setup all custom middleware for the application"""
    
    # Request logging
    logging_middleware = RequestLoggingMiddleware()
    app.middleware("http")(logging_middleware)
    
    # Rate limiting
    rate_limiter = RateLimitMiddleware(max_requests=100, window_seconds=60)
    app.middleware("http")(rate_limiter)
    
    logger.info("Custom middleware configured")
