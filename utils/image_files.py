"""询价单照片：去重筛选；OMS 附件一般为 JPG，已是 JPG 则原样使用，不强制转换。"""
import hashlib
import logging
import os
import shutil
from typing import List, Optional

logger = logging.getLogger(__name__)

try:
    from PIL import Image
except ImportError:
    Image = None


def is_jpg_file(path: str) -> bool:
    return str(path or "").lower().endswith((".jpg", ".jpeg"))


def format_file_size(num_bytes: int) -> str:
    """人类可读文件大小，如 85.3 KB。"""
    n = int(num_bytes or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def find_photos_below_min_bytes(paths, min_bytes: int):
    """返回 [(绝对路径, 字节数), ...] 小于 min_bytes 的 JPG/图片。"""
    small = []
    for p in paths or []:
        if not p:
            continue
        ap = os.path.abspath(str(p))
        if not os.path.isfile(ap):
            continue
        size = os.path.getsize(ap)
        if size < min_bytes:
            small.append((ap, size))
    return small


def boost_jpg_quality_in_place(
    path: str,
    min_bytes: int,
    quality: int = 100,
) -> bool:
    """
    备件网要求单张 ≥ min_bytes 时：对过小的 JPG 以最高质量原地重存（覆盖原文件）。
    返回 True 表示已增强且现满足体积要求；False 表示未改动或仍不足。
    """
    ap = os.path.abspath(str(path or ""))
    if not ap or not os.path.isfile(ap):
        return False
    if not is_jpg_file(ap):
        return False
    old_size = os.path.getsize(ap)
    if old_size >= min_bytes:
        return False
    if Image is None:
        logger.warning(
            f"  照片过小且未安装 Pillow，无法自动增强: {os.path.basename(ap)}"
        )
        return False

    tmp = ap + ".boost_tmp.jpg"
    try:
        with Image.open(ap) as im:
            im.convert("RGB").save(tmp, "JPEG", quality=quality, optimize=False)
        new_size = os.path.getsize(tmp)
        if new_size < min_bytes:
            logger.warning(
                f"  自动增强后仍 < {format_file_size(min_bytes)}: "
                f"{os.path.basename(ap)} ({format_file_size(new_size)})"
            )
            return False
        os.replace(tmp, ap)
        logger.info(
            f"  已自动增强照片: {os.path.basename(ap)} "
            f"({format_file_size(old_size)} → {format_file_size(new_size)})"
        )
        return True
    except Exception as e:
        logger.warning(f"  自动增强照片失败 {os.path.basename(ap)}: {e}")
        return False
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def ensure_photos_min_bytes(paths, min_bytes: int, quality: int = 100) -> int:
    """对路径列表中过小的 JPG 尝试 quality 增强；返回成功增强的张数。"""
    count = 0
    for p in paths or []:
        if boost_jpg_quality_in_place(p, min_bytes, quality=quality):
            count += 1
    return count


def prepare_photo_for_upload(path: str, output_path: Optional[str] = None) -> str:
    """
    备件网上传用照片路径。
    - 已是 .jpg / .jpeg：直接返回（或复制/移动到 output_path），不做格式转换。
    - 非 JPG（如误下的 PNG 缩略图）：有 Pillow 时才转换，否则报错提示。
    """
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"图片不存在: {src}")

    if output_path:
        dest = os.path.abspath(output_path)
        if not dest.lower().endswith(".jpg"):
            dest = os.path.splitext(dest)[0] + ".jpg"
    elif is_jpg_file(src):
        return src
    else:
        dest = os.path.splitext(src)[0] + ".jpg"

    if is_jpg_file(src):
        if os.path.abspath(src) == os.path.abspath(dest):
            return dest
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(src, dest)
        return dest

    if Image is None:
        raise RuntimeError(
            f"文件不是 JPG（{os.path.basename(src)}），"
            f"且未安装 Pillow，无法转换。请确保 OMS 下载的是 JPG 原图。"
        )

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with Image.open(src) as im:
        im.convert("RGB").save(dest, "JPEG", quality=92)
    if os.path.abspath(src) != os.path.abspath(dest):
        try:
            os.remove(src)
        except OSError:
            pass
    logger.info(f"  已将非 JPG 转为: {os.path.basename(dest)}")
    return dest


def load_template_photo_paths(template_dir: str) -> List[str]:
    """OMS 无附件时使用的 assets/photos 模板图（全部可用文件，不去重截断）。"""
    photos: List[str] = []
    if not template_dir or not os.path.isdir(template_dir):
        return photos
    for fname in sorted(os.listdir(template_dir)):
        fpath = os.path.join(template_dir, fname)
        if os.path.isfile(fpath) and fname.lower().endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
        ):
            photos.append(os.path.abspath(fpath))
    return photos


def pick_distinct_photo_files(
    paths: List[str],
    max_count: int = 1,
    min_bytes: int = 8000,
) -> List[str]:
    """按文件大小排序，去掉过小文件与内容重复项，最多保留 max_count 张。"""
    def _rank_key(p):
        # 优先 JPG、再按体积（与 OMS 手动下载一致）
        return (0 if is_jpg_file(p) else 1, -os.path.getsize(p))

    ranked = sorted(
        [os.path.abspath(p) for p in paths if p and os.path.isfile(p)],
        key=_rank_key,
    )
    chosen: List[str] = []
    seen_hash = set()

    for p in ranked:
        size = os.path.getsize(p)
        if size < min_bytes:
            logger.debug(f"  跳过过小图片 ({size}B): {os.path.basename(p)}")
            continue
        with open(p, "rb") as f:
            digest = hashlib.md5(f.read()).hexdigest()
        if digest in seen_hash:
            logger.debug(f"  跳过重复图片: {os.path.basename(p)}")
            continue
        seen_hash.add(digest)
        chosen.append(p)
        if len(chosen) >= max_count:
            break
    return chosen


def finalize_downloaded_attachment(src: str, dest_jpg: str) -> str:
    """将 OMS 下载的临时文件落盘为最终 .jpg 路径（已是 JPG 则仅移动/复制）。"""
    dest = prepare_photo_for_upload(src, dest_jpg)
    try:
        from config import SPAREPARTS_MIN_PHOTO_BYTES

        boost_jpg_quality_in_place(dest, SPAREPARTS_MIN_PHOTO_BYTES)
    except ImportError:
        boost_jpg_quality_in_place(dest, 100 * 1024)
    return dest
