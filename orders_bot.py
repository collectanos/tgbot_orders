import asyncio
import time
import logging
import os
from typing import Optional, List
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime

# === Настройка ===
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
admin_id = os.getenv("ADMIN_USER_ID")
if not admin_id:
    raise ValueError("Укажите ADMIN_USER_ID в .env")
ADMIN_USER_ID = int(admin_id)
DB_PATH = "orders.db"
START_TIME = time.time()
ORDERS_PER_PAGE = 10

if not BOT_TOKEN or not ADMIN_USER_ID:
    raise ValueError("Укажите BOT_TOKEN и ADMIN_USER_ID в файле .env")

# === Инициализация ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# === FSM ===
class CreateOrder(StatesGroup):
    waiting_for_agreement = State()
    waiting_for_agreement_refusal_confirm = State()
    waiting_for_title = State()
    waiting_for_tz_choice = State()
    waiting_for_tz = State()
    waiting_for_description = State()
    waiting_for_tags = State()
    waiting_for_price = State()
    waiting_for_promo_choice = State()
    waiting_for_promo_code = State()
    waiting_for_payment_method = State()
    waiting_for_equivalent_item = State()

class AdminFSM(StatesGroup):
    broadcast_text = State()
    discount_type = State()
    discount_value = State()
    promo_code = State()
    promo_type = State()
    promo_value = State()
    promo_uses = State()
    change_price = State()
    admin_comment = State()
    edit_order_title = State()
    waiting_for_promo_delete_code = State()
    waiting_for_promo_delete_confirm = State()

# === База данных ===
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            tz TEXT DEFAULT '',
            description TEXT NOT NULL,
            tags TEXT NOT NULL,
            client_price REAL NOT NULL,
            admin_proposed_price REAL,
            status TEXT NOT NULL DEFAULT 'awaiting_payment',
            payment_method TEXT DEFAULT '',
            admin_comment TEXT DEFAULT '',
            created_at REAL NOT NULL,
            completed_at REAL,
            auto_delete_at REAL,
            is_paid BOOLEAN DEFAULT 0
        )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON orders(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON orders(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON orders(created_at)")
        
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
        await db.execute("""
        CREATE TABLE IF NOT EXISTS action_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at REAL NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            blocked_at REAL NOT NULL,
            reason TEXT DEFAULT 'Отказ от соглашения'
        )
        """)
        await db.commit()
        logger.info("База данных инициализирована")

async def log_action(user_id: int, action: str, details: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO action_logs (user_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            (user_id, action, details, time.time())
        )
        await db.commit()

# --- Блокировка пользователей ---
async def is_user_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def block_user(user_id: int, reason: str = "Отказ от соглашения"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blocked_users (user_id, blocked_at, reason) VALUES (?, ?, ?)",
            (user_id, time.time(), reason)
        )
        await db.commit()

# --- Скидки и промокоды ---
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

# --- Основные функции ---
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

async def create_order(user_id: int, title: str, tz: str, description: str, tags: str, price: float, payment_method: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO orders (
                user_id, title, tz, description, tags, client_price, payment_method, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, title, tz, description, tags, price, payment_method, time.time())
        )
        await db.commit()
        return cursor.lastrowid

async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cursor:
            return await cursor.fetchone()

async def get_user_orders(user_id: int, limit: int = ORDERS_PER_PAGE, offset: int = 0) -> List:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)
        ) as cursor:
            return await cursor.fetchall()

async def get_user_orders_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def get_all_orders(limit: int = ORDERS_PER_PAGE, offset: int = 0, status_filter: str = None) -> List:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status_filter:
            async with db.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status_filter, limit, offset)
            ) as cursor:
                return await cursor.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ) as cursor:
                return await cursor.fetchall()

