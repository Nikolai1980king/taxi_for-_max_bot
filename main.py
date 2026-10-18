import asyncio
import json
import logging
import math
import os
import uuid
from collections import deque
from datetime import datetime, timedelta

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
from maxapi.types.attachments.buttons.request_geo_location_button import RequestGeoLocationButton

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("MAX_BOT_TOKEN")
if not TOKEN:
    raise ValueError("Токен не найден. Создайте файл .env с MAX_BOT_TOKEN=...")

YANDEX_GEOCODER_KEY = os.environ.get("YANDEX_GEOCODER_KEY")
if not YANDEX_GEOCODER_KEY:
    raise ValueError("Ключ Яндекс.Геокодера не найден. Добавьте YANDEX_GEOCODER_KEY в .env")

ADMIN_CHAT_ID = 21481223
ADMIN_USER_ID = 509245862

USERS_FILE = "users.json"
ORDERS_FILE = "orders.json"
STATES_FILE = "states.json"
DRIVERS_FILE = "drivers.json"
QUEUE_FILE = "queue.json"
EVENTS_FILE = "events.jsonl"
STATS_FILE = "stats.json"

ORDER_TIMEOUT = 60

RATE_PER_KM_TAXI = 40
RATE_PER_KM_DELIVERY = 43
MIN_PRICE_TAXI = 100
MIN_PRICE_DELIVERY = 150
ROAD_FACTOR = 1.4

RADIUS_STEPS = [6, 10, 15, 20, 25, 30, 35, 40, 45, 50]
LOCATION_TTL_MINUTES = 30
LOCATION_REMIND_INTERVAL = 30

bot = Bot(TOKEN)
dp = Dispatcher()

_processed_callbacks = deque(maxlen=500)
_reminder_tasks = {}


# ===================== ТЕКСТЫ ИНСТРУКЦИЙ =====================

HELP_MAIN = (
    "❓ Инструкция по использованию бота\n\n"
    "Выберите раздел:"
)

HELP_DRIVER = (
    "🚗 Инструкция для водителя / доставщика\n\n"
    "1️⃣ Зарегистрируйтесь: имя, марка машины, госномер, телефон.\n"
    "2️⃣ Нажмите «Выйти на линию».\n"
    "3️⃣ Отправьте геопозицию — обязательно, иначе не получите заказы.\n"
    "4️⃣ При заказе нажмите «✅ Принять» или «❌ Отказаться».\n"
    "5️⃣ Ведите поездку: «📍 Я на месте» → «🚗 Поехали» → «🏁 Завершить».\n\n"
    "Вы получаете и заказы такси, и заказы доставки.\n\n"
    "Подробнее — в разделах ниже:"
)

HELP_DRIVER_GEO = (
    "📍 Геопозиция — самое важное\n\n"
    "Без свежих координат вы не получите заказы.\n\n"
    "Как отправить:\n"
    "1. В меню водителя нажмите «📍 Геопозиция».\n"
    "2. Нажмите «📍 Отправить мою геопозицию».\n"
    "3. Разрешите доступ к геолокации.\n\n"
    "⚠️ Отправляйте с телефона — в браузере может не работать.\n\n"
    "🔄 Обновляйте каждые 30 минут — бот напомнит.\n\n"
    "Координаты старше 30 минут считаются устаревшими. Вы попадёте в резервный список и получите заказ, только если рядом никого не найдётся."
)

HELP_DRIVER_ORDERS = (
    "🚕 Как принимать заказы\n\n"
    "Когда клиент рядом (в радиусе 6 км) — вам придёт заказ:\n"
    "• Адрес «Откуда» и «Куда»\n"
    "• Расстояние до клиента\n"
    "• Длина поездки и цена\n"
    "• Телефон клиента\n\n"
    "У вас 60 секунд, чтобы:\n"
    "• Нажать «✅ Принять» — заказ ваш.\n"
    "• Нажать «❌ Отказаться» — заказ уйдёт следующему.\n\n"
    "Вы получаете и заказы такси (🚕), и заказы доставки (📦)."
)

HELP_DRIVER_TRIP = (
    "🏁 Как вести поездку\n\n"
    "Такси:\n"
    "1. Приняли заказ — едете к пассажиру.\n"
    "2. Приехали — «📍 Я на месте».\n"
    "3. Пассажир сел — «🚗 Поехали».\n"
    "4. Привезли — «🏁 Завершить поездку».\n\n"
    "Доставка:\n"
    "1. Приняли заказ — едете забирать посылку.\n"
    "2. Забрали — «📦 Забрал».\n"
    "3. Доставили — «🏁 Доставлено».\n\n"
    "После завершения вы снова участвуете в подборе."
)

HELP_PASSENGER = (
    "🧑 Инструкция для пассажира\n\n"
    "1️⃣ Нажмите «Заказать такси».\n"
    "2️⃣ Введите адрес «Откуда» — куда подать машину.\n"
    "3️⃣ Введите адрес «Куда» — куда ехать.\n"
    "4️⃣ Укажите телефон для связи (или пропустите).\n"
    "5️⃣ Подтвердите заказ — увидите цену и расстояние.\n"
    "6️⃣ Ждите водителя — придёт уведомление с данными машины.\n\n"
    "📍 Если адрес не распознаётся — проверьте написание: «город, улица, дом».\n\n"
    "❌ Отменить заказ можно кнопкой «Отменить заказ» в сообщении о заказе."
)

HELP_DELIVERY = (
    "📦 Инструкция для доставки\n\n"
    "1️⃣ Нажмите «📦 Доставка» на главном экране.\n"
    "2️⃣ Введите адрес «Откуда» — где забрать посылку.\n"
    "3️⃣ Введите адрес «Куда» — куда доставить.\n"
    "4️⃣ Укажите имя и телефон получателя.\n"
    "5️⃣ Добавьте комментарий (хрупкое, позвонить заранее и т.п.).\n"
    "6️⃣ Подтвердите заказ — увидите цену и расстояние.\n\n"
    "💰 Тариф доставки: 43 ₽/км, минимум 150 ₽.\n\n"
    "📞 Водитель свяжется с получателем по указанному телефону."
)


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


# ===================== СТАТИСТИКА =====================

def log_event(event_type: str, user_id: int = None, **extra):
    ts = datetime.now().isoformat()
    event = {"ts": ts, "event": event_type, "user_id": user_id, **extra}
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logging.warning(f"[STATS] Ошибка записи события: {e}")
    try:
        _update_stats(event)
    except Exception as e:
        logging.warning(f"[STATS] Ошибка обновления агрегата: {e}")


