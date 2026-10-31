import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import os
import time
from typing import Dict, List, Set, Tuple
from urllib.parse import parse_qsl

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse
import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://ВСТАВЬТЕ_СЮДА_ССЫЛКУ")
# Если хотите, чтобы /stats видели только вы — можете добавить ADMIN_ID в Render (ваш Telegram ID)
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
MARKER_TTL_SECONDS = 20 * 60  # Жизнь метки — ровно 20 минут (1200 секунд)

markers = []

# =====================================================================
# СТАТИСТИКА ПРОЕКТА
# =====================================================================
MSK_TZ = timezone(timedelta(hours=3))
SERVER_START_TIME = time.time()

stats_all_users: Set[str] = set()               # Уникальные пользователи за всё время работы
stats_daily_users: Dict[str, Set[str]] = {}     # Уникальные пользователи по дням (YYYY-MM-DD)
stats_total_markers: int = 0                    # Всего поставлено меток
stats_daily_markers: Dict[str, int] = {}        # Меток поставлено за день


def today_key() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d")


def register_user_visit(user_key: str):
    if not user_key:
        return
    stats_all_users.add(user_key)
    day = today_key()
    if day not in stats_daily_users:
        stats_daily_users[day] = set()
    stats_daily_users[day].add(user_key)


def register_marker_created():
    global stats_total_markers
    stats_total_markers += 1
    day = today_key()
    stats_daily_markers[day] = stats_daily_markers.get(day, 0) + 1


def extract_telegram_author(init_data: str, fallback_user: dict | None) -> Tuple[int | None, str, str]:
    """Проверяет подпись initData от Telegram и возвращает (id, имя, username) автора."""
    if init_data:
        try:
            parsed = dict(parse_qsl(init_data, keep_blank_values=True))
            received_hash = parsed.pop("hash", "")
            user_json = parsed.get("user")
            if user_json:
                valid = True
                if BOT_TOKEN and not BOT_TOKEN.startswith("ВСТАВЬТЕ") and received_hash:
                    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
                    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
                    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
                    valid = hmac.compare_digest(calc_hash, received_hash)

                if valid:
                    u = json.loads(user_json)
                    uid = u.get("id")
                    full_name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or "Участник"
                    username = u.get("username", "")
                    return uid, full_name, username
        except Exception:
            pass

    if isinstance(fallback_user, dict) and fallback_user.get("id"):
        uid = fallback_user.get("id")
        full_name = f"{fallback_user.get('first_name', '')} {fallback_user.get('last_name', '')}".strip() or "Участник"
        username = fallback_user.get("username", "")
        return uid, full_name, username

    return None, "Гость", ""


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


def clean_old_markers():
    """Удаляет метки старше 20 минут с момента последнего подтверждения"""
    global markers
    now = time.time()
    markers = [m for m in markers if now - m["timestamp"] < MARKER_TTL_SECONDS]


def format_uptime() -> str:
    sec = int(time.time() - SERVER_START_TIME)
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days > 0:
        return f"{days} дн. {hours} ч. {mins} мин."
    return f"{hours} ч. {mins} мин."


