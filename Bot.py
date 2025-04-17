import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.methods import DeleteWebhook
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import requests
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from rag_handler import RAGHandler
from rag_trainer import RAGTrainer
import os
from pathlib import Path
import uuid

# Инициализация RAG и RAGTrainer в начале файла
rag = RAGHandler()
rag_trainer = RAGTrainer()

GENERAL_QUESTIONS = {
    "как дела": "🌟 Всё отлично, спасибо! Готов помочь с вопросами о питании и здоровье! 😊",
    "кто ты": "🤖 Я бот-нутрициолог, ароматерапевт, созданный, чтобы помогать с вопросами о здоровом питании, образе жизни, эфирных маслах БАДах. Задай мне любой вопрос! 📝",
    "что ты умеешь": "🌟 Я умею отвечать на вопросы о питании, здоровье, БАДах, анализировать фото еды и давать полезные советы. Просто спроси! 😊",
    "кто тебя создал": "🚀 Меня создала команда AromaInc, чтобы я помогал людям заботиться о своём здоровье! 😄"
}

# Ключевые слова для общих вопросов
GENERAL_KEYWORDS = [
    "как дела", "кто ты", "что ты", "что умеешь",
    "кто создал", "как настроение", "чем занимаешься", "расскажи о себе"
]

# Конфигурация
TOKEN = '7705327980:AAHxGu09YYsvDsrjq_Ff-bGg-l4bb7x3wRU'
ADMIN_ID = 753655653
DATABASE_NAME = 'nutrition_bot.db'
LOCAL_LLM_URL = "http://localhost:11434/api/generate"

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    timestamp DATETIME NOT NULL,
                    question TEXT NOT NULL,
                    bot_answer TEXT,
                    expert_answer TEXT,
                    is_approved BOOLEAN DEFAULT 0,
                    is_edited BOOLEAN DEFAULT 0,
                    feedback TEXT)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS learning_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    approved_answer TEXT NOT NULL,
                    usage_count INTEGER DEFAULT 1,
                    last_used DATETIME)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registration_date DATETIME,
                    messages_count INTEGER DEFAULT 1)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    subscription_type TEXT NOT NULL,
                    start_date DATETIME NOT NULL,
                    end_date DATETIME NOT NULL,
                    payment_amount REAL NOT NULL,
                    payment_status TEXT DEFAULT 'pending',
                    payment_id TEXT)''')

    conn.commit()
    conn.close()

init_db()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Временное хранилище запросов
pending_requests = {}

# Состояния FSM
class AdminEditing(StatesGroup):
    waiting_for_edit = State()
    waiting_for_ai_refinement = State()
    waiting_for_new_query = State()

class SubscriptionStates(StatesGroup):
    waiting_for_payment = State()

class AdminStates(StatesGroup):
    waiting_for_training_file = State()
    waiting_for_test_query = State()

@dp.message(SubscriptionStates.waiting_for_payment)
async def process_subscription_choice(message: types.Message, state: FSMContext):
    sub_options = {
        "1 месяц - 299 руб": {"type": "1_month", "price": 299, "days": 30, "desc": "1 месяц"},
        "3 месяца - 799 руб": {"type": "3_month", "price": 799, "days": 90, "desc": "3 месяца"},
        "12 месяцев - 2499 руб": {"type": "12_month", "price": 2499, "days": 365, "desc": "12 месяцев"}
    }

    if message.text == "Назад":
        await handle_back(message, state)
        return
    elif message.text == "Стоп":
        await handle_stop(message, state)
        return
    elif message.text not in sub_options:
        await message.answer("Пожалуйста, выберите один из предложенных вариантов подписки.")
        return

    # Получаем данные выбранной подписки
    sub_info = sub_options[message.text]
    payment_id = f"sub_{message.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Сохраняем информацию о подписке в базе данных
    save_subscription(
        user_id=message.from_user.id,
        sub_type=sub_info["type"],
        amount=sub_info["price"],
        payment_id=payment_id,
        duration_days=sub_info["days"]
    )

    # Отправляем "окно оплаты" с инструкцией и кнопкой подтверждения
    await message.answer(
        f"Вы выбрали подписку: {sub_info['desc']}\n"
        f"Стоимость: {sub_info['price']} руб.\n\n"
        "Для оплаты переведите указанную сумму на наш кошелек (в реальном боте здесь будет ссылка на платеж).\n"
        "После оплаты нажмите кнопку ниже для подтверждения:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"confirm_pay_{payment_id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_payment")]
        ])
    )

    # Сохраняем payment_id в состоянии для дальнейшей проверки
    await state.update_data(payment_id=payment_id)

#========== Функции работы с БД ==========
def save_conversation(user_id, question, bot_answer, expert_answer=None, is_approved=False, is_edited=False):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    cursor.execute('''INSERT OR IGNORE INTO users 
                    (user_id, username, first_name, last_name, registration_date) 
                    VALUES (?, ?, ?, ?, ?)''',
                   (user_id, "", "", "", datetime.now().isoformat()))

    cursor.execute('''UPDATE users SET messages_count = messages_count + 1 
                    WHERE user_id = ?''', (user_id,))

    cursor.execute('''INSERT INTO conversations 
                    (user_id, timestamp, question, bot_answer, expert_answer, is_approved, is_edited) 
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                   (user_id, datetime.now().isoformat(), question, bot_answer,
                    expert_answer, is_approved, is_edited))

    conn.commit()
    conn.close()

def update_learning_data(question, approved_answer):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    cursor.execute('''SELECT id, usage_count FROM learning_data 
                    WHERE question = ? LIMIT 1''', (question,))
    result = cursor.fetchone()

    if result:
        cursor.execute('''UPDATE learning_data 
                        SET approved_answer = ?, usage_count = ?, last_used = ?
                        WHERE id = ?''',
                       (approved_answer, result[1] + 1, datetime.now().isoformat(), result[0]))
    else:
        cursor.execute('''INSERT INTO learning_data 
                        (question, approved_answer, last_used) 
                        VALUES (?, ?, ?)''',
                       (question, approved_answer, datetime.now().isoformat()))

    conn.commit()
    conn.close()

def get_learning_data(question):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    cursor.execute('''SELECT approved_answer FROM learning_data 
                    WHERE question LIKE ? 
                    ORDER BY usage_count DESC, last_used DESC 
                    LIMIT 1''', (f"%{question}%",))

    result = cursor.fetchone()
    conn.close()

    return result[0] if result else None

