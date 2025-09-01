import asyncio
import logging
import json
import uuid
from datetime import datetime
from typing import Dict, List

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, PollAnswer, InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove
from aiogram.types import CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from os import getenv

load_dotenv()
API_TOKEN = getenv("TOKEN")

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - \
                    %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Инициализация бота, диспетчера и планировщика
bot = Bot(
    token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
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
user_sessions: Dict[int, Dict] = {}  # user_id -> session_data


# Функция для создания клавиатуры
def get_days_markup():
    """Создает клавиатуру с днями недели"""
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


# Функции проверки администратора
async def is_admin(chat_id, user_id):
    """Проверяет, является ли пользователь администратором чата"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        status_str = str(member.status).lower()

        logger.info(f"Статус пользователя {user_id}: {status_str}")

        admin_statuses = ['administrator', 'creator', 'owner', 'admin']
        is_admin = any(
            admin_status in status_str for admin_status in admin_statuses
        )

        logger.info(f"Является администратором: {is_admin}")
        return is_admin

    except Exception as e:
        logger.error(f"Ошибка проверки прав администратора: {e}")
        return False


async def check_admin(message: Message) -> bool:
    """Проверяет права и отправляет сообщение об ошибке если нужно"""
    if message.chat.type not in ['group', 'supergroup']:
        return True

    if not await is_admin(message.chat.id, message.from_user.id):
        await message.answer(
            "❌ Эта команда доступна только администраторам группы!"
        )
        return False
    return True


def load_data():
    """Загрузка данных из файла"""
    global poll_settings
    try:
        with open('poll_data.json', 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Очищаем пустые записи
        poll_settings = {
            chat_id: settings for chat_id,
            settings in data.items() if settings
        }
        logger.info(f"Данные загружены из файла: \
                    {len(poll_settings)} чатов с опросами")

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


async def create_poll(chat_id: str, settings: Dict):
    """Создание опроса"""
    try:
        poll_message = await bot.send_poll(
            chat_id=chat_id,
            question=settings['poll_name'],
            options=["✅ Да", "❌ Нет", "❓ Под вопросом"],
            is_anonymous=False,
            allows_multiple_answers=False,
            type='regular'
        )

        poll_id = str(uuid.uuid4())
        active_polls[poll_id] = {
            'chat_id': chat_id,
            'poll_id': poll_message.poll.id,
            'message_id': poll_message.message_id,
            'user_ids': {'✅ Да': [], '❌ Нет': [], '❓ Под вопросом': []},
            'start_time': datetime.now(),
            'settings': settings,
            'original_poll_id': poll_message.poll.id
        }

        logger.info(
            f"Опрос создан: ID={poll_id}, chat={chat_id}, \
            name={settings['poll_name']}"
        )
        return poll_id

    except Exception as e:
        logger.error(f"Ошибка при создании опроса в чате {chat_id}: {e}")
        return None


async def close_poll(poll_id: str):
    """Закрытие опроса"""
    if poll_id not in active_polls:
        logger.warning(f"Опрос {poll_id} не найден в активных опросах")
        return

    try:
        poll_data = active_polls[poll_id]
        chat_id = poll_data['chat_id']

        # Останавливаем опрос - голосование становится невозможным
        await bot.stop_poll(chat_id, poll_data['message_id'])

        # Создаем клавиатуру с кнопкой для просмотра результатов
        keyboard = InlineKeyboardMarkup(
          inline_keyboard=[
            [InlineKeyboardButton(
             text="📊 Посмотреть результаты",
             url=f"https://t.me/c/{str(chat_id)[4:]}/{poll_data['message_id']}"
             )]
            ]
        )

        # Получаем статистику
        yes_count = len(poll_data['user_ids']['✅ Да'])
        question_count = len(poll_data['user_ids']['❓ Под вопросом'])

        # Формируем сообщение
        result_message = f"""
🏆 Опрос "{poll_data['settings']['poll_name']}" завершен!

📊 Итого:
✅ Придут: {yes_count} чел.
❓ Под вопросом: {question_count} чел.

Детальные результаты 👇
"""

        await bot.send_message(
            chat_id=chat_id,
            text=result_message,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

        logger.info(f"Результаты опроса опубликованы в чате {chat_id}")

        # Удаляем из активных опросов
        del active_polls[poll_id]

    except Exception as e:
        logger.error(f"Ошибка при закрытии опроса {poll_id}: {e}")


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
                CronTrigger(
                    day_of_week=start_day,
                    hour=start_hour,
                    minute=start_minute,
                    timezone='Europe/Moscow'
                ),
                args=[chat_id, settings],
                id=f'poll_start_{chat_id}_{i}'
            )

            # Закрытие опроса
            end_day = settings['end_day']
            end_hour = settings['end_time']['hour']
            end_minute = settings['end_time']['minute']

            # Сохраняем информацию об опросе для закрытия
            scheduler.add_job(
                close_poll_by_settings,
                CronTrigger(
                    day_of_week=end_day,
                    hour=end_hour,
                    minute=end_minute,
                    timezone='Europe/Moscow'
                ),
                args=[chat_id, i],
                id=f'poll_end_{chat_id}_{i}'
            )

    logger.info(f"Планировщик настроен для \
                {sum(len(v) for v in poll_settings.values())} опросов")


async def close_poll_by_settings(chat_id: str, settings_index: int):
    """Закрытие опроса по настройкам"""
    try:
        # Находим активный опрос с такими настройками
        for poll_id, poll_data in list(active_polls.items()):
            if (
                poll_data['chat_id'] == chat_id and
                poll_data['settings'] == poll_settings[chat_id][settings_index]
               ):
                await close_poll(poll_id)
                break
        else:
            logger.warning(f"Не найден активный опрос для закрытия: \
                           chat_id={chat_id}, index={settings_index}")
    except Exception as e:
        logger.error(f"Ошибка при закрытии опроса по настройкам: {e}")


@dp.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer):
    """Обработка ответов на опрос"""
    for poll_id, poll_data in list(active_polls.items()):
        if poll_data['original_poll_id'] == poll_answer.poll_id:
            try:
                user_id = poll_answer.user.id
                option_chosen = poll_answer.option_ids[0]
                option_text = [
                    "✅ Да",
                    "❌ Нет",
                    "❓ Под вопросом"
                ][option_chosen]

                # Удаляем пользователя из всех вариантов
                for option in poll_data['user_ids']:
                    if user_id in poll_data['user_ids'][option]:
                        poll_data['user_ids'][option].remove(user_id)

                # Добавляем к выбранному варианту
                poll_data['user_ids'][option_text].append(user_id)

                logger.info(f"Пользователь {user_id} в опросе \
                            {poll_id} выбрал: {option_text}")

            except Exception as e:
                logger.error(f"Ошибка обработки ответа: {e}")
            break


@dp.message(Command("start"))
async def handle_start(message: Message):
    """Команда start"""
    if not await check_admin(message):
        return

    if message.chat.type in ['group', 'supergroup']:
        await message.answer(
            '''Бот проведения опросов о тренировках запущен!\n
Используйте /set_poll для настройки.'''
        )
    else:
        await message.answer(
            '''Бот проведения опросов о тренировках запущен!\n
Добавьте меня в группу.'''
        )


@dp.message(Command("set_poll"))
async def handle_set_poll(message: Message, state: FSMContext):
    """Начать настройку опроса"""
    if not await check_admin(message):
        return

    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Эта команда работает только в группах!")
        return

    chat_id = str(message.chat.id)

    # Сохраняем информацию о чате
    await state.update_data(chat_id=chat_id)

    # Запрашиваем название опроса
    await message.answer(
        "Введите название опроса:", reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(PollCreationState.waiting_for_poll_name)


@dp.message(Command("active_polls"))
async def handle_active_polls(message: Message):
    """Показать активные опросы (для отладки)"""
    if not active_polls:
        await message.answer("Нет активных опросов")
        return

    response = "📊 Активные опросы:\n\n"
    for poll_id, poll_data in active_polls.items():
        response += (f"ID: {poll_id}\n"
                     f"Чат: {poll_data['chat_id']}\n"
                     f"Название: {poll_data['settings']['poll_name']}\n"
                     f"Голосов: ✅{len(poll_data['user_ids']['✅ Да'])} "
                     f"❓{len(poll_data['user_ids']['❓ Под вопросом'])} "
                     f"❌{len(poll_data['user_ids']['❌ Нет'])}\n\n")

    await message.answer(response)


@dp.message(PollCreationState.waiting_for_poll_name)
async def process_poll_name(message: Message, state: FSMContext):
    """Обработка названия опроса"""
    if not await check_admin(message):
        return

    await state.update_data(poll_name=message.text)

    # Запрашиваем день начала
    await message.answer(
        "Выберите день недели для начала опроса:",
        reply_markup=get_days_markup()
    )
    await state.set_state(PollCreationState.waiting_for_start_day)


@dp.callback_query(F.data.startswith("day_"))
async def handle_day_selection(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора дня"""
    day_number = int(callback.data.split("_")[1])
    day_name = number_to_day_name(day_number)

    current_state = await state.get_state()

    if current_state == PollCreationState.waiting_for_start_day:
        await state.update_data(start_day=day_number)
        await callback.message.answer(f"""Выбран день начала: {day_name}\n
Введите время начала опроса (например: 12:00):""")
        await state.set_state(PollCreationState.waiting_for_start_time)

    elif current_state == PollCreationState.waiting_for_end_day:
        await state.update_data(end_day=day_number)
        await callback.message.answer(f"""Выбран день окончания: {day_name}\n
Введите время окончания опроса (например: 18:00):""")
        await state.set_state(PollCreationState.waiting_for_end_time)

    await callback.answer()


@dp.message(PollCreationState.waiting_for_start_day)
async def process_start_day(message: Message, state: FSMContext):
    """Обработка дня начала"""
    if not await check_admin(message):
        return

    if message.text.lower() not in [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье"
    ]:
        await message.answer(
            "Пожалуйста, выберите день из предложенных вариантов:",
            reply_markup=get_days_markup()
        )
        return

    await state.update_data(start_day=day_name_to_number(message.text.lower()))

    # Запрашиваем время начала
    await message.answer(
        "Введите время начала опроса (например: 12:00):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(PollCreationState.waiting_for_start_time)


@dp.message(PollCreationState.waiting_for_start_time)
async def process_start_time(message: Message, state: FSMContext):
    """Обработка времени начала"""
    if not await check_admin(message):
        return

    try:
        time_parts = message.text.split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1])

        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError

        # Сохраняем время начала
        start_time = {'hour': hour, 'minute': minute}
        await state.update_data(start_time=start_time)

        # Запрашиваем день окончания
        await message.answer(
            "Выберите день недели для окончания опроса:",
            reply_markup=get_days_markup()
        )
        await state.set_state(PollCreationState.waiting_for_end_day)

    except (ValueError, IndexError):
        await message.answer(
            """Неверный формат времени.\n
Введите время в формате ЧЧ:MM (например: 12:00):""")