def _update_stats(event: dict):
    stats = load_json(STATS_FILE, {
        "total_users": 0,
        "total_passengers": 0,
        "total_drivers": 0,
        "total_delivery": 0,
        "users": {},
        "by_date": {},
        "orders": {
            "taxi_created": 0,
            "taxi_finished": 0,
            "taxi_cancelled": 0,
            "delivery_created": 0,
            "delivery_finished": 0,
            "delivery_cancelled": 0,
            "taxi_revenue": 0,
            "delivery_revenue": 0,
        },
    })

    etype = event["event"]
    uid = event.get("user_id")
    date = event["ts"][:10]

    if date not in stats["by_date"]:
        stats["by_date"][date] = {"new_users": 0, "orders": 0}

    if etype == "user_started":
        stats["total_users"] += 1
        stats["by_date"][date]["new_users"] += 1
        if uid:
            stats["users"][str(uid)] = {"first_seen": event["ts"], "role": None}

    elif etype == "role_selected":
        role = event.get("role")
        if role == "passenger":
            stats["total_passengers"] += 1
        elif role == "driver":
            stats["total_drivers"] += 1
        elif role == "delivery":
            stats["total_delivery"] += 1
        if uid:
            u = stats["users"].get(str(uid), {})
            u["role"] = role
            stats["users"][str(uid)] = u

    elif etype == "order_created":
        otype = event.get("order_type", "taxi")
        stats["orders"][f"{otype}_created"] += 1
        stats["by_date"][date]["orders"] += 1

    elif etype == "order_finished":
        otype = event.get("order_type", "taxi")
        price = event.get("price", 0) or 0
        stats["orders"][f"{otype}_finished"] += 1
        stats["orders"][f"{otype}_revenue"] += price

    elif etype == "order_cancelled":
        otype = event.get("order_type", "taxi")
        stats["orders"][f"{otype}_cancelled"] += 1

    save_json(STATS_FILE, stats)


def rebuild_stats_from_users():
    """Пересчитывает счётчики ролей и total_users из users.json (источник правды)."""
    users = load_json(USERS_FILE, {})
    stats = load_json(STATS_FILE, {
        "total_users": 0,
        "total_passengers": 0,
        "total_drivers": 0,
        "total_delivery": 0,
        "users": {},
        "by_date": {},
        "orders": {
            "taxi_created": 0,
            "taxi_finished": 0,
            "taxi_cancelled": 0,
            "delivery_created": 0,
            "delivery_finished": 0,
            "delivery_cancelled": 0,
            "taxi_revenue": 0,
            "delivery_revenue": 0,
        },
    })

    passengers = sum(1 for u in users.values() if u.get("role") == "passenger")
    drivers = sum(1 for u in users.values() if u.get("role") == "driver")
    delivery = sum(1 for u in users.values() if u.get("role") == "delivery")

    stats["total_passengers"] = passengers
    stats["total_drivers"] = drivers
    stats["total_delivery"] = delivery
    stats["total_users"] = max(stats.get("total_users", 0), len(users))

    save_json(STATS_FILE, stats)
    logging.info(
        f"[STATS] Пересчёт: пользователей={stats['total_users']}, "
        f"пассажиров={passengers}, водителей={drivers}, доставка={delivery}"
    )


def ensure_user_tracked(user_id: int):
    stats = load_json(STATS_FILE, {})
    users = stats.get("users", {})
    if str(user_id) not in users:
        log_event("user_started", user_id=user_id)


def get_stats_text(period: str = "all") -> str:
    # Автопересчёт ролей и total_users из users.json — всегда точные цифры
    rebuild_stats_from_users()

    stats = load_json(STATS_FILE, {})
    if not stats:
        return "📊 Статистика пока пуста — нет данных."

    now = datetime.now()
    if period == "today":
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        period_label = "сегодня"
    elif period == "week":
        period_start = now - timedelta(days=7)
        period_label = "за 7 дней"
    elif period == "month":
        period_start = now - timedelta(days=30)
        period_label = "за 30 дней"
    else:
        period_start = None
        period_label = "всё время"

    orders = stats.get("orders", {})
    total_users = stats.get("total_users", 0)
    total_passengers = stats.get("total_passengers", 0)
    total_drivers = stats.get("total_drivers", 0)
    total_delivery = stats.get("total_delivery", 0)

    if period_start:
        new_users = 0
        orders_in_period = 0
        for d, v in stats.get("by_date", {}).items():
            try:
                dt = datetime.fromisoformat(d + "T00:00:00")
            except Exception:
                continue
            if dt >= period_start:
                new_users += v.get("new_users", 0)
                orders_in_period += v.get("orders", 0)
    else:
        new_users = total_users
        orders_in_period = (
            orders.get("taxi_created", 0) + orders.get("delivery_created", 0)
        )

    taxi_rev = orders.get("taxi_revenue", 0)
    delivery_rev = orders.get("delivery_revenue", 0)

    lines = [
        f"📊 Статистика ({period_label})",
        "",
        "👥 Пользователи:",
        f"• Всего: {total_users}",
        f"• Пассажиров: {total_passengers}",
        f"• Водителей: {total_drivers}",
        f"• Доставка: {total_delivery}",
        f"• Новых {period_label}: {new_users}",
        "",
        f"📦 Заказов {period_label}: {orders_in_period}",
        "",
        "🚕 Такси (всего):",
        f"• Создано: {orders.get('taxi_created', 0)}",
        f"• Завершено: {orders.get('taxi_finished', 0)}",
        f"• Отменено: {orders.get('taxi_cancelled', 0)}",
        "",
        "📦 Доставка (всего):",
        f"• Создано: {orders.get('delivery_created', 0)}",
        f"• Завершено: {orders.get('delivery_finished', 0)}",
        f"• Отменено: {orders.get('delivery_cancelled', 0)}",
        "",
        "💰 Сумма заказов (завершённые):",
        f"• Такси: {taxi_rev} ₽",
        f"• Доставка: {delivery_rev} ₽",
        f"• Итого: {taxi_rev + delivery_rev} ₽",
    ]

    return "\n".join(lines)


def kb_stats_period():
    b = InlineKeyboardBuilder()
    b.row(
        CallbackButton(text="📅 Сегодня", payload="stats_today"),
        CallbackButton(text="📅 Неделя", payload="stats_week"),
    )
    b.row(
        CallbackButton(text="📅 Месяц", payload="stats_month"),
        CallbackButton(text="📅 Всё время", payload="stats_all"),
    )
    return b.as_markup()


# ===================== ГЕОКОДИРОВАНИЕ =====================

async def geocode_address(addr: dict):
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


async def calculate_trip(from_addr: dict, to_addr: dict, order_type: str = "taxi"):
    logging.info(f"[GEO] Старт расчёта ({order_type})")
    coords_from = await geocode_address(from_addr)
    if not coords_from:
        return None, None, None, None
    coords_to = await geocode_address(to_addr)
    if not coords_to:
        return None, None, None, None
    direct_km = haversine(
        coords_from[0], coords_from[1],
        coords_to[0], coords_to[1],
    )
    road_km = direct_km * ROAD_FACTOR

    if order_type == "delivery":
        rate = RATE_PER_KM_DELIVERY
        min_price = MIN_PRICE_DELIVERY
    else:
        rate = RATE_PER_KM_TAXI
        min_price = MIN_PRICE_TAXI

    price = max(min_price, round(road_km * rate))
    logging.info(f"[GEO] Прямо: {direct_km:.2f} км, дорога: {road_km:.2f} км, цена: {price} ₽ ({order_type})")
    return round(road_km, 1), price, coords_from[0], coords_from[1]


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


