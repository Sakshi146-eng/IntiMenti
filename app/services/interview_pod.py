from typing import Dict, Any, Optional
import asyncio
import uuid
from datetime import datetime, timedelta
import psutil
import time
from database.models import (
    InterviewSession, Message, MessageRole, Score,
    RecruiterCommand, CommandType, InterviewStatus
)
from database.database import redis_client, postgres_client
from messaging.rabbitmq import rabbitmq_client
from integrations.openai_client import openai_client
from core.exceptions import InterviewTimeoutError, PodAtCapacityError
from utils import Timer, InterviewMetrics
from core.config import settings
import logging

logger = logging.getLogger(__name__)


class InterviewAgent:
    """Production interview agent with real OpenAI integration"""
    
    def __init__(self, session: InterviewSession):
        self.session = session
        self.running = True
        self.current_question = None
        self.pending_followup = None
        self.question_count = 0
        self.max_questions = settings.MAX_QUESTIONS_PER_INTERVIEW
        self.start_time = time.time()
        self.last_activity = datetime.utcnow()
        self.metrics = InterviewMetrics()
        self.previous_scores = []
        self.timeout_at = datetime.utcnow() + timedelta(minutes=settings.INTERVIEW_TIMEOUT_MINUTES)
        
        # ── asyncio.Event signals (in-process, zero-cost, instant) ──
        # Both the wait loops and process_recruiter_command run in the
        # same event loop, so we use Events instead of Redis polling.
        self._score_received = asyncio.Event()
        self._decision_received = asyncio.Event()
        self._pending_decision: dict = {}   # set by process_recruiter_command
        self._pod: "InterviewPod | None" = None  # back-reference for cleanup on end
    
    async def start(self):
        """Start the interview process"""
        logger.info(f"Starting interview {self.session.id} for {self.session.candidate_name}")
        
        # Send welcome message
        welcome_msg = Message(
            id=str(uuid.uuid4()),
            role=MessageRole.AI,
            content=f"Hello {self.session.candidate_name}! I'll be interviewing you for the {self.session.job_role} position. Let's begin with the first question.",
            metadata={"type": "welcome"}
        )
        
        self.session.conversation_history.append(welcome_msg)
        self.session.status = InterviewStatus.ACTIVE
        self.session.timeout_at = self.timeout_at
        await redis_client.update_session(self.session)
        await postgres_client.log_interview(self.session)
        
        # Ask first question
        await self.ask_next_question()
    
    async def check_timeout(self):
        """Check if interview has timed out"""
        if datetime.utcnow() > self.timeout_at:
            logger.warning(f"Interview {self.session.id} timed out")
            await self.end_interview(reason="timeout")
            raise InterviewTimeoutError(self.session.id)
    
    async def ask_next_question(self):
        """Ask the next question — uses fixed recruiter questions first, then AI-generated"""
        await self.check_timeout()
        
        self.question_count += 1
        self.last_activity = datetime.utcnow()
        
        if self.question_count > self.max_questions:
            await self.end_interview(reason="completed")
            return
        
        # Use fixed recruiter questions first, then AI-generated
        fixed_questions = self.session.initial_questions or []
        
        if self.question_count <= len(fixed_questions):
            # Use recruiter's fixed question
            question_text = fixed_questions[self.question_count - 1]
            logger.info(f"Interview {self.session.id} - Using fixed question {self.question_count}/{len(fixed_questions)}")
        else:
            # Generate question using AI
            with Timer(f"generate_question_{self.session.id}"):
                previous_questions = [
                    msg.content for msg in self.session.conversation_history 
                    if msg.role == MessageRole.AI and "welcome" not in (msg.metadata or {}).get("type", "")
                ]
                
                question_text = await openai_client.generate_question(
                    job_role=self.session.job_role,
                    job_description=self.session.job_description,
                    previous_questions=previous_questions,
                    candidate_level=self._determine_candidate_level()
                )
        
        question = Message(
            id=str(uuid.uuid4()),
            role=MessageRole.AI,
            content=question_text,
            metadata={
                "question_number": self.question_count,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
        
        self.session.conversation_history.append(question)
        self.session.current_question = question_text
        self.session.question_count = self.question_count
        self.current_question = question
        
        await redis_client.update_session(self.session)
        
        logger.info(f"Interview {self.session.id} - Asked Q{self.question_count}")
        
        # Notify recruiter
        await self.notify_recruiter({
            "type": "NEW_QUESTION",
            "session_id": self.session.id,
            "candidate_name": self.session.candidate_name,
            "question_number": self.question_count,
            "question": question_text,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    async def process_response(self, response_text: str):
        """Process candidate's response with OpenAI evaluation"""
        await self.check_timeout()
        
        # Store response
        response = Message(
            id=str(uuid.uuid4()),
            role=MessageRole.CANDIDATE,
            content=response_text,
            metadata={
                "response_to": self.current_question.id if self.current_question else None,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
        
        self.session.conversation_history.append(response)
        self.last_activity = datetime.utcnow()
        await redis_client.update_session(self.session)
        
        logger.info(f"Interview {self.session.id} - Received response (length: {len(response_text)})")
        
        # Evaluate response with OpenAI
        with Timer(f"evaluate_response_{self.session.id}"):
            evaluation = await openai_client.evaluate_answer(
                question=self.current_question.content if self.current_question else "",
                answer=response_text,
                job_role=self.session.job_role,
                job_description=self.session.job_description,
                previous_scores=self.previous_scores
            )
        
        # Create score
        score = Score(
            id=str(uuid.uuid4()),
            question_id=self.current_question.id if self.current_question else str(uuid.uuid4()),
            question_text=self.current_question.content if self.current_question else "",
            answer_text=response_text,
            ai_score=evaluation["score"],
            reasoning=evaluation["reasoning"],
            feedback=evaluation.get("feedback"),
            evaluation_metadata={
                "key_points_covered": evaluation.get("key_points_covered", []),
                "key_points_missed": evaluation.get("key_points_missed", []),
                "strengths": evaluation.get("strengths", []),
                "areas_for_improvement": evaluation.get("areas_for_improvement", [])
            }
        )
        
        self.session.scores.append(score)
        self.previous_scores.append(evaluation["score"])
        
        # Store metrics
        self.metrics.record_evaluation(evaluation["score"], 0)  # Time recorded by Timer
        
        # Store pending action if follow-up suggested
        if evaluation.get("should_ask_followup") and evaluation.get("followup_suggestion"):
            self.pending_followup = {
                "suggestion": evaluation["followup_suggestion"],
                "score": evaluation["score"],
                "question": self.current_question.content if self.current_question else "",
                "answer": response_text,
                "reasoning": evaluation["reasoning"],
                "feedback": evaluation.get("feedback"),
                "timestamp": datetime.utcnow().isoformat()
            }
            
            await redis_client.set_pending_action(self.session.id, self.pending_followup)
        
        # Notify recruiter
        await self.notify_recruiter({
            "type": "RESPONSE_EVALUATED",
            "session_id": self.session.id,
            "candidate_name": self.session.candidate_name,
            "question": self.current_question.content if self.current_question else "",
            "question_number": self.question_count,
            "answer": response_text,
            "ai_score": evaluation["score"],
            "reasoning": evaluation["reasoning"],
            "feedback": evaluation.get("feedback"),
            "suggested_followup": evaluation.get("followup_suggestion"),
            "should_ask_followup": evaluation.get("should_ask_followup", False),
            "strengths": evaluation.get("strengths", []),
            "areas_for_improvement": evaluation.get("areas_for_improvement", []),
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Always wait for recruiter score/feedback (every question)
        await self.wait_for_recruiter_score()
        
        # After the first 3 fixed questions, wait for recruiter decision (buttons)
        fixed_count = len(self.session.initial_questions or [])
        if self.question_count > fixed_count:
            # Q4+ — wait for recruiter decision (accept followup / custom / end)
            await self.wait_for_recruiter_decision()
        else:
            # Q1-Q3 — auto-proceed to next question after score
            await self.ask_next_question()
    
    async def wait_for_recruiter_score(self):
        """Block until recruiter submits /score, or timeout fires.
        Uses asyncio.Event — zero Redis reads, wakes instantly when score arrives."""
        self._score_received.clear()
        logger.info(
            f"Interview {self.session.id} - Waiting for recruiter score "
            f"(timeout {settings.RECRUITER_SCORE_TIMEOUT}s)"
        )
        try:
            await asyncio.wait_for(
                self._score_received.wait(),
                timeout=settings.RECRUITER_SCORE_TIMEOUT
            )
            logger.info(f"Interview {self.session.id} - Recruiter score received")
        except asyncio.TimeoutError:
            logger.warning(
                f"Interview {self.session.id} - Recruiter score timeout after "
                f"{settings.RECRUITER_SCORE_TIMEOUT}s, proceeding"
            )
    
    async def wait_for_recruiter_decision(self):
        """Block until recruiter makes a decision (accept/custom/end), or timeout.
        Uses asyncio.Event — wakes instantly when recruiter acts."""
        self._decision_received.clear()
        self._pending_decision = {}
        logger.info(
            f"Interview {self.session.id} - Waiting for recruiter decision "
            f"(timeout {settings.RECRUITER_DECISION_TIMEOUT}s)"
        )
        try:
            await asyncio.wait_for(
                self._decision_received.wait(),
                timeout=settings.RECRUITER_DECISION_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Interview {self.session.id} - Recruiter decision timeout, "
                "auto-proceeding to next question"
            )
            await self.ask_next_question()
            return
        
        # Act on what recruiter decided
        await self._execute_decision()
    
    async def _execute_decision(self):
        """Execute the recruiter's decision stored in self._pending_decision."""
        decision = self._pending_decision.get("type", "")
        
        if decision == "end":
            # end_interview already spawned as background task by process_recruiter_command
            logger.info(f"Interview {self.session.id} - Executing end decision")
            return
        
        elif decision == "accept":
            suggestion = self._pending_decision.get("suggestion", "")
            if suggestion:
                followup_msg = Message(
                    id=str(uuid.uuid4()),
                    role=MessageRole.AI,
                    content=suggestion,
                    metadata={
                        "is_followup": True,
                        "recruiter_approved": True,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )
                self.session.conversation_history.append(followup_msg)
                self.session.current_question = suggestion
                await redis_client.update_session(self.session)
                logger.info(f"Interview {self.session.id} - Asked approved followup")
        
        elif decision == "custom":
            custom_q = self._pending_decision.get("question", "")
            if custom_q:
                custom_msg = Message(
                    id=str(uuid.uuid4()),
                    role=MessageRole.AI,
                    content=custom_q,
                    metadata={
                        "is_followup": True,
                        "custom": True,
                        "recruiter_question": True,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )
                self.session.conversation_history.append(custom_msg)
                self.session.current_question = custom_q
                await redis_client.update_session(self.session)
                logger.info(f"Interview {self.session.id} - Asked custom recruiter question")
        
        # After followup question is set, ask next question
        if decision in ("accept", "custom"):
            await asyncio.sleep(1)   # brief pause before continuing
            await self.ask_next_question()
    
    async def process_recruiter_command(self, command: RecruiterCommand):
        """Process command from recruiter"""
        logger.info(f"Processing recruiter command: {command.type} for session {self.session.id}")
        
        # Log command to database
        await postgres_client.log_recruiter_command(
            self.session.id,
            command.type.value,
            command.recruiter_id,
            command.data or {}
        )
        
        if command.type == CommandType.ACCEPT_FOLLOWUP:
            # Retrieve suggestion from pending action (still write pending to Redis for logging)
            pending = await redis_client.get_pending_action(self.session.id)
            suggestion = pending.get("suggestion", "") if pending else ""
            self._pending_decision = {"type": "accept", "suggestion": suggestion}
            self._decision_received.set()    # ← wakes wait_for_recruiter_decision instantly
            logger.info(f"Recruiter accepted follow-up for session {self.session.id}")
                
        elif command.type == CommandType.CUSTOM_FOLLOWUP:
            self._pending_decision = {
                "type": "custom",
                "question": (command.data or {}).get("question", "")
            }
            self._decision_received.set()    # ← wakes wait_for_recruiter_decision instantly
            logger.info(f"Recruiter sent custom question for session {self.session.id}")
        
        elif command.type == CommandType.SCORE_SUBMITTED:
            # Store score in session + Redis, then fire score event
            if command.recruiter_score is not None and self.session.scores:
                latest = self.session.scores[-1]
                latest.recruiter_score = command.recruiter_score
                if command.recruiter_feedback:
                    latest.feedback = command.recruiter_feedback
                await redis_client.update_session(self.session)
                await postgres_client.log_interview(self.session)
                logger.info(
                    f"Recruiter score {command.recruiter_score}/10 recorded "
                    f"for session {self.session.id}"
                )
            elif not self.session.scores:
                logger.warning(
                    f"Recruiter tried to score {self.session.id} but no evaluations done yet"
                )
            self._score_received.set()       # ← wakes wait_for_recruiter_score instantly
                
        elif command.type == CommandType.END_INTERVIEW:
            # Fire both events to unblock any waiting loop immediately,
            # then spawn end_interview as background task
            self._pending_decision = {"type": "end"}
            self.running = False
            self._decision_received.set()    # ← unblocks wait_for_recruiter_decision
            self._score_received.set()       # ← unblocks wait_for_recruiter_score if mid-wait
            logger.info(f"Recruiter ended interview {self.session.id}")
            asyncio.create_task(self.end_interview(reason="recruiter_ended"))
            
        elif command.type == CommandType.PAUSE_INTERVIEW:
            # Pause interview
            self.session.status = InterviewStatus.PAUSED
            await redis_client.update_session(self.session)
            logger.info(f"Interview {self.session.id} paused by recruiter")
            
        elif command.type == CommandType.RESUME_INTERVIEW:
            # Resume interview
            self.session.status = InterviewStatus.ACTIVE
            await redis_client.update_session(self.session)
            logger.info(f"Interview {self.session.id} resumed by recruiter")
            
        elif command.type == CommandType.REQUEST_SUMMARY:
            # Generate and send summary
            summary = await self.generate_summary()
            await self.notify_recruiter({
                "type": "INTERVIEW_SUMMARY",
                "session_id": self.session.id,
                "candidate_name": self.session.candidate_name,
                "summary": summary,
                "timestamp": datetime.utcnow().isoformat()
            })
        
        # Persist updated session
        await redis_client.update_session(self.session)
    
    async def generate_summary(self) -> str:
        """Generate interview summary using OpenAI"""
        conversation_summary = [
            {"role": m.role.value, "content": m.content[:200]} 
            for m in self.session.conversation_history[-10:]  # Last 10 messages
        ]
        
        scores = [s.ai_score for s in self.session.scores]
        
        return await openai_client.generate_summary(conversation_summary, scores)
    
    async def end_interview(self, reason: str = "completed"):
        """End the interview"""
        logger.info(f"Interview {self.session.id} - Ending (reason: {reason})")
        
        # Calculate final score
        if self.session.scores:
            avg_score = sum(s.ai_score for s in self.session.scores) / len(self.session.scores)
        else:
            avg_score = 0
        
        # Generate comprehensive feedback
        try:
            with Timer(f"generate_feedback_{self.session.id}"):
                feedback = await openai_client.generate_feedback(
                    session_data={
                        "candidate_name": self.session.candidate_name,
                        "job_role": self.session.job_role,
                        "company": self.session.company
                    },
                    scores=[s.model_dump() for s in self.session.scores]
                )
        except Exception as e:
            logger.error(f"Failed to generate feedback for {self.session.id}: {e}")
            feedback = "Feedback generation was not available."
        
        # Send farewell message
        farewell = Message(
            id=str(uuid.uuid4()),
            role=MessageRole.AI,
            content=f"Thank you for your time! Your average score was {avg_score:.1f}/10.\n\n{feedback}",
            metadata={
                "type": "farewell",
                "final_score": avg_score,
                "end_reason": reason
            }
        )
        
        self.session.conversation_history.append(farewell)
        # Map all valid end reasons to COMPLETED
        completed_reasons = {"completed", "recruiter_ended", "timeout"}
        self.session.status = (
            InterviewStatus.COMPLETED if reason in completed_reasons
            else InterviewStatus.FAILED
        )
        self.session.end_time = datetime.utcnow()
        
        # Get metrics summary
        metrics_summary = self.metrics.get_summary()
        self.session.metadata = {
            **(self.session.metadata or {}),
            "metrics": metrics_summary,
            "end_reason": reason
        }
        
        # Update in Redis and PostgreSQL
        await redis_client.update_session(self.session)
        await postgres_client.log_interview(self.session)
        await redis_client.increment_metric("interviews_completed")
        
        # Notify recruiter
        await self.notify_recruiter({
            "type": "INTERVIEW_COMPLETED",
            "session_id": self.session.id,
            "candidate_name": self.session.candidate_name,
            "final_score": avg_score,
            "question_count": len(self.session.scores),
            "feedback": feedback,
            "metrics": metrics_summary,
            "end_reason": reason,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        self.running = False
        
        # Remove self from pod's active_interviews so pod capacity is freed
        if self._pod and self.session.id in self._pod.active_interviews:
            del self._pod.active_interviews[self.session.id]
            logger.info(
                f"Interview {self.session.id} removed from pod {self._pod.pod_id}. "
                f"Active: {len(self._pod.active_interviews)}"
            )
    
    async def notify_recruiter(self, update: dict):
        """Send update to recruiter via RabbitMQ"""
        # Include recruiter_chat_id so Telegram bot knows where to send
        update["recruiter_chat_id"] = self.session.recruiter_chat_id
        await rabbitmq_client.publish("telegram_updates", update)
    
    def _determine_candidate_level(self) -> str:
        """Determine candidate level based on scores"""
        if not self.previous_scores:
            return "mid"
        
        avg_score = sum(self.previous_scores) / len(self.previous_scores)
        if avg_score >= 8:
            return "senior"
        elif avg_score >= 6:
            return "mid"
        else:
            return "junior"


class InterviewPod:
    """Production pod manager with resource monitoring"""
    
    def __init__(self, pod_id: str):
        self.pod_id = pod_id
        self.active_interviews: Dict[str, InterviewAgent] = {}
        self.max_concurrent = settings.MAX_INTERVIEWS_PER_POD
        self.start_time = time.time()
        self.process = psutil.Process()
    
    async def start_interview(self, session: InterviewSession) -> bool:
        """Start a new interview in this pod"""
        if len(self.active_interviews) >= self.max_concurrent:
            logger.warning(f"Pod {self.pod_id} at capacity ({len(self.active_interviews)}/{self.max_concurrent})")
            
            # Notify orchestrator
            await rabbitmq_client.publish("orchestrator_commands", {
                "type": "REJECT_SESSION",
                "session_id": session.id,
                "pod_id": self.pod_id,
                "reason": "capacity_full",
                "timestamp": datetime.utcnow().isoformat()
            })
            
            raise PodAtCapacityError(self.pod_id, self.max_concurrent)
        
        # Create and start agent
        agent = InterviewAgent(session)
        agent._pod = self      # back-reference so agent can remove itself from pod on end
        self.active_interviews[session.id] = agent
        
        # Start interview
        asyncio.create_task(agent.start())
        
        # Register pod with metadata
        await redis_client.register_pod(
            self.pod_id,
            metadata=self.get_metadata()
        )
        
        logger.info(f"Pod {self.pod_id} started interview {session.id} (active: {len(self.active_interviews)})")
        await redis_client.increment_metric("interviews_started")
        
        return True
    
    async def process_response(self, session_id: str, response: str) -> bool:
        """Process candidate response"""
        if session_id in self.active_interviews:
            await self.active_interviews[session_id].process_response(response)
            return True
        return False
    
    async def handle_recruiter_command(self, command: RecruiterCommand) -> bool:
        """Route recruiter command to appropriate interview"""
        if command.session_id in self.active_interviews:
            await self.active_interviews[command.session_id].process_recruiter_command(command)
            return True
        return False
    
    async def remove_interview(self, session_id: str):
        """Remove completed/failed interview"""
        if session_id in self.active_interviews:
            del self.active_interviews[session_id]
            logger.info(f"Pod {self.pod_id} removed interview {session_id}")
    
    def get_load(self) -> int:
        """Get current number of active interviews"""
        return len(self.active_interviews)
    
    def get_uptime(self) -> float:
        """Get pod uptime in seconds"""
        return time.time() - self.start_time
    
    def get_memory_usage(self) -> float:
        """Get memory usage in MB"""
        return self.process.memory_info().rss / 1024 / 1024
    
    def get_cpu_percent(self) -> float:
        """Get CPU usage percentage"""
        return self.process.cpu_percent(interval=0.1)
    
    def get_metadata(self) -> dict:
        """Get pod metadata"""
        return {
            "uptime": self.get_uptime(),
            "memory_mb": self.get_memory_usage(),
            "cpu_percent": self.get_cpu_percent(),
            "active_interviews": len(self.active_interviews),
            "max_capacity": self.max_concurrent
        }
    
    def get_status(self) -> dict:
        """Get detailed pod status"""
        return {
            "pod_id": self.pod_id,
            "active_interviews": len(self.active_interviews),
            "max_capacity": self.max_concurrent,
            "uptime_seconds": self.get_uptime(),
            "memory_usage_mb": self.get_memory_usage(),
            "cpu_percent": self.get_cpu_percent(),
            "interviews": list(self.active_interviews.keys())
        }