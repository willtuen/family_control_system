import tkinter as tk
from tkinter import font as tkfont
import threading
import time
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import subprocess
import sys
import os
import yaml

# 读取配置
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
with open(config_path) as f:
    config = yaml.safe_load(f)

raw_server_url = str(config.get('server', {}).get('url', '')).strip()
if not raw_server_url:
    raw_server_url = "http://127.0.0.1:5000"
if not raw_server_url.startswith(("http://", "https://")):
    raw_server_url = f"http://{raw_server_url}"
if raw_server_url.startswith("https://"):
    raw_server_url = "http://" + raw_server_url[len("https://"):]
SERVER_URL = raw_server_url.rstrip("/")

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 12
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

SESSION = requests.Session()
retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.4,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset(["POST"]),
)
adapter = HTTPAdapter(max_retries=retry)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

CURRENT_KID = None
REMAINING_SECONDS = None
TIMER_WINDOW = None
TIMER_LABEL = None
TIMER_JOB = None
COOLDOWN_REMAINING = None
BALANCE_SECONDS = None
BALANCE_LABEL = None
DAILY_LABEL = None
COOLDOWN_LABEL = None
IS_TEMP_ENTRY = None
TEMP_REMAINING_SECONDS = None

def post_api(path, payload, timeout=DEFAULT_TIMEOUT):
    return SESSION.post(f"{SERVER_URL}{path}", json=payload, timeout=timeout)

def format_remaining(seconds):
    if seconds is None:
        return "同步中"
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def format_cooldown(seconds):
    if seconds is None or seconds <= 0:
        return ""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"冷却中 {hours:02d}:{minutes:02d}:{secs:02d}"

def parse_time_string_to_seconds(value):
    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d+", text):
        return max(0, int(text))

    parts = text.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+", p.strip()) for p in parts):
        nums = [int(p.strip()) for p in parts]
        if len(nums) == 2:
            minutes, seconds = nums
            return max(0, minutes * 60 + seconds)
        hours, minutes, seconds = nums
        return max(0, hours * 3600 + minutes * 60 + seconds)

    return None

def parse_remaining_value(key, value):
    if value is None:
        return None

    key_name = str(key).lower()

    if isinstance(value, (int, float)):
        number = float(value)
        if "minute" in key_name or "分钟" in key_name:
            return max(0, int(number * 60))
        if "hour" in key_name or "小时" in key_name:
            return max(0, int(number * 3600))
        if "millisecond" in key_name or key_name.endswith("_ms"):
            return max(0, int(number / 1000))
        return max(0, int(number))

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if any(token in key_name for token in ["minute", "minutes", "分钟"]):
            try:
                return max(0, int(float(text) * 60))
            except ValueError:
                pass
        return parse_time_string_to_seconds(text)

    return None

def extract_remaining_seconds(payload):
    if isinstance(payload, list):
        for item in payload:
            result = extract_remaining_seconds(item)
            if result is not None:
                return result
        return None

    if not isinstance(payload, dict):
        return None

    preferred_keys = [
        "daily_remaining_seconds",
        "remaining_seconds", "remain_seconds", "seconds_left", "left_seconds",
        "daily_remaining_minutes",
        "remaining_time", "time_left", "left_time", "time_remaining", "remaining",
        "remaining_minutes", "remain_minutes", "minutes_left", "left_minutes",
        "remaining_ms", "left_ms",
        "剩余秒数", "剩余时间", "剩余分钟",
    ]

    for key in preferred_keys:
        if key in payload:
            parsed = parse_remaining_value(key, payload.get(key))
            if parsed is not None:
                return parsed

    for key, value in payload.items():
        lower_key = str(key).lower()
        if any(token in lower_key for token in ["remain", "left", "time", "剩余"]):
            parsed = parse_remaining_value(key, value)
            if parsed is not None:
                return parsed

    for value in payload.values():
        if isinstance(value, (dict, list)):
            nested = extract_remaining_seconds(value)
            if nested is not None:
                return nested

    return None

def extract_daily_remaining_seconds(payload):
    if isinstance(payload, list):
        for item in payload:
            result = extract_daily_remaining_seconds(item)
            if result is not None:
                return result
        return None

    if not isinstance(payload, dict):
        return None

    daily_keys = [
        "daily_remaining_seconds",
        "daily_remain_seconds",
        "daily_seconds_left",
        "today_remaining_seconds",
        "today_seconds_left",
        "daily_remaining_minutes",
        "today_remaining_minutes",
        "今日剩余秒数",
        "今日剩余时间",
        "今日剩余分钟",
    ]

    for key in daily_keys:
        if key in payload:
            parsed = parse_remaining_value(key, payload.get(key))
            if parsed is not None:
                return parsed

    for key, value in payload.items():
        lower_key = str(key).lower()
        if any(token in lower_key for token in ["daily", "today", "今日"]):
            parsed = parse_remaining_value(key, value)
            if parsed is not None:
                return parsed

    for value in payload.values():
        if isinstance(value, (dict, list)):
            nested = extract_daily_remaining_seconds(value)
            if nested is not None:
                return nested

    return None

def extract_remaining_from_headers(headers):
    if not headers:
        return None
    for key in ["X-Remaining-Seconds", "X-Time-Left", "X-Remain-Seconds"]:
        if key in headers:
            parsed = parse_remaining_value(key, headers.get(key))
            if parsed is not None:
                return parsed
    return None

def update_balance_labels(payload):
    """更新总余额和今日剩余标签"""
    global BALANCE_SECONDS
    if not isinstance(payload, dict):
        return

    # 更新总余额
    balance = payload.get('balance_seconds')
    if balance is not None:
        BALANCE_SECONDS = int(balance)
        if BALANCE_LABEL and BALANCE_LABEL.winfo_exists():
            BALANCE_LABEL.config(text=f"总余额: {format_remaining(balance)}")

    # 更新今日剩余
    daily_remain = payload.get('daily_remaining_seconds')
    if daily_remain is not None:
        if DAILY_LABEL and DAILY_LABEL.winfo_exists():
            DAILY_LABEL.config(text=f"今日剩余: {format_remaining(daily_remain)}")

