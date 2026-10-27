"""Android Auto 車載ナビ エミュレーター（Desktop Head Unit ランチャー）。

Google 公式の Desktop Head Unit (DHU) を使い、USB 接続したスマホの Android Auto を
PC 上の「車載ナビ画面」に映す。このアプリは次を GUI で行う。

- platform-tools / DHU の検出とインストール
- スマホの検出、Android Auto とヘッドユニットサーバーの状態確認
- adb のポート転送、DHU 設定ファイルの生成、DHU の起動と停止
- ステアリングスイッチ・昼夜・ギア・速度・GPS 走行などの車両操作（DHU コンソールへ送信）
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import sdk

APP_NAME = "Android Auto ヘッドユニット エミュレーター"
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "AAHeadUnitEmu"
SETTINGS_FILE = DATA_DIR / "settings.json"
INI_FILE = DATA_DIR / "headunit.ini"
LOG_FILE = DATA_DIR / "headunit.log"
UNKNOWN_COMMAND = re.compile(r'"(\w+)[^"]*" is not a known command')
PORT = 5277
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 画面プリセット: 表示名 -> DHU の [general] 設定
DISPLAY_PRESETS: dict[str, dict[str, str]] = {
    "標準 800×480": {"resolution": "800x480"},
    "HD 1280×720": {"resolution": "1280x720"},
    "フルHD 1920×1080": {"resolution": "1920x1080"},
    "ワイド 1920×720（上下マージン）": {"resolution": "1920x1080", "marginheight": "360"},
    "ワイド 1280×480（上下マージン）": {"resolution": "1280x720", "marginheight": "240"},
}
CONN_MODES = {
    "usb": "USB アクセサリーモード（実車と同じ接続方式）",
    "adb": "ADB 経由（ヘッドユニットサーバーを使う）",
}
INPUT_MODES = {"タッチ": "touch", "ロータリー（コマンダー）": "rotary", "ハイブリッド": "hybrid"}

# 皇居周回ルート（緯度, 経度）
ROUTE_TOKYO = [
    (35.6812, 139.7671), (35.6870, 139.7640), (35.6905, 139.7590), (35.6935, 139.7530),
    (35.6920, 139.7470), (35.6880, 139.7425), (35.6830, 139.7415), (35.6775, 139.7430),
    (35.6745, 139.7480), (35.6740, 139.7545), (35.6760, 139.7610), (35.6790, 139.7655),
]

DEFAULT_SETTINGS = {
    "sdk_path": "",
    "preset": "HD 1280×720",
    "dpi": 160,
    "input": "タッチ",
    "cluster": False,
    "playback": False,
    "driver_right": True,
    "conn": "usb",
}

PHONE_HELP = """■ USB アクセサリーモード（既定・実車と同じ方式）

実車と同じように USB の「アクセサリーモード（AOA）」でつなぐ方式です。
ヘッドユニットサーバーも adb のポート転送も使いません。

1. スマホを USB で PC につなぎ、画面のロックを解除して「▶ 起動」を押す。
2. スマホに Android Auto の確認画面が出たら許可する。

うまくいかないとき:
・DHU が「デバイスを開けない」等で失敗する場合、Windows では切り替わった後の
  USB デバイス（「Android Accessory Interface」など）に WinUSB ドライバーが必要です。
  Zadig（https://zadig.akeo.ie/）で該当デバイスに WinUSB を割り当ててください。
・Android Auto が DHU を受け付けない場合は、下の ADB 方式の 2〜5 を設定してください。
・スマホの機種一覧の表示には USB デバッグが必要ですが、この方式の接続自体には不要です
  （一覧が空でも起動でき、最初に見つかったスマホにつなぎます）。

■ ADB 経由（確実に動く開発用の方式）

1. スマホの「開発者向けオプション」で USB デバッグを ON にする。
2. Android Auto の設定を開く
   （Android 10 以降: 設定 → 接続済みのデバイス → 接続の設定 → Android Auto）。
3. 一番下の「バージョン」を 10 回タップして、Android Auto の開発者モードを有効にする。
4. 右上の︙メニュー →「ヘッドユニット サーバーを起動」をタップする。
   （通知に「ヘッドユニット サーバーが実行中」と出れば OK）
5. 設定の「以前接続された車」で「Android Auto に新しい車を追加」を ON にする。
6. USB で PC につなぎ、「USB デバッグを許可しますか？」で許可する。
7. 画面のロックを解除した状態で、このアプリの「▶ 起動」を押す。

初回接続時はスマホ側に利用規約や権限の確認が出るので、スマホの画面を見て許可してください。
スマホを再起動すると、ヘッドユニットサーバーは止まります（4 をやり直す）。"""

DRIVER_HELP = """スマホは USB アクセサリーモードに切り替わりましたが、
Windows 側に「Android Accessory Interface」用のドライバーが無いため、DHU が接続できませんでした。

【対処】そのインターフェイスに WinUSB ドライバーを割り当てます（初回のみ・管理者権限が必要）。

方法 A: デバイスマネージャー（追加ダウンロード不要）
 1. スマホを USB でつないだまま、下の「デバイスマネージャーを開く」を押す。
 2. 「ほかのデバイス」等にある「Android Accessory Interface」（⚠ 付き）を右クリック →「ドライバーの更新」。
 3. 「コンピューターを参照してドライバーを検索」→「コンピューター上の利用可能なドライバーの一覧から選択」。
 4. 「ユニバーサル シリアル バス デバイス」→ 製造元「WinUsb デバイス」→ モデル「WinUsb デバイス」→ 次へ（警告は「はい」）。

方法 B: Zadig（https://zadig.akeo.ie/）
 Options →「List All Devices」で「Android Accessory Interface (Interface 0)」を選び、WinUSB を選んで「Install Driver」。

※ 対象は USB ID が 18D1:2D01（または 2D00）のアクセサリー用デバイスだけです。
  通常のスマホ接続（ファイル転送・ADB）には影響しません。
