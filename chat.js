let currentConversationId = null;
let isStreaming = false;
let currentController = null;

const ICON_BOT = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M2 12h4l2-7 4 14 2-7h8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICON_USER = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="8" r="4" stroke="currentColor" stroke-width="2"/><path d="M4 20c0-4 3.5-6 8-6s8 2 8 6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
const ICON_TRASH = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 7h16M9 7V4h6v3m-8 0 1 13a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2l1-13" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

document.addEventListener('DOMContentLoaded', () => {
    loadConversations();
});

async function loadConversations() {
    try {
        const response = await fetch('/api/conversations');
        const data = await response.json();

        const conversationsList = document.getElementById('conversationsList');
        conversationsList.innerHTML = '';

        data.conversations.forEach((conv, index) => {
            const convItem = document.createElement('div');
            convItem.className = 'conversation-item';
            convItem.style.animationDelay = `${index * 0.05}s`;
            convItem.innerHTML = `
                <span class="conversation-title">${conv.title}</span>
                <button class="delete-btn" onclick="deleteConversation('${conv.id}', event)">${ICON_TRASH}</button>
            `;
            convItem.onclick = (e) => {
                if (!e.target.closest('.delete-btn')) {
                    loadConversation(conv.id, convItem);
                }
            };
            conversationsList.appendChild(convItem);
        });
    } catch (error) {
        console.error('Error loading conversations:', error);
    }
}

async function createNewConversation() {
    try {
        const response = await fetch('/api/conversations', { method: 'POST' });
        const data = await response.json();

        currentConversationId = data.id;
        document.getElementById('conversationTitle').textContent = data.title;

        showWelcomeScreen();
        await loadConversations();
    } catch (error) {
        console.error('Error creating conversation:', error);
    }
}

function showWelcomeScreen() {
    const chatMessages = document.getElementById('chatMessages');
    chatMessages.innerHTML = `
        <div class="welcome-screen" id="welcomeScreen">
            <div class="welcome-logo">${ICON_BOT}</div>
            <h2>مرحباً بك في Dr. SportsMed</h2>
            <p>مساعدك الذكي المتخصص في الطب الرياضي والتدليك والعلاج الطبيعي</p>
            <p>اسألني عن الإصابات الرياضية، برامج التأهيل، أو أي استفسار طبي رياضي</p>

            <div class="suggestions">
                <button onclick="askSuggestion('ما هي أعراض قطع الرباط الصليبي الأمامي؟')">
                    أعراض قطع الرباط الصليبي
                </button>
                <button onclick="askSuggestion('كيف يتم علاج الشد العضلي في الرياضة؟')">
                    علاج الشد العضلي
                </button>
                <button onclick="askSuggestion('ما هي تمارين التأهيل بعد الكسور؟')">
                    تمارين التأهيل بعد الكسور
                </button>
                <button onclick="askSuggestion('ما هي أنواع التدليك العلاجي والرياضي؟')">
                    أنواع التدليك العلاجي
                </button>
            </div>
        </div>
    `;
}

async function loadConversation(convId, element) {
    try {
        currentConversationId = convId;

        document.querySelectorAll('.conversation-item').forEach(item => {
            item.classList.remove('active');
        });
        if (element) element.classList.add('active');

        const response = await fetch(`/api/conversations/${convId}/messages`);
        const data = await response.json();

        const chatMessages = document.getElementById('chatMessages');
        chatMessages.innerHTML = '';

        if (data.messages.length === 0) {
            showWelcomeScreen();
        } else {
            data.messages.forEach(msg => {
                addMessageToChat(msg.role, msg.content, msg.sources, false);
            });
        }

    } catch (error) {
        console.error('Error loading conversation:', error);
    }
}