def update_remaining_from_response(response):
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    daily_remain = extract_daily_remaining_seconds(payload)
    if daily_remain is not None:
        update_remaining_from_payload({"daily_remaining_seconds": daily_remain})
        update_balance_labels(payload)
        return

    remain_in_header = extract_remaining_from_headers(response.headers)
    if remain_in_header is not None:
        update_remaining_from_payload({"remaining_seconds": remain_in_header})
        return

    if payload:
        update_remaining_from_payload(payload)
        update_balance_labels(payload)

def close_timer_window():
    global TIMER_WINDOW, TIMER_LABEL, TIMER_JOB, BALANCE_LABEL, DAILY_LABEL, COOLDOWN_LABEL
    if TIMER_JOB:
        root.after_cancel(TIMER_JOB)
        TIMER_JOB = None
    if TIMER_WINDOW:
        try:
            TIMER_WINDOW.destroy()
        except tk.TclError:
            pass
    TIMER_WINDOW = None
    TIMER_LABEL = None
    BALANCE_LABEL = None
    DAILY_LABEL = None
    COOLDOWN_LABEL = None

def show_timer_window():
    global TIMER_WINDOW, TIMER_LABEL, BALANCE_LABEL, DAILY_LABEL, COOLDOWN_LABEL
    if TIMER_WINDOW and TIMER_WINDOW.winfo_exists():
        return

    TIMER_WINDOW = tk.Toplevel(root)
    TIMER_WINDOW.title("剩余时间")
    TIMER_WINDOW.configure(bg=CLR_CARD)
    TIMER_WINDOW.attributes("-topmost", True)
    TIMER_WINDOW.resizable(False, False)
    TIMER_WINDOW.protocol("WM_DELETE_WINDOW", lambda: None)
    TIMER_WINDOW.bind("<Alt-F4>", lambda e: "break")

    width, height = 280, 280
    screen_w = root.winfo_screenwidth()
    x = screen_w - width - 18
    y = 70
    TIMER_WINDOW.geometry(f"{width}x{height}+{x}+{y}")

    tk.Frame(TIMER_WINDOW, bg=CLR_PRIMARY, height=4).pack(fill="x")
    tk.Label(TIMER_WINDOW,
             text="可用时间",
             font=("Microsoft YaHei UI", 10),
             fg=CLR_SUBTEXT,
             bg=CLR_CARD).pack(pady=(10, 0))
    TIMER_LABEL = tk.Label(TIMER_WINDOW,
                           text=format_remaining(REMAINING_SECONDS),
                           font=("Microsoft YaHei UI", 24, "bold"),
                           fg=CLR_TEXT,
                           bg=CLR_CARD)
    TIMER_LABEL.pack(pady=(0, 8))

    # 总余额显示
    BALANCE_LABEL = tk.Label(TIMER_WINDOW,
                             text="总余额: --",
                             font=("Microsoft YaHei UI", 10),
                             fg=CLR_SUBTEXT,
                             bg=CLR_CARD)
    BALANCE_LABEL.pack(pady=(0, 4))

    # 今日剩余显示
    DAILY_LABEL = tk.Label(TIMER_WINDOW,
                           text="今日剩余: --",
                           font=("Microsoft YaHei UI", 10),
                           fg=CLR_SUBTEXT,
                           bg=CLR_CARD)
    DAILY_LABEL.pack(pady=(0, 4))

    # 冷却时间显示
    COOLDOWN_LABEL = tk.Label(TIMER_WINDOW,
                              text="",
                              font=("Microsoft YaHei UI", 11),
                              fg=CLR_PRIMARY,
                              bg=CLR_CARD)
    COOLDOWN_LABEL.pack(pady=(0, 12))

    # 兑换CDK按钮
    tk.Button(TIMER_WINDOW,
              text="🎟️ 兑换CDK",
              font=("Microsoft YaHei UI", 10, "bold"),
              fg="white",
              bg=CLR_ACCENT,
              activebackground="#4f46e5",
              activeforeground="white",
              relief="flat",
              cursor="hand2",
              command=show_cdk_redeem_window).pack(fill="x", padx=12, pady=(0, 6), ipady=6)

    # 退出登录按钮
    tk.Button(TIMER_WINDOW,
              text="🚪 退出登录",
              font=("Microsoft YaHei UI", 10, "bold"),
              fg="white",
              bg=CLR_PRIMARY,
              activebackground=CLR_PRIMARY_HOVER,
              activeforeground="white",
              relief="flat",
              cursor="hand2",
              command=logout_and_lock).pack(fill="x", padx=12, pady=(0, 12), ipady=7)

def start_timer_countdown():
    global REMAINING_SECONDS, TIMER_JOB, COOLDOWN_REMAINING

    if not CURRENT_KID:
        TIMER_JOB = None
        return

    if TIMER_LABEL and TIMER_LABEL.winfo_exists():
        TIMER_LABEL.config(text=format_remaining(REMAINING_SECONDS))

    # 更新冷却倒计时
    if COOLDOWN_LABEL and COOLDOWN_LABEL.winfo_exists() and COOLDOWN_REMAINING is not None:
        if COOLDOWN_REMAINING > 0:
            COOLDOWN_LABEL.config(text=format_cooldown(COOLDOWN_REMAINING))
            COOLDOWN_REMAINING -= 1
        else:
            COOLDOWN_LABEL.config(text="")

    if REMAINING_SECONDS is not None and REMAINING_SECONDS > 0:
        REMAINING_SECONDS -= 1

    TIMER_JOB = root.after(1000, start_timer_countdown)

