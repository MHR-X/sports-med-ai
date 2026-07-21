from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, stream_with_context
import os
import re
import json
import sqlite3
import uuid
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import ollama
from google import genai
from google.genai import types as genai_types
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.embeddings import Embeddings
from typing import List, Any, Optional, Iterator
from pydantic import Field

app = Flask(__name__)

app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())

# ==================== إعدادات قاعدة البيانات ====================
DB_NAME = "data/website.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            role TEXT,
            content TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES conversations (id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS training_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            category TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()

# ==================== دوال المستخدمين ====================
def create_user(username, email, password):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        hashed_password = generate_password_hash(password)
        c.execute('''
            INSERT INTO users (username, email, password)
            VALUES (?, ?, ?)
        ''', (username, email, hashed_password))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def verify_user(email, password):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT id, username, email, password FROM users WHERE email = ?', (email,))
    user = c.fetchone()
    conn.close()

    if user and check_password_hash(user[3], password):
        return {'id': user[0], 'username': user[1], 'email': user[2]}
    return None

def user_still_exists(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT 1 FROM users WHERE id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

# ==================== دوال المحادثات ====================
def create_conversation(user_id, title="محادثة جديدة"):
    conv_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        INSERT INTO conversations (id, user_id, title)
        VALUES (?, ?, ?)
    ''', (conv_id, user_id, title))
    conn.commit()
    conn.close()
    return conv_id

def get_user_conversations(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        SELECT id, title, created_at FROM conversations
        WHERE user_id = ? ORDER BY updated_at DESC
    ''', (user_id,))
    conversations = c.fetchall()
    conn.close()
    return conversations

def get_conversation_messages(conv_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        SELECT id, role, content, sources, created_at FROM messages
        WHERE conversation_id = ? ORDER BY id ASC
    ''', (conv_id,))
    messages = c.fetchall()
    conn.close()
    return messages

def save_message(conv_id, role, content, sources=""):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        INSERT INTO messages (conversation_id, role, content, sources)
        VALUES (?, ?, ?, ?)
    ''', (conv_id, role, content, sources))

    new_id = c.lastrowid

    c.execute('''
        UPDATE conversations SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (conv_id,))

    conn.commit()
    conn.close()
    return new_id

def update_message_content(msg_id, content):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('UPDATE messages SET content = ? WHERE id = ?', (content, msg_id))
    conn.commit()
    conn.close()

def get_message(msg_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT id, conversation_id, role, content FROM messages WHERE id = ?', (msg_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_next_message(conv_id, msg_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        SELECT id, role FROM messages
        WHERE conversation_id = ? AND id > ?
        ORDER BY id ASC LIMIT 1
    ''', (conv_id, msg_id))
    row = c.fetchone()
    conn.close()
    return row

def delete_message_by_id(msg_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('DELETE FROM messages WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()

def delete_messages_after(conv_id, msg_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('DELETE FROM messages WHERE conversation_id = ? AND id > ?', (conv_id, msg_id))
    conn.commit()
    conn.close()

def update_conversation_title(conv_id, title):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('UPDATE conversations SET title = ? WHERE id = ?', (title, conv_id))
    conn.commit()
    conn.close()

def delete_conversation(conv_id, user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('DELETE FROM messages WHERE conversation_id = ?', (conv_id,))
    c.execute('DELETE FROM conversations WHERE id = ? AND user_id = ?', (conv_id, user_id))
    conn.commit()
    conn.close()

def verify_conversation_owner(conv_id, user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT id FROM conversations WHERE id = ? AND user_id = ?', (conv_id, user_id))
    result = c.fetchone()
    conn.close()
    return result is not None

# ==================== دوال بيانات التدريب ====================
def save_training_pair(conv_id, question, answer, category, sources):
    if not question or not answer:
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        INSERT INTO training_data (conversation_id, question, answer, category, sources)
        VALUES (?, ?, ?, ?, ?)
    ''', (conv_id, question, answer, category, json.dumps(sources, ensure_ascii=False)))
    conn.commit()
    conn.close()

def get_all_training_pairs(category=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if category:
        c.execute('SELECT question, answer, category, sources, created_at FROM training_data WHERE category = ? ORDER BY id ASC', (category,))
    else:
        c.execute('SELECT question, answer, category, sources, created_at FROM training_data ORDER BY id ASC')
    rows = c.fetchall()
    conn.close()
    return rows

# ==================== حماية من انحراف اللغة ====================
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')

def _has_cjk(text: str) -> bool:
    return bool(CJK_PATTERN.search(text))

def _truncate_at_cjk(text: str) -> str:
    match = CJK_PATTERN.search(text)
    if not match:
        return text

    truncated = text[:match.start()]
    last_break = max(
        truncated.rfind('.'), truncated.rfind('\n'),
        truncated.rfind('؟'), truncated.rfind('!'), truncated.rfind('؛')
    )
    if last_break > 0:
        truncated = truncated[:last_break + 1]
    return truncated.strip()

# ==================== تحميل ملفات الـ Prompts ====================
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

SYSTEM_PROMPT_SECTIONS = [
    "system",
    "language",
    "context",
    "rag_rules",
    "diagnosis",
    "injury_mode",
    "differential",
    "evidence_used",
    "clinical_reasoning",
    "missing_info",
    "regional_questions",
    "verification",
    "red_flags",
    "treatment",
    "rehabilitation",
    "medications",
    "outside_scope",
    "safety",
    "formatting",
    "confidence",  # قسم جديد لتعزيز الثقة
]


def _fill_prompt_template(template: str, **kwargs) -> str:
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result


def _load_prompt_file(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, f"{name}.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        print(f"⚠️ ملف البرومبت غير موجود: {path}")
        return ""


def _build_system_prompt_template() -> str:
    sections = [_load_prompt_file(name) for name in SYSTEM_PROMPT_SECTIONS]
    sections = [s for s in sections if s]
    body = "\n\n".join(sections)
    return (
        body
        + "\n\n## السياق من المراجع:\n{context}"
        + "\n\n## السؤال:\n{question}"
        + "\n\n## تقريرك (اتبع كل القواعد أعلاه بدقة، وأجب بلغة سؤال المستخدم):"
    )


SYSTEM_PROMPT = _build_system_prompt_template()
CLASSIFY_PROMPT = _load_prompt_file("classification")

# ==================== نظام الذكاء الاصطناعي ====================
# ---- تحميل مفاتيح Gemini من ملف، بالترتيب ----
# اعمل ملف اسمه gemini_keys.txt (بجانب app.py)، وحط فيه مفتاح في كل سطر بالترتيب
# اللي عايز يتجرب بيه. النظام هيبدأ بأول مفتاح، وأول ما كوتته تخلص هيروح تلقائي
# للي بعده في الملف، وهكذا لحد آخر مفتاح.
# أي سطر فاضي أو بيبدأ بـ # (تعليق) بيتجاهل.
GEMINI_KEYS_FILE = os.environ.get(
    "GEMINI_KEYS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_keys.txt"),
)


def _load_gemini_keys_from_file(path: str) -> List[str]:
    keys = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                keys.append(line)
    except FileNotFoundError:
        print(f"⚠️ ملف مفاتيح Gemini غير موجود: {path}")
    return keys


GEMINI_API_KEYS = _load_gemini_keys_from_file(GEMINI_KEYS_FILE)

# لو الملف فاضي أو مش موجود، جرب كـ خيار احتياطي متغيرات البيئة القديمة
if not GEMINI_API_KEYS:
    _raw_gemini_keys = os.environ.get("GEMINI_API_KEYS", "") or os.environ.get("GEMINI_API_KEY", "")
    GEMINI_API_KEYS = [k.strip() for k in _raw_gemini_keys.split(",") if k.strip()]

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_CLASSIFY_MODEL = "gemini-2.5-flash-lite"

EMBEDDING_MODEL = "nomic-embed-text-v2-moe"
KEEP_ALIVE = "1h"

EMBEDDING_USES_PREFIXES = True

EMBEDDING_CHUNK_SIZE = 700
EMBEDDING_CHUNK_OVERLAP = 100

GEN_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.1,  # خفضنا الحرارة عشان يبقى أكثر دقة وثبات
    top_p=0.9,
    max_output_tokens=8000,
    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
)

CLASSIFY_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.05,  # خفضناها عشان التصنيف يبقى دقيق
    max_output_tokens=200,
)

GROUNDED_MIN_DOCS = 2

SIMILARITY_THRESHOLD = 0.4

USE_RAG_CONTEXT = True

USE_AUTO_VERIFICATION = True

# ===== تحسينات الثقة وتقليل التنبيهات =====
AUTO_VERIFY_MIN_CONTENT_CHARS = 50  # زودناها عشان ما يظهرش للجمل القصيرة

AUTO_VERIFY_GROUNDING_THRESHOLD = 0.15  # خفضناها عشان يظهر أقل

# تقليل التنبيهات الطويلة
AUTO_VERIFY_NOTE = " (⚠️ معلومة غير مؤكدة في المرجع)"  # أقصر

GENERAL_KNOWLEDGE_NOTE = (
    "\n\n---\nℹ️ **تنبيه:** جزء من الرد مبني على المعرفة الطبية العامة.\n---"
)

CATEGORY_TO_METADATA = {
    "اصابات": "injuries",
    "تدليك": "massage",
    "تشريح": "anatomy",
    "تغذية": "nutrition",
    "بيوميكانيك": "biomechanics",
    "حجامة": "cupping",
    "قياس_وتقويم": "measurement",
    "بحث_علمي": "research_writing",
}

BOOK_SOURCE_NAMES = {
    "injuries": "📘 كتاب الإصابات الرياضية - د. أحمد الحاج",
    "massage": "📘 كتاب التدليك العلاجي - د. أحمد الحاج",
    "anatomy": "📘 كتاب التشريح العضلي والوظيفي - د. أحمد الحاج",
    "nutrition": "📘 كتاب التغذية الرياضية - د. أحمد الحاج",
    "biomechanics": "📘 كتاب البيوميكانيك - د. أحمد الحاج",
    "cupping": "📘 كتاب الحجامة - د. أحمد الحاج",
    "measurement": "📘 كتاب القياس والتقويم - د. أحمد الحاج",
    "research_writing": "📘 كتاب أساسيات كتابة البحث العلمي - د. أحمد الحاج",
}

GENERAL_KNOWLEDGE_SOURCE_LINE = "- ℹ️ بعض التفاصيل مكملة من المعرفة الطبية العامة"

NON_BOOK_SOURCE_NOTE = "\n\n---\nℹ️ رد تلقائي من النظام (غير مأخوذ من الكتب)."

TRUNCATED_NOTE = "\n\n---\n⚠️ تم قطع الرد تلقائياً."

STOPPED_NOTE = "\n\n---\n⏹️ تم إيقاف الرد."

# ===== تنويه طبي مختصر =====
MEDICAL_DISCLAIMER = (
    "\n\n---\n⚠️ **تنويه:** هذا تقرير معلوماتي، وليس بديلاً عن استشارة طبية مباشرة."
)

# ===== رسالة الصيانة (تظهر بدل أي إيرور تقني للمستخدم) =====
MAINTENANCE_MESSAGE = (
    "🛠️ **النظام تحت الصيانة مؤقتاً** بسبب ضغط على الطلبات حالياً.\n\n"
    "من فضلك جرب تاني بعد كام دقيقة. لو المشكلة استمرت، تواصل مع الدعم."
)


def format_source_footer(sources, has_general_knowledge_parts=False):
    lines = []

    if sources:
        book_names = []
        for s in sources:
            book_name = BOOK_SOURCE_NAMES.get(s.get("category"), "📘 مرجع طبي - د. أحمد الحاج")
            if book_name not in book_names:
                book_names.append(book_name)
        lines.extend([f"- {name}" for name in book_names])

    if has_general_knowledge_parts:
        lines.append(GENERAL_KNOWLEDGE_SOURCE_LINE)

    if not lines:
        return ""

    return "\n\n---\n📚 **المصدر:**\n" + "\n".join(lines)


def _log_rag_debug(question, category, docs_with_scores, kept_docs_with_scores):
    print(f"\n{'='*60}")
    print(f"🔍 RAG DEBUG | السؤال: {question[:60]}")
    print(f"   الفئة: {category} | حد التشابه المستخدم: {SIMILARITY_THRESHOLD}")
    print(f"   إجمالي النتائج المسترجعة قبل الفلترة: {len(docs_with_scores)}")
    for doc, score in docs_with_scores[:8]:
        marker = "✅" if score >= SIMILARITY_THRESHOLD else "❌"
        source = doc.metadata.get('source', '؟')
        preview = doc.page_content[:50].replace('\n', ' ')
        print(f"   {marker} score={score:.3f} | {source} | {preview}...")
    print(f"   النتائج اللي عدّت الحد ({SIMILARITY_THRESHOLD}): {len(kept_docs_with_scores)}")
    print(f"{'='*60}\n")


def _answer_has_general_knowledge_note(answer: str) -> bool:
    return "معرفة طبية عامة" in answer


AR_DIACRITICS_PATTERN = re.compile(r'[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]')
NON_WORD_SPLIT_PATTERN = re.compile(r'[^\w]+', re.UNICODE)
BULLET_PREFIX_PATTERN = re.compile(r'^[\s\-\*\u2022\d\.\)]+')


def _normalize_arabic_text(text: str) -> str:
    text = AR_DIACRITICS_PATTERN.sub('', text)
    text = re.sub(r'[إأآا]', 'ا', text)
    text = text.replace('ى', 'ي')
    text = text.replace('ة', 'ه')
    text = text.replace('ؤ', 'و')
    text = text.replace('ئ', 'ي')
    return text.lower()


def _extract_meaningful_words(text: str, min_len: int = 3) -> set:
    normalized = _normalize_arabic_text(text)
    words = NON_WORD_SPLIT_PATTERN.split(normalized)
    return {w for w in words if len(w) >= min_len}


def _grounding_overlap_score(line_text: str, context_words: set) -> float:
    line_words = _extract_meaningful_words(line_text)
    if not line_words:
        return 1.0
    overlap = line_words & context_words
    return len(overlap) / len(line_words)


SUBLINE_LABELS = [
    "وضع المريض", "وضع الفاحص", "وضع البدء", "الوضعية", "المقاومة",
    "الحركة", "النتيجة الإيجابية", "المجموعات", "التكرارات", "التردد",
    "كيفية التمييز سريريًا",
]
SUBLINE_LABELS_NORM = [_normalize_arabic_text(l) for l in SUBLINE_LABELS]


def _strip_line_markup(stripped_line: str) -> str:
    s = stripped_line.strip()
    s = BULLET_PREFIX_PATTERN.sub('', s)
    s = s.lstrip('*').strip()
    return s


def _is_subline_field(stripped_line: str) -> bool:
    cleaned = _strip_line_markup(stripped_line)
    normalized = _normalize_arabic_text(cleaned)
    for label_norm in SUBLINE_LABELS_NORM:
        if normalized.startswith(label_norm + ':') or normalized.startswith(label_norm + '：'):
            return True
    return False


def _is_boilerplate_line(stripped_line: str, is_first_content_line: bool) -> bool:
    if is_first_content_line and 'dr. sportsmed' in stripped_line.lower():
        return True
    normalized = _normalize_arabic_text(stripped_line)
    if normalized.startswith(_normalize_arabic_text('بالشفاء')) and 'مراجعه الحاله' in normalized:
        return True
    return False


def _auto_verify_grounding(text: str, context: str):
    if not context or not context.strip():
        return text, []

    context_words = _extract_meaningful_words(context)
    lines = text.split('\n')

    blocks = []
    skip_indices = set()
    current_block = None
    seen_first_content_line = False

    for idx, line in enumerate(lines):
        stripped = line.strip()

        if not stripped or stripped.startswith('#'):
            if current_block is not None:
                blocks.append(current_block)
                current_block = None
            blocks.append([idx])
            continue

        is_first = not seen_first_content_line
        seen_first_content_line = True

        if _is_boilerplate_line(stripped, is_first):
            if current_block is not None:
                blocks.append(current_block)
                current_block = None
            blocks.append([idx])
            skip_indices.add(idx)
            continue

        if current_block is not None and _is_subline_field(stripped):
            current_block.append(idx)
        else:
            if current_block is not None:
                blocks.append(current_block)
            current_block = [idx]

    if current_block is not None:
        blocks.append(current_block)

    new_lines = list(lines)
    flagged = []

    for block in blocks:
        if any(i in skip_indices for i in block):
            continue

        block_lines = [lines[i].strip() for i in block]
        combined = ' '.join(block_lines)

        is_header_or_empty = all((not l) or l.startswith('#') for l in block_lines)
        already_noted = any(
            'معرفه طبيه عامه موثوقه' in _normalize_arabic_text(l) or 'تحقق تلقائي' in l
            for l in block_lines
        )
        content_len = len(re.sub(r'[^\u0600-\u06FFa-zA-Z]', '', combined))

        if is_header_or_empty or already_noted or content_len < AUTO_VERIFY_MIN_CONTENT_CHARS:
            continue

        score = _grounding_overlap_score(combined, context_words)
        if score < AUTO_VERIFY_GROUNDING_THRESHOLD:
            last_idx = block[-1]
            new_lines[last_idx] = new_lines[last_idx] + AUTO_VERIFY_NOTE
            flagged.append({"line_preview": combined[:70], "score": round(score, 3)})

    return '\n'.join(new_lines), flagged


def _log_auto_verify_debug(flagged):
    if not flagged:
        print("🔎 AUTO-VERIFY: كل البلوكات عدّت التحقق.")
        return
    print(f"\n{'='*60}")
    print(f"🔎 AUTO-VERIFY: {len(flagged)} بلوك بدون تطابق:")
    for item in flagged:
        print(f"   ⚠️ score={item['score']:.3f} | {item['line_preview']}...")
    print(f"{'='*60}\n")


def _build_auto_verify_supplement(flagged):
    if not flagged:
        return ""
    lines = [
        "\n\n---",
        "🔎 **ملاحظة:** بعض الأجزاء غير مؤكدة في المرجع:",
    ]
    for item in flagged[:5]:  # خفضنا العدد عشان ما يطولش
        lines.append(f"- \"{item['line_preview'][:40]}...\"")
    if len(flagged) > 5:
        lines.append(f"- ...و{len(flagged) - 5} أجزاء أخرى")
    lines.append("---")
    return "\n".join(lines)


class GeminiLLMWrapper:
    """
    Wrapper بيدعم أكتر من مفتاح Gemini مع تبديل تلقائي (rotation) بينهم.
    لو مفتاح خلصت كوتته أو حصله أي إيرور، بيتحول تلقائي للمفتاح اللي بعده،
    وهكذا لحد ما يلاقي مفتاح شغال أو يخلص كل المفاتيح (ويرفع إيرور في الحالة دي).
    بيفتكر آخر مفتاح اشتغل عشان المرة الجاية يبدأ بيه على طول (توفير وقت ومحاولات).
    """

    def __init__(self, api_keys, model_name: str = GEMINI_MODEL, classify_model: str = GEMINI_CLASSIFY_MODEL):
        if isinstance(api_keys, str):
            api_keys = [api_keys] if api_keys else []
        api_keys = [k for k in (api_keys or []) if k]

        if not api_keys:
            raise ValueError(
                "لا يوجد أي مفتاح Gemini. من فضلك حط مفتاح واحد على الأقل في متغير البيئة "
                "GEMINI_API_KEYS (مفصولة بفاصلة لو أكتر من مفتاح) أو GEMINI_API_KEY."
            )

        self.api_keys = api_keys
        self.clients = [genai.Client(api_key=k) for k in api_keys]
        self.model_name = model_name
        self.classify_model = classify_model
        self.current_index = 0  # آخر مفتاح اشتغل بنجاح

    def _mask_key(self, idx: int) -> str:
        key = self.api_keys[idx]
        return f"مفتاح رقم {idx + 1} (...{key[-4:]})" if len(key) >= 4 else f"مفتاح رقم {idx + 1}"

    def _call(self, prompt: str) -> str:
        last_error = None
        n = len(self.clients)
        for i in range(n):
            idx = (self.current_index + i) % n
            try:
                response = self.clients[idx].models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=GEN_CONFIG,
                )
                self.current_index = idx
                text = response.text or ""
                return _truncate_at_cjk(text)
            except Exception as e:
                last_error = e
                print(f"⚠️ {self._mask_key(idx)} فشل: {e}")
                continue
        raise last_error

    def classify_call(self, prompt: str) -> str:
        last_error = None
        n = len(self.clients)
        for i in range(n):
            idx = (self.current_index + i) % n
            try:
                response = self.clients[idx].models.generate_content(
                    model=self.classify_model,
                    contents=prompt,
                    config=CLASSIFY_CONFIG,
                )
                self.current_index = idx
                return (response.text or "").strip()
            except Exception as e:
                last_error = e
                print(f"⚠️ {self._mask_key(idx)} فشل في التصنيف: {e}")
                continue
        raise last_error

    def stream_call(self, prompt: str) -> Iterator[str]:
        # ملحوظة: لو مفتاح فشل بعد ما بعت جزء من الرد (نادر، بيحصل غالباً على طول قبل أي توكن)،
        # هنبدأ من المفتاح اللي بعده من الأول، فممكن جزء بسيط يتكرر في حالات نادرة جداً.
        last_error = None
        n = len(self.clients)
        for i in range(n):
            idx = (self.current_index + i) % n
            try:
                stream = self.clients[idx].models.generate_content_stream(
                    model=self.model_name,
                    contents=prompt,
                    config=GEN_CONFIG,
                )
                for chunk in stream:
                    content = getattr(chunk, "text", None)
                    if not content:
                        continue

                    if _has_cjk(content):
                        match = CJK_PATTERN.search(content)
                        safe_part = content[:match.start()]
                        if safe_part:
                            yield safe_part
                        self.current_index = idx
                        return

                    yield content

                self.current_index = idx
                return
            except Exception as e:
                last_error = e
                print(f"⚠️ {self._mask_key(idx)} فشل أثناء الـ streaming: {e}")
                continue
        if last_error:
            raise last_error


class OllamaEmbeddingsWrapper(Embeddings):
    model_name: str = Field(default=EMBEDDING_MODEL)

    def __init__(self, model_name: str = EMBEDDING_MODEL, **kwargs):
        super().__init__(**kwargs)
        self.model_name = model_name

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        embeddings = []
        total = len(texts)
        for i, text in enumerate(texts, 1):
            prefixed_text = f"search_document: {text}" if EMBEDDING_USES_PREFIXES else text
            response = ollama.embeddings(model=self.model_name, prompt=prefixed_text, keep_alive=KEEP_ALIVE)
            embeddings.append(response['embedding'])
            if i % 20 == 0 or i == total:
                print(f"   ⏳ تم عمل embedding لـ {i}/{total} جزء...")
        return embeddings

    def embed_query(self, text: str) -> List[float]:
        prefixed_text = f"search_query: {text}" if EMBEDDING_USES_PREFIXES else text
        response = ollama.embeddings(model=self.model_name, prompt=prefixed_text, keep_alive=KEEP_ALIVE)
        return response['embedding']

IDENTITY_KEYWORDS = [
    "مين طورك", "من طورك", "من صنعك", "مين صنعك", "مين عملك", "من عملك",
    "من صممك", "مين صممك", "اسمك ايه", "اسمك إيه", "انت مين", "أنت مين",
    "من انت", "من أنت", "مين انت", "مين أنت", "انتي مين", "مين المطور",
    "who are you", "who made you", "who created you", "your name", "who built you",
]

IDENTITY_ANSWER = (
    "أنا **Dr. SportsMed**، متخصص في الطب الرياضي والتدليك والعلاج الطبيعي، "
    "طورت بواسطة المهندس محمد عبدالرحمن بالاستعانة بمراجع د. أحمد الحاج. "
    "أداة معلوماتية مساعدة، وليست بديلاً عن الطبيب."
)

GREETING_KEYWORDS = [
    "مرحبا", "مرحباً", "اهلا", "أهلا", "اهلين", "أهلين", "هاي", "هلا",
    "ازيك", "إزيك", "از الحال", "عامل ايه", "عامل إيه", "عاملة ايه", "عاملة إيه",
    "كيف حالك", "كيفك", "شلونك", "ايه الاخبار", "إيه الأخبار",
    "صباح الخير", "مساء الخير", "صباح النور", "مساء النور",
    "شكرا", "شكراً", "متشكر", "تسلم", "الله يسلمك",
    "مع السلامة", "باي", "تصبح على خير",
    "hi", "hello", "hey", "good morning", "good evening", "thanks", "thank you", "bye",
]

GREETING_ANSWER = (
    "أهلاً بك! 👋 أنا **Dr. SportsMed**، متخصص في الطب الرياضي. كيف يمكنني مساعدتك؟"
)

OUT_OF_SCOPE_ANSWER = (
    "⛔ هذا السؤال خارج نطاق تخصصي. أنا متخصص في الطب الرياضي والتدليك والعلاج الطبيعي فقط."
)

class SportsMedicineRAG:
    def __init__(self):
        self.vectorstore = None
        self.llm = GeminiLLMWrapper(api_keys=GEMINI_API_KEYS)
        print(f"✅ تم تحميل {len(GEMINI_API_KEYS)} مفتاح/مفاتيح Gemini من الملف مع تبديل تلقائي بينهم عند خلوص الكوتة.")

        self.embeddings = OllamaEmbeddingsWrapper(model_name=EMBEDDING_MODEL)

        self.pdf_folders = {
            "injuries": "data/pdfs/injuries",
            "massage": "data/pdfs/massage",
            "anatomy": "data/pdfs/anatomy",
            "nutrition": "data/pdfs/التغذية",
            "biomechanics": "data/pdfs/البيوميكانيك",
            "cupping": "data/pdfs/الحجامه",
            "measurement": "data/pdfs/القياس والتقويم",
            "research_writing": "data/pdfs/اساسيات كتابة البحث العلمي",
        }
        self.db_folder = "data/chroma_db"

        for folder in self.pdf_folders.values():
            os.makedirs(folder, exist_ok=True)
        os.makedirs(self.db_folder, exist_ok=True)

    def load_pdfs(self, category):
        folder = self.pdf_folders.get(category)
        if not folder or not os.path.exists(folder):
            return []

        documents = []
        for root, _, files in os.walk(folder):
            for filename in files:
                if filename.lower().endswith('.pdf'):
                    filepath = os.path.join(root, filename)
                    try:
                        loader = PyPDFLoader(filepath)
                        docs = loader.load()
                        for doc in docs:
                            doc.metadata['source'] = filename
                            doc.metadata['category'] = category
                        documents.extend(docs)
                    except Exception as e:
                        print(f"خطأ في تحميل {filename}: {e}")

        return documents

    def build_all(self):
        all_documents = []
        for category in self.pdf_folders:
            docs = self.load_pdfs(category)
            print(f"تم تحميل {len(docs)} صفحة من فئة '{category}'")
            all_documents.extend(docs)

        if not all_documents:
            print("⚠️ مفيش أي PDF اتلقى. تأكد إنك حاطط الملفات في data/pdfs/injuries و data/pdfs/massage و data/pdfs/anatomy")
            return False

        try:
            success = self.create_vectorstore(all_documents)
        except KeyboardInterrupt:
            print("\n⚠️ تم إيقاف العملية قبل ما تخلص - قاعدة البيانات لسه ناقصة/فاضية.")
            print("   شغل الأمر تاني وسيبه لحد ما يطبع '✅ تم بناء قاعدة البيانات بنجاح' من غير ما تقفله.")
            return False

        if success:
            print("✅ تم بناء قاعدة البيانات بنجاح")
        return success

    def create_vectorstore(self, documents):
        if not documents:
            return False

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=EMBEDDING_CHUNK_SIZE,
            chunk_overlap=EMBEDDING_CHUNK_OVERLAP,
            length_function=len,
        )

        chunks = text_splitter.split_documents(documents)

        print(f"📄 إجمالي عدد الأجزاء (chunks) اللي هيتعمللها embedding: {len(chunks)}")
        print("⏳ العملية دي ممكن تاخد كام دقيقة حسب عدد الأجزاء وسرعة جهازك - سيبها تخلص ومتقفلش الترمينال.")

        if os.path.exists(self.db_folder) and os.listdir(self.db_folder):
            import shutil
            shutil.rmtree(self.db_folder)
            os.makedirs(self.db_folder, exist_ok=True)

        self.vectorstore = Chroma.from_documents(
            documents=chunks,
            embedding=self.embeddings,
            persist_directory=self.db_folder,
            collection_metadata={"hnsw:space": "cosine"},
        )

        return True

    def load_existing_vectorstore(self):
        if os.path.exists(self.db_folder) and os.listdir(self.db_folder):
            try:
                self.vectorstore = Chroma(
                    persist_directory=self.db_folder,
                    embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"},
                )
                return True
            except Exception:
                return False
        return False

    def _is_identity_question(self, question):
        q = question.strip().lower()
        return any(keyword in q for keyword in IDENTITY_KEYWORDS)

    def _is_greeting(self, question):
        q = question.strip().lower()
        q_clean = q.strip("!?.,؟،  ")
        return any(keyword in q_clean for keyword in GREETING_KEYWORDS)

    def _detect_user_role(self, history):
        """تحديد دور المستخدم من سياق المحادثة"""
        if not history:
            return "مستخدم"
        
        # كلمات تدل على أن المستخدم متدرب
        trainee_keywords = ["متدرب", "طالب", "أتعلم", "دراسة", "تدريب", "تعليم"]
        # كلمات تدل على أن المستخدم طبيب
        doctor_keywords = ["طبيب", "دكتور", "أنا د", "معايا عيادة", "أعمل"]
        # كلمات تدل على أن المستخدم مريض
        patient_keywords = ["أنا مريض", "عندي ألم", "أعاني", "بيوجعني"]
        
        # افحص آخر 5 رسائل من المستخدم
        user_msgs = [m for m in history if m['role'] == 'user'][-5:]
        
        for msg in user_msgs:
            content = msg['content']
            if any(kw in content for kw in doctor_keywords):
                return "طبيب"
            if any(kw in content for kw in trainee_keywords):
                return "متدرب"
            if any(kw in content for kw in patient_keywords):
                return "مريض"
        
        return "مستخدم"

    def _classify_topic(self, question, history=None):
        """
        تصنيف السؤال مع مراعاة سياق المحادثة السابق
        """
        # === الخطوة 1: تحليل السياق لفهم الإشارات الضمنية ===
        if history and len(history) > 0:
            reference_keywords = ["هو", "هي", "دا", "ده", "دي", "ديه", "اللي", "اللي قولته", "زي ما قلت", "كما ذكرت"]
            patient_keywords = ["المريض", "المرض", "الحالة", "الإصابة", "العلاج"]
            
            if len(question.split()) < 5 or any(kw in question for kw in reference_keywords):
                last_user_msgs = [m for m in history if m['role'] == 'user'][-2:] if history else []
                last_assistant_msgs = [m for m in history if m['role'] == 'assistant'][-2:] if history else []
                
                for msg in last_assistant_msgs:
                    if any(kw in msg['content'] for kw in patient_keywords + ["تشخيص", "علاج", "إصابة"]):
                        return "اصابات", []
                
                for msg in last_user_msgs:
                    if any(kw in msg['content'] for kw in patient_keywords + ["مريض", "إصابة"]):
                        return "اصابات", []

        # === الخطوة 2: التصنيف العادي ===
        prompt = _fill_prompt_template(CLASSIFY_PROMPT, question=question)
        try:
            raw = self.llm.classify_call(prompt).strip()
        except Exception:
            return "عام", []

        cleaned = raw.replace("```json", "").replace("```", "").strip()

        try:
            data = json.loads(cleaned)
            category_raw = str(data.get("category", "")).strip()
            search_queries = data.get("search_queries", []) or []
            if not isinstance(search_queries, list):
                search_queries = []
            search_queries = [str(q).strip() for q in search_queries if str(q).strip()][:3]
        except Exception:
            category_raw = cleaned
            search_queries = []

        category_norm = category_raw.replace(" ", "")
        
        # === الخطوة 3: التحقق من كلمات مفتاحية إضافية ===
        sport_keywords = ["كرة", "قدم", "جري", "سباحة", "تنس", "سلة", "طائرة", "رياضي", "تمرين", "تدريب"]
        medical_keywords = ["مريض", "إصابة", "علاج", "تشخيص", "ألم", "تورم", "كسر", "خلع", "تمزق", "التواء"]
        age_keywords = ["عمر", "سنه", "سنة", "سن", "عندو", "عنده", "ولد", "بنت", "طفل"]
        
        if any(kw in question for kw in sport_keywords) or any(kw in question for kw in medical_keywords) or any(kw in question for kw in age_keywords):
            if "غير" in category_norm or "no" in category_norm.lower():
                return "اصابات", search_queries
        
        if "غير" in category_norm or "no" in category_norm.lower():
            return "خارج_التخصص", []
        
        if "تدليك" in category_norm:
            category = "تدليك"
        elif "اصاب" in category_norm or "إصاب" in category_norm:
            category = "اصابات"
        elif "تشريح" in category_norm:
            category = "تشريح"
        elif "تغذي" in category_norm or "غذاء" in category_norm:
            category = "تغذية"
        elif "بيوميكانيك" in category_norm or "ميكانيك" in category_norm:
            category = "بيوميكانيك"
        elif "حجام" in category_norm:
            category = "حجامة"
        elif "قياس" in category_norm or "تقويم" in category_norm:
            category = "قياس_وتقويم"
        elif "بحث" in category_norm:
            category = "بحث_علمي"
        else:
            category = "عام"

        return category, search_queries

    def _build_prompt(self, question, category=None, search_queries=None, history=None):
        """
        بناء البرومبت مع إضافة سياق المحادثة السابقة
        """
        # === تحديد دور المستخدم ===
        user_role = self._detect_user_role(history) if history else "مستخدم"
        role_instruction = ""
        if user_role == "متدرب":
            role_instruction = "\n\n## تعليمات إضافية للمتدرب:\nقدم شرحاً تعليمياً مع التشخيص، ووضح المصطلحات الطبية، واشرح خطوات التفكير السريري."
        elif user_role == "طبيب":
            role_instruction = "\n\n## تعليمات إضافية للطبيب:\nقدم تشخيصاً دقيقاً مختصراً، وركز على النقاط السريرية المهمة، وتحدث بلغة طبية مباشرة."
        elif user_role == "مريض":
            role_instruction = "\n\n## تعليمات إضافية للمريض:\nتحدث بلغة بسيطة واضحة، وقدم شرحاً مبسطاً، وتجنب المصطلحات الطبية المعقدة."

        # بناء سياق المحادثة التاريخي
        history_context = ""
        if history and len(history) > 0:
            history_parts = []
            for msg in history[-15:]:
                if msg['role'] == 'user':
                    history_parts.append(f"المستخدم: {msg['content']}")
                else:
                    history_parts.append(f"د. SportsMed: {msg['content']}")
            history_context = "\n".join(history_parts)
            history_context = f"\n\n## سياق المحادثة السابقة:\n{history_context}\n\n## السؤال الحالي:\n{question}"
        else:
            history_context = f"\n\n## السؤال:\n{question}"

        # إضافة تعليمات الدور
        history_context += role_instruction

        if not USE_RAG_CONTEXT:
            prompt = _fill_prompt_template(
                SYSTEM_PROMPT,
                context="(لا يوجد اعتماد على مراجع محددة - أجب من معرفتك الطبية)",
                question=history_context,
            )
            return prompt, [], False, ""

        metadata_category = CATEGORY_TO_METADATA.get(category)
        RETRIEVAL_K = 6
        MIN_DOCS_BEFORE_FALLBACK = 3
        MAX_TOTAL_DOCS = 8

        search_queries = search_queries or []
        all_queries = [question] + search_queries

        all_docs_with_scores = []
        seen_keys = set()

        def _add_unique_with_scores(results_with_scores):
            for d, score in results_with_scores:
                key = (d.metadata.get('source'), d.page_content[:80])
                if key not in seen_keys:
                    all_docs_with_scores.append((d, score))
                    seen_keys.add(key)

        for q in all_queries:
            if metadata_category:
                results = self.vectorstore.similarity_search_with_relevance_scores(
                    q, k=RETRIEVAL_K, filter={"category": metadata_category}
                )
            else:
                results = self.vectorstore.similarity_search_with_relevance_scores(q, k=RETRIEVAL_K)
            _add_unique_with_scores(results)

        relevant_so_far = [(d, s) for d, s in all_docs_with_scores if s >= SIMILARITY_THRESHOLD]

        if metadata_category and len(relevant_so_far) < MIN_DOCS_BEFORE_FALLBACK:
            for q in all_queries:
                broader = self.vectorstore.similarity_search_with_relevance_scores(q, k=RETRIEVAL_K)
                _add_unique_with_scores(broader)
            relevant_so_far = [(d, s) for d, s in all_docs_with_scores if s >= SIMILARITY_THRESHOLD]

        relevant_sorted = sorted(relevant_so_far, key=lambda x: x[1], reverse=True)[:MAX_TOTAL_DOCS]

        _log_rag_debug(question, category, all_docs_with_scores, relevant_sorted)

        docs = [d for d, _ in relevant_sorted]
        grounded = len(docs) >= GROUNDED_MIN_DOCS
        context = "\n\n".join([doc.page_content for doc in docs])

        prompt = _fill_prompt_template(
            SYSTEM_PROMPT,
            context=context if context else "(لا يوجد سياق كافٍ من المراجع - أجب من معرفتك الطبية)",
            question=history_context,
        )

        sources = []
        seen = set()
        for doc in docs:
            file_name = doc.metadata.get('source', 'غير معروف')
            doc_category = doc.metadata.get('category')
            key = (file_name, doc_category)
            if key not in seen:
                seen.add(key)
                sources.append({"file": file_name, "category": doc_category})

        return prompt, sources, grounded, context

    def ask(self, question, history=None):
        if self._is_identity_question(question):
            return IDENTITY_ANSWER + NON_BOOK_SOURCE_NOTE, []

        if self._is_greeting(question):
            return GREETING_ANSWER + NON_BOOK_SOURCE_NOTE, []

        if not self.vectorstore:
            return "⚠️ النظام غير جاهز. لم يتم رفع ملفات PDF بعد.", []

        topic, search_queries = self._classify_topic(question, history=history)
        if topic == "خارج_التخصص":
            return OUT_OF_SCOPE_ANSWER, []

        try:
            prompt, sources, grounded, context = self._build_prompt(
                question, category=topic, search_queries=search_queries, history=history
            )
            raw_answer = self.llm._call(prompt)
            was_truncated = _has_cjk(raw_answer)
            answer = raw_answer
            if was_truncated:
                answer += TRUNCATED_NOTE

            if grounded and USE_AUTO_VERIFICATION:
                answer, flagged = _auto_verify_grounding(answer, context)
                _log_auto_verify_debug(flagged)

            has_general_parts = _answer_has_general_knowledge_note(answer)

            if grounded:
                answer += format_source_footer(sources, has_general_parts)
                answer += MEDICAL_DISCLAIMER
                return answer, sources
            else:
                answer += GENERAL_KNOWLEDGE_NOTE
                answer += MEDICAL_DISCLAIMER
                return answer, []
        except Exception as e:
            # كل مفاتيح Gemini فشلت (كوتة خلصت في الكل)، أو أي إيرور تاني غير متوقع
            # - منورّيهوش خام للمستخدم، بنطلعله رسالة الصيانة بس، والإيرور الحقيقي بيتطبع هنا للمتابعة
            print(f"❌ خطأ في ask(): {e}")
            return MAINTENANCE_MESSAGE, []

    def ask_stream(self, question, history=None):
        """
        توليد رد مع مراعاة سياق المحادثة السابق
        """
        if self._is_identity_question(question):
            yield {"type": "chunk", "text": IDENTITY_ANSWER + NON_BOOK_SOURCE_NOTE}
            yield {"type": "done", "sources": [], "raw_answer": "", "category": None}
            return

        if self._is_greeting(question):
            yield {"type": "chunk", "text": GREETING_ANSWER + NON_BOOK_SOURCE_NOTE}
            yield {"type": "done", "sources": [], "raw_answer": "", "category": None}
            return

        if not self.vectorstore:
            yield {"type": "chunk", "text": "⚠️ النظام غير جاهز. لم يتم رفع ملفات PDF بعد."}
            yield {"type": "done", "sources": [], "raw_answer": "", "category": None}
            return

        topic, search_queries = self._classify_topic(question, history=history)
        if topic == "خارج_التخصص":
            yield {"type": "chunk", "text": OUT_OF_SCOPE_ANSWER}
            yield {"type": "done", "sources": [], "raw_answer": "", "category": None}
            return

        yield {"type": "meta", "category": topic}

        try:
            prompt, sources, grounded, context = self._build_prompt(
                question, category=topic, search_queries=search_queries, history=history
            )

            raw_answer = ""
            token_count = 0
            for token in self.llm.stream_call(prompt):
                token_count += 1
                raw_answer += token
                yield {"type": "chunk", "text": token}

            if token_count == 0:
                # كل مفاتيح Gemini رجّعت رد فاضي من غير ما ترفع إيرور
                yield {"type": "chunk", "text": MAINTENANCE_MESSAGE}
                yield {"type": "done", "sources": [], "raw_answer": "", "category": topic}
                return

            verified_answer = raw_answer
            auto_verify_supplement = ""
            if grounded and USE_AUTO_VERIFICATION:
                verified_answer, flagged = _auto_verify_grounding(raw_answer, context)
                _log_auto_verify_debug(flagged)
                auto_verify_supplement = _build_auto_verify_supplement(flagged)

            has_general_parts = _answer_has_general_knowledge_note(verified_answer)

            # === إضافة ملخص للردود الطويلة ===
            summary = ""
            if len(verified_answer.split()) > 300:
                # استخرج أول 3 جمل كملخص
                sentences = verified_answer.split('.')
                summary_sentences = [s.strip() for s in sentences[:3] if s.strip()]
                if summary_sentences:
                    summary = "\n\n---\n📋 **ملخص سريع:** " + ". ".join(summary_sentences[:2]) + "."

            if grounded:
                if auto_verify_supplement:
                    yield {"type": "chunk", "text": auto_verify_supplement}
                footer = format_source_footer(sources, has_general_parts)
                if footer:
                    yield {"type": "chunk", "text": footer}
                if summary:
                    yield {"type": "chunk", "text": summary}
                yield {"type": "chunk", "text": MEDICAL_DISCLAIMER}
                yield {"type": "done", "sources": sources, "raw_answer": verified_answer, "category": topic}
            else:
                if summary:
                    yield {"type": "chunk", "text": summary}
                yield {"type": "chunk", "text": GENERAL_KNOWLEDGE_NOTE}
                yield {"type": "chunk", "text": MEDICAL_DISCLAIMER}
                yield {"type": "done", "sources": [], "raw_answer": raw_answer, "category": topic}
        except Exception as e:
            # كل مفاتيح Gemini فشلت (كوتة خلصت في الكل)، أو أي إيرور تاني غير متوقع
            print(f"❌ خطأ في ask_stream(): {e}")
            yield {"type": "chunk", "text": MAINTENANCE_MESSAGE}
            yield {"type": "done", "sources": [], "raw_answer": "", "category": None}

# تهيئة نظام RAG
rag_system = SportsMedicineRAG()
rag_system.load_existing_vectorstore()

stop_flags = {}

# ==================== حراسة الحسابات المحذوفة ====================
PUBLIC_ENDPOINTS = {'login', 'register', 'static'}


@app.before_request
def enforce_account_still_active():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return

    user_id = session.get('user_id')
    if user_id is None:
        return

    if not user_still_exists(user_id):
        session.clear()

        if request.path.startswith('/api/'):
            return jsonify({
                'error': 'account_deleted',
                'message': 'تم حذف حسابك نهائيًا من المنصة من قبل الإدارة.'
            }), 401

        return redirect(url_for('login', deleted='1'))

# ==================== Routes ====================
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('chat'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.args.get('deleted') == '1':
        error = 'تم حذف حسابك نهائيًا من المنصة من قبل الإدارة. لو تعتقد إن ده حصل بالغلط، تواصل مع الدعم.'

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        user = verify_user(email, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('chat'))
        else:
            return render_template('index.html', error='بيانات غير صحيحة')

    return render_template('index.html', error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        if not username or not email or not password:
            return render_template('register.html', error='من فضلك املأ كل الحقول')
        if len(password) < 6:
            return render_template('register.html', error='كلمة المرور يجب أن تكون 6 أحرف على الأقل')
        if '@' not in email or '.' not in email.split('@')[-1]:
            return render_template('register.html', error='البريد الإلكتروني غير صحيح')

        if create_user(username, email, password):
            return redirect(url_for('login'))
        else:
            return render_template('register.html', error='البريد الإلكتروني أو اسم المستخدم مستخدم بالفعل')

    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/chat')
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    return render_template('chat.html', username=session['username'])

@app.route('/api/conversations', methods=['GET'])
def get_conversations():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    conversations = get_user_conversations(session['user_id'])
    return jsonify({
        'conversations': [
            {'id': c[0], 'title': c[1], 'created_at': c[2]}
            for c in conversations
        ]
    })

@app.route('/api/conversations', methods=['POST'])
def create_new_conversation():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    conv_id = create_conversation(session['user_id'])
    return jsonify({'id': conv_id, 'title': 'محادثة جديدة'})

@app.route('/api/conversations/<conv_id>/messages', methods=['GET'])
def get_messages(conv_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if not verify_conversation_owner(conv_id, session['user_id']):
        return jsonify({'error': 'Forbidden'}), 403

    messages = get_conversation_messages(conv_id)
    return jsonify({
        'messages': [
            {'id': m[0], 'role': m[1], 'content': m[2], 'sources': m[3], 'created_at': m[4]}
            for m in messages
        ]
    })

def _stream_and_save(conv_id, question, user_message_id):
    def generate():
        full_answer = ""
        sources_list = []
        raw_answer = ""
        category = None
        was_stopped = False

        messages = get_conversation_messages(conv_id)
        history = []
        for msg in messages[-15:]:
            history.append({
                'role': msg[1],
                'content': msg[2]
            })

        for event in rag_system.ask_stream(question, history=history):
            if stop_flags.get(conv_id):
                was_stopped = True
                break

            if event["type"] == "meta":
                category = event.get("category")
                continue

            if event["type"] == "chunk":
                full_answer += event["text"]
                payload = json.dumps({"type": "chunk", "text": event["text"]}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            elif event["type"] == "done":
                sources_list = event["sources"]
                raw_answer = event.get("raw_answer", "")
                category = event.get("category", category)

        stop_flags.pop(conv_id, None)

        if was_stopped:
            full_answer += STOPPED_NOTE
            stop_payload = json.dumps({"type": "chunk", "text": STOPPED_NOTE}, ensure_ascii=False)
            yield f"data: {stop_payload}\n\n"

        assistant_msg_id = save_message(conv_id, 'assistant', full_answer, json.dumps(sources_list, ensure_ascii=False))

        if not was_stopped and category and raw_answer.strip():
            save_training_pair(conv_id, question, raw_answer.strip(), category, sources_list)

        messages = get_conversation_messages(conv_id)
        if len(messages) == 2:
            update_conversation_title(conv_id, question[:30] + "...")

        done_payload = json.dumps({
            "type": "done",
            "sources": sources_list,
            "stopped": was_stopped,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_msg_id,
        }, ensure_ascii=False)
        yield f"data: {done_payload}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )

@app.route('/api/conversations/<conv_id>/messages', methods=['POST'])
def send_message(conv_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if not verify_conversation_owner(conv_id, session['user_id']):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.json or {}
    question = data.get('question')

    if not question:
        return jsonify({'error': 'Question is required'}), 400

    user_msg_id = save_message(conv_id, 'user', question)
    stop_flags.pop(conv_id, None)

    return _stream_and_save(conv_id, question, user_msg_id)

@app.route('/api/conversations/<conv_id>/stop', methods=['POST'])
def stop_generation(conv_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if not verify_conversation_owner(conv_id, session['user_id']):
        return jsonify({'error': 'Forbidden'}), 403

    stop_flags[conv_id] = True
    return jsonify({'success': True})

@app.route('/api/conversations/<conv_id>/messages/<int:msg_id>', methods=['PUT'])
def edit_message(conv_id, msg_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if not verify_conversation_owner(conv_id, session['user_id']):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.json or {}
    new_question = (data.get('question') or '').strip()
    if not new_question:
        return jsonify({'error': 'Question is required'}), 400

    message_row = get_message(msg_id)
    if not message_row or message_row[1] != conv_id or message_row[2] != 'user':
        return jsonify({'error': 'Message not found'}), 404

    update_message_content(msg_id, new_question)
    delete_messages_after(conv_id, msg_id)
    stop_flags.pop(conv_id, None)

    return _stream_and_save(conv_id, new_question, msg_id)

@app.route('/api/conversations/<conv_id>/messages/<int:msg_id>', methods=['DELETE'])
def delete_single_message(conv_id, msg_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if not verify_conversation_owner(conv_id, session['user_id']):
        return jsonify({'error': 'Forbidden'}), 403

    message_row = get_message(msg_id)
    if not message_row or message_row[1] != conv_id:
        return jsonify({'error': 'Message not found'}), 404

    role = message_row[2]
    deleted_ids = [msg_id]

    if role == 'user':
        next_msg = get_next_message(conv_id, msg_id)
        if next_msg and next_msg[1] == 'assistant':
            delete_message_by_id(next_msg[0])
            deleted_ids.append(next_msg[0])

    delete_message_by_id(msg_id)

    return jsonify({'success': True, 'deleted_ids': deleted_ids})

@app.route('/api/conversations/<conv_id>', methods=['DELETE'])
def delete_conv(conv_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    delete_conversation(conv_id, session['user_id'])
    return jsonify({'success': True})

@app.route('/api/training/export', methods=['GET'])
def export_training_data():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    category = request.args.get('category')
    rows = get_all_training_pairs(category)

    lines = []
    for question, answer, cat, sources_json, created_at in rows:
        try:
            sources = json.loads(sources_json) if sources_json else []
        except Exception:
            sources = []
        lines.append(json.dumps({
            "question": question,
            "answer": answer,
            "category": cat,
            "sources": sources,
            "created_at": created_at,
        }, ensure_ascii=False))

    jsonl_content = "\n".join(lines)

    return Response(
        jsonl_content,
        mimetype='application/jsonl',
        headers={
            'Content-Disposition': 'attachment; filename=training_data.jsonl'
        }
    )

if __name__ == '__main__':
    import sys
    init_db()

    if len(sys.argv) > 1 and sys.argv[1] == 'build':
        rag_system.build_all()
    else:
        debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
        app.run(debug=debug_mode, host='0.0.0.0', port=8000, threaded=True)