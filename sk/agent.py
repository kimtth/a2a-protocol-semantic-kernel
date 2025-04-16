"""
Semantic Kernel implementation of a currency conversion agent for the A2A protocol.

This agent uses Semantic Kernel to provide currency exchange rate information.
"""

import logging
import os
import httpx
import sk
import semantic_kernel
from typing import Any, Dict, AsyncIterable, Final, List, Optional, Annotated
from pydantic import BaseModel, Field
from uuid import uuid4
from dotenv import load_dotenv
from semantic_kernel.connectors.ai.open_ai import (
    OpenAIChatCompletion,
    AzureChatCompletion,
)
from semantic_kernel import Kernel
from semantic_kernel.contents.chat_history import ChatHistory
from semantic_kernel.functions.kernel_arguments import KernelArguments
from semantic_kernel.functions.kernel_function_decorator import kernel_function
from semantic_kernel.connectors.ai.chat_completion_client_base import (
    ChatCompletionClientBase,
)
from semantic_kernel.connectors.ai.function_choice_behavior import (
    FunctionChoiceBehavior,
)
from semantic_kernel.connectors.ai.open_ai.prompt_execution_settings.azure_chat_prompt_execution_settings import (
    AzureChatPromptExecutionSettings,
)

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

CONST_SERVICE_ID: Final = "chat-completion"



class ExchangeRateResult(BaseModel):
    base: str
    date: str
    rates: Dict[str, float]


class CurrencyAgent:
    """Currency conversion agent powered by Semantic Kernel.

    This agent provides currency exchange rate information using the
    Frankfurter API and Semantic Kernel for orchestration.
    """
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    SYSTEM_INSTRUCTION = """You are a specialized assistant for currency conversions. 
Your sole purpose is to use the exchange rate tool to answer questions about currency exchange rates.
If the user asks about anything other than currency conversion or exchange rates, 
politely state that you cannot help with that topic and can only assist with currency-related queries.
Do not attempt to answer unrelated questions or use tools for other purposes.

Here are some examples of questions you can answer:
- What is the exchange rate from USD to EUR?
- How much is 100 USD in JPY?
- What is the exchange rate for converting Canadian dollars to British pounds?

For each question, determine if:
1. You have all the information needed to call the exchange rate tool
2. You need to ask the user for more information

If you need more information, ask the user specific questions.
If you have all the information, call the exchange rate tool and respond with the result."""

    def __init__(self):
        """Initialize the currency conversion agent."""
        # Initialize the Semantic Kernel
        self.kernel = Kernel()

        # Set up AI service
        deployment_name = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-35-turbo")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")

        if api_key and endpoint:
            # Azure OpenAI setup
            self.kernel.add_service(
                AzureChatCompletion(
                    service_id=CONST_SERVICE_ID,
                    deployment_name=deployment_name,
                    endpoint=endpoint,
                    api_key=api_key,
                )
            )
        else:
            # Fallback to OpenAI (if OPENAI_API_KEY is set)
            openai_api_key = os.getenv("OPENAI_API_KEY")
            if not openai_api_key:
                raise ValueError(
                    "Either AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT or OPENAI_API_KEY must be set"
                )

            self.kernel.add_service(
                OpenAIChatCompletion(
                    service_id=CONST_SERVICE_ID,
                    ai_model_id=deployment_name,
                    api_key=openai_api_key,
                )
            )

        # Add the function to the kernel - using the decorated method directly
        self.kernel.add_plugin(ExchangeRatePlugin(), "ExchangeRatePlugin")

        # Initialize conversation memory storage
        self.conversations = dict()

    async def invoke(self, query: str, session_id: str) -> Dict[str, Any]:
        """Process a user query about currency conversion.

        Args:
            query: The user's question or request about currency conversion.
            session_id: A unique identifier for the user's session.

        Returns:
            A dictionary containing the response and status information.
        """
        # Check if the session ID exists in conversations, if not create a new one
        if session_id not in self.conversations:
            self.conversations[session_id] = ChatHistory(
                system_message=self.SYSTEM_INSTRUCTION
            )

        # Reference to the function in the plugin
        # https://learn.microsoft.com/en-us/semantic-kernel/concepts/plugins/?pivots=programming-language-python
        # python/samples/concepts/auto_function_calling/azure_python_code_interpreter_function_calling.py
        req_settings = AzureChatPromptExecutionSettings(service_id=CONST_SERVICE_ID)
        req_settings.function_choice_behavior = FunctionChoiceBehavior.Auto(
            filters={"excluded_plugins": []}
        )
        answer_srv = self.kernel.get_service(CONST_SERVICE_ID)
        answer = await answer_srv.get_chat_message_content(
            chat_history=self.conversations[session_id],
            settings=req_settings,
            kernel=self.kernel,
        )

        answer_str = str(answer)
        self.conversations[session_id].add_user_message(query)
        self.conversations[session_id].add_assistant_message(answer_str)

        # Check if the response indicates the need for more information
        if any(
            phrase in answer_str.lower()
            for phrase in [
                "which currency",
                "what currency",
                "specify",
                "need more information",
            ]
        ):
            return {
                "is_task_complete": False,
                "require_user_input": True,
                "content": answer_str,
            }
        else:
            return {
                "is_task_complete": True,
                "require_user_input": False,
                "content": answer_str,
            }

    async def stream(
        self, query: str, session_id: str
    ) -> AsyncIterable[Dict[str, Any]]:
        """Stream a response to a currency conversion query.

        Args:
            query: The user's question or request about currency conversion.
            session_id: A unique identifier for the user's session.

        Yields:
            Dictionaries containing update information during processing.
        """
        # Simulate a streaming response with interim updates
        yield {
            "is_task_complete": False,
            "require_user_input": False,
            "content": "Looking up the exchange rates...",
        }

        # Process the query to get the actual response
        response = await self.invoke(query, session_id)

        # Yield an intermediate processing update
        yield {
            "is_task_complete": False,
            "require_user_input": False,
            "content": "Processing the exchange rates...",
        }

        # Finally yield the complete response
        yield response


class ExchangeRatePlugin:
    """Plugin for handling exchange rate queries."""

    @kernel_function(
        description="Get the exchange rate between two currencies",
        name="get_exchange_rate",
    )
    async def get_exchange_rate(
        self,
        currency_from: Annotated[
            str, "The currency to convert from (e.g., 'USD')"
        ] = "USD",
        currency_to: Annotated[str, "The currency to convert to (e.g., 'EUR')"] = "EUR",
        currency_date: Annotated[
            str, "The date for the exchange rate or 'latest'"
        ] = "latest",
    ) -> str:
        """Get the exchange rate between two currencies.

        Args:
            currency_from: The currency to convert from (e.g., "USD").
            currency_to: The currency to convert to (e.g., "EUR").
            currency_date: The date for the exchange rate or "latest". Defaults to "latest".

        Returns:
            A JSON string containing the exchange rate data or an error message.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://api.frankfurter.app/{currency_date}",
                    params={"from": currency_from, "to": currency_to},
                )
                response.raise_for_status()

                data = response.json()
                if "rates" not in data:
                    return '{"error": "Invalid API response format."}'

                # Convert to a nice string format
                result = ExchangeRateResult(**data)
                rate = result.rates[currency_to]
                return f"The exchange rate from {currency_from} to {currency_to} is {rate}."

        except httpx.HTTPError as e:
            return f'{{"error": "API request failed: {str(e)}"}}'
        except ValueError as e:
            return '{"error": "Invalid JSON response from API."}'