async def get_all_orders_count(status_filter: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if status_filter:
            async with db.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status_filter,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
        else:
            async with db.execute("SELECT COUNT(*) FROM orders") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

async def get_orders_today() -> int:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (today,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

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
        f"📦 Заказ #{order['id']}\n"
        f"Статус: {status_text}\n"
        f"Название: {order['title']}\n"
    )
    if order["tz"]:
        text += f"ТЗ: {order['tz']}\n"
    text += (
        f"Описание: {order['description']}\n"
        f"Теги: {tags_str}\n"
        f"Цена: {price}\n"
        f"Оплата: {order['payment_method']}\n"
        f"Создан: {format_time_ago(order['created_at'])}\n"
    )
    if for_admin:
        text += f"👤 Клиент: {order['user_id']}\n"
        if order["admin_comment"]:
            text += f"💬 Комментарий: {order['admin_comment']}\n"
    return text

def format_time_ago(timestamp: float) -> str:
    delta = time.time() - timestamp
    if delta < 60:
        return f"{int(delta)} с назад"
    elif delta < 3600:
        return f"{int(delta // 60)} мин назад"
    elif delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    else:
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%d.%m %H:%M")

async def safe_send_message(chat_id: int, text: str, **kwargs):
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError as e:
        logger.warning(f"Не удалось отправить сообщение {chat_id}: {e}")

async def safe_edit_message(message: Message, text: str, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
    except TelegramAPIError as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            logger.debug("Сообщение не изменено")
        elif "MESSAGE_CANT_BE_EDITED" in str(e):
            logger.debug("Сообщение нельзя редактировать")
        else:
            logger.warning(f"Ошибка редактирования: {e}")

_background_tasks = set()

async def schedule_order_deletion(order_id: int, delay: int):
    await asyncio.sleep(delay)
    await delete_order(order_id)
    logger.info(f"Заказ #{order_id} удалён (автоочистка)")

def create_deletion_task(order_id: int, delay: int):
    task = asyncio.create_task(schedule_order_deletion(order_id, delay))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

# === Соглашение ===
AGREEMENT_TEXT = (
    "📌 Перед созданием заказа Вы (Заказчик) подтверждаете, что прочитали, поняли и безоговорочно принимаете следующие условия:\n"
    "1. Вы несёте полную ответственность за содержание, формулировку и законность заказа.\n"
    "2. Заказ не должен нарушать законодательство Российской Федерации, а также законодательство страны Вашего проживания. "
    "В случае нарушения — вся юридическая, финансовая и иная ответственность возлагается исключительно на Вас.\n"
    "3. Вы обязуетесь оплатить услугу/товар после подтверждения исполнителем готовности к выполнению заказа.\n"
    "4. Отмена заказа после начала работы возможна только по письменному согласованию с Исполнителем и не гарантирует возврат средств.\n"
    "5. Вы соглашаетесь, что Исполнитель вправе:\n"
    "   • отказать в выполнении заказа без объяснения причин (например, при некорректном оформлении, сомнениях в законности или неясной формулировке);\n"
    "   • предложить любую услугу или товар по своему усмотрению — если это не нарушает его прав;\n"
    "   • устанавливать любую цену на услугу/товар (в т.ч. индивидуально), в том числе изменять её до подтверждения заказа.\n"
    "6. Вы обязуетесь НЕ:\n"
    "   • разглашать третьим лицам информацию о содержании, стоимости или факте приобретения услуги/товара без предварительного письменного разрешения Исполнителя;\n"
    "   • распространять, публиковать, перепродавать, модифицировать или иным образом использовать полученный товар/услугу (включая результаты работы), если иное прямо не согласовано с Исполнителем.\n"
    "✅ Принимаете условия?"
)

# === Команды ===
@router.message(Command("start"))
async def cmd_start(message: Message):
    if await is_user_blocked(message.from_user.id):
        await message.answer("❌ Доступ к боту ограничен. Вы отказались от соглашения. Обратитесь к администратору.")
        return

    if message.from_user.id == ADMIN_USER_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💼 Заработок", callback_data="menu_adminstats")],
            [InlineKeyboardButton(text="📥 Все заказы", callback_data="menu_adminorders")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="menu_broadcast")],
            [InlineKeyboardButton(text="🎟 Промокоды", callback_data="menu_promo")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="menu_status")],
        ])
        await message.answer("👋 Админ-меню:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Создать заказ", callback_data="menu_neworder")],
            [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu_myorders")],
            [InlineKeyboardButton(text="💰 Мои траты", callback_data="menu_mystats")],
            [InlineKeyboardButton(text="📄 Соглашение", callback_data="menu_agreement")],
        ])
        await message.answer("👋 Меню:", reply_markup=kb)
    await log_action(message.from_user.id, "start", "Бот запущен")

# === Меню обработчики ===
@router.callback_query(F.data == "menu_neworder")
async def menu_neworder(callback: CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    await state.set_state(CreateOrder.waiting_for_agreement)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю", callback_data="agree_yes")],
        [InlineKeyboardButton(text="❌ Отказываюсь", callback_data="agree_no")]
    ])
    await callback.message.answer(AGREEMENT_TEXT, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "menu_myorders")
async def menu_myorders(callback: CallbackQuery, page: int = 0):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    user_id = callback.from_user.id
    orders_list = await get_user_orders(user_id, limit=ORDERS_PER_PAGE, offset=page * ORDERS_PER_PAGE)
    total_count = await get_user_orders_count(user_id)
    
    if not orders_list:
        await callback.message.answer("📭 У Вас пока нет заказов.")
        await callback.answer()
        return

    keyboard = []
    for order in orders_list:
        status_icon = {
            "pending": "⏳", "in_progress": "🛠", "completed": "✅",
            "cancelled": "❌", "awaiting_payment": "💳", "price_proposed": "💬"
        }.get(order["status"], "❓")
        
        title = (order['title'][:25] + "...") if len(order['title']) > 25 else order['title']
        time_str = format_time_ago(order["created_at"])
        
        keyboard.append([
            InlineKeyboardButton(
                text=f"{status_icon} #{order['id']} — {title} ({time_str})",
                callback_data=f"view_order_{order['id']}"
            )
        ])
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myorders_page_{page - 1}"))
    if (page + 1) * ORDERS_PER_PAGE < total_count:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"myorders_page_{page + 1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="start_menu")])
    
    await callback.message.answer(
        f"📋 Ваши заказы (стр. {page + 1}):\n",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("myorders_page_"))
