"""
BatteryTray - Theo dõi % pin chuột / tai nghe không dây trên Windows.
  * Cửa sổ giao diện hiển thị từng thiết bị kèm thanh pin
  * Icon số % ngay trên taskbar (khay hệ thống)
  * Nguồn dữ liệu:
      - Bluetooth: thông tin pin chuẩn của Windows
      - Logitech Lightspeed (G PRO X SUPERLIGHT 2, ...): giao thức HID++ qua USB receiver

Cài đặt:   pip install pystray pillow
Chạy:      pythonw battery_tray.py          (thêm --tray để khởi động ẩn)
Chẩn đoán: python battery_tray.py --scan    (in danh sách thiết bị HID + kết quả đọc pin)
Đóng gói:  pip install pyinstaller
           pyinstaller --noconsole --onefile battery_tray.py
"""
import ctypes
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt

DEBUG = False


def dbg(*a):
    if DEBUG:
        print(*a, flush=True)


# =====================================================================
#  PHẦN 1: HID tối giản bằng ctypes (không cần cài thư viện hidapi)
# =====================================================================
VP = ctypes.c_void_p
INVALID = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT),
                ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT), ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT), ("NumberInputButtonCaps", wt.USHORT),
                ("NumberInputValueCaps", wt.USHORT), ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT), ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT), ("NumberFeatureButtonCaps", wt.USHORT),
                ("NumberFeatureValueCaps", wt.USHORT), ("NumberFeatureDataIndices", wt.USHORT)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", ctypes.c_void_p)]


if sys.platform == "win32":
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hiddll = ctypes.WinDLL("hid", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, VP, wt.DWORD, wt.DWORD, VP]
    k32.CreateFileW.restype = VP
    k32.CloseHandle.argtypes = [VP]
    k32.WriteFile.argtypes = [VP, VP, wt.DWORD, VP, VP]
    k32.ReadFile.argtypes = [VP, VP, wt.DWORD, VP, VP]
    k32.GetOverlappedResult.argtypes = [VP, VP, VP, wt.BOOL]
    k32.CancelIoEx.argtypes = [VP, VP]
    k32.CreateEventW.argtypes = [VP, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    k32.CreateEventW.restype = VP
    k32.WaitForSingleObject.argtypes = [VP, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    k32.ResetEvent.argtypes = [VP]
    setupapi.SetupDiGetClassDevsW.argtypes = [VP, wt.LPCWSTR, VP, wt.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = VP
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [VP, VP, VP, wt.DWORD, VP]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [VP, VP, VP, wt.DWORD, VP, VP]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [VP]
    hiddll.HidD_GetHidGuid.argtypes = [VP]
    hiddll.HidD_GetAttributes.argtypes = [VP, VP]
    hiddll.HidD_GetPreparsedData.argtypes = [VP, VP]
    hiddll.HidD_FreePreparsedData.argtypes = [VP]
    hiddll.HidP_GetCaps.argtypes = [VP, VP]
    hiddll.HidP_GetCaps.restype = ctypes.c_long
    hiddll.HidD_GetProductString.argtypes = [VP, VP, wt.ULONG]


def enum_hid_paths():
    guid = GUID()
    hiddll.HidD_GetHidGuid(ctypes.byref(guid))
    info = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x2 | 0x10)
    paths = []
    if not info or info == INVALID:
        return paths
    try:
        i = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), i,
                                                        ctypes.byref(did)):
                break
            i += 1
            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(did), None, 0,
                                                      ctypes.byref(need), None)
            if need.value == 0:
                continue
            buf = ctypes.create_string_buffer(need.value)
            struct.pack_into("I", buf, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
            if setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(did), buf,
                                                         need.value, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)
    return paths


