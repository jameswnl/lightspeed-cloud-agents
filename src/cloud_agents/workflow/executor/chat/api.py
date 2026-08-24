"""FastAPI routes for the ChatWorkflowRunner.

Provides REST API endpoints for creating conversations, sending messages,
and retrieving conversation history. Routes are built via build_chat_router()
and mounted by the application startup.

No temporalio imports.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def build_chat_router(runner: Any, get_caller_identity: Any = None) -> APIRouter:
    """Build a FastAPI router for chat conversation endpoints.

    Parameters:
        runner: ChatWorkflowRunner instance.
        get_caller_identity: Optional callable to extract caller identity
            from the request. Not used yet (placeholder for auth integration).

    Returns:
        APIRouter with /chat, /chat/{id}/message, /chat/{id}/history routes.
    """
    router = APIRouter()

    @router.post("/chat")
    async def create_conversation(request: Request) -> dict[str, Any]:
        """Create a new conversation.

        Accepts optional user_id, session_id, and workflow_id in the
        request body. Returns the conversation ID.

        Parameters:
            request: FastAPI Request object.

        Returns:
            Dict with conversation_id.
        """
        body = await request.json()
        conv_id = await runner.start(body)
        return {"conversation_id": conv_id}

    @router.post("/chat/{conversation_id}/message")
    async def send_message(conversation_id: str, request: Request) -> Any:
        """Send a message to a conversation and get the response.

        Parameters:
            conversation_id: Target conversation/workflow ID.
            request: FastAPI Request object with prompt field.

        Returns:
            Dict with status, output, and optional error.
        """
        body = await request.json()
        prompt = body.get("prompt")
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={"error": "prompt is required"},
            )

        try:
            result = await runner.send_message(conversation_id, prompt)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": str(exc)},
            )

        return {
            "status": result.status,
            "output": result.output,
            "error": result.error,
        }

    @router.get("/chat/{conversation_id}/history")
    async def get_history(
        conversation_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Get conversation message history.

        Parameters:
            conversation_id: Target conversation/workflow ID.
            limit: Maximum number of turns to load (query param).

        Returns:
            Dict with messages list.
        """
        messages = await runner.get_history(conversation_id, limit=limit)
        return {"messages": [m.to_dict() for m in messages]}

    return router
