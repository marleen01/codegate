import asyncio
from typing import List, AsyncGenerator

import structlog
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from codegate.dashboard.post_processing import (
    parse_get_alert_conversation,
    parse_messages_in_conversations,
)
from codegate.dashboard.request_models import AlertConversation, Conversation
from codegate.db.connection import DbReader, alert_queue

logger = structlog.get_logger("codegate")

dashboard_router = APIRouter(tags=["Dashboard"])
db_reader = DbReader()


@dashboard_router.get("/dashboard/messages")
def get_messages() -> List[Conversation]:
    """
    Get all the messages from the database and return them as a list of conversations.
    """
    prompts_outputs = asyncio.run(db_reader.get_prompts_with_output())

    return asyncio.run(parse_messages_in_conversations(prompts_outputs))


@dashboard_router.get("/dashboard/alerts")
def get_alerts() -> List[AlertConversation]:
    """
    Get all the messages from the database and return them as a list of conversations.
    """
    alerts_prompt_output = asyncio.run(db_reader.get_alerts_with_prompt_and_output())
    return asyncio.run(parse_get_alert_conversation(alerts_prompt_output))


async def generate_sse_events() -> AsyncGenerator[str, None]:
    """
    SSE generator from queue
    """
    while True:
        message = await alert_queue.get()
        yield f"data: {message}\n\n"

@dashboard_router.get("/dashboard/alerts_notification")
async def stream_sse():
    """
    Send alerts event
    """
    return StreamingResponse(generate_sse_events(), media_type="text/event-stream")