def drivers_with_fresh_location() -> list:
    drivers = load_json(DRIVERS_FILE, {})
    now = datetime.now()
    result = []
    for uid_str, d in drivers.items():
        if d.get("status") != "online":
            continue
        lat = d.get("lat")
        lon = d.get("lon")
        upd = d.get("location_updated_at")
        if lat is None or lon is None or not upd:
            continue
        try:
            updated_dt = datetime.fromisoformat(upd)
        except Exception:
            continue
        age = now - updated_dt
        if age > timedelta(minutes=LOCATION_TTL_MINUTES):
            continue
        result.append((int(uid_str), lat, lon))
    return result


def drivers_online_count() -> int:
    drivers = load_json(DRIVERS_FILE, {})
    return sum(1 for d in drivers.values() if d.get("status") == "online")


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
    b.row(
        CallbackButton(text="Я пассажир", payload="role_passenger"),
        CallbackButton(text="Я водитель", payload="role_driver"),
    )
    b.row(CallbackButton(text="📦 Доставка", payload="role_delivery"))
    return b.as_markup()


def kb_passenger_menu():
    b = InlineKeyboardBuilder()
    b.row(
        CallbackButton(text="Заказать такси", payload="passenger_order"),
        CallbackButton(text="Мои заказы", payload="passenger_history"),
    )
    b.row(CallbackButton(text="🚕 Машины на линии", payload="passenger_cars_online"))
    b.row(CallbackButton(text="❓ Инструкция", payload="help_passenger"))
    b.row(
        CallbackButton(text="🔄 Сменить роль", payload="change_role"),
        CallbackButton(text="🗑 Сбросить всё", payload="full_reset"),
    )
    return b.as_markup()


def kb_delivery_menu():
    b = InlineKeyboardBuilder()
    b.row(
        CallbackButton(text="📦 Заказать доставку", payload="delivery_order"),
        CallbackButton(text="📋 Мои доставки", payload="delivery_history"),
    )
    b.row(CallbackButton(text="❓ Инструкция", payload="help_delivery"))
    b.row(
        CallbackButton(text="🔄 Сменить роль", payload="change_role"),
        CallbackButton(text="🗑 Сбросить всё", payload="full_reset"),
    )
    return b.as_markup()


def kb_driver_menu():
    b = InlineKeyboardBuilder()
    b.row(
        CallbackButton(text="Выйти на линию", payload="driver_online"),
        CallbackButton(text="Уйти с линии", payload="driver_offline"),
    )
    b.row(
        CallbackButton(text="📍 Геопозиция", payload="driver_location_request"),
        CallbackButton(text="📊 Статистика", payload="driver_stats"),
    )
    b.row(CallbackButton(text="❓ Инструкция", payload="help_driver"))
    b.row(
        CallbackButton(text="🔄 Сменить роль", payload="change_role"),
        CallbackButton(text="🗑 Сбросить всё", payload="full_reset"),
    )
    return b.as_markup()


def kb_location_only():
    b = InlineKeyboardBuilder()
    b.row(RequestGeoLocationButton(text="📍 Отправить мою геопозицию"))
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


def kb_help_driver():
    b = InlineKeyboardBuilder()
    b.row(CallbackButton(text="📍 Геопозиция", payload="help_driver_geo"))
    b.row(CallbackButton(text="🚕 Заказы", payload="help_driver_orders"))
    b.row(CallbackButton(text="🏁 Поездка", payload="help_driver_trip"))
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


def kb_skip_optional():
    b = InlineKeyboardBuilder()
    b.row(CallbackButton(text="⏭ Пропустить", payload="skip_optional"))
    return b.as_markup()


def kb_confirm_order():
    b = InlineKeyboardBuilder()
    b.row(
        CallbackButton(text="✅ Подтвердить", payload="order_confirm"),
        CallbackButton(text="❌ Отменить", payload="order_cancel"),
    )
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


def kb_passenger_active_order(order_id: str):
    b = InlineKeyboardBuilder()
    b.row(CallbackButton(text="❌ Отменить заказ", payload=f"passenger_cancel:{order_id}"))
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


def kb_driver_offer(order_id: str, order_type: str = "taxi"):
    b = InlineKeyboardBuilder()
    b.row(
        CallbackButton(text="✅ Принять", payload=f"offer_accept:{order_id}"),
        CallbackButton(text="❌ Отказаться", payload=f"offer_decline:{order_id}"),
    )
    return b.as_markup()


def kb_driver_on_way(order_id: str, order_type: str = "taxi"):
    b = InlineKeyboardBuilder()
    if order_type == "delivery":
        b.row(
            CallbackButton(text="📦 Забрал", payload=f"trip_picked:{order_id}"),
            CallbackButton(text="❌ Отменить заказ", payload=f"trip_cancel:{order_id}"),
        )
    else:
        b.row(
            CallbackButton(text="📍 Я на месте", payload=f"trip_arrived:{order_id}"),
            CallbackButton(text="❌ Отменить заказ", payload=f"trip_cancel:{order_id}"),
        )
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


def kb_driver_waiting(order_id: str):
    b = InlineKeyboardBuilder()
    b.row(
        CallbackButton(text="🚗 Поехали", payload=f"trip_start:{order_id}"),
        CallbackButton(text="❌ Отменить заказ", payload=f"trip_cancel:{order_id}"),
    )
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


def kb_driver_in_trip(order_id: str):
    b = InlineKeyboardBuilder()
    b.row(CallbackButton(text="🏁 Завершить поездку", payload=f"trip_finish:{order_id}"))
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


def kb_delivery_picked(order_id: str):
    b = InlineKeyboardBuilder()
    b.row(CallbackButton(text="🏁 Доставлено", payload=f"trip_finish:{order_id}"))
    b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
    return b.as_markup()


# ===================== НАПОМИНАНИЯ =====================

async def location_reminder_loop(user_id: int):
    while True:
        await asyncio.sleep(LOCATION_REMIND_INTERVAL * 60)
        driver = get_driver(user_id)
        if not driver or driver.get("status") != "online":
            _reminder_tasks.pop(user_id, None)
            return
        try:
            await bot.send_message(
                user_id=user_id,
                text=(
                    "📍 Обновите геопозицию, чтобы получать заказы.\n\n"
                    "Без свежих координат вы будете в конце очереди."
                ),
                attachments=[kb_location_only()],
            )
        except Exception as e:
            logging.warning(f"[REMIND] Ошибка отправки {user_id}: {e}")


def start_location_reminder(user_id: int):
    if user_id in _reminder_tasks and not _reminder_tasks[user_id].done():
        return
    task = asyncio.create_task(location_reminder_loop(user_id))
    _reminder_tasks[user_id] = task


def stop_location_reminder(user_id: int):
    task = _reminder_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


# ===================== ПОДБОР ВОДИТЕЛЯ =====================

