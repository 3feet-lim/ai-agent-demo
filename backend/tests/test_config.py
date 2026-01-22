"""
config.py 단위 테스트
"""
import os
from unittest.mock import patch

import pytest


class TestSettings:
    """Settings 클래스 테스트"""

    def test_default_values(self):
        """기본값 테스트"""
        with patch.dict(os.environ, {}, clear=True):
            # 캐시 초기화를 위해 새로운 Settings 인스턴스 생성
            from src.config import Settings
            settings = Settings()

            assert settings.aws_region == "ap-northeast-2"
            assert settings.bedrock_model_id == "anthropic.claude-sonnet-4-5-v2:0"
            assert settings.log_level == "INFO"
            assert settings.conversation_db_path == "./data/conversations.db"
            assert settings.mcp_server_url is None

    def test_environment_override(self):
        """환경 변수 오버라이드 테스트"""
        env_vars = {
            "AWS_REGION": "us-west-2",
            "BEDROCK_MODEL_ID": "anthropic.claude-3-haiku-20240307-v1:0",
            "LOG_LEVEL": "DEBUG",
            "MCP_SERVER_URL": "http://localhost:3000"
        }

        with patch.dict(os.environ, env_vars, clear=True):
            from src.config import Settings
            settings = Settings()

            assert settings.aws_region == "us-west-2"
            assert settings.bedrock_model_id == "anthropic.claude-3-haiku-20240307-v1:0"
            assert settings.log_level == "DEBUG"
            assert settings.mcp_server_url == "http://localhost:3000"

    def test_aws_credentials_optional(self):
        """AWS 자격 증명이 선택적인지 테스트"""
        with patch.dict(os.environ, {}, clear=True):
            from src.config import Settings
            settings = Settings()

            assert settings.aws_access_key_id is None
            assert settings.aws_secret_access_key is None

    def test_aws_credentials_from_env(self):
        """환경 변수에서 AWS 자격 증명 로드 테스트"""
        env_vars = {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        }

        with patch.dict(os.environ, env_vars, clear=True):
            from src.config import Settings
            settings = Settings()

            assert settings.aws_access_key_id == "AKIAIOSFODNN7EXAMPLE"
            assert settings.aws_secret_access_key == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class TestGetSettings:
    """get_settings 함수 테스트"""

    def test_get_settings_singleton(self):
        """싱글톤 패턴 테스트"""
        from src.config import get_settings

        # 캐시 초기화
        get_settings.cache_clear()

        settings1 = get_settings()
        settings2 = get_settings()

        assert settings1 is settings2
