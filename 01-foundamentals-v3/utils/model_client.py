import os
from utils.fdyauth import AuthHelper
from dotenv import load_dotenv
from agent_framework.foundry import FoundryChatClient
from agent_framework import BaseChatClient
import logging

# Resolve .env relative to the project root (parent of utils/)
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(_PROJECT_DIR, ".env"), override=True)

settings = AuthHelper.load_settings()
az_credential = AuthHelper.test_credential()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def create_chat_client(**kwargs) -> BaseChatClient:
    """Create a FoundryChatClient using project settings."""
    model_name = kwargs.get("model_name", settings.model_deployment_name)
    return FoundryChatClient(
        project_endpoint=settings.project_endpoint,
        model=model_name,
        credential=az_credential,
    )