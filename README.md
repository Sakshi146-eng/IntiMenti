# Recruiter-in-the-Loop Interview Orchestrator

## Project Overview
This project aims to streamline the interview process by orchestrating interviews efficiently, integrating various services for a comprehensive recruitment solution.

## Architecture Overview
- **FastAPI API**: Acts as the backbone of the application.
- **Orchestrator**: Manages the interview process.
- **Interview Pods**: Handles individual interview sessions.
- **Redis**: Caching layer for fast data access.
- **PostgreSQL**: Main database for persistent storage.
- **RabbitMQ**: Message broker for communication between services.
- **Telegram bot**: Interface for recruiters and candidates to interact.
- **OpenAI/Ollama**: AI components for improved interviewing.

## Prerequisites
- Python 3.8+
- Docker and Docker Compose
- PostgreSQL
- Redis
- RabbitMQ
- Environment variables set up as specified in the configuration section.

## .env Configuration
Ensure you have a `.env` file in the root directory with the following variables:
- `DATABASE_URL=postgres://user:password@localhost/dbname`
- `REDIS_URL=redis://localhost:6379/0`
- `RABBITMQ_URL=amqp://localhost`
- Additional environment variables as per project requirements.

## Docker Compose Quickstart
1. Clone the repository:
   ```bash
   git clone https://github.com/Sakshi146-eng/IntiMenti.git
   cd IntiMenti
   ```
2. Set up the environment:
   ```bash
   cp .env.example .env
   ```
3. Start the services:
   ```bash
   docker-compose up --build
   ```

## Local (Non-Docker) Run Steps
1. Install the required packages:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the application:
   ```bash
   uvicorn main:app --reload
   ```

## Key Features
- **Parallel Pods**: Run multiple interview sessions simultaneously.
- **Recruiter Scoring/Commands**: Recruiters can provide scores and feedback efficiently.
- **Audit Logging**: Every interaction during the interview process is logged for review.

## Basic API Endpoints
- `GET /api/interviews`: Retrieve all interviews.
- `POST /api/interviews`: Schedule a new interview.
- `GET /api/interviews/{id}`: Get details of a specific interview.
- `PUT /api/interviews/{id}`: Update an existing interview.
- `DELETE /api/interviews/{id}`: Cancel an interview.

For detailed API documentation, refer to the `/docs` endpoint after the server is running.