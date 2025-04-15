import os
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, List
from aiogram import Bot
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
import pdfplumber
import sqlite3
import logging
import requests
from nltk.tokenize import sent_tokenize
import nltk

# Загружаем ресурсы для NLTK
try:
    nltk.data.find('tokenizers/punkt_tab')
except LookupError:
    nltk.download('punkt_tab', quiet=True)

logger = logging.getLogger(__name__)

class RAGTrainer:
    def __init__(self, db_path: str = 'nutrition_bot.db', llm_url: str = "http://localhost:11434/api/generate"):
        self.db_path = db_path
        self.llm_url = llm_url
        self._init_db()
        logger.info("RAGTrainer инициализирован с базой данных: %s", db_path)

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS training_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename TEXT NOT NULL,
                        file_type TEXT NOT NULL,
                        processed_at DATETIME NOT NULL,
                        chunks_count INTEGER NOT NULL)''')
        conn.commit()
        conn.close()
        logger.info("Таблица training_files инициализирована")

    async def process_training_file(self, message: Message, bot: Bot, generate_qa=False) -> str:
        logger.info("Начата обработка файла: %s (ID: %s)", message.document.file_name, message.document.file_id)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                file_ext = Path(message.document.file_name).suffix.lower()
                original_file_type = 'pdf' if file_ext == '.pdf' else 'txt'
                file_path = os.path.join(temp_dir, f"training{file_ext}")
                logger.debug("Создан временный файл: %s", file_path)

                file_info = await bot.get_file(message.document.file_id)
                downloaded_file = await bot.download_file(file_info.file_path)
                logger.info("Файл загружен, размер: %s байт", len(downloaded_file.getbuffer()))

                with open(file_path, 'wb') as f:
                    f.write(downloaded_file.read())
                logger.debug("Файл сохранён во временной директории")

                if original_file_type == 'pdf':
                    text_chunks = self._process_large_pdf(file_path)
                else:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        text = f.read()
                    logger.info("Извлечён текст из TXT: %s символов", len(text))
                    text_chunks = self._split_text_into_chunks(text, chunk_size=2000)

                total_chunks = 0
                total_qa_pairs = 0
                from rag_handler import RAGHandler
                rag = RAGHandler()
                logger.debug("RAGHandler инициализирован для добавления чанков")

                for chunk, tags in text_chunks:
                    logger.info("Добавляется чанк: %s символов, теги: %s", len(chunk), tags or "нет")
                    rag.add_to_knowledge_base(context=chunk, is_from_pdf=True, tags=tags)
                    total_chunks += 1

                    if generate_qa:
                        qa_pairs = self._generate_qa_pairs(chunk)
                        logger.info("Сгенерировано %s QA-пар для чанка", len(qa_pairs))
                        for question, answer in qa_pairs:
                            logger.debug("Добавляется QA-пара: Вопрос: %s, Ответ: %s...",
                                        question[:50], answer[:50])
                            rag.add_to_knowledge_base(question, answer, is_from_pdf=True, tags=tags)
                            total_qa_pairs += 1

                self._save_file_metadata(
                    filename=message.document.file_name,
                    file_type=original_file_type,
                    chunks_count=total_chunks
                )
                logger.info("Метаданные файла сохранены: %s, чанков: %s",
                           message.document.file_name, total_chunks)

                #await self._send_extracted_text_to_admin(bot, message.document.file_name, [chunk for chunk, _ in text_chunks])
                logger.debug("Извлечённые чанки отправлены администратору")

                # Сохраняем полный текст чанков в лог-файл для анализа
                log_file = f"extracted_{message.document.file_name.replace('.pdf', '').replace('.txt', '')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.write(f"Файл: {message.document.file_name}\nОбработан: {datetime.now().isoformat()}\n\n")
                    for i, (chunk, tags) in enumerate(text_chunks, 1):
                        f.write(f"Чанк {i} ({len(chunk)} символов, теги: {tags or 'нет'}):\n{chunk}\n{'-' * 50}\n")
                logger.info("Полный текст чанков сохранён в файл: %s", log_file)

                back_keyboard = ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="Назад")]],
                    resize_keyboard=True
                )
                response = (
                    f"✅ Файл '{message.document.file_name}' успешно обработан!\n"
                    f"📚 Добавлено {total_chunks} текстовых фрагментов"
                    f"{f' и {total_qa_pairs} пар вопрос-ответ' if generate_qa else ''} в базу знаний.\n"
                    "Используйте команду /test_file, чтобы проверить, как бот отвечает на основе этого файла."
                )
                await message.answer(response, reply_markup=back_keyboard)
                logger.info("Обработка файла завершена успешно: %s", message.document.file_name)
                return "Файл успешно обработан и добавлен в базу знаний"

        except Exception as e:
            logger.error("Ошибка обработки файла %s: %s", message.document.file_name, str(e), exc_info=True)
            return f"❌ Ошибка обработки файла: {e}"

    def _process_large_pdf(self, file_path: str, max_chunk_size: int = 2000, max_pages_per_chunk: int = 10) -> List[
        tuple]:
        text_chunks = []
        current_chunk = ""
        page_count = 0
        total_chars = 0

        keyword_groups = {
            "sleep": ["сон", "бессонница", "уснуть", "отдых", "расслабление"],
            "stress": ["стресс", "тревога", "напряжение", "нервы", "успокоение"],
            "energy": ["энергия", "бодрость", "усталость", "активность"],
            "immunity": ["иммунитет", "простуда", "вирусы", "иммунная система"],
            "skin": ["кожа", "акне", "увлажнение", "псориаз"],
            "vitamins": ["витамины", "витамин", "минералы", "микроэлементы"],
            "weight": ["вес", "похудение", "метаболизм"],
            "joints": ["суставы", "кости", "артрит", "гибкость"],
            "gut": ["жкт", "пищеварение", "желудок", "кишечник"]
        }
        logger.debug("Инициализированы группы ключевых слов для тегирования: %s групп", len(keyword_groups))

        with pdfplumber.open(file_path) as pdf:
            logger.info("Открыт PDF-файл: %s, страниц: %s", file_path, len(pdf.pages))
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                logger.debug("Страница %s: извлечено %s символов", i + 1, len(page_text))

                # Извлечение таблиц
                tables = page.extract_tables()
                if tables:
                    table_text = ""
                    for table in tables:
                        # Обрабатываем каждую строку таблицы, заменяя None на ""
                        table_text += "\n".join(["\t".join(cell if cell is not None else "" for cell in row)
                                                 for row in table if row]) + "\n"
                    page_text += "\nТаблица:\n" + table_text
                    logger.info("Страница %s: добавлен текст из %s таблиц, %s символов",
                                i + 1, len(tables), len(table_text))

                if not page_text.strip():
                    logger.warning("Страница %s: текст не извлечён", i + 1)
                    page_text = ""

                total_chars += len(page_text)
                page_count += 1

                if len(current_chunk) + len(page_text) <= max_chunk_size and page_count <= max_pages_per_chunk:
                    current_chunk += page_text + "\n"
                    logger.debug("Страница %s добавлена в текущий чанк, размер чанка: %s",
                                 i + 1, len(current_chunk))
                else:
                    if current_chunk.strip():
                        tags = self._generate_tags(current_chunk, keyword_groups)
                        text_chunks.append((current_chunk.strip(), tags))
                        logger.info("Создан чанк %s: %s символов, теги: %s",
                                    len(text_chunks), len(current_chunk.strip()), tags or "нет")
                    current_chunk = page_text + "\n"
                    page_count = 1
                    logger.debug("Начат новый чанк с страницы %s, размер: %s",
                                 i + 1, len(current_chunk))

                if i == len(pdf.pages) - 1 and current_chunk.strip():
                    tags = self._generate_tags(current_chunk, keyword_groups)
                    text_chunks.append((current_chunk.strip(), tags))
                    logger.info("Создан финальный чанк %s: %s символов, теги: %s",
                                len(text_chunks), len(current_chunk.strip()), tags or "нет")

        logger.info("PDF обработан: %s чанков, всего извлечено %s символов", len(text_chunks), total_chars)
        return text_chunks

    def _generate_tags(self, text: str, keyword_groups: dict) -> str:
        text_lower = text.lower()
        tags = []
        for group, keywords in keyword_groups.items():
            if any(kw in text_lower for kw in keywords):
                tags.append(group)
        result = ",".join(tags) if tags else None
        logger.debug("Сгенерированы теги для текста (%s символов): %s", len(text), result or "нет")
        return result

    def _generate_qa_pairs(self, text: str) -> List[tuple]:
        qa_pairs = []
        chunks = self._split_text_into_chunks(text, chunk_size=1500)
        logger.info("Чанк разбит на %s подчанков для генерации QA-пар", len(chunks))

        for i, chunk in enumerate(chunks):
            logger.debug("Генерация QA-пар для подчанка %s: %s символов", i + 1, len(chunk))
            prompt = (
                f"На основе следующего текста создай 5-10 пар вопрос-ответ, связанных с питанием или здоровьем:\n\n"
                f"{chunk}\n\n"
                "Вопросы должны быть разнообразными, конкретными и полезными, основанными только на тексте. "
                "Примеры: 'Какой БАД помогает при выпадении волос?', 'Что полезно для иммунитета?'. "
                "Ответы — профессиональными, развернутыми, с использованием эмодзи (🌟, 📝, ✅) и заголовков. "
                "Если текста недостаточно, верни пустой результат.\n"
                "Формат:\nQ: [вопрос]\nA: [ответ]\n"
            )
            response = self._call_llm(prompt)
            if response:
                pairs = self._parse_qa_response(response)
                filtered_pairs = [(q, a) for q, a in pairs if q and a and len(q) > 10 and len(a) > 50]
                qa_pairs.extend(filtered_pairs[:7])
                logger.info("Подчанк %s: сгенерировано %s QA-пар", i + 1, len(filtered_pairs))
            else:
                logger.warning("Подчанк %s: не удалось сгенерировать QA-пары", i + 1)

        logger.info("Всего сгенерировано %s QA-пар из текста", len(qa_pairs))
        return qa_pairs

    def _call_llm(self, prompt: str) -> Optional[str]:
        logger.debug("Отправка запроса к LLM, длина промпта: %s символов", len(prompt))
        try:
            headers = {"Content-Type": "application/json"}
            data = {
                "model": "llama3.1:8b",
                "prompt": prompt,
                "system": (
                    "Ты - профессиональный нутрициолог. Отвечай ТОЛЬКО на русском языке. "
                    "Будь вежливым, используй эмодзи и заголовки для красивого оформления."
                ),
                "stream": False,
                "options": {"temperature": 0.7, "max_tokens": 1500}
            }
            response = requests.post(self.llm_url, headers=headers, json=data)
            response.raise_for_status()
            result = response.json().get("response")
            logger.info("LLM вернул ответ: %s символов", len(result) if result else 0)
            return result
        except Exception as e:
            logger.error("Ошибка вызова LLM: %s", str(e))
            return None

    def _parse_qa_response(self, response: str) -> List[tuple]:
        qa_pairs = []
        lines = response.strip().split('\n')
        question = None
        for line in lines:
            if line.startswith("Q:"):
                question = line[2:].strip()
                logger.debug("Извлечён вопрос: %s...", question[:50])
            elif line.startswith("A:") and question:
                answer = line[2:].strip()
                qa_pairs.append((question, answer))
                logger.debug("Извлечён ответ: %s...", answer[:50])
                question = None
        logger.info("Распарсено %s QA-пар из ответа LLM", len(qa_pairs))
        return qa_pairs

    def _split_text_into_chunks(self, text: str, chunk_size: int = 2000) -> List[str]:
        sentences = sent_tokenize(text, language="russian")
        chunks = []
        current_chunk = ""
        for sentence in sentences:
            if len(current_chunk) + len(sentence) <= chunk_size:
                current_chunk += sentence + " "
            else:
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                    logger.debug("Создан чанк: %s символов", len(current_chunk.strip()))
                current_chunk = sentence + " "
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
            logger.debug("Создан финальный чанк: %s символов", len(current_chunk.strip()))
        logger.info("Текст разбит на %s чанков", len(chunks))
        return chunks

    def _save_file_metadata(self, filename: str, file_type: str, chunks_count: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO training_files 
                        (filename, file_type, processed_at, chunks_count)
                        VALUES (?, ?, ?, ?)''',
                       (filename, file_type, datetime.now().isoformat(), chunks_count))
        conn.commit()
        conn.close()
        logger.info("Сохранены метаданные: файл=%s, тип=%s, чанков=%s", filename, file_type, chunks_count)

    async def _send_extracted_text_to_admin(self, bot: Bot, filename: str, text_chunks: List[str]):
        admin_id = 5440647148
        header = f"📄 Обработан файл: {filename}\n\n✂️ Извлеченные текстовые фрагменты:\n{'=' * 20}\n"
        total_chars = sum(len(chunk) for chunk in text_chunks)
        logger.info("Отправка %s чанков администратору, всего %s символов", len(text_chunks), total_chars)

        if len(text_chunks) > 10:
            summary = f"Всего чанков: {len(text_chunks)}\nПервые 10:\n"
            chunks_to_send = text_chunks[:10]
        else:
            summary = ""
            chunks_to_send = text_chunks

        full_text = "\n\n".join([f"Чанк {i+1} ({len(chunk)} символов):\n{chunk[:500]}..."
                                for i, chunk in enumerate(chunks_to_send)])
        message_text = header + summary + full_text

        if len(message_text) <= 4096:
            await bot.send_message(chat_id=admin_id, text=message_text)
            logger.debug("Отправлено одно сообщение администратору: %s символов", len(message_text))
        else:
            await bot.send_message(chat_id=admin_id, text=header + summary)
            logger.debug("Отправлен заголовок администратору")
            for i, chunk in enumerate(chunks_to_send):
                chunk_preview = f"Чанк {i+1} ({len(chunk)} символов):\n{chunk[:500]}..."
                for j in range(0, len(chunk_preview), 4096):
                    part = chunk_preview[j:j + 4096]
                    await bot.send_message(chat_id=admin_id, text=part)
                    logger.debug("Отправлена часть чанка %s: %s символов", i + 1, len(part))

    def get_training_stats(self) -> Optional[str]:
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''SELECT COUNT(*) FROM training_files''')
            total_files = cursor.fetchone()[0]
            cursor.execute('''SELECT SUM(chunks_count) FROM training_files''')
            total_chunks = cursor.fetchone()[0] or 0
            cursor.execute('''SELECT filename, processed_at 
                            FROM training_files 
                            ORDER BY processed_at DESC 
                            LIMIT 5''')
            recent_files = cursor.fetchall()
            conn.close()

            stats = [
                "📊 **Статистика обучения бота**",
                f"📚 Всего обработано файлов: {total_files}",
                f"✂️ Всего текстовых фрагментов: {total_chunks}",
                "\n🔍 **Последние 5 файлов:**"
            ]
            for i, (filename, date) in enumerate(recent_files, 1):
                stats.append(f"{i}. {filename} (дата: {date[:10]})")
            logger.info("Сформирована статистика обучения: %s файлов, %s чанков", total_files, total_chunks)
            return "\n".join(stats)
        except Exception as e:
            logger.error("Ошибка получения статистики обучения: %s", str(e))
            return None