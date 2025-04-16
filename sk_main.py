"""
Main entry point for the Semantic Kernel A2A protocol implementation.

This module initializes and starts the A2A server with a Semantic Kernel agent.
"""
import os
import click
import logging
from dotenv import load_dotenv
from common.server import A2AServer
from common.types import AgentCard, AgentCapabilities, AgentSkill, MissingAPIKeyError
from common.utils.push_notification_auth import PushNotificationSenderAuth
from sk.agent import CurrencyAgent
from sk.task_manager import AgentTaskManager

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@click.command()
@click.option("--host", "host", default="localhost")
@click.option("--port", "port", default=10002)
def main(host, port):
    """Start the Semantic Kernel A2A protocol server.
    
    This function initializes the A2A server with a Semantic Kernel-based 
    currency conversion agent and starts it on the specified host and port.
    
    Args:
        host: The hostname to bind the server to.
        port: The port number to bind the server to.
    """
    try:
        # Check for API keys
        if not os.getenv("AZURE_OPENAI_API_KEY") and not os.getenv("OPENAI_API_KEY"):
            raise MissingAPIKeyError("Either AZURE_OPENAI_API_KEY or OPENAI_API_KEY environment variable must be set.")

        # Configure agent capabilities
        capabilities = AgentCapabilities(streaming=True, pushNotifications=True)
        
        # Define agent skills
        skill = AgentSkill(
            id="convert_currency",
            name="Currency Exchange Rates Tool",
            description="Helps with exchange values between various currencies",
            tags=["currency conversion", "currency exchange"],
            examples=["What is exchange rate between USD and GBP?"],
        )
        
        # Create the agent card
        agent_card = AgentCard(
            name="Semantic Kernel Currency Agent",
            description="Helps with exchange rates for currencies using Semantic Kernel",
            url=f"http://{host}:{port}/",
            version="1.0.0",
            defaultInputModes=CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            defaultOutputModes=CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[skill],
        )

        # Set up push notification authentication
        notification_sender_auth = PushNotificationSenderAuth()
        notification_sender_auth.generate_jwk()
        
        # Create the agent and task manager
        agent = CurrencyAgent()
        task_manager = AgentTaskManager(
            agent=agent, 
            notification_sender_auth=notification_sender_auth
        )
        
        # Initialize the A2A server
        server = A2AServer(
            agent_card=agent_card,
            task_manager=task_manager,
            host=host,
            port=port,
        )

        # Add the JWKS endpoint for push notification authentication
        server.app.add_route(
            "/.well-known/jwks.json", 
            notification_sender_auth.handle_jwks_endpoint, 
            methods=["GET"]
        )

        # Start the server
        logger.info(f"Starting Semantic Kernel A2A server on {host}:{port}")
        server.start()
        
    except MissingAPIKeyError as e:
        logger.error(f"Error: {e}")
        exit(1)
    except Exception as e:
        logger.error(f"An error occurred during server startup: {e}")
        exit(1)


if __name__ == "__main__":
    main()