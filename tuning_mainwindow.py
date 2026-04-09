"""
tuning_mainwindow.py
====================
ALL hardware logic is here in Python — SPI open/ioctl, mmap FPGA,
DAC write, ADC read, register read/write, sweep loop, plot, log file.

C++ wrapper has ZERO hardware code. It only provides the Qt window shell.

Python does:
  - open("/dev/spidev2.1")  → DAC  (LTC2604)
  - open("/dev/spidev2.0")  → ADC  (ADS7841)
  - open("/dev/mem") + mmap → FPGA registers
  - All ioctl SPI transfers
  - All register writes (tune, freq_count, phaco_on/off)
  - Full sweep loop, bin averaging, log file, plot
"""

import sys
import os
import ctypes
import ctypes.util
import struct
import time
import mmap
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QPushButton, QLineEdit, QLabel,
    QVBoxLayout, QHBoxLayout, QFrame,
    QDialog, QGridLayout, QSizePolicy,
    QMessageBox
)
from PyQt5.QtCore import Qt, QCoreApplication, QTime
from PyQt5.QtGui import QColor

import matplotlib
matplotlib.use("Qt5Agg")
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

# ═══════════════════════════════════════════════════════════════════════════
#  CONSTANTS  — exact mirror of mainwindow.h / mainwindow.cpp
# ═══════════════════════════════════════════════════════════════════════════

LTC2604_DEVICE = "/dev/spidev2.1"   # DAC
ADS7841_DEVICE = "/dev/spidev2.0"   # ADC

SPI_MODE  = 0
SPI_BITS  = 8
SPI_SPEED = 1000000   # 1 MHz

# ioctl request codes — from linux/spi/spidev.h
SPI_IOC_WR_MODE          = 0x40016b01
SPI_IOC_WR_BITS_PER_WORD = 0x40016b03
SPI_IOC_WR_MAX_SPEED_HZ  = 0x40046b04
SPI_IOC_MESSAGE_1        = 0x40206b00   # SPI_IOC_MESSAGE(1)

# FPGA
XPAR_AXI_ADITHYA_0_BASEADDR = 0x43C00000
MAP_SIZE = 4096
MAP_MASK = MAP_SIZE - 1

# FPGA register byte offsets
REG_PHACO_ONOFF   = 0x00
REG_TUNE_REQ      = 0x02
REG_FREQ_COUNT    = 0x04
TUNE_REQUEST_MASK = 0x8000

# ADC
ADC_FULLSCALE            = 4095
ADS7841_FS_CH            = 0x97
ADS7841_PHACOSENSOR_CH   = 0xA7
ADS7841_SENSOR_CH        = 0xD7
ADS7841_VOLTAGESENSOR_CH = 0xE7

BIN_WIDTH = 0.3   # kHz


# ═══════════════════════════════════════════════════════════════════════════
#  spi_ioc_transfer struct — mirrors struct spi_ioc_transfer in spidev.h
# ═══════════════════════════════════════════════════════════════════════════
class SpiIocTransfer(ctypes.Structure):
    _fields_ = [
        ("tx_buf",           ctypes.c_uint64),
        ("rx_buf",           ctypes.c_uint64),
        ("len",              ctypes.c_uint32),
        ("speed_hz",         ctypes.c_uint32),
        ("delay_usecs",      ctypes.c_uint16),
        ("bits_per_word",    ctypes.c_uint8),
        ("cs_change",        ctypes.c_uint8),
        ("tx_nbits",         ctypes.c_uint8),
        ("rx_nbits",         ctypes.c_uint8),
        ("word_delay_usecs", ctypes.c_uint8),
        ("pad",              ctypes.c_uint8),
    ]


