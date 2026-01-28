import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from collections import deque
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
import httpx

load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('report_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Отдельный логгер для reported сообщений
reported_logger = logging.getLogger('reported_messages')
reported_handler = logging.FileHandler('reported_messages.log', encoding='utf-8')
reported_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
reported_logger.addHandler(reported_handler)
reported_logger.setLevel(logging.INFO)

# Кэш последних сообщений (до 50 сообщений в памяти)
message_cache = deque(maxlen=150)

# Данные о задействованных пользователях (для размута)
muted_users = {}  # user_id -> {'chat_id': ..., 'message_id': ...}
banned_users = {}  # user_id -> {'chat_id': ..., 'message_id': ...} для разбана
pending_bans = {}  # user_id -> {'chat_id': ..., 'target_id': ..., 'reason': ..., 'message_id': ...} для подтверждения BAN

# Кулдаун для /rep команды (30 сек)
rep_cooldown = {}  # user_id -> timestamp

# ================= КОНФИГ =================
TG_TOKEN = os.getenv("BOT_TOKEN_REPORT")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID"))
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
# =========================================

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# Системный промпт с правилами
SYSTEM_PROMPT = """
Ты — ИИ-модератор чата. Анализируй сообщение МАКСИМАЛЬНО ЛОЯЛЬНО.

⚠️ САМОЕ ВАЖНОЕ: ОПРЕДЕЛИ ИНИЦИАТОРА КОНФЛИКТА!
Если видишь что пользователь ОТВЕТИЛ на оскорбление - это ЗАЩИТА, НЕ карай его!
Если видишь что пользователь СПРОВОЦИРОВАЛ других - карай его!
Читай предыдущие сообщения, чтобы понять кто начал конфликт!

1.1 Флуд/Спам -> MUTE 35 мин (одно и то же 3+ раза подряд, сплошные символы)
1.2 Оскорбления -> MUTE 60 мин (ТОЛЬКО СЕРЬЁЗНЫЕ личные унижающие оскорбления ПРЯМО В АДРЕС типа:
    "ты урод", "ты говно", "ты мусор", "ты жалкий идиот" (как настоящее оскорбление, прямое в адрес)
    НЕ карай за:
    - "какашка" (детское/смешное слово, не унижение)
    - "дурак", "идиот" (без "ты" в адрес)
    - "ты дурак" в шутку
    - "бедный венсер" (шутка/критика, не унижение)
    - "бедный" в любом контексте (не унижение)
    - мат и ругательства в эмоциях
    - сарказм, шутки, прозвища)
1.3 Дискриминация -> MUTE 360 мин (явная дискриминация по национальности/полу/религии)
1.5 Реклама -> WARN (коммерческие ссылки, приглашения в другие сообщества, призывы перейти куда-то в другие чаты/сервисы/хаусы. Примеры: "переходите в мой хаус", "приходите на сервер", "присоединяйтесь", любые приглашения в приватные места/дискорды/игровые миры)
1.6 18+ контент -> MUTE 1440 мин (явно сексуальный контент, порно)
1.7 Политика/Нацизм -> MUTE 720 мин (пропаганда нацизма, фашизма)
1.9 Агрессия -> MUTE 60 мин (реальные угрозы физического вреда типа "я тебя найду и побью")
1.10 Вирусы/Ссылки -> BAN (явно вредоносные ссылки, фишинг)
1.11 Слив данных -> BAN (КРИТИЧНО! личные данные, домашние адреса, реальные ФИО с адресами, номера квартир, улицы и номера домов, координаты. Примеры: "улица пушкина дом 123 квартира 45", "живешь на улице...", любые конкретные адреса)
1.12 Угрозы -> BAN (серьёзные реальные угрозы жизни/здоровью)

ВСЕГДА OK - НИКОГДА НЕ КАРАЙ:
- Любой мат в выражении эмоций
- Детские ругательства ("какашка", "дураки", "идиоты" как обобщение)
- Ругательства в адрес технологий/сервисов/компаний
- Шутки, сарказм, мемы, иронию
- Критику, мнения
- Обычные ругательства между людьми БЕЗ личного унижения

КАРАТЬ ТОЛЬКО если это ЯВНОЕ ЛИЧНОЕ УНИЖЕНИЕ ("ты говно", "ты урод" И ТАК ДАЛЕЕ).

Также если там написано какая то помощь, где находится портал в энд и тд, помоги человеку, дай инструкцию, не игнорируй его.

Отвечай ТОЛЬКО JSON:
{"action": "MUTE/BAN/WARN/OK", "duration": число_или_null, "reason": "причина"}
"""

async def check_with_ai(text: str, context: str = ""):
    try:
        full_request = f"Текст для проверки: {text}"
        if context:
            full_request = f"Контекст:\n{context}\n\n{full_request}"
        
        logger.info(f"📤 Запрос к OpenRouter: {text[:100]}...")
        
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "HTTP-Referer": "https://github.com",
            "X-Title": "Report Bot",
            "Content-Type": "application/json; charset=utf-8"
        }
        
        data = {
            "model": "openrouter/auto",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": full_request}
            ],
            "temperature": 0.3,
            "max_tokens": 500
        }
        
        logger.info(f"📡 Отправляю в OpenRouter с контекстом...")
        
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=data
            )
            logger.info(f"📡 Статус ответа: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"❌ Статус: {response.status_code}, Ответ: {response.text}")
                return {"action": "ERROR", "reason": f"OpenRouter ошибка {response.status_code}"}
            
            result_data = response.json()
            
        # Извлекаем текст ответа
        ai_response = result_data['choices'][0]['message']['content']
        logger.info(f"📥 Ответ OpenRouter: {ai_response}")
        
        # Парсим JSON
        clean_json = ai_response.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean_json)
        return result
        
    except Exception as e:
        logger.error(f"❌ Ошибка OpenRouter: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"action": "ERROR", "reason": f"Ошибка ИИ: {e}"}

@dp.message(Command("rep"))
async def report_command(message: types.Message):
    user_id = message.from_user.id
    
    # Проверяем кулдаун
    if user_id in rep_cooldown:
        time_passed = (datetime.now() - rep_cooldown[user_id]).total_seconds()
        if time_passed < 30:
            time_left = 30 - time_passed
            logger.warning(f"⏱️ КУЛДАУН: {message.from_user.first_name} попытался использовать /rep (осталось {time_left:.1f}с)")
            await message.reply(f"⏱️ Подождите {time_left:.1f} сек перед следующим /rep")
            return
    
    # Кулдаун истёк или пользователя нет - устанавливаем новый
    rep_cooldown[user_id] = datetime.now()
    
    # Проверяем что это в разрешённом чате
    if message.chat.id != ALLOWED_CHAT_ID:
        logger.warning(f"⚠️ Попытка использовать /rep в чате {message.chat.id} (разрешён только {ALLOWED_CHAT_ID})")
        await message.reply("❌ Эта команда работает только в определённом чате")
        return
    
    # Проверяем что это ответ на сообщение
    if not message.reply_to_message:
        logger.warning(f"⚠️ {message.from_user.first_name} вызвал /rep не на сообщение")
        await message.reply("❌ Используй /rep в ответ на сообщение")
        return

    replied_msg = message.reply_to_message
    reporter = message.from_user.first_name
    target_user = replied_msg.from_user.first_name
    target_id = replied_msg.from_user.id
    
    # Текст для проверки
    text_to_check = replied_msg.text or replied_msg.caption or "[медиа без текста]"
    
    logger.info(f"📋 РЕПОРТ: {reporter} пожаловался на {target_user} ({target_id})")
    logger.info(f"   Текст: {text_to_check[:100]}...")

    # Собираем контекст из кэша - последние 5 сообщений ДО этого
    context_messages = []
    for msg_data in message_cache:
        if msg_data['message_id'] < replied_msg.message_id:
            context_messages.append(msg_data)
    
    # Берём последние 15 сообщений (для анализа конфликтов)
    context_messages = context_messages[-15:]
    
    # Форматируем контекст
    context = ""
    if context_messages:
        context = "📜 История диалога перед этим сообщением:\n"
        for msg in context_messages:
            context += f"{msg['username']}: {msg['text']}\n"
        context += f"\n⚠️ Проверяемое сообщение:\n{target_user}: {text_to_check}"
        logger.info(f"📜 Контекст собран: {len(context_messages)} сообщений")
    else:
        context = f"Сообщение от {target_user}: {text_to_check}"

    # Проверяем через ИИ с контекстом
    result = await check_with_ai(text_to_check, context)
    action = result.get("action", "ERROR")
    reason = result.get("reason", "")
    duration = result.get("duration", 0)

    # Формируем ответное сообщение
    if action == "MUTE":
        response_text = f"🔇 MUTE {duration} минут\n{reason}"
        logger.warning(f"🔇 МУТЕ: {target_user} на {duration} мин. Причина: {reason}")
        
        # Логируем reported сообщение
        reported_logger.info(f"MUTE | Пользователь: {target_user} ({target_id}) | Сообщение: {text_to_check} | Причина: {reason} | От кого: {reporter}")
        
        try:
            until = datetime.now() + timedelta(minutes=duration)
            await bot.restrict_chat_member(
                chat_id=replied_msg.chat.id,
                user_id=target_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_photos=False,
                    can_send_videos=False,
                    can_send_documents=False,
                    can_send_audios=False,
                    can_send_voice_notes=False,
                    can_send_video_notes=False,
                    can_send_animations=False,
                    can_send_stickers=False,
                    can_send_polls=False
                ),
                until_date=until
            )
            logger.info(f"✅ Мут успешно применен (запрещено всё)")
        except Exception as e:
            logger.error(f"❌ Ошибка при применении мута: {e}")
            response_text += f"\n⚠️ Ошибка: {e}"

    elif action == "BAN":
        response_text = f"🚫 BAN {target_user}\n{reason}"
        logger.critical(f"🚫 БАН ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ: {target_user} ({target_id}). Причина: {reason}")
        
        # Логируем reported сообщение
        reported_logger.info(f"REPORTED | Пользователь: {target_user} ({target_id}) | Сообщение: {text_to_check} | Причина: {reason} | От кого: {reporter}")
        
        # Отправляем в админ чат для подтверждения
        try:
            ban_confirm_text = f"🚫 ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ BAN\n\n👤 Пользователь: {target_user} ({target_id})\n📝 Причина: {reason}\n💬 От кого: {reporter}\n📋 Сообщение: {text_to_check}"
            
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Подтвердить BAN", callback_data=f"confirm_ban_{target_id}_{replied_msg.chat.id}"),
                    InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_ban_{target_id}")
                ]]
            )
            
            msg = await bot.send_message(chat_id=ADMIN_CHAT_ID, text=ban_confirm_text, reply_markup=keyboard)
            
            # Сохраняем в pending_bans
            pending_bans[target_id] = {
                'chat_id': replied_msg.chat.id,
                'target_id': target_id,
                'reason': reason,
                'message_id': msg.message_id,
                'admin_chat_id': ADMIN_CHAT_ID
            }
            
            logger.info(f"📤 BAN отправлен на подтверждение админам")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки BAN на подтверждение: {e}")
            response_text = f"🚫 БАН ОШИБКА\n{str(e)}"

    elif action == "WARN":
        response_text = f"⚠️ WARN\n{reason}"
        logger.warning(f"⚠️ ВАРН: {target_user} ({target_id}). Причина: {reason}")
        
        # Логируем reported сообщение
        reported_logger.info(f"WARN | Пользователь: {target_user} ({target_id}) | Сообщение: {text_to_check} | Причина: {reason} | От кого: {reporter}")

    elif action == "OK":
        response_text = f"✅ OK\n{reason}"
        logger.info(f"✅ Сообщение одобрено: {reason}")

    else:
        response_text = f"❌ Ошибка\n{reason}"
        logger.error(f"❌ Ошибка обработки: {reason}")

    # Отправляем ответ на исходное сообщение с кнопками
    if action == "MUTE":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="🔓 Размутить", callback_data=f"unmute_{target_id}")
            ]]
        )
        msg = await replied_msg.reply(response_text, reply_markup=keyboard)
        muted_users[target_id] = {
            'chat_id': replied_msg.chat.id,
            'message_id': msg.message_id
        }
    elif action == "BAN":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="🔓 Разбанить", callback_data=f"unban_{target_id}")
            ]]
        )
        msg = await replied_msg.reply(response_text, reply_markup=keyboard)
        banned_users[target_id] = {
            'chat_id': replied_msg.chat.id,
            'message_id': msg.message_id
        }
    elif action == "WARN":
        warn_text = f"⚠️ WARN {target_user}\n{reason}"
        msg = await replied_msg.reply(warn_text)
        
        # Отправляем варн в админ чат
        try:
            admin_warn_text = f"⚠️ ВАРН ВЫДАН\n\n👤 Пользователь: {target_user} ({target_id})\n📝 Причина: {reason}\n💬 От кого: {reporter}"
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_warn_text)
            logger.info(f"📤 Варн отправлен в админ чат")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки варна в админ чат: {e}")
    else:
        await replied_msg.reply(response_text)
    
    # ПОТОМ удаляем исходное сообщение если было наказание
    if action in ["MUTE", "BAN", "WARN"]:
        try:
            await replied_msg.delete()
            logger.info(f"🗑️ Сообщение удалено")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось удалить сообщение: {e}")
    
    logger.info(f"📤 Ответ отправлен: {response_text.replace(chr(10), ' | ')}")