async def myorders_pagination(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    page = int(callback.data.split("_")[2])
    await menu_myorders(callback, page)

@router.callback_query(F.data.startswith("view_order_"))
async def view_order_details(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    user_id = callback.from_user.id
    order_id = int(callback.data.split("_")[2])
    
    order = await get_order(order_id)
    if not order or order["user_id"] != user_id:
        await callback.answer("❌ Заказ не найден или не Ваш.", show_alert=True)
        return
    
    text = format_order_message(order)
    
    edit_buttons = []
    if order["status"] in ["pending", "awaiting_payment"]:
        edit_buttons.append(InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_order_{order_id}"))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        edit_buttons if edit_buttons else [],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="menu_myorders")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="start_menu")]
    ])
    
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("edit_order_"))
async def start_edit_order(callback: CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    user_id = callback.from_user.id
    order_id = int(callback.data.split("_")[2])
    
    order = await get_order(order_id)
    if not order or order["user_id"] != user_id:
        await callback.answer("❌ Заказ не найден или не Ваш.", show_alert=True)
        return
    
    if order["status"] not in ["pending", "awaiting_payment"]:
        await callback.answer("❌ Нельзя редактировать заказ в текущем статусе.", show_alert=True)
        return
    
    await state.set_state(AdminFSM.edit_order_title)
    await state.update_data(edit_order_id=order_id)
    await callback.message.answer("Введите новое название заказа:")
    await callback.answer()

@router.message(AdminFSM.edit_order_title)
async def process_edit_order_title(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id):
        return
    user_id = message.from_user.id
    data = await state.get_data()
    order_id = data.get("edit_order_id")
    
    order = await get_order(order_id)
    if not order or order["user_id"] != user_id:
        await message.answer("❌ Ошибка доступа.")
        await state.clear()
        return
    
    await update_order(order_id, title=message.text)
    await message.answer("✅ Название обновлено!")
    await state.clear()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К заказам", callback_data="menu_myorders")]
    ])
    await message.answer(format_order_message(await get_order(order_id)), reply_markup=kb)

@router.callback_query(F.data == "start_menu")
async def start_menu(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    if callback.from_user.id == ADMIN_USER_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💼 Заработок", callback_data="menu_adminstats")],
            [InlineKeyboardButton(text="📥 Все заказы", callback_data="menu_adminorders")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="menu_broadcast")],
            [InlineKeyboardButton(text="🎟 Промокоды", callback_data="menu_promo")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="menu_status")],
        ])
        await callback.message.answer("👋 Админ-меню:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Создать заказ", callback_data="menu_neworder")],
            [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu_myorders")],
            [InlineKeyboardButton(text="💰 Мои траты", callback_data="menu_mystats")],
            [InlineKeyboardButton(text="📄 Соглашение", callback_data="menu_agreement")],
        ])
        await callback.message.answer("👋 Меню:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "menu_mystats")
async def menu_mystats(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    total = await get_user_total_spent(callback.from_user.id)
    await callback.message.answer(f"💰 Всего потрачено: {total:.2f} ₽")
    await callback.answer()

@router.callback_query(F.data == "menu_adminstats")
async def menu_adminstats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    total = await get_admin_total_earned()
    await callback.message.answer(f"💼 Ваш заработок: {total:.2f} ₽")
    await callback.answer()

@router.callback_query(F.data == "menu_agreement")
async def menu_agreement(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    await callback.message.answer(AGREEMENT_TEXT)
    await callback.answer()

@router.callback_query(F.data == "menu_status")
async def menu_status(callback: CallbackQuery):
    uptime = time.time() - START_TIME
    days = int(uptime // 86400)
    hours = int((uptime % 86400) // 3600)
    orders_today = await get_orders_today()
    text = (
        f"📊 Статус бота:\n"
        f"⏱ Работает: {days} дн {hours} ч\n"
        f"📥 Заказов сегодня: {orders_today}\n"
        f"💬 Версия: 3.3 (Fixed Style)"
    )
    await callback.message.answer(text)
    await callback.answer()

@router.callback_query(F.data == "menu_broadcast")
async def menu_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminFSM.broadcast_text)
    await callback.message.answer("📢 Введите текст рассылки:")
    await callback.answer()

@router.callback_query(F.data == "menu_promo")
async def menu_promo(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Создать", callback_data="promo_create")],
        [InlineKeyboardButton(text="📋 Список", callback_data="promo_list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="promo_delete_start")],
    ])
    await callback.message.answer("🎟 Промокоды:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "promo_create")
async def promo_create_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminFSM.promo_code)
    await callback.message.answer("Название промокода:")
    await callback.answer()

@router.callback_query(F.data == "promo_list")
async def promo_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promo_codes") as cursor:
            promos = await cursor.fetchall()
        if not promos:
            await callback.message.answer("Нет промокодов.")
            await callback.answer()
            return
        text = "🎟 Промокоды:\n"
        for p in promos:
            disc = f"{p['discount_value']}%" if p['discount_type'] == 'percent' else f"{p['discount_value']} ₽"
            text += f"`{p['code']}` — {disc}, {p['current_uses']}/{p['max_uses']}\n"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В меню промокодов", callback_data="menu_promo")]
        ])
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
        await callback.answer()

@router.callback_query(F.data == "promo_delete_start")
async def promo_delete_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminFSM.waiting_for_promo_delete_code)
    await callback.message.answer("Введите код промокода, который хотите удалить:")
    await callback.answer()

@router.message(AdminFSM.waiting_for_promo_delete_code)
async def promo_delete_enter_code(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    
    code = message.text.strip().upper()
    promo = await get_promo_code(code)
    
    if not promo:
        await message.answer("❌ Промокод не найден. Введите корректный код:")
        return
    
    await state.update_data(delete_code=code)
    await state.set_state(AdminFSM.waiting_for_promo_delete_confirm)
    
    disc = f"{promo['discount_value']}%" if promo['discount_type'] == 'percent' else f"{promo['discount_value']} ₽"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="promo_confirm_del")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="promo_cancel_del")]
    ])
    await message.answer(
        f"⚠️ Вы уверены, что хотите удалить промокод?\n"
        f"Код: `{code}`\n"
        f"Скидка: {disc}\n"
        f"Использовано: {promo['current_uses']}/{promo['max_uses']}",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "promo_confirm_del")