# ═══════════════════════════════════════════════════════════════════════════
#  HWBridge — ALL hardware logic lives here in Python
#
#  Method-by-method mapping to original mainwindow.cpp:
#
#  _open_dac_spi()   → constructor: open spidev2.1 + 3x ioctl
#  _open_adc_spi()   → constructor: open spidev2.0 + 3x ioctl
#  _mmap_fpga()      → constructor: open /dev/mem + mmap
#  write_reg()       → MainWindow::write_reg()
#  read_reg()        → MainWindow::read_reg()
#  write_dac()       → MainWindow::write_dac()
#  read_adc()        → MainWindow::read_adc()
#  phaco_power()     → MainWindow::phaco_power()
#  emit_tune_start() → MainWindow::emitTuneStart()
#  emit_tune_stop()  → MainWindow::emitTuneStop()
#  freq_count()      → MainWindow::freq_count()
#  phaco_off()       → MainWindow::phaco_off()
#  destroy()         → MainWindow::~MainWindow()
# ═══════════════════════════════════════════════════════════════════════════
class HWBridge:

    def __init__(self):
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

        self.fd_ltc2604   = -1
        self.fd_ads7841   = -1
        self.memfd        = -1
        self._mmap_obj    = None
        self._page_offset = 0

        self._open_dac_spi()
        self._open_adc_spi()
        self._mmap_fpga()

    # ───────────────────────────────────────────────────────────────────────
    #  C++: fd_ltc2604 = ::open(ltc2604_device, O_RDWR);
    #       ioctl(fd_ltc2604, SPI_IOC_WR_MODE, &spi_mode);
    #       ioctl(fd_ltc2604, SPI_IOC_WR_BITS_PER_WORD, &spi_bits);
    #       ioctl(fd_ltc2604, SPI_IOC_WR_MAX_SPEED_HZ,  &spi_speed);
    # ───────────────────────────────────────────────────────────────────────
    def _open_dac_spi(self):
        try:
            self.fd_ltc2604 = os.open(LTC2604_DEVICE, os.O_RDWR)
            mode  = ctypes.c_uint8(SPI_MODE)
            bits  = ctypes.c_uint8(SPI_BITS)
            speed = ctypes.c_uint32(SPI_SPEED)
            self._libc.ioctl(self.fd_ltc2604, SPI_IOC_WR_MODE,
                             ctypes.byref(mode))
            self._libc.ioctl(self.fd_ltc2604, SPI_IOC_WR_BITS_PER_WORD,
                             ctypes.byref(bits))
            self._libc.ioctl(self.fd_ltc2604, SPI_IOC_WR_MAX_SPEED_HZ,
                             ctypes.byref(speed))
            print("LTC2604 DAC SPI OK")
        except OSError as e:
            print(f"ERROR: Cannot open {LTC2604_DEVICE}: {e}")
            self.fd_ltc2604 = -1

    # ───────────────────────────────────────────────────────────────────────
    #  C++: fd_ads7841 = ::open(ads7841_device, O_RDWR);
    #       ioctl(fd_ads7841, SPI_IOC_WR_MODE, &spi_mode);  ... same 3x
    # ───────────────────────────────────────────────────────────────────────
    def _open_adc_spi(self):
        try:
            self.fd_ads7841 = os.open(ADS7841_DEVICE, os.O_RDWR)
            mode  = ctypes.c_uint8(SPI_MODE)
            bits  = ctypes.c_uint8(SPI_BITS)
            speed = ctypes.c_uint32(SPI_SPEED)
            self._libc.ioctl(self.fd_ads7841, SPI_IOC_WR_MODE,
                             ctypes.byref(mode))
            self._libc.ioctl(self.fd_ads7841, SPI_IOC_WR_BITS_PER_WORD,
                             ctypes.byref(bits))
            self._libc.ioctl(self.fd_ads7841, SPI_IOC_WR_MAX_SPEED_HZ,
                             ctypes.byref(speed))
            print("ADS7841 ADC SPI OK")
        except OSError as e:
            print(f"ERROR: Cannot open {ADS7841_DEVICE}: {e}")
            self.fd_ads7841 = -1

    # ───────────────────────────────────────────────────────────────────────
    #  C++: memfd = ::open("/dev/mem", O_RDWR|O_SYNC);
    #       mapped_base = mmap(nullptr, MAP_SIZE, PROT_READ|PROT_WRITE,
    #                          MAP_SHARED, memfd, dev_base & ~MAP_MASK);
    #       mapped_dev_base = (uint8_t*)mapped_base + (dev_base & MAP_MASK);
    # ───────────────────────────────────────────────────────────────────────
    def _mmap_fpga(self):
        try:
            self.memfd      = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
            page_addr       = XPAR_AXI_ADITHYA_0_BASEADDR & ~MAP_MASK
            self._page_offset = XPAR_AXI_ADITHYA_0_BASEADDR & MAP_MASK
            self._mmap_obj  = mmap.mmap(
                self.memfd, MAP_SIZE,
                mmap.MAP_SHARED,
                mmap.PROT_READ | mmap.PROT_WRITE,
                offset=page_addr
            )
            print("FPGA mmap OK")
        except OSError as e:
            print(f"ERROR: mmap FPGA failed: {e}")
            self._mmap_obj    = None
            self._page_offset = 0

    # ───────────────────────────────────────────────────────────────────────
    #  C++: *reinterpret_cast<volatile uint16_t*>(mapped_dev_base+offset) = val
    # ───────────────────────────────────────────────────────────────────────
    def write_reg(self, byte_offset: int, val: int):
        if self._mmap_obj is None:
            return
        self._mmap_obj.seek(self._page_offset + byte_offset)
        self._mmap_obj.write(struct.pack("<H", val & 0xFFFF))

    # ───────────────────────────────────────────────────────────────────────
    #  C++: return *reinterpret_cast<volatile uint16_t*>(mapped_dev_base+offset)
    # ───────────────────────────────────────────────────────────────────────
    def read_reg(self, byte_offset: int) -> int:
        if self._mmap_obj is None:
            return 0
        self._mmap_obj.seek(self._page_offset + byte_offset)
        return struct.unpack("<H", self._mmap_obj.read(2))[0]

    # ───────────────────────────────────────────────────────────────────────
    #  C++: buf[4] = {0x00, 0x30, hi, lo}; ::write(fd_ltc2604, buf, 4)
    # ───────────────────────────────────────────────────────────────────────
    def write_dac(self, value: int):
        if self.fd_ltc2604 < 0:
            return
        hi  = (value >> 8) & 0xFF
        lo  =  value       & 0xFF
        os.write(self.fd_ltc2604, bytes([0x00, 0x30, hi, lo]))

    # ───────────────────────────────────────────────────────────────────────
    #  C++: SPI_IOC_MESSAGE(1) full-duplex 3-byte transfer
    #       result = ((rx[1]<<8)|rx[2]) >> 3
    # ───────────────────────────────────────────────────────────────────────
    def read_adc(self, channel: int) -> int:
        if self.fd_ads7841 < 0:
            return 0
        tx = (ctypes.c_uint8 * 3)(channel, 0x00, 0x00)
        rx = (ctypes.c_uint8 * 3)(0, 0, 0)
        tr = SpiIocTransfer()
        tr.tx_buf        = ctypes.cast(tx, ctypes.c_void_p).value
        tr.rx_buf        = ctypes.cast(rx, ctypes.c_void_p).value
        tr.len           = 3
        tr.speed_hz      = SPI_SPEED
        tr.bits_per_word = SPI_BITS
        tr.cs_change     = 1
        self._libc.ioctl(self.fd_ads7841, SPI_IOC_MESSAGE_1, ctypes.byref(tr))
        result = ((rx[1] << 8) | rx[2]) >> 3
        return min(result, ADC_FULLSCALE)

    # ───────────────────────────────────────────────────────────────────────
    #  C++: dacVal = 39321.0 + percent*249.03; write_dac(dacVal)
    # ───────────────────────────────────────────────────────────────────────
    def phaco_power(self, percent: int):
        percent = max(0, min(100, percent))
        dac_val = int(39321.0 + percent * 249.03)
        self.write_dac(min(dac_val, 64224))

    # C++: write_reg(REG_TUNE_REQ, TUNE_REQUEST_MASK)
    def emit_tune_start(self):
        self.write_reg(REG_TUNE_REQ, TUNE_REQUEST_MASK)

    # C++: write_reg(REG_TUNE_REQ, 0x0000)
    def emit_tune_stop(self):
        self.write_reg(REG_TUNE_REQ, 0x0000)

    # C++: write_reg(REG_FREQ_COUNT, cnt)
    def freq_count(self, cnt: int):
        self.write_reg(REG_FREQ_COUNT, cnt)

    # C++: write_reg(REG_PHACO_ONOFF,0); write_reg(REG_FREQ_COUNT,0)
    def phaco_off(self):
        self.write_reg(REG_PHACO_ONOFF, 0x0000)
        self.write_reg(REG_FREQ_COUNT,  0x0000)

    # C++: ~MainWindow() — close fds, munmap
    def destroy(self):
        self.phaco_off()
        if self.fd_ltc2604 >= 0:
            os.close(self.fd_ltc2604);  self.fd_ltc2604 = -1
        if self.fd_ads7841 >= 0:
            os.close(self.fd_ads7841);  self.fd_ads7841 = -1
        if self._mmap_obj:
            self._mmap_obj.close();     self._mmap_obj  = None
        if self.memfd >= 0:
            os.close(self.memfd);       self.memfd      = -1