def update_remaining_from_payload(payload):
    global REMAINING_SECONDS
    remain = extract_remaining_seconds(payload)
    if remain is None:
        return
    REMAINING_SECONDS = remain
    if TIMER_LABEL and TIMER_LABEL.winfo_exists():
        TIMER_LABEL.config(text=format_remaining(REMAINING_SECONDS))

def show_cdk_redeem_window():
    """显示CDK兑换窗口"""
    # 如果已登录，使用当前账号；否则需要输入账号
    is_logged_in = CURRENT_KID is not None

    cdk_window = tk.Toplevel(root)
    cdk_window.title("兑换CDK")
    cdk_window.configure(bg=CLR_CARD)
    cdk_window.attributes("-topmost", True)
    cdk_window.resizable(False, False)
    cdk_window.protocol("WM_DELETE_WINDOW", lambda: cdk_window.destroy())
    cdk_window.bind("<Alt-F4>", lambda e: "break")

    # 居中
    cdk_window.update_idletasks()
    w, h = 400, 280 if not is_logged_in else 220
    sw = cdk_window.winfo_screenwidth()
    sh = cdk_window.winfo_screenheight()
    cdk_window.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # 顶部色条
    tk.Frame(cdk_window, bg=CLR_PRIMARY, height=4).pack(fill="x")

    # 标题
    tk.Label(cdk_window, text="兑换时长码",
             font=("Microsoft YaHei UI", 14, "bold"),
             fg=CLR_TEXT, bg=CLR_CARD).pack(pady=(20, 10))

    # 存储输入框引用
    kid_entry_var = None
    kid_entry = None

    # 如果未登录，显示账号输入框
    if not is_logged_in:
        tk.Label(cdk_window, text="账号",
                 font=("Microsoft YaHei UI", 10),
                 fg=CLR_SUBTEXT, bg=CLR_CARD).pack(anchor="w", padx=(30, 0))

        kid_frame = tk.Frame(cdk_window, bg=CLR_ENTRY_BD, pady=1, padx=1)
        kid_frame.pack(fill="x", padx=30, pady=(0, 12))

        kid_host = tk.Frame(kid_frame, bg=CLR_ENTRY_BG)
        kid_host.pack(fill="both", expand=True)

        kid_entry = tk.Entry(kid_host,
                             font=("Microsoft YaHei UI", 12),
                             bg=CLR_ENTRY_BG, fg=CLR_TEXT,
                             insertbackground=CLR_TEXT,
                             relief="flat",
                             bd=0)
        kid_entry.pack(fill="x", ipady=8, ipadx=10)
        kid_entry.focus()

    # CDK输入框
    tk.Label(cdk_window, text="兑换码",
             font=("Microsoft YaHei UI", 10),
             fg=CLR_SUBTEXT, bg=CLR_CARD).pack(anchor="w", padx=(30, 0))

    cdk_frame = tk.Frame(cdk_window, bg=CLR_ENTRY_BD, pady=1, padx=1)
    cdk_frame.pack(fill="x", padx=30, pady=(0, 15))

    cdk_host = tk.Frame(cdk_frame, bg=CLR_ENTRY_BG)
    cdk_host.pack(fill="both", expand=True)

    cdk_entry = tk.Entry(cdk_host,
                         font=("Microsoft YaHei UI", 12),
                         bg=CLR_ENTRY_BG, fg=CLR_TEXT,
                         insertbackground=CLR_TEXT,
                         relief="flat",
                         bd=0)
    cdk_entry.pack(fill="x", ipady=8, ipadx=10)
    if kid_entry:
        cdk_entry.bind("<Tab>", lambda e: kid_entry.focus())

    def on_redeem():
        # 获取账号
        if is_logged_in:
            kid_id = CURRENT_KID
        else:
            kid_id = kid_entry.get().strip() if kid_entry else ""
            if not kid_id:
                show_alert("请输入账号", "error")
                return

        cdk_code = cdk_entry.get().strip().upper()
        if not cdk_code:
            show_alert("请输入兑换码", "error")
            return

        def do_request():
            try:
                resp = post_api("/api/cdk/redeem", {"kid_id": kid_id, "cdk_code": cdk_code}, timeout=(5, 10))
                if resp.status_code == 200:
                    result = resp.json()
                    added_seconds = result.get("added_seconds", 0)
                    new_balance = result.get("new_balance_seconds", 0)
                    # 如果已登录，更新余额显示
                    if is_logged_in:
                        root.after(0, lambda nb=new_balance: update_remaining_from_payload({"balance_seconds": nb}))
                    root.after(0, lambda secs=added_seconds: show_alert(f"兑换成功！增加 {format_remaining(secs)}", "success"))
                    root.after(0, lambda win=cdk_window: win.destroy())
                else:
                    error = resp.json().get("error", "未知错误")
                    root.after(0, lambda err=error: show_alert(f"兑换失败：{err}"))
            except Exception as e:
                root.after(0, lambda exc=e: show_alert(f"请求失败：{str(exc)}"))

        threading.Thread(target=do_request, daemon=True).start()

    # 兑换按钮
    tk.Button(cdk_window,
              text="立即兑换",
              font=("Microsoft YaHei UI", 11, "bold"),
              fg="white",
              bg=CLR_PRIMARY,
              activebackground="#c73652",
              activeforeground="white",
              relief="flat",
              cursor="hand2",
              command=on_redeem).pack(fill="x", padx=30, ipady=8)

    # 取消按钮
    tk.Button(cdk_window,
              text="取消",
              font=("Microsoft YaHei UI", 10),
              fg=CLR_SUBTEXT,
              bg=CLR_CARD,
              activebackground=CLR_CARD,
              activeforeground=CLR_TEXT,
              relief="flat",
              cursor="hand2",
              command=cdk_window.destroy).pack(pady=(10, 15))

    # 回车触发兑换
    cdk_entry.bind("<Return>", lambda e: on_redeem())
    if kid_entry:
        kid_entry.bind("<Return>", lambda e: cdk_entry.focus())