async def check_subscription(user_id: int) -> bool:
    """Проверяет, есть ли у пользователя активная подписка"""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    cursor.execute('''SELECT end_date FROM subscriptions 
                    WHERE user_id = ? AND end_date > ? AND payment_status = 'success'
                    ORDER BY end_date DESC LIMIT 1''',
                   (user_id, datetime.now().isoformat()))

    result = cursor.fetchone()
    conn.close()

    return result is not None

def save_subscription(user_id: int, sub_type: str, amount: float, payment_id: str, duration_days: int):
    """Сохраняет информацию о подписке в базу данных"""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    start_date = datetime.now()
    end_date = start_date + timedelta(days=duration_days)

    cursor.execute('''INSERT INTO subscriptions 
                    (user_id, subscription_type, start_date, end_date, payment_amount, payment_status, payment_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                   (user_id, sub_type, start_date.isoformat(), end_date.isoformat(),
                    amount, 'pending', payment_id))

    conn.commit()
    conn.close()

# ========== Вспомогательные функции ==========
async def send_media_with_caption(chat_id, file_id, caption, is_photo, reply_to_message_id=None, reply_markup=None):
    max_caption_length = 1024

    if len(caption) <= max_caption_length:
        if is_photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup
            )
        else:
            await bot.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup
            )
    else:
        if is_photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                reply_to_message_id=reply_to_message_id,
                reply_markup=None
            )
        else:
            await bot.send_video(
                chat_id=chat_id,
                video=file_id,
                reply_to_message_id=reply_to_message_id,
                reply_markup=None
            )

        for i in range(0, len(caption), 4096):
            part = caption[i:i + 4096]
            await bot.send_message(
                chat_id=chat_id,
                text=part,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup if i + 4096 >= len(caption) else None
            )


async def generate_ai_response(prompt):
    # Проверяем, является ли запрос общим
    prompt_lower = prompt.lower().strip()

    # Проверка на точное совпадение с общими вопросами
    if prompt_lower in GENERAL_QUESTIONS:
        return GENERAL_QUESTIONS[prompt_lower]

    # Проверка на ключевые слова
    if any(keyword in prompt_lower for keyword in GENERAL_KEYWORDS):
        return "😊 Кажется, ты хочешь поболтать! Я бот-нутрициолог, готов ответить на любые вопросы о питании, здоровье или БАДах. Задай что-нибудь интересное! 🌟"

    try:
        def local_llm_generate(prompt_text):
            try:
                headers = {"Content-Type": "application/json"}
                data = {
                    # "model": "llama3.1:8b",
                    "model": "llama3.1",
                    "prompt": prompt_text,
                    "system": (
                        "Ты - профессиональный нутрициолог,ароматерапевт помощник Татьяны Николаевны, знаешь всё о биологически активных добавках и эфирных маслах, с глубокими знаниями в области питания и здоровья, биологически активных добавок и эфирных масел. "
                        "Отвечай ТОЛЬКО на русском языке. "
                        "Если запрос связан с питанием, здоровьем, эфирными масласми или БАДами, анализируй контекст и давай развернутые, полезные ответы. "
                        "Если запрос не относится к питанию или здоровью, дай краткий, вежливый ответ без рекомендаций по БАДам и эфирным маслам. "
                        "Оформляй ответы красиво с эмодзи (🌟, 📝, ✅) и заголовками только для тематических вопросов. "
                        "Если в запросе или ответе упоминаются БАДы (биологически активные добавки) или ЭМ (эфирные масла), всегда указывай их ПОЛНОЕ настоящее название как оно есть, "
                        "избегая сокращений или общих фраз вроде 'витаминный комплекс' без конкретики."
                    ),
                    "stream": False,
                    "options": {"temperature": 0.7, "max_tokens": 3000}
                }
                response = requests.post(LOCAL_LLM_URL, headers=headers, json=data)
                response.raise_for_status()
                return response.json().get("response", "Не удалось получить ответ от модели.")
            except Exception as e:
                logger.error(f"Ошибка при генерации ответа AI: {e}")
                return "Извините, я не смог обработать ваш запрос из-за технической ошибки."

        rag_response = rag.generate_rag_response(prompt, local_llm_generate)
        if "Недостаточно информации" not in rag_response:
            rag.add_to_knowledge_base(prompt, rag_response)
        return rag_response
    except Exception as e:
        logger.error(f"Ошибка при генерации ответа с RAG: {e}")
        return "Извините, я не смог найти информацию по вашему запросу."

# ========== Обработчики команд ==========

@dp.message(lambda message: message.text in ["Статистика", "Статистика RAG", "Статистика обучения"])
async def handle_admin_stats_buttons(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    logger.info(f"Админ {message.from_user.id} нажал кнопку: {message.text}")
    try:
        if message.text == "Статистика":
            await cmd_stats(message)
        elif message.text == "Статистика RAG":
            await cmd_rag_stats(message)
        elif message.text == "Статистика обучения":
            await cmd_training_stats(message)
    except Exception as e:
        logger.error(f"Ошибка при обработке статистики ({message.text}): {e}")
        await message.answer(f"⚠️ Ошибка при получении статистики: {e}")

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        admin_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Обучение"), KeyboardButton(text="Статистика")],
                [KeyboardButton(text="Статистика RAG"), KeyboardButton(text="Статистика обучения")]
            ],
            resize_keyboard=True
        )
        await message.answer(
            "Вы вошли как администратор. Доступные команды:",
            reply_markup=admin_keyboard
        )
    else:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать диалог")],
                [KeyboardButton(text="Оплатить подписку")]
            ],
            resize_keyboard=True
        )
        await message.answer(
            'Привет! Я бот-нутрициолог. Задайте мне вопрос о питании и здоровье...',
            reply_markup=keyboard
        )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    try:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM users')
        users_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM conversations')
        conversations_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM learning_data')
        learning_items = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM conversations WHERE is_edited = 1')
        edited_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM subscriptions WHERE payment_status = "success"')
        active_subs = cursor.fetchone()[0]

        cursor.execute('''SELECT user_id, first_name, last_name, messages_count 
                        FROM users 
                        ORDER BY messages_count DESC 
                        LIMIT 5''')
        top_users = cursor.fetchall()

        cursor.execute('''SELECT question, MAX(timestamp) as last_time
                        FROM conversations
                        GROUP BY question
                        ORDER BY last_time DESC
                        LIMIT 5''')
        recent_questions = cursor.fetchall()

        conn.close()

        stats_text = [
            "📊 <b>Статистика бота</b>",
            f"👥 <b>Пользователей:</b> {users_count}",
            f"💬 <b>Диалогов:</b> {conversations_count}",
            f"🧠 <b>База знаний:</b> {learning_items} записей",
            f"✏️ <b>Отредактировано ответов:</b> {edited_count}",
            f"💰 <b>Активных подписок:</b> {active_subs}",
            "",
            "🏆 <b>Топ-5 активных пользователей:</b>"
        ]

        for i, (user_id, first_name, last_name, count) in enumerate(top_users, 1):
            name = f"{first_name} {last_name}" if first_name or last_name else f"ID: {user_id}"
            stats_text.append(f"{i}. {name} - {count} сообщ.")

        stats_text.extend([
            "",
            "🕒 <b>Последние 5 уникальных запросов:</b>"
        ])

        for i, (question, timestamp) in enumerate(recent_questions, 1):
            date = datetime.fromisoformat(timestamp).strftime("%d.%m %H:%M")
            stats_text.append(f"{i}. {date} - {question[:50]}{'...' if len(question) > 50 else ''}")

        await message.answer("\n".join(stats_text), parse_mode="HTML")
        logger.info("Общая статистика успешно отправлена")

    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer("⚠️ Произошла ошибка при получении статистики. Подробности в логах.")

@dp.message(Command("mysub"))
async def cmd_my_subscription(message: types.Message):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    cursor.execute('''SELECT subscription_type, end_date FROM subscriptions 
                    WHERE user_id = ? AND payment_status = 'success'
                    ORDER BY end_date DESC LIMIT 1''',
                   (message.from_user.id,))

    result = cursor.fetchone()
    conn.close()

    if result:
        sub_type, end_date = result
        end_date = datetime.fromisoformat(end_date).strftime("%d.%m.%Y")
        await message.answer(
            f"Ваша подписка: {sub_type.replace('_', ' ')}\n"
            f"Действует до: {end_date}"
        )
    else:
        await message.answer("У вас нет активной подписки.")
        await handle_subscription(message)

# ========== Обработчики сообщений ==========
@dp.message(lambda message: message.text == "Начать диалог")
async def handle_start_dialog(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return

    choice_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Написать боту")],
            [KeyboardButton(text="Написать боту (с экспертом)")],
            [KeyboardButton(text="Стоп")]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Выберите вариант общения:",
        reply_markup=choice_keyboard
    )

@dp.message(lambda message: message.text == "Оплатить подписку")
async def handle_subscription(message: types.Message, state: FSMContext):
    subscription_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1 месяц - 299 руб")],
            [KeyboardButton(text="3 месяца - 799 руб")],
            [KeyboardButton(text="12 месяцев - 2499 руб")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Выберите вариант подписки:",
        reply_markup=subscription_keyboard
    )
    await state.set_state(SubscriptionStates.waiting_for_payment)

@dp.message(lambda message: message.text == "Написать боту")
async def handle_direct_bot(message: types.Message, state: FSMContext):
    await state.update_data(expert_mode=False)
    return_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Вернуться к выбору")],
        ],
        resize_keyboard=True
    )
    await message.answer(
        "Вы выбрали прямой диалог с ботом. Задайте ваш вопрос о питании и здоровье или отправьте фото/видео, и я постараюсь помочь!",
        reply_markup=return_keyboard
    )

@dp.message(lambda message: message.text == "Написать боту (с экспертом)")
async def handle_expert_bot(message: types.Message, state: FSMContext):
    await state.update_data(expert_mode=True)
    return_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Вернуться к выбору")],
        ],
        resize_keyboard=True
    )
    await message.answer(
        "Вы выбрали диалог с проверкой эксперта. Задайте ваш вопрос о питании и здоровье или отправьте фото/видео, "
        "и наш эксперт проверит ответ перед отправкой.",
        reply_markup=return_keyboard
    )

@dp.message(lambda message: message.text == "Вернуться к выбору")
async def handle_return_to_choice(message: types.Message, state: FSMContext):
    await state.clear()
    choice_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Написать боту")],
            [KeyboardButton(text="Написать боту (с экспертом)")],
            [KeyboardButton(text="Стоп")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        "Выберите вариант общения:",
        reply_markup=choice_keyboard
    )

@dp.message(lambda message: message.text == "Назад")
async def handle_back(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        admin_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Обучение"), KeyboardButton(text="Статистика")],
                [KeyboardButton(text="Статистика RAG"), KeyboardButton(text="Статистика обучения")]
            ],
            resize_keyboard=True
        )
        await message.answer(
            "Вы вернулись в меню администратора. Выберите действие:",
            reply_markup=admin_keyboard
        )
    else:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Начать диалог")],
                [KeyboardButton(text="Оплатить подписку")],
                [KeyboardButton(text="Стоп")]
            ],
            resize_keyboard=True
        )
        await message.answer(
            "Вы вернулись в главное меню",
            reply_markup=keyboard
        )

@dp.message(lambda message: message.text == "Стоп")
async def handle_stop(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Диалог остановлен. Чтобы начать заново, нажмите /start",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(Command("test_file"))
async def cmd_test_file(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''SELECT filename FROM training_files ORDER BY processed_at DESC LIMIT 1''')
    last_file = cursor.fetchone()
    conn.close()

    if not last_file:
        await message.answer("Нет загруженных файлов для тестирования.")
        return

    await message.answer(
        f"Тестирование последнего файла: {last_file[0]}\n"
        "Введите запрос для проверки, как бот ответит на основе этого файла:"
    )
    await state.set_state(AdminStates.waiting_for_test_query)