async def find_driver_by_radius(order_id: str):
    order = find_order(order_id)
    if not order:
        return None, None

    from_addr = order.get("from", {})
    p_lat = from_addr.get("lat")
    p_lon = from_addr.get("lon")
    if p_lat is None or p_lon is None:
        return None, None

    drivers_loc = drivers_with_fresh_location()
    if not drivers_loc:
        return None, None

    with_distance = []
    for uid, d_lat, d_lon in drivers_loc:
        dist = haversine(p_lat, p_lon, d_lat, d_lon)
        with_distance.append((uid, dist))

    with_distance.sort(key=lambda x: x[1])

    for radius in RADIUS_STEPS:
        in_radius = [(uid, d) for uid, d in with_distance if d <= radius]
        if in_radius:
            uid, dist = in_radius[0]
            logging.info(f"[RADIUS] Найден {uid} на расстоянии {dist:.2f} км (радиус {radius})")
            return uid, dist

    return None, None


async def offer_order_to_driver(order_id: str, driver_id: int, distance_km: float = None):
    order = find_order(order_id)
    driver = get_driver(driver_id)
    if not order or not driver:
        return

    order_type = order.get("type", "taxi")
    from_text = format_address_full(order["from"])
    to_text = format_address_full(order["to"])
    phone = order.get("phone", "—") or "—"
    distance = order.get("distance_km", "—")
    price = order.get("price", "—")

    dist_line = ""
    if distance_km is not None:
        dist_line = f"🚗 До клиента: {distance_km:.1f} км\n"

    if order_type == "delivery":
        recipient_name = order.get("recipient_name", "—")
        recipient_phone = order.get("recipient_phone", "—")
        text = (
            f"📦 Новый заказ доставки!\n\n"
            f"{dist_line}"
            f"📍 Забрать:\n{from_text}\n\n"
            f"🏁 Доставить:\n{to_text}\n\n"
            f"👤 Получатель: {recipient_name}\n"
            f"📞 Телефон получателя: {recipient_phone}\n"
            f"📞 Телефон заказчика: {phone}\n\n"
            f"📏 Расстояние: {distance} км\n"
            f"💰 Цена: {price} ₽\n\n"
            f"⏱ У вас {ORDER_TIMEOUT} секунд, чтобы принять."
        )
    else:
        text = (
            f"🚕 Новый заказ такси!\n\n"
            f"{dist_line}"
            f"📍 Откуда:\n{from_text}\n\n"
            f"🏁 Куда:\n{to_text}\n\n"
            f"📏 Длина поездки: {distance} км\n"
            f"💰 Цена: {price} ₽\n"
            f"📞 Телефон пассажира: {phone}\n\n"
            f"⏱ У вас {ORDER_TIMEOUT} секунд, чтобы принять."
        )

    try:
        await bot.send_message(
            user_id=driver_id,
            text=text,
            attachments=[kb_driver_offer(order_id, order_type)],
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить заказ водителю {driver_id}: {e}")
        queue_move_to_end(driver_id)
        await try_next_driver(order_id)
        return

    update_order(order_id, status="offered", offered_to=str(driver_id))

    async def timeout_task():
        await asyncio.sleep(ORDER_TIMEOUT)
        current = find_order(order_id)
        if current and current.get("status") == "offered" and current.get("offered_to") == str(driver_id):
            logging.info(f"⏰ Таймер истёк для водителя {driver_id}")
            try:
                await bot.send_message(
                    user_id=driver_id,
                    text="⏰ Время истекло. Заказ передан следующему водителю.",
                )
            except Exception:
                pass
            queue_move_to_end(driver_id)
            await try_next_driver(order_id)

    asyncio.create_task(timeout_task())


async def try_next_driver(order_id: str):
    order = find_order(order_id)
    if not order or order.get("status") in ("accepted", "arrived", "in_trip", "picked", "finished", "cancelled", "cancelled_by_driver", "cancelled_by_passenger"):
        return

    if order.get("status") == "offered":
        update_order(order_id, status="new", offered_to=None)

    driver_id, dist = await find_driver_by_radius(order_id)
    if driver_id:
        await offer_order_to_driver(order_id, driver_id, dist)
        return

    q = queue_load()
    drivers_loc_ids = {uid for uid, _, _ in drivers_with_fresh_location()}
    no_coord_queue = [uid for uid in q if int(uid) not in drivers_loc_ids]

    if no_coord_queue:
        next_driver = int(no_coord_queue[0])
        await offer_order_to_driver(order_id, next_driver, None)
        return

    update_order(order_id, status="waiting")
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"⚠️ Нет свободных водителей!\n\n"
                f"Заказ {order_id} ({order.get('type', 'taxi')}):\n"
                f"Откуда: {format_address_full(order['from'])}\n"
                f"Куда: {format_address_full(order['to'])}\n"
                f"Телефон: {order.get('phone', '—')}\n"
                f"Цена: {order.get('price', '—')} ₽"
            ),
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить админа: {e}")


# ===================== ОБРАБОТЧИКИ =====================

@dp.bot_started()
async def on_bot_started(event: BotStarted):
    user_id = event.user.user_id
    chat_id = event.chat_id
    ensure_user_tracked(user_id)
    role = get_role(user_id)

    if role == "passenger":
        await bot.send_message(
            chat_id=chat_id,
            text="С возвращением! Вы пассажир. Выберите действие:",
            attachments=[kb_passenger_menu()],
        )
    elif role == "delivery":
        await bot.send_message(
            chat_id=chat_id,
            text="С возвращением! Вы в разделе доставки. Выберите действие:",
            attachments=[kb_delivery_menu()],
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
            text="Привет! Это сервис такси и доставки «По Южному».\n\nКто вы?",
            attachments=[kb_role_choice()],
        )


