import asyncio
import sqlite3
import requests
import base64
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

# ==============================
# НАСТРОЙКИ
# ==============================
TELEGRAM_TOKEN = "TELEGRAM_TOKEN"
GROQ_API_KEY   = "GROQ_API_KEY"
GROQ_MODEL     = "llama-3.3-70b-versatile"
GROQ_VISION    = "meta-llama/llama-4-scout-17b-16e-instruct"
MAX_HISTORY    = 30
DB_PATH        = "bot.db"

# ==============================
# БД
# ==============================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            active_chat INTEGER DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS chats (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            title      TEXT DEFAULT 'Новый чат',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

def db():
    return sqlite3.connect(DB_PATH)

def ensure_user(user_id: int):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()

def get_user(user_id: int):
    with db() as conn:
        row = conn.execute("SELECT user_id, active_chat FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return {"user_id": row[0], "active_chat": row[1]} if row else None

def create_chat(user_id: int) -> int:
    with db() as conn:
        cur = conn.execute("INSERT INTO chats (user_id) VALUES (?)", (user_id,))
        chat_id = cur.lastrowid
        conn.execute("UPDATE users SET active_chat = ? WHERE user_id = ?", (chat_id, user_id))
        conn.commit()
        return chat_id

def close_chat(user_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET active_chat = NULL WHERE user_id = ?", (user_id,))
        conn.commit()

def set_active_chat(user_id: int, chat_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET active_chat = ? WHERE user_id = ?", (chat_id, user_id))
        conn.commit()

def get_chat_history(chat_id: int) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?",
            (chat_id, MAX_HISTORY)
        ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def add_message(chat_id: int, role: str, content: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content)
        )
        if role == "user":
            count = conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
            if count == 1:
                title = content[:40] + ("..." if len(content) > 40 else "")
                conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
        conn.commit()

def get_user_chats(user_id: int) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM chats WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
            (user_id,)
        ).fetchall()
    return [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]

# ==============================
# GROQ
# ==============================
GROQ_HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json",
}

def ask_groq(messages: list) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=GROQ_HEADERS,
        json={"model": GROQ_MODEL, "messages": messages, "max_tokens": 1024},
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    return f"[Groq ошибка {resp.status_code}]: {resp.text}"

def ask_groq_vision(image_b64: str, mime: str, prompt: str, history: list) -> str:
    messages = history + [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            {"type": "text", "text": prompt or "Опиши что на изображении."}
        ]
    }]
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=GROQ_HEADERS,
        json={"model": GROQ_VISION, "messages": messages, "max_tokens": 1024},
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    return f"[Groq vision ошибка {resp.status_code}]: {resp.text}"

def download_file(url: str) -> bytes:
    return requests.get(url, timeout=30).content

# ==============================
# UI
# ==============================
def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    chats = get_user_chats(user_id)
    buttons = []
    for chat in chats:
        dt = chat["created_at"][:16]
        buttons.append([InlineKeyboardButton(
            text=f"💬 {chat['title']} ({dt})",
            callback_data=f"open:{chat['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Новый чат", callback_data="new")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def send_reply(message: Message, text: str):
    for i in range(0, len(text), 4095):
        await message.answer(text[i:i+4095])

# ==============================
# BOT
# ==============================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
@dp.message(Command("history"))
async def cmd_start(message: Message):
    ensure_user(message.from_user.id)
    await message.answer("Выбери чат или начни новый:", reply_markup=main_menu_keyboard(message.from_user.id))

@dp.message(Command("new"))
async def cmd_new(message: Message):
    ensure_user(message.from_user.id)
    create_chat(message.from_user.id)
    await message.answer("✅ Новый чат создан! Пиши — я отвечу.\n\n/close — закрыть чат\n/history — список чатов")

@dp.message(Command("close"))
async def cmd_close(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user or not user["active_chat"]:
        await message.answer("У тебя нет активного чата.")
        return
    close_chat(user_id)
    await message.answer("Чат закрыт.", reply_markup=main_menu_keyboard(user_id))

@dp.callback_query(F.data == "new")
async def cb_new(call: CallbackQuery):
    ensure_user(call.from_user.id)
    create_chat(call.from_user.id)
    await call.message.edit_text("✅ Новый чат создан! Пиши — я отвечу.\n\n/close — закрыть чат\n/history — список чатов")
    await call.answer()

@dp.callback_query(F.data.startswith("open:"))
async def cb_open(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    set_active_chat(call.from_user.id, chat_id)
    count = len(get_chat_history(chat_id))
    await call.message.edit_text(f"✅ Чат открыт ({count} сообщений). Продолжай!\n\n/close — закрыть чат")
    await call.answer()

@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    user = get_user(user_id)
    if not user["active_chat"]:
        await message.answer("Сначала выбери или создай чат:", reply_markup=main_menu_keyboard(user_id))
        return

    chat_id = user["active_chat"]
    caption = message.caption or ""
    await bot.send_chat_action(message.chat.id, "typing")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
    image_bytes = download_file(url)
    image_b64 = base64.b64encode(image_bytes).decode()

    history = get_chat_history(chat_id)
    answer = ask_groq_vision(image_b64, "image/jpeg", caption, history)

    add_message(chat_id, "user", f"[Фото] {caption}")
    add_message(chat_id, "assistant", answer)
    await send_reply(message, answer)

@dp.message(F.document)
async def handle_document(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    user = get_user(user_id)
    if not user["active_chat"]:
        await message.answer("Сначала выбери или создай чат:", reply_markup=main_menu_keyboard(user_id))
        return

    chat_id = user["active_chat"]
    caption = message.caption or ""
    doc = message.document
    mime = doc.mime_type or ""
    await bot.send_chat_action(message.chat.id, "typing")

    file = await bot.get_file(doc.file_id)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"
    file_bytes = download_file(url)

    if mime.startswith("image/"):
        image_b64 = base64.b64encode(file_bytes).decode()
        history = get_chat_history(chat_id)
        answer = ask_groq_vision(image_b64, mime, caption, history)

    elif mime.startswith("text/") or (doc.file_name or "").endswith((".txt", ".py", ".js", ".ts", ".json", ".md", ".csv", ".html", ".css")):
        file_text = file_bytes.decode("utf-8", errors="replace")
        prompt = f"{caption}\n\nСодержимое файла «{doc.file_name}»:\n{file_text[:8000]}"
        history = get_chat_history(chat_id)
        history.append({"role": "user", "content": prompt})
        answer = ask_groq(history)

    else:
        answer = f"Файл `{doc.file_name}` ({mime}) получен, но этот формат не поддерживается."

    add_message(chat_id, "user", f"[Файл: {doc.file_name}] {caption}")
    add_message(chat_id, "assistant", answer)
    await send_reply(message, answer)

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    user = get_user(user_id)
    if not user["active_chat"]:
        await message.answer("Сначала выбери или создай чат:", reply_markup=main_menu_keyboard(user_id))
        return

    chat_id = user["active_chat"]
    await bot.send_chat_action(message.chat.id, "typing")

    history = get_chat_history(chat_id)
    history.append({"role": "user", "content": message.text})
    answer = ask_groq(history)

    add_message(chat_id, "user", message.text)
    add_message(chat_id, "assistant", answer)
    await send_reply(message, answer)

# ==============================
# ЗАПУСК
# ==============================
async def main():
    init_db()
    print("=" * 45)
    print("  Telegram AI Bot — aiogram + Groq")
    print("=" * 45)
    print("Бот запущен. Ctrl+C для остановки.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
