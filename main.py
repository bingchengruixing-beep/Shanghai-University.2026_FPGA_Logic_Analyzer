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
import time

app = FastAPI(title="Logic Analyzer Backend")

# 允许跨域请求（前端独立运行必备）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局状态字典（已扩容“软件阀门”标志位）
state = {
    "ser": None,
    "running": False,
    "thread": None,
    "loop": None,
    "clients": set(),
    "software_valve_open": False  # 【新增】：决定数据是推给前端，还是被吸入黑洞
}


class ControlParams(BaseModel):
    cmd: int
    div: int
    mode: int
    p1: int
    p2: int

# ================= 底层硬件读取线程 (重构为永不停歇的食尸鬼线程) =================
def serial_reader_task():
    buffer = bytearray()
    ser = state["ser"]

    while state["running"] and ser and ser.is_open:
        try:
            # 【核心改造 1：数据黑洞机制】
            if not state["software_valve_open"]:
                # 阀门关闭时：只要 FPGA 还在吐数据，就疯狂读取并直接丢弃，严防 OS 缓冲区爆满死锁
                waiting = ser.in_waiting
                if waiting > 0:
                    ser.read(waiting)
                    buffer.clear() # 同步清空内存残留
                time.sleep(0.01)
                continue

            # 【核心改造 2：特征码绝对同步与滑窗对齐】
            # 阀门开启时，接管数据并尝试解包
            waiting = ser.in_waiting
            if waiting > 0:
                buffer.extend(ser.read(waiting))

            # 假设一帧为: 2字节头(5A A5) + 2048字节数据 + 1字节校验和 = 2051字节
            while len(buffer) >= 2051:
                # 严格寻找帧头
                if buffer[0] == 0x5A and buffer[1] == 0xA5:
                    frame_data = buffer[2:2050]
                    checksum = buffer[2050]

                    # 校验和逻辑：对 2048 个数据字节求和取低 8 位
                    if sum(frame_data) & 0xFF == checksum:
                        # 校验通过：NumPy 极速解包
                        raw_array = np.frombuffer(frame_data, dtype=np.uint8)
                        bits_matrix = np.unpackbits(raw_array.reshape(-1, 1), axis=1)[:, ::-1].T
                        data_list = bits_matrix.tolist()

                        # 安全投递到前端
                        if state["loop"] and state["clients"]:
                            asyncio.run_coroutine_threadsafe(
                                broadcast_waveform(data_list),
                                state["loop"]
                            )
                        # 推进缓冲池（切掉已处理的整帧）
                        buffer = buffer[2051:]
                    else:
                        # 找到了 5A A5，但校验和不对（伪帧头或丢包），丢掉首字节，继续滑窗
                        buffer.pop(0)
                else:
                    # 没对齐帧头，弹出一个字节，继续滑动窗口
                    buffer.pop(0)

            # 防止 CPU 空转
            if len(buffer) < 2051:
                time.sleep(0.005)

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
        state["software_valve_open"] = False # 连接时默认关闭阀门
        state["thread"] = threading.Thread(target=serial_reader_task, daemon=True)
        state["thread"].start()
        return {"status": "success", "msg": f"成功挂载硬件: {port} @ {baudrate}bps"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.post("/api/disconnect")
def disconnect_hardware():
    state["running"] = False
    state["software_valve_open"] = False
    if state["ser"]:
        state["ser"].close()
        state["ser"] = None
    return {"status": "success", "msg": "硬件连接已安全切断"}


@app.post("/api/control")
def send_control_frame(params: ControlParams):
    ser = state["ser"]
    if not ser or not ser.is_open:
        return {"status": "error", "msg": "硬件未连接"}

    try:
        # 【核心改造 3：软件阀门控制与陈旧数据冲刷】
        if params.cmd == 1:
            # 启动命令：彻底清空操作系统底层积攒的“旧水”，确保画出来的是当下最新的波形
            ser.reset_input_buffer()
            state["software_valve_open"] = True
        elif params.cmd == 2:
            # 中止命令：关阀
            state["software_valve_open"] = False

        cmd_byte = params.cmd & 0xFF
        div_byte = params.div & 0xFF
        mode_byte = params.mode & 0xFF
        p1_byte = params.p1 & 0xFF
        p2_byte = params.p2 & 0xFF

        frame = bytearray([0xAA, 0x55, cmd_byte, div_byte, mode_byte, p1_byte, p2_byte])
        checksum = sum(frame) & 0xFF
        frame.append(checksum)

        ser.write(frame)

        hex_str = ' '.join([f"{b:02X}" for b in frame])
        print(f"[{time.strftime('%H:%M:%S')}] 向上位机下发控制指令: {hex_str} | 阀门状态: {state['software_valve_open']}")

        return {"status": "success", "msg": f"指令已发送: {hex_str}"}

    except Exception as e:
        return {"status": "error", "msg": f"指令组装或发送引发异常: {str(e)}"}


@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state["clients"].add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        state["clients"].discard(websocket)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)