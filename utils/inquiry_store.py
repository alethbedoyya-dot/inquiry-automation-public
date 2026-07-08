"""Persistence helpers for OMS data, inquiry results, and session records."""

import glob
import json
import logging
import os
import time


OMS_DATA_FILE = "oms_data.json"

def merge_oms_data_group(project_name, items, filepath=OMS_DATA_FILE):
    """将单项目待处理行合并进 oms_data.json（扩展供应商后刷新用）。"""
    if not items:
        return
    groups = load_oms_data(filepath) or {}
    name = (project_name or "").strip()
    key = name
    for k in list(groups.keys()):
        if k == name or (name and name in k) or (k and k in name):
            key = k
            break
    serializable = []
    for item in items:
        clean = {}
        for k, v in item.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                clean[k] = v
            elif isinstance(v, list):
                clean[k] = [str(x) for x in v]
            else:
                clean[k] = str(v)
        serializable.append(clean)
    groups[key] = serializable
    save_oms_data(groups, filepath)


def save_oms_data(groups, filepath=OMS_DATA_FILE):
    """将OMS提取的分组数据保存为JSON（键为项目名称，无项目名时为梯号）"""
    serializable = {}
    for group_key, items in groups.items():
        clean_items = []
        for item in items:
            clean = {}
            for k, v in item.items():
                if isinstance(v, (str, int, float, bool, type(None))):
                    clean[k] = v
                elif isinstance(v, list):
                    clean[k] = [str(x) for x in v]
                else:
                    clean[k] = str(v)
            clean_items.append(clean)
        serializable[group_key] = clean_items
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    logging.getLogger(__name__).info(f"OMS数据已保存到: {filepath} ({len(serializable)} 组)")


def load_oms_data(filepath=OMS_DATA_FILE):
    """从JSON加载OMS分组数据"""
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    logging.getLogger(__name__).info(f"从 {filepath} 加载了 {len(data)} 组OMS数据")
    return data


# ============================================================
# 询价单结果持久化 — 支持两阶段（创建→人工审批→续接）
# ============================================================
INQUIRY_RESULTS_FILE = "inquiry_results.json"
# 阶段二全部完成时的唯一备份（短文件名；新备份会覆盖并删除旧的长名归档）
INQUIRY_LAST_FILE = "inquiry_last.json"
FINALIZE_RESULTS_FILE = "finalize_results.json"
LEGACY_ARCHIVE_GLOB = "inquiry_results_archived_*.json"
SESSIONS_DIR = "../sessions"  # 自动备份存放目录（项目上级目录）
SESSIONS_EXCEL = "../sessions/询价单记录.xlsx"  # 汇总 Excel


def _upsert_to_sessions_excel(rows):
    """向 sessions/询价单记录.xlsx 插入或覆盖记录行（按寻源单号+项目名称去重，不存在则创建）。"""
    try:
        from openpyxl import Workbook, load_workbook
    except ImportError:
        return
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    headers = ["备份时间", "项目名称", "询价单号", "物料数", "工厂", "供应商",
               "寻源单号", "报价单号", "阶段三状态"]
    if os.path.isfile(SESSIONS_EXCEL):
        wb = load_workbook(SESSIONS_EXCEL)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "询价单记录"
        ws.append(headers)

    # 解析表头，定位"寻源单号"和"项目名称"列
    col_sourcing = col_project = None
    for c in range(1, ws.max_column + 1):
        h = ws.cell(1, c).value
        if h and "寻源单号" in str(h):
            col_sourcing = c
        elif h and "项目名称" in str(h):
            col_project = c

    for row_data in rows:
        # 行数据顺序：[备份时间, 项目名称, 询价单号, 物料数, 工厂, 供应商, 寻源单号, 报价单号, 阶段三状态]
        sourcing_val = str(row_data[6] or "").strip() if len(row_data) > 6 else ""
        project_val = str(row_data[1] or "").strip() if len(row_data) > 1 else ""

        # 两个关键字段都为空时退化为追加
        if not sourcing_val and not project_val:
            ws.append(row_data)
            continue

        # 在现有行中查找匹配行（寻源单号 + 项目名称 同时相等）
        matched_row = None
        for r in range(2, ws.max_row + 1):
            cell_sourcing = str(ws.cell(r, col_sourcing).value or "").strip() if col_sourcing else ""
            cell_project = str(ws.cell(r, col_project).value or "").strip() if col_project else ""

            if sourcing_val and project_val:
                if cell_sourcing == sourcing_val and cell_project == project_val:
                    matched_row = r
                    break
            elif sourcing_val:
                if cell_sourcing == sourcing_val:
                    matched_row = r
                    break
            elif project_val:
                if cell_project == project_val:
                    matched_row = r
                    break

        if matched_row:
            # 原地覆盖整行
            for c, val in enumerate(row_data, start=1):
                ws.cell(matched_row, c).value = val
        else:
            ws.append(row_data)

    try:
        wb.save(SESSIONS_EXCEL)
    except PermissionError:
        pass


