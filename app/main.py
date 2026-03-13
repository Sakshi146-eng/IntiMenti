from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from typing import List, Optional
import logging
import time
import uuid

from core.config import settings, get_settings
from database.models import (
    InterviewSessionCreate, InterviewSession, 
    RecruiterCommand, CandidateResponse, PodHealth,
    SystemStats
)
from database.database import redis_client, postgres_client
from messaging.rabbitmq import rabbitmq_client
from services.orchestrator import orchestrator
from integrations.telegram_bot import telegram_bot
from core.exceptions import InterviewSystemException, http_exception_from_system_exception
from core.middleware import setup_middleware
from utils import Timer

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    logger.info(f"Starting {settings.APP_NAME} in {settings.ENVIRONMENT} mode...")
    
    # Connect to databases
    await redis_client.connect()
    await postgres_client.connect()
    await rabbitmq_client.connect()
    
    # Start services
    await orchestrator.start()
    await telegram_bot.start()  # Always start (runs in no-op mode if token not set)
    
    logger.info(f"Server started on http://{settings.HOST}:{settings.PORT}")
    
    yield
    
    # Cleanup
    logger.info("Shutting down...")
    await orchestrator.stop()
    await telegram_bot.stop()
    await rabbitmq_client.close()
    await redis_client.close()
    await postgres_client.close()


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    description="AI Interview System with Hybrid Microservices Architecture",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None
)

# Setup middleware
setup_middleware(app)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.DEBUG else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Exception handler
@app.exception_handler(InterviewSystemException)
async def interview_system_exception_handler(request: Request, exc: InterviewSystemException):
    logger.error(f"Interview system exception: {exc.message}", exc_info=True)
    return JSONResponse(
        status_code=http_exception_from_system_exception(exc).status_code,
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details
        }
    )


# Request ID middleware
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    with Timer(f"request_{request.method}_{request.url.path}"):
        response = await call_next(request)
    
    response.headers["X-Request-ID"] = request_id
    return response


# Health check
@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "timestamp": time.time()
    }


# Interview endpoints
@app.post(
    f"{settings.API_V1_PREFIX}/interviews/start",
    response_model=InterviewSession,
    tags=["Interviews"]
)
async def start_interview(session_data: InterviewSessionCreate):
    """Start a new interview"""
    try:
        session = await orchestrator.initialize_interview(session_data)
        logger.info(f"Interview started: {session.id}")
        return session
    except Exception as e:
        logger.error(f"Failed to start interview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    f"{settings.API_V1_PREFIX}/interviews/{{session_id}}/respond",
    tags=["Interviews"]
)
async def submit_response(session_id: str, response: CandidateResponse):
    """Submit candidate response"""
    try:
        await orchestrator.process_candidate_response(session_id, response.response)
        return {
            "status": "processing",
            "session_id": session_id,
            "timestamp": time.time()
        }
    except InterviewSystemException as e:
        raise http_exception_from_system_exception(e)
    except Exception as e:
        logger.error(f"Failed to process response: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    f"{settings.API_V1_PREFIX}/interviews/command",
    tags=["Interviews"]
)
async def send_recruiter_command(command: RecruiterCommand):
    """Send recruiter command"""
    try:
        await orchestrator.handle_recruiter_command(command)
        return {
            "status": "command_sent",
            "session_id": command.session_id,
            "timestamp": time.time()
        }
    except InterviewSystemException as e:
        raise http_exception_from_system_exception(e)
    except Exception as e:
        logger.error(f"Failed to send command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    f"{settings.API_V1_PREFIX}/interviews/{{session_id}}",
    response_model=InterviewSession,
    tags=["Interviews"]
)
async def get_interview(session_id: str):
    """Get interview session details"""
    session = await orchestrator.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")
    return session


@app.get(
    f"{settings.API_V1_PREFIX}/interviews",
    response_model=List[InterviewSession],
    tags=["Interviews"]
)
async def list_interviews(recruiter_id: Optional[str] = None):
    """List all interviews, optionally filtered by recruiter"""
    return await orchestrator.list_sessions(recruiter_id)


# Pod management endpoints
@app.get(
    f"{settings.API_V1_PREFIX}/pods",
    tags=["Pods"]
)
async def list_pods():
    """List all active pods"""
    statuses = await orchestrator.get_pod_status()
    return {
        "pods": statuses,
        "total": len(statuses),
        "timestamp": time.time()
    }


@app.get(
    f"{settings.API_V1_PREFIX}/pods/{{pod_id}}",
    response_model=PodHealth,
    tags=["Pods"]
)
async def get_pod_status(pod_id: str):
    """Get specific pod status"""
    pods = await orchestrator.get_pod_status()
    for pod in pods:
        if pod["pod_id"] == pod_id:
            return PodHealth(**pod)
    raise HTTPException(status_code=404, detail="Pod not found")


# Stats endpoint
@app.get(
    f"{settings.API_V1_PREFIX}/stats",
    response_model=SystemStats,
    tags=["Stats"]
)
async def get_system_stats():
    """Get system statistics"""
    return await orchestrator.get_system_stats()




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        workers=settings.WORKERS,
        log_level=settings.LOG_LEVEL.lower()
    )