async def promo_confirm_del(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    
    data = await state.get_data()
    code = data.get("delete_code")
    
    if code:
        await delete_promo_code(code)
        await log_action(ADMIN_USER_ID, "delete_promo", f"Удалён промокод {code}")
        await callback.message.answer(f"✅ Промокод `{code}` успешно удалён.")
    
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "promo_cancel_del")
async def promo_cancel_del(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
        return
    await state.clear()
    # ✅ ИСПРАВЛЕНО: Теперь сообщение отправляется через callback.message
    await callback.message.answer("❌ Удаление отменено.")
    await callback.answer()

@router.callback_query(F.data.in_({"agree_yes", "agree_no"}))
async def handle_agreement(callback: CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    if callback.data == "agree_no":
        await state.set_state(CreateOrder.waiting_for_agreement_refusal_confirm)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⛔ Да, заблокировать", callback_data="refusal_confirm_yes")],
            [InlineKeyboardButton(text="🔙 Нет, вернуться", callback_data="refusal_confirm_no")]
        ])
        await callback.message.answer(
            "⚠️ Вы уверены?\nВ случае подтверждения отказа доступ к боту будет ограничен. "
            "Для восстановления доступа Вам потребуется обратиться к администратору.",
            reply_markup=kb
        )
        await callback.answer()
        return

    await state.set_state(CreateOrder.waiting_for_title)
    await callback.message.answer("Название заказа:")
    await callback.answer()

@router.callback_query(F.data.in_({"refusal_confirm_yes", "refusal_confirm_no"}))
async def handle_agreement_refusal_confirm(callback: CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    if callback.data == "refusal_confirm_yes":
        await block_user(callback.from_user.id, "Отказ от соглашения")
        await state.clear()
        await callback.message.answer(
            "❌ Вы отказались от соглашения.\nДоступ к боту ограничен. Обратитесь к администратору для разблокировки."
        )
        await callback.answer()
    else:
        await state.set_state(CreateOrder.waiting_for_agreement)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принимаю", callback_data="agree_yes")],
            [InlineKeyboardButton(text="❌ Отказываюсь", callback_data="agree_no")]
        ])
        await callback.message.answer(AGREEMENT_TEXT, reply_markup=kb)
        await callback.answer()

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id):
        return
    await state.clear()
    await message.answer("❌ Состояние сброшено.")
    await log_action(message.from_user.id, "cancel", "FSM сброшен")

@router.message(Command("neworder"))
async def new_order_start(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id):
        await message.answer("❌ Доступ ограничен.")
        return
    if message.from_user.id == ADMIN_USER_ID:
        await message.answer("Админ не создаёт заказы.")
        return
    await state.set_state(CreateOrder.waiting_for_agreement)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю", callback_data="agree_yes")],
        [InlineKeyboardButton(text="❌ Отказываюсь", callback_data="agree_no")]
    ])
    await message.answer(AGREEMENT_TEXT, reply_markup=kb)

