import asyncio
import logging
import email
import sqlite3
from email.header import decode_header
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import aioimaplib
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = 'ТВОЙ_ТОКЕН_TELEGRAM' # Токен бота Telegram
GITHUB_URL = 'https://github.com/nagornyidan/pochtalion_bot/blob/main/README.md' # Ссылка на readme

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- БД ---
def init_db():
    with sqlite3.connect('users.db') as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS accounts_v5 (
                user_id INTEGER, 
                email TEXT, 
                password TEXT, 
                service TEXT,
                last_uid TEXT,
                UNIQUE(user_id, email)
            )
        ''')
        conn.commit()

def add_account(uid, em, pw, sv):
    with sqlite3.connect('users.db') as conn:
        conn.execute('INSERT OR REPLACE INTO accounts_v5 (user_id, email, password, service) VALUES (?, ?, ?, ?)', (uid, em, pw, sv))
        conn.commit()

def update_last_uid(uid, em, last_uid):
    with sqlite3.connect('users.db') as conn:
        conn.execute('UPDATE accounts_v5 SET last_uid = ? WHERE user_id = ? AND email = ?', (last_uid, uid, em))
        conn.commit()

def get_all_accounts(uid):
    with sqlite3.connect('users.db') as conn:
        return conn.execute('SELECT email, service FROM accounts_v5 WHERE user_id = ?', (uid,)).fetchall()

def get_account_details(uid, em):
    with sqlite3.connect('users.db') as conn:
        return conn.execute('SELECT email, password, service FROM accounts_v5 WHERE user_id = ? AND email = ?', (uid, em)).fetchone()

def get_all_global_accounts():
    with sqlite3.connect('users.db') as conn:
        return conn.execute('SELECT user_id, email, password, service, last_uid FROM accounts_v5').fetchall()

# --- Состояния FSM ---
class MailAuth(StatesGroup):
    waiting_for_service = State()
    waiting_for_email = State()
    waiting_for_password = State()

# --- Дополнительные функции ---
def decode_mime_words(s):
    if not s: return "Без темы"
    res = []
    try:
        for word, encoding in decode_header(s):
            if isinstance(word, bytes):
                res.append(word.decode(encoding or 'utf-8', errors='ignore'))
            else:
                res.append(str(word))
    except: return str(s)
    return "".join(res)

def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📥 Мои ящики")
    builder.button(text="➕ Добавить почту")
    builder.button(text="📖 Инструкция")
    builder.button(text="👤 Автор")
    builder.adjust(1, 2, 1)
    return builder.as_markup(resize_keyboard=True)

# --- Логика фоновой проверки ---
async def check_mails_job():
    accounts = get_all_global_accounts()
    hosts = {"gmail": "imap.gmail.com", "yandex": "imap.yandex.ru", "outlook": "outlook.office365.com", "mailru": "imap.mail.ru"}

    for user_id, em, pw, svc, last_uid in accounts:
        try:
            imap = aioimaplib.IMAP4_SSL(hosts.get(svc), port=993)
            await imap.wait_hello_from_server()
            login_res = await imap.login(em, pw)
            if "OK" not in login_res.result: continue

            await imap.select('INBOX')
            _, ids_data = await imap.search('ALL')
            mail_ids = ids_data[0].split()
            if not mail_ids: 
                await imap.logout()
                continue
                
            current_last_uid = mail_ids[-1].decode()

            if last_uid is None:
                update_last_uid(user_id, em, current_last_uid)
            elif current_last_uid != last_uid:
                _, msg_data = await imap.fetch(current_last_uid, '(RFC822.HEADER)')
                msg = email.message_from_bytes(msg_data[1])
                subj = decode_mime_words(msg.get("Subject"))
                
                await bot.send_message(user_id, f"🔔 **Новое письмо на {em}!**\n\n📌 Тема: {subj}")
                update_last_uid(user_id, em, current_last_uid)

            await imap.logout()
        except Exception as e:
            logging.error(f"Ошибка фона для {em}: {e}")

# --- Обработчики AIOGRAM ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    init_db()
    await message.answer("Почтовый менеджер запущен! Проверка почты — каждые 5 минут.", reply_markup=get_main_menu())

@dp.message(F.text == "👤 Автор")
async def show_author(message: types.Message):
    await message.answer("Бот создан для удобного управления почтой.\nАвтор: @nagornyidan")

@dp.message(F.text == "📖 Инструкция")
async def show_instruction(message: types.Message):
    await message.answer(f"Инструкция по настройке: {GITHUB_URL}")

@dp.message(F.text == "➕ Добавить почту")
async def start_add(message: types.Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    for s in ["Gmail", "Yandex", "Outlook", "Mailru"]:
        builder.add(types.InlineKeyboardButton(text=s, callback_data=f"svc_{s.lower()}"))
    builder.adjust(2)
    await message.answer("Выберите сервис:", reply_markup=builder.as_markup())
    await state.set_state(MailAuth.waiting_for_service)

@dp.callback_query(F.data.startswith("svc_"))
async def select_svc(callback: types.CallbackQuery, state: FSMContext):
    svc = callback.data.split("_")[1]
    await state.update_data(service=svc)
    await callback.message.edit_text(f"Выбран {svc.upper()}. Введите Email:")
    await state.set_state(MailAuth.waiting_for_email)

@dp.message(MailAuth.waiting_for_email)
async def process_email(message: types.Message, state: FSMContext):
    await state.update_data(email=message.text.strip())
    await message.answer("Введите **Пароль приложения**:")
    await state.set_state(MailAuth.waiting_for_password)

@dp.message(MailAuth.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    data = await state.get_data()
    add_account(message.from_user.id, data['email'], message.text.strip(), data['service'])
    await message.answer(f"✅ Ящик {data['email']} добавлен!", reply_markup=get_main_menu())
    await state.clear()

@dp.message(F.text == "📥 Мои ящики")
async def list_accounts(message: types.Message):
    accs = get_all_accounts(message.from_user.id)
    if not accs: return await message.answer("Список пуст.")
    builder = InlineKeyboardBuilder()
    for em, svc in accs:
        builder.row(types.InlineKeyboardButton(text=f"📧 {em} ({svc})", callback_data=f"open_{em}"))
    await message.answer("Ваши ящики:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("open_"))
async def open_mailbox(callback: types.CallbackQuery):
    email_addr = callback.data.replace("open_", "")
    user = get_account_details(callback.from_user.id, email_addr)
    em, pw, svc = user
    hosts = {"gmail": "imap.gmail.com", "yandex": "imap.yandex.ru", "outlook": "outlook.office365.com", "mailru": "imap.mail.ru"}

    await callback.message.answer(f"⌛️ Вхожу в {em}...")
    try:
        imap = aioimaplib.IMAP4_SSL(hosts.get(svc), port=993)
        await imap.wait_hello_from_server()
        if "OK" not in (await imap.login(em, pw)).result:
            return await callback.message.answer(f"❌ Ошибка входа в {svc.upper()}")

        await imap.select('INBOX')
        _, ids_data = await imap.search('ALL')
        mail_ids = ids_data[0].split()[-5:]

        for m_id in reversed(mail_ids):
            _, msg_data = await imap.fetch(m_id.decode(), '(RFC822.HEADER)')
            msg = email.message_from_bytes(msg_data[1])
            subj = decode_mime_words(msg.get("Subject"))
            kb = InlineKeyboardBuilder()
            kb.button(text="📖 Читать", callback_data=f"read_{m_id.decode()}|{em}")
            await callback.message.answer(f"📩 {subj}", reply_markup=kb.as_markup())
        await imap.logout()
    except Exception as e:
        await callback.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data.startswith("read_"))
async def read_mail(callback: types.CallbackQuery):
    m_id, email_addr = callback.data.replace("read_", "").split("|")
    user = get_account_details(callback.from_user.id, email_addr)
    em, pw, svc = user
    hosts = {"gmail": "imap.gmail.com", "yandex": "imap.yandex.ru", "outlook": "outlook.office365.com", "mailru": "imap.mail.ru"}

    try:
        imap = aioimaplib.IMAP4_SSL(hosts.get(svc), port=993)
        await imap.wait_hello_from_server()
        await imap.login(em, pw)
        await imap.select('INBOX')
        _, msg_data = await imap.fetch(m_id, '(RFC822)')
        msg = email.message_from_bytes(msg_data[1])
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(errors='ignore')
                    break
        else: body = msg.get_payload(decode=True).decode(errors='ignore')
        await callback.message.answer(f"📄 {em}:\n\n{body[:3500]}")
        await imap.logout()
    except Exception as e: await callback.message.answer(f"Ошибка: {e}")

# --- Запуск ---
async def main():
    init_db()
    scheduler.add_job(check_mails_job, "interval", minutes=5)
    scheduler.start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())