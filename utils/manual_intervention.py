"""
人工介入后继续：任意步骤出错时可暂停，由操作员在浏览器中手工处理后再续跑。
"""
from __future__ import annotations

import traceback
from typing import Any, Optional


# 操作员在暂停时选择的动作（与启动器 stdin 快捷键一致）
CHOICE_STEP_DONE = "step_done"       # Enter / c — 本步骤已在页面手工完成
CHOICE_RETRY = "retry"               # r — 重试本步骤
CHOICE_SKIP_GROUP = "skip_group"     # s — 跳过当前项目/询价单组
CHOICE_ABORT = "abort"               # q — 终止整场运行
CHOICE_INQUIRY_FULL_DONE = "inquiry_full_done"  # d — 阶段一：整单含提交已完成


class ManualInterventionNeeded(Exception):
    """业务逻辑主动请求人工介入（非未捕获异常）。"""

    def __init__(self, message: str, *, step_hint: str = "", context: Any = None):
        super().__init__(message)
        self.step_hint = step_hint or ""
        self.context = context


class SkipCurrentGroup(Exception):
    """操作员选择跳过当前项目组。"""


class AbortRun(Exception):
    """操作员选择终止整场自动化。"""


def format_exception_brief(exc: BaseException, max_len: int = 800) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def format_exception_detail(exc: BaseException, max_tb_lines: int = 8) -> str:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tail = "".join(lines[-max_tb_lines:]).strip()
    return tail or format_exception_brief(exc)


def parse_operator_choice(raw: str, *, allow_inquiry_full: bool = False) -> str:
    """解析终端/启动器输入的操作码。"""
    c = (raw or "").strip().lower()
    if c in ("", "c", "continue", "继续", "n"):
        return CHOICE_STEP_DONE
    if c in ("r", "retry", "重试"):
        return CHOICE_RETRY
    if c in ("s", "skip", "跳过"):
        return CHOICE_SKIP_GROUP
    if c in ("q", "quit", "abort", "退出", "终止"):
        return CHOICE_ABORT
    # 兼容旧版「整单已提交」，与「继续」相同
    if c in ("d", "done", "完成", "提交"):
        return CHOICE_STEP_DONE
    return CHOICE_STEP_DONE