// تحويل نص الـ Markdown البسيط لـ HTML مرتب (بدون الاعتماد على مكتبة خارجية)
function renderMarkdown(text) {
    if (!text) return '';

    let html = escapeHtml(text);

    // Code blocks ```...```
    html = html.replace(/```([\s\S]*?)```/g, (m, code) => `<pre><code>${code}</code></pre>`);
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold **text**
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Headings ### text
    html = html.replace(/^### (.*$)/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.*$)/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.*$)/gm, '<h3>$1</h3>');

    // تحويل السطور لقوائم <ul><li>
    const lines = html.split('\n');
    let out = [];
    let inList = false;

    for (let line of lines) {
        const bulletMatch = line.match(/^\s*[-*]\s+(.*)$/);
        const numberMatch = line.match(/^\s*\d+[\.\)]\s+(.*)$/);

        if (bulletMatch || numberMatch) {
            if (!inList) {
                out.push('<ul>');
                inList = true;
            }
            out.push(`<li>${bulletMatch ? bulletMatch[1] : numberMatch[1]}</li>`);
        } else {
            if (inList) {
                out.push('</ul>');
                inList = false;
            }
            if (line.trim() === '') {
                out.push('<br>');
            } else if (!line.startsWith('<h3>') && !line.startsWith('<h4>') && !line.startsWith('<pre>')) {
                out.push(`<p>${line}</p>`);
            } else {
                out.push(line);
            }
        }
    }
    if (inList) out.push('</ul>');

    return out.join('');
}

function escapeHtml(str) {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function setStreamingUI(streaming) {
    isStreaming = streaming;
    const sendBtn = document.getElementById('sendBtn');
    const stopBtn = document.getElementById('stopBtn');
    const input = document.getElementById('messageInput');

    if (streaming) {
        sendBtn.style.display = 'none';
        stopBtn.classList.add('visible');
        input.disabled = true;
    } else {
        sendBtn.style.display = 'flex';
        sendBtn.disabled = false;
        sendBtn.innerHTML = `<span>إرسال</span><svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 12h16M13 5l7 7-7 7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
        stopBtn.classList.remove('visible');
        input.disabled = false;
    }
}

// إيقاف الرد الجاري فورًا بإلغاء طلب الشبكة
function stopGeneration() {
    if (currentController) {
        currentController.abort();
    }
}

async function sendMessage() {
    if (isStreaming) return; // منع إرسال أكتر من رسالة أثناء الرد

    const input = document.getElementById('messageInput');
    const message = input.value.trim();

    if (!message) return;

    if (!currentConversationId) {
        await createNewConversation();
    }

    const welcomeScreen = document.getElementById('welcomeScreen');
    if (welcomeScreen) welcomeScreen.remove();

    addMessageToChat('user', escapeHtml(message).replace(/\n/g, '<br>'), '', true);
    input.value = '';

    setStreamingUI(true);
    currentController = new AbortController();

    // فقاعة الرد الفاضية (هتتملى تدريجيًا مع الـ Stream)
    const chatMessages = document.getElementById('chatMessages');
    const assistantDiv = document.createElement('div');
    assistantDiv.className = 'message assistant';
    assistantDiv.innerHTML = `
        <div class="message-wrapper">
            <div class="message-avatar">${ICON_BOT}</div>
            <div class="message-content">
                <div class="typing-indicator">
                    <svg viewBox="0 0 60 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path class="typing-pulse-path" d="M0 12h14l4-8 6 16 4-12 3 4h29" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                </div>
            </div>
        </div>
    `;
    chatMessages.appendChild(assistantDiv);
    scrollToBottom();

    const contentDiv = assistantDiv.querySelector('.message-content');
    let fullText = '';
    let firstChunkReceived = false;
    let wasAborted = false;

    try {
        const response = await fetch(`/api/conversations/${currentConversationId}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question: message }),
            signal: currentController.signal
        });

        if (!response.ok || !response.body) {
            throw new Error('Network response was not ok');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let sourcesList = [];

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // كل حدث SSE بينتهي بسطرين فاضيين \n\n
            const parts = buffer.split('\n\n');
            buffer = parts.pop(); // آخر جزء ممكن يكون ناقص، نسيبه للـ buffer

            for (const part of parts) {
                const line = part.trim();
                if (!line.startsWith('data:')) continue;

                const jsonStr = line.slice(5).trim();
                if (!jsonStr) continue;

                let event;
                try {
                    event = JSON.parse(jsonStr);
                } catch (e) {
                    continue;
                }

                if (event.type === 'chunk') {
                    if (!firstChunkReceived) {
                        firstChunkReceived = true;
                        contentDiv.innerHTML = '<span class="stream-cursor">▍</span>';
                    }
                    fullText += event.text;
                    contentDiv.innerHTML = renderMarkdown(fullText) + '<span class="stream-cursor">▍</span>';
                    scrollToBottom();
                } else if (event.type === 'done') {
                    sourcesList = event.sources || [];
                }
            }
        }

        // إزالة الكيرسور وعرض النص النهائي مع المصادر
        let sourcesHtml = '';
        if (sourcesList.length > 0) {
            sourcesHtml = `<div class="sources"><strong>المصادر:</strong><br>${sourcesList.join('، ')}</div>`;
        }
        contentDiv.innerHTML = renderMarkdown(fullText) + sourcesHtml;

        await loadConversations();

    } catch (error) {
        if (error.name === 'AbortError') {
            // المستخدم دوس على زر الإيقاف: نعرض اللي اتكتب لحد دلوقتي بدل ما نعتبره خطأ
            wasAborted = true;
            const stoppedNote = `<div class="stopped-note">⏹ تم إيقاف الرد بواسطتك</div>`;
            contentDiv.innerHTML = (fullText ? renderMarkdown(fullText) : '<p>تم إيقاف الرد قبل أن يبدأ.</p>') + stoppedNote;
            await loadConversations();
        } else {
            console.error('Error sending message:', error);
            contentDiv.innerHTML = '❌ حدث خطأ. حاول مرة أخرى.';
        }
    } finally {
        currentController = null;
        setStreamingUI(false);
        scrollToBottom();
    }
}

