import asyncio
import random
import json
import logging
import csv
import os
from datetime import datetime
from typing import List, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WeatherStation")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

CSV_FILE = "weather_history.csv"

# --- ГЛОБАЛЬНІ НАЛАШТУВАННЯ ---
SYSTEM_CONFIG = {
    "update_interval": 3.0 # Початкова швидкість оновлення (сек)
}

THRESHOLDS = {
    "temp": {"max": 30, "min": -5},
    "wind": {"max": 15},
    "uv": {"max": 8},
    "air": {"max": 100}
}

# --- Моделі даних ---
class IntervalUpdate(BaseModel):
    interval: float

def log_to_csv(sensor_id, name, value, unit):
    file_exists = os.path.isfile(CSV_FILE)
    try:
        with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(["Time", "Sensor ID", "Name", "Value", "Unit"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sensor_id, name, value, unit])
    except Exception as e:
        logger.error(f" помилка запису CSV: {e}")

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        await self.broadcast_log("SYSTEM", f"Клієнт підключено. Інтервал оновлення: {SYSTEM_CONFIG['update_interval']}с")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try: await connection.send_text(message)
            except: pass

    async def broadcast_log(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        packet = {"type": "log", "time": timestamp, "level": level, "message": msg}
        await self.broadcast(json.dumps(packet))

manager = ConnectionManager()

class SensorWorker:
    def __init__(self, sensor_id, name, unit, min_val, max_val):
        self.id = sensor_id
        self.name = name
        self.unit = unit
        self.min_val = min_val
        self.max_val = max_val
        self.is_running = True
        self.current_value = 0

    async def run(self):
        await manager.broadcast_log("INFO", f"Сенсор '{self.name}' готовий.")
        while True:
            if self.is_running:
                self.current_value = round(random.uniform(self.min_val, self.max_val), 1)
                log_to_csv(self.id, self.name, self.current_value, self.unit)
                
                # Перевірка лімітів
                limits = THRESHOLDS.get(self.id)
                if limits:
                    if self.current_value > limits.get("max", 999):
                        await manager.broadcast_log("WARNING", f"HIGH LIMIT: {self.name} {self.current_value}")
                    elif self.current_value < limits.get("min", -999):
                        await manager.broadcast_log("WARNING", f"LOW LIMIT: {self.name} {self.current_value}")

                data = {
                    "type": "data",
                    "sensor_id": self.id,
                    "name": self.name,
                    "value": self.current_value,
                    "unit": self.unit,
                    "timestamp": datetime.now().strftime("%H:%M:%S")
                }
                await manager.broadcast(json.dumps(data))
            
            # ВИКОРИСТОВУЄМО ГЛОБАЛЬНУ ЗМІННУ ІНТЕРВАЛУ
            await asyncio.sleep(SYSTEM_CONFIG["update_interval"])

    async def start(self):
        if not self.is_running:
            self.is_running = True
            await manager.broadcast_log("INFO", f"Сенсор '{self.name}' стартував.")

    async def stop(self):
        if self.is_running:
            self.is_running = False
            await manager.broadcast_log("INFO", f"Сенсор '{self.name}' зупинено.")

sensors_list = [
    SensorWorker("temp", "Температура", "°C", -10, 35),
    SensorWorker("humid", "Вологість", "%", 20, 90),
    SensorWorker("wind", "Швидкість вітру", "м/с", 0, 25),
    SensorWorker("press", "Тиск", "hPa", 980, 1050),
    SensorWorker("uv", "УФ-індекс", "idx", 0, 11),
    SensorWorker("rain", "Рівень опадів", "мм", 0, 50),
    SensorWorker("air", "Якість повітря", "AQI", 0, 150),
]
sensors_map = {s.id: s for s in sensors_list}

@app.on_event("startup")
async def startup_event():
    for sensor in sensors_list:
        asyncio.create_task(sensor.run())

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def get(request: Request):
    initial_state = {s.id: {"name": s.name, "unit": s.unit, "active": s.is_running} for s in sensors_list}
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "sensors": initial_state,
        "current_interval": SYSTEM_CONFIG["update_interval"] # Передаємо поточний інтервал
    })

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect: manager.disconnect(websocket)

@app.get("/history")
async def get_history_data():
    if not os.path.exists(CSV_FILE): return []
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))[-50:][::-1]
    except: return []

@app.get("/download-log")
async def download_log():
    if os.path.exists(CSV_FILE):
        return FileResponse(CSV_FILE, media_type='text/csv', filename="weather_log.csv")
    return {"error": "Log file not found"}

@app.delete("/clear-history")
async def clear_history():
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(["Time", "Sensor ID", "Name", "Value", "Unit"])
    return {"status": "cleared"}

# --- НОВИЙ API ДЛЯ ЗМІНИ ШВИДКОСТІ ---
@app.post("/set-interval")
async def set_interval(update: IntervalUpdate):
    new_interval = max(0.5, min(10.0, update.interval)) # Обмеження від 0.5с до 10с
    SYSTEM_CONFIG["update_interval"] = new_interval
    await manager.broadcast_log("SYSTEM", f"Швидкість оновлення змінено на {new_interval} сек")
    return {"status": "updated", "interval": new_interval}

@app.post("/start-sensor/{sensor_id}")
async def start_sensor(sensor_id: str):
    if sensor_id in sensors_map: await sensors_map[sensor_id].start()

@app.post("/stop-sensor/{sensor_id}")
async def stop_sensor(sensor_id: str):
    if sensor_id in sensors_map: await sensors_map[sensor_id].stop()

@app.post("/start-all")
async def start_all():
    for sensor in sensors_list: await sensor.start()

@app.post("/stop-all")
async def stop_all():
    for sensor in sensors_list: await sensor.stop()