@router.message(CreateOrder.waiting_for_title)
async def process_title(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id): return
    await state.update_data(title=message.text)
    await state.set_state(CreateOrder.waiting_for_tz_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Есть ТЗ", callback_data="tz_yes")],
        [InlineKeyboardButton(text="❌ Нет ТЗ", callback_data="tz_no")]
    ])
    await message.answer("Есть ли у Вас техническое задание (ТЗ)?", reply_markup=kb)

@router.callback_query(F.data.in_({"tz_yes", "tz_no"}))
async def process_tz_choice(callback: CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    if callback.data == "tz_yes":
        await state.set_state(CreateOrder.waiting_for_tz)
        await callback.message.answer("Пришлите текст ТЗ:")
    else:
        await state.update_data(tz="")
        await state.set_state(CreateOrder.waiting_for_description)
        await callback.message.answer("Описание:")
    await callback.answer()

@router.message(CreateOrder.waiting_for_tz)
async def process_tz(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id): return
    await state.update_data(tz=message.text)
    await state.set_state(CreateOrder.waiting_for_description)
    await message.answer("Описание:")

@router.message(CreateOrder.waiting_for_description)
async def process_description(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id): return
    await state.update_data(description=message.text)
    await state.set_state(CreateOrder.waiting_for_tags)
    await message.answer("Теги через запятую:")

@router.message(CreateOrder.waiting_for_tags)
async def process_tags(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id): return
    await state.update_data(tags=message.text)
    await state.set_state(CreateOrder.waiting_for_price)
    # ✅ ИСПРАВЛЕНО: обращение на "Вы"
    await message.answer("Цена в ₽:\n(указывается сколько Вы готовы оплатить за работу)")

@router.message(CreateOrder.waiting_for_price)
async def process_price(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id): return
    try:
        price = float(message.text)
        if price <= 0 or price > 1_000_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 0 до 1 000 000 000.")
        return
    await state.update_data(price=price)
    
    await state.set_state(CreateOrder.waiting_for_promo_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, есть", callback_data="promo_yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="promo_no")]
    ])
    await message.answer("Есть ли у Вас промокод?", reply_markup=kb)

@router.callback_query(F.data.in_({"promo_yes", "promo_no"}))
async def process_promo_choice(callback: CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return

    if callback.data == "promo_no":
        await state.update_data(final_price=await state.get_value('price'))
        await state.set_state(CreateOrder.waiting_for_payment_method)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 Наличные", callback_data="pay_cash")],
            [InlineKeyboardButton(text="💳 Карта (СБП ВТБ)", callback_data="pay_card")],
            [InlineKeyboardButton(text="🎁 Товар/Услуга (Эквивалент)", callback_data="pay_equivalent")]
        ])
        await callback.message.answer("Выберите способ оплаты:", reply_markup=kb)
        await callback.answer()
    else:
        await state.set_state(CreateOrder.waiting_for_promo_code)
        await state.update_data(promo_attempts=0)
        await callback.message.answer("Введите промокод:")
        await callback.answer()

@router.message(CreateOrder.waiting_for_promo_code)
async def process_promo_code(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id): return
    
    data = await state.get_data()
    attempts = data.get("promo_attempts", 0)
    original_price = data.get("price", 0)
    
    code = message.text.strip().upper()
    promo = await get_promo_code(code)
    
    if promo and promo["current_uses"] < promo["max_uses"]:
        discount = promo["discount_value"]
        if promo["discount_type"] == "percent":
            final_price = original_price * (1 - discount / 100)
            disc_text = f"{discount}%"
        else:
            final_price = max(0, original_price - discount)
            disc_text = f"{discount} ₽"
        
        await use_promo_code(code)
        await state.update_data(final_price=final_price)
        await message.answer(f"✅ Промокод применён! Скидка: {disc_text}. Новая цена: {final_price:.2f} ₽")
        
        await state.set_state(CreateOrder.waiting_for_payment_method)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 Наличные", callback_data="pay_cash")],
            [InlineKeyboardButton(text="💳 Карта (СБП ВТБ)", callback_data="pay_card")],
            [InlineKeyboardButton(text="🎁 Товар/Услуга (Эквивалент)", callback_data="pay_equivalent")]
        ])
        await message.answer("Выберите способ оплаты:", reply_markup=kb)
        await state.update_data(promo_applied=True)
    else:
        attempts += 1
        await state.update_data(promo_attempts=attempts)
        
        if attempts >= 3:
            await message.answer("❌ Промокод неверный (3 попытки исчерпаны). Продолжаем без скидки.")
            await state.update_data(final_price=original_price)
            await state.set_state(CreateOrder.waiting_for_payment_method)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💵 Наличные", callback_data="pay_cash")],
                [InlineKeyboardButton(text="💳 Карта (СБП ВТБ)", callback_data="pay_card")],
                [InlineKeyboardButton(text="🎁 Товар/Услуга (Эквивалент)", callback_data="pay_equivalent")]
            ])
            await message.answer("Выберите способ оплаты:", reply_markup=kb)
        else:
            await message.answer(f"❌ Неверный промокод. Осталось попыток: {3 - attempts}")