# ═══════════════════════════════════════════════════════════════════════════
#  KeypadDialog — mirrors keypaddialog.cpp
# ═══════════════════════════════════════════════════════════════════════════
class KeypadDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Enter Value")
        self.setFixedSize(260, 320)
        self._value = ""
        layout = QVBoxLayout(self)
        self._display = QLineEdit()
        self._display.setReadOnly(True)
        self._display.setAlignment(Qt.AlignRight)
        self._display.setStyleSheet("font-size:22px; padding:4px;")
        layout.addWidget(self._display)
        grid = QGridLayout()
        for label, row, col in [
            ("7",0,0),("8",0,1),("9",0,2),
            ("4",1,0),("5",1,1),("6",1,2),
            ("1",2,0),("2",2,1),("3",2,2),
            (".",3,0),("0",3,1),("⌫",3,2),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(50)
            btn.setStyleSheet("font-size:18px;")
            btn.clicked.connect(lambda _, l=label: self._on_key(l))
            grid.addWidget(btn, row, col)
        ok = QPushButton("OK")
        ok.setFixedHeight(50)
        ok.setStyleSheet("font-size:18px; background:#4CAF50; color:white;")
        ok.clicked.connect(self.accept)
        grid.addWidget(ok, 4, 0, 1, 3)
        layout.addLayout(grid)

    def _on_key(self, key):
        self._value = self._value[:-1] if key == "⌫" else self._value + key
        self._display.setText(self._value)

    def get_value(self):
        return self._value


# ═══════════════════════════════════════════════════════════════════════════
#  TuningWindow — mirrors C++ MainWindow class
# ═══════════════════════════════════════════════════════════════════════════
class TuningWindow(QMainWindow):

    def __init__(self, hw: HWBridge, parent=None):
        super().__init__(parent)
        self.hw = hw

        self.min_freq_khz         = 38.0
        self.max_freq_khz         = 44.0
        self.selected_adc_channel = ADS7841_PHACOSENSOR_CH
        self.m_sweep_running      = False
        self.data_points          = []
        self.curves               = []

        self.setWindowTitle("Phaco Tuning")
        self.setMinimumSize(900, 600)
        self._build_ui()

        # C++: if (fd_ltc2604 >= 0) write_dac(0xFFFF); phaco_off();
        self.hw.write_dac(0xFFFF)
        self.hw.phaco_off()
        print("Hardware ready.")

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Min Freq (kHz):"))
        self.line_min_freq = QLineEdit("38.0")
        self.line_min_freq.setReadOnly(True)
        self.line_min_freq.setFixedWidth(80)
        self.line_min_freq.mousePressEvent = lambda e: self._show_keypad_for_min()
        ctrl.addWidget(self.line_min_freq)

        ctrl.addWidget(QLabel("Max Freq (kHz):"))
        self.line_max_freq = QLineEdit("44.0")
        self.line_max_freq.setReadOnly(True)
        self.line_max_freq.setFixedWidth(80)
        self.line_max_freq.mousePressEvent = lambda e: self._show_keypad_for_max()
        ctrl.addWidget(self.line_max_freq)

        ctrl.addStretch()

        self.push_button = QPushButton("Start Sweep")
        self.push_button.setFixedWidth(120)
        self.push_button.clicked.connect(self.start_sweep)
        ctrl.addWidget(self.push_button)

        self.but_dac = QPushButton("HW Test")
        self.but_dac.setFixedWidth(100)
        self.but_dac.clicked.connect(self.on_but_dac_clicked)
        ctrl.addWidget(self.but_dac)

        self.but_clear = QPushButton("Clear")
        self.but_clear.setFixedWidth(80)
        self.but_clear.clicked.connect(self.on_but_clear_clicked)
        ctrl.addWidget(self.but_clear)

        root.addLayout(ctrl)

        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(5, 5, 5, 5)
        self.figure = Figure(figsize=(8, 4))
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        fl.addWidget(self.canvas)
        self.ax = self.figure.add_subplot(111)
        self._reset_axes()
        root.addWidget(frame)

    def _reset_axes(self):
        self.ax.set_title("Frequency vs Feedback")
        self.ax.set_xlabel("Frequency (kHz)")
        self.ax.set_ylabel("ADC Counts (0-4096)")
        self.ax.set_xlim(self.min_freq_khz, self.max_freq_khz)
        self.ax.set_ylim(0, 4096)
        self.canvas.draw()

    def _show_keypad_for_min(self):
        dlg = KeypadDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self.line_min_freq.setText(dlg.get_value())

    def _show_keypad_for_max(self):
        dlg = KeypadDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self.line_max_freq.setText(dlg.get_value())

    # ═══════════════════════════════════════════════════════════════════════
    #  start_sweep — exact Python mirror of C++ startSweep()
    # ═══════════════════════════════════════════════════════════════════════
    def start_sweep(self):
        if self.m_sweep_running:
            print("Sweep already running – ignoring extra click")
            return
        self.m_sweep_running = True
        self.push_button.setEnabled(False)
        print("\n=== START SWEEP ===")

        try:
            min_f = float(self.line_min_freq.text())
            max_f = float(self.line_max_freq.text())
        except ValueError:
            min_f, max_f = 0, 0

        if min_f <= 0 or max_f <= min_f:
            print("Invalid range – using 38–44 kHz")
            min_f, max_f = 38.0, 44.0

        self.min_freq_khz = min_f
        self.max_freq_khz = max_f
        self.ax.set_xlim(self.min_freq_khz, self.max_freq_khz)

        # C++: int countHigh = static_cast<int>(100000.0 / minFreqKHz);
        count_high = int(100000.0 / self.min_freq_khz)
        count_low  = int(100000.0 / self.max_freq_khz)
        print(f"countHigh={count_high}  countLow={count_low}")

        os.makedirs("/home/tune", exist_ok=True)
        file_name = f"/home/tune/tuning_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.dat"

        # C++: phaco_power(100); usleep(100); emitTuneStart(); phaco_power(100); usleep(100);
        self.hw.phaco_power(100)
        time.sleep(100e-6)
        self.hw.emit_tune_start()
        self.hw.phaco_power(100)
        time.sleep(100e-6)

        raw_samples = []
        self.data_points.clear()

        # C++: for (int iCount = countHigh; iCount >= countLow; --iCount)
        for i_count in range(count_high, count_low - 1, -1):
            self.hw.freq_count(i_count)
            time.sleep(0.1)                          # usleep(100000)

            total = 0
            for _ in range(25):                      # 25x average
                total += self.hw.read_adc(self.selected_adc_channel)

            raw_adc  = max(0, min(int(total / 25.0), ADC_FULLSCALE))
            freq_khz = 100000.0 / i_count
            raw_samples.append((freq_khz, raw_adc))
            print(f"cnt={i_count}  freq={freq_khz:.2f} kHz  ADC={raw_adc}")
            QCoreApplication.processEvents()

        # C++: emitTuneStop(); phaco_off(); phaco_power(0);
        self.hw.emit_tune_stop()
        self.hw.phaco_off()
        self.hw.phaco_power(0)

        # C++: QMap<int,QPair<long long,int>> binAccum — bin average
        bin_accum = {}
        for freq_khz, adc in raw_samples:
            idx = max(0, int((freq_khz - self.min_freq_khz) / BIN_WIDTH))
            if idx not in bin_accum:
                bin_accum[idx] = [0, 0]
            bin_accum[idx][0] += adc
            bin_accum[idx][1] += 1

        log_lines = []
        for idx in sorted(bin_accum.keys()):
            centre   = self.min_freq_khz + (idx + 0.5) * BIN_WIDTH
            mean_adc = int(bin_accum[idx][0] / bin_accum[idx][1])
            self.data_points.append((centre, mean_adc))
            log_lines.append(f"{centre:.3f}\t{mean_adc}")
            print(f"BIN centre={centre:.2f} kHz  meanADC={mean_adc}"
                  f"  (n={bin_accum[idx][1]})")

        with open(file_name, "w") as f:
            f.write("\n".join(log_lines) + "\n")
        print(f"Log saved: {file_name}")
        print(f"Raw steps: {len(raw_samples)}  Smoothed bins: {len(self.data_points)}")

        # C++: QColor::fromHsv((curves.size()*50)%360, 255, 200)
        if self.data_points:
            xs = [p[0] for p in self.data_points]
            ys = [p[1] for p in self.data_points]
            color = QColor.fromHsv((len(self.curves) * 50) % 360, 255, 200).name()
            line, = self.ax.plot(xs, ys, color=color, linewidth=2,
                                 marker="o", markersize=4,
                                 label=f"Run {len(self.curves)+1}")
            self.curves.append(line)
            self.ax.legend(loc="upper right", fontsize=8)

        self.canvas.draw()
        print("=== SWEEP COMPLETE ===\n")
        self.m_sweep_running = False
        self.push_button.setEnabled(True)

    # ═══════════════════════════════════════════════════════════════════════
    #  on_but_dac_clicked — mirrors C++ on_butDAC_clicked()
    # ═══════════════════════════════════════════════════════════════════════
    def on_but_dac_clicked(self):
        print("\n=== HARDWARE TEST ===")
        self.hw.phaco_power(100);  time.sleep(0.1)
        self.hw.emit_tune_start(); time.sleep(0.1)
        self.hw.freq_count(2500);  time.sleep(0.05)   # 100000/40kHz=2500
        print("40 kHz – all ADC channels (max=4095):")
        print(f"  FS   (0x97): {self.hw.read_adc(ADS7841_FS_CH)}")
        print(f"  Phaco(0xA7): {self.hw.read_adc(ADS7841_PHACOSENSOR_CH)}")
        print(f"  Sens (0xD7): {self.hw.read_adc(ADS7841_SENSOR_CH)}")
        print(f"  Volt (0xE7): {self.hw.read_adc(ADS7841_VOLTAGESENSOR_CH)}")
        self.hw.emit_tune_stop()
        self.hw.phaco_off()
        self.hw.phaco_power(0)
        print("=== TEST DONE ===\n")

    # ═══════════════════════════════════════════════════════════════════════
    #  on_but_clear_clicked — mirrors C++ on_butClear_clicked()
    # ═══════════════════════════════════════════════════════════════════════
    def on_but_clear_clicked(self):
        for line in self.curves:
            line.remove()
        self.curves.clear()
        self.data_points.clear()
        self.ax.legend_ = None
        self.canvas.draw()
        print("Plot cleared")

    # C++: ~MainWindow()
    def closeEvent(self, event):
        self.hw.phaco_off()
        self.hw.destroy()
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════
def main():
    app = QApplication(sys.argv)
    try:
        hw = HWBridge()
    except Exception as e:
        QMessageBox.critical(None, "Hardware Error", str(e))
        sys.exit(1)
    window = TuningWindow(hw)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
