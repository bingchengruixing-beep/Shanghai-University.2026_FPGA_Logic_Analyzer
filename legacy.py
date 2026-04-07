import sys
import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QComboBox, QLabel, QMessageBox
from PyQt5.QtCore import QThread, pyqtSignal
import serial
import serial.tools.list_ports

# ================= 1. 后台串口接收与状态机线程 =================
class SerialThread(QThread):
    # 定义信号：将解析好的 8x2048 Numpy二维数组传给主UI线程
    data_received = pyqtSignal(np.ndarray)

    def __init__(self, port='COM4', baudrate=115200):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.running = True

    def run(self):
        try:
            ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
            buffer = bytearray()

            while self.running:
                if ser.in_waiting:
                    buffer.extend(ser.read(ser.in_waiting))

                    # 状态机：寻找帧头并提取 2051 字节 (2头 + 2048数据 + 1校验和)
                    while len(buffer) >= 2051:
                        if buffer[0] == 0x5A and buffer[1] == 0xA5:
                            frame = buffer[2:2050]
                            checksum = buffer[2050]

                            # 校验和比对 (与运算截取低8位)
                            if sum(frame) & 0xFF == checksum:
                                # 【极速解包核心】: 将 2048 字节拆解为 8 个通道的 0/1 序列
                                raw_array = np.frombuffer(frame, dtype=np.uint8)
                                # unpackbits将字节拆成位，[::-1]反转使得Bit0在最前，.T进行转置
                                bits_matrix = np.unpackbits(raw_array.reshape(-1, 1), axis=1)[:, ::-1].T

                                # 将合规数据发射给 UI 线程
                                self.data_received.emit(bits_matrix)

                            # 无论校验是否成功，均丢弃已处理的这段数据
                            buffer = buffer[2051:]
                        else:
                            buffer.pop(0)  # 剔除无效错位字节
        except Exception as e:
            print(f"串口异常或未连接: {e}")

    def stop(self):
        self.running = False
        self.wait()


# ================= 2. 主UI界面与波形渲染 =================
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QPushButton
import pyqtgraph as pg
import numpy as np
import sys


# ... [保留原有的 SerialThread 类代码不变] ...

class LogicAnalyzerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("简易逻辑分析仪 - 串口动态选择版")
        self.resize(1200, 700)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # ================= 左侧控制面板 =================
        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)
        control_panel.setMaximumWidth(200)

        # 【新增】：串口扫描与选择UI
        self.lbl_port = QLabel("选择通信串口:")
        self.cb_ports = QComboBox()
        self.cb_ports.setMinimumHeight(30)
        self.btn_refresh_ports = QPushButton("刷新可用串口")
        self.btn_refresh_ports.setMinimumHeight(30)

        control_layout.addWidget(self.lbl_port)
        control_layout.addWidget(self.cb_ports)
        control_layout.addWidget(self.btn_refresh_ports)
        control_layout.addSpacing(20) # 增加垂直间距隔离不同功能区

        # 现有的启停按钮
        self.btn_start = QPushButton("开始接收")
        self.btn_start.setMinimumHeight(40)
        self.btn_stop = QPushButton("暂停接收")
        self.btn_stop.setMinimumHeight(40)
        self.btn_stop.setEnabled(False)

        control_layout.addWidget(self.btn_start)
        control_layout.addWidget(self.btn_stop)
        control_layout.addStretch() # 弹簧，将所有组件顶在上半部

        # 绑定事件
        self.btn_refresh_ports.clicked.connect(self.scan_ports)
        self.btn_start.clicked.connect(self.start_hardware_link)
        self.btn_stop.clicked.connect(self.stop_hardware_link)

        # ================= 右侧波形显示区 =================
        pg.setConfigOptions(antialias=False)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel('bottom', 'Samples (n)')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        self.plot_widget.setYRange(0, 16, padding=0)
        self.plot_widget.setXRange(0, 2048, padding=0)
        self.plot_widget.disableAutoRange(axis=pg.ViewBox.YAxis)

        y_axis = self.plot_widget.getAxis('left')
        ticks = [(i * 2 + 0.5, f"CH{i}") for i in range(8)]
        y_axis.setTicks([ticks])

        self.curves = []
        for i in range(8):
            color = pg.intColor(i, hues=8, alpha=255)
            curve = self.plot_widget.plot(pen=pg.mkPen(color=color, width=1.5), name=f"CH{i}")
            self.curves.append(curve)

        main_layout.addWidget(control_panel, 1)
        main_layout.addWidget(self.plot_widget, 6)

        self.serial_thread = None

        # 界面初始化时，自动执行一次扫描
        self.scan_ports()

    # 【新增】：硬件枚举核心逻辑
    def scan_ports(self):
        self.cb_ports.clear()
        # 调用操作系统底层API获取端口对象列表
        ports = serial.tools.list_ports.comports()
        for port in ports:
            self.cb_ports.addItem(port.device) # device属性即为 'COM3', 'COM4' 等纯文本

        if self.cb_ports.count() == 0:
            self.cb_ports.addItem("无可用硬件串口")

    def start_hardware_link(self):
        # 获取用户当前在下拉框中选中的文本
        selected_port = self.cb_ports.currentText()

        # 异常拦截：防止选取无效端口
        if selected_port == "无可用硬件串口" or not selected_port:
            QMessageBox.warning(self, "硬件异常", "未检测到或未选择有效的FPGA通信串口！")
            return

        # 锁定控制组件，防止运行时误切换
        self.cb_ports.setEnabled(False)
        self.btn_refresh_ports.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # 动态实例化串口线程
        self.serial_thread = SerialThread(port=selected_port, baudrate=115200)
        self.serial_thread.data_received.connect(self.update_plot)
        self.serial_thread.start()

    def stop_hardware_link(self):
        # 释放控制组件锁定
        self.cb_ports.setEnabled(True)
        self.btn_refresh_ports.setEnabled(True)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

        if self.serial_thread is not None:
            self.serial_thread.stop()
            self.serial_thread = None

    def update_plot(self, bit_matrix):
        for ch in range(8):
            y_data = bit_matrix[ch] + (ch * 2.0)
            x_data = np.arange(len(y_data) + 1)
            self.curves[ch].setData(x=x_data, y=y_data, stepMode="center")

    def closeEvent(self, event):
        self.stop_hardware_link()
        event.accept()


# ... [保留原本的 if __name__ == '__main__': 块] ...


if __name__ == '__main__':
    app = QApplication(sys.argv)
    gui = LogicAnalyzerGUI()
    gui.show()
    sys.exit(app.exec_())