@dp.message(PollCreationState.waiting_for_end_day)
async def process_end_day(message: Message, state: FSMContext):
    """Обработка дня окончания"""
    if not await check_admin(message):
        return

    if message.text.lower() not in [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье"
    ]:
        await message.answer(
            "Пожалуйста, выберите день из предложенных вариантов:",
            reply_markup=get_days_markup()
        )
        return

    await state.update_data(end_day=day_name_to_number(message.text.lower()))

    # Запрашиваем время окончания
    await message.answer(
        "Введите время окончания опроса (например: 18:00):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(PollCreationState.waiting_for_end_time)


@dp.message(PollCreationState.waiting_for_end_time)
async def process_end_time(message: Message, state: FSMContext):
    """Обработка времени окончания и сохранение настроек"""
    if not await check_admin(message):
        return

    try:
        time_parts = message.text.split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1])

        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError

        # Получаем все данные
        data = await state.get_data()
        chat_id = data['chat_id']

        # Сохраняем время окончания
        end_time = {'hour': hour, 'minute': minute}

        settings = {
            'poll_name': data['poll_name'],
            'start_day': data['start_day'],
            'start_time': data['start_time'],
            'end_day': data['end_day'],
            'end_time': end_time
        }

        # Добавляем настройки в список для этого чата
        if chat_id not in poll_settings:
            poll_settings[chat_id] = []
        poll_settings[chat_id].append(settings)

        save_data()

        # Перезапускаем планировщик
        setup_scheduler()

        # Форматируем информацию для ответа
        start_day_name = number_to_day_name(settings['start_day'])
        end_day_name = number_to_day_name(settings['end_day'])
        start_time_str = "{:02d}:{:02d}".format(
                settings['start_time']['hour'],
                settings['start_time']['minute']
        )
        end_time_str = "{:02d}:{:02d}".format(
                settings['end_time']['hour'],
                settings['end_time']['minute']
        )
        await message.answer(
            f"✅ Новый опрос добавлен!\n\n"
            f"📋 Название: {settings['poll_name']}\n"
            f"⏰ Начало: {start_day_name} в {start_time_str}\n"
            f"⏹️ Окончание: {end_day_name} в {end_time_str}\n\n"
            f"Всего опросов в этой группе: {len(poll_settings[chat_id])}"
        )

        await state.clear()

    except (ValueError, IndexError):
        await message.answer(
            """Неверный формат времени.\n
Введите время в формате ЧЧ:MM (например: 18:00):""")