@dp.message(Command("repno"))
async def repno_command(message: types.Message):
    """Анализирует сообщение через ИИ БЕЗ наказания, отправляет результат админам"""
    user_id = message.from_user.id
    
    # Проверяем что это в разрешённом чате
    if message.chat.id != ALLOWED_CHAT_ID:
        logger.warning(f"⚠️ Попытка использовать /repno в чате {message.chat.id}")
        await message.reply("❌ Эта команда работает только в определённом чате")
        return
    
    # Проверяем что это ответ на сообщение
    if not message.reply_to_message:
        await message.reply("❌ Используй /repno в ответ на сообщение")
        return

    replied_msg = message.reply_to_message
    reporter = message.from_user.first_name
    target_user = replied_msg.from_user.first_name
    target_id = replied_msg.from_user.id
    
    # Текст для проверки
    text_to_check = replied_msg.text or replied_msg.caption or "[медиа без текста]"
    
    logger.info(f"🔍 РЕПНО (анализ): {reporter} проверяет {target_user} ({target_id})")
    logger.info(f"   Текст: {text_to_check[:100]}...")

    # Собираем контекст из кэша - последние 15 сообщений
    context_messages = []
    for msg_data in message_cache:
        if msg_data['message_id'] < replied_msg.message_id:
            context_messages.append(msg_data)
    
    context_messages = context_messages[-15:]
    
    # Форматируем контекст
    context = ""
    if context_messages:
        context = "📜 История диалога перед этим сообщением:\n"
        for msg in context_messages:
            context += f"{msg['username']}: {msg['text']}\n"
        context += f"\n⚠️ Проверяемое сообщение:\n{target_user}: {text_to_check}"
    else:
        context = f"Сообщение от {target_user}: {text_to_check}"

    # Проверяем через ИИ с контекстом
    result = await check_with_ai(text_to_check, context)
    action = result.get("action", "ERROR")
    reason = result.get("reason", "")
    duration = result.get("duration", 0)

    # Формируем ответ ДЛЯ АДМИНОВ (без наказания)
    analysis_text = f"""
🔍 АНАЛИЗ БЕЗ НАКАЗАНИЯ (repno)

👤 От кого: {target_user} ({target_id})
📝 Сообщение: {text_to_check}

🤖 ИИ-анализ:
  ⚙️ Действие: {action}
  ⏱️ Длительность: {duration} мин
  📋 Причина: {reason}

💬 Заметил: {reporter}
"""
    
    # Отправляем админам
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=analysis_text
        )
        logger.info(f"📤 Анализ отправлен админам: {action} - {reason}")
        await message.reply("✅ Анализ отправлен администраторам (наказание НЕ применено)")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки анализа админам: {e}")
        await message.reply(f"⚠️ Анализ выполнен, но не удалось отправить админам: {e}")

