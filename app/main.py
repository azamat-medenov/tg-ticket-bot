import os
import json
import atexit
import asyncio
import logging
import uuid

import httpx

import aioschedule as schedule
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from datetime import datetime, UTC, timedelta
from dotenv import load_dotenv

from dataclasses import dataclass

logging.basicConfig(level=logging.INFO)

load_dotenv()  # Загрузка переменных из .env
# Чтение переменных
TOKEN = os.getenv("TOKEN")
REMINDER_INTERVAL = int(os.getenv("REMINDER_INTERVAL"))
REMINDER_TOPIC_ID = int(os.getenv("REMINDER_TOPIC_ID"))
STATUS_TOPIC_ID = int(os.getenv("STATUS_TOPIC_ID"))
BD_HOST = str(os.getenv("BD_HOST"))
LUCKYPAY_API_KEY = os.getenv("LUCKYPAY_API_KEY")
LUCKYPAY_URL = os.getenv("LUCKYPAY_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()
active_tickets = {}
scheduled_jobs = {}


@dataclass
class TicketFromAPI:
    client_name: str
    provider_name: str
    amount: str
    requisites: str
    provider_order_id: str
    message_id: int | None = None


def is_uuid(string: str) -> bool:
    try:
        uuid.UUID(string)
        return True
    except ValueError:
        return False


def now_utc3() -> datetime:
    return datetime.now(UTC) + timedelta(hours=3)


async def get_ticket_from_db(ticket_number: str) -> TicketFromAPI | None:
    for ticket in ticket_number.split():
        if is_uuid(ticket):  # наши id всегда UUID
            try:
                async with httpx.AsyncClient() as http_client:
                    order_response = await http_client.get(
                        LUCKYPAY_URL + f"/api/v1/order/{ticket}",
                        headers={"X-API-Key": LUCKYPAY_API_KEY}
                    )
                    data = order_response.json()

                    logging.info(f"Response received from LuckyPay {data}")

                    order_response.raise_for_status()

                    data = data["order"]

                    provider_order_id = data["provider_order_id"]
                    provider_id = data["provider_id"]
                    client_id = data["client_id"]

                    provider_response = await http_client.get(
                        LUCKYPAY_URL + f"/api/v1/provider/{provider_id}",
                        headers={"X-API-Key": LUCKYPAY_API_KEY}
                    )

                    provider_name = provider_response.json()["name"]

                    client_response = await http_client.get(
                        LUCKYPAY_URL + f"/api/v1/client/{client_id}",
                        headers={"X-API-Key": LUCKYPAY_API_KEY}
                    )
                    client_name = client_response.json()["name"]

                    return TicketFromAPI(
                        provider_name=provider_name,
                        client_name=client_name,
                        amount=str(data["amount"]),
                        requisites=data["holder_account"],
                        provider_order_id=provider_order_id

                    )
            except Exception as e:
                logging.error(f" === APP_LOG: error getting ticket in luckypay db, ticket - {ticket} {e}")


# Загрузка заявок из файла
def load_tickets():
    global active_tickets
    try:
        with open(BD_HOST, "r", encoding='utf-8') as file:
            active_tickets = json.load(file)
        logging.info(f" === APP_LOG: Loaded Tickets — successful: {active_tickets}")

    except FileNotFoundError:
        active_tickets = {}
        logging.info(" === APP_LOG: No tickets file found. Starting fresh.")


# Сохранение заявок в файл
def save_tickets():
    try:
        # Открываем файл в текстовом режиме записи
        with open(BD_HOST, "w", encoding="utf-8") as file:
            # Сериализуем данные в JSON и записываем
            json.dump(active_tickets, file, indent=4, ensure_ascii=False)
        logging.info(" === APP_LOG: Tickets saved successfully.")
    except Exception as e:
        logging.error(f" === APP_LOG: Error saving tickets: {e}")


# Загрузка задач для активных заявок
def load_scheduler_jobs():
    for ticket_number in active_tickets:
        logging.info(f" === APP_LOG: Scheduler Job for ticket {ticket_number} restored.")
        schedule_reminder(ticket_number)
        logging.info(f" === APP_LOG: Loaded Scheduler Jobs — successful")


# Создание напоминания в планировщике
def schedule_reminder(ticket_number):
    # Создаем задачу и сохраняем ее в словаре
    scheduled_jobs[ticket_number] = schedule.every(REMINDER_INTERVAL).seconds.do(
        lambda: asyncio.create_task(send_reminder(ticket_number))
    )
    logging.info(f" === APP_LOG: Scheduler Job for ticket \"{ticket_number}\" created.")


# Удаление напоминания из планировщика
def remove_reminder(ticket_number):
    job = scheduled_jobs.pop(ticket_number, None)
    if job:
        schedule.cancel_job(job)
        logging.info(f" === APP_LOG: Scheduler Job for ticket \"{ticket_number}\" removed.")
    else:
        logging.warning(f" === APP_LOG: Scheduler Job for ticket \"{ticket_number}\" not found.")


def count_elapsed_time(ticket: dict) -> int:
    start_time = datetime.strptime(ticket["start_time"], '%H:%M %d.%m.%Y').replace(tzinfo=UTC)
    elapsed_time = datetime.now(UTC) + timedelta(hours=3) - start_time  # Разница во времени
    return int(elapsed_time.total_seconds() // 60)


# Отправка напоминания
async def send_reminder(ticket_number: str) -> None:
    try:
        if (active_tickets[ticket_number]['remind_times'] * REMINDER_INTERVAL
                - REMINDER_INTERVAL != REMINDER_INTERVAL and
                active_tickets[ticket_number]['remind_times'] % 2 == 0
        ):
            # проверка чтобы добавить 45 минут в 30 минутный интервал
            active_tickets[ticket_number]['remind_times'] += 1
            return

        logging.info(f" === APP_LOG: send_reminder called for ticket {ticket_number}")
        ticket = active_tickets.get(ticket_number)
        if ticket is None:
            logging.error(f" === APP_LOG: not found found ticket to send reminder")
            return
        ticket["remind_times"] += 1

        # Отправляем сообщение как ответ на исходное сообщение
        sent_message = await bot.send_message(
            chat_id=ticket["chat_id"],
            text=f"{ticket_number} прошло {count_elapsed_time(ticket)} мин.",
            message_thread_id=REMINDER_TOPIC_ID
        )
        await remove_notifications(ticket, ticket_number)
        # Сохраняем message_id отправленного сообщения
        ticket["notification_messages"].append(sent_message.message_id)

        # Сохраняем обновленную заявку
        save_tickets()

        logging.info(f" === APP_LOG: Reminder sent for ticket {ticket_number}")
    except Exception as e:
        logging.error(f" === APP_LOG: Error in send_reminder for ticket {ticket_number}: {e}")


def date_time_formatter(start_time: str) -> str:
    try:
        # Преобразование строки в объект datetime
        start_time_obj = datetime.strptime(start_time, '%H:%M %d.%m.%Y')
        # Форматирование с разделителем "—"
        formatted_start_time = start_time_obj.strftime('%H:%M — %d.%m.%Y')
        return formatted_start_time
    except Exception as e:
        logging.error(f" === APP_LOG: Error in date_time_formater: {e}")
        return start_time  # Возврат исходной строки, если произошла ошибка


async def remove_notifications(
        ticket: dict,
        ticket_number: str,
) -> None:
    notification_messages = ticket.get("notification_messages", [])
    for msg_id in notification_messages:
        try:
            await bot.delete_message(chat_id=ticket["chat_id"], message_id=msg_id)
            logging.info(f" === APP_LOG: Deleted notification message {msg_id} for ticket {ticket_number}")
        except Exception as e:
            logging.error(
                f" === APP_LOG: Failed to delete notification message {msg_id} for ticket {ticket_number}: {e}")

    active_tickets[ticket_number]["notification_messages"] = []


async def close_ticket(
        ticket_number: str,
        chat_id: str,
        message: Message,
        sla: bool | None = False,
        change_status: bool | None = True
) -> None:
    try:
        # Удаляем сообщение с коммандой
        await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
        logging.info(f" === APP_LOG: Message '{message.text}' for ticket {ticket_number} deleted.")
    except Exception as e:
        logging.error(f" === APP_LOG: Failed to delete '{message.text}' message for ticket {ticket_number}: {e}")

    if ticket_number not in active_tickets:
        logging.error(f" === APP_LOG: ticket {ticket_number} not found in active tickets")
        try:
            # Удаляем сообщение с заявкой
            await bot.delete_message(chat_id=chat_id, message_id=message.reply_to_message.message_id)
            logging.info(
                f" === APP_LOG: Message {message.reply_to_message.message_id} for ticket {ticket_number} deleted.")
        except Exception as e:
            logging.error(
                f" === APP_LOG: Failed to delete message {message.reply_to_message.message_id} for ticket {ticket_number}: {e}")

        return

    ticket = active_tickets[ticket_number]

    if change_status:
        text = (f"{ticket_number}\n📥 открыт в {date_time_formatter(ticket['start_time'])}\n✅ закрыт "
                f"в {date_time_formatter(now_utc3().strftime('%H:%M %d.%m.%Y'))}")
        if sla:
            text += f"\n🟥SLA {count_elapsed_time(ticket)} мин."

        # Отправляем сообщение о закрытии в тему Статус
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=ticket['opens_message_id'],
            text=text
        )

    remove_reminder(ticket_number)  # Удаляем задачу из планировщика
    # Удаляем все сообщения-оповещения
    await remove_notifications(ticket, ticket_number)
    # Удаляем сообщение с деталями
    try:
        await bot.delete_message(chat_id=chat_id, message_id=ticket["detail_message_id"])
    except Exception as e:
        logging.error(f" APP_LOG: Failed to delete detail message for ticket {ticket_number} {e}")
    del active_tickets[ticket_number]  # Удаляем из списка активных заявок
    save_tickets()  # Сохраняем изменения
    logging.info(f" === APP_LOG: Removed ticket {ticket_number}")

    try:
        # Удаляем сообщение с заявкой
        await bot.delete_message(chat_id=chat_id, message_id=message.reply_to_message.message_id)
        logging.info(
            f" === APP_LOG: Message {message.reply_to_message.message_id} for ticket {ticket_number} deleted.")
    except Exception as e:
        logging.error(
            f" === APP_LOG: Failed to delete message {message.reply_to_message.message_id} for ticket {ticket_number}: {e}")


def get_ticket_from_reply(message: Message) -> str:
    """проверяет сообщение, на которую вызвали командуб и извлекает ticket number"""
    if message.reply_to_message is None:
        logging.warning(f"'{message.text}' command was used without reply")
        return
    if message.reply_to_message.caption is None:
        logging.warning(f"replied not to photo")
        return

    return " ".join(message.reply_to_message.caption.split())


# --- Команды бота ---------------------------------------------------------
@router.message()
async def handle_message(message: Message):
    # Проверяем, что сообщение пришло из группы или супергруппы
    if message.chat.type in ["group", "supergroup"]:
        logging.info(f" === APP_LOG: Message received from the group: {message.chat.title} | {message.text}")
        now = now_utc3().strftime('%H:%M %d.%m.%Y')
        chat_id = message.chat.id
        topic_id = message.message_thread_id

        # Проверка на наличие текста в сообщении
        if message.text is None:
            logging.warning("Received a message without text.")
            return

        # Открыть заявку
        if message.text == "+":
            ticket_number = get_ticket_from_reply(message)

            if ticket_number is None:
                return

            chat_id = message.reply_to_message.chat.id
            topic_id = message.reply_to_message.message_thread_id

            if ticket_number in active_tickets:
                logging.warning(f"Ticket {ticket_number} already exists.")
                await message.reply(f"Ticket {ticket_number} already exists.")
            else:
                ticket = await get_ticket_from_db(ticket_number)

                if ticket is None:
                    logging.warning(f" === APP_LOG: no ticket got from db - {ticket_number} ")
                else:
                    try:
                        ticket.message_id = (await message.reply_to_message.reply(
                            f"Провайдер: {ticket.provider_name}\nМерчант: {ticket.client_name}\n"
                            f"Сумма: {ticket.amount}\nРеквизиты: {ticket.requisites}\n\n"
                            f"{ticket.provider_order_id}"
                        )).message_id
                    except Exception:
                        logging.error(f" === APP_LOG: Failed to send detail message for ticket {ticket_number}")

                # Отправляем сообщение в тему Статус
                opens_message_id = await bot.send_message(
                    chat_id=chat_id,
                    text=f"{ticket_number}\n📥 открыт в {date_time_formatter(now)}",
                    message_thread_id=STATUS_TOPIC_ID
                )

                active_tickets[ticket_number] = {
                    "start_time": now,
                    "chat_id": chat_id,
                    "message_thread_id": topic_id,
                    "message_id": message.message_id,
                    "opens_message_id": opens_message_id.message_id,
                    "detail_message_id": ticket.message_id if ticket else None,
                    "remind_times": 0,
                    "notification_messages": []
                }
                save_tickets()
                schedule_reminder(ticket_number)

            try:
                # Удаляем сообщение с +
                await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
                logging.info(f" === APP_LOG: Message '{message.text}'for ticket {ticket_number} deleted.")
            except Exception as e:
                logging.error(f"Failed to delete message '{message.text}' for ticket {ticket_number}: {e}")

        elif message.text == "-":
            ticket_number = get_ticket_from_reply(message)

            logging.info(
                f" === APP_LOG: {now_utc3().strftime('%H:%M %d.%m.%Y')}: closing '{message.text}' method was used {ticket_number}")

            await close_ticket(ticket_number, chat_id, message)

        elif message.text == "!":
            ticket_number = get_ticket_from_reply(message)

            logging.info(
                f" === APP_LOG: {now_utc3().strftime('%H:%M %d.%m.%Y')}: closing '{message.text}' method was used {ticket_number}")

            await close_ticket(ticket_number, chat_id, message, sla=True)

        elif message.text.startswith("- "):
            # Извлекаем номер заявки
            ticket_number = " ".join(message.text.split()[1:])

            logging.info(
                f" === APP_LOG: {datetime.now().strftime('%H:%M %d.%m.%Y')}: ticket={ticket_number} Method=\"- \"")

            try:
                await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
                logging.info(f" === APP_LOG: Message for ticket {ticket_number} deleted.")
            except Exception as e:
                logging.error(f" === APP_LOG: Failed to delete message for ticket {ticket_number}: {e}")

            remove_reminder(ticket_number)

            if ticket_number in active_tickets:
                ticket = active_tickets.get(ticket_number)
                await remove_notifications(ticket, ticket_number)
                logging.info(
                    f" === APP_LOG reminder for ticket {ticket_number} deleted"
                )
        elif message.text.startswith("! "):
            logging.info(f" === APP_LOG: got '! ' command")
            ticket_number = " ".join(message.text.split()[1:])

            try:
                await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
                logging.info(f" === APP_LOG: Message for ! ticket {ticket_number} deleted.")
            except Exception as e:
                logging.error(f" === APP_LOG: Failed to delete ! message for ticket {ticket_number}: {e}")

            await close_ticket(ticket_number, chat_id, message, sla=True)

        elif message.text.startswith("— "):
            logging.info(f' === APP_LOG: got -- command')
            ticket_number = " ".join(message.text.split()[1:])

            try:
                await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
                logging.info(f" === APP_LOG: Message '--' for ticket {ticket_number} deleted.")
            except Exception as e:
                logging.error(f" === APP_LOG: Failed to delete message '--' for ticket {ticket_number}: {e}")

            await close_ticket(ticket_number, chat_id, message, change_status=False)

        elif message.text.startswith("—- "):
            logging.info(" === APP_LOG: got --- command")
            ticket_number = " ".join(message.text.split()[1:])

            try:
                await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
                logging.info(f" === APP_LOG: Message '---' for ticket {ticket_number} deleted.")
            except Exception as e:
                logging.error(f" === APP_LOG: Failed to delete message '---' for ticket {ticket_number}: {e}")

            await close_ticket(ticket_number, chat_id, message)

        # Показать открытые заявки
        elif "delete" == message.text:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
                logging.info(f" === APP_LOG: Delete command deleted.")
            except Exception as e:
                logging.error(f" === APP_LOG: Failed to delete Delete command: {e}")
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message.reply_to_message.message_id)
            except Exception:
                logging.error(f" === APP_LOG: Failed to delete message: {e}")


        elif "list" == message.text.lower():
            chunk_size = 4096
            json_data = json.dumps(active_tickets, ensure_ascii=False, indent=4)
            chunks = [json_data[i:i + chunk_size] for i in range(0, len(json_data), chunk_size)]

            # Send each chunk as a separate message
            for chunk in chunks:
                await bot.send_message(chat_id, chunk)

        # Показать содержимое файла tickets.json
        elif "dump" == message.text.lower():
            try:
                with open(BD_HOST, "r", encoding="utf-8") as file:
                    file_content = json.load(file)

                formatted_content = json.dumps(file_content, indent=4, ensure_ascii=False)

                await bot.send_message(
                    chat_id=message.chat.id,
                    text=f"<pre>{formatted_content}</pre>",
                    parse_mode="HTML",
                    message_thread_id=message.message_thread_id
                )
            except FileNotFoundError:
                logging.error(" === APP_LOG: tickets.json file not found.")
            except json.JSONDecodeError as e:
                logging.error(f" === APP_LOG: Error decoding tickets.json: {e}")
            except Exception as e:
                logging.error(f" === APP_LOG: Unexpected error while reading tickets.json: {e}")

        # Помощь по командам
        elif "bot help" == message.text.lower():
            logging.info(f" === APP_LOG: {now_utc3().strftime('%H:%M %d.%m.%Y')}: Method \"bot help\" triggered")

            help_text = (
                "📋 **Доступные команды**:\n"
                "1. **Открыть заявку:**\n"
                "Напишите `+ `<номер заявки> чтобы создать новое оповещение.\n"
                "   _Пример: + 1234_\n\n"
                "2. **Закрыть заявку:**\n"
                "Напишите `- `<номер заявки> чтобы удалить оповещение.\n"
                "   _Пример: - 1234_\n\n"
                "3. **Показать открытые заявки:**\n"
                "   Напишите `list` для просмотра списка открытых оповещений.\n"
            )

            await message.reply(help_text, parse_mode="Markdown")

        # Вернуть ID топика
        elif "tid" == message.text:
            logging.info(f" === APP_LOG: thread_id = {message.message_thread_id}")


# --- Инициализация ---------------------------------------------------------

logging.info(f" === APP_LOG: Inited Router  — {dp.include_router(router)}")  # Регистрация маршрутизатора
load_tickets()  # Загрузка заявок из БД
load_scheduler_jobs()  # Загрузка задач планировщика из БД


# Основной цикл для выполнения задач планировщика
async def run_scheduler():
    while True:
        for job in schedule.jobs:
            if job.should_run:
                await asyncio.create_task(job.run())
        await asyncio.sleep(1)


# Основная функция запуска
async def main():
    asyncio.create_task(run_scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    atexit.register(save_tickets)
    asyncio.run(main())
