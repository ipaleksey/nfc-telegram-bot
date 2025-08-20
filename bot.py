"""
NFC → Telegram VIP бот для доступа в закрытый канал/чат
======================================================

▶ Что делает бот
- При сканировании NFC-метки пользователь попадает в бота по ссылке вида
  https://t.me/<YOUR_BOT>?start=<UNIQUE_CODE>
- Бот проверяет <UNIQUE_CODE> в базе и выдаёт одноразовую пригласительную ссылку
  в закрытый канал/чат (либо отклоняет, если код невалиден/уже привязан к другому).
- Для уже привязанных владельцев доступ можно получить командой /access.
- Админ может массово генерировать коды, смотреть логи и управлять ключами.

▶ Требования
- Python 3.10+
- Библиотеки: aiogram>=3.4, aiosqlite, python-dotenv
- Бот должен быть админом в целевом канале/чате.

▶ Переменные окружения (.env)
BOT_TOKEN=123456:ABC...
TARGET_CHAT_ID=-1001234567890   # ID закрытого канала/супергруппы
ADMINS=123456789,987654321      # через запятую, Telegram user_id админов
INVITE_TTL_MINUTES=10           # срок жизни ссылки-приглашения

▶ Подготовка NFC
На NFC-наклейку запишите URL:  https://t.me/<YOUR_BOT>?start=<UNIQUE_CODE>
<UNIQUE_CODE> должен совпадать с кодом в таблице nfc_keys.code

"""
import asyncio
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.markdown import hbold, hcode
from dotenv import load_dotenv

DB_PATH = os.getenv("DB_PATH", "/data/nfc_access.db")

# ---------- Утилиты ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

