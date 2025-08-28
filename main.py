import asyncio
import logging
from datetime import datetime
from typing import Dict

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, PollAnswer, InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from os import getenv

load_dotenv()
API_TOKEN = getenv("TOKEN")

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s \
                    - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Инициализация бота, диспетчера, планировщика
bot = Bot(
    token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# Хранилище для данных опроса
poll_data: Dict[str, Dict] = {}
current_poll_id = None
group_chat_id = None
poll_ms_id = None


async def get_group_chat_id():
    """Получаем ID группы из сообщений бота"""
    global group_chat_id
    try:
        updates = await bot.get_updates()
        for update in updates:
            if update.message and update.message.chat.type in [
                'group', 'supergroup'
            ]:
                group_chat_id = update.message.chat.id
                logger.info(f"Найден ID группы: {group_chat_id}")
                return group_chat_id
    except Exception as e:
        logger.error(f"Ошибка при получении ID группы: {e}")
    return None


async def create_poll():
    """Создание опроса"""
    global current_poll_id, group_chat_id, poll_ms_id

    try:
        if not group_chat_id:
            group_chat_id = await get_group_chat_id()
            if not group_chat_id:
                logger.error("Не удалось получить ID группы")
                return

        logger.info(f"Попытка создать опрос в чате: {group_chat_id}")

        poll_message = await bot.send_poll(
            chat_id=group_chat_id,
            question="🎯 Треня Корал 20:00 среда",
            options=["✅ Иду", "❌ Не смогу", "❓ Под вопросом"],
            is_anonymous=False,
            allows_multiple_answers=False,
            type='regular'
        )

        current_poll_id = poll_message.poll.id
        poll_ms_id = poll_message.message_id
        poll_data[current_poll_id] = {
            'message_id': poll_message.message_id,
            'user_ids': {'✅ Да': [], '❌ Нет': [], '❓ Под вопросом': []},
            'start_time': datetime.now()
        }

        logger.info(f"Опрос создан: {current_poll_id}")

    except Exception as e:
        logger.error(f"Ошибка при создании опроса: {e}")


async def close_poll_and_publish_results():
    """Закрытие опроса и публикация результатов"""
    global current_poll_id, group_chat_id, poll_ms_id

    if not current_poll_id:
        logger.warning("Нет активного опроса для закрытия")
        return

    try:
        if not group_chat_id:
            logger.error("ID группы не установлен")
            return

        # Создаем клавиатуру с кнопкой для просмотра результатов
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="📊 Посмотреть результаты",
                    url=f"https://t.me/c/{str(group_chat_id)[4:]}/{poll_ms_id}"
                )]
            ]
        )

        # Получаем статистику для информационного сообщения
        yes_count = len(poll_data[current_poll_id]['user_ids']['✅ Да'])
        question_count = len(
            poll_data[current_poll_id]['user_ids']['❓ Под вопросом']
        )

        # Формируем информационное сообщение
        result_message = f"""
Итоги опроса:

✅ Придут: {yes_count} чел.

❓ Под вопросом: {question_count} чел.

Детальные результаты 👇
"""

        # Отправляем сообщение с кнопкой
        await bot.send_message(
            chat_id=group_chat_id,
            text=result_message,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

        logger.info("Результаты опроса опубликованы с кнопкой")

        # Очищаем данные
        current_poll_id = None
        poll_ms_id = None

    except Exception as e:
        logger.error(f"Ошибка при закрытии опроса: {e}")


@dp.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer):
    """Обработка ответов на опрос"""
    global current_poll_id, group_chat_id

    if current_poll_id and poll_answer.poll_id == current_poll_id:
        try:
            user_id = poll_answer.user.id

            option_chosen = poll_answer.option_ids[0]
            option_text = ["✅ Да", "❌ Нет", "❓ Под вопросом"][option_chosen]

            # Удаляем пользователя из всех вариантов ответов
            for option in poll_data[current_poll_id]['user_ids']:
                if user_id in poll_data[current_poll_id]['user_ids'][option]:
                    poll_data[
                        current_poll_id]['user_ids'][option].remove(user_id)

            # Добавляем пользователя к выбранному варианту
            poll_data[current_poll_id]['user_ids'][option_text].append(user_id)

            logger.info(f"Пользователь ID: {user_id} выбрал: {option_text}")

        except Exception as e:
            logger.error(f"Ошибка обработки ответа: {e}")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда start"""
    global group_chat_id

    if message.chat.type in ['group', 'supergroup']:
        group_chat_id = message.chat.id
        logger.info(f"ID группы установлен: {group_chat_id}")
        await message.answer("Бот запущен в этой группе!")
    else:
        await message.answer("Бот запущен! Добавьте меня в группу.")


@dp.message(Command("set_chat_id"))
async def cmd_set_chat_id(message: Message):
    """Установить ID чата вручную"""
    global group_chat_id
    if message.from_user.id == message.chat.id:  # только в личных сообщениях
        try:
            chat_id = int(message.text.split()[1])
            group_chat_id = chat_id
            await message.answer(f"ID группы установлен: {group_chat_id}")
        except (IndexError, ValueError):
            await message.answer("Использование: /set_chat_id <ID_чата>")


@dp.message(Command("get_chat_id"))
async def cmd_get_chat_id(message: Message):
    """Получить ID текущего чата"""
    await message.answer(f"ID этого чата: {message.chat.id}")


@dp.message(Command("manual_poll"))
async def cmd_manual_poll(message: Message):
    """Ручное создание опроса"""
    global group_chat_id

    if message.chat.type in ['group', 'supergroup']:
        group_chat_id = message.chat.id
        await create_poll()
    elif message.from_user.id == message.chat.id:  # личные сообщения
        if group_chat_id:
            await create_poll()
            await message.answer("Опрос создан!")
        else:
            await message.answer("Сначала установите ID \
                                 группы командой /set_chat_id")


@dp.message(Command("manual_results"))
async def cmd_manual_results(message: Message):
    """Ручное закрытие опроса"""
    if message.from_user.id == message.chat.id:  # только в личных сообщениях
        await close_poll_and_publish_results()
        await message.answer("Результаты опубликованы!")


@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    """Отладочная информация"""
    debug_info = f"""
📊 Отладочная информация:
• ID группы: {group_chat_id}
• Текущий опрос: {current_poll_id}
• ID сообщения опроса: {poll_ms_id}
• Данные опроса: {bool(poll_data)}
• Планировщик: {scheduler.running}
"""
    await message.answer(debug_info)


def schedule_polls():
    """Настройка расписания опросов"""
    # Каждый вторник в 12:00
    scheduler.add_job(
        create_poll,
        CronTrigger(
            day_of_week='tue',
            hour=12,
            minute=0,
            timezone='Europe/Moscow'
        ),
        id='weekly_poll'
    )

    # Каждую среду в 18:00
    scheduler.add_job(
        close_poll_and_publish_results,
        CronTrigger(
            day_of_week='wed',
            hour=18,
            minute=0,
            timezone='Europe/Moscow'
        ),
        id='weekly_poll_results'
    )


async def on_startup():
    """Действия при запуске бота"""
    scheduler.start()
    schedule_polls()
    logger.info("Бот запущен и планировщик настроен")


async def on_shutdown():
    """Действия при остановке бота"""
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