@router.callback_query(F.data.in_({"pay_cash", "pay_card", "pay_equivalent"}))
async def process_payment_method(callback: CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    if callback.from_user.id == ADMIN_USER_ID:
        await callback.answer("Админ не создаёт заказы.", show_alert=True)
        return

    data = await state.get_data()
    
    if callback.data == "pay_equivalent":
        await state.set_state(CreateOrder.waiting_for_equivalent_item)
        await callback.message.answer("Что именно Вы предлагаете взамен (название товара/услуги)?")
        await callback.answer()
        return

    method = "наличные" if callback.data == "pay_cash" else "карта"
    await state.update_data(payment_method=method)
    
    if callback.data == "pay_card":
        await callback.message.answer("💳 Оплатите по СБП на банк ВТБ:\n+7 953 850-72-79")
    
    order_id = await create_order(
        user_id=callback.from_user.id,
        title=data["title"],
        tz=data.get("tz", ""),
        description=data["description"],
        tags=data["tags"],
        price=data["final_price"],
        payment_method=method
    )
    await set_last_order_time(callback.from_user.id, time.time())
    await state.clear()
    await log_action(callback.from_user.id, "create_order", f"Заказ #{order_id}")
    
    if method == "наличные":
        await update_order(order_id, status="pending")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Взять", callback_data=f"take_{order_id}")],
            [InlineKeyboardButton(text="🗑 Игнорировать", callback_data=f"ignore_{order_id}")]
        ])
        await safe_send_message(
            ADMIN_USER_ID,
            f"🆕 Новый заказ!\n{format_order_message(await get_order(order_id), for_admin=True)}",
            reply_markup=kb
        )
        await callback.message.answer(f"✅ Заказ #{order_id} отправлен админу!\nДля повторного вызова меню напишите /start")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплатил", callback_data=f"paid_{order_id}")],
            [InlineKeyboardButton(text="❌ Не оплатил", callback_data=f"not_paid_{order_id}")]
        ])
        await callback.message.answer("Оплатили?", reply_markup=kb)
    await callback.answer()

@router.message(CreateOrder.waiting_for_equivalent_item)
async def process_equivalent_item(message: Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id): return
    
    item_name = message.text
    data = await state.get_data()
    
    payment_method_str = f"Эквивалент: {item_name}"
    
    order_id = await create_order(
        user_id=message.from_user.id,
        title=data["title"],
        tz=data.get("tz", ""),
        description=data["description"],
        tags=data["tags"],
        price=data["final_price"],
        payment_method=payment_method_str
    )
    await set_last_order_time(message.from_user.id, time.time())
    await state.clear()
    await log_action(message.from_user.id, "create_order", f"Заказ #{order_id} (Эквивалент)")
    
    await update_order(order_id, status="pending")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Взять", callback_data=f"take_{order_id}")],
        [InlineKeyboardButton(text="🗑 Игнорировать", callback_data=f"ignore_{order_id}")]
    ])
    await safe_send_message(
        ADMIN_USER_ID,
        f"🆕 Новый заказ (Эквивалент)!\n{format_order_message(await get_order(order_id), for_admin=True)}",
        reply_markup=kb
    )
    await message.answer(f"✅ Заказ #{order_id} отправлен админу!\nДля повторного вызова меню напишите /start")

@router.callback_query(F.data.startswith("paid_"))
async def handle_paid(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    if callback.from_user.id == ADMIN_USER_ID:
        return
    order_id = int(callback.data.split("_")[1])
    await update_order(order_id, status="pending")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Взять", callback_data=f"take_{order_id}")],
        [InlineKeyboardButton(text="🗑 Игнорировать", callback_data=f"ignore_{order_id}")]
    ])
    await safe_send_message(
        ADMIN_USER_ID,
        f"🆕 Новый заказ!\n{format_order_message(await get_order(order_id), for_admin=True)}",
        reply_markup=kb
    )
    await callback.message.answer(f"✅ Заказ #{order_id} отправлен админу!\nДля повторного вызова меню напишите /start")
    await callback.answer()

