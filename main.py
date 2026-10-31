import asyncio
import json
import os
import time
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://ВСТАВЬТЕ_СЮДА_ССЫЛКУ")
MARKER_TTL_SECONDS = 30 * 60  # Метка живет 30 минут (1800 секунд)

app = FastAPI()

markers = []

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
    global markers
    now = time.time()
    markers = [m for m in markers if now - m["timestamp"] < MARKER_TTL_SECONDS]

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

            if action == "add_marker":
                clean_old_markers()
                new_marker = {
                    "id": int(time.time() * 1000),
                    "lat": float(payload["lat"]),
                    "lng": float(payload["lng"]),
                    "angle": int(payload.get("angle", 0)),
                    "car_model": str(payload.get("car_model", "Lada Granta")),
                    "standing": bool(payload.get("standing", False)),
                    "comment": str(payload.get("comment", "")).strip()[:80],
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

async def start_bot():
    if BOT_TOKEN.startswith("ВСТАВЬТЕ") or not BOT_TOKEN:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        dp = Dispatcher()

        @dp.message(CommandStart())
        async def cmd_start(message: types.Message):
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
                "• Ставьте метку и направление в 2 касания",
                reply_markup=kb,
                parse_mode="HTML"
            )

        await dp.start_polling(bot)
    except Exception as e:
        print(f"Инфо бота: {e}")

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_bot())

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
