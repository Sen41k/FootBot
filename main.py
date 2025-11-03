import asyncio
import logging
import json
import uuid
from datetime import datetime
from typing import Dict, List

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from os import getenv
# from os import environ

load_dotenv()
TOKEN = getenv("TOKEN")

# TOKEN = environ.get("TOKEN")

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


# Состояния для FSM
class PollCreationState(StatesGroup):
    waiting_for_poll_name = State()
    waiting_for_start_day = State()
    waiting_for_start_time = State()
    waiting_for_end_day = State()
    waiting_for_end_time = State()


# Хранилище для данных
active_polls: Dict[str, Dict] = {}  # poll_id -> poll_data
poll_settings: Dict[str, List[Dict]] = {}  # chat_id -> list of settings


# Дни недели для inline клавиатуры
def get_days_inline_markup():
    """Создает inline-клавиатуру с днями недели"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Понедельник", callback_data="day_0")],
            [InlineKeyboardButton(text="Вторник", callback_data="day_1")],
            [InlineKeyboardButton(text="Среда", callback_data="day_2")],
            [InlineKeyboardButton(text="Четверг", callback_data="day_3")],
            [InlineKeyboardButton(text="Пятница", callback_data="day_4")],
            [InlineKeyboardButton(text="Суббота", callback_data="day_5")],
            [InlineKeyboardButton(text="Воскресенье", callback_data="day_6")]
        ]
    )


def load_data():
    """Загрузка данных из файла"""
    global poll_settings
    try:
        with open('poll_data.json', 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Очищаем пустые записи
        poll_settings = {chat_id: settings for chat_id, settings in data.items() if settings}
        logger.info(f"Данные загружены из файла: {len(poll_settings)} чатов с опросами")

    except FileNotFoundError:
        logger.info("Файл данных не найден, создаем новый")
        poll_settings = {}
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных: {e}")
        poll_settings = {}


def save_data():
    """Сохранение данных в файл"""
    with open('poll_data.json', 'w', encoding='utf-8') as f:
        json.dump(poll_settings, f, ensure_ascii=False, indent=2)
    logger.info("Данные сохранены в файл")


def day_name_to_number(day_name: str) -> int:
    """Конвертация названия дня в номер"""
    days = {
        "понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3,
        "пятница": 4, "суббота": 5, "воскресенье": 6
    }
    return days.get(day_name.lower(), 1)


def number_to_day_name(number: int) -> str:
    """Конвертация номера дня в название"""
    days = [
        "Понедельник", "Вторник", "Среда", "Четверг",
        "Пятница", "Суббота", "Воскресенье"
    ]
    return days[number] if 0 <= number < 7 else "Вторник"


async def is_admin(chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором чата"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        status_str = str(member.status).lower()

        admin_statuses = ['administrator', 'creator', 'owner', 'admin']
        is_admin_user = any(admin_status in status_str for admin_status in admin_statuses)

        return is_admin_user

    except Exception as e:
        logger.error(f"Ошибка проверки прав администратора: {e}")
        return False


async def check_admin(message: Message) -> bool:
    """Проверяет права и отправляет сообщение об ошибке если нужно"""
    if message.chat.type not in ['group', 'supergroup']:
        return True

    if not await is_admin(message.chat.id, message.from_user.id):
        await message.answer("❌ Эта команда доступна только администраторам группы!")
        return False
    return True


def format_poll_message(poll_name: str, votes_data: Dict, poll_id: str) -> str:
    """Форматирует сообщение с текущими результатами опроса"""
    yes_count = len(votes_data.get('yes', []))
    no_count = len(votes_data.get('no', []))
    maybe_count = len(votes_data.get('maybe', []))
    total_votes = yes_count + no_count + maybe_count

    message = f"""
🎯 <b>{poll_name}</b>

📊 <b>Текущие результаты:</b>
✅ Придут: {yes_count} чел.
❌ Не придут: {no_count} чел.
❓ Под вопросом: {maybe_count} чел.
👥 Всего проголосовало: {total_votes} чел.