@router.callback_query(F.data.startswith("not_paid_"))
async def handle_not_paid(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    if callback.from_user.id == ADMIN_USER_ID:
        return
    order_id = int(callback.data.split("_")[1])
    await callback.message.answer(
        f"Заказ #{order_id} сохранён в статусе «ожидает оплаты».\n"
        f"Когда оплатите — напишите: /pay_order {order_id}"
    )
    await callback.answer()

@router.message(Command("pay_order"))
async def pay_order_later(message: Message):
    if await is_user_blocked(message.from_user.id):
        await message.answer("❌ Доступ ограничен.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /pay_order ID")
        return
    try:
        order_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    order = await get_order(order_id)
    if not order or order["user_id"] != message.from_user.id:
        await message.answer("Заказ не найден.")
        return
    if order["status"] != "awaiting_payment":
        await message.answer("Этот заказ уже обработан.")
        return
    if order["payment_method"] != "карта":
        await message.answer("Этот заказ не требует онлайн-оплаты.")
        return
    await update_order(order_id, status="pending")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Взять", callback_data=f"take_{order_id}")],
        [InlineKeyboardButton(text="🗑 Игнорировать", callback_data=f"ignore_{order_id}")]
    ])
    await safe_send_message(
        ADMIN_USER_ID,
        f"🆕 Новый заказ!\n{format_order_message(order, for_admin=True)}",
        reply_markup=kb
    )
    await message.answer(f"✅ Заказ #{order_id} отправлен админу!\nДля повторного вызова меню напишите /start")

# === Рассылка ===
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
        if await is_user_blocked(uid):
            continue
        try:
            await bot.send_message(uid, f"📢 Рассылка:\n{message.text}")
            success += 1
        except:
            pass
    await message.answer(f"✅ Рассылка отправлена {success} пользователям.")
    await state.clear()
    await log_action(message.from_user.id, "broadcast", f"Отправлено {success} пользователям")

# === Промокоды (Создание) ===
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
        [InlineKeyboardButton(text="Проценты (%)", callback_data="promo_type_percent")],
        [InlineKeyboardButton(text="Рубли (₽)", callback_data="promo_type_fixed")]
    ])
    await message.answer("Тип скидки:", reply_markup=kb)

@router.callback_query(F.data.in_({"promo_type_percent", "promo_type_fixed"}))
async def promo_type_selected(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("❌ Доступ запрещён", show_alert=True)
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
    await log_action(message.from_user.id, "create_promo", f"Промокод {data['code']}")

# === Основные команды ===
@router.message(Command("myorders"))
async def my_orders_cmd(message: Message):
    if await is_user_blocked(message.from_user.id):
        await message.answer("❌ Доступ ограничен.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Открыть список", callback_data="menu_myorders")]
    ])
    await message.answer("📋 Перейдите к списку заказов:", reply_markup=kb)

@router.message(Command("mystats"))
async def my_stats_cmd(message: Message):
    if await is_user_blocked(message.from_user.id):
        await message.answer("❌ Доступ ограничен.")
        return
    total = await get_user_total_spent(message.from_user.id)
    await message.answer(f"💰 Всего потрачено: {total:.2f} ₽")

