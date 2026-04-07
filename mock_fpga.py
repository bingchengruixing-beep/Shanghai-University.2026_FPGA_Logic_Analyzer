import serial
import time
import numpy as np

# 注意：请使用 VSPD 等软件创建 COM2 和 COM3 的互联虚拟串口对
# 本脚本连接 COM2 发送，您的 Web 后端连接 COM3 接收
MOCK_PORT = 'COM2'
BAUDRATE = 115200


def generate_mock_frame():
    # 初始化 2048 字节全 0 数组
    data = np.zeros(2048, dtype=np.uint8)

    # 模拟 CH0：每 50 个样本翻转一次 (周期 = 100 样本)
    ch0_wave = (np.arange(2048) // 50) % 2
    # 模拟 CH1：每 20 个样本翻转一次 (周期 = 40 样本)
    ch1_wave = (np.arange(2048) // 20) % 2

    # 按位拼装到数据字节中 (CH0 占 Bit0, CH1 占 Bit1)
    data |= (ch0_wave << 0).astype(np.uint8)
    data |= (ch1_wave << 1).astype(np.uint8)

    # 构建完整帧
    frame = bytearray([0x5A, 0xA5])
    frame.extend(data.tobytes())
    checksum = sum(frame[2:2050]) & 0xFF
    frame.append(checksum)
    return frame


if __name__ == '__main__':
    try:
        ser = serial.Serial(MOCK_PORT, BAUDRATE)
        print(f"FPGA 模拟器已挂载至 {MOCK_PORT}，正在发送标准 2051 字节波形帧...")
        while True:
            frame = generate_mock_frame()
            ser.write(frame)
            # 115200 bps 下发 2051 字节大约需要 0.178 秒
            time.sleep(0.18)
    except Exception as e:
        print(f"模拟器启动失败，请检查端口: {e}")
