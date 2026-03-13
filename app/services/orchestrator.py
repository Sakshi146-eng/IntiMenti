import uuid
from typing import List, Optional, Dict
import asyncio
from datetime import datetime, timedelta
from database.models import (
    InterviewSession, InterviewSessionCreate, RecruiterCommand,
    InterviewStatus, SystemStats
)
from database.database import redis_client, postgres_client
from messaging.rabbitmq import rabbitmq_client
from services.interview_pod import InterviewPod
from core.exceptions import SessionNotFoundError, PodNotFoundError
from core.config import settings
import logging
import time

logger = logging.getLogger(__name__)


class Orchestrator:
    """Production orchestrator service"""
    
    def __init__(self):
        self.pods: Dict[str, InterviewPod] = {}
        self.pod_counter = 0
        self._running = True
        self.start_time = time.time()
    
    async def start(self):
        """Start orchestrator background tasks"""
        asyncio.create_task(self._pod_heartbeat_monitor())
        asyncio.create_task(self._consume_commands())
        asyncio.create_task(self._consume_recruiter_commands())
        asyncio.create_task(self._cleanup_stale_sessions())
        logger.info("Orchestrator started")
    
    async def stop(self):
        """Stop orchestrator"""
        self._running = False
    
    async def _pod_heartbeat_monitor(self):
        """Monitor pod heartbeats"""
        while self._running:
            try:
                # Get active pods from Redis
                active_pods = await redis_client.get_active_pods()
                
                # Check for stale pods
                for pod_id in list(self.pods.keys()):
                    if pod_id not in active_pods:
                        # Pod is stale, remove it
                        logger.warning(f"Pod {pod_id} is stale, removing")
                        del self.pods[pod_id]
                
                # Send heartbeats for our pods
                for pod_id, pod in self.pods.items():
                    await redis_client.register_pod(
                        pod_id,
                        metadata=pod.get_metadata()
                    )
                
                await asyncio.sleep(settings.POD_HEARTBEAT_INTERVAL)
            except Exception as e:
                logger.error(f"Error in pod heartbeat monitor: {e}")
                await asyncio.sleep(5)
    
    async def _consume_commands(self):
        """Consume commands from RabbitMQ"""
        async def handle_command(message: dict):
            try:
                command_type = message.get("type")
                
                if command_type == "REJECT_SESSION":
                    # Handle rejected session
                    session_id = message.get("session_id")
                    pod_id = message.get("pod_id")
                    reason = message.get("reason")
                    
                    logger.warning(f"Session {session_id} rejected by pod {pod_id}: {reason}")
                    
                    # Get session and reassign
                    session = await redis_client.get_session(session_id)
                    if session:
                        # Remove from old pod
                        if pod_id in self.pods:
                            await self.pods[pod_id].remove_interview(session_id)
                        
                        # Find new pod
                        new_pod = await self._find_optimal_pod()
                        session.pod_id = new_pod.pod_id
                        await redis_client.update_session(session)
                        
                        # Start in new pod
                        await new_pod.start_interview(session)
                        
                elif command_type == "POD_METRICS":
                    # Update pod metrics
                    pod_id = message.get("pod_id")
                    metrics = message.get("metrics")
                    if pod_id in self.pods:
                        # Store metrics if needed
                        pass
                        
            except Exception as e:
                logger.error(f"Error handling command: {e}")
        
        await rabbitmq_client.consume("orchestrator_commands", handle_command)
    
    async def _consume_recruiter_commands(self):
        """Consume recruiter commands from Telegram via RabbitMQ"""
        async def handle_recruiter_msg(message: dict):
            try:
                command_type = message.get("type", "")
                session_id = message.get("session_id", "")
                recruiter_id = message.get("recruiter_id", "")
                
                # Resolve short session IDs (first 8 chars)
                if len(session_id) < 36:
                    full_id = await self._resolve_session_id(session_id)
                    if not full_id:
                        logger.warning(f"Could not resolve session ID: {session_id}")
                        return
                    session_id = full_id
                
                # Build RecruiterCommand
                command = RecruiterCommand(
                    type=command_type,
                    session_id=session_id,
                    recruiter_id=recruiter_id,
                    data=message.get("data"),
                    recruiter_score=message.get("recruiter_score"),
                    recruiter_feedback=message.get("recruiter_feedback")
                )
                
                await self.handle_recruiter_command(command)
                logger.info(f"Processed Telegram command: {command_type} for session {session_id[:8]}")
                
            except Exception as e:
                logger.error(f"Error handling recruiter command from Telegram: {e}")
        
        await rabbitmq_client.consume("recruiter_commands", handle_recruiter_msg)
    
    async def _resolve_session_id(self, short_id: str) -> Optional[str]:
        """Resolve a short session ID to the full ID"""
        # Search active interviews across all pods
        for pod_id, pod in self.pods.items():
            for full_id in pod.active_interviews.keys():
                if full_id.startswith(short_id):
                    return full_id
        return None
    
    async def _cleanup_stale_sessions(self):
        """Clean up stale sessions"""
        while self._running:
            try:
                # Get all sessions
                sessions = await self.list_sessions()
                
                for session in sessions:
                    # Check if session is stale (no activity for 30 minutes)
                    if session.status == InterviewStatus.ACTIVE:
                        last_message = session.conversation_history[-1] if session.conversation_history else None
                        if last_message:
                            time_since_last = (datetime.utcnow() - last_message.timestamp).total_seconds()
                            if time_since_last > 1800:  # 30 minutes
                                logger.warning(f"Session {session.id} is stale, marking as failed")
                                session.status = InterviewStatus.FAILED
                                session.end_time = datetime.utcnow()
                                await redis_client.update_session(session)
                                await postgres_client.log_interview(session)
                
                await asyncio.sleep(300)  # Check every 5 minutes
            except Exception as e:
                logger.error(f"Error cleaning up stale sessions: {e}")
                await asyncio.sleep(60)
    
    async def initialize_interview(self, session_data: InterviewSessionCreate) -> InterviewSession:
        """Create and assign a new interview session"""
        # Find optimal pod
        target_pod = await self._find_optimal_pod()
        
        # Create session
        session = InterviewSession(
            id=str(uuid.uuid4()),
            candidate_id=session_data.candidate_id,
            candidate_name=session_data.candidate_name,
            candidate_email=session_data.candidate_email,
            job_role=session_data.job_role,
            job_description=session_data.job_description,
            company=session_data.company,
            status=InterviewStatus.PENDING,
            start_time=datetime.utcnow(),
            pod_id=target_pod.pod_id,
            recruiter_id=session_data.recruiter_id,
            recruiter_chat_id=session_data.recruiter_chat_id,
            initial_questions=session_data.initial_questions,
            metadata=session_data.metadata,
            timeout_at=datetime.utcnow() + timedelta(minutes=settings.INTERVIEW_TIMEOUT_MINUTES)
        )
        
        # Store in Redis
        await redis_client.set_session(session)
        
        # Log to PostgreSQL
        await postgres_client.log_interview(session)
        
        # Start interview in pod
        try:
            success = await target_pod.start_interview(session)
        except Exception as e:
            logger.error(f"Failed to start interview in pod {target_pod.pod_id}: {e}")
            # Try with new pod
            target_pod = await self._create_new_pod()
            session.pod_id = target_pod.pod_id
            await redis_client.update_session(session)
            await target_pod.start_interview(session)
        
        logger.info(f"Interview {session.id} initialized on pod {target_pod.pod_id}")
        
        # Increment metrics
        await redis_client.increment_metric("interviews_created")
        
        return session
    
    async def _find_optimal_pod(self) -> InterviewPod:
        """Find the least loaded pod using weighted algorithm"""
        if not self.pods:
            return await self._create_new_pod()
        
        # Calculate load scores for all pods
        pod_scores = []
        for pod_id, pod in self.pods.items():
            load = pod.get_load()
            if load < settings.MAX_INTERVIEWS_PER_POD:
                # Lower load = better score
                score = 1.0 - (load / settings.MAX_INTERVIEWS_PER_POD)
                pod_scores.append((pod, score))
        
        if pod_scores:
            # Weighted random selection based on scores
            import random
            total_score = sum(score for _, score in pod_scores)
            if total_score > 0:
                r = random.uniform(0, total_score)
                cumulative = 0
                for pod, score in pod_scores:
                    cumulative += score
                    if r <= cumulative:
                        return pod
        
        # All pods are at capacity or no suitable pods, create new one
        return await self._create_new_pod()
    
    async def _create_new_pod(self) -> InterviewPod:
        """Create a new interview pod"""
        self.pod_counter += 1
        pod_id = f"pod-{self.pod_counter:03d}"
        pod = InterviewPod(pod_id)
        self.pods[pod_id] = pod
        
        # Register in Redis
        await redis_client.register_pod(
            pod_id,
            metadata=pod.get_metadata()
        )
        
        logger.info(f"Created new pod: {pod_id}")
        return pod
    
    async def process_candidate_response(self, session_id: str, response: str):
        """Process candidate response"""
        session = await redis_client.get_session(session_id)
        if not session:
            raise SessionNotFoundError(session_id)
        
        # Find pod handling this session
        pod = self.pods.get(session.pod_id)
        if not pod:
            raise PodNotFoundError(session.pod_id)
        
        await pod.process_response(session_id, response)
    
    async def handle_recruiter_command(self, command: RecruiterCommand):
        """Process recruiter command"""
        # Get session to find which pod it's on
        session = await redis_client.get_session(command.session_id)
        if not session:
            raise SessionNotFoundError(command.session_id)
        
        # Find pod handling this session
        pod = self.pods.get(session.pod_id)
        if not pod:
            raise PodNotFoundError(session.pod_id)
        
        await pod.handle_recruiter_command(command)
        
        logger.info(f"Processed recruiter command for session {command.session_id}")
    
    async def get_session(self, session_id: str) -> Optional[InterviewSession]:
        """Get interview session by ID"""
        return await redis_client.get_session(session_id)
    
    async def list_sessions(self, recruiter_id: Optional[str] = None) -> List[InterviewSession]:
        """List all sessions, optionally filtered by recruiter"""
        sessions = []
        
        # Get all active pods
        active_pods = await redis_client.get_active_pods()
        
        for pod_id in active_pods:
            session_ids = await redis_client.redis.smembers(f"pod:{pod_id}:sessions")
            for session_id in session_ids:
                session = await redis_client.get_session(session_id)
                if session:
                    if not recruiter_id or session.recruiter_id == recruiter_id:
                        sessions.append(session)
        
        return sessions
    
    async def get_pod_status(self) -> List[dict]:
        """Get status of all pods"""
        return [pod.get_status() for pod in self.pods.values()]
    
    async def get_system_stats(self) -> SystemStats:
        """Get system statistics"""
        pods = await self.get_pod_status()
        sessions = await self.list_sessions()
        
        total_interviews = len(sessions)
        active_interviews = sum(1 for s in sessions if s.status == InterviewStatus.ACTIVE)
        completed_interviews = sum(1 for s in sessions if s.status == InterviewStatus.COMPLETED)
        failed_interviews = sum(1 for s in sessions if s.status == InterviewStatus.FAILED)
        
        pod_capacity = sum(p["max_capacity"] for p in pods)
        current_load = sum(p["active_interviews"] for p in pods)
        load_percentage = (current_load / pod_capacity * 100) if pod_capacity > 0 else 0
        
        # Calculate average response time (placeholder - would need actual metrics)
        avg_response_time = 2500  # 2.5 seconds placeholder
        
        return SystemStats(
            total_pods=len(pods),
            total_interviews=total_interviews,
            active_interviews=active_interviews,
            completed_interviews=completed_interviews,
            failed_interviews=failed_interviews,
            pod_capacity=pod_capacity,
            current_load=current_load,
            load_percentage=load_percentage,
            avg_response_time_ms=avg_response_time,
            uptime_seconds=time.time() - self.start_time
        )


# Global orchestrator instance
orchestrator = Orchestrator()