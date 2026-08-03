from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

# DOTENV = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
# print(f"DOTENV file path: {DOTENV}")

class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    All settings are validated through Pydantic's type system.
    """
    subscription_id: str = Field(..., env="SUBSCRIPTION_ID")
    resource_group: str = Field(..., env="RESOURCE_GROUP")
    workspace: str = Field(..., env="WORKSPACE")
    ssh_pub_key_name: str = Field(..., env="SSH_PUB_KEY_NAME")
    # Optional: HuggingFace Hub token for non-throttled downloads and gated
    # models. Empty string = anonymous (slow). Loaded from HF_TOKEN env var.
    hf_token: str = Field(default="", env="HF_TOKEN")

    class Config:
        """Pydantic model configuration"""
        # env_file = f"{DOTENV}"
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False # variable names are lowcase in the Settings

class AuthHelper:
    @classmethod
    def load_settings(cls):
        try:
            return Settings()
        except ValidationError as e:
            missing = [err['loc'][0] for err in e.errors()]
            raise EnvironmentError(f"Missing required env keys: {missing}")

    @staticmethod
    def test_credential():
        """
        Test Azure authentication and return a credential object.
        Falls back to InteractiveBrowserCredential if DefaultAzureCredential fails.
        """
        credential = None
        try:
            credential = DefaultAzureCredential()
            # credential.get_token("https://management.azure.com/.default")
            # print("DefaultAzureCredential authentication OK")
        except Exception:
            credential = InteractiveBrowserCredential()
            # print("Falling back to InteractiveBrowserCredential")
        return credential