/**
 * AI Agent Demo - 채팅 애플리케이션
 * Vanilla JS로 구현된 채팅 UI
 */

const API_BASE = '/api';

// DOM 요소
const messagesContainer = document.getElementById('messages-container');
const welcomeMessage = document.getElementById('welcome-message');
const messageInput = document.getElementById('message-input');
const sendBtn = document.getElementById('send-btn');
const newChatBtn = document.getElementById('new-chat-btn');
const conversationList = document.getElementById('conversation-list');

// 상태
let currentConversationId = null;
let isLoading = false;

// 초기화
document.addEventListener('DOMContentLoaded', () => {
    initEventListeners();
    loadConversations();
});

// 이벤트 리스너 설정
function initEventListeners() {
    // 전송 버튼 클릭
    sendBtn.addEventListener('click', sendMessage);

    // 입력창 이벤트
    messageInput.addEventListener('input', handleInputChange);
    messageInput.addEventListener('keydown', handleKeyDown);

    // 새 대화 버튼
    newChatBtn.addEventListener('click', startNewConversation);
}

// 입력값 변경 핸들러
function handleInputChange() {
    // 버튼 활성화/비활성화
    sendBtn.disabled = messageInput.value.trim() === '' || isLoading;

    // textarea 높이 자동 조절
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 150) + 'px';
}

// 키보드 이벤트 핸들러
function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!sendBtn.disabled) {
            sendMessage();
        }
    }
}

// 메시지 전송
async function sendMessage() {
    const content = messageInput.value.trim();
    if (!content || isLoading) return;

    // 환영 메시지 숨기기
    if (welcomeMessage) {
        welcomeMessage.style.display = 'none';
    }

    // 사용자 메시지 표시
    appendMessage('user', content);

    // 입력창 초기화
    messageInput.value = '';
    messageInput.style.height = 'auto';
    sendBtn.disabled = true;

    // 로딩 상태
    isLoading = true;
    const loadingEl = showLoading();

    try {
        const response = await fetch(`${API_BASE}/chat`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                message: content,
                conversation_id: currentConversationId,
            }),
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        // 대화 ID 저장
        if (data.conversation_id) {
            currentConversationId = data.conversation_id;
        }

        // 로딩 제거 후 응답 표시
        removeLoading(loadingEl);
        appendMessage('assistant', data.response);

        // 대화 목록 갱신
        loadConversations();

    } catch (error) {
        console.error('Error sending message:', error);
        removeLoading(loadingEl);
        showError('메시지 전송에 실패했습니다. 다시 시도해주세요.');
    } finally {
        isLoading = false;
        handleInputChange();
    }
}

// 메시지 추가
function appendMessage(role, content) {
    const messageEl = document.createElement('div');
    messageEl.className = `message ${role}`;

    const avatarEl = document.createElement('div');
    avatarEl.className = 'message-avatar';
    avatarEl.textContent = role === 'user' ? 'U' : 'AI';

    const contentEl = document.createElement('div');
    contentEl.className = 'message-content';
    contentEl.innerHTML = formatMessage(content);

    messageEl.appendChild(avatarEl);
    messageEl.appendChild(contentEl);

    messagesContainer.appendChild(messageEl);
    scrollToBottom();
}

// 메시지 포맷팅 (마크다운 기본 지원)
function formatMessage(content) {
    // XSS 방지를 위한 이스케이프
    let escaped = content
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    // 코드 블록 처리 (```...```)
    escaped = escaped.replace(/```(\w*)\n?([\s\S]*?)```/g, (match, lang, code) => {
        return `<pre><code class="language-${lang}">${code.trim()}</code></pre>`;
    });

    // 인라인 코드 처리 (`...`)
    escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');

    // 줄바꿈 처리
    escaped = escaped.replace(/\n/g, '<br>');

    // 단락으로 감싸기
    return `<p>${escaped}</p>`;
}

