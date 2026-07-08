# -*- coding: utf-8 -*-
"""
询价单自动化 — 图形启动器（tkinter）

用法:
  python launcher.py
  或双击 启动询价单自动化.bat
"""

from __future__ import annotations

import json
import locale
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
INQUIRY_RESULTS = "inquiry_results.json"
INQUIRY_LAST = "inquiry_last.json"


def _child_env() -> dict[str, str]:
    """子进程环境：尽量 UTF-8；启动器仍会对 stdout 做 GBK/UTF-8 自动识别。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _decode_subprocess_bytes(raw: bytes) -> str:
    """
    Windows 下 main.py 的 logging 往往按 GBK 写入管道；
    若用 UTF-8 硬解会出现乱码。对每行尝试常见编码并选最合理结果。
    """
    if not raw:
        return ""
    if sys.platform == "win32":
        candidates = ("gbk", "utf-8", "cp936")
    else:
        candidates = ("utf-8", "gbk")

    def _score(text: str) -> int:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        bad = text.count("\ufffd")
        return cjk * 4 - bad * 30

    best_text = ""
    best_score = -10**9
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        sc = _score(text)
        if sc > best_score:
            best_score = sc
            best_text = text
    if best_text:
        return best_text
    return raw.decode(locale.getpreferredencoding(False) or "gbk", errors="replace")


def _load_inquiries(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("inquiries") or [])
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _count_with_quotation(inquiries: list) -> int:
    return sum(1 for inv in inquiries if (inv.get("quotation_no") or "").strip())


def build_status_text() -> str:
    pending_path = os.path.join(APP_ROOT, INQUIRY_RESULTS)
    last_path = os.path.join(APP_ROOT, INQUIRY_LAST)
    pending = _load_inquiries(pending_path)
    last_items = _load_inquiries(last_path)
    pending_n = len(pending)
    last_n = len(last_items)
    pending_q = _count_with_quotation(pending)
    last_q = _count_with_quotation(last_items)

    parts = [f"待办 inquiry_results: {pending_n} 条"]
    if pending_q:
        parts.append(f"（其中 {pending_q} 条已有报价单号）")
    parts.append(f" | 备份 inquiry_last: {last_n} 条")
    if last_q:
        parts.append(f"（{last_q} 条有报价单号）")

    hints = []
    if pending_n:
        hints.append("可点「阶段二」")
    elif last_n and last_q:
        hints.append("阶段二可能已完成，可点「阶段三」")
    elif last_n and not last_q:
        hints.append("last 无报价单号，请先完成阶段二")
    else:
        hints.append("可先点「阶段一」")
    parts.append(" — " + "，".join(hints))
    return "".join(parts)


# 按钮旁说明（给操作员看，不发起命令）
PHASE_HELP = [
    (
        "create",
        "阶段一",
        "新建询价单+发供应商邮件",
    ),
        (
        "resume",
        "阶段二（审批完成后）",
        "已完结→购物车→报价单；若 PO 与 OMS 供应商不一致，"
        "会自动扩展供应商、删「已发送」、OMS 发邮件（不重建询价单，无需再点阶段一）",
    ),
    (
        "finalize",
        "阶段三",
        "导出PDF、EXCEL文件，填表并导入",
    ),
]


class LauncherApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("询价单自动化")
        self.minsize(780, 580)
        self.geometry("920x680")

        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._output_queue: queue.Queue[str | None] = queue.Queue()
        self._poll_id: str | None = None

        self._build_ui()
        self._refresh_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _add_phase_row(self, parent: ttk.Frame, phase: str, btn_text: str, help_text: str) -> ttk.Button:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=6)
        btn = ttk.Button(
            row, text=btn_text, width=20, command=lambda p=phase: self._start_phase(p)
        )
        btn.pack(side=tk.LEFT, padx=(0, 12), anchor=tk.N)
        ttk.Label(
            row,
            text=help_text,
            wraplength=640,
            justify=tk.LEFT,
            foreground="#444444",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, anchor=tk.NW)
        return btn

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(
            top,
            text="按顺序：阶段一 → 人工等审批 → 阶段二 → 阶段三。"
            "首次运行会提示录入账号；换账号请点下方「重新配置账号」。",
            wraplength=860,
        ).pack(anchor=tk.W, pady=(0, 8))

        phases = ttk.LabelFrame(top, text="选择要运行的阶段", padding=(8, 4))
        phases.pack(fill=tk.X)

        buttons = {}
        for phase, btn_text, help_text in PHASE_HELP:
            buttons[phase] = self._add_phase_row(phases, phase, btn_text, help_text)
        self.btn_phase1 = buttons["create"]
        self.btn_phase2 = buttons["resume"]
        self.btn_phase3 = buttons["finalize"]

        opts = ttk.Frame(top)
        opts.pack(fill=tk.X, pady=(4, 0))
        self.var_close_browser = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts,
            text="跑完后关闭 Edge（--close-browser）",
            variable=self.var_close_browser,
        ).pack(side=tk.LEFT, padx=8)

        self.lbl_run_state = ttk.Label(
            top,
            text="当前：空闲（三个阶段按钮均可点击）",
            foreground="#0066aa",
        )
        self.lbl_run_state.pack(anchor=tk.W, padx=8, pady=(4, 0))

        self.lbl_status = ttk.Label(top, text="", wraplength=860)
        self.lbl_status.pack(fill=tk.X, pady=(8, 4))
        btn_row = ttk.Frame(top)
        btn_row.pack(anchor=tk.W, padx=8)
        ttk.Button(btn_row, text="刷新状态", command=self._refresh_status).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_row, text="重新配置账号", command=self._reconfigure_accounts).pack(
            side=tk.LEFT
        )

        ctrl = ttk.LabelFrame(self, text="报错暂停时（日志含【需人工处理】）", padding=8)
        ctrl.pack(fill=tk.X, padx=10, pady=(0, 4))
        ctrl_row = ttk.Frame(ctrl)
        ctrl_row.pack(fill=tk.X)
        self.btn_continue = ttk.Button(
            ctrl_row,
            text="▶ 继续",
            width=10,
            command=self._send_continue,
            state=tk.DISABLED,
        )
        self.btn_continue.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_skip = ttk.Button(
            ctrl_row,
            text="跳过本项目",
            width=11,
            command=self._send_skip_group,
            state=tk.DISABLED,
        )
        self.btn_skip.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_stop = ttk.Button(
            ctrl_row, text="停止", command=self._stop_process, state=tk.DISABLED
        )
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        ttk.Label(
            ctrl,
            text="先在浏览器做完当前操作，再点按钮。"
            "「继续」=接下去自动跑；"
            "「跳过」=这组不要了；「停止」=结束程序。",
            wraplength=860,
            foreground="#444444",
        ).pack(anchor=tk.W, pady=(6, 0))

        log_frame = ttk.LabelFrame(self, text="运行日志", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = scrolledtext.ScrolledText(
            log_frame, height=22, wrap=tk.WORD, font=("Consolas", 10)
        )
        self.log.pack(fill=tk.BOTH, expand=True)

    def _refresh_status(self) -> None:
        self._sync_proc_state()
        try:
            self.lbl_status.config(text=build_status_text())
        except Exception as e:
            self.lbl_status.config(text=f"状态读取失败: {e}")

    def _reconfigure_accounts(self) -> None:
        """弹出控制台，重新录入 OMS/备件网账号并写入 user_config.py。"""
        if self._proc is not None and self._proc.poll() is None:
            messagebox.showwarning(
                "正在运行",
                "请先停止当前自动化任务，再重新配置账号。",
            )
            return
        if sys.platform == "win32":
            cmd = ["cmd", "/k", sys.executable, "-u", "main.py", "--setup-config"]
        else:
            cmd = [sys.executable, "-u", "main.py", "--setup-config"]
        self._append_log("\n>>> 打开控制台重新配置账号…\n")
        self._append_log(f">>> 命令: {' '.join(cmd)}\n")
        kwargs = {"cwd": APP_ROOT}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        try:
            subprocess.Popen(cmd, **kwargs)
            messagebox.showinfo(
                "重新配置账号",
                "已在新的黑色窗口中启动配置。\n"
                "请在该窗口按提示输入 OMS/备件网账号：\n"
                "  · 用户名直接回车可沿用旧的\n"
                "  · 密码需重新输入\n"
                "保存后可关闭黑窗口；之后阶段一/二/三均使用新账号。\n"
                "若自动化正在跑，须先点「停止」再改账号。",
            )
        except OSError as e:
            messagebox.showerror("启动失败", str(e))

    def _sync_proc_state(self) -> None:
        """进程已退出但界面仍显示「运行中」时，自动恢复按钮。"""
        if self._proc is not None and self._proc.poll() is not None:
            self._on_process_finished()
            return
        running = self._proc is not None and self._proc.poll() is None
        if running:
            self.lbl_run_state.config(
                text="当前：自动化正在运行 — 阶段按钮暂时变灰，请等结束或点「停止」",
                foreground="#b45309",
            )
        else:
            self.lbl_run_state.config(
                text="当前：空闲（三个阶段按钮均可点击）",
                foreground="#0066aa",
            )

    def _append_log(self, text: str) -> None:
        self.log.insert(tk.END, text)
        self.log.see(tk.END)

    def _set_running(self, running: bool) -> None:
        state_run = tk.DISABLED if running else tk.NORMAL
        for btn in (
            self.btn_phase1,
            self.btn_phase2,
            self.btn_phase3,
        ):
            btn.config(state=state_run)
        for btn in (self.btn_continue, self.btn_skip):
            btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL if running else tk.DISABLED)
        self._sync_proc_state()

    def _build_command(self, phase: str) -> list[str]:
        cmd = [sys.executable, "-u", "main.py"]
        if phase == "resume":
            cmd.append("--resume")
        elif phase == "finalize":
            cmd.append("--finalize")
            cmd.append("--import-submit")
        if self.var_close_browser.get():
            cmd.append("--close-browser")
        return cmd

    def _phase_label(self, phase: str) -> str:
        for p, label, _ in PHASE_HELP:
            if p == phase:
                return label
        return phase

    def _start_phase(self, phase: str) -> None:
        if self._proc is not None and self._proc.poll() is None:
            messagebox.showwarning("正在运行", "请先停止当前任务，或等待其结束。")
            return

        cmd = self._build_command(phase)
        label = self._phase_label(phase)
        self._append_log("\n" + "=" * 60 + "\n")
        self._append_log(f"启动: {label}\n")
        self._append_log(f"命令: {' '.join(cmd)}\n")
        self._append_log(f"目录: {APP_ROOT}\n")
        self._append_log("=" * 60 + "\n")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=APP_ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=_child_env(),
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as e:
            messagebox.showerror("启动失败", str(e))
            self._proc = None
            return

        self._set_running(True)
        self._output_queue = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._read_stdout, daemon=True
        )
        self._reader_thread.start()
        self._poll_output()

    def _read_stdout(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            self._output_queue.put(None)
            return
        try:
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    break
                self._output_queue.put(_decode_subprocess_bytes(raw))
        except Exception:
            pass
        finally:
            self._output_queue.put(None)

    def _poll_output(self) -> None:
        try:
            while True:
                item = self._output_queue.get_nowait()
                if item is None:
                    self._on_process_finished()
                    return
                self._append_log(item)
        except queue.Empty:
            pass
        self._poll_id = self.after(120, self._poll_output)

    def _on_process_finished(self) -> None:
        code = None
        if self._proc is not None:
            code = self._proc.poll()
        self._append_log(f"\n>>> 进程已结束，退出码: {code}\n")
        self._proc = None
        self._set_running(False)
        self._refresh_status()
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None

    def _send_stdin_line(self, payload: bytes, log_label: str) -> None:
        if not self._proc or self._proc.poll() is not None:
            messagebox.showinfo("未运行", "当前没有正在运行的任务。")
            return
        if self._proc.stdin is None:
            messagebox.showwarning("无法继续", "子进程未打开 stdin。")
            return
        try:
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
            self._append_log(f"\n>>> [界面] 已发送「{log_label}」\n")
        except OSError as e:
            messagebox.showerror("发送失败", str(e))

    def _send_continue(self) -> None:
        self._send_stdin_line(b"\n", "继续")

    def _send_skip_group(self) -> None:
        self._send_stdin_line(b"s\n", "跳过本项目")

    def _stop_process(self) -> None:
        if not self._proc or self._proc.poll() is not None:
            return
        if not messagebox.askyesno("确认停止", "确定要终止当前自动化进程吗？\n浏览器可能仍保持打开。"):
            return
        try:
            self._proc.terminate()
            self._append_log("\n>>> [界面] 已请求终止进程\n")
            self.after(1500, self._sync_proc_state)
        except OSError as e:
            messagebox.showerror("停止失败", str(e))

    def _on_close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            if not messagebox.askyesno(
                "退出",
                "自动化仍在运行。确定退出启动器吗？\n（不会自动关闭 Edge）",
            ):
                return
            try:
                self._proc.terminate()
            except OSError:
                pass
        self.destroy()


def main() -> None:
    os.chdir(APP_ROOT)
    from utils.user_config_store import ensure_user_config

    ensure_user_config()
    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
