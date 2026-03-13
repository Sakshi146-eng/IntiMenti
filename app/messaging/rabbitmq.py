import aio_pika
from aio_pika import Message as AMQPMessage, ExchangeType
from typing import Callable, Optional, Dict, Any
import json
import logging
import asyncio
from core.config import settings

logger = logging.getLogger(__name__)


class RabbitMQClient:
    """Production RabbitMQ client with connection management and pub/sub"""
    
    def __init__(self):
        self.connection: Optional[aio_pika.RobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self.exchange: Optional[aio_pika.Exchange] = None
        self._consumers: Dict[str, asyncio.Task] = {}
    
    async def connect(self):
        """Initialize RabbitMQ connection"""
        self.connection = await aio_pika.connect_robust(
            settings.RABBITMQ_URL,
            heartbeat=settings.RABBITMQ_HEARTBEAT
        )
        
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=10)
        
        # Declare default exchange
        self.exchange = await self.channel.declare_exchange(
            "interview_system",
            ExchangeType.TOPIC,
            durable=True
        )
        
        logger.info(f"Connected to RabbitMQ at {settings.RABBITMQ_HOST}:{settings.RABBITMQ_PORT}")
    
    async def close(self):
        """Close RabbitMQ connection"""
        # Cancel all consumers
        for task in self._consumers.values():
            task.cancel()
        self._consumers.clear()
        
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            logger.info("RabbitMQ connection closed")
    
    async def publish(self, routing_key: str, message: dict):
        """Publish message to exchange"""
        if not self.exchange:
            logger.warning("RabbitMQ not connected, skipping publish")
            return
        
        try:
            body = json.dumps(message, default=str).encode()
            await self.exchange.publish(
                AMQPMessage(
                    body=body,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                ),
                routing_key=routing_key
            )
            logger.debug(f"Published message to {routing_key}")
        except Exception as e:
            logger.error(f"Failed to publish message to {routing_key}: {e}")
    
    async def consume(self, queue_name: str, callback: Callable):
        """Consume messages from a queue"""
        if not self.channel:
            logger.warning("RabbitMQ not connected, skipping consume")
            return
        
        try:
            # Declare queue
            queue = await self.channel.declare_queue(
                queue_name,
                durable=True,
                auto_delete=False
            )
            
            # Bind queue to exchange
            await queue.bind(self.exchange, routing_key=queue_name)
            
            # Start consuming
            async def process_message(message: aio_pika.IncomingMessage):
                async with message.process(ignore_processed=True):
                    try:
                        body = json.loads(message.body.decode())
                        await callback(body)
                    except asyncio.CancelledError:
                        # A CancelledError (e.g. from LLM httpx client being cancelled)
                        # must NOT escape the context manager — it would corrupt the channel.
                        logger.warning(
                            f"Message processing was cancelled for queue '{queue_name}'. "
                            "Message will be nacked and requeued."
                        )
                        await message.nack(requeue=True)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON in message from {queue_name}")
                    except Exception as e:
                        logger.error(f"Error processing message from {queue_name}: {e}")
            
            consumer_tag = await queue.consume(process_message)
            logger.info(f"Started consuming from queue: {queue_name}")
            
        except Exception as e:
            logger.error(f"Failed to start consumer for {queue_name}: {e}")
    
    async def declare_queue(self, queue_name: str) -> Optional[aio_pika.Queue]:
        """Declare a queue"""
        if not self.channel:
            return None
        
        queue = await self.channel.declare_queue(
            queue_name,
            durable=True,
            auto_delete=False
        )
        return queue


# Global RabbitMQ client instance
rabbitmq_client = RabbitMQClient()