ℹ️ <i>Можно менять голос в любое время!</i>
👀 <i>Нажмите "Предпросмотр голосов" чтобы увидеть кто проголосовал (всплывающее окно)</i>
"""

    return message


def format_preview_alert(poll_data: Dict) -> str:
    """Форматирует сообщение для всплывающего окна предпросмотра"""
    yes_voters = poll_data['user_names'].get('yes', [])
    no_voters = poll_data['user_names'].get('no', [])
    maybe_voters = poll_data['user_names'].get('maybe', [])

    yes_count = len(yes_voters)
    no_count = len(no_voters)
    maybe_count = len(maybe_voters)
    total_votes = yes_count + no_count + maybe_count

    # Ограничиваем длину сообщения для всплывающего окна
    message = "Предпросмотр голосов\n"

    # Показываем имена в каждой категории
    if yes_voters:
        message += "\n✅ Приходят:\n"
        for name in yes_voters:
            message += f"• {name}\n"

    if maybe_voters:
        message += "\n❓ Под вопросом:\n"
        for name in maybe_voters:
            message += f"• {name}\n"

    if no_voters:
        message += "\n❌ Не придут:\n"
        for name in no_voters:
            message += f"• {name}\n"

    if total_votes == 0:
        message += "\n😢 Пока никто не проголосовал"

    # Обрезаем сообщение если слишком длинное (ограничение Telegram)
    if len(message) > 200:
        message = message[:197] + "..."

    return message


def format_final_results(poll_name: str, yes_voters: List[str], no_voters: List[str], maybe_voters: List[str]) -> str:
    """Форматирует финальные результаты опроса"""
    yes_count = len(yes_voters)
    no_count = len(no_voters)
    maybe_count = len(maybe_voters)
    total_votes = yes_count + no_count + maybe_count

    message = f"""
🎯 Опрос завершен: {poll_name}