def _backup_inquiry_results_if_needed():
    """
    阶段一启动时检查：若 inquiry_results.json 已有未完结数据，提示将与新数据合并。
    （不再创建 sessions/ JSON 备份——数据通过合并保存，不会丢失。）
    """
    log = logging.getLogger(__name__)
    existing = _load_inquiries_from_file(INQUIRY_RESULTS_FILE)
    if not existing:
        return
    log.warning(f"  ⚠ 发现之前未完结的待办数据（{len(existing)} 组），将与本轮新数据合并保存。")


def _log_results_to_sessions_excel(results):
    """阶段一完成后，将本轮创建的所有询价单结果记录到 sessions/询价单记录.xlsx（按寻源单号+项目名称去重）。"""
    if not results:
        return
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    excel_rows = []
    for inv in results:
        excel_rows.append([
            now_str,
            inv.get("project_name", ""),
            inv.get("inquiry_no", ""),
            inv.get("material_count", ""),
            inv.get("factory_type", ""),
            inv.get("oms_supplier", ""),
            inv.get("oms_sourcing_no", ""),
            inv.get("quotation_no", ""),
            inv.get("finalize_status", ""),
        ])
    _upsert_to_sessions_excel(excel_rows)


def _update_sessions_excel_for_inquiries(inquiries):
    """
    阶段二/三完成后，按 inquiry_no 匹配更新 Excel 中的 quotation_no 和 finalize_status。
    """
    if not inquiries or not os.path.isfile(SESSIONS_EXCEL):
        return
    try:
        from openpyxl import load_workbook
    except ImportError:
        return
    try:
        wb = load_workbook(SESSIONS_EXCEL)
        ws = wb.active
        header = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        col_inquiry = col_quotation = col_status = col_sourcing = col_project = None
        for i, h in enumerate(header):
            if h and "询价单号" in str(h):
                col_inquiry = i + 1
            elif h and "寻源单号" in str(h):
                col_sourcing = i + 1
            elif h and "项目名称" in str(h):
                col_project = i + 1
            elif h and "报价单号" in str(h):
                col_quotation = i + 1
            elif h and "阶段三状态" in str(h):
                col_status = i + 1
        if not col_inquiry:
            wb.close()
            return
        updated = 0
        for inv in inquiries:
            ino = (inv.get("inquiry_no") or "").strip()
            sno = (inv.get("oms_sourcing_no") or "").strip()
            pno = (inv.get("project_name") or "").strip()
            if not ino and not sno and not pno:
                continue
            for r in range(2, ws.max_row + 1):
                cell_inquiry = str(ws.cell(r, col_inquiry).value or "").strip()
                cell_sourcing = str(ws.cell(r, col_sourcing).value or "").strip() if col_sourcing else ""
                cell_project = str(ws.cell(r, col_project).value or "").strip() if col_project else ""
                matched = (ino and cell_inquiry == ino) or (sno and cell_sourcing == sno) or (pno and cell_project == pno)
                if matched:
                    if col_quotation and inv.get("quotation_no"):
                        ws.cell(r, col_quotation).value = inv["quotation_no"]
                        updated += 1
                    if col_status and inv.get("finalize_status"):
                        ws.cell(r, col_status).value = inv["finalize_status"]
        wb.save(SESSIONS_EXCEL)
        wb.close()
        if updated:
            logging.getLogger(__name__).info(f"  已更新 {SESSIONS_EXCEL}（{updated} 条）")
    except (PermissionError, OSError):
        pass