def on_login_success(kid_id, response, is_temp=False):
    global CURRENT_KID, TIMER_JOB, COOLDOWN_REMAINING, BALANCE_SECONDS, IS_TEMP_ENTRY, TEMP_REMAINING_SECONDS
    CURRENT_KID = kid_id
    COOLDOWN_REMAINING = 0  # 登录成功，无冷却
    IS_TEMP_ENTRY = is_temp

    data = response.json()
    BALANCE_SECONDS = data.get('balance_seconds', REMAINING_SECONDS)
    is_admin = data.get('is_admin', False)

    # 处理临时进入
    if is_temp and data.get('temp_entry'):
        TEMP_REMAINING_SECONDS = data.get('temp_remaining_seconds', 300)
        show_alert("临时进入5分钟\n请尽快保存存档！", "success")
    else:
        TEMP_REMAINING_SECONDS = None

    # Admin账户特殊处理
    if is_admin or kid_id == 'admin':
        show_alert("管理员登录成功\n无时间限制", "success")
        show_admin_panel()
        root.withdraw()
        start_heartbeat()
        return

    update_remaining_from_response(response)

    show_timer_window()
    if TIMER_JOB is None:
        start_timer_countdown()

    root.withdraw()
    start_heartbeat()

def show_admin_panel():
    """显示现代化管理员面板"""
    global TIMER_WINDOW
    if TIMER_WINDOW and TIMER_WINDOW.winfo_exists():
        TIMER_WINDOW.destroy()

    TIMER_WINDOW = tk.Toplevel(root)
    TIMER_WINDOW.title("管理员面板")
    TIMER_WINDOW.configure(bg=CLR_CARD)
    TIMER_WINDOW.attributes("-topmost", True)
    TIMER_WINDOW.resizable(False, False)
    TIMER_WINDOW.protocol("WM_DELETE_WINDOW", lambda: None)
    TIMER_WINDOW.bind("<Alt-F4>", lambda e: "break")

    width, height = 280, 200
    screen_w = root.winfo_screenwidth()
    x = screen_w - width - 18
    y = 70
    TIMER_WINDOW.geometry(f"{width}x{height}+{x}+{y}")

    # 顶部色条
    tk.Frame(TIMER_WINDOW, bg=CLR_PRIMARY, height=4).pack(fill="x")

    tk.Label(TIMER_WINDOW,
             text="👑 管理员模式",
             font=("Microsoft YaHei UI", 15, "bold"),
             fg=CLR_TEXT,
             bg=CLR_CARD).pack(pady=(24, 8))

    tk.Label(TIMER_WINDOW,
             text="✨ 无时间限制",
             font=("Microsoft YaHei UI", 11),
             fg=CLR_SUCCESS,
             bg=CLR_CARD).pack(pady=(0, 24))

    # 退出登录按钮
    tk.Button(TIMER_WINDOW,
              text="🚪 退出登录",
              font=("Microsoft YaHei UI", 10, "bold"),
              fg="white",
              bg=CLR_PRIMARY,
              activebackground=CLR_PRIMARY_HOVER,
              activeforeground="white",
              relief="flat",
              cursor="hand2",
              command=logout_and_lock).pack(fill="x", padx=24, pady=(0, 20), ipady=7)

# ── 配色 ──────────────────────────────────────────
# 现代化深色主题配色
CLR_BG        = "#0f0f1a"   # 深黑蓝背景
CLR_BG_GRAD    = "#1a1a2e"   # 背景渐变色
CLR_CARD      = "#1e1e2f"   # 卡片背景（稍亮）
CLR_CARD_LIGHT= "#252538"   # 卡片高亮
CLR_ACCENT    = "#6366f1"   # 现代紫色强调色
CLR_PRIMARY   = "#8b5cf6"   # 紫色主色
CLR_PRIMARY_HOVER = "#7c3aed"  # 主色悬停
CLR_TEXT      = "#f1f5f9"   # 主文字（更亮）
CLR_SUBTEXT   = "#94a3b8"   # 次要文字
CLR_ENTRY_BG  = "#13131f"   # 输入框背景
CLR_ENTRY_BD  = "#373747"   # 输入框边框
CLR_ENTRY_FOCUS= "#6366f1"  # 输入框聚焦边框
CLR_SUCCESS   = "#10b981"   # 现代绿色
CLR_WARNING   = "#f59e0b"   # 警告色
CLR_ERROR     = "#ef4444"   # 现代红色
CLR_DIVIDER   = "#2d2d3d"   # 分隔线颜色

def show_alert(msg, kind="error"):
    color = CLR_ERROR if kind == "error" else CLR_SUCCESS
    alert = tk.Toplevel(root)
    alert.title("")
    alert.configure(bg=CLR_CARD)
    alert.attributes("-topmost", True)
    alert.resizable(False, False)

    # 居中
    alert.update_idletasks()
    w, h = 420, 180
    sw = alert.winfo_screenwidth()
    sh = alert.winfo_screenheight()
    alert.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # 顶部色条
    bar = tk.Frame(alert, bg=color, height=5)
    bar.pack(fill="x")

    icon = "✖" if kind == "error" else "✔"
    tk.Label(alert, text=f"{icon}  {msg}",
             font=("Microsoft YaHei UI", 13),
             fg=color, bg=CLR_CARD,
             wraplength=380, justify="center").pack(expand=True)

    btn = tk.Button(alert, text="确定",
                    font=("Microsoft YaHei UI", 11, "bold"),
                    fg="white", bg=color,
                    activebackground=color, activeforeground="white",
                    relief="flat", cursor="hand2", width=8,
                    command=alert.destroy)
    btn.pack(pady=(0, 20))
    alert.grab_set()
    alert.focus_set()