📊 <b>Статистика:</b>
✅ Придут: {yes_count} чел.
❌ Не придут: {no_count} чел.
❓ Под вопросом: {maybe_count} чел.
"""

    # Добавляем списки имен, если есть голосовавшие
    if yes_voters:
        message += "\n✅ <b>Придут на тренировку:</b>\n" + "\n".join([f"• {name}" for name in yes_voters])

    if maybe_voters:
        message += "\n\n❓ <b>Под вопросом:</b>\n" + "\n".join([f"• {name}" for name in maybe_voters])

    if no_voters:
        message += "\n\n❌ <b>Не придут:</b>\n" + "\n".join([f"• {name}" for name in no_voters])

    if total_votes == 0:
        message += "\n\n😢 <i>Никто не проголосовал</i>"

    return message


def get_vote_display_name(vote_option: str) -> str:
    """Получить отображаемое название варианта голоса"""
    options = {
        'yes': '✅ Да',
        'no': '❌ Нет',
        'maybe': '❓ Под вопросом'
    }
    return options.get(vote_option, vote_option)


async def create_poll(chat_id: str, settings: Dict):
    """Создание опроса с inline голосованием и кнопкой предпросмотра"""
    try:
        poll_id = str(uuid.uuid4())

        # Создаем сообщение с inline кнопками
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да", callback_data=f"vote_{poll_id}_yes"),
                    InlineKeyboardButton(text="❌ Нет", callback_data=f"vote_{poll_id}_no"),
                    InlineKeyboardButton(text="❓ Под вопросом", callback_data=f"vote_{poll_id}_maybe")
                ],
                [
                    InlineKeyboardButton(text="🔄 Сбросить голос", callback_data=f"vote_{poll_id}_reset"),
                    InlineKeyboardButton(text="👀 Предпросмотр голосов", callback_data=f"preview_{poll_id}")
                ]
            ]
        )

        poll_message = await bot.send_message(
            chat_id=chat_id,
            text=format_poll_message(settings['poll_name'], {}, poll_id),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

        # Сохраняем данные опроса
        active_polls[poll_id] = {
            'chat_id': chat_id,
            'message_id': poll_message.message_id,
            'user_votes': {'yes': [], 'no': [], 'maybe': []},
            'user_names': {'yes': [], 'no': [], 'maybe': []},
            'start_time': datetime.now(),
            'settings': settings
        }

        logger.info(f"Создан опрос с кнопкой предпросмотра: {poll_id}")
        return poll_id

    except Exception as e:
        logger.error(f"Ошибка при создании опроса: {e}")
        return None


async def update_poll_message(poll_id: str):
    """Обновляет сообщение с результатами опроса"""
    if poll_id not in active_polls:
        return

    poll_data = active_polls[poll_id]

    # Создаем обновленную клавиатуру с кнопкой предпросмотра
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"vote_{poll_id}_yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"vote_{poll_id}_no"),
                InlineKeyboardButton(text="❓ Под вопросом", callback_data=f"vote_{poll_id}_maybe")
            ],
            [
                InlineKeyboardButton(text="🔄 Сбросить голос", callback_data=f"vote_{poll_id}_reset"),
                InlineKeyboardButton(text="👀 Предпросмотр голосов", callback_data=f"preview_{poll_id}")
            ]
        ]
    )

    # Обновляем сообщение
    try:
        await bot.edit_message_text(
            chat_id=poll_data['chat_id'],
            message_id=poll_data['message_id'],
            text=format_poll_message(poll_data['settings']['poll_name'], poll_data['user_votes'], poll_id),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Ошибка обновления сообщения опроса: {e}")


async def close_poll(poll_id: str):
    """Закрытие опроса с публикацией итогов"""
    if poll_id not in active_polls:
        return

    try:
        poll_data = active_polls[poll_id]
        chat_id = poll_data['chat_id']

        # Формируем финальные результаты
        yes_voters = poll_data['user_names'].get('yes', [])
        no_voters = poll_data['user_names'].get('no', [])
        maybe_voters = poll_data['user_names'].get('maybe', [])

        result_message = format_final_results(
            poll_data['settings']['poll_name'],
            yes_voters, no_voters, maybe_voters
        )

        # Отправляем финальные результаты
        await bot.send_message(
            chat_id=chat_id,
            text=result_message,
            parse_mode=ParseMode.HTML
        )

        # Удаляем сообщение опроса или делаем его неактивным
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=poll_data['message_id'],
                reply_markup=None
            )
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=poll_data['message_id'],
                text=f"🏁 Опрос завершен: {poll_data['settings']['poll_name']}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass  # Если сообщение уже изменено или удалено

        # Удаляем из активных опросов
        del active_polls[poll_id]

        logger.info(f"Опрос {poll_id} завершен")

    except Exception as e:
        logger.error(f"Ошибка при закрытии опроса: {e}")


async def close_poll_by_settings(chat_id: str, settings_index: int):
    """Закрытие опроса по настройкам"""
    try:
        # Находим активный опрос с такими настройками
        for poll_id, poll_data in list(active_polls.items()):
            if (poll_data['chat_id'] == chat_id and poll_data['settings'] == poll_settings[chat_id][settings_index]):
                await close_poll(poll_id)
                break
            else:
                logger.warning(f"Не найден активный опрос для закрытия: chat_id={chat_id}, index={settings_index}")
    except Exception as e:
        logger.error(f"Ошибка при закрытии опроса по настройкам: {e}")


def setup_scheduler():
    """Настройка планировщика для всех опросов"""
    scheduler.remove_all_jobs()

    for chat_id, settings_list in poll_settings.items():
        for i, settings in enumerate(settings_list):
            # Джоб для создания опроса
            start_day = settings['start_day']
            start_hour = settings['start_time']['hour']
            start_minute = settings['start_time']['minute']

            scheduler.add_job(
                create_poll,
                CronTrigger(day_of_week=start_day, hour=start_hour, minute=start_minute, timezone='Europe/Moscow'),
                args=[chat_id, settings],
                id=f'poll_start_{chat_id}_{i}'
            )

            # Джоб для закрытия опроса
            end_day = settings['end_day']
            end_hour = settings['end_time']['hour']
            end_minute = settings['end_time']['minute']

            scheduler.add_job(
                close_poll_by_settings,
                CronTrigger(day_of_week=end_day, hour=end_hour, minute=end_minute, timezone='Europe/Moscow'),
                args=[chat_id, i],
                id=f'poll_end_{chat_id}_{i}'
            )

    logger.info(f"Планировщик настроен для {sum(len(v) for v in poll_settings.values())} опросов")

# ===== ОБРАБОТЧИКИ КОМАНД =====


@dp.message(Command("start"))
async def handle_start(message: Message):
    """Команда start"""
    if message.chat.type in ['group', 'supergroup']:
        await message.answer("Бот запущен! Администраторы могут использовать /set_poll для настройки.")
    else:
        await message.answer("Бot запущен! Добавьте меня в группу.")


@dp.message(Command("set_poll"))
async def handle_set_poll(message: Message, state: FSMContext):
    """Начать настройку опроса"""
    if not await check_admin(message):
        return

    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Эта команда работает только в группах!")
        return

    chat_id = str(message.chat.id)
    await state.update_data(chat_id=chat_id)
    await message.answer("Введите название опроса:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(PollCreationState.waiting_for_poll_name)


@dp.message(PollCreationState.waiting_for_poll_name)
async def process_poll_name(message: Message, state: FSMContext):
    """Обработка названия опроса"""
    if not await check_admin(message):
        await state.clear()
        return

    await state.update_data(poll_name=message.text)
    markup = get_days_inline_markup()
    await message.answer("Выберите день недели для начала опроса:", reply_markup=markup)
    await state.set_state(PollCreationState.waiting_for_start_day)


@dp.callback_query(F.data.startswith("day_"))
async def handle_day_selection(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора дня через inline кнопки"""
    if not await check_admin(callback.message):
        await state.clear()
        return

    day_number = int(callback.data.split("_")[1])
    day_name = number_to_day_name(day_number)

    current_state = await state.get_state()

    if current_state == PollCreationState.waiting_for_start_day:
        await state.update_data(start_day=day_number)
        await callback.message.answer(f"Выбран день начала: {day_name}\nВведите время начала (например: 22:05):")
        await state.set_state(PollCreationState.waiting_for_start_time)

    elif current_state == PollCreationState.waiting_for_end_day:
        await state.update_data(end_day=day_number)
        await callback.message.answer(f"Выбран день окончания: {day_name}\nВведите время окончания (например: 18:00):")
        await state.set_state(PollCreationState.waiting_for_end_time)

    await callback.answer()