# Хендлер для подтверждения/отмены BAN (только для админов в админ чате)
@dp.callback_query(F.data.startswith("confirm_ban_"))
async def confirm_ban_callback(callback: types.CallbackQuery):
    # Проверяем что это админ
    member = await bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
    if member.status not in ["creator", "administrator"]:
        await callback.answer("❌ Только администраторы могут подтвердить BAN", show_alert=True)
        return
    
    data = callback.data.split("_")
    target_id = int(data[2])
    chat_id = int(data[3])
    
    try:
        if target_id in pending_bans:
            ban_info = pending_bans[target_id]
            
            # Баним пользователя
            await bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
            
            await callback.message.edit_text(f"✅ BAN ПОДТВЕРЖДЕН И ПРИМЕНЕН администратором {callback.from_user.first_name}")
            await callback.answer("✅ BAN применен", show_alert=False)
            logger.warning(f"🚫 BAN ПРИМЕНЕН: Администратор {callback.from_user.first_name} подтвердил бан пользователя {target_id}")
            
            del pending_bans[target_id]
        else:
            await callback.answer("❌ BAN не найден", show_alert=True)
    except Exception as e:
        logger.error(f"❌ Ошибка при применении BAN: {e}")
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

@dp.callback_query(F.data.startswith("cancel_ban_"))
async def cancel_ban_callback(callback: types.CallbackQuery):
    # Проверяем что это админ
    member = await bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
    if member.status not in ["creator", "administrator"]:
        await callback.answer("❌ Только администраторы могут отменить BAN", show_alert=True)
        return
    
    target_id = int(callback.data.split("_")[2])
    
    try:
        if target_id in pending_bans:
            await callback.message.edit_text(f"❌ BAN ОТМЕНЕН администратором {callback.from_user.first_name}")
            await callback.answer("✅ BAN отменен", show_alert=False)
            logger.warning(f"❌ BAN ОТМЕНЕН: Администратор {callback.from_user.first_name} отменил бан пользователя {target_id}")
            
            del pending_bans[target_id]
        else:
            await callback.answer("❌ BAN не найден", show_alert=True)
    except Exception as e:
        logger.error(f"❌ Ошибка при отмене BAN: {e}")
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