@dp.message(AdminStates.waiting_for_test_query)
async def handle_test_query(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    response = await generate_ai_response(message.text)
    await message.answer(f"Ответ бота:\n{response}")
    await state.clear()
    admin_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Обучение"), KeyboardButton(text="Статистика")],
            [KeyboardButton(text="Статистика RAG"), KeyboardButton(text="Статистика обучения")]
        ],
        resize_keyboard=True
    )
    await message.answer("Тестирование завершено. Выберите действие:", reply_markup=admin_keyboard)

@dp.message(Command("check_knowledge"))
async def cmd_check_knowledge(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    entries = rag.get_recent_entries()
    if not entries:
        await message.answer("База знаний пуста.")
        return

    response = "Последние 5 записей в базе знаний:\n"
    for i, (question, answer, context, last_used, usage_count, is_from_pdf) in enumerate(entries, 1):
        source = "PDF" if is_from_pdf else "Пользователь"
        response += (f"{i}. Источник: {source}\n"
                     f"   Вопрос: {question or 'Нет'}\n"
                     f"   Ответ: {answer or 'Нет'}\n"
                     f"   Контекст: {context[:50] + '...' if context else 'Нет'}\n"
                     f"   Использовано: {usage_count} раз\n")
    await message.answer(response)

@dp.message(lambda message: message.photo or message.video)
async def handle_media(message: types.Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Вы администратор. Вы можете только редактировать ответы бота.")
        return

    user_data = await state.get_data()
    expert_mode = user_data.get('expert_mode', False)

    if message.photo:
        media_type = "фото"
        photo = message.photo[-1]
        media_info = f"Размер: {photo.width}x{photo.height}"
        file_id = photo.file_id
        is_photo = True
    else:
        media_type = "видео"
        video = message.video
        media_info = f"Длительность: {video.duration} сек, размер: {video.width}x{video.height}"
        file_id = video.file_id
        is_photo = False

    caption = message.caption if message.caption else "без описания"

    prompt = (f"Пользователь отправил {media_type} ({media_info}) с описанием: '{caption}'. "
              f"Как нутрициолог, дай рекомендации по этому контенту. "
              f"Если это фото еды, проанализируй ее состав и пользу. "
              f"Если это видео, дай общие рекомендации по его содержанию. "
              f"Если контент не относится к питанию, вежливо сообщи об этом.")

    bot_text = await generate_ai_response(prompt)
    if not bot_text:
        await message.answer("Произошла ошибка при обработке вашего медиа. Пожалуйста, попробуйте позже.")
        return

    pending_requests[message.from_user.id] = {
        "question": f"{media_type} ({caption})",
        "answer": bot_text,
        "file_id": file_id,
        "is_photo": is_photo,
        "chat_id": message.chat.id,
        "message_id": message.message_id
    }

    save_conversation(
        user_id=message.from_user.id,
        question=f"{media_type} ({caption})",
        bot_answer=bot_text,
        is_approved=not expert_mode
    )

    if not expert_mode:
        return_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Вернуться к выбору")],
            ],
            resize_keyboard=True
        )

        await send_media_with_caption(
            chat_id=message.chat.id,
            file_id=file_id,
            caption=bot_text,
            is_photo=is_photo,
            reply_to_message_id=message.message_id,
            reply_markup=return_keyboard
        )

        update_learning_data(f"{media_type} ({caption})", bot_text)
        return

    if is_photo:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=file_id,
            caption=f"📸 Новое фото от пользователя {message.from_user.id}\n\nОписание: {caption}"
        )
    else:
        await bot.send_video(
            chat_id=ADMIN_ID,
            video=file_id,
            caption=f"🎥 Новое видео от пользователя {message.from_user.id}\n\nОписание: {caption}"
        )

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📨 Новый запрос от пользователя:\n\n"
             f"👤 User ID: {message.from_user.id}\n"
             f"📝 Вопрос: {caption}\n\n"
             f"🤖 Ответ Llama 3:\n{bot_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить как есть", callback_data=f"approve_{message.from_user.id}")],
            [InlineKeyboardButton(text="Редактировать", callback_data=f"edit_options_{message.from_user.id}")],
            [InlineKeyboardButton(text="Новый запрос (нейросеть)", callback_data=f"new_query_{message.from_user.id}")],
            [InlineKeyboardButton(text="Индивидуальная консультация", callback_data=f"consultation_{message.from_user.id}")]
        ])
    )

    await message.answer("Ваш запрос отправлен эксперту на проверку. Мы уведомим вас, когда эксперт проверит ответ.")
    await state.set_state(AdminEditing.waiting_for_edit)

