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

# === FSM ===
class CreateOrder(StatesGroup):
    waiting_for_title = State()
    waiting_for_tz_choice = State()
    waiting_for_tz = State()
    waiting_for_description = State()
    waiting_for_tags = State()
    waiting_for_price = State()
    waiting_for_payment_method = State()

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_last_order (
                user_id INTEGER PRIMARY KEY,
                last_order_time REAL NOT NULL
            )
        """)
        await db.commit()

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
    )
    if order["tz"]:
        text += f"ТЗ: {order['tz']}\n"
    text += (
        f"Описание: {order['description']}\n"
        f"Теги: {tags_str}\n"
        f"Цена: {price}\n"
        f"Оплата: {order['payment_method']}\n"
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

# === Команды ===
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id == ADMIN_USER_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💼 Заработок", callback_data="menu_adminstats")],
            [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu_myorders")]
        ])
        await message.answer("👋 Админ-меню:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Создать заказ", callback_data="menu_neworder")],
            [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu_myorders")],
            [InlineKeyboardButton(text="💰 Мои траты", callback_data="menu_mystats")]
        ])
        await message.answer("👋 Меню:", reply_markup=kb)

# === Меню обработчики ===
@router.callback_query(F.data == "menu_neworder")
async def menu_neworder(callback: CallbackQuery, state: FSMContext):
    await new_order_start(callback.message, state)
    await callback.answer()

@router.callback_query(F.data == "menu_myorders")
async def menu_myorders(callback: CallbackQuery):
    await my_orders(callback.message)
    await callback.answer()

@router.callback_query(F.data == "menu_mystats")
async def menu_mystats(callback: CallbackQuery):
    await my_stats(callback.message)
    await callback.answer()

@router.callback_query(F.data == "menu_adminstats")
async def menu_adminstats(callback: CallbackQuery):
    await admin_stats(callback.message)
    await callback.answer()

# === Создание заказа ===
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
    await state.set_state(CreateOrder.waiting_for_tz_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Есть ТЗ", callback_data="tz_yes")],
        [InlineKeyboardButton(text="❌ Нет ТЗ", callback_data="tz_no")]
    ])
    await message.answer("Есть ли у вас техническое задание (ТЗ)?", reply_markup=kb)

@router.callback_query(F.data.in_({"tz_yes", "tz_no"}))
async def process_tz_choice(callback: CallbackQuery, state: FSMContext):
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
    await state.update_data(tz=message.text)
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
        if price < 0 or price > 1_000_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 0 до 1 000 000 000.")
        return

    await state.update_data(price=price)
    await state.set_state(CreateOrder.waiting_for_payment_method)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Наличные", callback_data="pay_cash")],
        [InlineKeyboardButton(text="💳 Карта (СБП ВТБ)", callback_data="pay_card")]
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
            "💳 Оплатите по СБП на банк ВТБ:\n+7 953 850-72-79"
        )

    data = await state.get_data()
    order_id = await create_order(
        user_id=callback.from_user.id,
        title=data["title"],
        tz=data.get("tz", ""),
        description=data["description"],
        tags=data["tags"],
        price=data["price"],
        payment_method=method
    )
    await set_last_order_time(callback.from_user.id, time.time())
    await state.clear()

    if method == "наличные":
        # Для наличных — сразу отправляем админу, так как оплата "в момент передачи"
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
        await callback.message.answer(f"✅ Заказ #{order_id} отправлен админу!")
    else:
        # Для карты — ждём подтверждения оплаты
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оплатил", callback_data=f"paid_{order_id}")],
            [InlineKeyboardButton(text="❌ Не оплатил", callback_data=f"not_paid_{order_id}")]
        ])
        await callback.message.answer("Оплатили?", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("paid_"))
async def handle_paid(callback: CallbackQuery):
    if callback.from_user.id == ADMIN_USER_ID:
        await callback.answer()
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
    await callback.message.answer(f"✅ Заказ #{order_id} отправлен админу!")
    await callback.answer()

@router.callback_query(F.data.startswith("not_paid_"))
async def handle_not_paid(callback: CallbackQuery):
    if callback.from_user.id == ADMIN_USER_ID:
        await callback.answer()
        return
    order_id = int(callback.data.split("_")[1])
    await callback.message.answer(
        f"Заказ #{order_id} сохранён в статусе «ожидает оплаты».\n"
        f"Когда оплатите — напишите: /pay_order {order_id}"
    )
    await callback.answer()

@router.message(Command("pay_order"))
async def pay_order_later(message: Message):
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
    await message.answer(f"✅ Заказ #{order_id} отправлен админу!")

# === Мои заказы ===
@router.message(Command("myorders"))
async def my_orders(message: Message):
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
        title = (o['title'][:30] + "...") if len(o['title']) > 30 else o['title']
        text += f"{status_icon} #{o['id']} — {title}\n"
    await message.answer(text)

@router.message(Command("mystats"))
async def my_stats(message: Message):
    total = await get_user_total_spent(message.from_user.id)
    await message.answer(f"💰 Всего потрачено: {total:.2f} ₽")

@router.message(Command("adminstats"))
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    total = await get_admin_total_earned()
    await message.answer(f"💼 Ваш заработок: {total:.2f} ₽")

# === Админ-обработчики ===
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
        [InlineKeyboardButton(text="💬 Цена", callback_data=f"change_price_{order_id}")],
        [InlineKeyboardButton(text="✏️ Коммент", callback_data=f"comment_{order_id}")],
        [InlineKeyboardButton(text="✅ Завершить", callback_data=f"complete_{order_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_del_{order_id}")]
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
    await update_order(order_id, status="cancelled")
    await safe_send_message(
        (await get_order(order_id))["user_id"],
        f"❌ Ваш заказ #{order_id} отменён.\nХотите создать новый? /neworder"
    )
    await callback.message.edit_text("🗑 Игнорировано.")
    await callback.answer()

# --- Остальные обработчики (без изменений) ---
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
            [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_price_{order_id}")],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_price_{order_id}")]
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
        [InlineKeyboardButton(text=f"✅ Да, +{final_price:.2f} ₽", callback_data=f"confirm_earn_{order_id}")],
        [InlineKeyboardButton(text="❌ Нет", callback_data=f"skip_earn_{order_id}")]
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
    await callback.message.edit_text(f"✅ Сумма записана в заработок.")
    await callback.answer()

@router.callback_query(F.data.startswith("skip_earn_"))
async def skip_earning(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID: return
    await callback.message.edit_text("❌ Сумма НЕ записана в заработок.")
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
    logger.info("Бот запущен с ТЗ, оплатой и лимитом 1 млрд ₽.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())