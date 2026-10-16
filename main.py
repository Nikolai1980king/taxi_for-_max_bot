import asyncio
import json
import logging
import math
import os
import uuid
from collections import deque
from datetime import datetime

# ВАЖНО: системные сертификаты до импорта httpx
os.environ["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"
os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"

import httpx
from dotenv import load_dotenv

load_dotenv()

from maxapi import Bot, Dispatcher
from maxapi.types import MessageCreated, BotStarted, MessageCallback
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types.attachments.buttons.callback_button import CallbackButton

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("MAX_BOT_TOKEN")
if not TOKEN:
    raise ValueError("Токен не найден. Создайте файл .env с MAX_BOT_TOKEN=...")

YANDEX_GEOCODER_KEY = os.environ.get("YANDEX_GEOCODER_KEY")
if not YANDEX_GEOCODER_KEY:
    raise ValueError("Ключ Яндекс.Геокодера не найден. Добавьте YANDEX_GEOCODER_KEY в .env")

ADMIN_CHAT_ID = 21481223

USERS_FILE = "users.json"
ORDERS_FILE = "orders.json"
STATES_FILE = "states.json"
DRIVERS_FILE = "drivers.json"
QUEUE_FILE = "queue.json"

ORDER_TIMEOUT = 60

# ===================== ТАРИФЫ =====================

RATE_PER_KM = 30       # ₽ за км
MIN_PRICE = 100        # минимальная цена
ROAD_FACTOR = 1.4      # поправка на дороги

bot = Bot(TOKEN)
dp = Dispatcher()

# Защита от дублей callback'ов (MAX иногда шлёт одно событие дважды)
_processed_callbacks = deque(maxlen=500)


# ===================== ФАЙЛЫ =====================

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===================== ГЕОКОДИРОВАНИЕ И РАСЧЁТ =====================

async def geocode_address(addr: dict):
    """Превращает адрес в координаты (lat, lon). None, если не найдено."""
    query = f"{addr.get('city', '')}, улица {addr.get('street', '')}, дом {addr.get('house', '')}"
    logging.info(f"[GEO] Запрос: {query}")
    url = "https://geocode-maps.yandex.ru/1.x/"
    params = {
        "apikey": YANDEX_GEOCODER_KEY,
        "geocode": query,
        "format": "json",
        "results": 1,
    }
    try:
        async with httpx.AsyncClient(
            timeout=10,
            verify="/etc/ssl/certs/ca-certificates.crt",
        ) as client:
            r = await client.get(url, params=params)
            logging.info(f"[GEO] HTTP {r.status_code}")
            data = r.json()
        members = data["response"]["GeoObjectCollection"]["featureMember"]
        if not members:
            logging.warning(f"[GEO] Не найдено: {query}")
            return None
        pos = members[0]["GeoObject"]["Point"]["pos"]
        lon_str, lat_str = pos.split(" ")
        logging.info(f"[GEO] OK: lat={lat_str}, lon={lon_str}")
        return float(lat_str), float(lon_str)
    except Exception as e:
        logging.error(f"[GEO] Ошибка: {type(e).__name__}: {e}")
        return None


def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return R * c


async def calculate_trip(from_addr: dict, to_addr: dict):
    """Возвращает (distance_km, price) или (None, None)."""
    logging.info("[GEO] Старт расчёта")
    coords_from = await geocode_address(from_addr)
    if not coords_from:
        return None, None

    coords_to = await geocode_address(to_addr)
    if not coords_to:
        return None, None

    direct_km = haversine(
        coords_from[0], coords_from[1],
        coords_to[0], coords_to[1],
    )
    road_km = direct_km * ROAD_FACTOR
    price = max(MIN_PRICE, round(road_km * RATE_PER_KM))
    logging.info(f"[GEO] Прямо: {direct_km:.2f} км, дорога: {road_km:.2f} км, цена: {price} ₽")
    return round(road_km, 1), price


# ===================== ПОЛЬЗОВАТЕЛИ / РОЛИ =====================

def get_role(user_id: int):
    users = load_json(USERS_FILE, {})
    return users.get(str(user_id), {}).get("role")


def set_role(user_id: int, role: str):
    users = load_json(USERS_FILE, {})
    users[str(user_id)] = {"role": role, "since": datetime.now().isoformat()}
    save_json(USERS_FILE, users)


def clear_role(user_id: int):
    users = load_json(USERS_FILE, {})
    users.pop(str(user_id), None)
    save_json(USERS_FILE, users)


def reset_user(user_id: int):
    users = load_json(USERS_FILE, {})
    users.pop(str(user_id), None)
    save_json(USERS_FILE, users)
    states = load_json(STATES_FILE, {})
    states.pop(str(user_id), None)
    save_json(STATES_FILE, states)
    drivers = load_json(DRIVERS_FILE, {})
    drivers.pop(str(user_id), None)
    save_json(DRIVERS_FILE, drivers)
    q = load_json(QUEUE_FILE, [])
    if str(user_id) in q:
        q.remove(str(user_id))
        save_json(QUEUE_FILE, q)


# ===================== СОСТОЯНИЯ =====================

def get_state(user_id: int) -> dict:
    states = load_json(STATES_FILE, {})
    return states.get(str(user_id), {})


def set_state(user_id: int, state: dict):
    states = load_json(STATES_FILE, {})
    if state:
        states[str(user_id)] = state
    else:
        states.pop(str(user_id), None)
    save_json(STATES_FILE, states)


# ===================== ВОДИТЕЛИ =====================

def get_driver(user_id: int):
    drivers = load_json(DRIVERS_FILE, {})
    return drivers.get(str(user_id))


def save_driver(user_id: int, data: dict):
    drivers = load_json(DRIVERS_FILE, {})
    drivers[str(user_id)] = data
    save_json(DRIVERS_FILE, drivers)


# ===================== ОЧЕРЕДЬ =====================

def queue_load() -> list:
    return load_json(QUEUE_FILE, [])


def queue_save(q: list):
    save_json(QUEUE_FILE, q)


def queue_add(user_id: int):
    q = queue_load()
    uid = str(user_id)
    if uid not in q:
        q.append(uid)
        queue_save(q)
        logging.info(f"Очередь: {uid} добавлен. Текущая очередь: {q}")
    return q


def queue_remove(user_id: int):
    q = queue_load()
    uid = str(user_id)
    if uid in q:
        q.remove(uid)
        queue_save(q)
        logging.info(f"Очередь: {uid} удалён. Текущая очередь: {q}")
    return q


def queue_move_to_end(user_id: int):
    q = queue_load()
    uid = str(user_id)
    if uid in q:
        q.remove(uid)
    q.append(uid)
    queue_save(q)
    logging.info(f"Очередь: {uid} в конец. Текущая очередь: {q}")
    return q


def queue_count() -> int:
    return len(queue_load())


def queue_position(user_id: int) -> int:
    q = queue_load()
    uid = str(user_id)
    if uid in q:
        return q.index(uid) + 1
    return 0


def drivers_online_count() -> int:
    drivers = load_json(DRIVERS_FILE, {})
    return sum(1 for d in drivers.values() if d.get("status") == "online")


# ===================== ЗАКАЗЫ =====================

def orders_load() -> list:
    return load_json(ORDERS_FILE, [])


def orders_save(orders: list):
    save_json(ORDERS_FILE, orders)


def save_order(order: dict):
    orders = orders_load()
    orders.append(order)
    orders_save(orders)


def find_order(order_id: str):
    orders = orders_load()
    for o in orders:
        if o.get("id") == order_id:
            return o
    return None


def update_order(order_id: str, **fields):
    orders = orders_load()
    for o in orders:
        if o.get("id") == order_id:
            o.update(fields)
            break
    orders_save(orders)


# ===================== ФОРМАТИРОВАНИЕ =====================

def format_address(addr: dict) -> str:
    parts = []
    if addr.get("city"):
        parts.append(addr["city"])
    if addr.get("street"):
        parts.append(f"ул. {addr['street']}")
    if addr.get("house"):
        parts.append(f"д. {addr['house']}")
    return ", ".join(parts) if parts else "—"


def format_address_full(addr: dict) -> str:
    base = format_address(addr)
    comment = addr.get("comment", "").strip()
    if comment:
        return f"{base}\n💬 {comment}"
    return base


# ===================== КЛАВИАТУРЫ =====================

def kb_role_choice():
    b = InlineKeyboardBuilder()
    b.add(
        CallbackButton(text="Я пассажир", payload="role_passenger"),
        CallbackButton(text="Я водитель", payload="role_driver"),
    )
    return b.as_markup()


def kb_passenger_menu():
    b = InlineKeyboardBuilder()
    b.add(
        CallbackButton(text="Заказать такси", payload="passenger_order"),
        CallbackButton(text="Мои заказы", payload="passenger_history"),
    )
    b.add(CallbackButton(text="🚕 Машины на линии", payload="passenger_cars_online"))
    b.add(
        CallbackButton(text="🔄 Сменить роль", payload="change_role"),
        CallbackButton(text="🗑 Сбросить всё", payload="full_reset"),
    )
    return b.as_markup()


def kb_driver_menu():
    b = InlineKeyboardBuilder()
    b.add(
        CallbackButton(text="Выйти на линию", payload="driver_online"),
        CallbackButton(text="Уйти с линии", payload="driver_offline"),
    )
    b.add(CallbackButton(text="📊 Место в очереди", payload="driver_queue_info"))
    b.add(
        CallbackButton(text="🔄 Сменить роль", payload="change_role"),
        CallbackButton(text="🗑 Сбросить всё", payload="full_reset"),
    )
    return b.as_markup()


def kb_skip_optional():
    b = InlineKeyboardBuilder()
    b.add(CallbackButton(text="⏭ Пропустить", payload="skip_optional"))
    return b.as_markup()


def kb_confirm_order():
    b = InlineKeyboardBuilder()
    b.add(
        CallbackButton(text="✅ Подтвердить", payload="order_confirm"),
        CallbackButton(text="❌ Отменить", payload="order_cancel"),
    )
    return b.as_markup()


def kb_passenger_active_order(order_id: str):
    b = InlineKeyboardBuilder()
    b.add(CallbackButton(text="❌ Отменить заказ", payload=f"passenger_cancel:{order_id}"))
    return b.as_markup()


def kb_driver_offer(order_id: str):
    b = InlineKeyboardBuilder()
    b.add(
        CallbackButton(text="✅ Принять", payload=f"offer_accept:{order_id}"),
        CallbackButton(text="❌ Отказаться", payload=f"offer_decline:{order_id}"),
    )
    return b.as_markup()


def kb_driver_on_way(order_id: str):
    b = InlineKeyboardBuilder()
    b.add(
        CallbackButton(text="📍 Я на месте", payload=f"trip_arrived:{order_id}"),
        CallbackButton(text="❌ Отменить заказ", payload=f"trip_cancel:{order_id}"),
    )
    return b.as_markup()


def kb_driver_waiting(order_id: str):
    b = InlineKeyboardBuilder()
    b.add(
        CallbackButton(text="🚗 Поехали", payload=f"trip_start:{order_id}"),
        CallbackButton(text="❌ Отменить заказ", payload=f"trip_cancel:{order_id}"),
    )
    return b.as_markup()


def kb_driver_in_trip(order_id: str):
    b = InlineKeyboardBuilder()
    b.add(CallbackButton(text="🏁 Завершить поездку", payload=f"trip_finish:{order_id}"))
    return b.as_markup()


# ===================== ОТПРАВКА ЗАКАЗА ВОДИТЕЛЮ =====================

async def offer_order_to_driver(order_id: str, driver_id: str):
    order = find_order(order_id)
    driver = get_driver(int(driver_id))

    if not order or not driver:
        logging.warning(f"offer: заказ {order_id} или водитель {driver_id} не найдены")
        return

    from_text = format_address_full(order["from"])
    to_text = format_address_full(order["to"])
    phone = order.get("phone", "—") or "—"
    distance = order.get("distance_km", "—")
    price = order.get("price", "—")

    text = (
        f"🚕 Новый заказ!\n\n"
        f"📍 Откуда:\n{from_text}\n\n"
        f"🏁 Куда:\n{to_text}\n\n"
        f"📏 Расстояние: {distance} км\n"
        f"💰 Цена: {price} ₽\n"
        f"📞 Телефон пассажира: {phone}\n\n"
        f"⏱ У вас {ORDER_TIMEOUT} секунд, чтобы принять."
    )

    try:
        await bot.send_message(
            user_id=int(driver_id),
            text=text,
            attachments=[kb_driver_offer(order_id)],
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить заказ водителю {driver_id}: {e}")
        queue_move_to_end(int(driver_id))
        await try_next_driver(order_id)
        return

    update_order(order_id, status="offered", offered_to=driver_id)

    async def timeout_task():
        await asyncio.sleep(ORDER_TIMEOUT)
        current = find_order(order_id)
        if current and current.get("status") == "offered" and current.get("offered_to") == driver_id:
            logging.info(f"⏰ Таймер истёк для водителя {driver_id}")
            try:
                await bot.send_message(
                    user_id=int(driver_id),
                    text="⏰ Время истекло. Заказ передан следующему водителю.",
                )
            except Exception:
                pass
            queue_move_to_end(int(driver_id))
            await try_next_driver(order_id)

    asyncio.create_task(timeout_task())


async def try_next_driver(order_id: str):
    order = find_order(order_id)
    if not order or order.get("status") in ("accepted", "arrived", "in_trip", "finished", "cancelled", "cancelled_by_driver", "cancelled_by_passenger"):
        return

    q = queue_load()
    if not q:
        update_order(order_id, status="waiting")
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"⚠️ Нет свободных водителей!\n\n"
                    f"Заказ {order_id}:\n"
                    f"Откуда: {format_address_full(order['from'])}\n"
                    f"Куда: {format_address_full(order['to'])}\n"
                    f"Телефон: {order.get('phone', '—')}\n"
                    f"Цена: {order.get('price', '—')} ₽"
                ),
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить админа: {e}")
        return

    next_driver = q[0]
    update_order(order_id, status="offered", offered_to=next_driver)
    await offer_order_to_driver(order_id, next_driver)


# ===================== ОБРАБОТЧИКИ =====================

@dp.bot_started()
async def on_bot_started(event: BotStarted):
    user_id = event.user.user_id
    chat_id = event.chat_id
    role = get_role(user_id)

    if role == "passenger":
        await bot.send_message(
            chat_id=chat_id,
            text="С возвращением! Вы пассажир. Выберите действие:",
            attachments=[kb_passenger_menu()],
        )
    elif role == "driver":
        driver = get_driver(user_id)
        if not driver:
            set_state(user_id, {"step": "driver_reg", "data": {}})
            await bot.send_message(
                chat_id=chat_id,
                text="Похоже, вы ещё не зарегистрированы как водитель.\n\nВведите ваше имя:",
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"С возвращением, {driver['name']}! Выберите действие:",
                attachments=[kb_driver_menu()],
            )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text="Привет! Это сервис такси «По Южному».\n\nКто вы?",
            attachments=[kb_role_choice()],
        )


@dp.message_created()
async def on_message(event: MessageCreated):
    user_id = event.message.sender.user_id
    text = (event.message.body.text or "").strip()
    logging.info(f"📩 {user_id}: {text}")

    if text == "/reset":
        reset_user(user_id)
        await event.message.answer("🔄 Полный сброс выполнен.", attachments=[kb_role_choice()])
        return

    if text == "/start":
        role = get_role(user_id)
        if role == "passenger":
            await event.message.answer(
                "С возвращением! Вы пассажир. Выберите действие:",
                attachments=[kb_passenger_menu()],
            )
        elif role == "driver":
            driver = get_driver(user_id)
            if not driver:
                set_state(user_id, {"step": "driver_reg", "data": {}})
                await event.message.answer("Введите ваше имя:")
            else:
                await event.message.answer(
                    f"С возвращением, {driver['name']}! Выберите действие:",
                    attachments=[kb_driver_menu()],
                )
        else:
            await event.message.answer(
                "Привет! Это сервис такси «По Южному».\n\nКто вы?",
                attachments=[kb_role_choice()],
            )
        return

    state = get_state(user_id)
    step = state.get("step")

    if step == "order":
        await handle_order_step(event, user_id, text, state)
        return

    if step == "driver_reg":
        await handle_driver_reg(event, user_id, text, state)
        return

    if text.lower() in ("пассажир", "1"):
        set_role(user_id, "passenger")
        await event.message.answer("Отлично! Вы пассажир.", attachments=[kb_passenger_menu()])
        return
    if text.lower() in ("водитель", "2"):
        set_role(user_id, "driver")
        set_state(user_id, {"step": "driver_reg", "data": {}})
        await event.message.answer("Отлично! Вы водитель.\n\nВведите ваше имя:")
        return

    role = get_role(user_id)
    if role == "passenger":
        await event.message.answer("Вы пассажир. Выберите действие:", attachments=[kb_passenger_menu()])
    elif role == "driver":
        driver = get_driver(user_id)
        if not driver:
            set_state(user_id, {"step": "driver_reg", "data": {}})
            await event.message.answer("Введите ваше имя:")
        else:
            await event.message.answer(
                f"Вы водитель, {driver['name']}. Выберите действие:",
                attachments=[kb_driver_menu()],
            )
    else:
        await event.message.answer(
            "Привет! Это сервис такси «По Южному».\n\nКто вы?",
            attachments=[kb_role_choice()],
        )


# ===================== ВВОД АДРЕСА =====================

ORDER_STEPS = [
    ("from", "city", "📍 ОТКУДА\n\n🏙 Введите населённый пункт (город / станица / посёлок / хутор):"),
    ("from", "street", "📍 ОТКУДА\n\n🛣 Введите улицу:"),
    ("from", "house", "📍 ОТКУДА\n\n🏠 Введите номер дома:"),
    ("from", "phone", "📍 ОТКУДА\n\n📞 Введите ваш телефон для связи\n\nИли нажмите «Пропустить»:"),
    ("from", "comment", "📍 ОТКУДА\n\n💬 Комментарий для водителя (подъезд, как найти, и т.п.)\n\nИли нажмите «Пропустить»:"),
    ("to", "city", "🏁 КУДА\n\n🏙 Введите населённый пункт (город / станица / посёлок / хутор):"),
    ("to", "street", "🏁 КУДА\n\n🛣 Введите улицу:"),
    ("to", "house", "🏁 КУДА\n\n🏠 Введите номер дома:"),
    ("to", "comment", "🏁 КУДА\n\n💬 Комментарий для водителя (подъезд, как найти, и т.п.)\n\nИли нажмите «Пропустить»:"),
]


def get_current_step(state: dict):
    data = state.get("data", {})
    for addr_key, field, question in ORDER_STEPS:
        addr = data.get(addr_key, {})
        if field not in addr:
            return addr_key, field, question
    return None