async def start_bot():
    if BOT_TOKEN.startswith("ВСТАВЬТЕ") or not BOT_TOKEN:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        dp = Dispatcher()

        @dp.message(CommandStart())
        async def cmd_start(message: types.Message):
            if message.from_user:
                register_user_visit(f"tg_{message.from_user.id}")

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🚓 Открыть Радар Жибера",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )]
            ])
            await message.answer(
                "👋 <b>Добро пожаловать в Радар Жибера!</b> 🚕\n\n"
                "Операция «Кобра» в прямом эфире! Нажмите кнопку ниже, чтобы открыть живую карту:\n"
                "• Смотрите, где сейчас Комиссар и Эмильен и куда они направляются\n"
                "• Ставьте метку и направление в 2 касания\n\n"
                "<i>Создано исключительно в развлекательных целях по мотивам фильма «Такси».</i>",
                reply_markup=kb,
                parse_mode="HTML"
            )

        @dp.message(Command("stats"))
        async def cmd_stats(message: types.Message):
            # Если задан ADMIN_ID, показываем статистику только владельцу
            if ADMIN_ID and message.from_user and str(message.from_user.id) != ADMIN_ID:
                return

            clean_old_markers()
            day = today_key()
            online_now = len(manager.active_connections)
            today_users_count = len(stats_daily_users.get(day, set()))
            all_users_count = len(stats_all_users)
            active_markers_now = len(markers)
            today_markers_count = stats_daily_markers.get(day, 0)

            text = (
                "📊 <b>Статистика «Радар Жибера»</b>\n\n"
                f"🟢 <b>Сейчас на карте (онлайн):</b> {online_now} чел.\n"
                f"👥 <b>Уникальных за сегодня:</b> {today_users_count} чел.\n"
                f"🌍 <b>Уникальных всего:</b> {all_users_count} чел.\n\n"
                f"🚓 <b>Активных меток сейчас:</b> {active_markers_now}\n"
                f"📍 <b>Поставлено за сегодня:</b> {today_markers_count}\n"
                f"📈 <b>Поставлено всего:</b> {stats_total_markers}\n\n"
                f"⏱ <b>Аптайм сервера:</b> {format_uptime()}\n"
                f"🆔 <i>Ваш Telegram ID:</i> <code>{message.from_user.id if message.from_user else ''}</code>"
            )
            await message.answer(text, parse_mode="HTML")

        await dp.start_polling(bot)
    except Exception as e:
        print(f"Инфо бота: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = asyncio.create_task(start_bot())
    yield
    bot_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/ping")
async def ping():
    """Легкий адрес для анти-сна (cron-job.org / UptimeRobot)"""
    return PlainTextResponse("ok")


@app.get("/")
async def get_map():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/markers")
async def get_markers():
    clean_old_markers()
    return {"markers": markers, "server_time": time.time()}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global markers
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            action = payload.get("action")

            # Фиксация входа пользователя на карту для точной статистики /stats
            if action == "hello":
                uid, _, _ = extract_telegram_author(
                    payload.get("init_data", ""),
                    payload.get("fallback_user")
                )
                client_cid = str(payload.get("client_id", "")).strip()[:40]
                user_key = f"tg_{uid}" if uid else (f"web_{client_cid}" if client_cid else "")
                register_user_visit(user_key)

            elif action == "add_marker":
                clean_old_markers()
                author_id, author_name, author_username = extract_telegram_author(
                    payload.get("init_data", ""),
                    payload.get("fallback_user")
                )
                if author_id:
                    register_user_visit(f"tg_{author_id}")
                register_marker_created()

                new_marker = {
                    "id": int(time.time() * 1000),
                    "lat": float(payload["lat"]),
                    "lng": float(payload["lng"]),
                    "angle": int(payload.get("angle", 0)),
                    "car_model": str(payload.get("car_model", "Lada Granta")),
                    "standing": bool(payload.get("standing", False)),
                    "comment": str(payload.get("comment", "")).strip()[:80],
                    "author_id": author_id,
                    "author_name": author_name,
                    "author_username": author_username,
                    "confirms": 1,
                    "denials": 0,
                    "timestamp": time.time()
                }
                markers.append(new_marker)
                await manager.broadcast({"event": "new_marker", "marker": new_marker})

            elif action == "vote_marker":
                marker_id = payload.get("id")
                vote_type = payload.get("vote")
                for m in list(markers):
                    if m["id"] == marker_id:
                        if vote_type == "confirm":
                            m["confirms"] = m.get("confirms", 1) + 1
                            m["timestamp"] = time.time()
                            await manager.broadcast({"event": "update_marker", "marker": m})
                        elif vote_type == "deny":
                            m["denials"] = m.get("denials", 0) + 1
                            if m["denials"] >= 2:
                                markers = [x for x in markers if x["id"] != marker_id]
                                await manager.broadcast({"event": "delete_marker", "id": marker_id})
                            else:
                                await manager.broadcast({"event": "update_marker", "marker": m})
                        break
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