def set_login_ui_state(enabled: bool):
    """登录时禁用/恢复控件，防止重复点击。"""
    state = "normal" if enabled else "disabled"
    entry_id.config(state=state)
    entry_pwd.config(state=state)
    btn_login.config(state=state,
                     text="登 录" if enabled else "验证中…")

def show_password_dialog(title, message, callback):
    """显示密码输入对话框"""
    pwd_dialog = tk.Toplevel(root)
    pwd_dialog.title(title)
    pwd_dialog.configure(bg=CLR_CARD)
    pwd_dialog.attributes("-topmost", True)
    pwd_dialog.resizable(False, False)
    pwd_dialog.protocol("WM_DELETE_WINDOW", lambda: pwd_dialog.destroy())

    # 居中
    pwd_dialog.update_idletasks()
    w, h = 360, 200
    sw = pwd_dialog.winfo_screenwidth()
    sh = pwd_dialog.winfo_screenheight()
    pwd_dialog.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # 顶部色条
    tk.Frame(pwd_dialog, bg=CLR_PRIMARY, height=4).pack(fill="x")

    # 消息
    tk.Label(pwd_dialog, text=message,
             font=("Microsoft YaHei UI", 12),
             fg=CLR_TEXT, bg=CLR_CARD).pack(pady=(20, 15))

    # 输入框容器
    input_frame = tk.Frame(pwd_dialog, bg=CLR_ENTRY_BD, pady=1, padx=1)
    input_frame.pack(fill="x", padx=30, pady=(0, 15))

    input_host = tk.Frame(input_frame, bg=CLR_ENTRY_BG)
    input_host.pack(fill="both", expand=True)

    pwd_entry = tk.Entry(input_host,
                         font=("Microsoft YaHei UI", 12),
                         bg=CLR_ENTRY_BG, fg=CLR_TEXT,
                         insertbackground=CLR_TEXT,
                         show="*",
                         relief="flat",
                         bd=0)
    pwd_entry.pack(fill="x", ipady=8, ipadx=10)
    pwd_entry.focus()

    def on_confirm():
        password = pwd_entry.get().strip()
        pwd_dialog.destroy()
        callback(password)

    def on_cancel():
        pwd_dialog.destroy()

    # 按钮容器
    btn_frame = tk.Frame(pwd_dialog, bg=CLR_CARD)
    btn_frame.pack(pady=(0, 15))

    # 确认按钮
    tk.Button(btn_frame,
              text="确认",
              font=("Microsoft YaHei UI", 10, "bold"),
              fg="white",
              bg=CLR_PRIMARY,
              activebackground="#c73652",
              activeforeground="white",
              relief="flat",
              cursor="hand2",
              width=8,
              command=on_confirm).pack(side="left", padx=5)

    # 取消按钮
    tk.Button(btn_frame,
              text="取消",
              font=("Microsoft YaHei UI", 10),
              fg=CLR_SUBTEXT,
              bg=CLR_CARD,
              activebackground=CLR_CARD,
              activeforeground=CLR_TEXT,
              relief="flat",
              cursor="hand2",
              width=8,
              command=on_cancel).pack(side="left", padx=5)

    # 回车确认
    pwd_entry.bind("<Return>", lambda e: on_confirm())
    pwd_dialog.grab_set()
    pwd_dialog.focus_set()

def on_force_reset():
    """强制重置登录状态"""
    kid_id = entry_id.get().strip()
    if not kid_id:
        show_alert("请输入需要重置的账号！")
        return

    def verify_password(password):
        if not password:
            show_alert("请输入密码！")
            return

        def do_request():
            try:
                resp = post_api("/api/force_reset", {"kid_id": kid_id, "password": password}, timeout=(5, 10))
                if resp.status_code == 200:
                    root.after(0, lambda: show_alert("重置成功！现在可以重新登录了", "success"))
                else:
                    error = resp.json().get("error", "未知错误")
                    root.after(0, lambda err=error: show_alert(f"重置失败：{err}"))
            except Exception as e:
                root.after(0, lambda exc=e: show_alert(f"请求失败：{str(exc)}"))

        threading.Thread(target=do_request, daemon=True).start()

    show_password_dialog("身份验证", f"请输入 {kid_id} 的密码以重置状态", verify_password)

def on_exit_program():
    """退出程序（需要超级密码验证）"""
    # 从配置文件读取超级密码，默认为 admin888
    super_password = "admin888"

    def verify_super_password(password):
        if password == super_password:
            show_alert("验证成功，程序将退出", "success")
            root.after(1000, root.destroy)
        else:
            show_alert("超级密码错误！")

    show_password_dialog("退出程序验证", "请输入超级密码以退出程序", verify_super_password)

def on_temp_entry():
    """临时进入系统（每天一次，5分钟）"""
    kid_id = entry_id.get().strip()
    pwd = entry_pwd.get().strip()
    if not kid_id or not pwd:
        show_alert("请输入账号和密码！")
        return

    set_login_ui_state(False)

    def do_request():
        try:
            resp = post_api("/api/temp_entry", {"kid_id": kid_id, "password": pwd})
            if resp.status_code == 200:
                root.after(0, lambda kid=kid_id, r=resp: on_login_success(kid, r, is_temp=True))
            else:
                error_data = resp.json()
                error = error_data.get("error", "未知错误")
                root.after(0, lambda err=error: show_alert(f"临时进入失败：{err}"))
                root.after(0, lambda: set_login_ui_state(True))
        except Exception as e:
            root.after(0, lambda exc=e: show_alert(f"请求失败：{str(exc)}"))
            root.after(0, lambda: set_login_ui_state(True))

    threading.Thread(target=do_request, daemon=True).start()

