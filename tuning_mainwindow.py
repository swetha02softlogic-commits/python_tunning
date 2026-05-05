#!/usr/bin/env python3

import os
import sys
import time
import signal
import threading
import ctypes
import ctypes.util
from datetime import datetime
from collections import defaultdict

from PyQt5.QtCore import Qt, QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QPushButton, QLabel, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTextEdit, QSpinBox
)
from PyQt5.QtGui import QColor, QPainter, QPixmap, QPen, QFont

# ─────────────────────────────────────────────
# EXIT HANDLER
# ─────────────────────────────────────────────
def handle_exit(sig, frame):
    print("\n[INFO] Exit")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)

# ── Lock framebuffer BEFORE Qt starts ──────────────────────────────────────
os.system("fbset -fb /dev/fb0 -yoffset 0 2>/dev/null")
os.environ["QT_QPA_PLATFORM"]         = "linuxfb:fb=/dev/fb0:noblit:nographicsmodeswitch"
os.environ["QT_QPA_FB_DISABLE_INPUT"] = "0"

try:
    open("/sys/class/graphics/fb0/blank", "w").write("0")
except Exception:
    pass

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
ADS7841_DEVICE = "/dev/spidev2.0"
LTC2604_DEVICE = "/dev/spidev2.1"

SPI_MODE  = 0
SPI_BITS  = 8
SPI_SPEED = 1000000

SPI_IOC_WR_MODE          = 0x40016b01
SPI_IOC_WR_BITS_PER_WORD = 0x40016b03
SPI_IOC_WR_MAX_SPEED_HZ  = 0x40046b04

ADC_FULLSCALE = 4095

ADS7841_PHACOSENSOR_CH = 0xA7

XPAR_BASE = 0x43C20000
MAP_SIZE  = 4096
MAP_MASK  = MAP_SIZE - 1

REG_PHACO_ONOFF = 0x00
REG_FS_COUNT    = 0x02
REG_PULSE_COUNT = 0x04
REG_PDM_MODE    = 0x06
REG_COLD_PULSE  = 0x0A
REG_FREQ_COUNT  = 0x0C
REG_TUNE_REQ    = 0x0E

TUNE_REQUEST_MASK = 0x8000

# Plot Y-axis ticks
Y_TICKS = [0, 1000, 2000, 3000, 4000, 4095]

# ─────────────────────────────────────────────
# SPI STRUCT
# ─────────────────────────────────────────────
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

def _spi_msg(n):
    sz = ctypes.sizeof(SpiIocTransfer) * n
    return (0x40000000 | ((sz & 0x3FFF) << 16) | (0x6b << 8) | 0)

SPI_IOC_MESSAGE_1 = _spi_msg(1)