@router.message(Command("adminstats"))
async def admin_stats_cmd(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    total = await get_admin_total_earned()
    await message.answer(f"💼 Ваш заработок: {total:.2f} ₽")

@router.message(Command("status"))
async def status_cmd(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    uptime = time.time() - START_TIME
    days = int(uptime // 86400)
    hours = int((uptime % 86400) // 3600)
    orders_today = await get_orders_today()
    text = (
        f"📊 Статус бота:\n"
        f"⏱ Работает: {days} дн {hours} ч\n"
        f"📥 Заказов сегодня: {orders_today}\n"
        f"💬 Версия: 3.3 (Fixed Style)"
    )
    await message.answer(text)

# === Админ-обработчики заказов ===
@router.callback_query(F.data.startswith("take_"))
async def admin_take(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    
    await update_order(order_id, status="in_progress")
    await safe_send_message(order["user_id"], f"✅ Заказ #{order_id} взят в работу!")
    await log_action(ADMIN_USER_ID, "take_order", f"Заказ #{order_id}")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Цена", callback_data=f"change_price_{order_id}")],
        [InlineKeyboardButton(text="✏️ Коммент", callback_data=f"comment_{order_id}")],
        [InlineKeyboardButton(text="✅ Завершить", callback_data=f"complete_{order_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_del_{order_id}")]
    ])
    await safe_edit_message(
        callback.message,
        f"{format_order_message(order, for_admin=True)}\n🛠 Взят в работу.",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("ignore_"))
async def admin_ignore(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    await update_order(order_id, status="cancelled")
    order = await get_order(order_id)
    if order:
        await safe_send_message(
            order["user_id"],
            f"❌ Ваш заказ #{order_id} отменён.\nХотите создать новый? /neworder"
        )
    await log_action(ADMIN_USER_ID, "ignore_order", f"Заказ #{order_id}")
    await safe_edit_message(callback.message, "🗑 Игнорировано.")
    await callback.answer()

@router.callback_query(F.data.startswith("change_price_"))
async def start_change_price(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    await state.set_state(AdminFSM.change_price)
    await state.update_data(order_id=order_id)
    await callback.message.answer("Новая цена в ₽:")
    await callback.answer()

@router.callback_query(F.data.startswith("comment_"))
async def start_comment(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    await state.set_state(AdminFSM.admin_comment)
    await state.update_data(order_id=order_id)
    await callback.message.answer("Комментарий клиенту:")
    await callback.answer()

@router.message(AdminFSM.change_price)
async def handle_admin_change_price(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_USER_ID:
        return
    data = await state.get_data()
    order_id = data["order_id"]
    await update_order(order_id, admin_comment=message.text)
    order = await get_order(order_id)
    await safe_send_message(order["user_id"], f"💬 {message.text}")
    await message.answer("Комментарий отправлен.")
    await state.clear()
    await log_action(ADMIN_USER_ID, "add_comment", f"Заказ #{order_id}")

@router.callback_query(F.data.startswith("accept_price_"))
async def client_accept_price(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("Не Ваш заказ.", show_alert=True)
        return
    await update_order(order_id, status="in_progress")
    await safe_edit_message(callback.message, "✅ Цена принята. Заказ в работе.")
    await safe_send_message(ADMIN_USER_ID, f"Клиент принял цену по заказу #{order_id}.")
    await callback.answer()

@router.callback_query(F.data.startswith("reject_price_"))
async def client_reject_price(callback: CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("❌ Доступ ограничен.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    order = await get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("Не Ваш заказ.", show_alert=True)
        return
    await update_order(order_id, status="cancelled")
    await safe_edit_message(callback.message, "❌ Заказ отменён.")
    await safe_send_message(ADMIN_USER_ID, f"Клиент отклонил цену по заказу #{order_id}.")
    await callback.answer()

@router.callback_query(F.data.startswith("complete_"))
async def complete_order(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    await update_order(order_id, status="completed", completed_at=time.time())
    order = await get_order(order_id)
    if order:
        await safe_send_message(order["user_id"], f"✅ Заказ #{order_id} завершён!")
    final_price = order["admin_proposed_price"] if order and order["admin_proposed_price"] else order["client_price"] if order else 0
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Да, +{final_price:.2f} ₽", callback_data=f"confirm_earn_{order_id}")],
        [InlineKeyboardButton(text="❌ Нет", callback_data=f"skip_earn_{order_id}")]
    ])
    await safe_edit_message(
        callback.message,
        f"Заказ #{order_id} завершён.\nЗаписать {final_price:.2f} ₽ в заработок?",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_earn_"))
async def confirm_earning(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    await update_order(order_id, is_paid=1)
    order = await get_order(order_id)
    final_price = order["admin_proposed_price"] if order and order["admin_proposed_price"] else order["client_price"] if order else 0
    await safe_edit_message(callback.message, f"✅ {final_price:.2f} ₽ записано в заработок.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕒 3 ч", callback_data=f"keep_3_{order_id}")],
        [InlineKeyboardButton(text="🕕 6 ч", callback_data=f"keep_6_{order_id}")],
        [InlineKeyboardButton(text="🕛 12 ч", callback_data=f"keep_12_{order_id}")],
        [InlineKeyboardButton(text="📆 24 ч", callback_data=f"keep_24_{order_id}")],
        [InlineKeyboardButton(text="🗑 Сейчас", callback_data=f"del_now_{order_id}")]
    ])
    await callback.message.answer("Сохранить заказ на:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("skip_earn_"))
async def skip_earning(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    await safe_edit_message(callback.message, "❌ Сумма НЕ записана в заработок.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕒 3 ч", callback_data=f"keep_3_{order_id}")],
        [InlineKeyboardButton(text="🕕 6 ч", callback_data=f"keep_6_{order_id}")],
        [InlineKeyboardButton(text="🕛 12 ч", callback_data=f"keep_12_{order_id}")],
        [InlineKeyboardButton(text="📆 24 ч", callback_data=f"keep_24_{order_id}")],
        [InlineKeyboardButton(text="🗑 Сейчас", callback_data=f"del_now_{order_id}")]
    ])
    await callback.message.answer("Сохранить заказ на:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("keep_"))
async def keep_order(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    hours = int(callback.data.split("_")[1])
    order_id = int(callback.data.split("_")[2])
    delay = hours * 3600
    await update_order(order_id, auto_delete_at=time.time() + delay)
    create_deletion_task(order_id, delay)
    await safe_edit_message(callback.message, f"Будет удалён через {hours} ч.")
    await callback.answer()

@router.callback_query(F.data.startswith("del_now_"))
async def del_now(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    await delete_order(order_id)
    await safe_edit_message(callback.message, "Удалён.")
    await callback.answer()

@router.callback_query(F.data.startswith("admin_del_"))
async def admin_del(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: 
        await callback.answer("❌ Только для админа", show_alert=True)
        return
    order_id = int(callback.data.split("_")[2])
    await delete_order(order_id)
    await safe_edit_message(callback.message, "Удалён вручную.")
    await callback.answer()

# === Запуск ===
async def main():
    await init_db()
    logger.info("🚀 Бот запущен (Версия 3.3 - Fixed Style)")
    logger.info(f"👤 Admin ID: {ADMIN_USER_ID}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())