def list_hid():
    """Liệt kê mọi giao diện HID: vid, pid, usage page, usage, độ dài report, tên."""
    out = []
    if sys.platform != "win32":
        return out
    for path in enum_hid_paths():
        h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)   # chỉ để truy vấn, không cần quyền đọc/ghi
        if h is None or h == INVALID:
            continue
        try:
            attr = HIDD_ATTRIBUTES()
            attr.Size = ctypes.sizeof(attr)
            if not hiddll.HidD_GetAttributes(h, ctypes.byref(attr)):
                continue
            usage_page = usage = in_len = out_len = 0
            pp = ctypes.c_void_p()
            if hiddll.HidD_GetPreparsedData(h, ctypes.byref(pp)):
                try:
                    caps = HIDP_CAPS()
                    if hiddll.HidP_GetCaps(pp, ctypes.byref(caps)) == 0x110000:
                        usage_page, usage = caps.UsagePage, caps.Usage
                        in_len, out_len = caps.InputReportByteLength, caps.OutputReportByteLength
                finally:
                    hiddll.HidD_FreePreparsedData(pp)
            name = ctypes.create_unicode_buffer(128)
            hiddll.HidD_GetProductString(h, name, ctypes.sizeof(name))
            out.append(dict(path=path, vid=attr.VendorID, pid=attr.ProductID,
                            usage_page=usage_page, usage=usage,
                            in_len=in_len, out_len=out_len, product=name.value))
        finally:
            k32.CloseHandle(h)
    return out


class HidDevice:
    """Mở một giao diện HID để gửi/nhận report (overlapped I/O, có timeout)."""

    def __init__(self, info):
        h = k32.CreateFileW(info["path"], 0xC0000000, 3, None, 3, 0x40000000, None)
        if h is None or h == INVALID:
            raise OSError("không mở được thiết bị HID")
        self.h = h
        self.in_len = info["in_len"] or 20
        self.out_len = info["out_len"] or 20
        self.event = k32.CreateEventW(None, True, False, None)

    def close(self):
        try:
            k32.CloseHandle(self.h)
            k32.CloseHandle(self.event)
        except Exception:
            pass

    def write(self, data):
        data = bytes(data).ljust(self.out_len, b"\0")
        ov = OVERLAPPED()
        ov.hEvent = self.event
        k32.ResetEvent(self.event)
        buf = ctypes.create_string_buffer(data, len(data))
        written = wt.DWORD(0)
        if not k32.WriteFile(self.h, buf, len(data), ctypes.byref(written), ctypes.byref(ov)):
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                raise OSError("ghi HID thất bại")
            if k32.WaitForSingleObject(self.event, 1500) != 0:
                k32.CancelIoEx(self.h, ctypes.byref(ov))
                k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(written), True)
                raise OSError("ghi HID quá thời gian")
            k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(written), False)

    def read(self, timeout_ms):
        buf = ctypes.create_string_buffer(self.in_len)
        ov = OVERLAPPED()
        ov.hEvent = self.event
        k32.ResetEvent(self.event)
        got = wt.DWORD(0)
        if not k32.ReadFile(self.h, buf, self.in_len, ctypes.byref(got), ctypes.byref(ov)):
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                return None
            if k32.WaitForSingleObject(self.event, max(1, int(timeout_ms))) != 0:
                k32.CancelIoEx(self.h, ctypes.byref(ov))
                k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), True)
                return None
            if not k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(got), False):
                return None
        return buf.raw[:got.value]


# =====================================================================
#  PHẦN 2: Logitech HID++ 2.0 - đọc pin chuột qua receiver Lightspeed
# =====================================================================
LOGI_VID = 0x046D
SW_ID = 0x07
VOLT_LUT = [(4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60), (3811, 50),
            (3778, 40), (3751, 30), (3717, 20), (3671, 10), (3646, 5), (3579, 2), (3500, 0)]


def volt_to_pct(mv):
    """Ước lượng % pin từ điện áp (mV) - chỉ dùng cho thiết bị báo điện áp."""
    if mv >= VOLT_LUT[0][0]:
        return 100
    for (v1, p1), (v2, p2) in zip(VOLT_LUT, VOLT_LUT[1:]):
        if mv >= v2:
            return round(p2 + (p1 - p2) * (mv - v2) / (v1 - v2))
    return 0


def hidpp_request(dev, idx, feat_idx, func, params=b"", timeout=1500):
    """Gửi 1 lệnh HID++ 2.0 (report dài 0x11) và chờ phản hồi khớp. Trả về phần params hoặc None."""
    msg = bytearray(20)
    msg[0], msg[1], msg[2], msg[3] = 0x11, idx, feat_idx, (func << 4) | SW_ID
    msg[4:4 + len(params)] = params
    try:
        dev.write(msg)
    except OSError:
        return None
    deadline = time.monotonic() + timeout / 1000.0
    while True:
        remaining = (deadline - time.monotonic()) * 1000
        if remaining <= 0:
            return None
        r = dev.read(remaining)
        if not r:
            return None
        if len(r) < 7 or r[0] not in (0x10, 0x11) or r[1] != idx:
            continue
        if r[2] in (0x8F, 0xFF):          # báo lỗi (thiết bị ngủ / không tồn tại / không hỗ trợ)
            dbg(f"   idx={idx} lỗi HID++: {r[:7].hex()}")
            return None
        if r[2] == feat_idx and r[3] == ((func << 4) | SW_ID):
            return r[4:]