function addMessageToChat(role, content, sources = '', animate = true) {
    const chatMessages = document.getElementById('chatMessages');

    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;
    if (!animate) messageDiv.style.animation = 'none';

    const avatarSvg = role === 'user' ? ICON_USER : ICON_BOT;

    let sourcesHtml = '';
    if (sources && sources !== '[]' && role === 'assistant') {
        let sourcesText = sources;
        try {
            const parsed = JSON.parse(sources.replace(/'/g, '"'));
            if (Array.isArray(parsed)) sourcesText = parsed.join('، ');
        } catch (e) { /* استخدم النص زي ما هو */ }
        sourcesHtml = `<div class="sources"><strong>المصادر:</strong><br>${sourcesText}</div>`;
    }

    const renderedContent = role === 'assistant' ? renderMarkdown(content) : content;

    messageDiv.innerHTML = `
        <div class="message-wrapper">
            <div class="message-avatar">${avatarSvg}</div>
            <div class="message-content">
                ${renderedContent}
                ${sourcesHtml}
            </div>
        </div>
    `;

    chatMessages.appendChild(messageDiv);
    scrollToBottom();
}

function scrollToBottom() {
    const chatMessages = document.getElementById('chatMessages');
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function askSuggestion(question) {
    document.getElementById('messageInput').value = question;
    sendMessage();
}

async function deleteConversation(convId, event) {
    event.stopPropagation();

    if (!confirm('هل أنت متأكد من حذف هذه المحادثة؟')) return;

    try {
        await fetch(`/api/conversations/${convId}`, { method: 'DELETE' });

        if (currentConversationId === convId) {
            currentConversationId = null;
            await createNewConversation();
        }

        await loadConversations();
    } catch (error) {
        console.error('Error deleting conversation:', error);
    }
}

function handleKeyPress(event) {
    if (event.key === 'Enter' && !isStreaming) {
        sendMessage();
    }
}