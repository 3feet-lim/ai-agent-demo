"""
Account Profile Resolver

알람 메시지에서 키워드를 매칭하여 적절한 AWS profile을 결정하는 모듈.
메타데이터 파일(account_profiles.json)을 기반으로 동작하며,
매 도구 호출 시점에 동적으로 profile을 결정합니다.
"""

import json
from loguru import logger
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AccountInfo:
    """AWS 계정 정보"""
    account_id: str
    profile: str
    role_arn: Optional[str] = None
    alias: str = ""
    description: str = ""
    matchers: list[str] = field(default_factory=list)
    # 컴파일된 정규식 패턴 (초기화 시 생성)
    _compiled_patterns: list[re.Pattern] = field(default_factory=list, repr=False)

    def __post_init__(self):
        """matcher 문자열을 정규식 패턴으로 컴파일"""
        self._compiled_patterns = []
        for matcher in self.matchers:
            try:
                # 대소문자 무시, 유니코드(한글) 지원
                pattern = re.compile(re.escape(matcher), re.IGNORECASE | re.UNICODE)
                self._compiled_patterns.append(pattern)
            except re.error as e:
                logger.warning(f"잘못된 matcher 패턴 무시: {matcher} ({e})")

    def matches(self, text: str) -> Optional[str]:
        """
        텍스트에서 매칭되는 패턴을 찾아 반환.
        매칭되면 해당 matcher 문자열을, 아니면 None 반환.
        """
        for i, pattern in enumerate(self._compiled_patterns):
            if pattern.search(text):
                return self.matchers[i]
        return None


class AccountProfileResolver:
    """
    알람 메시지 기반 AWS profile 결정기.

    account_profiles.json 파일을 로드하여 메시지 내용에서
    키워드를 매칭하고, 해당 계정의 AWS profile을 반환합니다.
    """

    def __init__(self, config_path: Optional[str] = None):
        self._accounts: list[AccountInfo] = []
        self._default_profile: Optional[str] = None
        self._config_path = config_path or str(
            Path(__file__).parent.parent / "config" / "account_profiles.json"
        )
        self._load_config()

    def _load_config(self):
        """메타데이터 파일 로드"""
        config_file = Path(self._config_path)
        if not config_file.exists():
            logger.warning(f"Account profiles 설정 파일 없음: {self._config_path}")
            return

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._default_profile = data.get("default_profile", None)
            self._accounts = []

            for acc in data.get("accounts", []):
                self._accounts.append(AccountInfo(
                    account_id=acc.get("account_id", ""),
                    profile=acc.get("profile", ""),
                    role_arn=acc.get("role_arn"),
                    alias=acc.get("alias", ""),
                    description=acc.get("description", ""),
                    matchers=acc.get("matchers", []),
                ))

            logger.info(
                f"Account profiles 로드 완료: {len(self._accounts)}개 계정, "
                f"기본 profile: {self._default_profile}"
            )
        except Exception as e:
            logger.error(f"Account profiles 로드 실패: {e}")

    def reload(self):
        """설정 파일 다시 로드 (런타임 중 변경 반영)"""
        self._load_config()

    def resolve(self, message: str) -> Optional[str]:
        """
        메시지에서 계정을 식별하고 해당 AWS profile을 반환.

        Args:
            message: 알람 메시지 또는 사용자 입력 텍스트

        Returns:
            매칭된 AWS profile 이름. 매칭 실패 시 default_profile 반환 (None 가능).
        """
        if not message or not self._accounts:
            return self._default_profile

        for account in self._accounts:
            matched = account.matches(message)
            if matched:
                logger.info(
                    f"[Profile 결정] 매칭: '{matched}' → "
                    f"계정: {account.alias}({account.account_id}), "
                    f"profile: {account.profile}"
                )
                return account.profile

        logger.info(f"[Profile 결정] 매칭 없음 → 기본 profile: {self._default_profile}")
        return self._default_profile

    def resolve_account(self, message: str) -> Optional[AccountInfo]:
        """
        메시지에서 계정 정보 전체를 반환.
        매칭 실패 시 None 반환.
        """
        if not message or not self._accounts:
            return None

        for account in self._accounts:
            if account.matches(message):
                return account
        return None

    def find_by_profile(self, profile: str) -> Optional[AccountInfo]:
        """profile 이름으로 계정 정보를 직접 조회."""
        for account in self._accounts:
            if account.profile == profile:
                return account
        return None

    def get_known_aliases(self) -> list[str]:
        """프롬프트 주입용: 모든 계정의 alias + matchers 목록 반환.

        LLM이 계정 별칭을 리소스 이름으로 오인하지 않도록,
        알려진 계정 참조 문자열을 모두 반환한다.
        """
        aliases = []
        for account in self._accounts:
            if account.alias:
                aliases.append(account.alias)
            aliases.extend(account.matchers)
        return aliases

    @property
    def default_profile(self) -> Optional[str]:
        return self._default_profile

    @property
    def accounts(self) -> list[AccountInfo]:
        return self._accounts