# ─────────────────────────────────────────────
# HARDWARE
# ─────────────────────────────────────────────
class HWBridge:

    def __init__(self):
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

        self.fd_adc = self.open_spi(ADS7841_DEVICE)
        self.fd_dac = self.open_spi(LTC2604_DEVICE)

        self.memfd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.map_fpga()

        if self.fd_dac >= 0:
            self.write_dac(0xFFFF)

        self.phaco_off()
        print("[INIT] Hardware ready.")

    def open_spi(self, dev):
        try:
            fd = os.open(dev, os.O_RDWR)
            self.libc.ioctl(fd, SPI_IOC_WR_MODE,
                            ctypes.byref(ctypes.c_uint8(SPI_MODE)))
            self.libc.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD,
                            ctypes.byref(ctypes.c_uint8(SPI_BITS)))
            self.libc.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ,
                            ctypes.byref(ctypes.c_uint32(SPI_SPEED)))
            print("[SPI OK]", dev)
            return fd
        except Exception as e:
            print("[SPI FAIL]", dev, e)
            return -1

    def map_fpga(self):
        base   = XPAR_BASE
        page   = base & ~MAP_MASK
        offset = base & MAP_MASK

        self.libc.mmap.restype = ctypes.c_void_p
        mapped = self.libc.mmap(None, MAP_SIZE, 3, 1, self.memfd, page)
        self.base = mapped + offset
        print("[FPGA] mmap OK")

    def write_reg(self, off, val):
        ptr = ctypes.cast(self.base + off, ctypes.POINTER(ctypes.c_uint16))
        ptr[0] = val & 0xFFFF

    def read_reg(self, off):
        ptr = ctypes.cast(self.base + off, ctypes.POINTER(ctypes.c_uint16))
        return ptr[0]

    def write_dac(self, val):
        os.write(self.fd_dac,
                 bytes([0x00, 0x30, (val >> 8) & 0xFF, val & 0xFF]))

    def phaco_power(self, percent):
        if percent < 0:   percent = 0
        if percent > 100: percent = 100
        dac_val = int(39321.0 + percent * 249.03)
        if dac_val > 64224:
            dac_val = 64224
        self.write_dac(dac_val)

    def emit_tune_start(self):
        self.write_reg(REG_TUNE_REQ, TUNE_REQUEST_MASK)

    def emit_tune_stop(self):
        self.write_reg(REG_TUNE_REQ, 0x0000)

    def freq_count(self, cnt):
        self.write_reg(REG_FREQ_COUNT, cnt)

    def phaco_off(self):
        self.write_reg(REG_PHACO_ONOFF, 0x0000)
        self.write_reg(REG_FREQ_COUNT,  0x0000)

    def read_adc(self, channel=ADS7841_PHACOSENSOR_CH):
        tx = (ctypes.c_uint8 * 3)(channel, 0x00, 0x00)
        rx = (ctypes.c_uint8 * 3)()

        tr = SpiIocTransfer()
        tr.tx_buf        = ctypes.cast(tx, ctypes.c_void_p).value
        tr.rx_buf        = ctypes.cast(rx, ctypes.c_void_p).value
        tr.len           = 3
        tr.speed_hz      = SPI_SPEED
        tr.bits_per_word = SPI_BITS
        tr.cs_change     = 1

        self.libc.ioctl(self.fd_adc, SPI_IOC_MESSAGE_1, ctypes.byref(tr))

        result = ((rx[1] << 8) | rx[2]) >> 3
        if result > ADC_FULLSCALE:
            result = ADC_FULLSCALE
        return result

    def sweep(self, min_freq_khz=38.0, max_freq_khz=44.0,
              progress_cb=None, stop_event=None):

        count_high = int(100000.0 / min_freq_khz)
        count_low  = int(100000.0 / max_freq_khz)
        print(f"countHigh={count_high}  countLow={count_low}")

        os.makedirs("/home/tune", exist_ok=True)
        ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = f"/home/tune/tuning_{ts}.dat"

        self.phaco_power(100)
        time.sleep(100e-6)
        self.emit_tune_start()
        self.phaco_power(100)
        time.sleep(100e-6)

        raw_samples = []

        for i_count in range(count_high, count_low - 1, -1):
            if stop_event and stop_event.is_set():
                break

            self.freq_count(i_count)
            time.sleep(0.1)

            total = 0
            for _ in range(25):
                total += self.read_adc()

            raw_adc = int(total / 25.0)
            if raw_adc < 0:             raw_adc = 0
            if raw_adc > ADC_FULLSCALE: raw_adc = ADC_FULLSCALE

            freq_khz = 100000.0 / i_count
            raw_samples.append((freq_khz, raw_adc))

            if progress_cb:
                progress_cb(freq_khz, raw_adc)

            print(f"cnt={i_count}  freq={freq_khz:.2f} kHz  ADC={raw_adc}")

        self.emit_tune_stop()
        self.phaco_off()
        self.phaco_power(0)

        BIN_WIDTH = 0.3
        bin_accum = {}

        for (freq_khz, adc_val) in raw_samples:
            bin_idx = int((freq_khz - min_freq_khz) / BIN_WIDTH)
            if bin_idx < 0:
                bin_idx = 0
            if bin_idx not in bin_accum:
                bin_accum[bin_idx] = [0, 0]
            bin_accum[bin_idx][0] += adc_val
            bin_accum[bin_idx][1] += 1

        smoothed = []

        with open(log_path, "w") as f:
            for bin_idx in sorted(bin_accum.keys()):
                total_adc, n = bin_accum[bin_idx]
                bin_centre = min_freq_khz + (bin_idx + 0.5) * BIN_WIDTH
                mean_adc   = int(total_adc / n)
                smoothed.append((bin_centre, mean_adc))
                f.write(f"{bin_centre:.3f}\t{mean_adc}\n")

        print(f"Log saved: {log_path}")
        print(f"Raw steps: {len(raw_samples)}  Smoothed bins: {len(smoothed)}")
        return smoothed

    def destroy(self):
        try:
            self.phaco_off()
        except Exception:
            pass