def _cleanup_old_session_backups():
    """删除 sessions/ 中超过 SESSIONS_RETENTION_DAYS 天的备份 JSON 文件。"""
    from config import SESSIONS_RETENTION_DAYS
    if not SESSIONS_RETENTION_DAYS or not os.path.isdir(SESSIONS_DIR):
        return
    cutoff = time.time() - SESSIONS_RETENTION_DAYS * 86400
    log = logging.getLogger(__name__)
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.startswith("inquiry_results_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(SESSIONS_DIR, fname)
        try:
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                log.info(f"  已清理过期备份: {fname}")
        except OSError:
            pass


def _load_inquiries_from_file(filepath):
    """读取 JSON 中的 inquiries 列表；失败或不存在返回 []。"""
    if not filepath or not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("inquiries") or [])
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _inquiry_json_has_items(filepath):
    return len(_load_inquiries_from_file(filepath)) > 0


def _inquiries_any_quotation_no(inquiries):
    return any((inv.get("quotation_no") or "").strip() for inv in (inquiries or []))


def _inquiries_all_quotation_no(inquiries):
    if not inquiries:
        return False
    return all((inv.get("quotation_no") or "").strip() for inv in inquiries)


def _find_newest_legacy_inquiry_archive():
    """兼容旧版 inquiry_results_archived_时间戳.json，取最新一份。"""
    candidates = glob.glob(LEGACY_ARCHIVE_GLOB)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def cleanup_legacy_inquiry_archives():
    """删除旧的长文件名归档，只保留 inquiry_last.json。"""
    log = logging.getLogger(__name__)
    for path in glob.glob(LEGACY_ARCHIVE_GLOB):
        try:
            os.remove(path)
            log.info(f"  已删除旧归档: {os.path.basename(path)}")
        except OSError as e:
            log.warning(f"  无法删除旧归档 {path}: {e}")


def resolve_inquiry_results_path(explicit_path=None, for_finalize=False):
    """
    决定 --resume / --finalize 读取哪个 JSON。

    --resume：优先 inquiry_results.json（待办）；空则用 inquiry_last.json。
    --finalize：优先「含 quotation_no」的文件；inquiry_results 仅有阶段一残留
    而 inquiry_last 已含报价单号时，自动用 inquiry_last.json。
    """
    log = logging.getLogger(__name__)
    if explicit_path:
        return os.path.abspath(explicit_path)

    primary = os.path.abspath(INQUIRY_RESULTS_FILE)
    backup = os.path.abspath(INQUIRY_LAST_FILE)
    p_items = _load_inquiries_from_file(primary)
    b_items = _load_inquiries_from_file(backup)

    if for_finalize and p_items and b_items:
        p_has = _inquiries_any_quotation_no(p_items)
        b_has = _inquiries_any_quotation_no(b_items)
        if b_has and not p_has:
            log.info(
                f"  {INQUIRY_RESULTS_FILE} 无报价单号，阶段三将使用 {INQUIRY_LAST_FILE}"
            )
            cleanup_legacy_inquiry_archives()
            return backup
        if p_has:
            return primary
        if b_has:
            return backup

    if for_finalize:
        if p_items and _inquiries_any_quotation_no(p_items):
            return primary
        if b_items and _inquiries_any_quotation_no(b_items):
            if not p_items:
                log.info(f"  将使用: {INQUIRY_LAST_FILE}")
            cleanup_legacy_inquiry_archives()
            return backup

    # --resume：待办列表优先
    if p_items:
        return primary

    if b_items:
        log.info(f"  {INQUIRY_RESULTS_FILE} 无待办，将使用: {INQUIRY_LAST_FILE}")
        cleanup_legacy_inquiry_archives()
        return backup

    # 扫描 sessions/ 中的自动备份
    session_backups = []
    if os.path.isdir(SESSIONS_DIR):
        session_backups = sorted(
            glob.glob(os.path.join(SESSIONS_DIR, "inquiry_results_*.json")),
            key=os.path.getmtime,
            reverse=True,
        )
    for backup_file in session_backups:
        items = _load_inquiries_from_file(backup_file)
        if items:
            log.info(
                f"  自动发现会话备份: {os.path.basename(backup_file)}"
                f"（{len(items)} 组待办）"
            )
            cleanup_legacy_inquiry_archives()
            return os.path.abspath(backup_file)

    legacy = _find_newest_legacy_inquiry_archive()
    if legacy:
        log.info(
            f"  将使用旧版归档: {os.path.basename(legacy)}"
            f"（下次完成后会合并为 {INQUIRY_LAST_FILE}）"
        )
        return os.path.abspath(legacy)

    return primary


