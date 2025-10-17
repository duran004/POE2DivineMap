import sys, time, hashlib
import cv2, numpy as np, mss, pytesseract, keyboard
from PyQt5 import QtCore, QtGui, QtWidgets
import win32con, win32gui
from ctypes import windll

try:
    windll.user32.SetProcessDPIAware()
except:
    pass

# --- AYARLAR ---
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
SCAN_BOX = (300, 95, 0, -55)  # (w,h,dx,dy) imleç merkezli, dy<0: aşağı
SCAN_EVERY_MS = 1  # kaç ms'de bir tarasın
HOLD_MS = 500  # ne kadar süre gösterimde kalsın
FONT_PX = 28  # başlık font boyutu
CONFIDENCE_MIN = 35  # isabet OCR eşiği
YELLOW_RATIO_MIN = 0.0002  # sarı piksel oranı eşiği (gold başlık için)

WATER_MAPS = {
    "CALDERA",
    "WAYWARD",
    "STRONGHOLD",
    "RUGOSA",
    "CRIMSON",
    "SHORES",
    "SANDSPIT",
    "ROCKPOOLS",
    "RUPTURE",
    "WETLANDS",
    "BURIAL",
    "SUMP",
}

# --- Hızlı tesseract config (tek satır, whitelist)
TESS_CFG = "--psm 7 --oem 1 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ- "


def is_water(s: str) -> bool:
    s = (s or "").upper()
    return any(name in s for name in WATER_MAPS)


def mouse_bbox():
    pt = QtGui.QCursor.pos()
    w, h, dx, dy = SCAN_BOX
    left = max(0, pt.x() - dx - w // 2)
    top = max(0, pt.y() - dy)
    return {"left": left, "top": top, "width": w, "height": h}, pt


# --- Basit, hızlı pipeline'lar
def gold_mask(img_bgr):
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    lo = np.array((15, 80, 80), np.uint8)
    hi = np.array((40, 255, 255), np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    # hafif kapama
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1
    )
    return mask


def preprocess_gold_fast(img_bgr):
    mask = gold_mask(img_bgr)
    # 2x ölçek (ucuz)
    mask = cv2.resize(mask, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LINEAR)
    return mask


def preprocess_gray_fast(img_bgr):
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LINEAR)
    # ucuz global threshold
    _, thr = cv2.threshold(g, 150, 255, cv2.THRESH_BINARY)
    return thr