@dp.message_created()
async def on_message(event: MessageCreated):
    user_id = event.message.sender.user_id
    text = (event.message.body.text or "").strip()
    ensure_user_tracked(user_id)
    logging.info(f"📩 {user_id}: '{text}'")

    if text == "/admin":
        if user_id != ADMIN_USER_ID:
            return
        await event.message.answer(
            get_stats_text("all"),
            attachments=[kb_stats_period()],
        )
        return

    attachments = event.message.body.attachments if event.message.body else None
    if attachments:
        for att in attachments:
            att_type = getattr(att, "type", None)
            if att_type == "location" or str(att_type) == "location":
                lat = getattr(att, "latitude", None)
                lon = getattr(att, "longitude", None)
                if lat is not None and lon is not None:
                    logging.info(f"📍 Геолокация водителя {user_id}: lat={lat}, lon={lon}")
                    driver = get_driver(user_id)
                    if driver:
                        driver["lat"] = lat
                        driver["lon"] = lon
                        driver["location_updated_at"] = datetime.now().isoformat()
                        save_driver(user_id, driver)
                        await event.message.answer(
                            f"✅ Геопозиция обновлена.\n\n"
                            f"Координаты: {lat:.5f}, {lon:.5f}\n\n"
                            f"Теперь вы участвуете в подборе заказов рядом с вами.",
                            attachments=[kb_driver_menu()],
                        )
                    else:
                        await event.message.answer("⚠️ Сначала зарегистрируйтесь как водитель.")
                    return

    if text == "/reset":
        reset_user(user_id)
        stop_location_reminder(user_id)
        await event.message.answer("🔄 Полный сброс выполнен.", attachments=[kb_role_choice()])
        return

    if text == "/start":
        role = get_role(user_id)
        if role == "passenger":
            await event.message.answer(
                "С возвращением! Вы пассажир. Выберите действие:",
                attachments=[kb_passenger_menu()],
            )
        elif role == "delivery":
            await event.message.answer(
                "С возвращением! Вы в разделе доставки. Выберите действие:",
                attachments=[kb_delivery_menu()],
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
                "Привет! Это сервис такси и доставки «По Южному».\n\nКто вы?",
                attachments=[kb_role_choice()],
            )
        return

    state = get_state(user_id)
    step = state.get("step")

    if step == "order":
        await handle_order_step(event, user_id, text, state)
        return

    if step == "delivery_order":
        await handle_delivery_step(event, user_id, text, state)
        return

    if step == "driver_reg":
        await handle_driver_reg(event, user_id, text, state)
        return

    if text.lower() in ("пассажир", "1"):
        set_role(user_id, "passenger")
        log_event("role_selected", user_id=user_id, role="passenger")
        await event.message.answer("Отлично! Вы пассажир.", attachments=[kb_passenger_menu()])
        return
    if text.lower() in ("доставка", "3"):
        set_role(user_id, "delivery")
        log_event("role_selected", user_id=user_id, role="delivery")
        await event.message.answer("Отлично! Вы в разделе доставки.", attachments=[kb_delivery_menu()])
        return
    if text.lower() in ("водитель", "2"):
        set_role(user_id, "driver")
        log_event("role_selected", user_id=user_id, role="driver")
        set_state(user_id, {"step": "driver_reg", "data": {}})
        await event.message.answer("Отлично! Вы водитель.\n\nВведите ваше имя:")
        return

    role = get_role(user_id)
    if role == "passenger":
        await event.message.answer("Вы пассажир. Выберите действие:", attachments=[kb_passenger_menu()])
    elif role == "delivery":
        await event.message.answer("Вы в разделе доставки. Выберите действие:", attachments=[kb_delivery_menu()])
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
            "Привет! Это сервис такси и доставки «По Южному».\n\nКто вы?",
            attachments=[kb_role_choice()],
        )


# ===================== ВВОД АДРЕСА (ТАКСИ) =====================

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


# ===================== ВВОД АДРЕСА (ДОСТАВКА) =====================

DELIVERY_STEPS = [
    ("from", "city", "📦 ОТКУДА ЗАБРАТЬ\n\n🏙 Введите населённый пункт:"),
    ("from", "street", "📦 ОТКУДА ЗАБРАТЬ\n\n🛣 Введите улицу:"),
    ("from", "house", "📦 ОТКУДА ЗАБРАТЬ\n\n🏠 Введите номер дома:"),
    ("from", "comment", "📦 ОТКУДА ЗАБРАТЬ\n\n💬 Комментарий (что забрать, как найти)\n\nИли нажмите «Пропустить»:"),
    ("to", "city", "📦 КУДА ДОСТАВИТЬ\n\n🏙 Введите населённый пункт:"),
    ("to", "street", "📦 КУДА ДОСТАВИТЬ\n\n🛣 Введите улицу:"),
    ("to", "house", "📦 КУДА ДОСТАВИТЬ\n\n🏠 Введите номер дома:"),
    ("to", "comment", "📦 КУДА ДОСТАВИТЬ\n\n💬 Комментарий (как найти получателя)\n\nИли нажмите «Пропустить»:"),
    ("recipient", "name", "👤 Введите имя получателя:"),
    ("recipient", "phone", "📞 Введите телефон получателя:"),
    ("customer", "phone", "📞 Введите ваш телефон для связи\n\nИли нажмите «Пропустить»:"),
]


def get_current_step(state: dict):
    data = state.get("data", {})
    for addr_key, field, question in ORDER_STEPS:
        addr = data.get(addr_key, {})
        if field not in addr:
            return addr_key, field, question
    return None


def get_current_delivery_step(state: dict):
    data = state.get("data", {})
    for addr_key, field, question in DELIVERY_STEPS:
        addr = data.get(addr_key, {})
        if field not in addr:
            return addr_key, field, question
    return None


async def send_next_step(chat_answer, data: dict):
    next_step = get_current_step({"data": data})
    if next_step is None:
        return False
    _, next_field, next_question = next_step
    kb = kb_skip_optional() if next_field in ("comment", "phone") else None
    await chat_answer(next_question, attachments=[kb] if kb else None)
    return True


async def send_next_delivery_step(chat_answer, data: dict):
    next_step = get_current_delivery_step({"data": data})
    if next_step is None:
        return False
    _, next_field, next_question = next_step
    kb = kb_skip_optional() if next_field == "comment" else None
    if next_field == "phone" and next_step[0] == "customer":
        kb = kb_skip_optional()
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

    if get_current_step({"data": data}) is None:
        await show_order_summary(event, user_id, data)
        return

    await send_next_step(event.message.answer, data)


async def handle_delivery_step(event: MessageCreated, user_id: int, text: str, state: dict):
    data = state.get("data", {})

    current = get_current_delivery_step(state)
    if current is None:
        await show_delivery_summary(event, user_id, data)
        return

    addr_key, field, question = current

    if addr_key in ("from", "to") and field in ("city", "street", "house"):
        if not text or text.lower() in ("пропустить", "skip", "/skip"):
            await event.message.answer(
                "❌ Это поле обязательно. Пожалуйста, введите значение:\n\n" + question
            )
            return

    if addr_key == "recipient" and field in ("name", "phone"):
        if not text or text.lower() in ("пропустить", "skip", "/skip"):
            await event.message.answer(
                "❌ Это поле обязательно. Пожалуйста, введите значение:\n\n" + question
            )
            return

    if addr_key not in data:
        data[addr_key] = {}
    data[addr_key][field] = text
    set_state(user_id, {"step": "delivery_order", "data": data})

    if get_current_delivery_step({"data": data}) is None:
        await show_delivery_summary(event, user_id, data)
        return

    await send_next_delivery_step(event.message.answer, data)


async def show_order_summary(event, user_id: int, data: dict):
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

    distance, price, from_lat, from_lon = await calculate_trip(from_addr, to_addr, "taxi")

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

    from_addr["lat"] = from_lat
    from_addr["lon"] = from_lon
    data["from"] = from_addr
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


