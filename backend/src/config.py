"""
환경 설정 관리
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """애플리케이션 설정"""

    # AWS 설정
    aws_region: str = "ap-northeast-2"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    # Bedrock 모델 설정
    bedrock_model_id: str = "anthropic.claude-sonnet-4-5-v2:0"
    # Sub-agent용 모델 (데이터 수집용, 미설정 시 bedrock_model_id 사용)
    bedrock_sub_agent_model_id: str | None = None

    # 애플리케이션 설정
    log_level: str = "INFO"
    conversation_db_path: str = "./data/conversations.db"

    # MCP 설정
    mcp_server_url: str | None = None

    # Grafana MCP 설정
    grafana_url: str | None = None
    grafana_service_account_token: str | None = None  # Grafana Service Account Token
    grafana_mcp_url: str | None = None  # Grafana MCP 서버 URL (docker-compose 서비스명)

    # CloudWatch MCP 설정 (AWS 자격 증명은 위에서 관리)
    aws_profile: str | None = None
    cloudwatch_mcp_url: str | None = None  # CloudWatch MCP 서버 URL
    aws_api_mcp_url: str | None = None  # AWS API MCP 서버 URL
    aws_docs_mcp_url: str | None = None  # AWS Documentation MCP 서버 URL

    # Slack 설정
    slack_webhook_url: str | None = None  # Slack Incoming Webhook URL
    slack_channel: str | None = None  # Slack 채널 (선택, webhook 기본 채널 사용 시 불필요)

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),  # 프로젝트 루트(..) 우선, 로컬(.env) fallback
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,  # 환경변수 대소문자 구분 안함
    )


@lru_cache()
def get_settings() -> Settings:
    """설정 싱글톤 반환"""
    return Settings()