@dp.message(lambda message: message.text == "Обучение" and message.from_user.id == ADMIN_ID)
async def handle_training_button(message: Message):
    training_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Загрузить PDF для обучения")],
            [KeyboardButton(text="Загрузить TXT для обучения")],
            [KeyboardButton(text="Назад")]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Выберите тип файла для обучения бота:",
        reply_markup=training_keyboard
    )

@dp.message(lambda message: message.text in ["Загрузить PDF для обучения", "Загрузить TXT для обучения"]
            and message.from_user.id == ADMIN_ID)
async def handle_training_file_type(message: Message, state: FSMContext):
    file_type = "pdf" if "PDF" in message.text else "txt"
    await state.update_data(training_file_type=file_type)
    await message.answer(
        f"Пожалуйста, загрузите {file_type.upper()} файл для обучения бота.",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(AdminStates.waiting_for_training_file)

@dp.message(AdminStates.waiting_for_training_file)
async def handle_training_file_upload(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text == "Назад":
        await state.clear()
        admin_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Обучение"), KeyboardButton(text="Статистика")],
                [KeyboardButton(text="Статистика RAG"), KeyboardButton(text="Статистика обучения")]
            ],
            resize_keyboard=True
        )
        await message.answer(
            "Вы вернулись в меню администратора. Выберите действие:",
            reply_markup=admin_keyboard
        )
        return

    if not message.document:
        back_keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Назад")]],
            resize_keyboard=True
        )
        await message.answer(
            "Пожалуйста, загрузите файл как документ или нажмите 'Назад'",
            reply_markup=back_keyboard
        )
        return

    user_data = await state.get_data()
    file_type = user_data.get('training_file_type')
    file_name = message.document.file_name
    file_ext = Path(file_name).suffix.lower() if file_name else ''

    if (file_type == "pdf" and file_ext == ".pdf") or (file_type == "txt" and file_ext == ".txt"):
        try:
            result = await rag_trainer.process_training_file(message, bot, generate_qa=False)
            if "Ошибка" in result:
                back_keyboard = ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="Назад")]],
                    resize_keyboard=True
                )
                await message.answer(result, reply_markup=back_keyboard)
        except Exception as e:
            logger.error(f"Ошибка обработки файла: {e}")
            back_keyboard = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Назад")]],
                resize_keyboard=True
            )
            await message.answer(
                f"❌ Произошла ошибка при обработке файла: {e}",
                reply_markup=back_keyboard
            )
    else:
        back_keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Назад")]],
            resize_keyboard=True
        )
        await message.answer(
            f"❌ Неверный тип файла. Ожидается {file_type.upper()}.",
            reply_markup=back_keyboard
        )

    await state.clear()