async def show_delivery_summary(event, user_id: int, data: dict):
    if hasattr(event, "message") and event.message is not None:
        send_func = event.message.answer
    else:
        send_func = event.send

    from_addr = data.get("from", {})
    to_addr = data.get("to", {})
    recipient = data.get("recipient", {})
    customer = data.get("customer", {})

    from_text = format_address_full(from_addr)
    to_text = format_address_full(to_addr)
    recipient_name = recipient.get("name", "—")
    recipient_phone = recipient.get("phone", "—")
    customer_phone = customer.get("phone", "").strip() or "—"

    await send_func("🔎 Считаю расстояние и цену доставки, подождите...")

    distance, price, from_lat, from_lon = await calculate_trip(from_addr, to_addr, "delivery")

    if distance is None:
        await send_func(
            "⚠️ Не удалось определить адрес на карте.\n\n"
            "Проверьте, правильно ли указаны адреса.\n\n"
            "Начните заново: /start → «📦 Доставка».",
            attachments=[kb_delivery_menu()],
        )
        set_state(user_id, {})
        return

    from_addr["lat"] = from_lat
    from_addr["lon"] = from_lon
    data["from"] = from_addr
    data["distance_km"] = distance
    data["price"] = price
    set_state(user_id, {"step": "delivery_order", "data": data})

    await send_func(
        f"📋 Проверьте заказ доставки:\n\n"
        f"📦 ЗАБРАТЬ:\n{from_text}\n\n"
        f"🏁 ДОСТАВИТЬ:\n{to_text}\n\n"
        f"👤 Получатель: {recipient_name}\n"
        f"📞 Телефон получателя: {recipient_phone}\n"
        f"📞 Ваш телефон: {customer_phone}\n\n"
        f"📏 Расстояние: {distance} км\n"
        f"💰 Цена: {price} ₽\n\n"
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
            f"Чтобы начать получать заказы, нажмите «Выйти на линию» и обязательно отправьте геопозицию.",
            attachments=[kb_driver_menu()],
        )
        return