def shutdown_pc():
    """关机"""
    confirm = tk.Toplevel(root)
    confirm.title("确认")
    confirm.configure(bg=CLR_CARD)
    confirm.attributes("-topmost", True)
    confirm.resizable(False, False)

    # 居中
    confirm.update_idletasks()
    w, h = 320, 160
    sw = confirm.winfo_screenwidth()
    sh = confirm.winfo_screenheight()
    confirm.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # 顶部色条
    tk.Frame(confirm, bg=CLR_PRIMARY, height=4).pack(fill="x")

    tk.Label(confirm, text="确定要关机吗？",
             font=("Microsoft YaHei UI", 13),
             fg=CLR_TEXT, bg=CLR_CARD).pack(expand=True)

    btn_frame = tk.Frame(confirm, bg=CLR_CARD)
    btn_frame.pack(pady=(0, 20))

    tk.Button(btn_frame, text="确定",
              font=("Microsoft YaHei UI", 10, "bold"),
              fg="white", bg=CLR_PRIMARY,
              activebackground="#c73652", activeforeground="white",
              relief="flat", cursor="hand2", width=8,
              command=lambda: [confirm.destroy(), subprocess.run(["shutdown", "/s", "/t", "0"], shell=True)]).pack(side="left", padx=5)

    tk.Button(btn_frame, text="取消",
              font=("Microsoft YaHei UI", 10),
              fg=CLR_SUBTEXT, bg=CLR_CARD,
              activebackground=CLR_CARD, activeforeground=CLR_TEXT,
              relief="flat", cursor="hand2", width=8,
              command=confirm.destroy).pack(side="left", padx=5)
    confirm.grab_set()
    confirm.focus_set()

def restart_pc():
    """重启"""
    confirm = tk.Toplevel(root)
    confirm.title("确认")
    confirm.configure(bg=CLR_CARD)
    confirm.attributes("-topmost", True)
    confirm.resizable(False, False)

    # 居中
    confirm.update_idletasks()
    w, h = 320, 160
    sw = confirm.winfo_screenwidth()
    sh = confirm.winfo_screenheight()
    confirm.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # 顶部色条
    tk.Frame(confirm, bg=CLR_PRIMARY, height=4).pack(fill="x")

    tk.Label(confirm, text="确定要重启吗？",
             font=("Microsoft YaHei UI", 13),
             fg=CLR_TEXT, bg=CLR_CARD).pack(expand=True)

    btn_frame = tk.Frame(confirm, bg=CLR_CARD)
    btn_frame.pack(pady=(0, 20))

    tk.Button(btn_frame, text="确定",
              font=("Microsoft YaHei UI", 10, "bold"),
              fg="white", bg=CLR_PRIMARY,
              activebackground="#c73652", activeforeground="white",
              relief="flat", cursor="hand2", width=8,
              command=lambda: [confirm.destroy(), subprocess.run(["shutdown", "/r", "/t", "0"], shell=True)]).pack(side="left", padx=5)

    tk.Button(btn_frame, text="取消",
              font=("Microsoft YaHei UI", 10),
              fg=CLR_SUBTEXT, bg=CLR_CARD,
              activebackground=CLR_CARD, activeforeground=CLR_TEXT,
              relief="flat", cursor="hand2", width=8,
              command=confirm.destroy).pack(side="left", padx=5)
    confirm.grab_set()
    confirm.focus_set()

def on_login():
    kid_id = entry_id.get().strip()
    pwd = entry_pwd.get().strip()
    if not kid_id or not pwd:
        show_alert("请输入账号和密码！")
        return

    set_login_ui_state(False)

    def do_request():
        try:
            resp = post_api("/api/login", {"kid_id": kid_id, "password": pwd})
            if resp.status_code == 200:
                root.after(0, lambda kid=kid_id, r=resp: on_login_success(kid, r))
            else:
                error_data = resp.json()
                error = error_data.get("error", "未知错误")

                if "cooldown" in error.lower():
                    cooldown_remaining = error_data.get("cooldown_remaining_seconds", 0)
                    root.after(0, lambda cr=cooldown_remaining: show_alert(f"账号冷却中\n请等待 {format_remaining(cr)} 后重试"))
                else:
                    root.after(0, lambda err=error: show_alert(f"登录失败：{err}"))

                root.after(0, lambda: set_login_ui_state(True))
        except requests.exceptions.ConnectTimeout:
            root.after(0, lambda: show_alert(f"连接超时：{SERVER_URL}\n请检查服务端是否在线、IP/端口是否正确"))
            root.after(0, lambda: set_login_ui_state(True))
        except requests.exceptions.ReadTimeout:
            root.after(0, lambda: show_alert(f"服务器响应超时：{SERVER_URL}\n服务端处理过慢或网络不稳定"))
            root.after(0, lambda: set_login_ui_state(True))
        except requests.exceptions.ConnectionError as e:
            root.after(0, lambda exc=e: show_alert(f"无法连接到服务端：{SERVER_URL}\n{str(exc)}"))
            root.after(0, lambda: set_login_ui_state(True))
        except Exception as e:
            root.after(0, lambda exc=e: show_alert(f"请求失败：{str(exc)}"))
            root.after(0, lambda: set_login_ui_state(True))

    threading.Thread(target=do_request, daemon=True).start()