@dp.message()
async def handle_text_message(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        if message.text in ["Обучение", "Статистика", "Статистика RAG", "Статистика обучения", "Назад"]:
            return
        current_state = await state.get_state()
        if current_state == AdminEditing.waiting_for_ai_refinement.state:
            return await handle_ai_refinement(message, state)
        elif current_state == AdminEditing.waiting_for_edit.state:
            return await handle_admin_edit(message, state)
        elif current_state == AdminEditing.waiting_for_new_query.state:
            return await handle_new_query(message, state)
        else:
            await message.answer("Вы администратор. Вы можете только редактировать ответы бота.")
            return

    if message.text in ["Начать диалог", "Оплатить подписку", "Написать боту", "Написать боту (с экспертом)",
                        "Вернуться к выбору"]:
        return

    user_data = await state.get_data()
    expert_mode = user_data.get('expert_mode', False)

    bot_text = await generate_ai_response(message.text)
    if not bot_text:
        await message.answer("Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже.")
        return

    pending_requests[message.from_user.id] = {
        "question": message.text,
        "answer": bot_text,
        "chat_id": message.chat.id,
        "message_id": message.message_id
    }

    save_conversation(
        user_id=message.from_user.id,
        question=message.text,
        bot_answer=bot_text,
        is_approved=not expert_mode
    )

    if not expert_mode:
        return_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Вернуться к выбору")],
            ],
            resize_keyboard=True
        )

        for i in range(0, len(bot_text), 4096):
            part = bot_text[i:i + 4096]
            await bot.send_message(
                chat_id=message.chat.id,
                text=part,
                reply_to_message_id=message.message_id,
                reply_markup=return_keyboard if i + 4096 >= len(bot_text) else None
            )

        update_learning_data(message.text, bot_text)
        return

    await state.update_data(
        original_message=message.text,
        original_user=message.from_user.id,
        original_chat=message.chat.id,
        original_message_id=message.message_id,
        original_bot_response=bot_text
    )

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📨 Новый запрос от пользователя:\n\n"
             f"👤 User ID: {message.from_user.id}\n"
             f"📝 Вопрос: {message.text}\n\n"
             f"🤖 Ответ Llama 3:\n{bot_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить как есть", callback_data=f"approve_{message.from_user.id}")],
            [InlineKeyboardButton(text="Редактировать", callback_data=f"edit_options_{message.from_user.id}")],
            [InlineKeyboardButton(text="Новый запрос (нейросеть)", callback_data=f"new_query_{message.from_user.id}")],
                [InlineKeyboardButton(text="Индивидуальная консультация", callback_data=f"consultation_{message.from_user.id}")]
        ])
    )

    await message.answer("Ваш запрос отправлен эксперту на проверку. Мы уведомим вас, когда эксперт проверит ответ.")
    await state.set_state(AdminEditing.waiting_for_edit)

# ========== Обработчики колбэков ==========
@dp.callback_query(lambda c: c.data.startswith("sub_"))
async def process_subscription(callback: types.CallbackQuery, state: FSMContext):
    sub_type = callback.data.split("_")[1] + "_" + callback.data.split("_")[2]
    sub_types = {
        "1_month": {"price": 299, "days": 30, "desc": "1 месяц"},
        "3_month": {"price": 799, "days": 90, "desc": "3 месяца"},
        "12_month": {"price": 2499, "days": 365, "desc": "12 месяцев"}
    }

    if sub_type not in sub_types:
        await callback.answer("Неверный тип подписки")
        return

    sub_info = sub_types[sub_type]
    payment_id = f"sub_{callback.from_user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    save_subscription(
        user_id=callback.from_user.id,
        sub_type=sub_type,
        amount=sub_info["price"],
        payment_id=payment_id,
        duration_days=sub_info["days"]
    )

    await callback.message.answer(
        f"Вы выбрали подписку на {sub_info['desc']}. Стоимость: {sub_info['price']} руб.\n\n"
        "Для завершения оплаты нажмите кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплатить подписку", callback_data=f"confirm_pay_{payment_id}")]
        ])
    )

    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("confirm_pay_"))