def feature_index(dev, idx, feature_id):
    r = hidpp_request(dev, idx, 0x00, 0, bytes([feature_id >> 8, feature_id & 0xFF]))
    return r[0] if r and r[0] != 0 else None


def read_logitech_battery(dev, idx):
    """Thử lần lượt các feature pin của HID++: 0x1004, 0x1000, 0x1001."""
    fi = feature_index(dev, idx, 0x1004)            # Unified Battery
    if fi:
        r = hidpp_request(dev, idx, fi, 1)
        if r:
            soc, flags, status = r[0], r[1], r[2]
            if soc == 0 and flags:                  # chỉ có mức thô
                soc = 100 if flags & 8 else 50 if flags & 4 else 15 if flags & 2 else 5
            return {"level": soc, "charging": status in (1, 2)}
    fi = feature_index(dev, idx, 0x1000)            # Battery Level Status
    if fi:
        r = hidpp_request(dev, idx, fi, 0)
        if r:
            return {"level": r[0], "charging": r[2] in (1, 2, 4)}
    fi = feature_index(dev, idx, 0x1001)            # Battery Voltage
    if fi:
        r = hidpp_request(dev, idx, fi, 0)
        if r:
            mv = (r[0] << 8) | r[1]
            return {"level": volt_to_pct(mv), "charging": bool(r[2] & 0x80)}
    return None


_name_cache = {}


def logitech_name(dev, path, idx):
    key = (path, idx)
    if key in _name_cache:
        return _name_cache[key]
    name = None
    try:
        fi = feature_index(dev, idx, 0x0005)        # Device Name
        r = hidpp_request(dev, idx, fi, 0) if fi else None
        if r:
            total, text = r[0], b""
            for _ in range(5):
                if len(text) >= total:
                    break
                r = hidpp_request(dev, idx, fi, 1, bytes([len(text)]))
                if not r:
                    break
                chunk = bytes(r[:16]).split(b"\0")[0]
                if not chunk:
                    break
                text += chunk
            name = text.decode("ascii", "ignore").strip() or None
    except Exception:
        name = None
    if name and "logitech" not in name.lower():
        name = "Logitech " + name
    name = name or "Chuột Logitech"
    _name_cache[key] = name
    return name


def is_hidpp_long(info):
    return (info["usage_page"] == 0xFF00 and info["usage"] == 0x0002) or \
           (info["usage_page"] == 0xFF43 and info["usage"] == 0x0202)


def read_logitech():
    """Đọc pin mọi thiết bị Logitech đang kết nối qua receiver (hoặc cáp USB)."""
    results = {}
    if sys.platform != "win32":
        return results
    try:
        for info in list_hid():
            if info["vid"] != LOGI_VID or not is_hidpp_long(info):
                continue
            dbg(f"Logitech HID++ interface: PID={info['pid']:04X} {info['product']}")
            try:
                dev = HidDevice(info)
            except OSError as e:
                dbg("   không mở được:", e)
                continue
            try:
                found = False
                for idx in (1, 2, 3, 4, 5, 6, 0xFF, 0):
                    if found and idx in (0xFF, 0):
                        break
                    if hidpp_request(dev, idx, 0x00, 1, bytes([0, 0, 0x55]), timeout=500) is None:
                        continue
                    bat = read_logitech_battery(dev, idx)
                    dbg(f"   idx={idx} pin: {bat}")
                    if bat:
                        found = True
                        bat["src"] = "Logitech Lightspeed"
                        results[logitech_name(dev, info["path"], idx)] = bat
            finally:
                dev.close()
    except Exception as e:
        dbg("Lỗi đọc Logitech:", e)
    return results


# =====================================================================
#  PHẦN 3: Bluetooth (thông tin pin chuẩn của Windows)
# =====================================================================
PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$key = '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2'
$out = foreach ($d in Get-PnpDevice -PresentOnly) {
    if ($d.InstanceId -notmatch '^(BTHENUM|BTHLE|HID|BTH)') { continue }
    $p = Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName $key
    if ($p -and $null -ne $p.Data) {
        [pscustomobject]@{ Name = $d.FriendlyName; Battery = [int]$p.Data }
    }
}
@($out) | ConvertTo-Json -Compress
"""


def read_bluetooth():
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
            capture_output=True, text=True, timeout=90, creationflags=flags,
        )
        raw = res.stdout.strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        result = {}
        for item in data:
            name = (item.get("Name") or "Thiết bị Bluetooth").strip()
            level = int(item.get("Battery"))
            if 0 <= level <= 100:
                result[name] = {"level": level, "charging": None, "src": "Bluetooth"}
        return result
    except Exception:
        return {}


def read_all():
    """Gộp mọi nguồn: {tên: {"level", "charging", "src"}}."""
    merged = read_bluetooth()
    merged.update(read_logitech())
    return merged


# =====================================================================
#  PHẦN 4: Giao diện + khay hệ thống
# =====================================================================
def run_scan():
    global DEBUG
    DEBUG = True
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=== Danh sach giao dien HID (bo qua ban phim/chuot thuong) ===")
    for i in list_hid():
        if i["usage_page"] == 1 and i["usage"] in (2, 6):
            continue
        print(f"VID={i['vid']:04X} PID={i['pid']:04X} usage_page=0x{i['usage_page']:04X} "
              f"usage=0x{i['usage']:04X} in={i['in_len']} out={i['out_len']} "
              f"name='{i['product']}'")
    print("\n=== Doc pin (buoc Bluetooth co the mat 10-30 giay, vui long cho) ===", flush=True)
    for name, d in read_all().items():
        print(f"{name}: {d['level']}% charging={d['charging']} ({d['src']})")
    print("\nXong. Hay gui toan bo noi dung nay neu can ho tro them thiet bi.")


def run_listen():
    """Chỉ LẮNG NGHE report từ dongle SteelSeries (VID 1038), không gửi lệnh nào.
    Chạy theo từng bước để gắn nhãn mỗi gói tin với hành động vừa làm."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    targets = [i for i in list_hid()
               if i["vid"] == 0x1038 and i["in_len"] > 0 and i["usage_page"] >= 0xFF00]
    if not targets:
        print("Khong thay dongle SteelSeries (VID 1038).")
        return
    print(f"Dang lang nghe {len(targets)} giao dien (chi nghe, khong gui lenh).\n")
    ref = input("Nhap % pin hien tai tren app SteelSeries/GG theo thu tu: trai phai hop "
                "(vi du: 31 80 60; bo trong neu khong biet): ")
    print("PIN THAM CHIEU:", ref or "(khong co)", "\n", flush=True)

    t0 = time.monotonic()
    lock = threading.Lock()
    stop = threading.Event()
    label = ["bat dau"]

    def worker(info):
        try:
            dev = HidDevice(info)
        except OSError as e:
            with lock:
                print(f"Khong mo duoc giao dien 0x{info['usage_page']:04X}: {e}")
            return
        try:
            while not stop.is_set():
                r = dev.read(300)
                if r:
                    with lock:
                        print(f"[{time.monotonic() - t0:5.1f}s] [{label[0]}] "
                              f"page=0x{info['usage_page']:04X} {r.hex(' ')}", flush=True)
        finally:
            dev.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in targets]
    for t in threads:
        t.start()

    steps = [
        "KHONG LAM GI - cho khoang 10 giay",
        "Bo tai nghe TRAI vao hop sac",
        "Lay tai nghe TRAI ra khoi hop",
        "Bo tai nghe PHAI vao hop sac",
        "Lay tai nghe PHAI ra khoi hop",
        "Bam nut cam ung 1 lan",
        "Chinh am luong len 1 nac",
    ]
    for n, text in enumerate(steps, 1):
        label[0] = f"buoc {n}"
        with lock:
            print(f"\n>>> BUOC {n}: {text}", flush=True)
        input("    Lam xong thi nhan Enter... ")
    time.sleep(1)
    stop.set()
    for t in threads:
        t.join()
    print("\nXong. Hay gui toan bo noi dung nay.")


if __name__ == "__main__" and "--listen" in sys.argv:
    run_listen()
    sys.exit(0)

if __name__ == "__main__" and "--scan" in sys.argv:
    run_scan()
    sys.exit(0)

import tkinter as tk                      # noqa: E402
from tkinter import ttk                   # noqa: E402

import pystray                            # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

try:
    from PIL import ImageTk
except Exception:
    ImageTk = None

try:
    import winreg
except ImportError:
    winreg = None

APP_NAME = "BatteryTray"
AUTO = "auto"
AUTO_LABEL = "Tự động (thiết bị yếu nhất)"
INTERVALS = {"30 giây": 30, "1 phút": 60, "2 phút": 120, "5 phút": 300}

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_CFG = {"selected": AUTO, "low": 20, "interval": 60}

BG, CARD, TEXT, MUTED = "#1b1c23", "#272935", "#f2f3f7", "#9aa0b4"
GREEN, ORANGE, RED, ACCENT, TRACK = "#2ecc71", "#f0a020", "#e74c3c", "#4c8dff", "#3a3d4d"


def load_config():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
    except Exception:
        pass


def level_color(level, low):
    if level <= low:
        return RED
    if level <= 50:
        return ORANGE
    return GREEN


def get_font(size):
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_icon(level, low=20, charging=False):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if level is None:
        bg, text = (90, 90, 90, 255), "?"
    else:
        bg = {RED: (214, 48, 49, 255), ORANGE: (230, 160, 20, 255)}.get(
            level_color(level, low), (39, 174, 96, 255))
        text = str(level)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=bg)
    font = get_font(44 if len(text) <= 2 else 34)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1] + 3), text, font=font, fill="white")
    if charging:   # tia chớp nhỏ ở góc trên phải
        d.polygon([(57, 2), (46, 16), (52, 16), (47, 28), (60, 12), (54, 12)],
                  fill=(255, 235, 0, 255))
    return img


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def command_line():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}" --tray'


def autostart_enabled():
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    if not winreg:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, command_line())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