def logout_and_lock():
    global CURRENT_KID, REMAINING_SECONDS, COOLDOWN_REMAINING, IS_TEMP_ENTRY, TEMP_REMAINING_SECONDS
    if CURRENT_KID:
        try:
            post_api("/api/logout", {"kid_id": CURRENT_KID}, timeout=(2, 4))
        except:
            pass
        CURRENT_KID = None
    REMAINING_SECONDS = None
    COOLDOWN_REMAINING = None
    IS_TEMP_ENTRY = None
    TEMP_REMAINING_SECONDS = None
    close_timer_window()
    entry_id.delete(0, "end")
    entry_pwd.delete(0, "end")
    set_login_ui_state(True)
    root.deiconify()
    entry_id.focus()

def start_heartbeat():
    def loop():
        while CURRENT_KID:
            try:
                resp = post_api("/api/heartbeat", {"kid_id": CURRENT_KID}, timeout=(2, 8))
                if resp.status_code != 200:
                    root.after(0, logout_and_lock)
                    break
                action = None
                try:
                    action = resp.json().get("action")
                except ValueError:
                    action = None
                if action in ("time_up", "logout", "session_limit", "temp_entry_expired"):
                    if action == "session_limit":
                        cooldown_duration = resp.json().get("cooldown_duration_seconds", 0)
                        root.after(0, lambda cd=cooldown_duration: show_alert(f"单次使用时长已达上限\n进入冷却期 {format_remaining(cd)}"))
                    elif action == "temp_entry_expired":
                        root.after(0, lambda: show_alert("临时进入时间已用完！"))
                    root.after(0, logout_and_lock)
                    break

                # 更新临时进入剩余时间
                data = resp.json()
                if data.get('temp_entry'):
                    global TEMP_REMAINING_SECONDS
                    TEMP_REMAINING_SECONDS = data.get('temp_remaining_seconds', 0)
                    if TEMP_REMAINING_SECONDS > 0 and TIMER_LABEL and TIMER_LABEL.winfo_exists():
                        TIMER_LABEL.config(text=f"临时: {format_remaining(TEMP_REMAINING_SECONDS)}")

                root.after(0, lambda r=resp: update_remaining_from_response(r))
            except:
                pass
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()

def on_entry_focus_in(entry, placeholder, e):
    """输入框聚焦事件"""
    if entry.get() == placeholder:
        entry.delete(0, "end")
        if getattr(entry, "_is_password", False):
            entry.config(show="*")
        entry.config(fg=CLR_TEXT)
    # 聚焦时改变边框颜色
    entry.master.master.config(bg=CLR_ENTRY_FOCUS)

def on_entry_focus_out(entry, placeholder, e):
    """输入框失焦事件"""
    if not entry.get():
        entry.insert(0, placeholder)
        entry.config(show="", fg=CLR_SUBTEXT)
    # 失焦时恢复边框颜色
    entry.master.master.config(bg=CLR_ENTRY_BD)

def make_entry(parent, placeholder, is_password=False):
    """创建现代化输入框，带placeholder和聚焦效果"""
    # 外层边框（用于聚焦效果）
    wrap = tk.Frame(parent, bg=CLR_ENTRY_BD, pady=1, padx=1)
    field_host = tk.Frame(wrap, bg=CLR_ENTRY_BG)
    field_host.pack(fill="both", expand=True)

    e = tk.Entry(field_host,
                 font=("Microsoft YaHei UI", 13),
                 bg=CLR_ENTRY_BG,
                 fg=CLR_SUBTEXT,
                 insertbackground=CLR_PRIMARY,
                 relief="flat",
                 bd=0,
                 takefocus=1,
                 highlightthickness=0)
    e._is_password = is_password
    e.insert(0, placeholder)

    e.pack(fill="x", ipady=8, ipadx=12)
    wrap.pack(fill="x", pady=6)

    e.bind("<FocusIn>",  lambda ev: on_entry_focus_in(e, placeholder, ev))
    e.bind("<FocusOut>", lambda ev: on_entry_focus_out(e, placeholder, ev))
    return e

def btn_hover(btn, enter, original_bg):
    """现代化按钮悬停效果"""
    if enter:
        btn.config(bg=CLR_PRIMARY_HOVER)
    else:
        btn.config(bg=original_bg)

# ══════════════════════════════════════════════════
# UI 构建
# ══════════════════════════════════════════════════
root = tk.Tk()
root.title("家庭时间管理")
root.attributes("-fullscreen", True)
root.configure(bg=CLR_BG)
root.wm_attributes("-topmost", True)
root.protocol("WM_DELETE_WINDOW", lambda: None)

# ── 渐变背景层（全屏）──
bg_frame = tk.Frame(root, bg=CLR_BG)
bg_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

# 添加底部渐变装饰
bg_decor = tk.Frame(root, bg=CLR_BG_GRAD)
bg_decor.place(relx=0, rely=0.7, relwidth=1, relheight=0.3)

# ── 顶部品牌条 ──
header = tk.Frame(root, bg=CLR_CARD, height=64)
header.place(x=0, y=0, relwidth=1)
# 底部分隔线
tk.Frame(header, bg=CLR_DIVIDER, height=1).place(x=0, rely=1, relwidth=1)

# 左侧图标和标题
tk.Label(header, text="⏰",
         font=("Segoe UI Emoji", 24),
         fg=CLR_PRIMARY, bg=CLR_CARD).place(x=30, rely=0.5, anchor="w")

tk.Label(header, text="家庭时间管理",
         font=("Microsoft YaHei UI", 18, "bold"),
         fg=CLR_TEXT, bg=CLR_CARD).place(x=65, rely=0.5, anchor="w")

tk.Label(header, text="Smart Family Time Control",
         font=("Microsoft YaHei UI", 9),
         fg=CLR_SUBTEXT, bg=CLR_CARD).place(x=66, rely=0.8, anchor="w")

# 右上角退出程序按钮
exit_btn = tk.Button(header, text="✖ 退出",
                    font=("Microsoft YaHei UI", 9, "bold"),
                    fg=CLR_SUBTEXT,
                    bg=CLR_CARD_LIGHT,
                    activebackground=CLR_CARD,
                    activeforeground=CLR_ERROR,
                    relief="flat",
                    cursor="hand2",
                    command=on_exit_program)