async def confirm_payment(callback: types.CallbackQuery, state: FSMContext):
    payment_id = callback.data.split("_")[2]

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''SELECT user_id, subscription_type, payment_amount FROM subscriptions 
                      WHERE payment_id = ? AND payment_status = 'pending' ''', (payment_id,))
    result = cursor.fetchone()

    if not result:
        await callback.message.edit_text("Ошибка: платеж не найден или уже обработан.")
        await callback.answer()
        return

    user_id, sub_type, amount = result

    cursor.execute('''UPDATE subscriptions 
                      SET payment_status = 'success' 
                      WHERE payment_id = ?''', (payment_id,))
    conn.commit()
    conn.close()

    await callback.message.edit_text(
        f"✅ Оплата на сумму {amount} руб. успешно подтверждена!\n"
        f"Ваша подписка '{sub_type.replace('_', ' ')}' активирована.\n"
        "Теперь вы можете пользоваться всеми функциями бота!",
        reply_markup=None
    )

    await state.clear()

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать диалог")],
            [KeyboardButton(text="Оплатить подписку")]
        ],
        resize_keyboard=True
    )
    await bot.send_message(
        chat_id=callback.from_user.id,
        text="Вы вернулись в главное меню",
        reply_markup=keyboard
    )

    await callback.answer("Подписка активирована!")

@dp.callback_query(lambda c: c.data.startswith("approve_"))
async def approve_original(callback: types.CallbackQuery):
    try:
        user_id = int(callback.data.split("_")[1])
        request_data = pending_requests.get(user_id)

        if not request_data:
            await callback.answer("Ошибка: данные не найдены.")
            return

        bot_text = request_data["answer"]
        chat_id = request_data["chat_id"]
        message_id = request_data["message_id"]

        save_conversation(
            user_id=user_id,
            question=request_data.get("question", ""),
            bot_answer=bot_text,
            expert_answer=bot_text,
            is_approved=True
        )

        update_learning_data(request_data.get("question", ""), bot_text)

        return_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Вернуться к выбору")],
            ],
            resize_keyboard=True
        )

        if "file_id" in request_data:
            await send_media_with_caption(
                chat_id=chat_id,
                file_id=request_data["file_id"],
                caption=bot_text,
                is_photo=request_data.get("is_photo", False),
                reply_to_message_id=message_id,
                reply_markup=return_keyboard
            )
        else:
            for i in range(0, len(bot_text), 4096):
                part = bot_text[i:i + 4096]
                await bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    reply_to_message_id=message_id,
                    reply_markup=return_keyboard if i + 4096 >= len(bot_text) else None
                )

        await callback.message.edit_text(
            text=callback.message.text + "\n\n✅ Ответ отправлен в чат без изменений",
            reply_markup=None
        )
        await callback.answer("Ответ отправлен в чат.")

        del pending_requests[user_id]

    except Exception as e:
        logger.error(f"Ошибка отправки ответа: {e}")
        await callback.answer("Ошибка при отправке сообщения.")

@dp.callback_query(lambda c: c.data.startswith("edit_options_"))
async def edit_options(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    request_data = pending_requests.get(user_id)

    if not request_data:
        await callback.answer("Ошибка: данные не найдены.")
        return

    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Ручная ред.", callback_data=f"edit_{user_id}"),
                InlineKeyboardButton(text="Нейросетевая ред.", callback_data=f"refine_{user_id}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data=f"back_to_main_{user_id}")
            ]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("back_to_main_"))
async def back_to_main(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[3])
    request_data = pending_requests.get(user_id)

    if not request_data:
        await callback.answer("Ошибка: данные не найдены.")
        return

    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить как есть", callback_data=f"approve_{user_id}"),
                InlineKeyboardButton(text="Редактировать", callback_data=f"edit_options_{user_id}"),
                InlineKeyboardButton(text="Новый запрос (нейросеть)", callback_data=f"new_query_{user_id}"),
                InlineKeyboardButton(text="Индивидуальная консультация", callback_data=f"consultation_{user_id}")
            ]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("edit_"))
async def start_editing(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    request_data = pending_requests.get(user_id)

    if not request_data:
        await callback.answer("Ошибка: данные не найдены.")
        return

    await state.update_data(
        editing_user_id=user_id,
        original_question=request_data.get("question", "медиа-контент"),
        original_answer=request_data["answer"],
        file_id=request_data.get("file_id"),
        is_photo=request_data.get("is_photo", False)
    )

    await callback.message.edit_text(
        text=f"✏️ Редактирование ответа для пользователя {user_id}:\n\n"
             f"📝 Вопрос: {request_data.get('question', 'медиа-контент')}\n\n"
             f"🤖 Оригинальный ответ:\n{request_data['answer']}\n\n"
             f"Отправьте ваш исправленный вариант ответа:",
        reply_markup=None
    )

    await callback.answer("Теперь вы можете отправить исправленный ответ.")
    await state.set_state(AdminEditing.waiting_for_edit)

@dp.callback_query(lambda c: c.data.startswith("refine_"))
async def start_ai_refinement(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    request_data = pending_requests.get(user_id)

    if not request_data:
        await callback.answer("Ошибка: данные не найдены.")
        return

    await state.update_data(
        refining_user_id=user_id,
        refining_question=request_data.get("question", "медиа-контент"),
        refining_answer=request_data["answer"],
        refining_file_id=request_data.get("file_id"),
        refining_is_photo=request_data.get("is_photo", False),
        refinement_count=0
    )

    await callback.message.edit_text(
        text=f"🤖 Редакция через нейросеть для пользователя {user_id}:\n\n"
             f"📝 Вопрос: {request_data.get('question', 'медиа-контент')}\n\n"
             f"💬 Текущий ответ:\n{request_data['answer']}\n\n"
             f"Отправьте ваши инструкции для нейросети (что изменить/уточнить в ответе):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отменить редакцию", callback_data=f"cancel_refine_{user_id}")]
        ])
    )

    await callback.answer("Теперь вы можете отправить инструкции для нейросети.")
    await state.set_state(AdminEditing.waiting_for_ai_refinement)

@dp.callback_query(lambda c: c.data.startswith("cancel_refine_"))
async def cancel_ai_refinement(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    request_data = pending_requests.get(user_id)

    if not request_data:
        await callback.answer("Ошибка: данные не найдены.")
        return

    await callback.message.edit_text(
        text=f"📨 Запрос от пользователя:\n\n"
             f"👤 User ID: {user_id}\n"
             f"📝 Вопрос: {request_data.get('question', 'медиа-контент')}\n\n"
             f"🤖 Ответ Llama 3:\n{request_data['answer']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить как есть", callback_data=f"approve_{user_id}")],
            [InlineKeyboardButton(text="Редактировать", callback_data=f"edit_options_{user_id}")],
            [InlineKeyboardButton(text="Новый запрос (нейросеть)", callback_data=f"new_query_{user_id}")],
            [InlineKeyboardButton(text="Индивидуальная консультация", callback_data=f"consultation_{user_id}")]
        ])
    )

    await callback.answer("Редакция через нейросеть отменена.")
    await state.set_state(AdminEditing.waiting_for_edit)

@dp.callback_query(lambda c: c.data.startswith("consultation_"))
async def handle_consultation(callback: types.CallbackQuery):
    try:
        user_id = int(callback.data.split("_")[1])
        request_data = pending_requests.get(user_id)

        if not request_data:
            await callback.answer("Ошибка: данные не найдены.")
            return

        chat_id = request_data["chat_id"]
        message_id = request_data["message_id"]

        # Текст сообщения для пользователя
        consultation_text = (
            "🌟 Для ответа на ваш вопрос требуется индивидуальная консультация!\n\n"
            "📞 Свяжитесь с нашим специалистом (@TaNikBob) для получения персонализированных рекомендаций."
        )

        # Отправка сообщения пользователю
        return_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Вернуться к выбору")],
            ],
            resize_keyboard=True
        )

        if "file_id" in request_data:
            await send_media_with_caption(
                chat_id=chat_id,
                file_id=request_data["file_id"],
                caption=consultation_text,
                is_photo=request_data.get("is_photo", False),
                reply_to_message_id=message_id,
                reply_markup=return_keyboard
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=consultation_text,
                reply_to_message_id=message_id,
                reply_markup=return_keyboard
            )

        # Сохранение в базе данных
        save_conversation(
            user_id=user_id,
            question=request_data.get("question", ""),
            bot_answer=request_data.get("answer", ""),
            expert_answer=consultation_text,
            is_approved=True,
            is_edited=True
        )

        # Обновление базы знаний
        update_learning_data(request_data.get("question", ""), consultation_text)

        # Уведомление администратора
        await callback.message.edit_text(
            text=callback.message.text + "\n\n✅ Пользователю отправлено сообщение об индивидуальной консультации",
            reply_markup=None
        )
        await callback.answer("Сообщение об индивидуальной консультации отправлено.")

        # Удаление запроса из pending_requests
        if user_id in pending_requests:
            del pending_requests[user_id]

    except Exception as e:
        logger.error(f"Ошибка при обработке запроса на консультацию: {e}")
        await callback.answer("Ошибка при отправке сообщения.")

@dp.callback_query(lambda c: c.data.startswith("new_query_"))
async def start_new_query(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    request_data = pending_requests.get(user_id)

    if not request_data:
        await callback.answer("Ошибка: данные не найдены.")
        return

    await state.update_data(
        new_query_user_id=user_id,
        original_question=request_data.get("question", "медиа-контент"),
        original_answer=request_data["answer"],
        file_id=request_data.get("file_id"),
        is_photo=request_data.get("is_photo", False),
        chat_id=request_data.get("chat_id"),
        message_id=request_data.get("message_id")
    )

    await callback.message.edit_text(
        text=f"📝 Новый запрос для пользователя {user_id}:\n\n"
             f"Исходный вопрос: {request_data.get('question', 'медиа-контент')}\n\n"
             f"🤖 Оригинальный ответ:\n{request_data['answer']}\n\n"
             f"Пожалуйста, укажите, что именно должен ответить бот:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отменить", callback_data=f"cancel_new_query_{user_id}")]
        ])
    )

    await callback.answer("Введите новый запрос для нейросети.")
    await state.set_state(AdminEditing.waiting_for_new_query)

@dp.callback_query(lambda c: c.data.startswith("cancel_new_query_"))
async def cancel_new_query(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[3])
    request_data = pending_requests.get(user_id)

    if not request_data:
        await callback.answer("Ошибка: данные не найдены.")
        return

    await callback.message.edit_text(
        text=f"📨 Запрос от пользователя:\n\n"
             f"👤 User ID: {user_id}\n"
             f"📝 Вопрос: {request_data.get('question', 'медиа-контент')}\n\n"
             f"🤖 Ответ Llama 3:\n{request_data['answer']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить как есть", callback_data=f"approve_{user_id}")],
            [InlineKeyboardButton(text="Редактировать", callback_data=f"edit_options_{user_id}")],
            [InlineKeyboardButton(text="Новый запрос (нейросеть)", callback_data=f"new_query_{user_id}")],
            [InlineKeyboardButton(text="Индивидуальная консультация", callback_data=f"consultation_{user_id}")]
        ])
    )

    await callback.answer("Создание нового запроса отменено.")
    await state.clear()

@dp.callback_query(lambda c: c.data.startswith("continue_refine_"))
async def continue_ai_refinement(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    current_answer = data.get("refining_answer", "")

    await callback.message.edit_text(
        text=f"🤖 Продолжение редакции через нейросеть для пользователя {user_id}:\n\n"
             f"💬 Текущий ответ:\n{current_answer}\n\n"
             f"Отправьте ваши инструкции для нейросети (что изменить/уточнить в ответе):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отменить редакцию", callback_data=f"cancel_refine_{user_id}")]
        ])
    )

    await callback.answer("Теперь вы можете отправить новые инструкции для нейросети.")
    await state.set_state(AdminEditing.waiting_for_ai_refinement)

@dp.callback_query(lambda c: c.data == "cancel_payment")
async def cancel_payment(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()

    data = await state.get_data()
    payment_id = data.get("payment_id")
    if payment_id:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute('''DELETE FROM subscriptions WHERE payment_id = ? AND payment_status = 'pending' ''', (payment_id,))
        conn.commit()
        conn.close()

    await callback.message.edit_text(
        "❌ Оплата отменена. Вы можете выбрать другой вариант подписки или вернуться в меню.",
        reply_markup=None
    )

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Начать диалог")],
            [KeyboardButton(text="Оплатить подписку")]
        ],
        resize_keyboard=True
    )
    await bot.send_message(
        chat_id=callback.from_user.id,
        text="Вы вернулись в главное меню",
        reply_markup=keyboard
    )

    await callback.answer("Оплата отменена.")

# ========== Обработчики состояний ==========
async def handle_ai_refinement(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    user_id = data.get("refining_user_id")
    refinement_count = data.get("refinement_count", 0) + 1

    if refinement_count > 5:
        await message.answer("⚠️ Достигнут лимит уточнений (5 раз). Пожалуйста, отправьте ответ вручную.")
        await state.set_state(AdminEditing.waiting_for_edit)
        return

    instructions = message.text
    original_question = data.get("refining_question", "")
    current_answer = data.get("refining_answer", "")

    prompt = (f"Исходный вопрос пользователя: {original_question}\n\n"
              f"Текущий ответ бота: {current_answer}\n\n"
              f"Инструкции эксперта по редактированию ответа: {instructions}\n\n"
              f"Пожалуйста, переформулируй ответ согласно инструкциям, сохраняя профессиональный тон.")

    new_answer = await generate_ai_response(prompt)
    if not new_answer:
        await message.answer("Ошибка при генерации ответа. Пожалуйста, попробуйте еще раз.")
        return

    await state.update_data(
        refining_answer=new_answer,
        refinement_count=refinement_count
    )

    if user_id in pending_requests:
        pending_requests[user_id]["answer"] = new_answer

    await message.answer(
        f"🔄 Ответ после уточнения #{refinement_count}:\n\n{new_answer}\n\n"
        f"Отправьте новые инструкции для дальнейшего уточнения или выберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить ", callback_data=f"approve_{user_id}"),
                InlineKeyboardButton(text="Продолжить ред.", callback_data=f"continue_refine_{user_id}"),
                InlineKeyboardButton(text="Ручная ред.", callback_data=f"edit_{user_id}")
            ]
        ])
    )

@dp.message(AdminEditing.waiting_for_new_query)
async def handle_new_query(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    user_id = data.get("new_query_user_id")

    if not user_id:
        await message.reply("Ошибка: не найден ID пользователя.")
        return

    new_query = message.text
    original_question = data.get("original_question", "")

    prompt = (
        f"Исходный вопрос пользователя: {original_question}\n\n"
        f"Инструкции эксперта: {new_query}\n\n"
        "Ты - профессиональный нутрициолог. На основе инструкций эксперта создай новый ответ. "
        "Ответ должен быть развернутым, полезным, оформленным красиво с эмодзи (🌟, 📝, ✅) и заголовками. "
        "Ориентируйся только на указания эксперта и не используй предыдущий ответ бота."
    )

    new_answer = await generate_ai_response(prompt)
    if not new_answer:
        await message.reply("Ошибка при генерации нового ответа. Пожалуйста, попробуйте еще раз.")
        return

    if user_id in pending_requests:
        pending_requests[user_id]["answer"] = new_answer

    await message.reply(
        f"🔄 Новый ответ для пользователя {user_id}:\n\n"
        f"📝 На основе ваших указаний:\n{new_query}\n\n"
        f"🤖 Ответ нейросети:\n{new_answer}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить", callback_data=f"approve_{user_id}")],
            [InlineKeyboardButton(text="Редактировать", callback_data=f"edit_{user_id}")],
            [InlineKeyboardButton(text="Новый запрос (нейросеть)", callback_data=f"new_query_{user_id}")]
        ])
    )

    await state.clear()

@dp.message(AdminEditing.waiting_for_edit)
async def handle_admin_edit(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    user_id = data.get("editing_user_id")

    if not user_id:
        await message.reply("Ошибка: не найден ID пользователя.")
        return

    request_data = pending_requests.get(user_id, {})
    chat_id = request_data.get("chat_id")
    message_id = request_data.get("message_id")

    return_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Вернуться к выбору")],
        ],
        resize_keyboard=True
    )

    try:
        if data.get("file_id"):
            await send_media_with_caption(
                chat_id=chat_id,
                file_id=data["file_id"],
                caption=message.text,
                is_photo=data.get("is_photo", False),
                reply_to_message_id=message_id,
                reply_markup=return_keyboard
            )
        else:
            for i in range(0, len(message.text), 4096):
                part = message.text[i:i + 4096]
                await bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    reply_to_message_id=message_id,
                    reply_markup=return_keyboard if i + 4096 >= len(message.text) else None
                )

        save_conversation(
            user_id=user_id,
            question=request_data.get("question", ""),
            bot_answer=request_data.get("answer", ""),
            expert_answer=message.text,
            is_approved=True,
            is_edited=True
        )

        update_learning_data(request_data.get("question", ""), message.text)

        await message.reply(f"✅ Отредактированный ответ отправлен в чат для пользователя {user_id}")

        if user_id in pending_requests:
            del pending_requests[user_id]

        await state.clear()

    except Exception as e:
        await message.reply(f"❌ Ошибка при отправке сообщения: {e}")

@dp.message(Command("check_db"))
async def cmd_check_db(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT question, answer, usage_count FROM knowledge_vectors ORDER BY last_used DESC LIMIT 5")
    results = cursor.fetchall()
    conn.close()

    if not results:
        await message.answer("База знаний пуста.")
        return

    response = "Последние 5 записей в базе знаний:\n"
    for i, (question, answer, count) in enumerate(results, 1):
        response += f"{i}. Вопрос: {question[:50]}...\n   Ответ: {answer[:50]}...\n   Использовано: {count} раз\n"
    await message.answer(response)

@dp.message(Command("rag_stats"))
async def cmd_rag_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    try:
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM knowledge_vectors')
        total = cursor.fetchone()[0]

        if total == 0:
            await message.answer("📊 База знаний RAG пуста. Загрузите данные для обучения!")
            conn.close()
            return

        cursor.execute('SELECT COUNT(*) FROM knowledge_vectors WHERE usage_count > 5')
        popular = cursor.fetchone()[0]

        cursor.execute('SELECT question, usage_count FROM knowledge_vectors ORDER BY usage_count DESC LIMIT 5')
        top_questions = cursor.fetchall()

        stats_text = [
            "📊 <b>RAG Статистика</b>",
            f"🧠 <b>Всего векторов:</b> {total}",
            f"🏆 <b>Популярные (использовано > 5):</b> {popular}",
            "",
            "🔝 <b>Топ-5 вопросов:</b>"
        ]

        for i, (question, count) in enumerate(top_questions, 1):
            question_text = question[:50] + "..." if question and len(question) > 50 else question or "Без вопроса"
            stats_text.append(f"{i}. {question_text} - {count} раз")

        conn.close()
        await message.answer("\n".join(stats_text), parse_mode="HTML")
        logger.info("RAG статистика успешно отправлена")

    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных при получении RAG статистики: {e}")
        await message.answer("⚠️ Ошибка базы данных при получении статистики RAG.")
    except Exception as e:
        logger.error(f"Ошибка при получении RAG статистики: {e}")
        await message.answer("⚠️ Произошла ошибка при получении статистики RAG.")

@dp.message(Command("train"))
async def cmd_train(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    training_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Загрузить PDF для обучения")],
            [KeyboardButton(text="Загрузить TXT для обучения")],
            [KeyboardButton(text="Назад")]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Выберите тип файла для обучения бота:",
        reply_markup=training_keyboard
    )

@dp.message(lambda message: message.text in ["Загрузить PDF для обучения", "Загрузить TXT для обучения"])
async def handle_training_file_type(message: Message, state: FSMContext):
    file_type = "pdf" if "PDF" in message.text else "txt"
    await state.update_data(training_file_type=file_type)

    back_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Назад")]],
        resize_keyboard=True
    )

    await message.answer(
        f"Пожалуйста, загрузите {file_type.upper()} файл для обучения бота или нажмите 'Назад'.",
        reply_markup=back_keyboard
    )
    await state.set_state(AdminStates.waiting_for_training_file)

@dp.message(Command("training_stats"))
async def cmd_training_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    try:
        stats = rag_trainer.get_training_stats()
        if stats:
            await message.answer(stats, parse_mode="HTML")
            logger.info("Статистика обучения успешно отправлена")
        else:
            await message.answer("❌ Не удалось получить статистику обучения. База данных пуста или произошла ошибка.")
    except Exception as e:
        logger.error(f"Ошибка при получении статистики обучения: {e}")
        await message.answer("⚠️ Произошла ошибка при получении статистики обучения.")

@dp.message(lambda message: message.text in ["Статистика", "Статистика RAG", "Статистика обучения"])
async def handle_admin_stats_buttons(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return

    if message.text == "Статистика":
        await cmd_stats(message)
    elif message.text == "Статистика RAG":
        await cmd_rag_stats(message)
    elif message.text == "Статистика обучения":
        await cmd_training_stats(message)

# ========== Запуск бота ==========
async def main():
    rag.optimize_knowledge_base()
    await bot(DeleteWebhook(drop_pending_updates=True))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())