@dp.message(PollCreationState.waiting_for_start_day)
async def process_start_day(message: Message, state: FSMContext):
    """Если пользователь ввел текст вместо выбора кнопки"""
    if not await check_admin(message):
        await state.clear()
        return

    markup = get_days_inline_markup()
    await message.answer("Пожалуйста, выберите день из кнопок ниже:", reply_markup=markup)


@dp.message(PollCreationState.waiting_for_start_time)
async def process_start_time(message: Message, state: FSMContext):
    """Обработка времени начала"""
    if not await check_admin(message):
        await state.clear()
        return

    try:
        time_parts = message.text.split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1])

        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError

        start_time = {'hour': hour, 'minute': minute}
        await state.update_data(start_time=start_time)

        markup = get_days_inline_markup()
        await message.answer("Выберите день недели для окончания опроса:", reply_markup=markup)
        await state.set_state(PollCreationState.waiting_for_end_day)

    except (ValueError, IndexError):
        await message.answer("Неверный формат времени. Введите время в формате ЧЧ:MM (например: 22:05):")


@dp.message(PollCreationState.waiting_for_end_day)
async def process_end_day(message: Message, state: FSMContext):
    """Если пользователь ввел текст вместо выбора кнопки"""
    if not await check_admin(message):
        await state.clear()
        return

    markup = get_days_inline_markup()
    await message.answer("Пожалуйста, выберите день из кнопок ниже:", reply_markup=markup)


@dp.message(PollCreationState.waiting_for_end_time)
async def process_end_time(message: Message, state: FSMContext):
    """Обработка времени окончания и сохранение настроек"""
    if not await check_admin(message):
        await state.clear()
        return

    try:
        time_parts = message.text.split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1])

        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError

        data = await state.get_data()
        chat_id = data['chat_id']

        end_time = {'hour': hour, 'minute': minute}

        settings = {
            'poll_name': data['poll_name'],
            'start_day': data['start_day'],
            'start_time': data['start_time'],
            'end_day': data['end_day'],
            'end_time': end_time
        }

        if chat_id not in poll_settings:
            poll_settings[chat_id] = []
        poll_settings[chat_id].append(settings)

        save_data()
        setup_scheduler()

        start_day_name = number_to_day_name(settings['start_day'])
        end_day_name = number_to_day_name(settings['end_day'])
        start_hour = settings['start_time']['hour']
        start_minute = settings['start_time']['minute']
        end_hour = settings['end_time']['hour']
        end_minute = settings['end_time']['minute']

        start_time_str = f"{start_hour:02d}:{start_minute:02d}"
        end_time_str = f"{end_hour:02d}:{end_minute:02d}"

        await message.answer(
            f"✅ Новый опрос добавлен!\n\n"
            f"📋 Название: {settings['poll_name']}\n"
            f"⏰ Начало: {start_day_name} в {start_time_str}\n"
            f"⏹️ Окончание: {end_day_name} в {end_time_str}\n\n"
            f"Всего опросов в этой группе: {len(poll_settings[chat_id])}"
        )

        await state.clear()

    except (ValueError, IndexError):
        await message.answer("Неверный формат времени. Введите время в формате ЧЧ:MM (например: 18:00):")