exit_btn.place(x=root.winfo_screenwidth() - 120, rely=0.5, anchor="center", width=100, height=36)

# ── 居中登录卡片 ──
CARD_W, CARD_H = 440, 540
card = tk.Frame(root, bg=CLR_CARD,
                highlightbackground=CLR_DIVIDER, highlightthickness=2)
card.place(relx=0.5, rely=0.5, anchor="center", width=CARD_W, height=CARD_H)

inner = tk.Frame(card, bg=CLR_CARD, padx=40, pady=24)
inner.pack(fill="both", expand=True)

# 图标 + 标题
tk.Label(inner, text="🔐",
         font=("Segoe UI Emoji", 40),
         bg=CLR_CARD, fg=CLR_PRIMARY).pack(pady=(12, 8))

tk.Label(inner, text="欢迎回来",
         font=("Microsoft YaHei UI", 16, "bold"),
         fg=CLR_TEXT, bg=CLR_CARD).pack()
tk.Label(inner, text="请使用您的账号登录系统",
         font=("Microsoft YaHei UI", 9),
         fg=CLR_SUBTEXT, bg=CLR_CARD).pack(pady=(4, 16))

# 输入框
entry_id  = make_entry(inner, "👤 账号 / Kid ID")
entry_pwd = make_entry(inner, "🔒 密码", is_password=True)
# 回车触发登录
entry_pwd.bind("<Return>", lambda e: on_login())
entry_id.bind("<Return>",  lambda e: entry_pwd.focus())

# 登录按钮
btn_login = tk.Button(inner, text="登 录",
                      font=("Microsoft YaHei UI", 11, "bold"),
                      fg="white", bg=CLR_PRIMARY,
                      activebackground=CLR_PRIMARY_HOVER,
                      activeforeground="white",
                      relief="flat", cursor="hand2",
                      command=on_login)
btn_login.pack(fill="x", ipady=10, pady=(16, 8))
btn_login.bind("<Enter>", lambda e: btn_hover(btn_login, True, CLR_PRIMARY))
btn_login.bind("<Leave>", lambda e: btn_hover(btn_login, False, CLR_PRIMARY))

# 功能按钮容器
func_btn_frame = tk.Frame(inner, bg=CLR_CARD)
func_btn_frame.pack(fill="x", pady=(0, 10))

# 临时进入按钮
tk.Button(func_btn_frame, text="⚡ 临时进入",
          font=("Microsoft YaHei UI", 9, "bold"),
          fg=CLR_SUCCESS,
          bg=CLR_CARD_LIGHT,
          activebackground=CLR_CARD,
          activeforeground="#059669",
          relief="flat",
          cursor="hand2",
          command=on_temp_entry).pack(side="left", expand=True, ipady=6, padx=2)

# 兑换CDK按钮
tk.Button(func_btn_frame, text="🎟️ 兑换CDK",
          font=("Microsoft YaHei UI", 9, "bold"),
          fg=CLR_ACCENT,
          bg=CLR_CARD_LIGHT,
          activebackground=CLR_CARD,
          activeforeground="#4f46e5",
          relief="flat",
          cursor="hand2",
          command=show_cdk_redeem_window).pack(side="left", expand=True, ipady=6, padx=2)

# 分隔线
tk.Frame(inner, bg=CLR_DIVIDER, height=1).pack(fill="x", pady=(10, 10))

# 系统操作按钮容器
sys_btn_frame = tk.Frame(inner, bg=CLR_CARD)
sys_btn_frame.pack(fill="x", pady=(0, 8))

# 关机按钮
tk.Button(sys_btn_frame, text="🔴 关机",
          font=("Microsoft YaHei UI", 9),
          fg=CLR_ERROR,
          bg=CLR_CARD_LIGHT,
          activebackground=CLR_CARD,
          activeforeground="#dc2626",
          relief="flat",
          cursor="hand2",
          command=shutdown_pc).pack(side="left", expand=True, ipady=6, padx=2)

# 重启按钮
tk.Button(sys_btn_frame, text="🟠 重启",
          font=("Microsoft YaHei UI", 9),
          fg=CLR_WARNING,
          bg=CLR_CARD_LIGHT,
          activebackground=CLR_CARD,
          activeforeground="#d97706",
          relief="flat",
          cursor="hand2",
          command=restart_pc).pack(side="left", expand=True, ipady=6, padx=2)

# 重置状态按钮
tk.Button(sys_btn_frame, text="⚙️ 重置",
          font=("Microsoft YaHei UI", 9),
          fg=CLR_SUBTEXT,
          bg=CLR_CARD_LIGHT,
          activebackground=CLR_CARD,
          activeforeground=CLR_TEXT,
          relief="flat",
          cursor="hand2",
          command=on_force_reset).pack(side="left", expand=True, ipady=6, padx=2)

# ── 底部版权 ──
tk.Label(root, text="💬 需要帮助？请联系家长",
         font=("Microsoft YaHei UI", 9),
         fg=CLR_SUBTEXT, bg=CLR_BG).place(relx=0.5, rely=0.97, anchor="center")

tk.Label(root, text="© 2024 Family Time Control",
         font=("Microsoft YaHei UI", 8),
         fg="#64748b", bg=CLR_BG).place(relx=0.5, rely=0.94, anchor="center")

# 启动 AutoHotkey 屏蔽脚本
# ahk_exe = os.path.join(os.path.dirname(__file__), "block_keys.exe")
# if os.path.exists(ahk_exe):
#     subprocess.Popen([ahk_exe], cwd=os.path.dirname(__file__))

entry_id.focus()
root.mainloop()