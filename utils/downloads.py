"""浏览器下载文件等待与移动。"""
import os
import time
import logging
import shutil
from typing import Optional, Tuple

from config import DOWNLOAD_WAIT_TIMEOUT

logger = logging.getLogger(__name__)


def _newest_file(directory, extensions=(".pdf", ".xlsx", ".xls", ".crdownload")):
    if not directory or not os.path.isdir(directory):
        return None
    best = None
    best_mtime = 0
    for name in os.listdir(directory):
        low = name.lower()
        if low.endswith(".crdownload") or low.endswith(".tmp"):
            continue
        if extensions and not any(low.endswith(ext.lstrip(".")) for ext in extensions if ext != ".crdownload"):
            if not (low.endswith(".pdf") or low.endswith(".xlsx") or low.endswith(".xls")):
                continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        mtime = os.path.getmtime(path)
        if mtime > best_mtime:
            best_mtime = mtime
            best = path
    return best


def _download_search_dirs(directory, extra_dirs=None):
    """归档目录 + 额外目录（去重、仅保留存在的路径）。"""
    dirs = []
    for d in [directory] + (extra_dirs or []):
        if not d:
            continue
        d = os.path.abspath(d)
        if d in dirs:
            continue
        if os.path.isdir(d):
            dirs.append(d)
    user_downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    if user_downloads not in dirs and os.path.isdir(user_downloads):
        dirs.append(user_downloads)
    return dirs


def wait_for_new_download(
    directory,
    since_time=None,
    timeout=DOWNLOAD_WAIT_TIMEOUT,
    extensions=(".pdf", ".xlsx", ".xls"),
    extra_dirs=None,
) -> Optional[str]:
    """
    等待目录中出现新完成的下载文件（无 .crdownload）。
    since_time: 仅接受修改时间 >= since_time 的文件。
    extra_dirs: 额外扫描目录（如系统「下载」文件夹）。
    """
    if since_time is None:
        since_time = time.time() - 2
    deadline = time.time() + timeout
    search_dirs = _download_search_dirs(directory, extra_dirs)

    while time.time() < deadline:
        for directory in search_dirs:
            pending = False
            for name in os.listdir(directory):
                if name.endswith(".crdownload") or name.endswith(".tmp"):
                    pending = True
                    break
            if pending:
                break
            for name in os.listdir(directory):
                low = name.lower()
                if not any(low.endswith(ext) for ext in extensions):
                    continue
                path = os.path.join(directory, name)
                if os.path.isfile(path) and os.path.getmtime(path) >= since_time - 1:
                    s1 = os.path.getsize(path)
                    time.sleep(0.4)
                    s2 = os.path.getsize(path)
                    if s1 == s2 and s1 > 0:
                        logger.info(f"  检测到下载文件: {path}")
                        return path
        time.sleep(0.6)
    logger.warning(f"  未在以下目录检测到新文件: {search_dirs}")
    return None


def move_to_target(download_path, target_path) -> str:
    """将下载文件移动/重命名为目标路径。"""
    if not download_path or not os.path.isfile(download_path):
        raise FileNotFoundError(f"下载文件不存在: {download_path}")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if os.path.abspath(download_path) == os.path.abspath(target_path):
        return target_path
    if os.path.exists(target_path):
        os.remove(target_path)
    shutil.move(download_path, target_path)
    logger.info(f"  已归档: {target_path}")
    return target_path


def wait_for_new_image_download(
    directory,
    since_time=None,
    timeout=None,
    extra_dirs=None,
) -> Optional[str]:
    """等待图片类下载完成（阶段一 OMS 附件）。"""
    from config import OMS_ATTACHMENT_DOWNLOAD_TIMEOUT, OMS_ATTACHMENT_IMAGE_EXTENSIONS

    if timeout is None:
        timeout = OMS_ATTACHMENT_DOWNLOAD_TIMEOUT
    return wait_for_new_download(
        directory,
        since_time=since_time,
        timeout=timeout,
        extensions=OMS_ATTACHMENT_IMAGE_EXTENSIONS,
        extra_dirs=extra_dirs,
    )


def wait_and_archive(
    directory,
    target_path,
    since_time=None,
    timeout=DOWNLOAD_WAIT_TIMEOUT,
    extra_dirs=None,
) -> str:
    ext = os.path.splitext(target_path)[1].lower()
    exts = (ext,) if ext else (".pdf", ".xlsx", ".xls")
    found = wait_for_new_download(
        directory,
        since_time=since_time,
        timeout=timeout,
        extensions=exts,
        extra_dirs=extra_dirs,
    )
    if not found:
        searched = _download_search_dirs(directory, extra_dirs)
        raise TimeoutError(
            f"在 {timeout}s 内未检测到下载文件 (已扫描: {searched})"
        )
    return move_to_target(found, target_path)
