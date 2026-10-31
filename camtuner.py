"""
CamTuner

Reads a physical webcam, applies adjustments (zoom, pan, rotation, exposure,
brightness, contrast, color, sharpness) and publishes the result through the
OBS Virtual Camera driver, so Chrome and every other app can pick
"OBS Virtual Camera" and get the adjusted picture.

The real webcam is only switched on while another app is using the virtual
camera, or while the CamTuner window is open.

Requires OBS Studio installed on the PC (only for its virtual camera driver).
"""
import json
import os
import queue
import sys
import threading
import time
import traceback

APP_NAME = "CamTuner"
IS_WIN = sys.platform.startswith("win")


def _data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = _data_dir()
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LOG_PATH = os.path.join(DATA_DIR, "log.txt")

# A windowed .exe has no console; send prints and errors to a log file.
if sys.stdout is None or sys.stderr is None:
    _log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import tkinter as tk  # noqa: E402
from tkinter import ttk, messagebox  # noqa: E402
from PIL import Image, ImageDraw, ImageTk  # noqa: E402

import processing as proc  # noqa: E402

try:
    import pyvirtualcam
except Exception:
    pyvirtualcam = None
    traceback.print_exc()

try:
    import pystray
except Exception:
    pystray = None
    traceback.print_exc()

if IS_WIN:
    try:
        import usage
    except Exception:
        usage = None
        traceback.print_exc()
else:
    usage = None

OUTPUT_SIZES = ["1920x1080", "1280x720", "960x540", "640x480"]
OUT_FPS = 30
CAPTURE_W, CAPTURE_H = 1920, 1080
PREV_W, PREV_H = 768, 432
USAGE_POLL_SECONDS = 1.5
OFF_DELAY_SECONDS = 5.0  # keep the webcam on this long after the last app stops

APP_DEFAULTS = dict(proc.DEFAULTS)
APP_DEFAULTS.update({"camera_index": 0, "camera_name": "", "output": "1920x1080",
                     "on_demand": True})


# ---------------------------------------------------------------- settings