async def send_next_step(chat_answer, data: dict):
    """Отправляет следующий шаг (или сводку). chat_answer — метод .answer или .send."""
    next_step = get_current_step({"data": data})
    if next_step is None:
        return False
    _, next_field, next_question = next_step
    kb = kb_skip_optional() if next_field in ("comment", "phone") else None
    await chat_answer(next_question, attachments=[kb] if kb else None)
    return True


async def handle_order_step(event: MessageCreated, user_id: int, text: str, state: dict):
    data = state.get("data", {})

    current = get_current_step(state)
    if current is None:
        await show_order_summary(event, user_id, data)
        return

    addr_key, field, question = current

    if field in ("city", "street", "house"):
        if not text or text.lower() in ("пропустить", "skip", "/skip"):
            await event.message.answer(
                "❌ Это поле обязательно. Пожалуйста, введите значение:\n\n" + question
            )
            return

    if addr_key not in data:
        data[addr_key] = {}
    data[addr_key][field] = text
    set_state(user_id, {"step": "order", "data": data})

    # Следующий шаг или сводка
    if get_current_step({"data": data}) is None:
        await show_order_summary(event, user_id, data)
        return

    await send_next_step(event.message.answer, data)


async def show_order_summary(event, user_id: int, data: dict):
    """
    Универсальная функция. Работает и с MessageCreated, и с MessageCallback.
    """
    # Определяем, как отправлять сообщение
    if hasattr(event, "message") and event.message is not None:
        send_func = event.message.answer
    else:
        send_func = event.send

    from_addr = data.get("from", {})
    to_addr = data.get("to", {})
    from_text = format_address_full(from_addr)
    to_text = format_address_full(to_addr)
    phone = from_addr.get("phone", "").strip() or "—"

    await send_func("🔎 Считаю расстояние и цену, подождите...")

    distance, price = await calculate_trip(from_addr, to_addr)

    if distance is None:
        await send_func(
            "⚠️ Не удалось определить адрес на карте.\n\n"
            "Проверьте, правильно ли указаны:\n"
            "• населённый пункт\n"
            "• улица\n"
            "• номер дома\n\n"
            "Начните заказ заново: /start → «Заказать такси».",
            attachments=[kb_passenger_menu()],
        )
        set_state(user_id, {})
        return

    data["distance_km"] = distance
    data["price"] = price
    set_state(user_id, {"step": "order", "data": data})

    await send_func(
        f"📋 Проверьте ваш заказ:\n\n"
        f"📍 ОТКУДА:\n{from_text}\n\n"
        f"🏁 КУДА:\n{to_text}\n\n"
        f"📏 Расстояние: {distance} км\n"
        f"💰 Цена: {price} ₽\n"
        f"📞 Телефон: {phone}\n\n"
        f"Всё верно?",
        attachments=[kb_confirm_order()],
    )