@dp.message(Command("poll_list"))
async def handle_poll_list(message: Message):
    """Список всех опросов в группе"""
    print(f"Обработка /poll_list для чата {message.chat.id}")  # Debug

    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("Эта команда работает только в группах!")
        return

    chat_id = str(message.chat.id)

    # Проверяем есть ли опросы в этой группе
    if chat_id not in poll_settings or not poll_settings[chat_id]:
        await message.answer(
            """В этой группе нет настроенных опросов.\n
Используйте /set_poll для создания."""
        )
        return

    response = "📋 Список опросов в этой группе:\n\n"
    for i, settings in enumerate(poll_settings[chat_id], 1):
        start_day_name = number_to_day_name(settings['start_day'])
        end_day_name = number_to_day_name(settings['end_day'])
        start_time_str = "{:02d}:{:02d}".format(
                settings['start_time']['hour'],
                settings['start_time']['minute']
        )
        end_time_str = "{:02d}:{:02d}".format(
                settings['end_time']['hour'],
                settings['end_time']['minute']
        )
        response += (f"{i}. {settings['poll_name']}\n"
                     f"   Начало: {start_day_name} в {start_time_str}\n"
                     f"   Конец: {end_day_name} в {end_time_str}\n\n")

    response += "Для удаления используйте: /delete_poll номер"
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

    # Проверяем есть ли опросы в этой группе
    if chat_id not in poll_settings or not poll_settings[chat_id]:
        await message.answer("В этой группе нет опросов для удаления.")
        return

    # Парсим аргументы команды
    args = message.text.split()

    if len(args) == 1:
        # Команда без аргументов - показываем список опросов
        response = "📋 Список опросов для удаления:\n\n"
        for i, settings in enumerate(poll_settings[chat_id], 1):
            start_day_name = number_to_day_name(settings['start_day'])
            end_day_name = number_to_day_name(settings['end_day'])
            start_time_str = "{:02d}:{:02d}".format(
                    settings['start_time']['hour'],
                    settings['start_time']['minute']
            )
            end_time_str = "{:02d}:{:02d}".format(
                    settings['end_time']['hour'],
                    settings['end_time']['minute']
            )
            response += (f"{i}. {settings['poll_name']}\n"
                         f"   Начало: {start_day_name} в {start_time_str}\n"
                         f"   Конец: {end_day_name} в {end_time_str}\n\n")

        response += "Для удаления используйте: /delete_poll номер"
        await message.answer(response)

    elif len(args) == 2:
        # Команда с номером опроса
        try:
            poll_number = int(args[1])
            if 1 <= poll_number <= len(poll_settings[chat_id]):
                # Удаляем опрос
                deleted_poll = poll_settings[chat_id].pop(poll_number - 1)

                # Если опросов не осталось, удаляем запись чата
                if not poll_settings[chat_id]:
                    del poll_settings[chat_id]

                save_data()
                setup_scheduler()

                await message.answer(
                    f"✅ Опрос '{deleted_poll['poll_name']}' удален!"
                )
            else:
                await message.answer(
                    """Неверный номер опроса.\n
Используйте /poll_list для просмотра списка."""
                )

        except ValueError:
            await message.answer(
                "Использование: /delete_poll номер (должно быть число)"
            )

    else:
        await message.answer(
            "Использование: /delete_poll или /delete_poll номер"
        )


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

    debug_info += f"\nВсе чаты с опросами: {list(poll_settings.keys())}"

    await message.answer(debug_info)


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