@dp.message(Command("poll_list"))
async def handle_poll_list(message: Message):
    """Список всех опросов в группе"""
    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Эта команда работает только в группах!")
        return

    chat_id = str(message.chat.id)

    if chat_id not in poll_settings or not poll_settings[chat_id]:
        await message.answer("В этой группе нет настроенных опросов. Используйте /set_poll для создания.")
        return

    response = "📋 Список опросов в этой группе:\n\n"
    for i, settings in enumerate(poll_settings[chat_id], 1):
        start_day_name = number_to_day_name(settings['start_day'])
        end_day_name = number_to_day_name(settings['end_day'])
        start_hour = settings['start_time']['hour']
        start_minute = settings['start_time']['minute']
        end_hour = settings['end_time']['hour']
        end_minute = settings['end_time']['minute']

        start_time_str = f"{start_hour:02d}:{start_minute:02d}"
        end_time_str = f"{end_hour:02d}:{end_minute:02d}"

        response += (f"{i}. {settings['poll_name']}\n"
                     f"   Начало: {start_day_name} в {start_time_str}\n"
                     f"   Конец: {end_day_name} в {end_time_str}\n\n")

    response += "Для удаления используйте: /delete_poll <номер>"
    await message.answer(response)


@dp.message(Command("delete_poll"))
async def handle_delete_poll(message: Message):
    """Удаление опроса"""
    if not await check_admin(message):
        return

    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Эта команда работает только в группах!")
        return

    chat_id = str(message.chat.id)

    if chat_id not in poll_settings or not poll_settings[chat_id]:
        await message.answer("В этой группе нет опросов для удаления.")
        return

    args = message.text.split()

    if len(args) == 1:
        response = "📋 Список опросов для удаления:\n\n"
        for i, settings in enumerate(poll_settings[chat_id], 1):
            start_day_name = number_to_day_name(settings['start_day'])
            end_day_name = number_to_day_name(settings['end_day'])
            start_hour = settings['start_time']['hour']
            start_minute = settings['start_time']['minute']
            end_hour = settings['end_time']['hour']
            end_minute = settings['end_time']['minute']

            start_time_str = f"{start_hour:02d}:{start_minute:02d}"
            end_time_str = f"{end_hour:02d}:{end_minute:02d}"

            response += (f"{i}. {settings['poll_name']}\n"
                         f"   Начало: {start_day_name} в {start_time_str}\n"
                         f"   Конец: {end_day_name} в {end_time_str}\n\n")

        response += "Для удаления используйте: /delete_poll <номер>"
        await message.answer(response)

    elif len(args) == 2:
        try:
            poll_number = int(args[1])
            if 1 <= poll_number <= len(poll_settings[chat_id]):
                deleted_poll = poll_settings[chat_id].pop(poll_number - 1)

                if not poll_settings[chat_id]:
                    del poll_settings[chat_id]

                save_data()
                setup_scheduler()

                await message.answer(f"✅ Опрос '{deleted_poll['poll_name']}' удален!")
            else:
                await message.answer("❌ Неверный номер опроса. Используйте /poll_list для просмотра списка.")

        except ValueError:
            await message.answer("❌ Использование: /delete_poll <номер> (номер должен быть числом)")

    else:
        await message.answer("❌ Использование: /delete_poll или /delete_poll <номер>")


@dp.message(Command("delete_all_polls"))
async def handle_delete_all_polls(message: Message):
    """Удаление всех опросов в группе"""
    if not await check_admin(message):
        return

    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Эта команда работает только в группах!")
        return

    chat_id = str(message.chat.id)

    if chat_id in poll_settings and poll_settings[chat_id]:
        count = len(poll_settings[chat_id])
        del poll_settings[chat_id]
        save_data()
        setup_scheduler()

        await message.answer(f"✅ Все {count} опросов удалены!")
    else:
        await message.answer("В этой группе нет опросов для удаления.")


@dp.message(Command("manual_poll"))
async def handle_manual_poll(message: Message):
    """Ручное создание опроса"""
    try:
        if not await check_admin(message):
            return

        if message.chat.type not in ['group', 'supergroup']:
            await message.answer("Эта команда работает только в группах!")
            return

        chat_id = str(message.chat.id)

        if chat_id in poll_settings and poll_settings[chat_id]:
            if len(poll_settings[chat_id]) > 1:
                await message.answer("Используйте /manual_poll <номер> для запуска опроса. Список: /poll_list")
                return

            poll_id = await create_poll(chat_id, poll_settings[chat_id][0])
            if poll_id:
                await message.answer("Опрос создан вручную!")

    except Exception as e:
        logger.error(f"Ошибка при ручном создании опроса: {e}")