async def handle_driver_reg(event: MessageCreated, user_id: int, text: str, state: dict):
    data = state.get("data", {})

    if "name" not in data:
        data["name"] = text
        set_state(user_id, {"step": "driver_reg", "data": data})
        await event.message.answer(f"Имя: {text}\n\nВведите марку и цвет машины:")
        return

    if "car" not in data:
        data["car"] = text
        set_state(user_id, {"step": "driver_reg", "data": data})
        await event.message.answer(f"Машина: {text}\n\nВведите госномер:")
        return

    if "plate" not in data:
        data["plate"] = text
        set_state(user_id, {"step": "driver_reg", "data": data})
        await event.message.answer(f"Госномер: {text}\n\nВведите ваш телефон:")
        return

    if "phone" not in data:
        data["phone"] = text
        save_driver(user_id, {
            "name": data["name"],
            "car": data["car"],
            "plate": data["plate"],
            "phone": data["phone"],
            "status": "offline",
            "registered_at": datetime.now().isoformat(),
        })
        set_state(user_id, {})
        await event.message.answer(
            f"✅ Регистрация завершена!\n\n"
            f"Имя: {data['name']}\n"
            f"Машина: {data['car']}\n"
            f"Госномер: {data['plate']}\n"
            f"Телефон: {data['phone']}\n\n"
            f"Чтобы начать получать заказы, нажмите «Выйти на линию».",
            attachments=[kb_driver_menu()],
        )
        return