def save_inquiry_last_archive(inquiries):
    """阶段二全部成功：写入 inquiry_last.json，并清理旧的长名归档。"""
    log = logging.getLogger(__name__)
    path = os.path.abspath(INQUIRY_LAST_FILE)
    payload = {
        "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inquiries": inquiries,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    cleanup_legacy_inquiry_archives()
    log.info(f"\n✓ 全部询价单处理完毕，已写入备份: {INQUIRY_LAST_FILE}")
    log.info(f"  下次可直接运行: python main.py --resume")
    return path


def _inquiry_dedup_key(inv):
    """按寻源单号+项目名称生成去重键。"""
    sno = (inv.get("oms_sourcing_no") or "").strip()
    pn = (inv.get("project_name") or "").strip()
    return f"{sno}|||{pn}"


def replace_inquiry_results(inquiries, filepath=INQUIRY_RESULTS_FILE):
    """Overwrite inquiry_results.json with the given pending inquiries."""
    data = {
        "inquiries": inquiries,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(inquiries),
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_inquiry_results(results, filepath=INQUIRY_RESULTS_FILE):
    """
    将第一阶段创建的询价单结果合并保存（按寻源单号+项目名称与已有数据去重合并）。
    传入空列表时清空文件（阶段二/三全部完成后调用）。

    保存内容: inquiry_no, project_name, ladder_no, factory_type, oms_address, material_count
    供 --resume 模式加载后继续审批→购物车→报价单流程。
    """
    # 空列表 → 清空文件
    if not results:
        data = {"inquiries": [], "created_at": time.strftime('%Y-%m-%d %H:%M:%S'), "total": 0}
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    # 加载已有数据，与新数据按"寻源单号+项目名称"合并
    existing = _load_inquiries_from_file(filepath) or []
    merged_map = {}
    for inv in existing:
        key = _inquiry_dedup_key(inv)
        merged_map[key] = inv
    for inv in results:
        merged_map[_inquiry_dedup_key(inv)] = inv
    merged = list(merged_map.values())

    data = {
        "inquiries": merged,
        "created_at": time.strftime('%Y-%m-%d %H:%M:%S'),
        "total": len(merged)
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger = logging.getLogger(__name__)
    merge_note = f"（{len(results)} 组新增/更新，共 {len(merged)} 组）" if existing else f"（{len(results)} 组）"
    logger.info(f"询价单结果已保存到: {filepath} {merge_note}")
    if not results:
        return
    logger.info("")
    logger.info("=" * 60)
    logger.info("  阶段一完成！所有询价单已创建并提交。")
    logger.info(f"  结果已保存到 {filepath}")
    logger.info("")
    logger.info("  接下来请等待所有询价单审批完成（状态变为'已完结'），")
    logger.info(f"  然后运行: python main.py --resume")
    logger.info("  继续执行购物车→报价单流程。")
    logger.info("=" * 60)


def load_inquiry_results(filepath=INQUIRY_RESULTS_FILE):
    """从JSON加载第一阶段产生的询价单结果；文件不存在返回 None，存在则返回 inquiries 列表（可能为空）。"""
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    logger = logging.getLogger(__name__)
    inquiries = data.get("inquiries", [])
    logger.info(f"从 {filepath} 加载了 {len(inquiries)} 组询价单结果")
    logger.info(f"  创建时间: {data.get('created_at', data.get('archived_at', '未知'))}")
    return inquiries


def save_finalize_results(inquiries, filepath=FINALIZE_RESULTS_FILE):
    """保存第三阶段处理结果（含 finalize_status / 错误信息）。"""
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inquiries": inquiries,
    }
    path = os.path.abspath(filepath)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logging.getLogger(__name__).info(f"第三阶段结果已保存: {path}")
