from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum
import uuid


class InterviewStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class MessageRole(str, Enum):
    AI = "ai"
    CANDIDATE = "candidate"
    RECRUITER = "recruiter"
    SYSTEM = "system"


class CommandType(str, Enum):
    END_INTERVIEW = "end_interview"
    ACCEPT_FOLLOWUP = "accept_followup"
    CUSTOM_FOLLOWUP = "custom_followup"
    PAUSE_INTERVIEW = "pause_interview"
    RESUME_INTERVIEW = "resume_interview"
    REQUEST_SUMMARY = "request_summary"


class Message(BaseModel):
    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: MessageRole
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Optional[Dict[str, Any]] = None


class Score(BaseModel):
    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question_id: str
    question_text: str
    answer_text: str
    ai_score: float = Field(ge=0, le=10)
    recruiter_score: Optional[float] = Field(None, ge=0, le=10)
    reasoning: str
    feedback: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    evaluation_metadata: Optional[Dict[str, Any]] = None


class InterviewSession(BaseModel):
    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})
    
    id: str
    candidate_id: str
    candidate_name: str
    candidate_email: Optional[str] = None
    job_role: str
    job_description: Optional[str] = None
    company: Optional[str] = None
    status: InterviewStatus = InterviewStatus.PENDING
    start_time: datetime = Field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    current_question: Optional[str] = None
    conversation_history: List[Message] = []
    scores: List[Score] = []
    pod_id: str
    recruiter_id: str
    recruiter_chat_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    initial_questions: List[str] = []  # Fixed questions from recruiter
    question_count: int = 0
    timeout_at: Optional[datetime] = None


class InterviewSessionCreate(BaseModel):
    candidate_id: str
    candidate_name: str
    candidate_email: Optional[str] = None
    job_role: str
    job_description: Optional[str] = None
    company: Optional[str] = None
    recruiter_id: str
    recruiter_chat_id: Optional[str] = None
    initial_questions: List[str] = []  # Fixed questions from recruiter
    metadata: Optional[Dict[str, Any]] = None


class RecruiterCommand(BaseModel):
    type: CommandType
    session_id: str
    recruiter_id: str
    data: Optional[Dict[str, Any]] = None
    recruiter_score: Optional[float] = Field(None, ge=0, le=10)
    recruiter_feedback: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class CandidateResponse(BaseModel):
    session_id: str
    response: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    @field_validator('response')
    def response_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('Response cannot be empty')
        return v.strip()


class EvaluationResponse(BaseModel):
    score: float
    followup_suggestion: Optional[str] = None
    reasoning: str
    feedback: Optional[str] = None
    should_ask_followup: bool = False
    evaluation_metadata: Dict[str, Any] = Field(default_factory=dict)


class PodHealth(BaseModel):
    pod_id: str
    active_interviews: int
    max_capacity: int
    uptime_seconds: float
    memory_usage_mb: float
    cpu_percent: float
    status: str = "healthy"
    interviews: List[str] = []


class SystemStats(BaseModel):
    total_pods: int
    total_interviews: int
    active_interviews: int
    completed_interviews: int
    failed_interviews: int
    pod_capacity: int
    current_load: int
    load_percentage: float
    avg_response_time_ms: float
    uptime_seconds: float