async def ensure_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_seen  TEXT
            );

            CREATE TABLE IF NOT EXISTS nfc_keys (
                code                TEXT PRIMARY KEY,
                product_id          TEXT,               -- опционально: артикул/серийник изделия
                assigned_user_id    INTEGER,            -- владелец (telegram id)
                status              TEXT NOT NULL,      -- new | claimed | revoked
                created_at          TEXT NOT NULL,
                claimed_at          TEXT
            );

            CREATE TABLE IF NOT EXISTS access_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                code        TEXT,
                action      TEXT,       -- attempt | granted | rejected | invite_created
                reason      TEXT,
                created_at  TEXT
            );
            """
        )
        await db.commit()

async def add_log(user_id: Optional[int], code: Optional[str], action: str, reason: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO access_logs(user_id, code, action, reason, created_at) VALUES (?,?,?,?,?)",
            (user_id, code, action, reason, now_utc().isoformat()),
        )
        await db.commit()

async def upsert_user(user_id: int, username: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row:
            await db.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        else:
            await db.execute(
                "INSERT INTO users(user_id, username, first_seen) VALUES (?,?,?)",
                (user_id, username, now_utc().isoformat()),
            )
        await db.commit()

async def get_key(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT code, product_id, assigned_user_id, status, created_at, claimed_at FROM nfc_keys WHERE code=?", (code,))
        return await cur.fetchone()

async def claim_key_for_user(code: str, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем текущий статус
        cur = await db.execute("SELECT status, assigned_user_id FROM nfc_keys WHERE code=?", (code,))
        row = await cur.fetchone()
        if not row:
            return False
        status, assigned_user_id = row
        if status == "revoked":
            return False
        if assigned_user_id is None:
            await db.execute(
                "UPDATE nfc_keys SET assigned_user_id=?, status='claimed', claimed_at=? WHERE code=?",
                (user_id, now_utc().isoformat(), code),
            )
            await db.commit()
            return True
        return assigned_user_id == user_id

async def create_keys_batch(amount: int, product_id: Optional[str] = None) -> List[str]:
    codes: List[str] = []
    alphabet = string.ascii_uppercase + string.digits
    async with aiosqlite.connect(DB_PATH) as db:
        for _ in range(amount):
            while True:
                code = "".join(secrets.choice(alphabet) for _ in range(12))
                cur = await db.execute("SELECT 1 FROM nfc_keys WHERE code=?", (code,))
                if not await cur.fetchone():
                    break
            await db.execute(
                "INSERT INTO nfc_keys(code, product_id, assigned_user_id, status, created_at, claimed_at) VALUES (?,?,?,?,?,?)",
                (code, product_id, None, "new", now_utc().isoformat(), None),
            )
            codes.append(code)
        await db.commit()
    return codes

async def revoke_key(code: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT status FROM nfc_keys WHERE code=?", (code,))
        if not await cur.fetchone():
            return False
        await db.execute("UPDATE nfc_keys SET status='revoked' WHERE code=?", (code,))
        await db.commit()
        return True

# ---------- Конфиг ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
ADMINS = {int(x.strip()) for x in os.getenv("ADMINS", "").split(",") if x.strip()}
INVITE_TTL_MIN = int(os.getenv("INVITE_TTL_MINUTES", "10"))

if not BOT_TOKEN or TARGET_CHAT_ID == 0:
    raise RuntimeError("Set BOT_TOKEN and TARGET_CHAT_ID in .env")

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ---------- Хэндлеры пользователя ----------

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    await upsert_user(message.from_user.id, message.from_user.username)
    code = (command.args or "").strip()
    if not code:
        text = (
            "Привет! Это закрытый клуб.\n\n"
            "Если у вас есть NFC-метка, приложите её — откроется ссылка с параметром.\n"
            "Либо пришлите команду /access для запроса доступа, если вы уже владелец."
        )
        return await message.answer(text)

    await add_log(message.from_user.id, code, "attempt", "start_param")
    row = await get_key(code)
    if not row:
        await add_log(message.from_user.id, code, "rejected", "code_not_found")
        return await message.answer("<b>Код не найден.</b> Проверьте URL с NFC-метки или обратитесь в поддержку.")

    # row: code, product_id, assigned_user_id, status, created_at, claimed_at
    _, product_id, assigned_user_id, status, _, _ = row

    if status == "revoked":
        await add_log(message.from_user.id, code, "rejected", "code_revoked")
        return await message.answer("Этот ключ <b>отозван</b>. Свяжитесь с поддержкой.")

    ok = await claim_key_for_user(code, message.from_user.id)
    if not ok:
        await add_log(message.from_user.id, code, "rejected", "owned_by_another")
        return await message.answer(
            "Код уже привязан к другому владельцу. Если вы считаете это ошибкой — напишите поддержке."
        )

    # Создаём одноразовую ссылку-приглашение
    expire_date = now_utc() + timedelta(minutes=INVITE_TTL_MIN)
    invite = await bot.create_chat_invite_link(
        chat_id=TARGET_CHAT_ID,
        name=f"NFC {code} → @{message.from_user.username or message.from_user.id}",
        expire_date=int(expire_date.timestamp()),
        member_limit=1,
        creates_join_request=False,
    )
    await add_log(message.from_user.id, code, "invite_created", "start_flow")

    product_line = f"\nИзделие: <code>{product_id}</code>" if product_id else ""
    await message.answer(
        (
            f"Ключ подтверждён ✅{product_line}\n\n"
            f"Ваша персональная ссылка действует <b>{INVITE_TTL_MIN}</b> мин и на <b>1</b> вход:\n"
            f"{invite.invite_link}\n\n"
            f"Если не успели — используйте /access для выпуска новой."
        )
    )

@dp.message(Command("access"))
async def cmd_access(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username)
    # Ищем любой активный ключ, принадлежащий пользователю
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT code FROM nfc_keys WHERE assigned_user_id=? AND status='claimed' ORDER BY claimed_at DESC LIMIT 1",
            (message.from_user.id,),
        )
        row = await cur.fetchone()
    if not row:
        return await message.answer("Ключ не найден. Сканируйте NFC-метку (или используйте ссылку с параметром).")

    code = row[0]
    expire_date = now_utc() + timedelta(minutes=INVITE_TTL_MIN)
    invite = await bot.create_chat_invite_link(
        chat_id=TARGET_CHAT_ID,
        name=f"/access {code} → @{message.from_user.username or message.from_user.id}",
        expire_date=int(expire_date.timestamp()),
        member_limit=1,
        creates_join_request=False,
    )
    await add_log(message.from_user.id, code, "invite_created", "access_cmd")

    await message.answer(
        (
            f"Ваша новая ссылка на вход (действует {INVITE_TTL_MIN} мин, 1 использование):\n"
            f"{invite.invite_link}"
        )
    )

# ---------- Админ-команды ----------

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

@dp.message(Command("gen"))
async def cmd_gen(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("Команда доступна только администратору.")
    try:
        args = (command.args or "").split()
        amount = int(args[0]) if args else 10
        product_id = args[1] if len(args) > 1 else None
    except Exception:
        return await message.answer("Формат: /gen <кол-во> [product_id]")

    codes = await create_keys_batch(amount, product_id)
    base = f"https://t.me/{(await bot.me()).username}?start="
    lines = "\n".join(f"{c}\t{base}{c}" for c in codes)
    await message.answer(
        "Созданы коды (code\turl):\n" + hcode(lines)[:3900]
    )

@dp.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("Команда доступна только администратору.")
    code = (command.args or "").strip()
    if not code:
        return await message.answer("Формат: /revoke <code>")
    ok = await revoke_key(code)
    await message.answer("Ок" if ok else "Код не найден")

@dp.message(Command("who"))
async def cmd_who(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("Команда доступна только администратору.")
    code = (command.args or "").strip()
    if not code:
        return await message.answer("Формат: /who <code>")
    row = await get_key(code)
    if not row:
        return await message.answer("Код не найден")
    code, product_id, assigned_user_id, status, created_at, claimed_at = row
    await message.answer(
        (
            f"<b>Код:</b> {hcode(code)}\n"
            f"<b>Статус:</b> {status}\n"
            f"<b>Владелец:</b> {assigned_user_id}\n"
            f"<b>Изделие:</b> {hcode(product_id or '-') }\n"
            f"<b>Создан:</b> {created_at}\n"
            f"<b>Назначен:</b> {claimed_at or '-'}"
        )
    )

@dp.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return await message.answer("Команда доступна только администратору.")
    try:
        limit = int((command.args or "").strip() or 20)
    except Exception:
        limit = 20
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT created_at, user_id, code, action, reason FROM access_logs ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cur.fetchall()
    if not rows:
        return await message.answer("Логи пусты.")
    lines = [
        f"{r[0]} | uid={r[1]} | code={r[2]} | {r[3]} | {r[4]}" for r in rows
    ]
    await message.answer(hcode("\n".join(lines))[:3900])

# ---------- Старт ----------
async def main():
    await ensure_db()
    me = await bot.get_me()
    print(f"Bot @{me.username} started. Target chat: {TARGET_CHAT_ID}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