// 로딩 인디케이터 표시
function showLoading() {
    const loadingEl = document.createElement('div');
    loadingEl.className = 'message assistant';
    loadingEl.innerHTML = `
        <div class="message-avatar">AI</div>
        <div class="message-content">
            <div class="typing-indicator">
                <span></span>
                <span></span>
                <span></span>
            </div>
        </div>
    `;
    messagesContainer.appendChild(loadingEl);
    scrollToBottom();
    return loadingEl;
}

// 로딩 인디케이터 제거
function removeLoading(loadingEl) {
    if (loadingEl && loadingEl.parentNode) {
        loadingEl.parentNode.removeChild(loadingEl);
    }
}

// 에러 메시지 표시
function showError(message) {
    const errorEl = document.createElement('div');
    errorEl.className = 'error-message';
    errorEl.textContent = message;
    messagesContainer.appendChild(errorEl);
    scrollToBottom();

    // 5초 후 자동 제거
    setTimeout(() => {
        if (errorEl.parentNode) {
            errorEl.parentNode.removeChild(errorEl);
        }
    }, 5000);
}

// 스크롤 하단으로 이동
function scrollToBottom() {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// 새 대화 시작
function startNewConversation() {
    currentConversationId = null;
    messagesContainer.innerHTML = '';

    // 환영 메시지 다시 표시
    const welcomeEl = document.createElement('div');
    welcomeEl.className = 'welcome-message';
    welcomeEl.id = 'welcome-message';
    welcomeEl.innerHTML = `
        <h2>무엇을 도와드릴까요?</h2>
        <p>질문을 입력하시면 AI가 답변해 드립니다.</p>
    `;
    messagesContainer.appendChild(welcomeEl);

    // 대화 목록에서 active 제거
    document.querySelectorAll('.conversation-item').forEach(item => {
        item.classList.remove('active');
    });

    messageInput.focus();
}

// 대화 목록 로드
async function loadConversations() {
    try {
        const response = await fetch(`${API_BASE}/conversations`);
        if (!response.ok) return;

        const conversations = await response.json();
        renderConversationList(conversations);
    } catch (error) {
        console.error('Error loading conversations:', error);
    }
}

// 대화 목록 렌더링
function renderConversationList(conversations) {
    conversationList.innerHTML = '';

    conversations.forEach(conv => {
        const itemEl = document.createElement('div');
        itemEl.className = 'conversation-item';
        if (conv.id === currentConversationId) {
            itemEl.classList.add('active');
        }

        // 제목이 없으면 첫 메시지 일부 사용
        const title = conv.title || conv.preview || '새 대화';
        itemEl.textContent = title;
        itemEl.title = title;

        itemEl.addEventListener('click', () => loadConversation(conv.id));

        conversationList.appendChild(itemEl);
    });
}

// 특정 대화 로드
async function loadConversation(conversationId) {
    try {
        const response = await fetch(`${API_BASE}/conversations/${conversationId}`);
        if (!response.ok) {
            throw new Error('Failed to load conversation');
        }

        const conversation = await response.json();

        // 상태 업데이트
        currentConversationId = conversationId;

        // 메시지 영역 초기화
        messagesContainer.innerHTML = '';

        // 메시지 렌더링
        if (conversation.messages && conversation.messages.length > 0) {
            conversation.messages.forEach(msg => {
                appendMessage(msg.role, msg.content);
            });
        }

        // 대화 목록 active 상태 업데이트
        document.querySelectorAll('.conversation-item').forEach(item => {
            item.classList.remove('active');
        });
        const activeItem = [...document.querySelectorAll('.conversation-item')]
            .find(item => item.textContent === conversation.title);
        if (activeItem) {
            activeItem.classList.add('active');
        }

        messageInput.focus();

    } catch (error) {
        console.error('Error loading conversation:', error);
        showError('대화를 불러오는데 실패했습니다.');
    }
}
