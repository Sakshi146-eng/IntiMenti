# 🤖 AI Interview System

An AI-powered technical interview platform built with **FastAPI** and a hybrid microservices architecture. Recruiters can start, monitor, and control interviews in real-time while an AI conducts adaptive technical interviews with candidates.

## ✨ Features

- **AI-Driven Interviews** — Uses LLM (Ollama/OpenAI) to conduct adaptive technical interviews with dynamic follow-up questions
- **Real-Time Recruiter Control** — Recruiters can pause, resume, end interviews, and inject custom questions mid-interview
- **Automated Scoring** — AI evaluates candidate responses on a 0–10 scale with detailed reasoning and feedback
- **Telegram Bot Integration** — Recruiters receive real-time notifications and can control interviews via Telegram
- **Pod-Based Architecture** — Interviews run in isolated pods for scalability and resource management
- **Comprehensive Audit Logging** — All interviews, messages, scores, and recruiter commands are persisted to PostgreSQL

## 🏗️ Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   FastAPI     │────▶│    Redis     │     │  PostgreSQL  │
│   (API)       │     │  (Sessions)  │     │   (Audit)    │
└──────┬───────┘     └──────────────┘     └──────────────┘
       │
       ├─────────────┐
       ▼             ▼
┌──────────────┐  ┌──────────────┐
│  RabbitMQ    │  │  Ollama/     │
│  (Messages)  │  │  OpenAI      │
└──────────────┘  └──────────────┘
```

| Component | Purpose |
|-----------|---------|
| **FastAPI** | REST API server with Swagger docs |
| **Redis** | Session state management with TTL |
| **PostgreSQL** | Persistent audit logging |
| **RabbitMQ** | Async message queuing between services |
| **Ollama / OpenAI** | LLM for interview question generation & evaluation |
| **Telegram Bot** | Real-time recruiter notifications |

## 📁 Project Structure

```
ai-interview-system/
├── app/
│   ├── core/
│   │   ├── config.py           # Pydantic settings & environment config
│   │   ├── exceptions.py       # Custom exception classes
│   │   └── middleware.py       # Custom middleware (logging, rate limiting)
│   ├── database/
│   │   ├── database.py         # Redis & PostgreSQL clients
│   │   └── models.py           # Data models (Session, Score, Message, etc.)
│   ├── integrations/
│   │   ├── openai_client.py    # LLM client (OpenAI-compatible / Ollama)
│   │   └── telegram_bot.py     # Telegram bot for recruiter notifications
│   ├── messaging/
│   │   └── rabbitmq.py         # RabbitMQ async client
│   ├── services/
│   │   ├── interview_pod.py    # Individual interview pod logic
│   │   └── orchestrator.py     # Interview lifecycle orchestration
│   ├── main.py                 # FastAPI application & API routes
│   ├── utils.py                # Utility functions (Timer, Metrics)
│   └── test_parallel.py        # Parallel interview testing
├── logs/                       # Application log files
├── docker-compose.yml          # Docker services orchestration
├── Dockerfile                  # API container image
├── init-db.sql                 # PostgreSQL schema initialization
├── requirements.txt            # Python dependencies
├── .env                        # Environment variables (not committed)
└── .gitignore
```

## 🚀 Getting Started

### Prerequisites

- **Python 3.11+**
- **Docker & Docker Compose**
- **Ollama** (for local LLM) or an **OpenAI API key**

### 1. Clone & Setup Environment

```bash
git clone <repo-url>
cd ai-interview-system

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

Copy and edit the `.env` file:

```bash
cp .env.example .env
```

