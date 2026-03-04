# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import importlib.resources
import json
import os
import requests
from collections import defaultdict
from loguru import logger
from pathlib import Path
from typing import List


SERVICE_REFERENCE_URL = 'https://servicereference.us-east-1.amazonaws.com/'
METADATA_FILE = 'data/api_metadata.json'
DEFAULT_REQUEST_TIMEOUT = 5
OVERRIDES = {
    'sts': {
        'AssumeRole': False,
        'AssumeRoleWithWebIdentity': False,
        'AssumeRoleWithSAML': False,
        'GetSessionToken': False,
        'GetFederationToken': False,
        'AssumeRoot': False,
    },
    'iam': {
        'CreateAccessKey': False,
    },
    'cognito-identity': {
        'GetCredentialsForIdentity': False,
        'GetOpenIdToken': False,
    },
    'sso': {
        'GetRoleCredentials': False,
    },
}

# 로컬 캐시 디렉토리 경로
# 환경변수 AWS_API_MCP_CACHE_DIR로 지정 가능, 미지정 시 ~/.aws/aws-api-mcp/cache/
CACHE_DIR = Path(os.environ.get('AWS_API_MCP_CACHE_DIR', str(Path.home() / '.aws' / 'aws-api-mcp' / 'cache')))
# 서비스 참조 URL 목록 캐시 파일
SERVICE_REFERENCE_CACHE_FILE = CACHE_DIR / 'service_reference_urls.json'
# 서비스별 읽기 전용 작업 목록 캐시 디렉토리
SERVICE_OPERATIONS_CACHE_DIR = CACHE_DIR / 'service_operations'