@dp.message_callback()
async def on_callback(event: MessageCallback):
    user_id = event.callback.user.user_id
    payload = event.callback.payload or ""
    callback_id = event.callback.callback_id

    if callback_id in _processed_callbacks:
        return
    _processed_callbacks.append(callback_id)

    logging.info(f"🔘 callback: user_id={user_id}, payload={payload}")

    if payload in ("stats_today", "stats_week", "stats_month", "stats_all"):
        if user_id != ADMIN_USER_ID:
            return
        period_map = {
            "stats_today": "today",
            "stats_week": "week",
            "stats_month": "month",
            "stats_all": "all",
        }
        period = period_map[payload]
        await event.send(
            text=get_stats_text(period),
            attachments=[kb_stats_period()],
        )
        return

    if payload == "main_menu":
        role = get_role(user_id)
        if role == "passenger":
            await event.send(text="Вы пассажир. Выберите действие:", attachments=[kb_passenger_menu()])
        elif role == "delivery":
            await event.send(text="Вы в разделе доставки. Выберите действие:", attachments=[kb_delivery_menu()])
        elif role == "driver":
            driver = get_driver(user_id)
            if driver:
                await event.send(text=f"Вы водитель, {driver['name']}. Выберите действие:", attachments=[kb_driver_menu()])
            else:
                set_state(user_id, {"step": "driver_reg", "data": {}})
                await event.send(text="Введите ваше имя:")
        else:
            await event.send(text="Кто вы?", attachments=[kb_role_choice()])
        return

    if payload == "help_driver":
        await event.send(text=HELP_DRIVER, attachments=[kb_help_driver()])
        return

    if payload == "help_driver_geo":
        await event.send(text=HELP_DRIVER_GEO, attachments=[kb_help_driver()])
        return

    if payload == "help_driver_orders":
        await event.send(text=HELP_DRIVER_ORDERS, attachments=[kb_help_driver()])
        return

    if payload == "help_driver_trip":
        await event.send(text=HELP_DRIVER_TRIP, attachments=[kb_help_driver()])
        return

    if payload == "help_passenger":
        b = InlineKeyboardBuilder()
        b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
        await event.send(text=HELP_PASSENGER, attachments=[b.as_markup()])
        return

    if payload == "help_delivery":
        b = InlineKeyboardBuilder()
        b.row(CallbackButton(text="🏠 Назад в меню", payload="main_menu"))
        await event.send(text=HELP_DELIVERY, attachments=[b.as_markup()])
        return

    if payload == "skip_optional":
        state = get_state(user_id)
        step = state.get("step")
        data = state.get("data", {})

        if step == "delivery_order":
            current = get_current_delivery_step(state)
            if current is None:
                await event.send(text="Все данные уже собраны.")
                return
            addr_key, field, _ = current
            allowed_skip = (field == "comment" or (addr_key == "customer" and field == "phone"))
            if not allowed_skip:
                await event.send(text="Это поле обязательно, его нельзя пропустить.")
                return
            if addr_key not in data:
                data[addr_key] = {}
            data[addr_key][field] = ""
            set_state(user_id, {"step": "delivery_order", "data": data})

            if get_current_delivery_step({"data": data}) is None:
                await show_delivery_summary(event, user_id, data)
                return
            await send_next_delivery_step(event.send, data)
            return

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
        stop_location_reminder(user_id)
        await event.send(text="Роль сброшена. Кто вы?", attachments=[kb_role_choice()])
        return

    if payload == "full_reset":
        reset_user(user_id)
        stop_location_reminder(user_id)
        await event.send(text="🗑 Полный сброс выполнен.\n\nКто вы?", attachments=[kb_role_choice()])
        return

    if payload == "role_passenger":
        set_role(user_id, "passenger")
        log_event("role_selected", user_id=user_id, role="passenger")
        await event.send(text="Отлично! Вы пассажир. Выберите действие:", attachments=[kb_passenger_menu()])
        return

    if payload == "role_delivery":
        set_role(user_id, "delivery")
        log_event("role_selected", user_id=user_id, role="delivery")
        await event.send(text="Отлично! Вы в разделе доставки. Выберите действие:", attachments=[kb_delivery_menu()])
        return

    if payload == "role_driver":
        set_role(user_id, "driver")
        log_event("role_selected", user_id=user_id, role="driver")
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
        my = [o for o in orders if o.get("user_id") == user_id and o.get("type", "taxi") == "taxi"]
        if not my:
            await event.send(text="У вас пока нет заказов такси.", attachments=[kb_passenger_menu()])
        else:
            text = "Ваши последние заказы такси:\n\n"
            for o in my[-5:]:
                from_text = format_address(o.get("from", {}))
                to_text = format_address(o.get("to", {}))
                price = o.get("price", "—")
                text += f"• {from_text} → {to_text} — {price} ₽ ({o.get('status')})\n"
            await event.send(text=text, attachments=[kb_passenger_menu()])
        return

    if payload == "passenger_cars_online":
        count = drivers_online_count()
        with_coords = len(drivers_with_fresh_location())
        if count == 0:
            text = "Сейчас на линии нет машин. Попробуйте позже."
        else:
            text = (
                f"🚕 Сейчас на линии: {count} машин.\n"
                f"Со свежими координатами: {with_coords}.\n\n"
                f"Заказывайте — мы найдём ближайшую!"
            )
        await event.send(text=text, attachments=[kb_passenger_menu()])
        return

    if payload == "delivery_order":
        free = queue_count()
        if free == 0:
            info = "⚠️ Свободных машин сейчас нет, но мы поищем для вас."
        else:
            info = f"🚕 Свободных машин на линии: {free}."
        set_state(user_id, {"step": "delivery_order", "data": {}})
        first_question = DELIVERY_STEPS[0][2]
        await event.send(text=f"{info}\n\n📦 Оформляем доставку.\n\n{first_question}")
        return

    if payload == "delivery_history":
        orders = orders_load()
        my = [o for o in orders if o.get("user_id") == user_id and o.get("type") == "delivery"]
        if not my:
            await event.send(text="У вас пока нет заказов доставки.", attachments=[kb_delivery_menu()])
        else:
            text = "Ваши последние доставки:\n\n"
            for o in my[-5:]:
                from_text = format_address(o.get("from", {}))
                to_text = format_address(o.get("to", {}))
                price = o.get("price", "—")
                text += f"• {from_text} → {to_text} — {price} ₽ ({o.get('status')})\n"
            await event.send(text=text, attachments=[kb_delivery_menu()])
        return

    if payload == "order_confirm":
        state = get_state(user_id)
        step = state.get("step")
        data = state.get("data", {})
        from_addr = data.get("from", {})
        to_addr = data.get("to", {})
        distance = data.get("distance_km")
        price = data.get("price")

        if not from_addr.get("city") or not to_addr.get("city") or distance is None:
            await event.send(text="Ошибка: данные заказа потеряны. Начните заново.")
            set_state(user_id, {})
            return

        order_id = "order_" + uuid.uuid4().hex[:8]

        if step == "delivery_order":
            recipient = data.get("recipient", {})
            customer = data.get("customer", {})
            phone = customer.get("phone", "").strip() or "—"

            order = {
                "id": order_id,
                "type": "delivery",
                "user_id": user_id,
                "from": from_addr,
                "to": to_addr,
                "phone": phone,
                "recipient_name": recipient.get("name", "—"),
                "recipient_phone": recipient.get("phone", "—"),
                "distance_km": distance,
                "price": price,
                "status": "new",
                "driver_id": None,
                "created_at": datetime.now().isoformat(),
            }
            save_order(order)
            set_state(user_id, {})

            log_event("order_created", user_id=user_id, order_id=order_id, order_type="delivery")

            await event.send(
                text=(
                    f"✅ Заказ доставки принят!\n\n"
                    f"📦 Забрать: {format_address(from_addr)}\n"
                    f"🏁 Доставить: {format_address(to_addr)}\n"
                    f"👤 Получатель: {recipient.get('name', '—')}\n"
                    f"📞 Телефон получателя: {recipient.get('phone', '—')}\n"
                    f"📏 Расстояние: {distance} км\n"
                    f"💰 Цена: {price} ₽\n\n"
                    f"Ищем для вас курьера..."
                ),
            )

            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"📦 Новый заказ доставки {order_id}\n\n"
                        f"Забрать: {format_address_full(from_addr)}\n"
                        f"Доставить: {format_address_full(to_addr)}\n"
                        f"Получатель: {recipient.get('name', '—')}, {recipient.get('phone', '—')}\n"
                        f"Телефон заказчика: {phone}\n"
                        f"Расстояние: {distance} км\n"
                        f"Цена: {price} ₽\n"
                        f"Заказчик: {user_id}"
                    ),
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить админа: {e}")

            await try_next_driver(order_id)
            return

        phone = from_addr.get("phone", "").strip()
        phone_text = phone if phone else "—"

        order = {
            "id": order_id,
            "type": "taxi",
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

        log_event("order_created", user_id=user_id, order_id=order_id, order_type="taxi")

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
                    f"🚕 Новый заказ такси {order_id}\n\n"
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
        role = get_role(user_id)
        set_state(user_id, {})
        if role == "delivery":
            await event.send(
                text="Заказ доставки отменён. Возвращайтесь, когда понадобится.",
                attachments=[kb_delivery_menu()],
            )
        else:
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

        log_event(
            "order_cancelled",
            user_id=user_id,
            order_id=order_id,
            order_type=order.get("type", "taxi"),
            cancelled_by="passenger",
        )

        driver_id = order.get("driver_id")
        if driver_id:
            drv = get_driver(int(driver_id))
            if drv and drv.get("status") == "online":
                queue_move_to_end(int(driver_id))
            try:
                await bot.send_message(
                    user_id=int(driver_id),
                    text="⚠️ Клиент отменил заказ. Возвращаетесь в конец очереди.",
                    attachments=[kb_driver_menu()],
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить водителя: {e}")

        role = get_role(user_id)
        if role == "delivery":
            await event.send(
                text="❌ Заказ доставки отменён.",
                attachments=[kb_delivery_menu()],
            )
        else:
            await event.send(
                text="❌ Заказ отменён.",
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
        log_event("driver_online", user_id=user_id)

        has_coords = driver.get("lat") is not None and driver.get("lon") is not None
        hint = ""
        if not has_coords:
            hint = "\n\n⚠️ Отправьте геопозицию, чтобы получать заказы рядом с вами."

        await event.send(text=f"✅ Вы на линии.{hint}", attachments=[kb_driver_menu()])
        start_location_reminder(user_id)
        return

    if payload == "driver_offline":
        driver = get_driver(user_id)
        if driver:
            driver["status"] = "offline"
            save_driver(user_id, driver)
        queue_remove(user_id)
        stop_location_reminder(user_id)
        log_event("driver_offline", user_id=user_id)
        await event.send(text="Вы ушли с линии.", attachments=[kb_driver_menu()])
        return

    if payload == "driver_location_request":
        await event.send(
            text=(
                "📍 Нажмите кнопку ниже, чтобы отправить свою геопозицию.\n\n"
                "По ней мы будем предлагать заказы рядом с вами.\n\n"
                "⚠️ Отправляйте геопозицию с телефона — в браузере может не работать."
            ),
            attachments=[kb_location_only()],
        )
        return

    if payload == "driver_stats":
        driver = get_driver(user_id)
        if not driver:
            await event.send(text="Сначала зарегистрируйтесь.", attachments=[kb_driver_menu()])
            return

        total = drivers_online_count()
        with_coords = len(drivers_with_fresh_location())

        lines = ["📊 Статистика", ""]
        lines.append("👤 Ваш статус:")

        if driver.get("status") == "online":
            lines.append("✅ Вы на линии")
        else:
            lines.append("⛔ Вы не на линии")

        lat = driver.get("lat")
        lon = driver.get("lon")
        upd = driver.get("location_updated_at")

        if lat is not None and lon is not None and upd:
            try:
                dt = datetime.fromisoformat(upd)
                age_min = int((datetime.now() - dt).total_seconds() / 60)
                lines.append(f"📍 Координаты: {lat:.5f}, {lon:.5f}")
                if age_min > LOCATION_TTL_MINUTES:
                    lines.append(f"⚠️ Обновлены {age_min} мин назад — устарели!")
                else:
                    lines.append(f"⏱ Обновлены {age_min} мин назад.")
            except Exception:
                lines.append("📍 Координаты: (ошибка чтения времени)")
        else:
            lines.append("📍 Координаты: не отправлены ⚠️")

        lines.append("")
        lines.append(f"🚗 Всего на линии: {total} водителей")
        lines.append(f"📡 Со свежими координатами: {with_coords}")

        if driver.get("status") == "online":
            lines.append("")
            if lat is not None and lon is not None and upd:
                try:
                    dt = datetime.fromisoformat(upd)
                    age_min = int((datetime.now() - dt).total_seconds() / 60)
                    if age_min > LOCATION_TTL_MINUTES:
                        lines.append("Обновите геопозицию, чтобы получать заказы.")
                    else:
                        lines.append("Вы участвуете в подборе заказов по расстоянию.")
                except Exception:
                    pass
            else:
                lines.append("⚠️ Вы в резервном списке.")
                lines.append("Заказ получите, только если рядом никого не найдётся.")
                lines.append("")
                lines.append("Отправьте геопозицию, чтобы получать заказы первыми.")

        text = "\n".join(lines)
        await event.send(text=text, attachments=[kb_driver_menu()])
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

        order_type = order.get("type", "taxi")
        from_text = format_address_full(order["from"])
        to_text = format_address_full(order["to"])
        phone = order.get("phone", "—") or "—"
        distance = order.get("distance_km", "—")
        price = order.get("price", "—")

        if order_type == "delivery":
            recipient_name = order.get("recipient_name", "—")
            recipient_phone = order.get("recipient_phone", "—")

            await event.send(
                text=(
                    f"✅ Доставка принята! Езжайте забирать посылку.\n\n"
                    f"📦 Забрать:\n{from_text}\n\n"
                    f"🏁 Доставить:\n{to_text}\n\n"
                    f"👤 Получатель: {recipient_name}\n"
                    f"📞 Телефон получателя: {recipient_phone}\n"
                    f"📞 Телефон заказчика: {phone}\n\n"
                    f"📏 Расстояние: {distance} км\n"
                    f"💰 Цена: {price} ₽\n\n"
                    f"Когда заберёте — нажмите «📦 Забрал»."
                ),
                attachments=[kb_driver_on_way(order_id, "delivery")],
            )

            try:
                await bot.send_message(
                    user_id=order["user_id"],
                    text=(
                        f"📦 Курьер найден!\n\n"
                        f"Имя: {driver['name']}\n"
                        f"Машина: {driver['car']} ({driver['plate']})\n"
                        f"Телефон курьера: {driver['phone']}\n\n"
                        f"💰 Цена доставки: {price} ₽\n\n"
                        f"Курьер едет забирать посылку по адресу: {format_address(order['from'])}"
                    ),
                    attachments=[kb_passenger_active_order(order_id)],
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить заказчика: {e}")
            return

        await event.send(
            text=(
                f"✅ Заказ принят! Езжайте к пассажиру.\n\n"
                f"📍 Откуда:\n{from_text}\n\n"
                f"🏁 Куда:\n{to_text}\n\n"
                f"📏 Длина поездки: {distance} км\n"
                f"💰 Цена: {price} ₽\n"
                f"📞 Телефон пассажира: {phone}"
            ),
            attachments=[kb_driver_on_way(order_id, "taxi")],
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
        await event.send(text="Вы отказались. Заказ передан следующему водителю.", attachments=[kb_driver_menu()])
        await try_next_driver(order_id)
        return

    if payload.startswith("trip_picked:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("status") != "accepted" or order.get("driver_id") != str(user_id):
            await event.send(text="Этот заказ не ваш или уже не актуален.")
            return
        if order.get("type") != "delivery":
            await event.send(text="Это не заказ доставки.")
            return

        update_order(order_id, status="picked", picked_at=datetime.now().isoformat())

        await event.send(
            text=(
                f"📦 Посылка забрана. Везём получателю.\n\n"
                f"🏁 Доставить: {format_address(order['to'])}\n"
                f"👤 Получатель: {order.get('recipient_name', '—')}\n"
                f"📞 Телефон получателя: {order.get('recipient_phone', '—')}"
            ),
            attachments=[kb_delivery_picked(order_id)],
        )

        try:
            await bot.send_message(
                user_id=order["user_id"],
                text=(
                    f"📦 Курьер забрал посылку и везёт получателю.\n\n"
                    f"🏁 Доставить: {format_address(order['to'])}"
                ),
                attachments=[kb_passenger_active_order(order_id)],
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить заказчика: {e}")
        return

    if payload.startswith("trip_arrived:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("status") != "accepted" or order.get("driver_id") != str(user_id):
            await event.send(text="Этот заказ не ваш или уже не актуален.")
            return
        if order.get("type") != "taxi":
            await event.send(text="Это не заказ такси.")
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
        log_event(
            "order_cancelled",
            user_id=user_id,
            order_id=order_id,
            order_type=order.get("type", "taxi"),
            cancelled_by="driver",
        )
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
            logging.warning(f"Не удалось уведомить клиента: {e}")
        update_order(order_id, status="new", driver_id=None)
        await try_next_driver(order_id)
        return

    if payload.startswith("trip_finish:"):
        order_id = payload.split(":", 1)[1]
        order = find_order(order_id)
        if not order or order.get("driver_id") != str(user_id):
            await event.send(text="Этот заказ не ваш или уже завершён.")
            return

        order_type = order.get("type", "taxi")

        if order_type == "delivery":
            if order.get("status") != "picked":
                await event.send(text="Сначала нажмите «📦 Забрал», потом «🏁 Доставлено».")
                return
            update_order(order_id, status="finished", finished_at=datetime.now().isoformat())
            driver = get_driver(user_id)
            if driver and driver.get("status") == "online":
                queue_move_to_end(user_id)

            log_event(
                "order_finished",
                user_id=order.get("user_id"),
                order_id=order_id,
                order_type="delivery",
                price=order.get("price", 0),
            )

            await event.send(
                text="🏁 Доставка завершена. Спасибо!",
                attachments=[kb_driver_menu()],
            )
            try:
                await bot.send_message(
                    user_id=order["user_id"],
                    text="🏁 Ваша доставка завершена. Спасибо, что выбрали нас!",
                )
            except Exception as e:
                logging.warning(f"Не удалось уведомить заказчика: {e}")
            return

        if order.get("status") != "in_trip":
            await event.send(text="Этот заказ не ваш или уже завершён.")
            return
        update_order(order_id, status="finished", finished_at=datetime.now().isoformat())
        driver = get_driver(user_id)
        if driver and driver.get("status") == "online":
            queue_move_to_end(user_id)

        log_event(
            "order_finished",
            user_id=order.get("user_id"),
            order_id=order_id,
            order_type="taxi",
            price=order.get("price", 0),
        )

        await event.send(
            text="🏁 Поездка завершена. Спасибо!",
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