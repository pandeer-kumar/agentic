from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    azure_openai_endpoint: str = "http://127.0.0.1:8900"
    azure_openai_api_key: str = "mock-key"
    azure_openai_deployment: str = "mock-gpt"
    azure_openai_api_version: str = "2024-10-21"

    database_url: str = "postgresql://agentic:agentic_dev@127.0.0.1:5433/agentic"

    config_dir: Path = ROOT / "config"
    data_dir: Path = ROOT / "data"

    agent_max_steps: int = 12
    max_records_per_fetch: int = 5000
    max_tool_result_chars: int = 12000
    mcp_timeout_seconds: float = 60
    sql_timeout_seconds: float = 20
    host: str = "127.0.0.1"
    port: int = 8000