@dp.message_callback()
async def on_callback(event: MessageCallback):
    user_id = event.callback.user.user_id
    payload = event.callback.payload or ""
    callback_id = event.callback.callback_id

    # Защита от дублей — MAX иногда присылает один callback дважды
    if callback_id in _processed_callbacks:
        logging.info(f"⏭ Дубль callback {callback_id} — игнорирую")
        return
    _processed_callbacks.append(callback_id)

    logging.info(f"🔘 callback: user_id={user_id}, payload={payload}")

    # ===== Пропустить опциональное =====
    if payload == "skip_optional":
        state = get_state(user_id)
        data = state.get("data", {})
        current = get_current_step(state)
        if current is None:
            await event.send(text="Все данные уже собраны.")
            return
        addr_key, field, _ = current
        if field not in ("comment", "phone"):
            await event.send(text="Это поле обязательно, его нельзя пропустить.")
            return
        if addr_key not in data:
            data[addr_key] = {}
        data[addr_key][field] = ""
        set_state(user_id, {"step": "order", "data": data})

        if get_current_step({"data": data}) is None:
            await show_order_summary(event, user_id, data)
            return
        await send_next_step(event.send, data)
        return

    if payload == "change_role":
        clear_role(user_id)
        set_state(user_id, {})
        await event.send(text="Роль сброшена. Кто вы?", attachments=[kb_role_choice()])
        return

    if payload == "full_reset":
        reset_user(user_id)
        await event.send(text="🗑 Полный сброс выполнен.\n\nКто вы?", attachments=[kb_role_choice()])
        return

    if payload == "role_passenger":
        set_role(user_id, "passenger")
        await event.send(text="Отлично! Вы пассажир. Выберите действие:", attachments=[kb_passenger_menu()])
        return

    if payload == "role_driver":
        set_role(user_id, "driver")
        driver = get_driver(user_id)
        if driver:
            await event.send(
                text=f"С возвращением, {driver['name']}! Выберите действие:",
                attachments=[kb_driver_menu()],
            )
        else:
            set_state(user_id, {"step": "driver_reg", "data": {}})
            await event.send(text="Введите ваше имя:")
        return

    if payload == "passenger_order":
        free = queue_count()
        if free == 0:
            info = "⚠️ Свободных машин сейчас нет, но мы поищем для вас."
        else:
            info = f"🚕 Свободных машин на линии: {free}."
        set_state(user_id, {"step": "order", "data": {}})
        first_question = ORDER_STEPS[0][2]
        await event.send(text=f"{info}\n\n{first_question}")
        return

    if payload == "passenger_history":
        orders = orders_load()
        my = [o for o in orders if o.get("user_id") == user_id]
        if not my:
            await event.send(text="У вас пока нет заказов.", attachments=[kb_passenger_menu()])
        else:
            text = "Ваши последние заказы:\n\n"
            for o in my[-5:]:
                from_text = format_address(o.get("from", {}))
                to_text = format_address(o.get("to", {}))
                price = o.get("price", "—")
                text += f"• {from_text} → {to_text} — {price} ₽ ({o.get('status')})\n"
            await event.send(text=text, attachments=[kb_passenger_menu()])
        return

    if payload == "passenger_cars_online":
        count = drivers_online_count()
        free = queue_count()
        if count == 0:
            text = "Сейчас на линии нет машин. Попробуйте позже."
        else:
            text = (
                f"🚕 Сейчас на линии: {count} машин.\n"
                f"Свободны и ждут заказ: {free}.\n\n"
                f"Заказывайте — мы найдём ближайшую!"
            )
        await event.send(text=text, attachments=[kb_passenger_menu()])
        return

    if payload == "order_confirm":
        state = get_state(user_id)
        data = state.get("data", {})
        from_addr = data.get("from", {})
        to_addr = data.get("to", {})
        distance = data.get("distance_km")
        price = data.get("price")

        if not from_addr.get("city") or not to_addr.get("city") or distance is None:
            await event.send(text="Ошибка: данные заказа потеряны. Начните заново.", attachments=[kb_passenger_menu()])
            set_state(user_id, {})
            return

        phone = from_addr.get("phone", "").strip()
        phone_text = phone if phone else "—"

        order_id = "order_" + uuid.uuid4().hex[:8]
        order = {
            "id": order_id,
            "user_id": user_id,
            "from": from_addr,
            "to": to_addr,
            "phone": phone_text,
            "distance_km": distance,
            "price": price,
            "status": "new",
            "driver_id": None,
            "created_at": datetime.now().isoformat(),
        }
        save_order(order)
        set_state(user_id, {})

        await event.send(
            text=(
                f"✅ Заказ принят!\n\n"
                f"📍 Откуда: {format_address(from_addr)}\n"
                f"🏁 Куда: {format_address(to_addr)}\n"
                f"📏 Расстояние: {distance} км\n"
                f"💰 Цена: {price} ₽\n"
                f"📞 Телефон: {phone_text}\n\n"
                f"Ищем для вас машину..."
            ),
        )

        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"🚕 Новый заказ {order_id}\n\n"
                    f"Откуда: {format_address_full(from_addr)}\n"
                    f"Куда: {format_address_full(to_addr)}\n"
                    f"Расстояние: {distance} км\n"
                    f"Цена: {price} ₽\n"
                    f"Телефон: {phone_text}\n"
                    f"Пассажир: {user_id}"
                ),
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить админа: {e}")

        await try_next_driver(order_id)
        return

    if payload == "order_cancel":
        set_state(user_id, {})
        await event.send(
            text="Заказ отменён. Возвращайтесь, когда понадобится такси.",
            attachments=[kb_passenger_menu()],
        )
        return

    if payload.startswith("passenger_cancel:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("user_id") != user_id:
            await event.send(text="Этот заказ не ваш.")
            return
        if order.get("status") in ("finished", "cancelled", "cancelled_by_driver", "cancelled_by_passenger"):
            await event.send(text="Заказ уже завершён или отменён.")
            return

        update_order(order_id, status="cancelled_by_passenger",
                     cancelled_at=datetime.now().isoformat())

        driver_id = order.get("driver_id")
        if driver_id:
            drv = get_driver(int(driver_id))
            if drv and drv.get("status") == "online":
                queue_move_to_end(int(driver_id))
            try:
                await bot.send_message(
                    user_id=int(driver_id),
                    text="⚠️ Пассажир отменил заказ. Возвращаетесь в конец очереди.",
                    attachments=[kb_driver_menu()],
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить водителя: {e}")

        await event.send(
            text="❌ Заказ отменён. Возвращайтесь, когда понадобится такси.",
            attachments=[kb_passenger_menu()],
        )
        return

    if payload == "driver_online":
        driver = get_driver(user_id)
        if not driver:
            await event.send(text="Сначала зарегистрируйтесь.")
            return
        driver["status"] = "online"
        save_driver(user_id, driver)
        queue_add(user_id)
        position = queue_position(user_id)
        total = queue_count()
        await event.send(text=f"✅ Вы на линии.\n\nВаше место в очереди: {position} из {total}.")
        return

    if payload == "driver_offline":
        driver = get_driver(user_id)
        if driver:
            driver["status"] = "offline"
            save_driver(user_id, driver)
        queue_remove(user_id)
        await event.send(text="Вы ушли с линии.")
        return

    if payload == "driver_queue_info":
        driver = get_driver(user_id)
        if not driver:
            await event.send(text="Сначала зарегистрируйтесь.")
            return
        if driver.get("status") != "online":
            await event.send(
                text="Вы сейчас не на линии.\nНажмите «Выйти на линию», чтобы начать получать заказы.",
                attachments=[kb_driver_menu()],
            )
            return
        position = queue_position(user_id)
        total = queue_count()
        if position == 0:
            await event.send(text="Вы сейчас выполняете заказ.\nПосле завершения вернётесь в конец очереди.")
            return
        text = f"📊 Вы на линии.\n\nВаше место в очереди: {position} из {total}.\n"
        if position == 1:
            text += "\nВы первый — следующий заказ ваш! 🎯"
        else:
            text += f"\nПеред вами: {position - 1}."
        await event.send(text=text)
        return

    if payload.startswith("offer_accept:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order:
            await event.send(text="Заказ уже недоступен.")
            return
        if order.get("status") != "offered" or order.get("offered_to") != str(user_id):
            await event.send(text="Этот заказ уже не для вас.")
            return

        driver = get_driver(user_id)
        update_order(order_id, status="accepted", driver_id=str(user_id))
        queue_remove(user_id)

        from_text = format_address_full(order["from"])
        to_text = format_address_full(order["to"])
        phone = order.get("phone", "—") or "—"
        distance = order.get("distance_km", "—")
        price = order.get("price", "—")

        await event.send(
            text=(
                f"✅ Заказ принят! Езжайте к пассажиру.\n\n"
                f"📍 Откуда:\n{from_text}\n\n"
                f"🏁 Куда:\n{to_text}\n\n"
                f"📏 Расстояние: {distance} км\n"
                f"💰 Цена: {price} ₽\n"
                f"📞 Телефон пассажира: {phone}"
            ),
            attachments=[kb_driver_on_way(order_id)],
        )

        try:
            await bot.send_message(
                user_id=order["user_id"],
                text=(
                    f"🚕 Ваш водитель найден!\n\n"
                    f"Имя: {driver['name']}\n"
                    f"Машина: {driver['car']} ({driver['plate']})\n"
                    f"Телефон водителя: {driver['phone']}\n\n"
                    f"💰 Цена поездки: {price} ₽\n\n"
                    f"Ожидайте машину по адресу: {format_address(order['from'])}"
                ),
                attachments=[kb_passenger_active_order(order_id)],
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить пассажира: {e}")
        return

    if payload.startswith("offer_decline:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("status") != "offered":
            await event.send(text="Заказ уже недоступен.")
            return
        if order.get("offered_to") != str(user_id):
            await event.send(text="Этот заказ уже не для вас.")
            return
        queue_move_to_end(user_id)
        await event.send(text="Вы отказались. Заказ передан следующему водителю.")
        await try_next_driver(order_id)
        return

    if payload.startswith("trip_arrived:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("status") != "accepted" or order.get("driver_id") != str(user_id):
            await event.send(text="Этот заказ не ваш или уже не актуален.")
            return
        update_order(order_id, status="arrived", arrived_at=datetime.now().isoformat())
        await event.send(
            text=(
                f"📍 Вы на месте. Ждём пассажира.\n\n"
                f"Адрес: {format_address(order['from'])}\n"
                f"📞 Телефон пассажира: {order.get('phone', '—')}"
            ),
            attachments=[kb_driver_waiting(order_id)],
        )
        try:
            await bot.send_message(
                user_id=order["user_id"],
                text="📍 Ваш водитель на месте. Выходите!",
                attachments=[kb_passenger_active_order(order_id)],
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить пассажира: {e}")
        return

    if payload.startswith("trip_start:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("status") != "arrived" or order.get("driver_id") != str(user_id):
            await event.send(text="Этот заказ не ваш или уже не актуален.")
            return
        update_order(order_id, status="in_trip", started_at=datetime.now().isoformat())
        await event.send(
            text=(
                f"🚗 В путь!\n\n"
                f"🏁 Куда: {format_address(order['to'])}\n"
                f"💰 Цена: {order.get('price', '—')} ₽\n"
                f"📞 Телефон пассажира: {order.get('phone', '—')}"
            ),
            attachments=[kb_driver_in_trip(order_id)],
        )
        try:
            await bot.send_message(
                user_id=order["user_id"],
                text=f"🚗 Поехали! В пути до: {format_address(order['to'])}",
                attachments=[kb_passenger_active_order(order_id)],
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить пассажира: {e}")
        return

    if payload.startswith("trip_cancel:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("driver_id") != str(user_id):
            await event.send(text="Этот заказ не ваш.")
            return
        if order.get("status") not in ("accepted", "arrived"):
            await event.send(text="Заказ уже нельзя отменить на этом этапе.")
            return
        update_order(order_id, status="cancelled_by_driver",
                     cancelled_at=datetime.now().isoformat())
        queue_move_to_end(user_id)
        await event.send(
            text="❌ Заказ отменён. Вы в конце очереди.",
            attachments=[kb_driver_menu()],
        )
        try:
            await bot.send_message(
                user_id=order["user_id"],
                text="⚠️ К сожалению, водитель отменил заказ.\nМы ищем для вас другого водителя...",
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить пассажира: {e}")
        update_order(order_id, status="new", driver_id=None)
        await try_next_driver(order_id)
        return

    if payload.startswith("trip_finish:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if (not order or order.get("status") != "in_trip"
                or order.get("driver_id") != str(user_id)):
            await event.send(text="Этот заказ не ваш или уже завершён.")
            return
        update_order(order_id, status="finished", finished_at=datetime.now().isoformat())
        driver = get_driver(user_id)
        if driver and driver.get("status") == "online":
            queue_move_to_end(user_id)
        position = queue_position(user_id)
        total = queue_count()
        await event.send(
            text=f"🏁 Поездка завершена. Спасибо!\n\nВаше место в очереди: {position} из {total}.",
            attachments=[kb_driver_menu()],
        )
        try:
            await bot.send_message(
                user_id=order["user_id"],
                text="🏁 Ваша поездка завершена. Спасибо, что выбрали нас!",
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить пассажира: {e}")
        return

    await event.send(text=f"Команда {payload} пока не реализована.")


async def main():
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    logging.info("Бот запущен, слушаю события...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())