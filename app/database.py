import redis.asyncio as redis
import asyncpg
from typing import Optional, List, Dict, Any
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from config import settings
from models import InterviewSession
from exceptions import SessionNotFoundError

logger = logging.getLogger(__name__)


class RedisClient:
    """Production Redis client with connection pooling"""
    
    def __init__(self):
        self.pool: Optional[redis.ConnectionPool] = None
        self.redis: Optional[redis.Redis] = None
    
    async def connect(self):
        """Initialize Redis connection pool"""
        self.pool = redis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            decode_responses=True,
            socket_timeout=settings.REDIS_TIMEOUT,
            socket_connect_timeout=settings.REDIS_TIMEOUT
        )
        self.redis = redis.Redis(connection_pool=self.pool)
        
        # Test connection
        await self.redis.ping()
        logger.info(f"Connected to Redis at {settings.REDIS_HOST}:{settings.REDIS_PORT}")
    
    async def close(self):
        """Close Redis connection"""
        if self.pool:
            await self.pool.disconnect()
    
    @asynccontextmanager
    async def pipeline(self):
        """Get Redis pipeline"""
        async with self.redis.pipeline() as pipe:
            yield pipe
    
    async def set_session(self, session: InterviewSession):
        """Store session in Redis with TTL"""
        key = f"session:{session.id}"
        value = session.model_dump_json()
        
        async with self.pipeline() as pipe:
            await pipe.setex(key, settings.REDIS_TTL, value)
            await pipe.sadd(f"pod:{session.pod_id}:sessions", session.id)
            await pipe.execute()
        
        logger.debug(f"Session {session.id} stored in Redis")
    
    async def get_session(self, session_id: str) -> Optional[InterviewSession]:
        """Retrieve session from Redis"""
        key = f"session:{session_id}"
        data = await self.redis.get(key)
        
        if data:
            return InterviewSession.model_validate_json(data)
        
        logger.warning(f"Session {session_id} not found in Redis")
        return None
    
    async def update_session(self, session: InterviewSession):
        """Update existing session"""
        await self.set_session(session)
    
    async def delete_session(self, session_id: str):
        """Remove session"""
        key = f"session:{session_id}"
        session = await self.get_session(session_id)
        
        async with self.pipeline() as pipe:
            if session:
                await pipe.srem(f"pod:{session.pod_id}:sessions", session_id)
            await pipe.delete(key)
            await pipe.execute()
    
    async def get_pod_load(self, pod_id: str) -> int:
        """Get number of active interviews for a pod"""
        return await self.redis.scard(f"pod:{pod_id}:sessions")
    
    async def register_pod(self, pod_id: str, metadata: Dict[str, Any] = None):
        """Register active pod with metadata"""
        pipe = self.redis.pipeline()
        await pipe.sadd("active_pods", pod_id)
        await pipe.expire("active_pods", settings.POD_HEARTBEAT_INTERVAL * 2)
        
        if metadata:
            await pipe.hset(f"pod:metadata:{pod_id}", mapping=metadata)
            await pipe.expire(f"pod:metadata:{pod_id}", settings.POD_HEARTBEAT_INTERVAL * 2)
        
        await pipe.execute()
    
    async def get_active_pods(self) -> List[str]:
        """Get all active pods"""
        return await self.redis.smembers("active_pods")
    
    async def get_pod_metadata(self, pod_id: str) -> Dict[str, Any]:
        """Get pod metadata"""
        return await self.redis.hgetall(f"pod:metadata:{pod_id}")
    
    async def set_pending_action(self, session_id: str, action: dict):
        """Store pending recruiter action"""
        key = f"session:{session_id}:pending_action"
        await self.redis.setex(key, 300, json.dumps(action))  # 5 min TTL
    
    async def get_pending_action(self, session_id: str) -> Optional[dict]:
        """Get pending recruiter action"""
        key = f"session:{session_id}:pending_action"
        data = await self.redis.get(key)
        return json.loads(data) if data else None
    
    async def clear_pending_action(self, session_id: str):
        """Clear pending action"""
        key = f"session:{session_id}:pending_action"
        await self.redis.delete(key)
    
    async def increment_metric(self, metric_name: str, increment: int = 1):
        """Increment a metric counter"""
        key = f"metrics:{metric_name}"
        await self.redis.incrby(key, increment)
        # Expire after 24 hours to prevent unbounded growth
        await self.redis.expire(key, 86400)


