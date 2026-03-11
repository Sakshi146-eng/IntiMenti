import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional
import uuid
from functools import wraps
import asyncio

logger = logging.getLogger(__name__)


def generate_id(prefix: str = "") -> str:
    """Generate unique ID with optional prefix"""
    return f"{prefix}{uuid.uuid4().hex}" if prefix else uuid.uuid4().hex


def format_datetime(dt: datetime) -> str:
    """Format datetime to ISO format with timezone"""
    return dt.isoformat()


def parse_datetime(dt_str: str) -> datetime:
    """Parse ISO format datetime"""
    return datetime.fromisoformat(dt_str)


def truncate_text(text: str, max_length: int = 100) -> str:
    """Truncate text to max length"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def safe_json_loads(data: str, default: Any = None) -> Any:
    """Safely load JSON string"""
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return default


def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Async retry decorator with exponential backoff"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Retry {attempt + 1}/{max_retries} for {func.__name__}: {str(e)}"
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"All retries failed for {func.__name__}: {str(e)}"
                        )
            
            raise last_exception
        return wrapper
    return decorator


class Timer:
    """Context manager for timing operations"""
    
    def __init__(self, name: str = None):
        self.name = name or "operation"
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, *args):
        elapsed = time.time() - self.start_time
        logger.debug(f"{self.name} took {elapsed:.3f}s")
    
    @property
    def elapsed(self) -> float:
        if self.start_time:
            return time.time() - self.start_time
        return 0.0


class InterviewMetrics:
    """Collect interview metrics"""
    
    def __init__(self):
        self.metrics: Dict[str, Any] = {}
    
    def record_question(self, question_id: str, response_time: float):
        """Record question response time"""
        if "question_times" not in self.metrics:
            self.metrics["question_times"] = []
        self.metrics["question_times"].append({
            "question_id": question_id,
            "response_time": response_time
        })
    
    def record_evaluation(self, score: float, evaluation_time: float):
        """Record evaluation metrics"""
        if "evaluations" not in self.metrics:
            self.metrics["evaluations"] = []
        self.metrics["evaluations"].append({
            "score": score,
            "evaluation_time": evaluation_time
        })
    
    def get_summary(self) -> Dict[str, Any]:
        """Get metrics summary"""
        summary = {}
        
        if "question_times" in self.metrics:
            times = [q["response_time"] for q in self.metrics["question_times"]]
            summary["avg_response_time"] = sum(times) / len(times) if times else 0
            summary["total_questions"] = len(times)
        
        if "evaluations" in self.metrics:
            scores = [e["score"] for e in self.metrics["evaluations"]]
            summary["avg_score"] = sum(scores) / len(scores) if scores else 0
        
        return summary