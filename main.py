import asyncio
import threading
import serial
import serial.tools.list_ports
import numpy as np
import json
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Logic Analyzer Backend")

# 允许跨域请求（前端独立运行必备）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局状态字典
state = {
    "ser": None,
    "running": False,
    "thread": None,
    "loop": None,
    "clients": set()
}


class ControlParams(BaseModel):
    cmd: int
    div: int
    trig_mode: int
    trig_ch: int


# ================= 底层硬件读取线程 =================
def serial_reader_task():
    buffer = bytearray()
    ser = state["ser"]

    while state["running"] and ser and ser.is_open:
        try:
            if ser.in_waiting:
                buffer.extend(ser.read(ser.in_waiting))

                while len(buffer) >= 2051:
                    if buffer[0] == 0x5A and buffer[1] == 0xA5:
                        frame = buffer[2:2050]
                        checksum = buffer[2050]

                        if sum(frame) & 0xFF == checksum:
                            # NumPy 极速解包
                            raw_array = np.frombuffer(frame, dtype=np.uint8)
                            bits_matrix = np.unpackbits(raw_array.reshape(-1, 1), axis=1)[:, ::-1].T

                            # 转换为标准的 Python 嵌套列表以供 JSON 序列化
                            data_list = bits_matrix.tolist()

                            # 安全地跨线程投递到 FastAPI 的异步广播函数
                            if state["loop"] and state["clients"]:
                                asyncio.run_coroutine_threadsafe(
                                    broadcast_waveform(data_list),
                                    state["loop"]
                                )
                        # 推进缓冲池
                        buffer = buffer[2051:]
                    else:
                        buffer.pop(0)
        except Exception as e:
            print(f"串口读取致命错误: {e}")
            state["running"] = False
            break


async def broadcast_waveform(data_matrix):
    message = json.dumps({"type": "waveform", "data": data_matrix})
    dead_clients = set()
    for client in state["clients"]:
        try:
            await client.send_text(message)
        except:
            dead_clients.add(client)
    # 清理断开的连接
    for client in dead_clients:
        state["clients"].discard(client)


# ================= FastAPI 路由端点 =================
@app.on_event("startup")
async def startup_event():
    # 获取主线程的异步事件循环
    state["loop"] = asyncio.get_running_loop()


@app.get("/api/ports")
def get_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    return {"status": "success", "data": ports}


@app.post("/api/connect")
def connect_hardware(port: str, baudrate: int = 115200):
    if state["running"]:
        return {"status": "error", "msg": "系统已在运行中，请先断开。"}
    try:
        state["ser"] = serial.Serial(port, baudrate, timeout=0.1)
        state["running"] = True
        state["thread"] = threading.Thread(target=serial_reader_task, daemon=True)
        state["thread"].start()
        return {"status": "success", "msg": f"成功挂载硬件: {port} @ {baudrate}bps"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.post("/api/disconnect")
def disconnect_hardware():
    state["running"] = False
    if state["ser"]:
        state["ser"].close()
        state["ser"] = None
    return {"status": "success", "msg": "硬件连接已安全切断"}


@app.post("/api/control")
def send_control_frame(params: ControlParams):
    ser = state["ser"]
    if not ser or not ser.is_open:
        return {"status": "error", "msg": "硬件未连接"}

    # 组装 8 字节下行帧: AA 55 CMD DIV T_MODE T_CH RES CHK
    frame = bytearray([0xAA, 0x55, params.cmd, params.div, params.trig_mode, params.trig_ch, 0x00])
    checksum = sum(frame) & 0xFF
    frame.append(checksum)

    try:
        ser.write(frame)
        return {"status": "success", "msg": "指令下发成功"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state["clients"].add(websocket)
    try:
        while True:
            # 维持连接心跳
            await websocket.receive_text()
    except WebSocketDisconnect:
        state["clients"].discard(websocket)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)