class PostgresClient:
    """Production PostgreSQL client with connection pooling"""
    
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
    
    async def connect(self):
        """Initialize PostgreSQL connection pool"""
        self.pool = await asyncpg.create_pool(
            settings.DATABASE_URL,
            min_size=5,
            max_size=settings.POSTGRES_POOL_SIZE,
            max_queries=50000,
            max_inactive_connection_lifetime=300,
            command_timeout=60,
            ssl='require' if settings.ENVIRONMENT == 'production' else None
        )
        
        await self.init_db()
        logger.info(f"Connected to PostgreSQL at {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}")
    
    async def close(self):
        """Close PostgreSQL connection pool"""
        if self.pool:
            await self.pool.close()
    
    async def init_db(self):
        """Create tables and indexes if they don't exist"""
        async with self.pool.acquire() as conn:
            # Interviews table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS interviews (
                    id VARCHAR(50) PRIMARY KEY,
                    candidate_id VARCHAR(50) NOT NULL,
                    candidate_name VARCHAR(100) NOT NULL,
                    candidate_email VARCHAR(255),
                    job_role VARCHAR(100) NOT NULL,
                    job_description TEXT,
                    company VARCHAR(100),
                    status VARCHAR(20) NOT NULL,
                    start_time TIMESTAMP NOT NULL,
                    end_time TIMESTAMP,
                    pod_id VARCHAR(50) NOT NULL,
                    recruiter_id VARCHAR(50) NOT NULL,
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Messages table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id VARCHAR(50) PRIMARY KEY,
                    session_id VARCHAR(50) NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Scores table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS scores (
                    id VARCHAR(50) PRIMARY KEY,
                    session_id VARCHAR(50) NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    question_id VARCHAR(50) NOT NULL,
                    question_text TEXT NOT NULL,
                    answer_text TEXT NOT NULL,
                    ai_score FLOAT NOT NULL,
                    recruiter_score FLOAT,
                    reasoning TEXT,
                    feedback TEXT,
                    evaluation_metadata JSONB,
                    timestamp TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Commands log table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS recruiter_commands (
                    id SERIAL PRIMARY KEY,
                    session_id VARCHAR(50) NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    command_type VARCHAR(50) NOT NULL,
                    recruiter_id VARCHAR(50) NOT NULL,
                    command_data JSONB,
                    timestamp TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_interviews_recruiter ON interviews(recruiter_id);
                CREATE INDEX IF NOT EXISTS idx_interviews_status ON interviews(status);
                CREATE INDEX IF NOT EXISTS idx_interviews_pod ON interviews(pod_id);
                CREATE INDEX IF NOT EXISTS idx_interviews_candidate ON interviews(candidate_id);
                CREATE INDEX IF NOT EXISTS idx_interviews_start_time ON interviews(start_time);
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_scores_session ON scores(session_id);
                CREATE INDEX IF NOT EXISTS idx_commands_session ON recruiter_commands(session_id);
            """)
    
    async def log_interview(self, session: InterviewSession):
        """Log interview to PostgreSQL for audit"""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Insert/Update interview
                await conn.execute("""
                    INSERT INTO interviews (
                        id, candidate_id, candidate_name, candidate_email,
                        job_role, job_description, company, status,
                        start_time, end_time, pod_id, recruiter_id, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        end_time = EXCLUDED.end_time,
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                """,
                    session.id,
                    session.candidate_id,
                    session.candidate_name,
                    session.candidate_email,
                    session.job_role,
                    session.job_description,
                    session.company,
                    session.status.value,
                    session.start_time,
                    session.end_time,
                    session.pod_id,
                    session.recruiter_id,
                    json.dumps(session.metadata) if session.metadata else None
                )
                
                # Insert messages (skip if already exist)
                for msg in session.conversation_history:
                    await conn.execute("""
                        INSERT INTO messages (id, session_id, role, content, timestamp, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (id) DO NOTHING
                    """,
                        msg.id,
                        session.id,
                        msg.role.value,
                        msg.content,
                        msg.timestamp,
                        json.dumps(msg.metadata) if msg.metadata else None
                    )
                
                # Insert scores
                for score in session.scores:
                    await conn.execute("""
                        INSERT INTO scores (
                            id, session_id, question_id, question_text, answer_text,
                            ai_score, recruiter_score, reasoning, feedback,
                            evaluation_metadata, timestamp
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                        ON CONFLICT (id) DO NOTHING
                    """,
                        score.id,
                        session.id,
                        score.question_id,
                        score.question_text,
                        score.answer_text,
                        score.ai_score,
                        score.recruiter_score,
                        score.reasoning,
                        score.feedback,
                        json.dumps(score.evaluation_metadata) if score.evaluation_metadata else None,
                        score.timestamp
                    )
    
    async def log_recruiter_command(self, session_id: str, command_type: str, 
                                    recruiter_id: str, command_data: dict):
        """Log recruiter command"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO recruiter_commands (session_id, command_type, recruiter_id, command_data, timestamp)
                VALUES ($1, $2, $3, $4, $5)
            """,
                session_id,
                command_type,
                recruiter_id,
                json.dumps(command_data),
                datetime.utcnow()
            )
    
    async def get_interview_history(self, session_id: str) -> Dict[str, Any]:
        """Get complete interview history from database"""
        async with self.pool.acquire() as conn:
            # Get interview
            interview = await conn.fetchrow(
                "SELECT * FROM interviews WHERE id = $1",
                session_id
            )
            
            if not interview:
                raise SessionNotFoundError(session_id)
            
            # Get messages
            messages = await conn.fetch(
                "SELECT * FROM messages WHERE session_id = $1 ORDER BY timestamp",
                session_id
            )
            
            # Get scores
            scores = await conn.fetch(
                "SELECT * FROM scores WHERE session_id = $1 ORDER BY timestamp",
                session_id
            )
            
            # Get commands
            commands = await conn.fetch(
                "SELECT * FROM recruiter_commands WHERE session_id = $1 ORDER BY timestamp",
                session_id
            )
            
            return {
                "interview": dict(interview),
                "messages": [dict(m) for m in messages],
                "scores": [dict(s) for s in scores],
                "commands": [dict(c) for c in commands]
            }


# Global instances
redis_client = RedisClient()
postgres_client = PostgresClient()