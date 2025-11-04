import asyncio
import time
import logging
import os
from typing import Optional
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime

# === Настройка ===
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
DB_PATH = "orders.db"
if not BOT_TOKEN or not ADMIN_USER_ID:
    raise ValueError("Укажите BOT_TOKEN и ADMIN_USER_ID в файле .env")

# === Инициализация ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
os.system("pip install aiosqlite")

# === FSM ===
class CreateOrder(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_tags = State()
    waiting_for_price = State()
    waiting_for_payment_method = State()
    waiting_for_payment_confirmation = State()

# === База данных ===
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                tags TEXT NOT NULL,
                client_price REAL NOT NULL,
                admin_proposed_price REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_comment TEXT DEFAULT '',
                created_at REAL NOT NULL,
                completed_at REAL,
                auto_delete_at REAL,
                is_paid BOOLEAN DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_last_order (
                user_id INTEGER PRIMARY KEY,
                last_order_time REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS global_discount (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                discount_value REAL,
                discount_type TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                discount_value REAL NOT NULL,
                discount_type TEXT NOT NULL,
                max_uses INTEGER NOT NULL DEFAULT 1,
                current_uses INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_promo_attempts (
                user_id INTEGER PRIMARY KEY,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until REAL
            )
        """)
        await db.commit()

# --- Функции для скидок и промокодов ---
async def get_global_discount():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT discount_value, discount_type FROM global_discount WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return {"value": row[0], "type": row[1]} if row else None

async def set_global_discount(value: float, disc_type: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO global_discount (id, discount_value, discount_type) VALUES (1, ?, ?)",
            (value, disc_type)
        )
        await db.commit()

async def remove_global_discount():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM global_discount WHERE id = 1")
        await db.commit()

async def create_promo_code(code: str, value: float, disc_type: str, max_uses: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promo_codes (code, discount_value, discount_type, max_uses) VALUES (?, ?, ?, ?)",
            (code.upper(), value, disc_type, max_uses)
        )
        await db.commit()

async def get_promo_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promo_codes WHERE code = ?", (code.upper(),)) as cursor:
            return await cursor.fetchone()

async def use_promo_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = ? AND current_uses < max_uses",
            (code.upper(),)
        )
        await db.commit()

async def delete_promo_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promo_codes WHERE code = ?", (code.upper(),))
        await db.commit()

async def is_user_blocked_from_promo(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT locked_until FROM user_promo_attempts WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] > time.time():
                return True
            return False

async def record_failed_promo_attempt(user_id: int):
    now = time.time()
    unlock_time = now + 15 * 60
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_promo_attempts (user_id, failed_attempts, locked_until)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                failed_attempts = failed_attempts + 1,
                locked_until = CASE
                    WHEN failed_attempts + 1 >= 3 THEN ?
                    ELSE locked_until
                END
            """,
            (user_id, unlock_time, unlock_time)
        )
        await db.commit()

async def reset_promo_attempts(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_promo_attempts WHERE user_id = ?", (user_id,))
        await db.commit()

# --- Стандартные функции ---
async def get_last_order_time(user_id: int) -> Optional[float]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_order_time FROM user_last_order WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_last_order_time(user_id: int, timestamp: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_last_order (user_id, last_order_time) VALUES (?, ?)",
            (user_id, timestamp)
        )
        await db.commit()

async def create_order(user_id: int, title: str, description: str, tags: str, price: float) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO orders (
                user_id, title, description, tags, client_price, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, title, description, tags, price, time.time(), "awaiting_payment")
        )
        await db.commit()
        return cursor.lastrowid

async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cursor:
            return await cursor.fetchone()

async def get_user_orders(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ) as cursor:
            return await cursor.fetchall()

async def update_order(order_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [order_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE orders SET {fields} WHERE id = ?", values)
        await db.commit()

async def delete_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        await db.commit()

async def get_user_total_spent(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT SUM(
                CASE 
                    WHEN admin_proposed_price IS NOT NULL THEN admin_proposed_price
                    ELSE client_price
                END
            ) 
            FROM orders 
            WHERE user_id = ? AND status = 'completed' AND is_paid = 1
            """,
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row[0] else 0.0

async def get_admin_total_earned() -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT SUM(
                CASE 
                    WHEN admin_proposed_price IS NOT NULL THEN admin_proposed_price
                    ELSE client_price
                END
            ) 
            FROM orders 
            WHERE status = 'completed' AND is_paid = 1
            """
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row[0] else 0.0

# === Вспомогательные функции ===
def parse_tags(tags_str: str):
    return [t.strip() for t in tags_str.split(",") if t.strip()]

def format_order_message(order, for_admin=False) -> str:
    tags = parse_tags(order["tags"])
    tags_str = ", ".join(tags) if tags else "—"
    price = f"{order['admin_proposed_price']:.2f} ₽" if order['admin_proposed_price'] is not None else f"{order['client_price']:.2f} ₽"
    status_map = {
        "pending": "⏳ Ожидает",
        "price_proposed": "💬 Предложена новая цена",
        "in_progress": "🛠 В работе",
        "completed": "✅ Завершён",
        "cancelled": "❌ Отменён",
        "awaiting_payment": "💳 Ожидает оплату"
    }
    status_text = status_map.get(order["status"], order["status"])
    text = (
        f"Заказ #{order['id']}\n"
        f"Статус: {status_text}\n"
        f"Название: {order['title']}\n"
        f"Описание: {order['description']}\n"
        f"Теги: {tags_str}\n"
        f"Цена: {price}\n"
    )
    if for_admin:
        text += f"Клиент: {order['user_id']}\n"
    if order["admin_comment"]:
        text += f"Комментарий: {order['admin_comment']}\n"
    return text

async def safe_send_message(chat_id: int, text: str, **kwargs):
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError as e:
        logger.warning(f"Не удалось отправить сообщение {chat_id}: {e}")

_background_tasks = set()
async def schedule_order_deletion(order_id: int, delay: int):
    await asyncio.sleep(delay)
    await delete_order(order_id)
    logger.info(f"Заказ #{order_id} удалён (автоочистка)")

def create_deletion_task(order_id: int, delay: int):
    task = asyncio.create_task(schedule_order_deletion(order_id, delay))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

# === Команды ===
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id == ADMIN_USER_ID:
        await message.answer(
            "👋 Админ-панель:\n"
            "/adminstats — заработок\n"
            "/broadcast — рассылка\n"
            "/discount — скидка\n"
            "/discount_off — убрать скидку\n"
            "/promo_create — создать промокод\n"
            "/promo_list — список\n"
            "/promo_delete КОД — удалить"
        )
    else:
        await message.answer(
            "👋 Привет!\n"
            "/neworder — создать заказ\n"
            "/myorders — мои заказы\n"
            "/mystats — мои траты\n"
            "/cancel_order — отменить неоплаченный заказ\n"
            "/promo КОД — применить промокод"
        )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Создание заказа отменено.")

# === Новый заказ ===
@router.message(Command("neworder"))
async def new_order_start(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_USER_ID:
        await message.answer("Админ не создаёт заказы.")
        return
    last_time = await get_last_order_time(message.from_user.id)
    now = time.time()
    if last_time and (now - last_time) < 300:
        remaining = int(300 - (now - last_time))
        await message.answer(f"⏳ Подождите {remaining} секунд.")
        return
    await state.set_state(CreateOrder.waiting_for_title)
    await message.answer("Название заказа:")

@router.message(CreateOrder.waiting_for_title)
async def process_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(CreateOrder.waiting_for_description)
    await message.answer("Описание:")

@router.message(CreateOrder.waiting_for_description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(CreateOrder.waiting_for_tags)
    await message.answer("Теги через запятую:")

@router.message(CreateOrder.waiting_for_tags)
async def process_tags(message: Message, state: FSMContext):
    await state.update_data(tags=message.text)
    await state.set_state(CreateOrder.waiting_for_price)
    await message.answer("Цена в ₽:")

@router.message(CreateOrder.waiting_for_price)
async def process_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        if price < 0:
            raise ValueError
        if price > 1_000_000:
            await message.answer("Максимальная сумма заказа — 1 000 000 ₽.")
            return
    except ValueError:
        await message.answer("Введите число от 0 до 1 000 000.")
        return

    await state.update_data(price=price)
    await state.set_state(CreateOrder.waiting_for_payment_method)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("💵 Наличные", callback_data="pay_cash")],
        [InlineKeyboardButton("💳 Карта (СБП ВТБ)", callback_data="pay_card")]
    ])
    await message.answer("Выберите способ оплаты:", reply_markup=kb)

@router.callback_query(F.data.in_({"pay_cash", "pay_card"}))
async def process_payment_method(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id == ADMIN_USER_ID:
        await callback.answer("Админ не создаёт заказы.")
        return

    method = "наличные" if callback.data == "pay_cash" else "карта"
    await state.update_data(payment_method=method)

    if callback.data == "pay_card":
        await callback.message.answer(
            "💳 Оплатите по СБП на банк ВТБ:\n+7 953 850-72-79\n\n"
            "После оплаты нажмите ✅ Оплатил."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Оплатил", callback_data="payment_yes")],
        [InlineKeyboardButton("❌ Ещё не оплатил", callback_data="payment_no")]
    ])
    await callback.message.answer("Вы уже оплатили?", reply_markup=kb)
    await state.set_state(CreateOrder.waiting_for_payment_confirmation)
    await callback.answer()

@router.callback_query(F.data.in_({"payment_yes", "payment_no"}))
async def process_payment_confirmation(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id == ADMIN_USER_ID:
        await callback.answer("Админ не создаёт заказы.")
        return

    if callback.data == "payment_no":
        await callback.message.answer("Хорошо, создайте заказ заново, когда будете готовы оплатить: /neworder")
        await state.clear()
        await callback.answer()
        return

    data = await state.get_data()
    order_id = await create_order(
        user_id=callback.from_user.id,
        title=data["title"],
        description=data["description"],
        tags=data["tags"],
        price=data["price"]
    )
    await update_order(order_id, status="pending")
    await set_last_order_time(callback.from_user.id, time.time())
    await state.clear()

    order = await get_order(order_id)
    payment_info = f"Способ оплаты: {data['payment_method']}"
    if data['payment_method'] == "карта":
        payment_info += "\n❗ Админ: проверьте поступление по СБП."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("📥 Взять", callback_data=f"take_{order_id}")],
        [InlineKeyboardButton("🗑 Игнорировать", callback_data=f"ignore_{order_id}")]
    ])

    await safe_send_message(
        ADMIN_USER_ID,
        f"🆕 Новый заказ!\n{format_order_message(order, for_admin=True)}\n{payment_info}",
        reply_markup=kb
    )

    await callback.message.answer(f"✅ Заказ #{order_id} создан и отправлен админу!\nОжидайте подтверждения.")
    await callback.answer()

@router.message(Command("cancel_order"))
async def cancel_order(message: Message):
    if message.from_user.id == ADMIN_USER_ID:
        await message.answer("Админ не отменяет заказы.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id FROM orders 
            WHERE user_id = ? AND status = 'awaiting_payment'
            ORDER BY created_at DESC LIMIT 1
            """,
            (message.from_user.id,)
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer("Нет активных заказов в статусе «ожидает оплаты».")
        return

    order_id = row["id"]
    await delete_order(order_id)
    await message.answer(f"Заказ #{order_id} отменён.")

@router.message(Command("myorders"))
async def my_orders(message: Message):
    if message.from_user.id == ADMIN_USER_ID:
        await message.answer("Админ: смотрите уведомления.")
        return
    orders_list = await get_user_orders(message.from_user.id)
    if not orders_list:
        await message.answer("У вас нет заказов.")
        return
    text = "Ваши заказы:\n"
    for o in orders_list:
        status_icon = {
            "pending": "⏳", "in_progress": "🛠", "completed": "✅",
            "cancelled": "❌", "awaiting_payment": "💳"
        }.get(o["status"], "❓")
        text += f"{status_icon} #{o['id']} — {o['title']}\n"
    await message.answer(text)

@router.message(Command("mystats"))
async def my_stats(message: Message):
    if message.from_user.id == ADMIN_USER_ID:
        await message.answer("Админ: используйте /adminstats")
        return

    user_id = message.from_user.id
    total = await get_user_total_spent(user_id)
    orders = await get_user_orders(user_id)
    total_orders = len(orders)
    avg = total / total_orders if total_orders > 0 else 0.0
    last_order_date = "—"
    if orders:
        last_ts = orders[0]["created_at"]
        last_order_date = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M")

    await message.answer(
        f"💰 Всего потрачено: {total:.2f} ₽\n"
        f"📦 Всего заказов: {total_orders}\n"
        f"🧮 Средний чек: {avg:.2f} ₽\n"
        f"📅 Последний заказ: {last_order_date}"
    )

@router.message(Command("adminstats"))
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    total = await get_admin_total_earned()
    await message.answer(f"💼 Ваш заработок: {total:.2f} ₽")

# === Рассылка ===
class AdminFSM(StatesGroup):
    broadcast_text = State()
    discount_type = State()
    discount_value = State()
    promo_code = State()
    promo_type = State()
    promo_value = State()
    promo_uses = State()

@router.message(Command("broadcast"))
async def broadcast_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    await state.set_state(AdminFSM.broadcast_text)
    await message.answer("Введите текст рассылки:")

@router.message(AdminFSM.broadcast_text)
async def handle_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM orders") as cursor:
            users = [row[0] for row in await cursor.fetchall()]
    success = 0
    for uid in users:
        if uid == ADMIN_USER_ID:
            continue
        try:
            await bot.send_message(uid, f"📢 Рассылка:\n\n{message.text}")
            success += 1
        except:
            pass
    await message.answer(f"✅ Рассылка отправлена {success} пользователям.")
    await state.clear()

# === Глобальная скидка ===
@router.message(Command("discount"))
async def discount_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    await state.set_state(AdminFSM.discount_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Проценты (%)", callback_data="disc_type_percent")],
        [InlineKeyboardButton("Рубли (₽)", callback_data="disc_type_fixed")]
    ])
    await message.answer("Тип скидки:", reply_markup=kb)

@router.callback_query(F.data.in_({"disc_type_percent", "disc_type_fixed"}))
async def discount_type_chosen(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        return
    disc_type = "percent" if callback.data == "disc_type_percent" else "fixed"
    await state.update_data(disc_type=disc_type)
    await state.set_state(AdminFSM.discount_value)
    text = "Размер скидки в %:" if disc_type == "percent" else "Сумма в ₽:"
    await callback.message.answer(text)
    await callback.answer()

@router.message(AdminFSM.discount_value)
async def discount_value_entered(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    try:
        value = float(message.text)
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Положительное число.")
        return
    data = await state.get_data()
    await set_global_discount(value, data["disc_type"])
    disc_str = f"{value}%" if data["disc_type"] == "percent" else f"{value} ₽"
    await message.answer(f"✅ Глобальная скидка: {disc_str}.")
    await state.clear()

@router.message(Command("discount_off"))
async def discount_off(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    await remove_global_discount()
    await message.answer("✅ Глобальная скидка отключена.")

# === Промокоды ===
@router.message(Command("promo_create"))
async def promo_create_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    await state.set_state(AdminFSM.promo_code)
    await message.answer("Название промокода:")

@router.message(AdminFSM.promo_code)
async def promo_code_entered(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    code = message.text.strip().upper()
    if not code.replace("_", "").replace("-", "").isalnum():
        await message.answer("Только буквы, цифры, _ или -")
        return
    await state.update_data(code=code)
    await state.set_state(AdminFSM.promo_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Проценты (%)", callback_data="promo_type_percent")],
        [InlineKeyboardButton("Рубли (₽)", callback_data="promo_type_fixed")]
    ])
    await message.answer("Тип скидки:", reply_markup=kb)

@router.callback_query(F.data.in_({"promo_type_percent", "promo_type_fixed"}))
async def promo_type_selected(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        return
    ptype = "percent" if callback.data == "promo_type_percent" else "fixed"
    await state.update_data(promo_type=ptype)
    await state.set_state(AdminFSM.promo_value)
    text = "Размер скидки в %:" if ptype == "percent" else "Сумма в ₽:"
    await callback.message.answer(text)
    await callback.answer()

@router.message(AdminFSM.promo_value)
async def promo_value_entered(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    try:
        value = float(message.text)
        if value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Положительное число.")
        return
    await state.update_data(promo_value=value)
    await state.set_state(AdminFSM.promo_uses)
    await message.answer("Макс. число применений:")

@router.message(AdminFSM.promo_uses)
async def promo_uses_entered(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    try:
        uses = int(message.text)
        if uses <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Целое число > 0.")
        return
    data = await state.get_data()
    await create_promo_code(data["code"], data["promo_value"], data["promo_type"], uses)
    disc = f"{data['promo_value']}%" if data["promo_type"] == "percent" else f"{data['promo_value']} ₽"
    await message.answer(f"✅ Промокод `{data['code']}` создан!\nСкидка: {disc}\nЛимит: {uses}")
    await state.clear()

@router.message(Command("promo_list"))
async def promo_list(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promo_codes") as cursor:
            promos = await cursor.fetchall()
    if not promos:
        await message.answer("Нет промокодов.")
        return
    text = "Промокоды:\n"
    for p in promos:
        disc = f"{p['discount_value']}%" if p['discount_type'] == 'percent' else f"{p['discount_value']} ₽"
        text += f"`{p['code']}` — {disc}, {p['current_uses']}/{p['max_uses']}\n"
    await message.answer(text, parse_mode="Markdown")

@router.message(Command("promo_delete"))
async def promo_delete(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /promo_delete КОД")
        return
    code = parts[1].strip().upper()
    await delete_promo_code(code)
    await message.answer(f"Промокод `{code}` удалён.", parse_mode="Markdown")

@router.message(Command("promo"))
async def user_apply_promo(message: Message):
    if message.from_user.id == ADMIN_USER_ID:
        await message.answer("Админ не использует промокоды.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /promo КОД")
        return
    code = parts[1].strip().upper()
    if await is_user_blocked_from_promo(message.from_user.id):
        await message.answer("Вы заблокированы на 15 минут из-за частых ошибок.")
        return
    promo = await get_promo_code(code)
    if not promo:
        await record_failed_promo_attempt(message.from_user.id)
        await message.answer("❌ Неверный промокод.")
        return
    if promo["current_uses"] >= promo["max_uses"]:
        await message.answer("❌ Промокод исчерпан.")
        return
    await use_promo_code(code)
    await reset_promo_attempts(message.from_user.id)
    disc = f"{promo['discount_value']}%" if promo['discount_type'] == 'percent' else f"{promo['discount_value']} ₽"
    await message.answer(f"✅ Промокод `{code}` применён! Скидка: {disc}.", parse_mode="Markdown")

# === Админ-обработчики заказов (без изменений) ===
@router.callback_query(F.data.startswith("take_"))
async def admin_take(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[1])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.")
        return
    await update_order(order_id, status="in_progress")
    await safe_send_message(order["user_id"], f"✅ Заказ #{order_id} взят в работу!")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("💬 Цена", callback_data=f"change_price_{order_id}")],
        [InlineKeyboardButton("✏️ Коммент", callback_data=f"comment_{order_id}")],
        [InlineKeyboardButton("✅ Завершить", callback_data=f"complete_{order_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"admin_del_{order_id}")]
    ])
    await callback.message.edit_text(
        f"{format_order_message(order, for_admin=True)}\n🛠 Взят в работу.",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("ignore_"))
async def admin_ignore(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[1])
    await delete_order(order_id)
    await callback.message.edit_text("🗑 Игнорировано.")
    await callback.answer()

@router.callback_query(F.data.startswith("change_price_"))
async def start_change_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[2])
    await state.set_state("change_price")
    await state.update_data(order_id=order_id)
    await callback.message.answer("Новая цена в ₽:")
    await callback.answer()

@router.message(F.text, lambda msg: msg.from_user.id == ADMIN_USER_ID)
async def handle_admin_input(message: Message, state: FSMContext):
    current = await state.get_state()
    if current == "change_price":
        try:
            price = float(message.text)
            if price < 0: raise ValueError
        except ValueError:
            await message.answer("Число ≥ 0.")
            return
        data = await state.get_data()
        order_id = data["order_id"]
        await update_order(order_id, admin_proposed_price=price, status="price_proposed")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("✅ Принять", callback_data=f"accept_price_{order_id}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_price_{order_id}")]
        ])
        await safe_send_message(
            (await get_order(order_id))["user_id"],
            f"Предложена новая цена: {price:.2f} ₽. Принять?",
            reply_markup=kb
        )
        await message.answer("Предложение отправлено.")
        await state.clear()
    elif current == "admin_comment":
        data = await state.get_data()
        order_id = data["order_id"]
        await update_order(order_id, admin_comment=message.text)
        await safe_send_message((await get_order(order_id))["user_id"], f"💬 {message.text}")
        await message.answer("Комментарий отправлен.")
        await state.clear()

@router.callback_query(F.data.startswith("accept_price_"))
async def client_accept_price(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("Не ваш заказ.")
        return
    await update_order(order_id, admin_proposed_price=None, status="in_progress")
    await callback.message.edit_text("✅ Цена принята. Заказ в работе.")
    await safe_send_message(ADMIN_USER_ID, f"Клиент принял цену по заказу #{order_id}.")
    await callback.answer()

@router.callback_query(F.data.startswith("reject_price_"))
async def client_reject_price(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("Не ваш заказ.")
        return
    await update_order(order_id, status="cancelled")
    await callback.message.edit_text("❌ Заказ отменён.")
    await safe_send_message(ADMIN_USER_ID, f"Клиент отклонил цену по заказу #{order_id}.")
    await callback.answer()

@router.callback_query(F.data.startswith("comment_"))
async def start_comment(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[1])
    await state.set_state("admin_comment")
    await state.update_data(order_id=order_id)
    await callback.message.answer("Комментарий клиенту:")
    await callback.answer()

@router.callback_query(F.data.startswith("complete_"))
async def complete_order(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[1])
    await update_order(order_id, status="completed", completed_at=time.time())
    await safe_send_message((await get_order(order_id))["user_id"], f"✅ Заказ #{order_id} завершён!")
    order = await get_order(order_id)
    final_price = order["admin_proposed_price"] or order["client_price"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(f"✅ Да, +{final_price:.2f} ₽", callback_data=f"confirm_earn_{order_id}")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"skip_earn_{order_id}")]
    ])
    await callback.message.edit_text(
        f"Заказ #{order_id} завершён.\nЗаписать {final_price:.2f} ₽ в заработок?",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_earn_"))
async def confirm_earning(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[2])
    await update_order(order_id, is_paid=1)
    order = await get_order(order_id)
    final_price = order["admin_proposed_price"] or order["client_price"]
    await callback.message.edit_text(f"✅ {final_price:.2f} ₽ записано в заработок.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🕒 3 ч", callback_data=f"keep_3_{order_id}")],
        [InlineKeyboardButton("🕕 6 ч", callback_data=f"keep_6_{order_id}")],
        [InlineKeyboardButton("🕛 12 ч", callback_data=f"keep_12_{order_id}")],
        [InlineKeyboardButton("📆 24 ч", callback_data=f"keep_24_{order_id}")],
        [InlineKeyboardButton("🗑 Сейчас", callback_data=f"del_now_{order_id}")]
    ])
    await callback.message.answer("Сохранить заказ на:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("skip_earn_"))
async def skip_earning(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[2])
    await callback.message.edit_text("❌ Сумма НЕ записана в заработок.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🕒 3 ч", callback_data=f"keep_3_{order_id}")],
        [InlineKeyboardButton("🕕 6 ч", callback_data=f"keep_6_{order_id}")],
        [InlineKeyboardButton("🕛 12 ч", callback_data=f"keep_12_{order_id}")],
        [InlineKeyboardButton("📆 24 ч", callback_data=f"keep_24_{order_id}")],
        [InlineKeyboardButton("🗑 Сейчас", callback_data=f"del_now_{order_id}")]
    ])
    await callback.message.answer("Сохранить заказ на:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("keep_"))
async def keep_order(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    hours = int(callback.data.split("_")[1])
    order_id = int(callback.data.split("_")[2])
    delay = hours * 3600
    await update_order(order_id, auto_delete_at=time.time() + delay)
    create_deletion_task(order_id, delay)
    await callback.message.edit_text(f"Будет удалён через {hours} ч.")
    await callback.answer()

@router.callback_query(F.data.startswith("del_now_"))
async def del_now(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[2])
    await delete_order(order_id)
    await callback.message.edit_text("Удалён.")
    await callback.answer()

@router.callback_query(F.data.startswith("admin_del_"))
async def admin_del(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    order_id = int(callback.data.split("_")[2])
    await delete_order(order_id)
    await callback.message.edit_text("Удалён вручную.")
    await callback.answer()

# === Запуск ===
async def main():
    await init_db()
    logger.info("Бот запущен с системой оплаты, скидок и промокодов.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())