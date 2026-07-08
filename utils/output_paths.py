"""第三阶段输出路径：按日期子文件夹归档 PDF/Excel。"""
import os
import re
from datetime import datetime

from config import QUOTE_OUTPUT_ROOT


def date_folder_name(dt=None):
    """M.D 格式，如 5.18（不补零）。"""
    dt = dt or datetime.now()
    return f"{dt.month}.{dt.day}"


def get_quote_output_dir(date_label=None, root=None):
    """
    返回当日归档目录，不存在则创建。
    date_label: 如 '5.18'；默认今天。
    """
    root = root or QUOTE_OUTPUT_ROOT
    label = date_label or date_folder_name()
    path = os.path.join(root, label)
    os.makedirs(path, exist_ok=True)
    return path


def safe_filename(name):
    """去掉 Windows 非法文件名字符。"""
    if not name:
        return "unknown"
    s = str(name).strip()
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    return s[:200] if s else "unknown"


def target_paths(sourcing_no, date_label=None, root=None, pdf_base=None):
    """
    返回 (目录, pdf路径, xlsx路径)。
    PDF 使用 pdf_base（系统询价号，默认与 sourcing_no 相同）；Excel 使用寻源单号 sourcing_no。
    """
    folder = get_quote_output_dir(date_label=date_label, root=root)
    pdf_name = safe_filename(pdf_base or sourcing_no)
    xlsx_name = safe_filename(sourcing_no)
    return (
        folder,
        os.path.join(folder, f"{pdf_name}.pdf"),
        os.path.join(folder, f"{xlsx_name}.xlsx"),
    )