def _ensure_cache_dir():
    """캐시 디렉토리가 존재하지 않으면 생성한다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_OPERATIONS_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class ServiceReferenceUrlsByService(dict):
    """Service reference urls by service."""

    def __init__(self):
        """Initialize the urls by service map.

        로컬 캐시를 우선 사용하고, 캐시가 없을 때만 외부 URL을 호출한다.
        """
        super().__init__()
        # 로컬 캐시 우선
        if self._load_cache():
            logger.info('Service reference loaded from local cache')
            return
        # 캐시가 없으면 외부 호출
        try:
            response = requests.get(SERVICE_REFERENCE_URL, timeout=DEFAULT_REQUEST_TIMEOUT).json()
            for service_reference in response:
                self[service_reference['service']] = service_reference['url']
            self._save_cache(response)
            logger.info('Service reference loaded from remote and cached locally')
        except Exception as e:
            logger.error(f'Error retrieving the service reference document: {e}')
            raise RuntimeError(f'Error retrieving the service reference document: {e}')

    def _save_cache(self, response: list):
        """외부 호출 결과를 로컬 캐시 파일에 저장한다."""
        try:
            _ensure_cache_dir()
            with open(SERVICE_REFERENCE_CACHE_FILE, 'w') as f:
                json.dump(response, f)
        except Exception as e:
            logger.warning(f'Failed to save service reference cache: {e}')

    def _load_cache(self) -> bool:
        """로컬 캐시 파일에서 서비스 참조 URL 목록을 로드한다."""
        if not SERVICE_REFERENCE_CACHE_FILE.exists():
            return False
        try:
            with open(SERVICE_REFERENCE_CACHE_FILE, 'r') as f:
                response = json.load(f)
            for service_reference in response:
                self[service_reference['service']] = service_reference['url']
            return True
        except Exception as e:
            logger.warning(f'Failed to load service reference cache: {e}')
            return False


class ReadOnlyOperations(dict):
    """Read only operations list by service."""

    def __init__(self, service_reference_urls_by_service: dict[str, str]):
        """Initialize the read only operations list."""
        super().__init__()
        self._service_reference_urls_by_service = service_reference_urls_by_service
        self._known_readonly_operations = self._get_known_readonly_operations_from_metadata()
        for service, operations in self._get_custom_readonly_operations().items():
            if service in self._known_readonly_operations:
                self._known_readonly_operations[service] = [
                    *self._known_readonly_operations[service],
                    *operations,
                ]
            else:
                self._known_readonly_operations[service] = operations

    def has(self, service, operation) -> bool:
        """Check if the operation is in the read only operations list."""
        logger.info(f'checking in read only list : {service} - {operation}')
        if service in OVERRIDES and operation in OVERRIDES[service]:
            return OVERRIDES[service][operation]
        if (
            service in self._known_readonly_operations
            and operation in self._known_readonly_operations[service]
        ):
            return True
        if service not in self:
            if service not in self._service_reference_urls_by_service:
                return False
            self._cache_ready_only_operations_for_service(service)
        return operation in self[service]

    def _cache_ready_only_operations_for_service(self, service: str):
        """서비스별 읽기 전용 작업 목록을 가져온다.

        로컬 캐시를 우선 사용하고, 캐시가 없을 때만 외부 URL을 호출한다.
        """
        cache_file = SERVICE_OPERATIONS_CACHE_DIR / f'{service}.json'

        # 로컬 캐시 우선
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    self[service] = json.load(f)
                logger.info(f'Service operations for {service} loaded from local cache')
                return
            except Exception as e:
                logger.warning(f'Failed to load service operations cache for {service}: {e}')

        # 캐시가 없으면 외부 호출
        try:
            response = requests.get(
                self._service_reference_urls_by_service[service], timeout=DEFAULT_REQUEST_TIMEOUT
            ).json()
            self[service] = []
            for action in response['Actions']:
                if not action['Annotations']['Properties']['IsWrite']:
                    self[service].append(action['Name'])
            self._save_service_cache(service, self[service])
        except Exception as e:
            logger.error(f'Error retrieving the service reference document for {service}: {e}')
            raise RuntimeError(
                f'Error retrieving the service reference document for {service}: {e}'
            )

    @staticmethod
    def _save_service_cache(service: str, operations: list):
        """서비스별 읽기 전용 작업 목록을 로컬 캐시에 저장한다."""
        try:
            _ensure_cache_dir()
            cache_file = SERVICE_OPERATIONS_CACHE_DIR / f'{service}.json'
            with open(cache_file, 'w') as f:
                json.dump(operations, f)
        except Exception as e:
            logger.warning(f'Failed to save service operations cache for {service}: {e}')

    def _get_known_readonly_operations_from_metadata(self) -> dict[str, List[str]]:
        known_readonly_operations = defaultdict(list)
        with (
            importlib.resources.files('awslabs.aws_api_mcp_server.core')
            .joinpath(METADATA_FILE)
            .open() as metadata_file
        ):
            data = json.load(metadata_file)
        for service, operations in data.items():
            for operation, operation_metadata in operations.items():
                operation_type = operation_metadata.get('type')
                if operation_type == 'ReadOnly':
                    known_readonly_operations[service].append(operation)
        return known_readonly_operations

    @staticmethod
    def _get_custom_readonly_operations() -> dict[str, List[str]]:
        return {
            's3': ['ls', 'presign'],
            'cloudfront': ['sign'],
            'cloudtrail': ['validate-logs'],
            'codeartifact': ['login'],
            'codecommit': ['credential-helper'],
            'datapipeline': ['list-runs'],
            'ecr': ['get-login', 'get-login-password'],
            'ecr-public': ['get-login-password'],
            'eks': ['get-token'],
            'emr': ['describe-cluster'],
            'gamelift': ['get-game-session-log'],
            'logs': ['start-live-tail'],
            'rds': ['generate-db-auth-token'],
            'configservice': ['get-status'],
        }


def get_read_only_operations() -> ReadOnlyOperations:
    """Get the read only operations."""
    return ReadOnlyOperations(ServiceReferenceUrlsByService())
