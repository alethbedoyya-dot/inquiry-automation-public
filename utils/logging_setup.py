"""Logging setup and end-of-run summary helpers."""

import html
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


class ErrorCollector(logging.Handler):
    """收集所有 ERROR / WARNING 级别日志，运行结束后统一打印"""

    def __init__(self):
        super().__init__()
        self.errors = []
        self.warnings = []
        self.setLevel(logging.WARNING)
        self.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))

    def emit(self, record):
        msg = self.format(record)
        if record.levelno >= logging.ERROR:
            self.errors.append(msg)
        elif record.levelno >= logging.WARNING:
            self.warnings.append(msg)

    def print_summary(self):
        """打印醒目的错误/警告摘要（错误始终展开；警告默认折叠，可点击 HTML 或按键展开）"""
        def safe_print(text):
            """安全打印，处理Windows GBK编码无法输出的Unicode字符"""
            try:
                print(text)
            except UnicodeEncodeError:
                print(text.encode(sys.stdout.encoding or 'gbk', errors='replace').decode(sys.stdout.encoding or 'gbk'))

        total_errors = len(self.errors)
        total_warnings = len(self.warnings)

        if total_errors == 0 and total_warnings == 0:
            safe_print("\n" + "=" * 60)
            safe_print("  OK 运行完毕，无错误无警告")
            safe_print("=" * 60)
            return

        summary_html = self._write_summary_html()

        safe_print("\n" + "=" * 60)
        safe_print(f"  !! 运行摘要: {total_errors} 个错误, {total_warnings} 个警告")
        safe_print("=" * 60)

        if self.errors:
            safe_print(f"\n  ERROR 错误 ({total_errors}):")
            safe_print("  " + "-" * 56)
            for i, msg in enumerate(self.errors, 1):
                safe_print(f"  [{i}] {self._shorten(msg)}")

        if self.warnings:
            safe_print(f"\n  WARN 警告 ({total_warnings}) — 已折叠")
            safe_print("  " + "-" * 56)
            safe_print(f"  共 {total_warnings} 条，默认不显示明细")
            if summary_html:
                uri = Path(summary_html).resolve().as_uri()
                safe_print(f"  点击打开摘要页，再点击「警告」标题可展开全部:")
                safe_print(f"  {uri}")
            if sys.stdin.isatty():
                try:
                    ans = input("  >> 在终端展开请输入 w 后回车，直接回车跳过: ").strip().lower()
                except EOFError:
                    ans = ""
                if ans == "w":
                    self._print_warnings_expanded(safe_print)

        safe_print("\n" + "=" * 60)

    def _print_warnings_expanded(self, safe_print):
        """在终端打印全部警告明细"""
        safe_print(f"\n  WARN 警告明细 ({len(self.warnings)}):")
        safe_print("  " + "-" * 56)
        for i, msg in enumerate(self.warnings, 1):
            safe_print(f"  [{i}] {self._shorten(msg)}")

    def _write_summary_html(self):
        """生成可点击展开警告的 HTML 摘要（warnings 使用 <details> 默认折叠）"""
        if not self.errors and not self.warnings:
            return None

        log_dir = os.path.join(PROJECT_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"run_summary_{time.strftime('%Y%m%d_%H%M%S')}.html")

        def li_items(messages):
            if not messages:
                return "<li>（无）</li>"
            return "".join(
                f"<li><pre>{html.escape(self._shorten(m))}</pre></li>"
                for m in messages
            )

        err_count = len(self.errors)
        warn_count = len(self.warnings)
        generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>运行摘要</title>
<style>
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #f5f5f5; }}
  h1 {{ font-size: 1.25rem; }}
  .meta {{ color: #666; margin-bottom: 20px; }}
  section {{ background: #fff; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  section.errors {{ border-left: 4px solid #c62828; }}
  details.warnings {{ border-left: 4px solid #f9a825; }}
  summary {{ cursor: pointer; font-weight: 600; user-select: none; }}
  summary:hover {{ color: #1565c0; }}
  ol {{ margin: 12px 0 0; padding-left: 1.4em; }}
  li {{ margin: 8px 0; }}
  pre {{ margin: 0; white-space: pre-wrap; word-break: break-word; font-family: inherit; }}
</style>
</head>
<body>
<h1>询价单自动化 — 运行摘要</h1>
<p class="meta">生成时间 {html.escape(generated)} · {err_count} 个错误 · {warn_count} 个警告</p>

<section class="errors">
<h2>错误 ({err_count})</h2>
<ol>
{li_items(self.errors)}
</ol>
</section>

<details class="warnings">
<summary>警告 ({warn_count}) — 点击展开/收起</summary>
<ol>
{li_items(self.warnings)}
</ol>
</details>
</body>
</html>
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    @staticmethod
    def _shorten(msg):
        """缩短日志消息用于摘要显示"""
        idx = msg.find('] ')
        if idx > 0:
            return msg[idx + 2:]
        return msg


def setup_logging():
    """配置日志系统，返回 error_collector"""
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # 控制台handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    ))
    logger.addHandler(console)
    
    # 文件handler
    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"run_{time.strftime('%Y%m%d_%H%M%S')}.log"),
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    ))
    logger.addHandler(file_handler)

    # 错误收集器 — 运行结束时醒目展示所有错误/警告
    error_collector = ErrorCollector()
    logger.addHandler(error_collector)
    
    return error_collector