# ─────────────────────────────────────────────
# PLOT WIDGET
# ─────────────────────────────────────────────
class PlotWidget(QWidget):
    COLORS = ["#00b4ff", "#ff643c", "#50dc50", "#ffdc28", "#dc50dc"]

    def __init__(self, x_label="Freq (kHz)"):
        super().__init__()
        self._curves  = []
        self._buf     = None
        self._dirty   = True
        self._x_min   = 38.0
        self._x_max   = 44.0
        self._x_label = x_label

    def set_x_range(self, x_min, x_max):
        if x_min >= x_max:
            return
        self._x_min = x_min
        self._x_max = x_max
        self._dirty = True
        self.update()

    def add_curve(self, xs, ys):
        smooth = []
        window = 2
        for i in range(len(ys)):
            s = max(0, i - window)
            e = min(len(ys), i + window + 1)
            smooth.append(sum(ys[s:e]) / (e - s))
        color = self.COLORS[len(self._curves) % len(self.COLORS)]
        self._curves.append((xs, smooth, color))
        self._dirty = True
        self.update()

    def clear(self):
        self._curves.clear()
        self._dirty = True
        self.update()

    def resizeEvent(self, e):
        self._dirty = True
        super().resizeEvent(e)

    def _redraw(self):
        W, H = self.width(), self.height()
        ML, MR, MT, MB = 60, 20, 30, 44
        self._buf = QPixmap(W, H)

        p = QPainter(self._buf)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(0, 0, W, H, QColor("#1a1a2e"))

        rx = ML; ry = MT
        rw = W - ML - MR; rh = H - MT - MB

        ymin, ymax   = 0, ADC_FULLSCALE
        xmin, xmax   = self._x_min, self._x_max
        num_x_ticks  = 7

        def y_to_px(val):
            return ry + rh - int((val - ymin) / (ymax - ymin) * rh)

        def x_to_px(val):
            if xmax == xmin:
                return rx
            return rx + int((val - xmin) / (xmax - xmin) * rw)

        # Grid
        p.setPen(QPen(QColor("#3c3c5a"), 1))
        for ytick in Y_TICKS:
            yp = y_to_px(ytick)
            p.drawLine(rx, yp, rx + rw, yp)
        for i in range(num_x_ticks):
            xp = rx + int(i * rw / (num_x_ticks - 1))
            p.drawLine(xp, ry, xp, ry + rh)

        p.setPen(QPen(QColor("#6478a0"), 1))
        p.drawRect(rx, ry, rw, rh)

        # Labels
        f8 = QFont(); f8.setPointSize(8)
        p.setFont(f8); p.setPen(QColor("#b4bee0"))

        for i in range(num_x_ticks):
            val = xmin + (xmax - xmin) * i / (num_x_ticks - 1)
            label = f"{val:.1f}"
            xp = rx + int(i * rw / (num_x_ticks - 1))
            p.drawText(xp - 14, ry + rh + 16, label)

        for ytick in Y_TICKS:
            yp = y_to_px(ytick)
            p.drawText(2, yp + 4, str(ytick))

        f9 = QFont(); f9.setPointSize(9); f9.setBold(True)
        p.setFont(f9); p.setPen(QColor("#c8d2e6"))
        p.drawText(rx + rw // 2 - 30, H - 4, self._x_label)

        p.save()
        p.translate(14, ry + rh // 2 + 50)
        p.rotate(-90)
        p.drawText(0, 0, "ADC Counts")
        p.restore()

        f10 = QFont(); f10.setPointSize(10); f10.setBold(True)
        p.setFont(f10); p.setPen(QColor("#dce6ff"))
        p.drawText(rx + rw // 2 - 80, MT - 8, "Frequency Sweep — ADC Response")

        # Curves
        p.setClipRect(rx, ry, rw, rh)
        for xs, ys, color in self._curves:
            if len(xs) < 2:
                continue
            p.setPen(QPen(QColor(color), 2))
            for i in range(len(xs) - 1):
                x1 = x_to_px(xs[i]);   x2 = x_to_px(xs[i + 1])
                y1 = y_to_px(ys[i]);   y2 = y_to_px(ys[i + 1])
                p.drawLine(x1, y1, x2, y1)
                p.drawLine(x2, y1, x2, y2)

        p.setClipping(False)
        p.end()
        self._dirty = False

    def paintEvent(self, e):
        if self._buf is None or self._dirty:
            self._redraw()
        QPainter(self).drawPixmap(0, 0, self._buf)


# ─────────────────────────────────────────────
# SIGNAL BRIDGE
# ─────────────────────────────────────────────
class SweepBridge(QObject):
    done     = pyqtSignal(object)
    progress = pyqtSignal(float, int)   # freq_khz, adc_val


# ─────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────
class Main(QMainWindow):

    STYLESHEET = """
        QMainWindow, QWidget { background:#12121f; color:#e0e0e0; }
        QLabel               { font-size:14px; }
        QPushButton          { background:#2a2a4a; color:white;
                               border:1px solid #4a4a7a; border-radius:6px;
                               padding:8px 16px; font-size:14px; }
        QPushButton:pressed  { background:#3a3a6a; }
        QPushButton:disabled { background:#1a1a2a; color:#555; }
        QLineEdit            { background:#2a2a4a; color:#e0e0e0;
                               border:1px solid #4a4a7a; border-radius:4px;
                               padding:4px 6px; font-size:13px;
                               min-width:70px; }
    """

    def __init__(self):
        super().__init__()
        self.hw            = HWBridge()
        self.sweep_running = False
        self.curve_count   = 0
        self.bridge        = SweepBridge()
        self.bridge.done.connect(self._on_sweep_done)
        self.bridge.progress.connect(self._on_progress)

        # live data buffers
        self._live_xs = []
        self._live_ys = []

        self.setWindowTitle("Phaco Tuning")
        self.setStyleSheet(self.STYLESHEET)
        self._build_ui()

    # ── build UI ──────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout()
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        # ── row 1 : status + peak ─────────────────────────────────────────
        row1 = QHBoxLayout()
        self.lbl_status = QLabel("Status: Ready")
        self.lbl_status.setStyleSheet("color:#80ff80;")
        row1.addWidget(self.lbl_status)
        row1.addStretch()
        self.lbl_peak = QLabel("Peak: —")
        self.lbl_peak.setStyleSheet("color:#ffdd60;")
        row1.addWidget(self.lbl_peak)
        root.addLayout(row1)

        # ── plot ──────────────────────────────────────────────────────────
        self.plot = PlotWidget(x_label="Freq (kHz)")
        root.addWidget(self.plot, stretch=1)

        # ── row 2 : controls ──────────────────────────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(12)

        row2.addWidget(QLabel("Min (kHz):"))
        self.min_freq_edit = QLineEdit("38.0")
        row2.addWidget(self.min_freq_edit)

        row2.addWidget(QLabel("Max (kHz):"))
        self.max_freq_edit = QLineEdit("44.0")
        row2.addWidget(self.max_freq_edit)

        btn_apply = QPushButton("Apply Range")
        btn_apply.clicked.connect(self._apply_range)
        row2.addWidget(btn_apply)

        row2.addSpacing(20)

        self.btn_sweep = QPushButton("▶  Start Sweep")
        self.btn_sweep.setStyleSheet(
            "background:#1a6a1a; border:1px solid #2a9a2a;"
            "border-radius:6px; padding:10px 20px; font-size:15px;")
        self.btn_sweep.clicked.connect(self._start_sweep)
        row2.addWidget(self.btn_sweep)

        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear)
        row2.addWidget(btn_clear)

        row2.addStretch()
        root.addLayout(row2)

        cw = QWidget()
        cw.setLayout(root)
        self.setCentralWidget(cw)

    # ── range apply ───────────────────────────────────────────────────────
    def _apply_range(self):
        try:
            xmin = float(self.min_freq_edit.text())
            xmax = float(self.max_freq_edit.text())
            if xmin >= xmax:
                raise ValueError
        except ValueError:
            self.lbl_status.setText("Status: Invalid range")
            self.lbl_status.setStyleSheet("color:#ff6060;")
            return
        self.plot.set_x_range(xmin, xmax)
        self.lbl_status.setText(f"Status: Range {xmin}–{xmax} kHz applied")
        self.lbl_status.setStyleSheet("color:#80ff80;")

    # ── start sweep ───────────────────────────────────────────────────────
    def _start_sweep(self):
        if self.sweep_running:
            self.lbl_status.setText("Status: Sweep already running")
            return

        try:
            min_f = float(self.min_freq_edit.text())
            max_f = float(self.max_freq_edit.text())
            if min_f <= 0 or max_f <= min_f:
                raise ValueError
        except ValueError:
            self.lbl_status.setText("Status: Invalid range — using 38–44 kHz")
            self.lbl_status.setStyleSheet("color:#ff6060;")
            min_f, max_f = 38.0, 44.0

        self.sweep_running = True
        self.btn_sweep.setEnabled(False)
        self.lbl_status.setText("Status: Sweeping…")
        self.lbl_status.setStyleSheet("color:#ffaa00;")

        # apply range to plot before sweep starts
        self.plot.set_x_range(min_f, max_f)

        # reset live buffers
        self._live_xs = []
        self._live_ys = []

        def _progress_cb(freq_khz, adc_val):
            self.bridge.progress.emit(freq_khz, adc_val)

        def work():
            try:
                self.hw.sweep(min_f, max_f, progress_cb=_progress_cb)
            finally:
                self.bridge.done.emit((self._live_xs[:], self._live_ys[:]))

        threading.Thread(target=work, daemon=True).start()

    # ── live progress ─────────────────────────────────────────────────────
    @pyqtSlot(float, int)
    def _on_progress(self, freq_khz, adc_val):
        self._live_xs.append(freq_khz)
        self._live_ys.append(adc_val)

        # update status with current freq
        self.lbl_status.setText(f"Status: {freq_khz:.2f} kHz  ADC={adc_val}")

    # ── sweep done ────────────────────────────────────────────────────────
    @pyqtSlot(object)
    def _on_sweep_done(self, data):
        xs, ys = data
        if xs and ys:
            self.plot.add_curve(xs, ys)
            self.curve_count += 1
            peak_i = ys.index(max(ys))
            self.lbl_peak.setText(
                f"Peak: {xs[peak_i]:.2f} kHz  ({ys[peak_i]} cts)")

        self.sweep_running = False
        self.btn_sweep.setEnabled(True)
        self.lbl_status.setText("Status: Ready")
        self.lbl_status.setStyleSheet("color:#80ff80;")

    # ── clear ─────────────────────────────────────────────────────────────
    def _clear(self):
        self.plot.clear()
        self.curve_count = 0
        self.lbl_peak.setText("Peak: —")

    def closeEvent(self, e):
        try:
            self.hw.destroy()
        except Exception:
            pass
        e.accept()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Main()
    w.showFullScreen()
    app.processEvents()

    def _hw_init():
        try:
            w.hw.phaco_off()
            print("[HW] init done", flush=True)
        except Exception as e:
            print(f"[HW] init error: {e}", flush=True)

    QTimer.singleShot(500, _hw_init)
    sys.exit(app.exec_())