# Хендлер для размута по кнопке (только для админов)
@dp.callback_query(F.data.startswith("unmute_"))
async def unmute_callback(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    logger.info(f"📋 Попытка размута: user_id={user_id}")
    
    # Получаем информацию о муте
    if user_id not in muted_users:
        logger.warning(f"⚠️ Мут не найден для user_id={user_id}")
        await callback.answer("❌ Мут не найден", show_alert=True)
        return
    
    mute_info = muted_users[user_id]
    chat_id = mute_info['chat_id']
    logger.info(f"📋 Chat ID из muted_users: {chat_id}")
    
    # Проверяем что это админ В ОСНОВНОМ ЧАТЕ
    try:
        member = await bot.get_chat_member(chat_id, callback.from_user.id)
        logger.info(f"📋 Статус администратора {callback.from_user.first_name}: {member.status}")
        
        if member.status not in ["creator", "administrator"]:
            await callback.answer("❌ Только администраторы могут размутить", show_alert=True)
            return
    except Exception as e:
        logger.error(f"❌ Ошибка проверки прав админа: {str(e)}")
        await callback.answer(f"❌ Ошибка проверки: {str(e)}", show_alert=True)
        return
    
    try:
        logger.info(f"🔓 Размутиваю пользователя {user_id} в чате {chat_id}")
        
        # Размутим пользователя - разрешаем ВСЕ права
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions()
        )
        
        logger.info(f"✅ Размут применен для {user_id}")
        
        try:
            await callback.message.edit_text(f"✅ Пользователь размучен администратором {callback.from_user.first_name}")
        except:
            pass  # Если не удалось отредактировать - не критично
        
        await callback.answer("✅ Пользователь размучен", show_alert=False)
        logger.warning(f"🔓 РАЗМУТ: Администратор {callback.from_user.first_name} размутил пользователя {user_id}")
        
        del muted_users[user_id]
            
    except Exception as e:
        logger.error(f"❌ Ошибка при размуте: {str(e)}")
        logger.error(f"Детали: {repr(e)}")
        await callback.answer(f"❌ Ошибка размута: {str(e)}", show_alert=True)