def ocr_text_fast(bin_img):
    data = pytesseract.image_to_data(
        bin_img, lang="eng", config=TESS_CFG, output_type=pytesseract.Output.DICT
    )
    words, confs = [], []
    for t, c in zip(data["text"], data["conf"]):
        t = (t or "").strip()
        if not t:
            continue
        try:
            ci = int(c)
        except:
            ci = 0
        if ci >= CONFIDENCE_MIN:
            words.append(t)
            confs.append(ci)
    text = " ".join(words)
    text = "".join(ch for ch in text if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ- ")
    return " ".join(text.split())


def mse(a, b):
    # hızlı değişim algılama
    if a is None or b is None:
        return 9999.0
    if a.shape != b.shape:
        return 9999.0
    diff = cv2.absdiff(a, b)
    return float(np.mean(diff))


class Overlay(QtWidgets.QWidget):
    def __init__(self):
        super().__init__(
            None,
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool,
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setGeometry(QtWidgets.QApplication.primaryScreen().geometry())
        self.show()
        hwnd = int(self.winId())
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(
            hwnd,
            win32con.GWL_EXSTYLE,
            ex | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT,
        )

        self.text = ""
        self.debug_text = ""
        self.opacity = 0.0
        self.target = 0.0
        self.last_hit = 0
        self.scan_rect = None
        self.anchor = QtCore.QPoint(0, 0)
        self.sct = mss.mss()

        self.prev_small = None  # değişim algılama için önceki küçük görüntü

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.scan_once)
        self.timer.start(SCAN_EVERY_MS)
        self.anim = QtCore.QTimer(self)
        self.anim.timeout.connect(self.tick)
        self.anim.start(16)

    def hotkey_down(self):
        return keyboard.is_pressed("ctrl") and keyboard.is_pressed("shift")

    def scan_once(self):
        # hotkey yoksa her şeyi gizle
        if not self.hotkey_down():
            self.scan_rect = None
            self.debug_text = ""
            if time.time() - self.last_hit > 0.05:
                self.target = 0.0
            return

        bbox, pt = mouse_bbox()
        self.scan_rect = bbox
        raw = np.array(self.sct.grab(bbox))
        img = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

        # 1) Değişim algılama
        small = cv2.resize(img, (int(bbox["width"] / 4), int(bbox["height"] / 4)))
        if mse(self.prev_small, small) < 2.5:
            # hareket yok
            self.prev_small = small
            return
        self.prev_small = small

        # 2) Önce sarı oranına bak
        mask = gold_mask(img)
        yellow_ratio = float(np.count_nonzero(mask)) / mask.size

        if yellow_ratio >= YELLOW_RATIO_MIN:
            bin_img = preprocess_gold_fast(img)
        else:
            bin_img = preprocess_gray_fast(img)

        txt = ocr_text_fast(bin_img)
        self.debug_text = txt or "(no text)" + f" | y%={yellow_ratio:.3f}"
        now = time.time()

        if txt and is_water(txt):
            self.text = "DIVINE MAP"
            self.anchor = QtCore.QPoint(pt.x(), pt.y() + 35)
            self.target = 1.0
            self.last_hit = now
        elif now - self.last_hit > HOLD_MS / 1000.0:
            self.target = 0.0

    def tick(self):
        self.opacity += (self.target - self.opacity) * 0.15
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        # Tarama alanı
        if hasattr(self, "scan_rect") and self.scan_rect and self.hotkey_down():
            r = self.scan_rect
            p.setPen(QtGui.QPen(QtGui.QColor(255, 0, 0, 200), 2))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRect(r["left"], r["top"], r["width"], r["height"])

        # Debug metin
        if self.hotkey_down() and getattr(self, "debug_text", ""):
            font_dbg = QtGui.QFont("Consolas", 12)
            p.setFont(font_dbg)
            mpos = QtGui.QCursor.pos()
            rect = QtCore.QRect(mpos.x() - 170, mpos.y() - 72, 340, 46)
            p.fillRect(rect, QtGui.QColor(0, 0, 0, 150))
            p.setPen(QtGui.QColor(220, 220, 220))
            p.drawText(rect, QtCore.Qt.AlignCenter, self.debug_text)

        # ---- DIVINE MAP rozeti: beyaz zemin + kırmızı çerçeve + kırmızı yazı ----
        if self.opacity > 0.02 and getattr(self, "text", ""):
            p.setOpacity(self.opacity)

            # Yazı
            font = QtGui.QFont("Trajan Pro", FONT_PX)
            font.setBold(False)
            p.setFont(font)
            fm = QtGui.QFontMetrics(font)
            tw = fm.horizontalAdvance(self.text)
            th = fm.height()

            # Konumlandırma (imleç etrafı)
            x_center = self.anchor.x()
            y_base = self.anchor.y()  # rozetin alt kenarı referansı gibi düşün
            padding = 10
            radius = 1
            border_w = 2

            # Kutunun konumu: metni ortala, kutuyu beyaz doldur, kırmızı çerçeve çiz
            bg_rect = QtCore.QRect(
                x_center - (tw // 2) - padding,
                y_base - th - padding,
                tw + padding * 2,
                th + padding * 2,
            )

            # Beyaz arkaplan + kırmızı çerçeve
            p.setPen(QtGui.QPen(QtGui.QColor(200, 0, 0), border_w))
            p.setBrush(QtGui.QColor(255, 255, 255))
            p.drawRoundedRect(bg_rect, radius, radius)

            # Kırmızı yazı
            p.setPen(QtGui.QColor(200, 0, 0))
            p.drawText(bg_rect, QtCore.Qt.AlignCenter, self.text)

        p.end()


def main():
    app = QtWidgets.QApplication(sys.argv)
    ov = Overlay()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
