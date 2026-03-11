from pydantic_settings import BaseSettings
from pydantic import Field, validator
from typing import Optional, List
from functools import lru_cache
import secrets


class Settings(BaseSettings):
    # App
    APP_NAME: str = "AI Interview System"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = Field(default_factory=secrets.token_urlsafe)
    API_V1_PREFIX: str = "/api/v1"
    
    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4
    
    # LLM Settings (OpenAI-compatible - works with Ollama, OpenAI, etc.)
    OPENAI_API_KEY: str = "ollama"  # Dummy key for Ollama
    OPENAI_BASE_URL: str = "http://localhost:11434/v1"  # Ollama's OpenAI-compatible endpoint
    OPENAI_MODEL: str = "llama3.2:3b"
    OPENAI_MAX_TOKENS: int = 2000
    OPENAI_TEMPERATURE: float = 0.7
    
    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    REDIS_SSL: bool = False
    REDIS_TIMEOUT: int = 5
    REDIS_MAX_CONNECTIONS: int = 50
    
    @property
    def REDIS_URL(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        ssl = "?ssl=true" if self.REDIS_SSL else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}{ssl}"
    
    # RabbitMQ
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASS: str = "guest"
    RABBITMQ_VHOST: str = "/"
    RABBITMQ_HEARTBEAT: int = 60
    
    @property
    def RABBITMQ_URL(self) -> str:
        return f"amqp://{self.RABBITMQ_USER}:{self.RABBITMQ_PASS}@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}/{self.RABBITMQ_VHOST}"
    
    # PostgreSQL
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "interview_db"
    POSTGRES_POOL_SIZE: int = 20
    POSTGRES_MAX_OVERFLOW: int = 10
    
    @property
    def DATABASE_URL(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    
    # Interview Settings
    MAX_INTERVIEWS_PER_POD: int = 10
    POD_HEARTBEAT_INTERVAL: int = 30
    REDIS_TTL: int = 7200  # 2 hours
    MAX_QUESTIONS_PER_INTERVIEW: int = 15
    INTERVIEW_TIMEOUT_MINUTES: int = 60
    
    # Telegram
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    
    # Monitoring
    SENTRY_DSN: Optional[str] = None
    PROMETHEUS_ENABLED: bool = True
    METRICS_PORT: int = 9090
    
    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"
    LOG_FILE: Optional[str] = None
    
    class Config:
        env_file = "../.env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()