# Хендлер для разбана по кнопке (только для админов)
@dp.callback_query(F.data.startswith("unban_"))
async def unban_callback(callback: types.CallbackQuery):
    # Проверяем что это админ
    member = await bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
    if member.status not in ["creator", "administrator"]:
        await callback.answer("❌ Только администраторы могут разбанить", show_alert=True)
        return

    user_id = int(callback.data.split("_")[1])
    chat_id = callback.message.chat.id
    
    try:
        # Разбаним пользователя
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
        
        await callback.message.edit_text(f"✅ Пользователь разбанен администратором {callback.from_user.first_name}")
        await callback.answer("✅ Пользователь разбанен", show_alert=False)
        logger.warning(f"🔓 РАЗБАН: Администратор {callback.from_user.first_name} разбанил пользователя {user_id}")
        
        if user_id in banned_users:
            del banned_users[user_id]
            
    except Exception as e:
        logger.error(f"❌ Ошибка при разбане: {e}")
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

# Команда /unmuteall для размута всех (только админы)
@dp.message(Command("unmuteall"))
async def unmuteall_command(message: types.Message):
    # Проверяем что это админ
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ["creator", "administrator"]:
        await message.reply("❌ Только администраторы могут использовать эту команду")
        logger.warning(f"⚠️ Попытка /unmuteall от {message.from_user.first_name} (не админ)")
        return
    
    # Проверяем что это в разрешённом чате
    if message.chat.id != ALLOWED_CHAT_ID:
        await message.reply("❌ Команда работает только в определённом чате")
        return
    
    if not muted_users:
        await message.reply("✅ Нет мученых пользователей")
        return
    
    unmuted_count = 0
    failed_count = 0
    
    for user_id in list(muted_users.keys()):
        try:
            # Разрешаем всё
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=user_id,
                permissions=ChatPermissions()
            )
            unmuted_count += 1
            logger.info(f"🔓 Размучен: {user_id}")
            # Удаляем из списка ТОЛЬКО если успешно
            del muted_users[user_id]
        except Exception as e:
            failed_count += 1
            logger.error(f"❌ Ошибка при размуте {user_id}: {e}")
            # НЕ удаляем из списка, чтобы попробовать в следующий раз
    
    await message.reply(f"✅ Размучено: {unmuted_count}\n❌ Ошибок: {failed_count}")
    logger.warning(f"🔓 РАЗМУТ ВСЕ: {unmuted_count} пользователей размучено")

# Кэшируем все сообщения из чата для контекста
@dp.message()
async def cache_messages(message: types.Message):
    if message.chat.id == ALLOWED_CHAT_ID:
        message_cache.append({
            'message_id': message.message_id,
            'username': message.from_user.first_name or "unknown",
            'text': message.text or message.caption or "[медиа]",
            'timestamp': datetime.now()
        })
    
    # Логируем ЛС
    if message.chat.type == "private":
        logger.info(f"💬 ЛС от {message.from_user.first_name} ({message.from_user.id}): {message.text or message.caption or '[медиа]'}")

async def main():
    logger.info("="*50)
    logger.info("🤖 Report бот запущен...")
    logger.info("="*50)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