Key settings to configure:

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | LLM API key (`ollama` for local) | `ollama` |
| `OPENAI_BASE_URL` | LLM endpoint | `http://localhost:11434/v1` |
| `OPENAI_MODEL` | Model name | `llama3.2:1b` |
| `OPENAI_MAX_TOKENS` | Max tokens per LLM response | `2000` |
| `OPENAI_TEMPERATURE` | LLM sampling temperature | `0.7` |
| `REDIS_HOST` | Redis host | `localhost` |
| `REDIS_PASSWORD` | Redis authentication password | `myredispass123` |
| `RABBITMQ_HOST` | RabbitMQ host | `localhost` |
| `RABBITMQ_USER` | RabbitMQ username | `admin` |
| `RABBITMQ_PASS` | RabbitMQ password | `myrabbitpass123` |
| `RABBITMQ_VHOST` | RabbitMQ virtual host | `/` |
| `POSTGRES_HOST` | PostgreSQL host | `localhost` |
| `POSTGRES_USER` | PostgreSQL user | `app_user` |
| `POSTGRES_PASSWORD` | PostgreSQL password | `mypassword123` |
| `POSTGRES_DB` | PostgreSQL database name | `interview_db` |
| `MAX_QUESTIONS_PER_INTERVIEW` | Max questions per session | `15` |
| `INTERVIEW_TIMEOUT_MINUTES` | Session timeout | `60` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) | — |
| `DEBUG` | Enable Swagger docs at `/docs` | `true` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `LOG_FILE` | Log output file path | `logs/app.log` |

> **Note:** When running the API locally, ensure `*_HOST=localhost` for Redis, PostgreSQL, and RabbitMQ. When running fully in Docker, use the service names (`redis`, `postgres`, `rabbitmq`).

### 3. Start Infrastructure Services

```bash
# Start Redis, PostgreSQL, RabbitMQ, and Ollama via Docker
docker compose up -d redis postgres rabbitmq ollama
```

Verify all services are healthy:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
```

### 4. Pull an Ollama Model (if using local LLM)

```bash
docker exec -it ai-interview-system-ollama-1 ollama pull llama3.2:1b
```

### 5. Run the API Server

```bash
cd app
uvicorn main:app --reload
```

The API will be available at:
- **API**: http://127.0.0.1:8000
- **Swagger Docs**: http://127.0.0.1:8000/docs (requires `DEBUG=true`)
- **ReDoc**: http://127.0.0.1:8000/redoc

### Full Docker Deployment (All Services)

To run everything in Docker including the API:

```bash
docker compose up -d
```

## 📡 API Endpoints

### Health
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |

### Interviews
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/interviews/start` | Start a new interview |
| `POST` | `/api/v1/interviews/{session_id}/respond` | Submit candidate response |
| `POST` | `/api/v1/interviews/command` | Send recruiter command |
| `GET` | `/api/v1/interviews/{session_id}` | Get interview details |
| `GET` | `/api/v1/interviews` | List all interviews |

### Pods & Stats
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/pods` | List active pods |
| `GET` | `/api/v1/pods/{pod_id}` | Get pod status |
| `GET` | `/api/v1/stats` | System statistics |

### Recruiter Commands

Recruiters can send commands during an active interview:

| Command | Description |
|---------|-------------|
| `end_interview` | End the interview and generate final scores |
| `pause_interview` | Pause the interview |
| `resume_interview` | Resume a paused interview |
| `accept_followup` | Accept AI-suggested follow-up question |
| `custom_followup` | Inject a custom question |
| `request_summary` | Get a summary of the interview so far |

## 🐳 Docker Services

| Service | Port(s) | Description |
|---------|---------|-------------|
| `api` | `8000`, `9090` | FastAPI application (2 replicas) |
| `redis` | `6379` | Session state management |
| `postgres` | `5432` | Audit logging database |
| `rabbitmq` | `5672`, `15672` | Message queue + management UI |
| `ollama` | `11434` | Local LLM inference |

Access RabbitMQ Management UI at http://localhost:15672 (default: `admin` / `myrabbitpass123`).

## 🛠️ Development

### Running Tests

```bash
cd app
python test_parallel.py
```

### Local Development (without Docker for app)

For faster iteration, run only infrastructure in Docker and the API locally:

```bash
# Start only infrastructure
docker compose up -d redis postgres rabbitmq ollama

# Run API locally with hot-reload
cd app
uvicorn main:app --reload
```

## 📝 License

This project is for educational purposes.