割り当てたら、スマホのケーブルを一度抜き差ししてから、もう一度「▶ 起動」を押してください。"""

NO_ACCESSORY_HELP = """スマホが USB アクセサリーモードに切り替わりませんでした。

・スマホの画面ロックを解除してから、もう一度「▶ 起動」を押してください。
・Android Auto がインストールされているか確認してください。
・充電専用ケーブルではなく、データ通信できるケーブルを使ってください。
・うまくいかない場合は、接続方式を「ADB 経由」に切り替えてください。"""

# 配色（車載ナビ風のダークテーマ）
BG, PANEL, FG, SUB, ACCENT, OK, WARN, NG = (
    "#0f1419", "#1a2129", "#e6edf3", "#8b98a5", "#4c8dff", "#3fb950", "#d29922", "#f85149")


def run_cmd(args: list[str], timeout: float = 15) -> tuple[int, str]:
    # 出力はパイプではなく一時ファイルで受ける。adb が初回に起動するサーバー常駐プロセスが
    # パイプを掴んだままになり、パイプだと読み取りが永久に終わらないため。
    with tempfile.TemporaryFile() as out:
        try:
            p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 creationflags=NO_WINDOW)
        except OSError as e:
            return -1, str(e)
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            code = -1
        out.seek(0)
        return code, out.read().decode("utf-8", errors="replace")


def accessory_devices() -> list[dict]:
    """アクセサリーモード（Google VID 18D1, PID 2D00〜2D05）の AOA インターフェイスとドライバー状態。

    ADB インターフェイス（MI_01）は除外する。Service が WinUSB で無ければ DHU（libusb）から開けない。
    """
    script = ("Get-CimInstance Win32_PnPEntity -Filter \"DeviceID LIKE 'USB\\\\VID_18D1&PID_2D0%'\" | "
              "Select-Object Name, DeviceID, Service, ConfigManagerErrorCode | ConvertTo-Json -Compress")
    _, out = run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                      "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script], timeout=40)
    try:
        data = json.loads(out.strip() or "[]")
    except ValueError:
        return []
    result = []
    for d in data if isinstance(data, list) else [data]:
        dev_id = (d.get("DeviceID") or "").upper()
        # AOA は複合デバイスなら MI_00、単独（PID 2D00）ならデバイスそのもの
        if "&MI_00" in dev_id or ("PID_2D00" in dev_id and "&MI_" not in dev_id):
            result.append(d)
    return result


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def build_ini(s: dict) -> str:
    general = {"touch": "true", "dpi": str(int(s["dpi"])), "framerate": "30"}
    general.update(DISPLAY_PRESETS.get(s["preset"], DISPLAY_PRESETS["HD 1280×720"]))
    general["instrumentcluster"] = "true" if s["cluster"] else "false"
    general["playbackstatus"] = "true" if s["playback"] else "false"
    general["driverposition"] = "right" if s["driver_right"] else "left"
    general["fueltypes"] = "unleaded"
    sensors = ["location", "compass", "speed", "odometer", "fuel", "gear", "parking_brake",
               "night_mode", "driving_status"]
    lines = ["[general]"] + [f"{k} = {v}" for k, v in general.items()]
    lines += ["", "[sensors]"] + [f"{k} = true" for k in sensors]
    return "\n".join(lines) + "\n"


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class DhuProcess:
    """DHU プロセス。標準入力にコンソールコマンドを流し、出力を queue に集める。"""

    def __init__(self, out: queue.Queue):
        self.out = out
        self.proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, exe: Path, args: list[str]) -> None:
        self.proc = subprocess.Popen(
            [str(exe), *args], cwd=str(exe.parent), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        for raw in proc.stdout:
            self.out.put(("dhu", raw.decode("utf-8", errors="replace").rstrip()))
        self.out.put(("exit", proc.wait()))

    def send(self, command: str) -> bool:
        if not self.running or not self.proc or not self.proc.stdin:
            return False
        try:
            self.proc.stdin.write((command + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            return True
        except OSError:
            return False

    def stop(self) -> None:
        if not self.proc:
            return
        if self.running:
            self.send("quit")
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=BG)
        # 高 DPI（表示スケール 150% など）でもはみ出さない大きさにする
        self.scale = self.winfo_fpixels("1i") / 96
        w = min(int(1240 * self.scale), self.winfo_screenwidth() - 40)
        h = min(int(780 * self.scale), self.winfo_screenheight() - int(110 * self.scale))
        self.geometry(f"{w}x{h}+20+10")
        self.minsize(int(960 * self.scale), int(600 * self.scale))

        self.settings = load_settings()
        self.events: queue.Queue = queue.Queue()
        self.dhu = DhuProcess(self.events)
        self.status = sdk.detect(self.settings["sdk_path"] or None)
        self.devices: list[tuple[str, str, str]] = []  # (serial, state, model)
        self.busy = False

        # 車両シミュレーション状態
        self.sim_on = tk.BooleanVar(value=False)
        self.speed_kmh = tk.DoubleVar(value=0)
        self.fuel = tk.IntVar(value=70)
        self.gear = tk.StringVar(value="P")
        self.parking = tk.BooleanVar(value=True)
        self.auto_restrict = tk.BooleanVar(value=True)
        self.night = tk.BooleanVar(value=False)
        self.route_index = 0
        self.route_pos = ROUTE_TOKYO[0]
        self.odometer_km = 12345.0
        self.restricted: bool | None = None
        self.dhu_lines: list[str] = []
        self.closing = False
        self.unsupported: set[str] = set()

        self._style()
        self._build()
        self.refresh_sdk()
        self.after(200, self.refresh_devices)
        self.after(100, self._poll_events)
        self.after(1000, self._sim_tick)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- 見た目 ---------------------------------------------------------------

    def _style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        font = ("Yu Gothic UI", 10)
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=font,
                     bordercolor="#30363d", lightcolor=PANEL, darkcolor=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabelframe", background=PANEL, bordercolor="#30363d")
        st.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT, font=("Yu Gothic UI", 10, "bold"))
        st.configure("TLabel", background=PANEL, foreground=FG)
        st.configure("Sub.TLabel", background=PANEL, foreground=SUB)
        st.configure("Title.TLabel", background=BG, foreground=FG, font=("Yu Gothic UI", 16, "bold"))
        st.configure("Top.TLabel", background=BG, foreground=SUB)
        st.configure("TButton", background="#262f3a", foreground=FG, padding=(10, 6), borderwidth=0)
        st.map("TButton", background=[("disabled", "#1d242c"), ("active", "#33404f")],
               foreground=[("disabled", "#56606b")])
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Yu Gothic UI", 11, "bold"))
        st.map("Accent.TButton", background=[("disabled", "#1d2a40"), ("active", "#6aa1ff")])
        st.configure("Wheel.TButton", padding=(6, 12), font=("Yu Gothic UI", 10, "bold"))
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("TRadiobutton", background=PANEL, foreground=FG, indicatorbackground=PANEL,
                     indicatorforeground=FG)
        st.map("TRadiobutton", background=[("active", PANEL)],
               indicatorbackground=[("selected", ACCENT), ("!selected", PANEL)])
        st.configure("Toolbutton", background="#262f3a", foreground=FG, padding=(14, 8), font=("Yu Gothic UI", 11, "bold"))
        st.map("Toolbutton", background=[("selected", ACCENT), ("active", "#33404f")])
        st.configure("TCombobox", fieldbackground=PANEL, background="#262f3a", foreground=FG, arrowcolor=FG)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        st.configure("Horizontal.TScale", background=PANEL, troughcolor="#262f3a")
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#262f3a")
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)

    def _build(self) -> None:
        top = ttk.Frame(self, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        ttk.Label(top, text="🚗  " + APP_NAME, style="Title.TLabel").pack(side="left")
        self.run_label = tk.Label(top, text="● 停止中", bg=BG, fg=SUB, font=("Yu Gothic UI", 12, "bold"))
        self.run_label.pack(side="right")

        body = ttk.Frame(self, padding=(16, 4, 16, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=int(380 * self.scale))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_setup(left)
        self._build_controls(right)

        logf = ttk.LabelFrame(right, text="ログ", padding=6)
        logf.pack(fill="both", expand=True, pady=(10, 0))
        self.log_box = scrolledtext.ScrolledText(
            logf, height=4, bg="#0b0f13", fg="#c9d1d9", insertbackground=FG,
            font=("Consolas", 9), relief="flat", state="disabled")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("err", foreground=NG)
        self.log_box.tag_config("ok", foreground=OK)
        self.log_box.tag_config("cmd", foreground=ACCENT)

    def _build_setup(self, parent: ttk.Frame) -> None:
        # 1. SDK
        f = ttk.LabelFrame(parent, text="1. 必要なツール", padding=10)
        f.pack(fill="x", pady=(0, 10))
        self.sdk_path_label = ttk.Label(f, style="Sub.TLabel", wraplength=int(360 * self.scale))
        self.sdk_path_label.pack(anchor="w")
        self.adb_label = ttk.Label(f)
        self.adb_label.pack(anchor="w", pady=(4, 0))
        self.dhu_label = ttk.Label(f)
        self.dhu_label.pack(anchor="w")
        row = ttk.Frame(f, style="Panel.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self.install_btn = ttk.Button(row, text="ダウンロードしてインストール", command=self.install_tools)
        self.install_btn.pack(side="left")
        ttk.Button(row, text="SDK フォルダを指定…", command=self.choose_sdk).pack(side="left", padx=6)
        self.progress = ttk.Progressbar(f, mode="determinate")

        # 2. スマホ
        f = ttk.LabelFrame(parent, text="2. スマホ（USB 接続）", padding=10)
        f.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(f, style="Panel.TFrame")
        row.pack(fill="x")
        self.device_combo = ttk.Combobox(row, state="readonly", width=34)
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.device_combo.bind("<<ComboboxSelected>>", lambda e: self.check_phone())
        ttk.Button(row, text="再検出", command=self.refresh_devices).pack(side="left", padx=(6, 0))
        self.phone_label = ttk.Label(f, text="", wraplength=int(360 * self.scale), justify="left")
        self.phone_label.pack(anchor="w", pady=(6, 0))
        self.conn_var = tk.StringVar(value=self.settings["conn"] if self.settings["conn"] in CONN_MODES else "usb")
        for key, label in CONN_MODES.items():
            ttk.Radiobutton(f, text=label, value=key, variable=self.conn_var,
                            command=self.check_phone).pack(anchor="w")
        ttk.Button(f, text="スマホ側の準備手順を見る", command=self.show_help).pack(anchor="w", pady=(6, 0))

        # 3. 画面
        f = ttk.LabelFrame(parent, text="3. ナビ画面の設定", padding=10)
        f.pack(fill="x", pady=(0, 10))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="画面サイズ").grid(row=0, column=0, sticky="w", pady=2)
        self.preset_combo = ttk.Combobox(f, state="readonly", values=list(DISPLAY_PRESETS))
        self.preset_combo.set(self.settings["preset"])
        self.preset_combo.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(f, text="DPI").grid(row=1, column=0, sticky="w", pady=2)
        self.dpi_spin = ttk.Spinbox(f, from_=100, to=400, increment=10, width=8)
        self.dpi_spin.set(self.settings["dpi"])
        self.dpi_spin.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(f, text="操作方式").grid(row=2, column=0, sticky="w", pady=2)
        self.input_combo = ttk.Combobox(f, state="readonly", values=list(INPUT_MODES))
        self.input_combo.set(self.settings["input"])
        self.input_combo.grid(row=2, column=1, sticky="ew", pady=2)
        self.cluster_var = tk.BooleanVar(value=self.settings["cluster"])
        self.playback_var = tk.BooleanVar(value=self.settings["playback"])
        self.driver_right_var = tk.BooleanVar(value=self.settings["driver_right"])
        ttk.Checkbutton(f, text="メーターパネル（案内表示）ウィンドウ", variable=self.cluster_var).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(f, text="再生状況ウィンドウ", variable=self.playback_var).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(f, text="右ハンドル", variable=self.driver_right_var).grid(row=5, column=0, columnspan=2, sticky="w")

        row = ttk.Frame(parent)
        row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="▶  起動", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.stop_btn = ttk.Button(row, text="■  停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))

    def _build_controls(self, parent: ttk.Frame) -> None:
        self.control_widgets: list[tk.Widget] = []

        # ステアリングスイッチ
        f = ttk.LabelFrame(parent, text="ステアリングスイッチ / 本体ボタン", padding=10)
        f.pack(fill="x", pady=(0, 10))
        buttons = [
            ("⌂ ホーム", "keycode home"), ("↩ 戻る", "keycode back"), ("🧭 ナビ", "keycode navigation"),
            ("📞 電話", "keycode tel"), ("🎵 メディア", "keycode media"), ("🎤 音声", "mic begin"),
            ("⏮ 前の曲", "keycode media_previous"), ("⏯ 再生/停止", "keycode media_play_pause"),
            ("⏭ 次の曲", "keycode media_next"), ("✆ 発信", "keycode call"), ("✕ 終話", "keycode endcall"),
            ("🔍 検索", "keycode search"),
        ]
        for i, (label, cmd) in enumerate(buttons):
            b = ttk.Button(f, text=label, style="Wheel.TButton", command=lambda c=cmd: self.send(c))
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=3, pady=3)
            f.columnconfigure(i % 4, weight=1)
            self.control_widgets.append(b)
        # ロータリーコマンダー
        rot = ttk.Frame(f, style="Panel.TFrame")
        rot.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Label(rot, text="コマンダー:", style="Sub.TLabel").pack(side="left", padx=(3, 6))
        for label, cmd in [("⟲", "dpad rotate left"), ("⟳", "dpad rotate right"), ("▲", "dpad up"),
                           ("▼", "dpad down"), ("◀", "dpad left"), ("▶", "dpad right"), ("決定", "dpad click"),
                           ("戻る", "dpad back")]:
            b = ttk.Button(rot, text=label, width=5, command=lambda c=cmd: self.send(c))
            b.pack(side="left", padx=2)
            self.control_widgets.append(b)

        # 車両状態
        f = ttk.LabelFrame(parent, text="車両の状態", padding=10)
        f.pack(fill="x", pady=(0, 10))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="照明").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=0, column=1, sticky="w", pady=3)
        for label, val in [("☀ 昼", False), ("🌙 夜", True)]:
            b = ttk.Radiobutton(row, text=label, value=val, variable=self.night, style="Toolbutton",
                                command=self.apply_night)
            b.pack(side="left", padx=(0, 4))
            self.control_widgets.append(b)

        ttk.Label(f, text="シフト").grid(row=1, column=0, sticky="w")
        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=1, column=1, sticky="w", pady=3)
        for g in "PRND":
            b = ttk.Radiobutton(row, text=g, value=g, variable=self.gear, style="Toolbutton", command=self.apply_gear)
            b.pack(side="left", padx=(0, 4))
            self.control_widgets.append(b)
        b = ttk.Checkbutton(row, text="パーキングブレーキ", variable=self.parking, command=self.apply_parking)
        b.pack(side="left", padx=(12, 0))
        self.control_widgets.append(b)

        ttk.Label(f, text="車速").grid(row=2, column=0, sticky="w")
        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=2, column=1, sticky="ew", pady=3)
        s = ttk.Scale(row, from_=0, to=120, variable=self.speed_kmh, command=lambda v: self._speed_changed())
        s.pack(side="left", fill="x", expand=True)
        self.control_widgets.append(s)
        self.speed_label = ttk.Label(row, text="0 km/h", width=9)
        self.speed_label.pack(side="left", padx=(8, 0))

        ttk.Label(f, text="燃料").grid(row=3, column=0, sticky="w")
        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=3, column=1, sticky="ew", pady=3)
        s = ttk.Scale(row, from_=0, to=100, variable=self.fuel, command=lambda v: self._fuel_label())
        s.pack(side="left", fill="x", expand=True)
        s.bind("<ButtonRelease-1>", lambda e: self.apply_fuel())
        self.control_widgets.append(s)
        self.fuel_label = ttk.Label(row, text="70 %", width=9)
        self.fuel_label.pack(side="left", padx=(8, 0))

        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        b = ttk.Checkbutton(row, text="GPS 走行シミュレーション（皇居周回）", variable=self.sim_on,
                            command=self._sim_toggled)
        b.pack(side="left")
        self.control_widgets.append(b)
        b = ttk.Checkbutton(f, text="走行中は操作制限をかける（運転中の画面制限を再現）", variable=self.auto_restrict,
                            command=self.update_restriction)
        b.grid(row=5, column=0, columnspan=2, sticky="w")
        self.control_widgets.append(b)
        self.sim_label = ttk.Label(f, text="", style="Sub.TLabel")
        self.sim_label.grid(row=6, column=0, columnspan=2, sticky="w")

        # 音声・その他
        f = ttk.LabelFrame(parent, text="音声コマンド / その他", padding=10)
        f.pack(fill="x")
        row = ttk.Frame(f, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="録音音声:").pack(side="left")
        self.voice_combo = ttk.Combobox(row, state="readonly", width=22)
        self.voice_combo.pack(side="left", padx=6)
        b = ttk.Button(row, text="マイクに流す", command=self.play_voice)
        b.pack(side="left")
        self.control_widgets.append(b)
        b = ttk.Button(row, text="📷 スクリーンショット", command=self.screenshot)
        b.pack(side="right")
        self.control_widgets.append(b)

        row = ttk.Frame(f, style="Panel.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="DHU コマンド:").pack(side="left")
        self.cmd_entry = ttk.Entry(row)
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.cmd_entry.bind("<Return>", lambda e: self.send_entry())
        b = ttk.Button(row, text="送信", command=self.send_entry)
        b.pack(side="left")
        self.control_widgets += [self.cmd_entry, b]

        self.set_controls(False)

    # ---- 共通 -----------------------------------------------------------------

    def log(self, text: str, tag: str | None = None) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except OSError:
            pass
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n", tag or ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def in_thread(self, fn, *args) -> None:
        def wrapper():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - GUI にエラーを表示する
                self.events.put(("error", f"{type(e).__name__}: {e}"))
        threading.Thread(target=wrapper, daemon=True).start()

    def ui(self, fn, *args) -> None:
        """ワーカースレッドから UI 処理を依頼する。"""
        self.events.put(("call", (fn, args)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "dhu":
                    self.dhu_lines.append(payload)
                    m = UNKNOWN_COMMAND.search(payload)
                    if m and m.group(1) not in self.unsupported:
                        self.unsupported.add(m.group(1))
                        self.log(f"この DHU は「{m.group(1)}」に未対応のため、以後は送信しません", "err")
                        continue
                    low = payload.lower()
                    tag = "err" if payload.startswith("[E]") or "error" in low else None
                    self.log(payload, tag)
                elif kind == "exit":
                    self.on_dhu_exit(payload)
                elif kind == "error":
                    self.log("エラー: " + payload, "err")
                    self.set_busy(False)
                elif kind == "call":
                    fn, args = payload
                    fn(*args)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        running = self.dhu.running
        self.start_btn.configure(state="disabled" if busy or running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.install_btn.configure(state="disabled" if busy or running else "normal")

    def set_controls(self, enabled: bool) -> None:
        for w in self.control_widgets:
            try:
                w.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def current_settings(self) -> dict:
        try:
            dpi = max(80, min(480, int(self.dpi_spin.get())))
        except ValueError:
            dpi = 160
        self.settings.update(
            preset=self.preset_combo.get(), dpi=dpi, input=self.input_combo.get(),
            cluster=self.cluster_var.get(), playback=self.playback_var.get(),
            driver_right=self.driver_right_var.get(), conn=self.conn_var.get())
        return self.settings

    # ---- SDK ------------------------------------------------------------------

    def refresh_sdk(self) -> None:
        self.status = sdk.detect(self.settings["sdk_path"] or None)
        st = self.status
        self.sdk_path_label.configure(text=f"SDK: {st.root}")
        if st.adb:
            self.adb_label.configure(text=f"✔ adb（platform-tools {st.adb_rev or ''}）", foreground=OK)
        else:
            self.adb_label.configure(text="✘ adb が見つかりません", foreground=NG)
        if st.dhu:
            self.dhu_label.configure(text=f"✔ Desktop Head Unit {st.dhu_rev or ''}", foreground=OK)
            voices = sorted(p.name for p in (st.dhu.parent / "voice").glob("*.wav"))
            self.voice_combo.configure(values=voices)
            if voices and not self.voice_combo.get():
                self.voice_combo.set(voices[0])
        else:
            self.dhu_label.configure(text="✘ Desktop Head Unit が見つかりません", foreground=NG)
        self.install_btn.configure(
            text="ダウンロードしてインストール" if not (st.adb and st.dhu) else "最新版に更新")

    def choose_sdk(self) -> None:
        d = filedialog.askdirectory(title="Android SDK フォルダを選択", initialdir=str(self.status.root))
        if d:
            self.settings["sdk_path"] = d
            save_settings(self.settings)
            self.refresh_sdk()
            self.refresh_devices()

    def install_tools(self) -> None:
        self.set_busy(True)
        self.log("Google の SDK リポジトリを確認しています…")
        self.in_thread(self._install_fetch)

    def _install_fetch(self) -> None:
        packages, licenses = sdk.fetch_remote()
        missing = [p for p in (sdk.PKG_PLATFORM_TOOLS, sdk.PKG_DHU) if p not in packages]
        if missing:
            raise RuntimeError("リポジトリにパッケージが見つかりません: " + ", ".join(missing))
        lic_ids = {p.license_id for p in packages.values()}
        lic_text = "\n\n".join(licenses.get(i, "") for i in lic_ids)
        self.ui(self._install_confirm, packages, lic_text)

    def _install_confirm(self, packages: dict, lic_text: str) -> None:
        root = self.status.root
        dlg = tk.Toplevel(self, bg=BG)
        dlg.title("Android SDK ライセンス")
        dlg.geometry("720x560")
        dlg.transient(self)
        dlg.grab_set()
        names = "\n".join(f"・{p.path} {p.revision}（{p.size / 1e6:.1f} MB）" for p in packages.values())
        tk.Label(dlg, bg=BG, fg=FG, justify="left", font=("Yu Gothic UI", 10), anchor="w",
                 text=f"次のパッケージを {root} にインストールします。\n{names}\n\n"
                      "続けるには、以下の Android SDK ライセンスに同意する必要があります。"
                 ).pack(fill="x", padx=12, pady=10)
        box = scrolledtext.ScrolledText(dlg, bg="#0b0f13", fg="#c9d1d9", font=("Consolas", 9), wrap="word")
        box.insert("1.0", lic_text)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, padx=12)
        result = {"ok": False}

        def done(ok: bool) -> None:
            result["ok"] = ok
            dlg.destroy()
        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=12, pady=10)
        ttk.Button(row, text="同意してインストール", style="Accent.TButton", command=lambda: done(True)).pack(side="right")
        ttk.Button(row, text="キャンセル", command=lambda: done(False)).pack(side="right", padx=8)
        self.wait_window(dlg)

        if not result["ok"]:
            self.log("インストールをキャンセルしました。")
            self.set_busy(False)
            return
        self.progress.pack(fill="x", pady=(8, 0))
        self.in_thread(self._install_run, list(packages.values()), root)

    def _install_run(self, packages: list, root: Path) -> None:
        # 更新時に adb.exe が使用中だと置き換えられないので、サーバーを止めておく
        if self.status.adb and self.status.adb.exists():
            run_cmd([str(self.status.adb), "kill-server"], timeout=10)
        for pkg in packages:
            self.ui(self.log, f"ダウンロード中: {pkg.path} {pkg.revision}")
            dest = sdk.install(pkg, root, lambda name, d, t: self.ui(self._progress, d, t))
            self.ui(self.log, f"インストールしました: {dest}", "ok")
        self.ui(self._install_done)

    def _progress(self, done: int, total: int) -> None:
        self.progress.configure(maximum=max(total, 1), value=done)

    def _install_done(self) -> None:
        self.progress.pack_forget()
        self.refresh_sdk()
        self.set_busy(False)
        self.refresh_devices()

    # ---- スマホ ---------------------------------------------------------------

    def adb(self, *args: str, serial: str | None = None, timeout: float = 15) -> tuple[int, str]:
        if not self.status.adb:
            return -1, "adb がありません"
        cmd = [str(self.status.adb)] + (["-s", serial] if serial else []) + list(args)
        return run_cmd(cmd, timeout)

    def selected_serial(self) -> str | None:
        i = self.device_combo.current()
        return self.devices[i][0] if 0 <= i < len(self.devices) else None

    def refresh_devices(self) -> None:
        if not self.status.adb:
            self.device_combo.configure(values=[])
            self.device_combo.set("")
            self.phone_label.configure(text="先に「1. 必要なツール」をインストールしてください。", foreground=WARN)
            return
        self.phone_label.configure(text="検出中…", foreground=SUB)
        self.in_thread(self._refresh_devices_worker)

    def _refresh_devices_worker(self) -> None:
        code, out = self.adb("devices", "-l", timeout=20)
        devices = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2 or line.startswith("List of") or line.startswith("*"):
                continue
            model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
            devices.append((parts[0], parts[1], model.replace("_", " ")))
        self.ui(self._devices_loaded, devices)

    def _devices_loaded(self, devices: list) -> None:
        prev = self.selected_serial()
        self.devices = devices
        labels = []
        for serial, state, model in devices:
            note = {"device": "", "unauthorized": "（未許可）", "offline": "（オフライン）"}.get(state, f"（{state}）")
            labels.append(f"{model or serial}  [{serial}]{note}")
        self.device_combo.configure(values=labels)
        if not devices:
            self.device_combo.set("")
            self.phone_label.configure(
                text="スマホが見つかりません。USB で接続し、USB デバッグを許可してください。", foreground=WARN)
            return
        serials = [d[0] for d in devices]
        self.device_combo.current(serials.index(prev) if prev in serials else 0)
        self.check_phone()

    def check_phone(self) -> None:
        i = self.device_combo.current()
        if i < 0:
            return
        serial, state, _ = self.devices[i]
        if state == "unauthorized":
            self.phone_label.configure(text="スマホの画面で「USB デバッグを許可」をタップしてから再検出してください。", foreground=WARN)
            return
        if state != "device":
            self.phone_label.configure(text=f"スマホの状態: {state}。ケーブルを挿し直してください。", foreground=WARN)
            return
        self.phone_label.configure(text="スマホを確認中…", foreground=SUB)
        self.in_thread(self._check_phone_worker, serial)

    def _check_phone_worker(self, serial: str) -> None:
        _, pk = self.adb("shell", "pm", "list", "packages", sdk.GEARHEAD_PKG, serial=serial)
        has_aa = f"package:{sdk.GEARHEAD_PKG}" in pk
        _, ver = self.adb("shell", "dumpsys", "package", sdk.GEARHEAD_PKG, serial=serial, timeout=20)
        version = next((l.split("=", 1)[1] for l in ver.splitlines() if "versionName=" in l), "").strip()
        server = self._head_unit_server_state(serial)
        self.ui(self._phone_checked, has_aa, version, server)

    def _head_unit_server_state(self, serial: str) -> bool | None:
        """スマホ側で 5277 番ポートが LISTEN しているか。読めなければ None。"""
        _, out = self.adb("shell", "cat", "/proc/net/tcp", "/proc/net/tcp6", serial=serial)
        rows = [l.split() for l in out.splitlines() if ":" in l and len(l.split()) > 3]
        if not rows:
            return None
        hex_port = f"{PORT:04X}"
        return any(r[1].endswith(":" + hex_port) and r[3] == "0A" for r in rows)

    def _phone_checked(self, has_aa: bool, version: str, server: bool | None) -> None:
        lines, color = [], OK
        if has_aa:
            lines.append(f"✔ Android Auto {version}")
        else:
            lines.append("✘ Android Auto が入っていません（Play ストアからインストール）")
            color = NG
        if self.conn_var.get() == "usb":
            lines.append("✔ USB アクセサリーモードで接続します（ヘッドユニットサーバー不要）")
        elif server is True:
            lines.append("✔ ヘッドユニットサーバー起動中")
        elif server is False:
            lines.append("✘ ヘッドユニットサーバーが動いていません（準備手順の 4 を参照）")
            color = NG if color == NG else WARN
        else:
            lines.append("? ヘッドユニットサーバーの状態を確認できませんでした")
            color = NG if color == NG else WARN
        self.phone_label.configure(text="\n".join(lines), foreground=color)

    def show_help(self) -> None:
        messagebox.showinfo("スマホ側の準備", PHONE_HELP, parent=self)

    # ---- 起動・停止 -----------------------------------------------------------

    def start(self) -> None:
        if not (self.status.adb and self.status.dhu):
            messagebox.showwarning(APP_NAME, "先に「ダウンロードしてインストール」で必要なツールを入れてください。", parent=self)
            return
        serial = self.selected_serial()
        if not serial and self.conn_var.get() != "usb":
            messagebox.showwarning(APP_NAME, "スマホが選ばれていません。USB で接続して「再検出」を押してください。", parent=self)
            return
        s = self.current_settings()
        save_settings(s)
        self.set_busy(True)
        self.in_thread(self._start_worker, serial, s)

    def _start_worker(self, serial: str | None, s: dict) -> None:
        if s["conn"] == "usb":
            accessories = accessory_devices()
            bad = [d for d in accessories if (d.get("Service") or "").lower() != "winusb"]
            if bad:
                self.ui(self.log, f"アクセサリー用インターフェイスにドライバーがありません: {bad[0].get('Name')}"
                                  f"（エラーコード {bad[0].get('ConfigManagerErrorCode')}）", "err")
                self.ui(self.show_driver_help)
                self.ui(self.set_busy, False)
                return
            # 前回のセッションが残ったアクセサリーモードのままだと、スマホと同期が取れず即終了する
            if accessories and not self.reset_accessory(serial):
                self.ui(self.log, "スマホがアクセサリーモードのまま戻りません。ケーブルを抜き差ししてください。", "err")
                self.ui(self.set_busy, False)
                return
            # adb が USB デバイスを掴んでいると DHU がアクセサリーモードに切り替えられない
            self.ui(self.log, "adb サーバーを停止し、USB アクセサリーモードで接続します", "cmd")
            self.adb("kill-server", timeout=10)
            transport = f"--usb={serial}" if serial else "--usb"
        else:
            self.ui(self.log, f"ポートを転送します: tcp:{PORT} → スマホ tcp:{PORT}", "cmd")
            code, out = self.adb("forward", f"tcp:{PORT}", f"tcp:{PORT}", serial=serial)
            if code != 0:
                raise RuntimeError("adb forward に失敗しました: " + out.strip())
            transport = f"--adb={PORT}"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        INI_FILE.write_text(build_ini(s), encoding="utf-8")
        args = ["-c", str(INI_FILE), "-i", INPUT_MODES.get(s["input"], "touch"), transport]
        self.ui(self.log, f"DHU を起動します（{s['preset']} / {s['dpi']}dpi / {s['input']} / {transport}）", "cmd")
        self.dhu_lines = []
        self.dhu.start(self.status.dhu, args)
        self.ui(self._started)

    def reset_accessory(self, serial: str | None) -> bool:
        """スマホの USB をつなぎ直させてアクセサリーモードを解除する（ケーブルの抜き差しの代わり）。

        アクセサリーモード中も ADB インターフェイスは生きているので、adb で USB 機能を
        設定し直すとスマホが再接続し、通常モードで見え直す。USB デバッグが許可されていることが前提。
        """
        self.ui(self.log, "前回のアクセサリーモードを解除しています（USB を再接続）…", "cmd")
        code, out = self.adb("shell", "svc", "usb", "setFunctions", serial=serial, timeout=20)
        if code != 0 and "setCurrentFunctions" not in out:
            self.ui(self.log, "USB のリセットに失敗しました: " + out.strip(), "err")
            return False
        for _ in range(15):
            time.sleep(1)
            if not accessory_devices():
                time.sleep(1)  # 通常モードでの再認識を待つ
                return True
        return False

    def _started(self) -> None:
        self.run_label.configure(text="● 起動中", fg=OK)
        self.set_busy(False)
        self.set_controls(True)
        self.restricted = None
        # 現在の車両状態を DHU に反映
        self.after(2500, self.push_vehicle_state)

    def push_vehicle_state(self) -> None:
        if not self.dhu.running:
            return
        self.apply_night()
        self.apply_gear()
        self.apply_parking()
        self.apply_fuel()
        self.send(f"location {self.route_pos[0]:.6f} {self.route_pos[1]:.6f} 5 40 0 0", quiet=True)
        self.update_restriction()

    def stop(self) -> None:
        self.stop_btn.configure(state="disabled")
        self.in_thread(self.dhu.stop)

    def on_dhu_exit(self, code: int) -> None:
        # DHU は接続に失敗しても終了コード 0 を返すので、出力から判定する
        failed = code != 0 or any(m in l for l in self.dhu_lines
                                  for m in ("Failed to start", "Out of sync", "Unrecoverable error"))
        self.log(f"DHU が{'異常' if failed else ''}終了しました（終了コード {code}）", "err" if failed else None)
        if failed:
            self.log(f"ログファイル: {LOG_FILE}")
            if self.settings.get("conn") == "usb" and any("compatible USB device" in l for l in self.dhu_lines):
                self.in_thread(self._diagnose_usb)
        self.run_label.configure(text="● 停止中", fg=SUB)
        self.set_controls(False)
        self.sim_on.set(False)
        self.set_busy(False)
        serial = self.selected_serial()
        if self.settings.get("conn") == "adb":
            self.in_thread(lambda: self.adb("forward", "--remove", f"tcp:{PORT}", serial=serial))
        elif not self.closing:
            # 停止したらスマホを通常モードに戻しておく（次回の起動と、スマホ側の表示のため）
            self.set_busy(True)
            self.in_thread(self._release_accessory, serial)

    def _release_accessory(self, serial: str | None) -> None:
        try:
            if accessory_devices():
                self.reset_accessory(serial)
        finally:
            self.ui(self.set_busy, False)

    def _diagnose_usb(self) -> None:
        devices = accessory_devices()
        if any((d.get("Service") or "").lower() != "winusb" for d in devices):
            self.ui(self.log, "原因: アクセサリー用インターフェイスに WinUSB ドライバーが割り当てられていません", "err")
            self.ui(self.show_driver_help)
        elif not devices:
            self.ui(self.log, "原因: スマホが USB アクセサリーモードに切り替わっていません", "err")
            self.ui(messagebox.showwarning, APP_NAME, NO_ACCESSORY_HELP)
        else:
            self.ui(self.log, "ドライバーは WinUSB です。ケーブルを抜き差しして再度お試しください。", "err")

    def show_driver_help(self) -> None:
        dlg = tk.Toplevel(self, bg=BG)
        dlg.title("USB ドライバーの設定が必要です")
        dlg.transient(self)
        tk.Label(dlg, text=DRIVER_HELP, bg=BG, fg=FG, justify="left", font=("Yu Gothic UI", 10),
                 wraplength=int(640 * self.scale)).pack(padx=16, pady=12)
        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=16, pady=(0, 12))
        ttk.Button(row, text="閉じる", command=dlg.destroy).pack(side="right")
        ttk.Button(row, text="Zadig のサイトを開く",
                   command=lambda: os.startfile("https://zadig.akeo.ie/")).pack(side="right", padx=8)
        ttk.Button(row, text="デバイスマネージャーを開く", style="Accent.TButton",
                   command=lambda: os.startfile("devmgmt.msc")).pack(side="right")

    def on_close(self) -> None:
        self.current_settings()
        save_settings(self.settings)
        self.closing = True
        if self.dhu.running:
            self.dhu.stop()
            if self.settings.get("conn") == "usb" and accessory_devices():
                self.reset_accessory(self.selected_serial())
        self.destroy()

    # ---- 車両操作 -------------------------------------------------------------

    def send(self, command: str, quiet: bool = False) -> None:
        # DHU のバージョンによって無いコマンド（2.0 の gear / parking_brake など）は送らない
        parts = [c for c in (p.strip() for p in command.split(";")) if c.split()[0] not in self.unsupported]
        if not parts:
            return
        command = "; ".join(parts)
        if not self.dhu.send(command):
            if not quiet:
                self.log("DHU が起動していないため送信できません: " + command, "err")
            return
        if not quiet:
            self.log("> " + command, "cmd")

    def send_entry(self) -> None:
        cmd = self.cmd_entry.get().strip()
        if cmd:
            self.send(cmd)
            self.cmd_entry.delete(0, "end")

    def apply_night(self) -> None:
        self.send("night" if self.night.get() else "day")

    def apply_gear(self) -> None:
        self.send("gear " + {"P": "101", "R": "102", "N": "0", "D": "100"}[self.gear.get()])
        if self.gear.get() in "PN" and self.speed_kmh.get() > 0:
            self.speed_kmh.set(0)
            self._speed_changed()
        self.update_restriction()

    def apply_parking(self) -> None:
        self.send("parking_brake " + ("true" if self.parking.get() else "false"))
        self.update_restriction()

    def apply_fuel(self) -> None:
        self._fuel_label()
        self.send(f"fuel {self.fuel.get()}")

    def _fuel_label(self) -> None:
        self.fuel.set(int(float(self.fuel.get())))
        self.fuel_label.configure(text=f"{self.fuel.get()} %")

    def _speed_changed(self) -> None:
        kmh = round(self.speed_kmh.get())
        self.speed_label.configure(text=f"{kmh} km/h")
        if kmh > 0 and self.gear.get() in "PN":
            self.gear.set("D")
            self.apply_gear()
        if kmh > 0 and self.parking.get():
            self.parking.set(False)
            self.apply_parking()
        self.update_restriction()

    def is_driving(self) -> bool:
        return self.speed_kmh.get() >= 1 and not self.parking.get() and self.gear.get() in "DR"

    def update_restriction(self) -> None:
        want = self.auto_restrict.get() and self.is_driving()
        if want != self.restricted and self.dhu.running:
            self.restricted = want
            self.send("restrict all" if want else "restrict none")

    def _sim_toggled(self) -> None:
        if self.sim_on.get() and self.speed_kmh.get() < 1:
            self.speed_kmh.set(40)
            self._speed_changed()
        if not self.sim_on.get():
            self.sim_label.configure(text="")

    def _sim_tick(self) -> None:
        try:
            if self.dhu.running and self.sim_on.get():
                self._advance(1.0)
            elif self.dhu.running and self.speed_kmh.get() >= 1:
                self.send(f"speed {self.speed_kmh.get() / 3.6:.2f}", quiet=True)
        finally:
            self.after(1000, self._sim_tick)

    def _advance(self, dt: float) -> None:
        v = self.speed_kmh.get() / 3.6 if self.gear.get() == "D" else 0.0
        remaining = v * dt
        while remaining > 0:
            target = ROUTE_TOKYO[(self.route_index + 1) % len(ROUTE_TOKYO)]
            dist = haversine_m(self.route_pos, target)
            if dist <= remaining:
                self.route_pos = target
                self.route_index = (self.route_index + 1) % len(ROUTE_TOKYO)
                remaining -= dist
            else:
                f = remaining / dist
                self.route_pos = (self.route_pos[0] + (target[0] - self.route_pos[0]) * f,
                                  self.route_pos[1] + (target[1] - self.route_pos[1]) * f)
                remaining = 0
        target = ROUTE_TOKYO[(self.route_index + 1) % len(ROUTE_TOKYO)]
        brg = bearing_deg(self.route_pos, target)
        self.odometer_km += v * dt / 1000
        lat, lon = self.route_pos
        self.send(f"location {lat:.6f} {lon:.6f} 5 40 {v:.2f} {brg:.1f}; speed {v:.2f}; "
                  f"compass {brg:.1f}; odometer {self.odometer_km:.2f}", quiet=True)
        self.sim_label.configure(text=f"現在地 {lat:.5f}, {lon:.5f}  方位 {brg:.0f}°  走行距離 {self.odometer_km:.1f} km")

    def play_voice(self) -> None:
        name = self.voice_combo.get()
        if name and self.status.dhu:
            self.send(f"mic play {(self.status.dhu.parent / 'voice' / name).as_posix()}")

    def screenshot(self) -> None:
        pics = Path.home() / "Pictures" / "AAHeadUnit"
        pics.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        path = pics / f"headunit_{datetime.now():%Y%m%d_%H%M%S}.png"
        self.send(f"screenshot {path.as_posix()}")
        self.log(f"保存先: {path}")


def main() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高 DPI で文字がぼやけないように
    except (AttributeError, OSError):
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