def load_settings():
    s = dict(APP_DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in s:
                continue
            ref = APP_DEFAULTS[k]
            try:
                if isinstance(ref, bool):
                    s[k] = bool(v)
                elif isinstance(ref, int):
                    s[k] = int(v)
                elif isinstance(ref, float):
                    s[k] = float(v)
                else:
                    s[k] = str(v)
            except (TypeError, ValueError):
                pass
    except FileNotFoundError:
        pass
    except Exception:
        traceback.print_exc()
    if s["output"] not in OUTPUT_SIZES:
        s["output"] = APP_DEFAULTS["output"]
    return s


def save_settings(s):
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- windows helpers

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s" --minimized' % sys.executable
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.exists(pyw):
        py = pyw
    return '"%s" "%s" --minimized' % (py, os.path.abspath(__file__))


def get_autostart():
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(on):
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


_MUTEX = None


def ensure_single_instance():
    """Two copies would fight over the webcam, so only allow one."""
    global _MUTEX
    if not IS_WIN:
        return
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MUTEX = k32.CreateMutexW(None, False, "Local\\CamTunerSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "CamTuner is already running.\n\n"
                                      "Look for its icon in the system tray, next to the clock.")
        r.destroy()
        sys.exit(0)


def enable_dpi_awareness():
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def list_cameras():
    """Returns [(index, name)]. Names come from DirectShow via pygrabber;
    falls back to generic numbered entries if that is unavailable."""
    names = []
    if IS_WIN:
        try:
            from pygrabber.dshow_graph import FilterGraph
            names = FilterGraph().get_input_devices()
        except Exception:
            traceback.print_exc()
            names = []
    if names:
        return list(enumerate(names))
    return [(i, "Camera %d" % i) for i in range(4)]


def is_virtual(name):
    return "obs virtual" in name.lower()


def log(msg):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def make_icon_image(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = size / 64.0
    blue = (37, 117, 214, 255)
    d.rounded_rectangle((4 * k, 18 * k, 44 * k, 50 * k), radius=int(7 * k), fill=blue)
    d.polygon([(44 * k, 28 * k), (60 * k, 19 * k), (60 * k, 49 * k), (44 * k, 40 * k)], fill=blue)
    d.ellipse((15 * k, 25 * k, 33 * k, 43 * k), fill=(255, 255, 255, 255))
    d.ellipse((20 * k, 30 * k, 28 * k, 38 * k), fill=blue)
    return img


def make_placeholder(w, h, text):
    img = np.full((h, w, 3), 24, dtype=np.uint8)
    scale = max(0.6, w / 1280.0)
    thick = max(1, int(round(2 * scale)))
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    cv2.putText(img, text, ((w - tw) // 2, (h + th) // 2), cv2.FONT_HERSHEY_SIMPLEX,
                scale, (200, 200, 200), thick, cv2.LINE_AA)
    return img


# ---------------------------------------------------------------- usage monitor

class UsageMonitor(threading.Thread):
    """Polls which other programs have the virtual camera open."""

    def __init__(self):
        super().__init__(daemon=True)
        self.users = []      # programs counted as using the camera
        self.details = []    # every program with the driver loaded, with its state
        self.available = usage is not None
        self.error = ""

    def run(self):
        if usage is None:
            return
        try:
            det = usage.Detector()
            log("OBS virtual camera DLLs: %s" % (sorted(det.dll_names) or "not found, using name match"))
        except Exception as e:
            traceback.print_exc()
            self.error = str(e)
            return
        last = None
        while True:
            try:
                details = det.scan()
                for msg in det.log:
                    log(msg)
                det.log.clear()
                self.details = details
                self.users = sorted({d["program"] for d in details
                                     if d["state"] in ("active", "unknown")})
                self.error = ""
                summary = sorted((d["program"], d["pid"], d["state"]) for d in details)
                if summary != last:
                    log("Apps with the virtual camera driver: %s" % (summary or "none"))
                    last = summary
            except Exception as e:
                traceback.print_exc()
                self.error = str(e)
            time.sleep(USAGE_POLL_SECONDS)


# ---------------------------------------------------------------- pipeline

class Pipeline(threading.Thread):
    """Capture -> process -> send to virtual camera, on a background thread.
    The real webcam is opened only while it is needed."""

    def __init__(self, settings, cam_index, out_size, monitor):
        super().__init__(daemon=True)
        self.settings = settings
        self.cam_index = cam_index
        self.out_w, self.out_h = out_size
        self.monitor = monitor
        self._stop_evt = threading.Event()
        self.preview_wanted = True
        self.camera_on = False
        self.latest = None
        self.latest_raw = None
        self.geom = None
        self.fps = 0.0
        self.cam_status = "Starting..."
        self.vcam_status = ""
        self.vcam_ok = False
        self._placeholder = make_placeholder(self.out_w, self.out_h, "Starting camera...")

    def stop(self):
        self._stop_evt.set()

    def run(self):
        try:
            self._run()
        except Exception as e:
            traceback.print_exc()
            self.cam_status = "Error: %s" % e

    def _wanted(self):
        if not self.settings.get("on_demand", True):
            return True
        if self.preview_wanted:
            return True
        return bool(self.monitor.users)

    def _open_camera(self):
        backend = cv2.CAP_DSHOW if IS_WIN else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.cam_index, backend)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_H)
        cap.set(cv2.CAP_PROP_FPS, OUT_FPS)
        return cap

    def _run(self):
        vcam = None
        if pyvirtualcam is None:
            self.vcam_status = "Virtual camera library missing (pyvirtualcam)."
        else:
            try:
                kwargs = {"backend": "obs"} if IS_WIN else {}
                vcam = pyvirtualcam.Camera(width=self.out_w, height=self.out_h, fps=OUT_FPS,
                                           fmt=pyvirtualcam.PixelFormat.BGR, **kwargs)
                self.vcam_ok = True
                self.vcam_status = "Output: %s (%dx%d)" % (vcam.device, self.out_w, self.out_h)
            except Exception as e:
                traceback.print_exc()
                self.vcam_status = "Virtual camera not available: %s. See the Help tab." % e

        cap = None
        last_wanted = 0.0
        next_open_try = 0.0
        last_placeholder = 0.0
        lut_key, lut = None, None
        fails, frames, t0 = 0, 0, time.time()
        try:
            while not self._stop_evt.is_set():
                now = time.time()
                if self._wanted():
                    last_wanted = now
                active = (now - last_wanted) < OFF_DELAY_SECONDS

                if active and cap is None and now >= next_open_try:
                    self.cam_status = "Turning webcam on..."
                    cap = self._open_camera()
                    if cap is None:
                        self.cam_status = ("Could not open camera %d. Is another app using it?"
                                           % self.cam_index)
                        next_open_try = time.time() + 2.0
                    else:
                        fails, frames, t0 = 0, 0, time.time()
                elif not active and cap is not None:
                    cap.release()
                    cap = None
                    self.fps = 0.0

                self.camera_on = cap is not None
                if cap is None:
                    if not active:
                        self.cam_status = "Webcam off. It turns on when an app uses OBS Virtual Camera."
                    if vcam is not None and now - last_placeholder >= 0.1:
                        vcam.send(self._placeholder)
                        last_placeholder = now
                    self.latest = self.latest_raw = self._placeholder
                    self.geom = None
                    time.sleep(0.05)
                    continue

                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 100:
                        self.cam_status = "Webcam stopped sending frames. Retrying..."
                        cap.release()
                        cap = None
                        next_open_try = time.time() + 2.0
                    else:
                        time.sleep(0.02)
                    continue
                fails = 0
                s = dict(self.settings)
                key = tuple(float(s[k]) for k in proc.COLOR_KEYS)
                if key != lut_key:
                    lut = None if proc.color_is_neutral(s) else proc.build_lut(s)
                    lut_key = key
                out, geom = proc.process_frame(frame, s, self.out_w, self.out_h, lut)
                if vcam is not None:
                    vcam.send(out)
                self.latest_raw, self.latest, self.geom = frame, out, geom

                frames += 1
                now = time.time()
                if now - t0 >= 1.0:
                    self.fps = frames / (now - t0)
                    frames, t0 = 0, now
                    h, w = frame.shape[:2]
                    self.cam_status = "Webcam on: camera %d, %dx%d, %.0f fps" % (
                        self.cam_index, w, h, self.fps)
        finally:
            if cap is not None:
                cap.release()
            self.camera_on = False
            if vcam is not None:
                vcam.close()


# ---------------------------------------------------------------- GUI

class App:
    SLIDERS = {
        # key: (label, from, to, resolution)
        "zoom": ("Zoom", 1.0, 5.0, 0.01),
        "pan_x": ("Pan left / right", -100, 100, 0.1),
        "pan_y": ("Pan up / down", -100, 100, 0.1),
        "rotation": ("Rotation (degrees, + is clockwise)", -180, 180, 0.5),
        "exposure": ("Exposure (stops)", -3.0, 3.0, 0.05),
        "brightness": ("Brightness", -100, 100, 1),
        "contrast": ("Contrast", -100, 100, 1),
        "gamma": ("Gamma (midtones)", 0.3, 3.0, 0.01),
        "saturation": ("Saturation (%)", 0, 200, 1),
        "temperature": ("Temperature (cool / warm)", -100, 100, 1),
        "tint": ("Tint (green / magenta)", -100, 100, 1),
        "sharpness": ("Sharpness", 0, 100, 1),
    }

    def __init__(self, root, start_minimized=False):
        self.root = root
        root.title(APP_NAME)
        root.resizable(False, False)
        self.settings = load_settings()
        self.pipeline = None
        self.vars = {}
        self._save_job = None
        self._photo = None
        self._shown = None
        self._prev_scale = 1.0
        self._drag = None
        self._events = queue.Queue()
        self._quitting = False

        icon_img = make_icon_image(64)
        self._win_icon = ImageTk.PhotoImage(icon_img)
        root.iconphoto(True, self._win_icon)

        self.monitor = UsageMonitor()
        self.monitor.start()

        self.devices = list_cameras()
        self._build_ui()
        self._select_initial_camera()

        if IS_WIN and get_autostart():
            # keep the registry entry pointing at this exe if it was moved
            try:
                set_autostart(True)
            except Exception:
                traceback.print_exc()

        self.tray = self._start_tray(icon_img)

        self.start_pipeline()
        root.protocol("WM_DELETE_WINDOW", self.request_quit)
        root.bind("<Unmap>", self.on_unmap)
        root.after(40, self.refresh)
        if start_minimized:
            if self.tray is not None:
                root.withdraw()
            else:
                root.iconify()

    # ---- tray

    def _start_tray(self, image):
        if pystray is None:
            return None
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open CamTuner", lambda icon, item: self._events.put("show"),
                                 default=True),
                pystray.MenuItem("Quit", lambda icon, item: self._events.put("quit")),
            )
            icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
            threading.Thread(target=icon.run, daemon=True).start()
            return icon
        except Exception:
            traceback.print_exc()
            return None

    def on_unmap(self, e):
        # Minimize button: hide to the tray instead of the taskbar
        if e.widget is self.root and self.tray is not None and not self._quitting:
            if self.root.state() == "iconic":
                self.root.after(10, self.root.withdraw)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def request_quit(self):
        self.show_window()
        if messagebox.askyesno(
                APP_NAME,
                "Quit CamTuner?\n\nApps using OBS Virtual Camera will stop getting your "
                "adjusted video.\n\nTo keep it running in the background, minimize it "
                "to the tray instead.",
                parent=self.root):
            self.quit()

    def quit(self):
        self._quitting = True
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        save_settings(self.settings)
        if self.pipeline is not None:
            self.pipeline.stop()
            self.pipeline.join(timeout=3)
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                traceback.print_exc()
        self.root.destroy()

    # ---- layout

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=8)
        outer.grid(sticky="nsew")

        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="n")
        self.preview = tk.Label(left, bg="black", width=PREV_W, height=PREV_H, cursor="fleur")
        self.preview.grid(row=0, column=0)
        self._blank = ImageTk.PhotoImage(Image.new("RGB", (PREV_W, PREV_H), (0, 0, 0)))
        self.preview.configure(image=self._blank)
        self.preview.bind("<ButtonPress-1>", self.on_press)
        self.preview.bind("<B1-Motion>", self.on_drag)
        self.preview.bind("<MouseWheel>", self.on_wheel)

        under = ttk.Frame(left)
        under.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(under, text="Drag the picture to pan, scroll to zoom.").pack(side="left")
        self.show_orig = tk.BooleanVar(value=False)
        ttk.Checkbutton(under, text="Show original camera", variable=self.show_orig).pack(side="right")

        right = ttk.Frame(outer, padding=(10, 0, 0, 0))
        right.grid(row=0, column=1, sticky="n")

        ttk.Label(right, text="Webcam").grid(row=0, column=0, sticky="w")
        self.cam_combo = ttk.Combobox(right, state="readonly", width=34)
        self.cam_combo.grid(row=1, column=0, sticky="w")
        self.cam_combo.bind("<<ComboboxSelected>>", self.on_camera_change)
        ttk.Button(right, text="Refresh", command=self.refresh_devices).grid(row=1, column=1, padx=(4, 0))

        ttk.Label(right, text="Virtual camera resolution").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.out_combo = ttk.Combobox(right, state="readonly", width=34, values=OUTPUT_SIZES)
        self.out_combo.set(self.settings["output"])
        self.out_combo.grid(row=3, column=0, sticky="w")
        self.out_combo.bind("<<ComboboxSelected>>", self.on_output_change)
        ttk.Button(right, text="Restart", command=self.start_pipeline).grid(row=3, column=1, padx=(4, 0))

        nb = ttk.Notebook(right)
        nb.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(10, 0))

        framing = ttk.Frame(nb, padding=6)
        light = ttk.Frame(nb, padding=6)
        color = ttk.Frame(nb, padding=6)
        helptab = ttk.Frame(nb, padding=6)
        nb.add(framing, text="Framing")
        nb.add(light, text="Light")
        nb.add(color, text="Color")
        nb.add(helptab, text="Help")

        r = 0
        for key in ("zoom", "pan_x", "pan_y", "rotation"):
            r = self._slider(framing, key, r)
        btns = ttk.Frame(framing)
        btns.grid(row=r, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(btns, text="Rotate left 90", command=lambda: self.rotate_by(-90)).pack(side="left")
        ttk.Button(btns, text="Rotate right 90", command=lambda: self.rotate_by(90)).pack(side="left", padx=4)
        r += 1
        for key, text in (("flip_h", "Mirror (flip left / right)"),
                          ("flip_v", "Flip upside down"),
                          ("auto_fill", "Auto zoom to hide corners when rotated")):
            v = tk.BooleanVar(value=bool(self.settings[key]))
            self.vars[key] = v
            ttk.Checkbutton(framing, text=text, variable=v,
                            command=lambda k=key, vv=v: self.set_value(k, bool(vv.get()))
                            ).grid(row=r, column=0, columnspan=2, sticky="w")
            r += 1

        r = 0
        for key in ("exposure", "brightness", "contrast", "gamma"):
            r = self._slider(light, key, r)

        r = 0
        for key in ("saturation", "temperature", "tint", "sharpness"):
            r = self._slider(color, key, r)

        help_text = (
            "In Chrome or any app, choose the camera named\n"
            "\"OBS Virtual Camera\".\n\n"
            "Minimize CamTuner to keep it running in the tray.\n"
            "Quitting stops the adjusted video.\n\n"
            "If the virtual camera is not available:\n"
            "1. Install OBS Studio.\n"
            "2. Open OBS, click Start Virtual Camera,\n"
            "   then Stop Virtual Camera, then close OBS.\n"
            "3. Press Restart here."
        )
        ttk.Label(helptab, text=help_text, justify="left").grid(row=0, column=0, sticky="w")

        self.on_demand_var = tk.BooleanVar(value=bool(self.settings["on_demand"]))
        ttk.Checkbutton(helptab, text="Webcam on only while an app is using it",
                        variable=self.on_demand_var,
                        command=lambda: self.set_value("on_demand", bool(self.on_demand_var.get()))
                        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.users_label = ttk.Label(helptab, text="", justify="left", wraplength=320)
        self.users_label.grid(row=2, column=0, sticky="w", padx=(20, 0))

        self.autostart_var = tk.BooleanVar(value=get_autostart())
        ttk.Checkbutton(helptab, text="Start with Windows (in the tray)", variable=self.autostart_var,
                        command=self.on_autostart).grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Button(helptab, text="Open settings folder", command=self.open_data_dir
                   ).grid(row=4, column=0, sticky="w", pady=(6, 0))

        ttk.Button(right, text="Reset all adjustments", command=self.reset_all
                   ).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        self.status = ttk.Label(outer, text="", anchor="w", wraplength=PREV_W + 360)
        self.status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _slider(self, parent, key, row):
        label, lo, hi, res = self.SLIDERS[key]
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(4, 0))
        var = tk.DoubleVar(value=float(self.settings[key]))
        self.vars[key] = var
        sc = tk.Scale(parent, from_=lo, to=hi, resolution=res, orient="horizontal", length=300,
                      variable=var, showvalue=True,
                      command=lambda v, k=key: self.set_value(k, float(v), update_var=False))
        sc.grid(row=row + 1, column=0, sticky="w")
        ttk.Button(parent, text="Reset", width=6,
                   command=lambda k=key: self.set_value(k, proc.DEFAULTS[k])
                   ).grid(row=row + 1, column=1, padx=(4, 0), sticky="s")
        return row + 2

    # ---- settings changes

    def set_value(self, key, value, update_var=True):
        if key in self.SLIDERS:
            _, lo, hi, _ = self.SLIDERS[key]
            value = min(max(float(value), lo), hi)
        self.settings[key] = value
        if update_var and key in self.vars:
            self.vars[key].set(value)
        self.schedule_save()

    def schedule_save(self):
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(800, self._save_now)

    def _save_now(self):
        self._save_job = None
        save_settings(self.settings)

    def rotate_by(self, deg):
        r = float(self.settings["rotation"]) + deg
        r = ((r + 180.0) % 360.0) - 180.0
        self.set_value("rotation", r)

    def reset_all(self):
        for k, v in proc.DEFAULTS.items():
            self.set_value(k, v)

    def on_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror(APP_NAME, "Could not change the startup setting:\n%s" % e)
        self.autostart_var.set(get_autostart())

    def open_data_dir(self):
        if IS_WIN:
            os.startfile(DATA_DIR)

    # ---- cameras and pipeline

    def _device_label(self, i, name):
        return "%d: %s" % (i, name)

    def _fill_camera_combo(self):
        self.cam_combo["values"] = [self._device_label(i, n) for i, n in self.devices]

    def _select_initial_camera(self):
        self._fill_camera_combo()
        chosen = None
        saved = self.settings.get("camera_name", "")
        for i, n in self.devices:
            if saved and n == saved:
                chosen = (i, n)
                break
        if chosen is None:
            idx = int(self.settings.get("camera_index", 0))
            for i, n in self.devices:
                if i == idx and not is_virtual(n):
                    chosen = (i, n)
                    break
        if chosen is None:
            for i, n in self.devices:
                if not is_virtual(n):
                    chosen = (i, n)
                    break
        if chosen is None and self.devices:
            chosen = self.devices[0]
        if chosen is not None:
            self.settings["camera_index"], self.settings["camera_name"] = chosen[0], chosen[1]
            self.cam_combo.set(self._device_label(*chosen))

    def refresh_devices(self):
        self.devices = list_cameras()
        self._select_initial_camera()

    def on_camera_change(self, _evt=None):
        label = self.cam_combo.get()
        idx = int(label.split(":", 1)[0])
        name = label.split(":", 1)[1].strip()
        if is_virtual(name):
            messagebox.showwarning(APP_NAME, "That is the virtual camera CamTuner sends to. "
                                             "Pick your real webcam instead.")
            self._select_initial_camera()
            return
        self.settings["camera_index"], self.settings["camera_name"] = idx, name
        self.schedule_save()
        self.start_pipeline()

    def on_output_change(self, _evt=None):
        self.settings["output"] = self.out_combo.get()
        self.schedule_save()
        self.start_pipeline()

    def start_pipeline(self):
        if self.pipeline is not None:
            self.pipeline.stop()
            self.pipeline.join(timeout=3)
        self._shown = None
        out = proc.parse_size(self.settings["output"])
        self.pipeline = Pipeline(self.settings, int(self.settings["camera_index"]), out, self.monitor)
        self.pipeline.preview_wanted = self._window_visible()
        self.pipeline.start()

    def _window_visible(self):
        try:
            return self.root.state() not in ("iconic", "withdrawn")
        except tk.TclError:
            return False

    # ---- periodic update

    def refresh(self):
        if self._quitting:
            return
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                break
            if ev == "show":
                self.show_window()
            elif ev == "quit":
                self.request_quit()
                if self._quitting:
                    return

        p = self.pipeline
        visible = self._window_visible()
        if p is not None:
            p.preview_wanted = visible
        if p is not None and visible:
            frame = p.latest_raw if self.show_orig.get() else p.latest
            if frame is not None and frame is not self._shown:
                self._shown = frame
                h, w = frame.shape[:2]
                sc = min(PREV_W / w, PREV_H / h)
                nw, nh = max(1, int(w * sc)), max(1, int(h * sc))
                small = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
                canvas = np.zeros((PREV_H, PREV_W, 3), dtype=np.uint8)
                y0, x0 = (PREV_H - nh) // 2, (PREV_W - nw) // 2
                canvas[y0:y0 + nh, x0:x0 + nw] = small
                rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
                self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
                self.preview.configure(image=self._photo)
                if not self.show_orig.get():
                    self._prev_scale = sc
            parts = [p.cam_status, p.vcam_status]
            self.status.configure(text="   |   ".join(x for x in parts if x))

            if not self.monitor.available:
                users_text = "App detection is not available on this system."
            elif self.monitor.error:
                users_text = "App detection error: %s" % self.monitor.error
            elif self.monitor.details:
                labels = {"active": "using the camera",
                          "idle": "not using the camera",
                          "unknown": "cannot check, treated as using it"}
                # one line per program, showing its most "in use" process
                rank = {"idle": 0, "unknown": 1, "active": 2}
                best = {}
                for d in self.monitor.details:
                    if rank[d["state"]] > rank[best.get(d["program"], "idle")] \
                            or d["program"] not in best:
                        best[d["program"]] = d["state"]
                users_text = "\n".join("%s: %s" % (p, labels[s]) for p, s in sorted(best.items()))
            else:
                users_text = "No app is using the virtual camera right now."
            if visible and self.settings.get("on_demand", True):
                users_text += "\n(The webcam stays on while this window is open.)"
            self.users_label.configure(text=users_text)
        self.root.after(40, self.refresh)

    # ---- mouse

    def on_press(self, e):
        self._drag = (e.x, e.y)

    def on_drag(self, e):
        if self._drag is None:
            return
        dx, dy = e.x - self._drag[0], e.y - self._drag[1]
        self._drag = (e.x, e.y)
        p = self.pipeline
        if p is None or p.geom is None or self.show_orig.get():
            return
        sc = self._prev_scale or 1.0
        dpx, dpy = proc.pan_delta_for_drag(p.geom, dx / sc, dy / sc)
        if dpx:
            self.set_value("pan_x", float(self.settings["pan_x"]) + dpx)
        if dpy:
            self.set_value("pan_y", float(self.settings["pan_y"]) + dpy)

    def on_wheel(self, e):
        steps = e.delta / 120.0
        self.set_value("zoom", float(self.settings["zoom"]) * (1.1 ** steps))


def main():
    enable_dpi_awareness()
    ensure_single_instance()
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if IS_WIN else "clam")
    except tk.TclError:
        pass
    App(root, start_minimized="--minimized" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise
