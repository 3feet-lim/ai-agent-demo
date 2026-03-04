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

"""CloudWatch MCP Server 설정 모듈."""

import os
from typing import Literal, cast


def get_transport_from_env() -> Literal['stdio', 'streamable-http']:
    """환경변수에서 transport 값을 가져옵니다. 기본값은 stdio입니다."""
    transport = os.getenv('CLOUDWATCH_MCP_TRANSPORT', 'stdio')
    if transport not in ['stdio', 'streamable-http']:
        raise ValueError(f'Invalid transport: {transport}')

    return cast(Literal['stdio', 'streamable-http'], transport)


# Transport 설정
TRANSPORT = get_transport_from_env()

# HTTP 서버 설정 (streamable-http 사용 시)
HOST = os.getenv('CLOUDWATCH_MCP_HOST', '0.0.0.0')
PORT = int(os.getenv('CLOUDWATCH_MCP_PORT', '8000'))
