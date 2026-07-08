"""
OMS 附件本地缓存清理：按项目子文件夹保留期删除过期目录。
起算时间 = 该文件夹内文件的最后一次修改时间（最后更新时间）。
"""
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _cache_root(project_root=None):
    if project_root:
        base = Path(project_root)
    else:
        base = Path(__file__).resolve().parent.parent
    from config import OMS_ATTACHMENTS_CACHE_DIR

    return (base / OMS_ATTACHMENTS_CACHE_DIR).resolve()


def _collect_protected_project_dirs(project_root=None):
    """当前 oms_data.json 中仍引用的附件所在项目子目录，清理时跳过。"""
    if project_root:
        base = Path(project_root)
    else:
        base = Path(__file__).resolve().parent.parent
    oms_path = base / "oms_data.json"
    root = _cache_root(project_root)
    protected = set()
    if not oms_path.is_file():
        return protected
    try:
        with open(oms_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return protected
    if not isinstance(data, dict):
        return protected
    for items in data.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for att in item.get("attachments") or []:
                try:
                    p = Path(str(att)).resolve()
                except (OSError, ValueError):
                    continue
                if root in p.parents or p.parent == root:
                    protected.add(p.parent.resolve())
    return protected


def _project_folder_last_update_time(dir_path: Path):
    """
    项目子文件夹的「最后更新时间」：遍历其内全部文件的修改时间 (st_mtime)，取最大值。
    若文件夹为空，则使用该文件夹自身的修改时间。
    返回 Unix 时间戳；无法读取时返回 None。
    """
    if not dir_path.is_dir():
        return None
    latest = None
    for root, _dirs, files in os.walk(dir_path):
        for name in files:
            fp = Path(root) / name
            try:
                t = fp.stat().st_mtime
            except OSError:
                continue
            latest = t if latest is None else max(latest, t)
    if latest is not None:
        return latest
    try:
        return dir_path.stat().st_mtime
    except OSError:
        return None


def cleanup_stale_oms_attachment_caches(
    retention_days,
    project_root=None,
    protect_referenced=True,
):
    """
    删除 cache/oms_attachments 下超过 retention_days 的项目子文件夹。

    自「最后更新时间」起满 retention_days 天才删除（非创建时间）。
    最后更新时间 = 该子文件夹内所有附件文件 st_mtime 的最大值。

    retention_days <= 0 时不执行。
    protect_referenced: 跳过 oms_data.json 仍引用的路径所在目录。
    返回已删除的子目录绝对路径列表。
    """
    days = int(retention_days or 0)
    if days <= 0:
        return []

    root = _cache_root(project_root)
    if not root.is_dir():
        return []

    logger.info(
        f"  OMS 附件缓存检查: 保留 {days} 天，"
        f"以各项目文件夹内「最后文件更新时间」起算"
    )

    now = time.time()
    cutoff = now - days * 86400
    protected = _collect_protected_project_dirs(project_root) if protect_referenced else set()
    removed = []

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        resolved = entry.resolve()
        if protected and resolved in protected:
            logger.debug(f"  跳过清理（oms_data 仍引用）: {entry.name}")
            continue
        last_update = _project_folder_last_update_time(entry)
        if last_update is None:
            continue
        # 最后更新时间仍在保留期内 → 保留
        if last_update >= cutoff:
            continue
        try:
            shutil.rmtree(entry)
            removed.append(str(resolved))
            last_str = datetime.fromtimestamp(last_update).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            age_days = int((now - last_update) // 86400)
            logger.info(
                f"  已清理 OMS 附件缓存: {entry.name} "
                f"（最后更新 {last_str}，已满 {age_days} 天）"
            )
        except OSError as e:
            logger.warning(f"  无法删除附件缓存目录 {entry}: {e}")

    if removed:
        logger.info(f"  OMS 附件缓存清理完成: 删除 {len(removed)} 个项目文件夹")
    return removed
