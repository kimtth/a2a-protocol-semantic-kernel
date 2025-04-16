"""
Task manager for Semantic Kernel agent implementation of the A2A protocol.

This module connects a Semantic Kernel agent to the A2A protocol server.
"""

import asyncio
import logging
import traceback
from typing import AsyncIterable, Union

from common.server.task_manager import InMemoryTaskManager
from common.types import (
    Artifact,
    InternalError,
    InvalidParamsError,
    JSONRPCResponse,
    Message,
    PushNotificationConfig,
    SendTaskRequest,
    SendTaskResponse,
    SendTaskStreamingRequest,
    SendTaskStreamingResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from common.utils.push_notification_auth import PushNotificationSenderAuth
import common.server.utils as utils
from sk.agent import CurrencyAgent

logger = logging.getLogger(__name__)


class AgentTaskManager(InMemoryTaskManager):
    """Task manager for Semantic Kernel agent implementation of the A2A protocol."""

    def __init__(self, agent: CurrencyAgent, notification_sender_auth: PushNotificationSenderAuth = None):
        """Initialize the agent task manager.
        
        Args:
            agent: The Semantic Kernel agent to use for processing tasks.
            notification_sender_auth: Authentication for push notifications.
        """
        super().__init__()
        self.agent = agent
        self.notification_sender_auth = notification_sender_auth

    def _validate_request(
        self, request: Union[SendTaskRequest, SendTaskStreamingRequest]
    ) -> JSONRPCResponse | None:
        """Validate an incoming request.
        
        Args:
            request: The request to validate.
            
        Returns:
            A JSONRPCResponse if the request is invalid, None if it's valid.
        """
        task_send_params: TaskSendParams = request.params
        if not utils.are_modalities_compatible(
            task_send_params.acceptedOutputModes, CurrencyAgent.SUPPORTED_CONTENT_TYPES
        ):
            logger.warning(
                "Unsupported output mode. Received %s, Support %s",
                task_send_params.acceptedOutputModes,
                CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            )
            return utils.new_incompatible_types_error(request.id)
        
        if task_send_params.pushNotification and not task_send_params.pushNotification.url:
            logger.warning("Push notification URL is missing")
            return JSONRPCResponse(id=request.id, error=InvalidParamsError(message="Push notification URL is missing"))
        
        return None

    async def on_send_task(self, request: SendTaskRequest) -> SendTaskResponse:
        """Handle a send task request.
        
        Args:
            request: The request to handle.
            
        Returns:
            A response to the request.
        """
        validation_error = self._validate_request(request)
        if validation_error:
            return SendTaskResponse(id=request.id, error=validation_error.error)
        
        # Handle push notifications if configured
        if request.params.pushNotification and self.notification_sender_auth:
            if not await self.set_push_notification_info(request.params.id, request.params.pushNotification):
                return SendTaskResponse(id=request.id, error=InvalidParamsError(message="Push notification URL is invalid"))

        # Create or update the task
        await self.upsert_task(request.params)
        task = await self.update_store(
            request.params.id, TaskStatus(state=TaskState.WORKING), None
        )
        
        # Send notification if configured
        if self.notification_sender_auth:
            await self.send_task_notification(task)

        # Process the query with the agent
        task_send_params: TaskSendParams = request.params
        query = self._get_user_query(task_send_params)
        try:
            agent_response = await self.agent.invoke(query, task_send_params.sessionId)
        except Exception as e:
            logger.error(f"Error invoking agent: {e}")
            return SendTaskResponse(
                id=request.id, 
                error=InternalError(message=f"Error invoking agent: {str(e)}")
            )
            
        return await self._process_agent_response(request, agent_response)

    async def on_send_task_subscribe(
        self, request: SendTaskStreamingRequest
    ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
        """Handle a streaming task request.
        
        Args:
            request: The streaming request to handle.
            
        Returns:
            A streaming response or an error response.
        """
        try:
            error = self._validate_request(request)
            if error:
                return error

            await self.upsert_task(request.params)

            if request.params.pushNotification and self.notification_sender_auth:
                if not await self.set_push_notification_info(request.params.id, request.params.pushNotification):
                    return JSONRPCResponse(id=request.id, error=InvalidParamsError(message="Push notification URL is invalid"))

            task_send_params: TaskSendParams = request.params
            sse_event_queue = await self.setup_sse_consumer(task_send_params.id, False)            

            asyncio.create_task(self._run_streaming_agent(request))

            return self.dequeue_events_for_sse(
                request.id, task_send_params.id, sse_event_queue
            )
        except Exception as e:
            logger.error(f"Error in SSE stream: {e}")
            print(traceback.format_exc())
            return JSONRPCResponse(
                id=request.id,
                error=InternalError(
                    message="An error occurred while streaming the response"
                ),
            )

    async def _run_streaming_agent(self, request: SendTaskStreamingRequest):
        """Run the agent in streaming mode for a request.
        
        Args:
            request: The streaming request to process.
        """
        task_send_params: TaskSendParams = request.params
        query = self._get_user_query(task_send_params)

        try:
            async for item in self.agent.stream(query, task_send_params.sessionId):
                is_task_complete = item["is_task_complete"]
                require_user_input = item["require_user_input"]
                artifact = None
                message = None
                parts = [TextPart(type="text", text=item["content"])]
                end_stream = False

                if not is_task_complete and not require_user_input:
                    task_state = TaskState.WORKING
                    message = Message(role="agent", parts=parts)
                elif require_user_input:
                    task_state = TaskState.INPUT_REQUIRED
                    message = Message(role="agent", parts=parts)
                    end_stream = True
                else:
                    task_state = TaskState.COMPLETED
                    artifact = Artifact(parts=parts, index=0, append=False)
                    end_stream = True

                task_status = TaskStatus(state=task_state, message=message)
                latest_task = await self.update_store(
                    task_send_params.id,
                    task_status,
                    None if artifact is None else [artifact],
                )
                
                # Send notification if configured
                if self.notification_sender_auth:
                    await self.send_task_notification(latest_task)

                if artifact:
                    task_artifact_update_event = TaskArtifactUpdateEvent(
                        id=task_send_params.id, artifact=artifact
                    )
                    await self.enqueue_events_for_sse(
                        task_send_params.id, task_artifact_update_event
                    )                    
                    
                task_update_event = TaskStatusUpdateEvent(
                    id=task_send_params.id, status=task_status, final=end_stream
                )
                await self.enqueue_events_for_sse(
                    task_send_params.id, task_update_event
                )

        except Exception as e:
            logger.error(f"An error occurred while streaming the response: {e}")
            await self.enqueue_events_for_sse(
                task_send_params.id,
                InternalError(message=f"An error occurred while streaming the response: {e}")                
            )

    async def _process_agent_response(
        self, request: SendTaskRequest, agent_response: dict
    ) -> SendTaskResponse:
        """Process the agent's response and update the task store.
        
        Args:
            request: The original request.
            agent_response: The response from the agent.
            
        Returns:
            A response to send to the client.
        """
        task_send_params: TaskSendParams = request.params
        task_id = task_send_params.id
        history_length = task_send_params.historyLength
        task_status = None

        parts = [TextPart(type="text", text=agent_response["content"])]
        artifact = None
        if agent_response["require_user_input"]:
            task_status = TaskStatus(
                state=TaskState.INPUT_REQUIRED,
                message=Message(role="agent", parts=parts),
            )
        else:
            task_status = TaskStatus(state=TaskState.COMPLETED)
            artifact = Artifact(parts=parts)
            
        task = await self.update_store(
            task_id, task_status, None if artifact is None else [artifact]
        )
        task_result = self.append_task_history(task, history_length)
        
        # Send notification if configured
        if self.notification_sender_auth:
            await self.send_task_notification(task)
            
        return SendTaskResponse(id=request.id, result=task_result)
    
    def _get_user_query(self, task_send_params: TaskSendParams) -> str:
        """Extract the user query from the task parameters.
        
        Args:
            task_send_params: The task parameters.
            
        Returns:
            The user query as a string.
            
        Raises:
            ValueError: If the task message doesn't contain a text part.
        """
        part = task_send_params.message.parts[0]
        if not isinstance(part, TextPart):
            raise ValueError("Only text parts are supported")
        return part.text
    
    async def send_task_notification(self, task: Task):
        """Send a notification for a task update.
        
        Args:
            task: The updated task.
        """
        if not self.notification_sender_auth:
            return
            
        if not await self.has_push_notification_info(task.id):
            logger.info(f"No push notification info found for task {task.id}")
            return
            
        push_info = await self.get_push_notification_info(task.id)

        logger.info(f"Notifying for task {task.id} => {task.status.state}")
        await self.notification_sender_auth.send_push_notification(
            push_info.url,
            data=task.model_dump(exclude_none=True)
        )

    async def on_resubscribe_to_task(
        self, request: TaskIdParams
    ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
        """Handle a request to resubscribe to a task's events.
        
        Args:
            request: The resubscription request.
            
        Returns:
            A streaming response or an error response.
        """
        task_id_params: TaskIdParams = request.params
        try:
            sse_event_queue = await self.setup_sse_consumer(task_id_params.id, True)
            return self.dequeue_events_for_sse(request.id, task_id_params.id, sse_event_queue)
        except Exception as e:
            logger.error(f"Error while reconnecting to SSE stream: {e}")
            return JSONRPCResponse(
                id=request.id,
                error=InternalError(
                    message=f"An error occurred while reconnecting to stream: {e}"
                ),
            )
    
    async def set_push_notification_info(self, task_id: str, push_notification_config: PushNotificationConfig):
        """Set push notification information for a task.
        
        Args:
            task_id: The ID of the task.
            push_notification_config: The push notification configuration.
            
        Returns:
            True if successful, False otherwise.
        """
        if not self.notification_sender_auth:
            return False
            
        # Verify the ownership of notification URL by issuing a challenge request.
        is_verified = await self.notification_sender_auth.verify_push_notification_url(push_notification_config.url)
        if not is_verified:
            return False
        
        await super().set_push_notification_info(task_id, push_notification_config)
        return True