class App:
    def __init__(self):
        self.cfg = load_config()
        self.devices = {}
        self.warned = set()
        self.first_hide = True
        self.q = queue.Queue()
        self.wake = threading.Event()
        self.stop = threading.Event()

        self.root = tk.Tk()
        self.root.title("Pin thiết bị không dây")
        self.root.configure(bg=BG)
        self.root.geometry("420x660")
        self.root.minsize(420, 540)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        if ImageTk:
            try:
                self._win_icon = ImageTk.PhotoImage(make_icon(None))
                self.root.iconphoto(True, self._win_icon)
            except Exception:
                pass

        self.setup_style()
        self.build_ui()

        self.icon = pystray.Icon(APP_NAME, make_icon(None), "Đang đọc pin...",
                                 menu=pystray.Menu(self.tray_menu))
        self.icon.run_detached()

        threading.Thread(target=self.loop, daemon=True).start()
        self.root.after(200, self.pump)
        if "--tray" in sys.argv:
            self.root.withdraw()

    def setup_style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                    arrowcolor=TEXT, bordercolor=TRACK, lightcolor=CARD, darkcolor=CARD)
        s.map("TCombobox", fieldbackground=[("readonly", CARD)],
              foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)])
        s.configure("TSpinbox", fieldbackground=CARD, background=CARD, foreground=TEXT,
                    arrowcolor=TEXT, bordercolor=TRACK, lightcolor=CARD, darkcolor=CARD)
        s.configure("Dark.TCheckbutton", background=BG, foreground=TEXT, focuscolor=BG)
        s.map("Dark.TCheckbutton", background=[("active", BG)], foreground=[("active", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def build_ui(self):
        r = self.root
        header = tk.Frame(r, bg=BG)
        header.pack(fill="x", padx=18, pady=(18, 4))
        tk.Label(header, text="🔋 Pin thiết bị", bg=BG, fg=TEXT,
                 font=("Segoe UI", 17, "bold")).pack(side="left")
        tk.Button(header, text="⟳ Làm mới", command=self.refresh_now, bg=ACCENT, fg="white",
                  activebackground="#3a78e0", activeforeground="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=12, pady=4, cursor="hand2"
                  ).pack(side="right")

        self.status = tk.Label(r, text="Đang đọc pin...", bg=BG, fg=MUTED,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", padx=20)

        self.cards = tk.Frame(r, bg=BG)
        self.cards.pack(fill="both", expand=True, padx=18, pady=10)

        box = tk.Frame(r, bg=BG)
        box.pack(fill="x", padx=18, pady=(0, 14))
        tk.Frame(box, bg=TRACK, height=1).pack(fill="x", pady=(0, 10))
        tk.Label(box, text="Cài đặt", bg=BG, fg=TEXT,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 6))

        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="Hiện trên taskbar:", bg=BG, fg=MUTED, font=("Segoe UI", 10)
                 ).pack(side="left")
        self.sel_var = tk.StringVar(value=AUTO_LABEL)
        self.sel_box = ttk.Combobox(row, textvariable=self.sel_var, state="readonly",
                                    values=[AUTO_LABEL], width=28)
        self.sel_box.pack(side="right")
        self.sel_box.bind("<<ComboboxSelected>>", self.on_select_combo)

        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="Cảnh báo khi pin ≤ (%):", bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(side="left")
        self.low_var = tk.IntVar(value=int(self.cfg["low"]))
        sp = ttk.Spinbox(row, from_=5, to=50, textvariable=self.low_var, width=6,
                         command=self.on_low_change)
        sp.pack(side="right")
        sp.bind("<FocusOut>", lambda e: self.on_low_change())
        sp.bind("<Return>", lambda e: self.on_low_change())

        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="Cập nhật mỗi:", bg=BG, fg=MUTED, font=("Segoe UI", 10)
                 ).pack(side="left")
        cur = next((k for k, v in INTERVALS.items() if v == self.cfg["interval"]), "1 phút")
        self.int_var = tk.StringVar(value=cur)
        cb = ttk.Combobox(row, textvariable=self.int_var, state="readonly",
                          values=list(INTERVALS), width=10)
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", self.on_interval_change)

        self.auto_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(box, text="Khởi động cùng Windows", variable=self.auto_var,
                        style="Dark.TCheckbutton", command=self.on_autostart
                        ).pack(anchor="w", pady=(8, 0))

        self.render_cards()

    def render_cards(self):
        for w in self.cards.winfo_children():
            w.destroy()
        low = int(self.cfg["low"])
        shown, _ = self.current()

        if not self.devices:
            msg = ("Không tìm thấy thiết bị nào có thông tin pin.\n\n"
                   "• Chuột Logitech: cắm USB receiver và bật chuột.\n"
                   "• Thiết bị Bluetooth: hãy chắc chắn đã kết nối.\n"
                   "• Tai nghe SteelSeries dùng dongle 2.4GHz hiện chưa được hỗ trợ "
                   "(xem thêm hướng dẫn chạy chế độ --scan).")
            tk.Label(self.cards, text=msg, bg=CARD, fg=MUTED, justify="left", wraplength=340,
                     font=("Segoe UI", 10), padx=16, pady=16).pack(fill="x")
            return

        for name, dv in sorted(self.devices.items()):
            lvl, charging = dv["level"], dv.get("charging")
            color = level_color(lvl, low)
            selected = (name == shown)
            card = tk.Frame(self.cards, bg=CARD, highlightthickness=2, cursor="hand2",
                            highlightbackground=ACCENT if selected else CARD,
                            highlightcolor=ACCENT if selected else CARD)
            card.pack(fill="x", pady=5)
            inner = tk.Frame(card, bg=CARD)
            inner.pack(fill="x", padx=14, pady=12)

            top = tk.Frame(inner, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text=name, bg=CARD, fg=TEXT, font=("Segoe UI", 11, "bold"),
                     anchor="w", wraplength=240, justify="left").pack(side="left")
            tk.Label(top, text=f"{'⚡' if charging else ''}{lvl}%", bg=CARD, fg=color,
                     font=("Segoe UI", 20, "bold")).pack(side="right")

            bar = tk.Canvas(inner, width=340, height=14, bg=CARD, highlightthickness=0)
            bar.pack(fill="x", pady=(6, 4))
            bar.create_line(7, 7, 333, 7, width=14, capstyle="round", fill=TRACK)
            if lvl > 0:
                bar.create_line(7, 7, 7 + 326 * lvl / 100, 7, width=14,
                                capstyle="round", fill=color)

            parts = []
            if charging:
                parts.append("Đang sạc")
            elif lvl <= low:
                parts.append("⚠ Pin yếu - nên sạc ngay")
            elif lvl <= 50:
                parts.append("Pin trung bình")
            else:
                parts.append("Pin tốt")
            if dv.get("src"):
                parts.append(dv["src"])
            if selected:
                parts.append("Đang hiện trên taskbar")
            tk.Label(inner, text="  •  ".join(parts), bg=CARD, fg=MUTED, font=("Segoe UI", 9),
                     anchor="w").pack(fill="x")

            for w in (card, inner, top, bar) + tuple(inner.winfo_children()) + tuple(top.winfo_children()):
                w.bind("<Button-1>", lambda e, n=name: self.select(n))

    # ---------- logic
    def current(self):
        if not self.devices:
            return None, None
        sel = self.cfg.get("selected", AUTO)
        if sel != AUTO and sel in self.devices:
            return sel, self.devices[sel]
        name = min(self.devices, key=lambda n: self.devices[n]["level"])
        return name, self.devices[name]

    def select(self, name):
        self.cfg["selected"] = name
        save_config(self.cfg)
        self.refresh_view()

    def on_select_combo(self, _=None):
        v = self.sel_var.get()
        self.select(AUTO if v == AUTO_LABEL else v)

    def on_low_change(self):
        try:
            v = max(5, min(50, int(self.low_var.get())))
        except Exception:
            v = int(self.cfg["low"])
        self.low_var.set(v)
        self.cfg["low"] = v
        save_config(self.cfg)
        self.warned.clear()
        self.refresh_view()

    def on_interval_change(self, _=None):
        self.cfg["interval"] = INTERVALS.get(self.int_var.get(), 60)
        save_config(self.cfg)
        self.wake.set()

    def on_autostart(self):
        try:
            set_autostart(self.auto_var.get())
        except Exception:
            self.auto_var.set(autostart_enabled())

    def refresh_now(self):
        self.status.config(text="Đang cập nhật...")
        self.wake.set()

    def refresh_view(self):
        low = int(self.cfg["low"])
        self.sel_box.config(values=[AUTO_LABEL] + sorted(self.devices))
        sel = self.cfg.get("selected", AUTO)
        self.sel_var.set(sel if sel in self.devices else AUTO_LABEL)
        self.render_cards()

        _, cur = self.current()
        self.icon.icon = make_icon(cur["level"] if cur else None, low,
                                   bool(cur and cur.get("charging")))
        tip = "\n".join(
            f"{n}: {d['level']}%{' (đang sạc)' if d.get('charging') else ''}"
            for n, d in sorted(self.devices.items())) or "Không tìm thấy thiết bị có thông tin pin"
        self.icon.title = tip[:127]
        self.icon.update_menu()

    def on_data(self, devices):
        self.devices = devices
        self.status.config(text="Cập nhật lúc " + time.strftime("%H:%M:%S"))
        self.refresh_view()
        low = int(self.cfg["low"])
        for name, d in devices.items():
            lvl = d["level"]
            if lvl <= low and not d.get("charging") and name not in self.warned:
                self.warned.add(name)
                self.icon.notify(f"{name} chỉ còn {lvl}% pin. Hãy sạc sớm!", "Pin yếu")
            elif lvl > low + 10 or d.get("charging"):
                self.warned.discard(name)

    def loop(self):
        while not self.stop.is_set():
            data = read_all()
            self.q.put(lambda d=data: self.on_data(d))
            self.wake.wait(int(self.cfg["interval"]))
            self.wake.clear()

    def pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(200, self.pump)

    def show(self, *_):
        def _show():
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        self.q.put(_show)

    def hide(self):
        self.root.withdraw()
        if self.first_hide:
            self.first_hide = False
            self.icon.notify("Ứng dụng vẫn chạy ở khay hệ thống. "
                             "Bấm vào icon để mở lại.", "Pin thiết bị")

    def tray_menu(self):
        items = [pystray.MenuItem("Mở cửa sổ", self.show, default=True)]
        for name, d in sorted(self.devices.items()):
            items.append(pystray.MenuItem(f"{name}: {d['level']}%", None, enabled=False))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Làm mới", lambda i, m: self.q.put(self.refresh_now)),
            pystray.MenuItem("Thoát", self.quit),
        ]
        return items

    def quit(self, *_):
        def _quit():
            self.stop.set()
            self.wake.set()
            try:
                self.icon.stop()
            except Exception:
                pass
            self.root.destroy()
        self.q.put(_quit)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
