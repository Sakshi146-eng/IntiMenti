import logging
import asyncio
from typing import Optional, Dict, Any
from config import settings
from rabbitmq import rabbitmq_client

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot for recruiter notifications and commands.
    
    Uses polling mode for local development (no public URL needed).
    Consumes interview updates from RabbitMQ and sends to recruiters.
    Parses recruiter replies as commands (accept/custom/end/score).
    """
    
    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self._running = False
        self._consumer_task: Optional[asyncio.Task] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._last_update_id = 0
    
    @property
    def is_configured(self) -> bool:
        return bool(self.token) and self.token not in ("your-telegram-bot-token", "")
    
    async def start(self):
        """Start the Telegram bot"""
        if not self.is_configured:
            logger.warning(
                "Telegram bot token not configured. "
                "Running in no-op mode — updates will be logged but not sent."
            )
            self._running = True
            self._consumer_task = asyncio.create_task(self._consume_updates())
            return
        
        self._running = True
        
        # Start consuming RabbitMQ messages (outgoing notifications)
        self._consumer_task = asyncio.create_task(self._consume_updates())
        
        # Start polling for incoming Telegram messages
        self._polling_task = asyncio.create_task(self._poll_updates())
        
        logger.info("Telegram bot started (polling mode)")
    
    async def stop(self):
        """Stop the Telegram bot"""
        self._running = False
        for task in [self._consumer_task, self._polling_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("Telegram bot stopped")
    
    # ─── Outgoing: Send notifications to recruiters ──────────────
    
    async def _consume_updates(self):
        """Consume interview updates from RabbitMQ and forward to Telegram"""
        async def handle_update(message: dict):
            update_type = message.get("type", "UNKNOWN")
            session_id = message.get("session_id", "N/A")
            candidate_name = message.get("candidate_name", "Unknown")
            
            if not self.is_configured:
                logger.info(
                    f"[Telegram No-Op] {update_type} | "
                    f"Session: {session_id} | Candidate: {candidate_name}"
                )
                return
            
            # Build notification text
            text = self._format_notification(message)
            
            # Build inline keyboard if recruiter action needed
            reply_markup = self._build_keyboard(message)
            
            # Send to recruiter's chat
            chat_id = message.get("recruiter_chat_id")
            if chat_id:
                await self._send_message(chat_id, text, reply_markup=reply_markup)
            else:
                logger.debug(f"No chat_id for update {update_type}, skipping Telegram send")
        
        try:
            await rabbitmq_client.consume("telegram_updates", handle_update)
        except Exception as e:
            logger.error(f"Error in Telegram update consumer: {e}")
    
    def _format_notification(self, message: dict) -> str:
        """Format update message for Telegram"""
        update_type = message.get("type", "UNKNOWN")
        candidate = message.get("candidate_name", "Unknown")
        session_id = message.get("session_id", "N/A")
        short_id = session_id[:8] if session_id else "N/A"
        
        if update_type == "NEW_QUESTION":
            q_num = message.get("question_number", "?")
            question = message.get("question", "")
            return (
                f"📝 *Q{q_num} asked to {candidate}*\n"
                f"Session: `{short_id}`\n\n"
                f"_{question}_"
            )
        
        elif update_type == "RESPONSE_EVALUATED":
            score = message.get("ai_score", 0)
            reasoning = message.get("reasoning", "")
            answer = message.get("answer", "")[:200]
            followup = message.get("suggested_followup", "")
            text = (
                f"✅ *Response Evaluated* — *{candidate}*\n"
                f"Session: `{short_id}`\n\n"
                f"📊 AI Score: *{score}/10*\n"
                f"💬 Answer: _{answer}_\n\n"
                f"🧠 Reasoning: {reasoning[:200]}"
            )
            if message.get("should_ask_followup") and followup:
                text += (
                    f"\n\n💡 *Suggested Follow-up:*\n_{followup}_\n\n"
                    f"Reply with:\n"
                    f"`/accept {short_id}` — Ask AI's followup\n"
                    f"`/custom {short_id} your question` — Ask your own\n"
                    f"`/end {short_id}` — End interview\n"
                    f"`/score {short_id} 8 Great answer` — Score + feedback"
                )
            return text
        
        elif update_type == "INTERVIEW_COMPLETED":
            final_score = message.get("final_score", 0)
            q_count = message.get("question_count", 0)
            reason = message.get("end_reason", "completed")
            return (
                f"🏁 *Interview Completed* — *{candidate}*\n"
                f"Session: `{short_id}`\n\n"
                f"📊 Final Score: *{final_score:.1f}/10*\n"
                f"❓ Questions: {q_count}\n"
                f"📋 Reason: {reason}"
            )
        
        elif update_type == "INTERVIEW_SUMMARY":
            summary = message.get("summary", "No summary available.")
            return f"📊 *Interview Summary* — *{candidate}*\n\n{summary}"
        
        else:
            return f"ℹ️ *{update_type}* — Session: `{short_id}`"
    
    def _build_keyboard(self, message: dict) -> Optional[dict]:
        """Build inline keyboard for recruiter actions"""
        update_type = message.get("type", "")
        session_id = message.get("session_id", "")
        short_id = session_id[:8] if session_id else ""
        
        if update_type == "RESPONSE_EVALUATED" and message.get("should_ask_followup"):
            return {
                "inline_keyboard": [
                    [
                        {"text": "✅ Accept Followup", "callback_data": f"accept:{session_id}"},
                        {"text": "❌ End Interview", "callback_data": f"end:{session_id}"}
                    ]
                ]
            }
        return None
    
    # ─── Incoming: Poll for recruiter messages ──────────────────
    
    async def _poll_updates(self):
        """Poll Telegram for incoming messages (long polling)"""
        import httpx
        
        logger.info("Starting Telegram polling...")
        
        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,  # Long polling
                    "allowed_updates": ["message", "callback_query"]
                }
                
                async with httpx.AsyncClient(timeout=40) as client:
                    response = await client.get(url, params=params)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") and data.get("result"):
                        for update in data["result"]:
                            self._last_update_id = update["update_id"]
                            await self._process_update(update)
                elif response.status_code == 401:
                    logger.error("Telegram bot token is invalid! Stopping polling.")
                    self._running = False
                    return
                else:
                    logger.warning(f"Telegram polling error: {response.status_code}")
                    await asyncio.sleep(5)
                    
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Telegram polling error: {e}")
                await asyncio.sleep(5)
    
    async def _process_update(self, update: dict):
        """Process incoming Telegram update"""
        try:
            # Handle callback queries (inline button presses)
            if "callback_query" in update:
                await self._handle_callback(update["callback_query"])
                return
            
            # Handle text messages
            message = update.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text", "")
            
            if not chat_id or not text:
                return
            
            logger.info(f"Received Telegram message from {chat_id}: {text[:50]}")
            
            # Parse commands
            if text.startswith("/start"):
                await self._send_message(
                    chat_id,
                    "👋 *Welcome to AI Interview System!*\n\n"
                    "You'll receive interview updates here.\n\n"
                    "Commands:\n"
                    "`/accept <session_id>` — Accept AI's followup question\n"
                    "`/custom <session_id> <question>` — Send your own question\n"
                    "`/end <session_id>` — End the interview\n"
                    "`/score <session_id> <score> <feedback>` — Add your score\n"
                    "`/help` — Show this help message"
                )
            elif text.startswith("/help"):
                await self._send_message(
                    chat_id,
                    "🤖 *AI Interview System Commands*\n\n"
                    "`/accept <id>` — Accept AI's suggested followup\n"
                    "`/custom <id> <your question>` — Ask your own question\n"
                    "`/end <id>` — End the interview\n"
                    "`/score <id> <0-10> <feedback>` — Give your score\n\n"
                    "You can use the first 8 chars of the session ID."
                )
            elif text.startswith("/accept"):
                await self._handle_accept(chat_id, text)
            elif text.startswith("/custom"):
                await self._handle_custom(chat_id, text)
            elif text.startswith("/end"):
                await self._handle_end(chat_id, text)
            elif text.startswith("/score"):
                await self._handle_score(chat_id, text)
            else:
                await self._send_message(
                    chat_id,
                    "❓ Unknown command. Use /help for available commands."
                )
                
        except Exception as e:
            logger.error(f"Error processing Telegram update: {e}")
    
    async def _handle_callback(self, callback_query: dict):
        """Handle inline keyboard button presses"""
        data = callback_query.get("data", "")
        chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        
        if ":" not in data:
            return
        
        action, session_id = data.split(":", 1)
        
        if action == "accept":
            await self._send_recruiter_command(
                session_id, chat_id, "accept_followup"
            )
            await self._answer_callback(callback_query["id"], "✅ Followup accepted!")
        elif action == "end":
            await self._send_recruiter_command(
                session_id, chat_id, "end_interview"
            )
            await self._answer_callback(callback_query["id"], "❌ Interview ended!")
    
    async def _handle_accept(self, chat_id: str, text: str):
        """Handle /accept command"""
        parts = text.split()
        if len(parts) < 2:
            await self._send_message(chat_id, "Usage: `/accept <session_id>`")
            return
        session_id = parts[1]
        await self._send_recruiter_command(session_id, chat_id, "accept_followup")
        await self._send_message(chat_id, f"✅ Followup accepted for session `{session_id[:8]}`")
    
    async def _handle_custom(self, chat_id: str, text: str):
        """Handle /custom command"""
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await self._send_message(chat_id, "Usage: `/custom <session_id> <your question>`")
            return
        session_id = parts[1]
        question = parts[2]
        await self._send_recruiter_command(
            session_id, chat_id, "custom_followup",
            data={"question": question}
        )
        await self._send_message(chat_id, f"📝 Custom question sent for session `{session_id[:8]}`")
    
    async def _handle_end(self, chat_id: str, text: str):
        """Handle /end command"""
        parts = text.split()
        if len(parts) < 2:
            await self._send_message(chat_id, "Usage: `/end <session_id>`")
            return
        session_id = parts[1]
        await self._send_recruiter_command(session_id, chat_id, "end_interview")
        await self._send_message(chat_id, f"🛑 Interview ended for session `{session_id[:8]}`")
    
    async def _handle_score(self, chat_id: str, text: str):
        """Handle /score command"""
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            await self._send_message(chat_id, "Usage: `/score <session_id> <0-10> [feedback]`")
            return
        
        session_id = parts[1]
        try:
            score = float(parts[2])
            if score < 0 or score > 10:
                raise ValueError()
        except ValueError:
            await self._send_message(chat_id, "❌ Score must be a number between 0 and 10")
            return
        
        feedback = parts[3] if len(parts) > 3 else None
        await self._send_recruiter_command(
            session_id, chat_id, "accept_followup",
            recruiter_score=score, recruiter_feedback=feedback
        )
        await self._send_message(
            chat_id,
            f"📊 Score {score}/10 recorded for session `{session_id[:8]}`"
            + (f"\n💬 Feedback: {feedback}" if feedback else "")
        )
    
    async def _send_recruiter_command(
        self, session_id: str, chat_id: str, command_type: str,
        data: Optional[dict] = None,
        recruiter_score: Optional[float] = None,
        recruiter_feedback: Optional[str] = None
    ):
        """Send a recruiter command via RabbitMQ to the orchestrator"""
        command = {
            "type": command_type,
            "session_id": session_id,
            "recruiter_id": f"telegram_{chat_id}",
            "data": data or {},
            "recruiter_score": recruiter_score,
            "recruiter_feedback": recruiter_feedback,
            "source": "telegram"
        }
        await rabbitmq_client.publish("recruiter_commands", command)
        logger.info(f"Sent recruiter command: {command_type} for session {session_id}")
    
    # ─── Telegram API helpers ────────────────────────────────────
    
    async def _send_message(
        self, chat_id: str, text: str,
        reply_markup: Optional[dict] = None
    ):
        """Send message via Telegram Bot API"""
        try:
            import httpx
            
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=10)
                
                if response.status_code != 200:
                    logger.error(
                        f"Telegram API error: {response.status_code} — {response.text}"
                    )
                else:
                    logger.debug(f"Telegram message sent to {chat_id}")
                    
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
    
    async def _answer_callback(self, callback_query_id: str, text: str):
        """Answer a callback query (inline button press)"""
        try:
            import httpx
            
            url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
            payload = {
                "callback_query_id": callback_query_id,
                "text": text
            }
            
            async with httpx.AsyncClient() as client:
                await client.post(url, json=payload, timeout=10)
                
        except Exception as e:
            logger.error(f"Failed to answer callback query: {e}")


# Global Telegram bot instance
telegram_bot = TelegramBot()