@dp.message(Command("debug_polls"))
async def handle_debug_polls(message: Message):
    """Отладочная информация об опросах"""
    if not await check_admin(message):
        return

    chat_id = str(message.chat.id)

    debug_info = f"""
🔧 Отладочная информация:
Чат ID: {chat_id}
В poll_settings: {chat_id in poll_settings}
"""

    if chat_id in poll_settings:
        debug_info += f"Количество опросов: {len(poll_settings[chat_id])}\n"
        for i, settings in enumerate(poll_settings[chat_id]):
            debug_info += f"Опрос {i+1}: {settings['poll_name']}\n"
    else:
        debug_info += "Нет опросов в этом чате\n"

    debug_info += f"\nАктивных опросов: {len([p for p in active_polls.values() if p['chat_id'] == chat_id])}"
    debug_info += f"\nВсе чаты с опросами: {list(poll_settings.keys())}"

    await message.answer(debug_info)


# ===== ОБРАБОТЧИКИ INLINE КНОПОК =====
@dp.callback_query(F.data.startswith("vote_"))
async def handle_vote_callback(callback: types.CallbackQuery):
    """Обработка всех действий голосования"""
    try:
        data_parts = callback.data.split("_")
        poll_id = data_parts[1]
        action = data_parts[2]  # yes, no, maybe, reset

        if poll_id not in active_polls:
            await callback.answer("Опрос завершен!", show_alert=True)
            return

        poll_data = active_polls[poll_id]
        user_id = callback.from_user.id
        user_name = f"{callback.from_user.first_name} {callback.from_user.last_name or ''}".strip()

        if action == "reset":
            # Сброс голоса
            vote_removed = False
            for option in ['yes', 'no', 'maybe']:
                if user_id in poll_data['user_votes'].get(option, []):
                    poll_data['user_votes'][option].remove(user_id)
                    if user_name in poll_data['user_names'].get(option, []):
                        poll_data['user_names'][option].remove(user_name)
                    vote_removed = True

            if vote_removed:
                await update_poll_message(poll_id)
                await callback.answer("✅ Ваш голос сброшен!")
            else:
                await callback.answer("❌ У вас нет активного голоса")

        else:
            # Голосование за вариант
            previous_vote = None
            # Удаляем предыдущий голос
            for option in ['yes', 'no', 'maybe']:
                if user_id in poll_data['user_votes'].get(option, []):
                    poll_data['user_votes'][option].remove(user_id)
                    if user_name in poll_data['user_names'].get(option, []):
                        poll_data['user_names'][option].remove(user_name)
                    previous_vote = option

            # Добавляем новый голос
            poll_data['user_votes'][action].append(user_id)
            poll_data['user_names'][action].append(user_name)

            await update_poll_message(poll_id)

            if previous_vote:
                await callback.answer(
                    f"✅ Голос изменен: {get_vote_display_name(previous_vote)} → {get_vote_display_name(action)}")
            else:
                await callback.answer(f"✅ Ваш голос: {get_vote_display_name(action)}")

    except Exception as e:
        logger.error(f"Ошибка обработки голоса: {e}")
        await callback.answer("Ошибка при обработке голоса", show_alert=True)


@dp.callback_query(F.data.startswith("preview_"))
async def handle_preview_callback(callback: types.CallbackQuery):
    """Обработка нажатия кнопки предпросмотра голосов (всплывающее окно)"""
    try:
        poll_id = callback.data.split("_")[1]

        if poll_id not in active_polls:
            await callback.answer("Опрос завершен!", show_alert=True)
            return

        poll_data = active_polls[poll_id]

        # Формируем сообщение для всплывающего окна
        preview_message = format_preview_alert(poll_data)

        # Показываем во всплывающем окне (только тому, кто нажал)
        await callback.answer(preview_message, show_alert=True)

    except Exception as e:
        logger.error(f"Ошибка обработки предпросмотра: {e}")
        await callback.answer("Ошибка при загрузке результатов", show_alert=True)


async def on_startup():
    """Действия при запуске бота"""
    load_data()
    scheduler.start()
    setup_scheduler()
    logger.info("Бот запущен и планировщик настроен")


async def on_shutdown():
    """Действия при остановке бота"""
    save_data()
    scheduler.shutdown()
    await bot.session.close()


async def main():
    """Основная функция запуска"""
    await on_startup()
    try:
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == '__main__':
    asyncio.run(main())
