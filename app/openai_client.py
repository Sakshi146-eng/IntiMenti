import openai
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from typing import List, Dict, Any, Optional
import json
import logging
import tiktoken
from config import settings
from exceptions import OpenAIError
from utils import Timer

logger = logging.getLogger(__name__)


class OpenAIClient:
    """Production OpenAI client with error handling, retries, and token management"""
    
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL
        )
        self.model = settings.OPENAI_MODEL
        self.max_tokens = settings.OPENAI_MAX_TOKENS
        self.temperature = settings.OPENAI_TEMPERATURE
        try:
            self.encoder = tiktoken.encoding_for_model(self.model)
        except KeyError:
            # Fallback for newer models not yet in tiktoken's registry
            self.encoder = tiktoken.get_encoding("cl100k_base")
    
    def count_tokens(self, text: str) -> int:
        """Count tokens in text"""
        return len(self.encoder.encode(text))
    
    def truncate_to_token_limit(self, text: str, max_tokens: int) -> str:
        """Truncate text to fit within token limit"""
        tokens = self.encoder.encode(text)
        if len(tokens) <= max_tokens:
            return text
        truncated_tokens = tokens[:max_tokens]
        return self.encoder.decode(truncated_tokens)
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    async def generate_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, str]] = None
    ) -> str:
        """Generate completion from LLM (OpenAI, Ollama, etc.)"""
        try:
            with Timer("llm_completion"):
                kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature or self.temperature,
                    "max_tokens": max_tokens or self.max_tokens,
                }
                # Only add response_format if provided (not all providers support it)
                if response_format:
                    kwargs["response_format"] = response_format
                
                response = await self.client.chat.completions.create(**kwargs)
            
            content = response.choices[0].message.content
            
            # Log usage if available (Ollama may not return usage stats)
            if hasattr(response, 'usage') and response.usage:
                logger.info(
                    f"LLM completion: {response.usage.prompt_tokens} prompt tokens, "
                    f"{response.usage.completion_tokens} completion tokens, "
                    f"total: {response.usage.total_tokens}"
                )
            else:
                logger.info("LLM completion successful")
            
            return content
            
        except openai.APIError as e:
            logger.error(f"LLM API error: {e}")
            raise OpenAIError(f"LLM API error: {str(e)}", original_error=e)
        except Exception as e:
            logger.error(f"Unexpected error in LLM completion: {e}")
            raise OpenAIError(f"Unexpected error: {str(e)}", original_error=e)
    
    async def generate_question(
        self,
        job_role: str,
        job_description: Optional[str] = None,
        previous_questions: List[str] = None,
        candidate_level: str = "mid"
    ) -> str:
        """Generate interview question based on job role"""
        
        system_prompt = """You are an expert technical interviewer. Generate relevant, 
        challenging interview questions based on the job role and candidate's level. 
        Questions should test both theoretical knowledge and practical experience."""
        
        context = f"Job Role: {job_role}\n"
        if job_description:
            context += f"Job Description: {job_description}\n"
        context += f"Candidate Level: {candidate_level}\n"
        
        if previous_questions:
            context += f"Previous Questions Asked: {', '.join(previous_questions)}\n"
            context += "Generate a different question that hasn't been asked yet."
        
        user_prompt = f"""{context}
        
        Generate a single, specific interview question. The question should:
        1. Be relevant to the role
        2. Test both knowledge and experience
        3. Encourage detailed responses
        4. Be appropriate for the candidate's level
        
        Return only the question, no additional text or formatting."""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        return await self.generate_completion(messages)
    
    async def evaluate_answer(
        self,
        question: str,
        answer: str,
        job_role: str,
        job_description: Optional[str] = None,
        previous_scores: List[float] = None
    ) -> Dict[str, Any]:
        """Evaluate candidate's answer with detailed scoring"""
        
        system_prompt = """You are an expert interviewer evaluating candidate responses. 
        Provide detailed evaluation with score, reasoning, and follow-up suggestions.
        Return response in JSON format."""
        
        context = f"Job Role: {job_role}\n"
        if job_description:
            context += f"Job Description: {job_description}\n"
        if previous_scores:
            avg_previous = sum(previous_scores) / len(previous_scores)
            context += f"Previous Average Score: {avg_previous:.1f}/10\n"
        
        user_prompt = f"""{context}
        
        Question: {question}
        Candidate Answer: {answer}
        
        Evaluate this answer and return a JSON object with:
        - score: number from 1-10 (10 being excellent)
        - reasoning: detailed explanation of the score
        - feedback: constructive feedback for the candidate
        - followup_suggestion: a relevant follow-up question to explore further (or null if not needed)
        - should_ask_followup: boolean indicating if follow-up is recommended
        - key_points_covered: list of key points the candidate addressed
        - key_points_missed: list of important points the candidate missed
        - strengths: list of strengths demonstrated
        - areas_for_improvement: list of areas to improve
        
        Be objective and fair in your evaluation."""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        response = await self.generate_completion(
            messages,
            response_format={"type": "json_object"}
        )
        
        try:
            evaluation = json.loads(response)
            # Ensure all required fields
            evaluation.setdefault("score", 5)
            evaluation.setdefault("reasoning", "")
            evaluation.setdefault("feedback", "")
            evaluation.setdefault("followup_suggestion", None)
            evaluation.setdefault("should_ask_followup", False)
            evaluation.setdefault("key_points_covered", [])
            evaluation.setdefault("key_points_missed", [])
            evaluation.setdefault("strengths", [])
            evaluation.setdefault("areas_for_improvement", [])
            
            return evaluation
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OpenAI response as JSON: {response}")
            raise OpenAIError(f"Invalid JSON response: {str(e)}")
    
    async def generate_feedback(
        self,
        session_data: Dict[str, Any],
        scores: List[Dict[str, Any]]
    ) -> str:
        """Generate comprehensive feedback for completed interview"""
        
        system_prompt = """You are an expert interviewer providing comprehensive feedback 
        to a candidate after an interview. Be constructive, specific, and helpful."""
        
        avg_score = sum(s["ai_score"] for s in scores) / len(scores) if scores else 0
        
        questions_summary = []
        for i, s in enumerate(scores, 1):
            questions_summary.append(
                f"Q{i}: {s['question_text'][:100]}...\n"
                f"Score: {s['ai_score']}/10\n"
                f"Feedback: {s.get('feedback', 'No specific feedback')}\n"
            )
        
        user_prompt = f"""
        Candidate Name: {session_data.get('candidate_name')}
        Job Role: {session_data.get('job_role')}
        Company: {session_data.get('company', 'N/A')}
        
        Overall Statistics:
        - Average Score: {avg_score:.1f}/10
        - Total Questions: {len(scores)}
        
        Detailed Question Analysis:
        {''.join(questions_summary)}
        
        Generate comprehensive feedback including:
        1. Overall impression
        2. Key strengths demonstrated
        3. Areas for improvement
        4. Specific recommendations for future interviews
        5. Final verdict (Strong Hire / Hire / Consider / No Hire)
        
        Be encouraging but honest. Format as a professional interview feedback."""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        return await self.generate_completion(messages)
    
    async def generate_summary(
        self,
        conversation: List[Dict[str, str]],
        scores: List[float]
    ) -> str:
        """Generate executive summary of interview"""
        
        system_prompt = """You are an executive assistant creating a brief summary 
        of an interview for hiring managers. Be concise and highlight key points."""
        
        avg_score = sum(scores) / len(scores) if scores else 0
        
        user_prompt = f"""
        Interview Conversation Summary:
        {json.dumps(conversation[-10:], indent=2)}  # Last 10 exchanges
        
        Average Score: {avg_score:.1f}/10
        
        Create a one-paragraph executive summary covering:
        - Overall performance
        - Key strengths
        - Main concerns (if any)
        - Recommendation
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        return await self.generate_completion(
            messages,
            max_tokens=300,
            temperature=0.5
        )


# Global OpenAI client instance
openai_client = OpenAIClient()