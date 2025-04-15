import sqlite3
from datetime import datetime
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import logging
import nltk
from nltk.tokenize import sent_tokenize

# Загружаем ресурсы для NLTK
try:
    nltk.data.find('tokenizers/punkt_tab')
except LookupError:
    nltk.download('punkt_tab', quiet=True)


logger = logging.getLogger(__name__)

class RAGHandler:
    def __init__(self, db_path='nutrition_bot.db',
                 model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'):
        self.db_path = db_path
        self.model = SentenceTransformer(model_name)
        self.init_vector_db()
        self.abbreviations = {
            "жкт": "желудочно-кишечный тракт",
            "цнс": "центральная нервная система",
            "имт": "индекс массы тела",
            "ср": "средство",
            "бад": "биологически активная добавка",
            "эм": "эфирное масло",
            "дотерра": "doTERRA БАД",
            "Дотерр": "doTERRA БАД",
            "вит": "витамин",
            "мин": "минерал",
            "антиокс": "антиоксидант"
        }

    def init_vector_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS knowledge_vectors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        question TEXT,
                        answer TEXT,
                        context TEXT,
                        vector BLOB NOT NULL,
                        last_used DATETIME,
                        usage_count INTEGER DEFAULT 1,
                        is_from_pdf BOOLEAN DEFAULT 0,
                        tags TEXT)''')
        conn.commit()
        conn.close()
        logger.info("Векторная база знаний инициализирована")

    def text_to_vector(self, text):
        if not text or not isinstance(text, str):
            logger.warning("Попытка векторизации пустого текста")
            return np.zeros(self.model.get_sentence_embedding_dimension())
        return self.model.encode(text)

    def vector_to_blob(self, vector):
        return vector.tobytes()

    def blob_to_vector(self, blob):
        return np.frombuffer(blob, dtype=np.float32)

    def expand_abbreviations(self, query):
        query_lower = query.lower()
        expanded_query = query_lower
        for abbr, full_form in self.abbreviations.items():
            if f" {abbr} " in f" {query_lower} " or query_lower.startswith(f"{abbr} ") or query_lower.endswith(f" {abbr}"):
                expanded_query += f" {full_form}"
        logger.debug(f"Расширенный запрос: {expanded_query}")
        return expanded_query.strip()

    def _split_text_into_chunks(self, text: str, chunk_size: int = 2000) -> list:
        """Разбивает текст на чанки по предложениям, сохраняя смысловую целостность"""
        sentences = sent_tokenize(text, language="russian")
        chunks = []
        current_chunk = ""
        for sentence in sentences:
            if len(current_chunk) + len(sentence) <= chunk_size:
                current_chunk += sentence + " "
            else:
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + " "
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
        logger.debug(f"Текст разбит на {len(chunks)} чанков")
        return chunks

    def find_relevant_context(self, query, threshold=0.7, top_k=3, min_threshold=0.4):
        expanded_query = self.expand_abbreviations(query)
        query_vector = self.text_to_vector(expanded_query)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''SELECT id, question, answer, context, vector, is_from_pdf, tags 
                         FROM knowledge_vectors''')
        results = cursor.fetchall()
        conn.close()

        similarities = []
        query_lower = expanded_query.lower()
        keyword_groups = {
            "heart": [
                "сердце", "сосуды", "кровообращение", "давление", "холестерин", "гипертония", "артерии",
                "аритмия", "тахикардия", "брадикардия", "атеросклероз", "сердечная недостаточность",
                "кровяное давление", "венозная система", "капилляры", "сердечный ритм", "миокард"
            ],
            "sleep": [
                "сон", "бессонница", "уснуть", "отдых", "расслабление", "спать", "ночной",
                "качество сна", "глубокий сон", "проблемы со сном", "сонливость", "циркадный ритм",
                "мелатонин", "нарушение сна", "дневная усталость", "пробуждение"
            ],
            "stress": [
                "стресс", "тревога", "напряжение", "нервы", "успокоение", "паника", "эмоции",
                "нервозность", "переутомление", "выгорание", "эмоциональное напряжение", "релаксация",
                "психоэмоциональное состояние", "стрессоустойчивость", "кортизол", "адаптация"
            ],
            "energy": [
                "энергия", "бодрость", "усталость", "активность", "жизненная сила", "тонус",
                "упадок сил", "энергичность", "вялость", "хроническая усталость", "выносливость",
                "митохондрии", "энергетический баланс", "жизненный тонус", "сила"
            ],
            "gut": [
                "жкт", "пищеварение", "желудок", "кишечник", "микрофлора", "запор", "диарея",
                "гастрит", "дисбактериоз", "вздутие", "рефлюкс", "перистальтика", "энзимы",
                "микробиом", "синдром раздраженного кишечника", "ферменты", "кишечная проницаемость",
                "язва", "колит"
            ],
            "immunity": [
                "иммунитет", "защита организма", "инфекции", "простуда", "вирусы", "иммунная система",
                "грипп", "ОРВИ", "иммунодефицит", "иммунный ответ", "вакцинация", "антитела",
                "лимфоциты", "воспалительные процессы", "интерфероны", "укрепление иммунитета"
            ],
            "joints": [
                "суставы", "кости", "хрящи", "артрит", "боль в суставах", "гибкость", "остеопороз",
                "артроз", "ревматизм", "остеохондроз", "подагра", "хондропротекторы", "синовиальная жидкость",
                "суставная смазка", "коллаген", "минерализация костей", "переломы"
            ],
            "skin": [
                "кожа", "дерма", "акне", "прыщи", "увлажнение кожи", "экзема", "псориаз",
                "дерматит", "сухость кожи", "морщины", "пигментация", "покраснение", "сыпь",
                "коллаген кожи", "эластичность кожи", "заживление ран", "кожный зуд", "шелушение"
            ],
            "hair": [
                "волосы", "выпадение волос", "ломкость волос", "перхоть", "рост волос", "здоровье волос",
                "себорея", "алопеция", "секущиеся концы", "укрепление волос", "волосяные луковицы",
                "кожа головы", "жирность волос", "сухость волос", "блеск волос"
            ],
            "vision": [
                "зрение", "глаза", "усталость глаз", "катаракта", "глаукома", "здоровье глаз",
                "дальнозоркость", "близорукость", "астигматизм", "сухость глаз", "сетчатка",
                "хрусталик", "глазное давление", "зрительная нагрузка", "цветовое восприятие"
            ],
            "hormones": [
                "гормоны", "гормональный баланс", "щитовидка", "менопауза", "либидо", "эндокринная система",
                "тиреоидные гормоны", "эстроген", "прогестерон", "тестостерон", "инсулин",
                "гормон роста", "адреналин", "гормональный сбой", "надпочечники", "гипоталамус"
            ],
            "detox": [
                "детоксикация", "очищение организма", "токсины", "печень", "почки", "чистка",
                "шлаки", "детокс-программы", "антиоксиданты", "выведение токсинов", "лимфодренаж",
                "очищение кишечника", "гепатопротекторы", "очищение крови", "почечная фильтрация"
            ],
            "weight": [
                "вес", "похудение", "лишний вес", "ожирение", "метаболизм", "контроль веса",
                "жиросжигание", "масса тела", "диета", "калорийность", "аппетит", "набор веса",
                "индекс массы тела", "обмен веществ", "жировая ткань", "стройность"
            ],
            "muscles": [
                "мышцы", "мышечная масса", "боль в мышцах", "восстановление мышц", "сила",
                "спазмы", "крепатура", "мышечный тонус", "рост мышц", "миофибриллы",
                "анаболизм", "белковый синтез", "мышечная выносливость", "растяжение мышц"
            ],
            "allergies": [
                "аллергия", "аллергические реакции", "сыпь", "зуд", "астма", "ринит",
                "анафилаксия", "аллергены", "гистамин", "поллиноз", "пищевая аллергия",
                "контактный дерматит", "крапивница", "отек Квинке", "аллергический кашель"
            ],
            "respiratory": [
                "дыхание", "легкие", "бронхи", "кашель", "одышка", "дыхательная система",
                "бронхит", "пневмония", "туберкулез", "хрипы", "оксигенация", "дыхательная гимнастика",
                "мукоцилиарный клиренс", "легочная вентиляция", "эмфизема"
            ],
            "blood_sugar": [
                "сахар в крови", "диабет", "глюкоза", "инсулин", "гликемия",
                "гипогликемия", "гиергликемия", "глюкометр", "гликемический индекс",
                "инсулинорезистентность", "диабет 2 типа", "углеводный обмен", "панкреас"
            ],
            "memory": [
                "память", "концентрация", "мозг", "когнитивные функции", "фокус", "ясность ума",
                "нейропластичность", "запоминание", "внимание", "умственная работоспособность",
                "когнитивный спад", "деменция", "нейротрансмиттеры", "мозговая активность"
            ],
            "inflammation": [
                "воспаление", "противовоспалительное", "отек", "хроническое воспаление",
                "цитокины", "воспалительные маркеры", "боль при воспалении", "покраснение",
                "воспалительный процесс", "иммунное воспаление", "острые воспаления"
            ],
            "circulation": [
                "кровоток", "микроциркуляция", "варикоз", "тромбы", "капилляры",
                "венозный отток", "кровообращение", "гемодинамика", "тромбофлебит",
                "кровяные сгустки", "артериальный кровоток", "лимфоток", "ангиопатия"
            ],
            # Новые группы
            "liver": [
                "печень", "гепатопротекторы", "желчь", "гепатит", "цирроз", "жировой гепатоз",
                "детоксикация печени", "ферменты печени", "холестаз", "печеночная недостаточность",
                "очищение печени", "желчегонные", "печеночный метаболизм"
            ],
            "reproductive": [
                "репродуктивное здоровье", "фертильность", "менструация", "беременность", "либидо",
                "эректильная дисфункция", "простата", "яичники", "матка", "сперматогенез",
                "овуляция", "репродуктивная система", "бесплодие", "гормоны пола"
            ],
            "mental_health": [
                "психическое здоровье", "депрессия", "тревожное расстройство", "эмоциональное состояние",
                "психоэмоциональный баланс", "апатия", "настроение", "биполярное расстройство",
                "психологическое благополучие", "антидепрессанты", "серотонин"
            ],
            "thyroid": [
                "щитовидная железа", "тиреоидные гормоны", "гипотериоз", "гипертериоз", "зоб",
                "йод", "тироксин", "ТТГ", "аутоиммунный тиреоидит", "узлы щитовидки",
                "метаболизм щитовидки", "эндокринология"
            ],
            "kidneys": [
                "почки", "мочевыделительная система", "почечная недостаточность", "мочекаменная болезнь",
                "пиелонефрит", "почечная фильтрация", "мочеиспускание", "уремия", "диуретики",
                "отечность", "почечные канальцы", "гломерулонефрит"
            ],
            "pain": [
                "боль", "хроническая боль", "головная боль", "мигрень", "невралгия", "мышечная боль",
                "суставная боль", "болеутоляющее", "спазмолитическое", "боль в спине", "острая боль",
                "фибромиалгия", "боль в шее"
            ],
            "aging": [
                "старение", "антивозрастной", "долголетие", "возрастные изменения", "антиоксиданты",
                "клеточное обновление", "морщины", "снижение тонуса", "возрастной метаболизм",
                "гериатрия", "оксидативный стресс", "теломеры"
            ]
        }

        seen_entries = set()
        for row in results:
            vec_id, question, answer, context, vec_blob, is_from_pdf, tags = row
            text_to_compare = context if context else (question or answer)
            if not text_to_compare:
                logger.warning(f"Пустая запись в базе: id={vec_id}")
                continue

            entry_key = (question or "", answer or "", context or "")
            if entry_key in seen_entries:
                logger.debug(f"Пропущена дублирующаяся запись: id={vec_id}")
                continue
            seen_entries.add(entry_key)

            stored_vector = self.blob_to_vector(vec_blob)
            similarity = cosine_similarity([query_vector], [stored_vector])[0][0]

            text_lower = text_to_compare.lower()
            tags_lower = tags.lower() if tags else ""
            keyword_boost = 1.0
            for group, keywords in keyword_groups.items():
                if any(kw in query_lower for kw in keywords) and \
                   (any(kw in text_lower for kw in keywords) or any(kw in tags_lower for kw in keywords)):
                    keyword_boost = 1.5
                    break

            adjusted_similarity = similarity * (1.2 if is_from_pdf else 1.0) * keyword_boost
            similarities.append((vec_id, question, answer, context, adjusted_similarity, is_from_pdf, tags))

        similarities.sort(key=lambda x: x[4], reverse=True)
        filtered = [item for item in similarities if item[4] >= threshold][:top_k]
        if not filtered and similarities:
            filtered = [item for item in similarities if item[4] >= min_threshold][:top_k]
        logger.info(f"Найдено {len(filtered)} релевантных записей: threshold={threshold}, min_threshold={min_threshold}")
        return filtered

    def add_to_knowledge_base(self, question=None, answer=None, context=None, is_from_pdf=False, tags=None):
        text_to_vectorize = context if context else question
        if not text_to_vectorize:
            logger.warning("Попытка добавить пустую запись")
            return

        # Разбиваем длинный контекст на чанки, если нужно
        if context and len(context) > 2000:
            chunks = self._split_text_into_chunks(context, chunk_size=2000)
        else:
            chunks = [context] if context else [question]

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        for chunk in chunks:
            if not chunk:
                continue
            vector = self.text_to_vector(chunk)
            vector_blob = self.vector_to_blob(vector)

            if question and chunk == question:
                cursor.execute('''SELECT id, usage_count FROM knowledge_vectors 
                                WHERE question = ? LIMIT 1''', (question,))
                existing = cursor.fetchone()
                if existing:
                    vec_id, count = existing
                    cursor.execute('''UPDATE knowledge_vectors 
                                    SET usage_count = ?, last_used = ?, answer = ?, context = ?, tags = ?
                                    WHERE id = ?''',
                                   (count + 1, datetime.now().isoformat(), answer, context, tags, vec_id))
                    logger.debug(f"Обновлена существующая запись: question={question}")
                else:
                    cursor.execute('''INSERT INTO knowledge_vectors 
                                    (question, answer, context, vector, last_used, is_from_pdf, tags)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                                   (question, answer, context, vector_blob, datetime.now().isoformat(), int(is_from_pdf), tags))
                    logger.debug(f"Добавлена новая запись: question={question}")
            else:
                cursor.execute('''INSERT INTO knowledge_vectors 
                                (context, vector, last_used, is_from_pdf, tags)
                                VALUES (?, ?, ?, ?, ?)''',
                               (chunk, vector_blob, datetime.now().isoformat(), int(is_from_pdf), tags))
                logger.debug(f"Добавлен новый контекстный чанк: {chunk[:50]}...")

        conn.commit()
        conn.close()
        logger.info(f"Добавлено {len(chunks)} чанков: question={question}, is_from_pdf={is_from_pdf}")

    def generate_rag_response(self, query, llm_generate_func):
        query_lower = query.lower().strip()
        general_keywords = [
            "как дела", "кто ты", "что ты", "что умеешь",
            "кто создал", "как настроение", "чем занимаешься", "расскажи о себе"
        ]
        if any(keyword in query_lower for keyword in general_keywords):
            return "😊 Кажется, это общий вопрос! Я бот-нутрициолог, готов ответить на темы питания, здоровья или БАДов. Задай что-нибудь ещё! 🌟"

        relevant = self.find_relevant_context(query, threshold=0.7, top_k=3, min_threshold=0.4)
        prompt_base = (
            f"Пользователь задал вопрос: {query}\n\n"
            "Ты - профессиональный нутрициолог, помощник Татьяны Николаевны, ароматерапевт, знаешь всё о биологически активных добавках. Если вопрос связан с питанием, здоровьем или БАДами, используй в первую очередь предоставленный контекст из базы знаний, "
            "чтобы дать точный и развернутый ответ. Если в контексте есть упоминания БАДов, укажи их с подробным описанием (название, состав, свойства, применение, дозировка, противопоказания). "
            "Если вопрос не связан с питанием или здоровьем, дай краткий, вежливый ответ без рекомендаций по БАДам. "
            "Если контекст не содержит подходящей информации, четко укажи: 'Нет данных о БАДах для этого запроса.' и предложи общие рекомендации только для тематических вопросов. "
            "Оформляй тематические ответы красиво с эмодзи (🌟, 📝, ✅) и заголовками. "
            "Не придумывай информацию о БАДах, которых нет в контексте."
        )

        seen_answers = set()
        if relevant:
            logger.info(f"Найдено {len(relevant)} релевантных контекстов")
            context = "Контекст из базы знаний:\n"
            pdf_context = ""
            user_context = ""
            for i, (_, q, a, ctx, sim, is_from_pdf, tags) in enumerate(relevant, 1):
                if not a and not ctx:
                    logger.warning(f"Пустой ответ и контекст для вопроса: {q}")
                    continue
                answer_key = a if a else ctx
                if answer_key in seen_answers:
                    logger.info(f"Пропущен дублирующийся ответ: {answer_key[:50]}...")
                    continue
                seen_answers.add(answer_key)
                entry = f"\nЗапись {i} (схожесть: {sim:.2f}):\n"
                if ctx:
                    entry += f"Контекст: {ctx[:1500]}...\n"
                if q and a:
                    entry += f"Вопрос: {q}\nОтвет: {a}\n"
                if tags:
                    entry += f"Теги: {tags}\n"
                if is_from_pdf:
                    pdf_context += entry
                else:
                    user_context += entry

            context = pdf_context + (f"\nДанные от пользователей:\n{user_context}" if user_context else "")
            prompt = (
                f"{prompt_base}\n\n"
                f"{context}\n\n"
                "Проанализируй контекст. Если вопрос связан с питанием или здоровьем, извлеки информацию о БАДах, если она есть, "
                "и укажи: название, состав, свойства, применение, дозировку, противопоказания. "
                "Если в контексте нет подходящих БАДов для запроса, четко укажи это в ответе и предложи общие рекомендации. "
                "Если вопрос не тематический, ответь кратко и вежливо, без рекомендаций."
            )
        else:
            logger.info("Релевантный контекст не найден")
            prompt = (
                f"{prompt_base}\n\n"
                "В базе знаний нет подходящей информации для ответа на запрос. "
                "Если вопрос связан с питанием или здоровьем, укажи: 'Нет данных о БАДах для этого запроса.' "
                "и предложи общие рекомендации как профессиональный нутрициолог. "
                "Если вопрос не тематический, дай краткий, вежливый ответ без рекомендаций."
            )

        response = llm_generate_func(prompt)
        if not response:
            logger.error("LLM не вернул ответ")
            return "Извините, не удалось сгенерировать ответ. Попробуйте уточнить запрос."
        if relevant:
            self._update_usage_counts([item[0] for item in relevant])
        return response

    def get_recent_entries(self, limit=5):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''SELECT question, answer, context, last_used, usage_count, is_from_pdf 
                          FROM knowledge_vectors 
                          ORDER BY last_used DESC 
                          LIMIT ?''', (limit,))
        results = cursor.fetchall()
        conn.close()
        return results

    def _update_usage_counts(self, vector_ids):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        for vec_id in vector_ids:
            cursor.execute('''UPDATE knowledge_vectors 
                            SET usage_count = usage_count + 1, last_used = ?
                            WHERE id = ?''',
                           (datetime.now().isoformat(), vec_id))
        conn.commit()
        conn.close()
        logger.info(f"Обновлены счетчики для {len(vector_ids)} записей")

    def optimize_knowledge_base(self, min_usage=5, max_items=2000):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM knowledge_vectors')
        total = cursor.fetchone()[0]
        if total <= max_items:
            conn.close()
            logger.info("Оптимизация не требуется")
            return

        # Приоритет для записей с тегами и PDF
        cursor.execute('''DELETE FROM knowledge_vectors 
                        WHERE usage_count < ? 
                        AND is_from_pdf = 0 
                        AND (tags IS NULL OR tags = '')
                        AND id NOT IN (
                            SELECT id FROM knowledge_vectors 
                            ORDER BY last_used DESC 
                            LIMIT ?
                        )''', (min_usage, max_items))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        logger.info(f"Удалено {deleted} редко используемых записей")