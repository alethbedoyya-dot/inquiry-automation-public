"""Mixin helpers split from modules.oms."""

import hashlib
import json
import logging
import os
import re
import shutil
import time
import urllib.parse
import urllib.request

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import (
    FINALIZE_FILTER_BY_SOURCING_NO,
    OMS_ATTACHMENT_DOWNLOAD_TIMEOUT,
    OMS_ATTACHMENT_HEADER_KEYWORDS,
    OMS_ATTACHMENT_IMAGE_EXTENSIONS,
    OMS_ATTACHMENT_MIN_FILE_BYTES,
    OMS_ATTACHMENTS_CACHE_DIR,
    OMS_GROUP_MATCH_MIN_LEN,
    OMS_HOME_URL,
    OMS_MAX_ATTACHMENTS_PER_ROW,
    OMS_SOURCING_COLUMN,
    OMS_SUPPLIER_EXTEND_SEARCH_KEY,
    OMS_SUPPLIER_SHANGHAI,
    OMS_SUPPLIER_ZHONGSHAN,
    OMS_URL,
    PAGE_LOAD_TIMEOUT,
    ELEMENT_WAIT_TIMEOUT,
)
from utils.downloads import wait_for_new_image_download
from utils.image_files import finalize_downloaded_attachment, pick_distinct_photo_files
from utils.oms_grouping import (
    collect_group_match_needles,
    group_oms_rows,
    parse_anonymous_group_key,
    resolve_group_oms_email_filter,
)

logger = logging.getLogger(__name__)

_OMS_COLUMN_FILTER_ALIASES = {
    "????": ("????",),
    "????": ("????", "??", OMS_SOURCING_COLUMN),
    "?????": ("?????", "??????", "??????"),
    "????": ("????",),
    "??": ("??", "WBS??"),
}


class OMSFinalizeMixin:
    _OMS_SELECTION_JS = """
            function norm(s) { return (s || '').replace(/\\s+/g, ''); }
            function isClonedOrAuxTable(table) {
                if (!table) return true;
                var node = table;
                for (var d = 0; d < 8 && node; d++) {
                    var cls = (node.className || '') + '';
                    if (/DTFC_Cloned|DTFC_Left|DTFC_Right|dataTables_scrollFoot|fixedColumn|FixedColumns/i.test(cls)) {
                        return true;
                    }
                    node = node.parentElement;
                }
                return false;
            }
            function isVisibleEl(el) {
                if (!el) return false;
                try {
                    var r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) return false;
                    var st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (parseFloat(st.opacity || '1') < 0.05) return false;
                } catch (e) { return false; }
                return true;
            }
            function isVisibleDataRow(tr) {
                if (!tr || !tr.querySelectorAll('td').length) return false;
                if (tr.querySelectorAll('td').length < 3) return false;
                if (!isVisibleEl(tr)) return false;
                var tbl = tr.closest('table');
                if (isClonedOrAuxTable(tbl)) return false;
                var r = tr.getBoundingClientRect();
                if (r.bottom < 0 || r.top > (window.innerHeight || 800) + 50) return false;
                return true;
            }
            function findPrimaryOmsTable() {
                var best = null, bestVis = 0;
                var tables = document.querySelectorAll('table.dataTable, table');
                for (var i = 0; i < tables.length; i++) {
                    var tbl = tables[i];
                    if (isClonedOrAuxTable(tbl)) continue;
                    var inScroll = tbl.closest('.dataTables_scrollBody');
                    var trs = (inScroll || tbl).querySelectorAll('tbody tr');
                    var vis = 0;
                    for (var j = 0; j < trs.length; j++) {
                        if (isVisibleDataRow(trs[j])) vis++;
                    }
                    if (vis > bestVis) { bestVis = vis; best = tbl; }
                }
                return {table: best, visibleRows: bestVis};
            }
            function extractSourcingItemKey(rowText) {
                var sn = '', item = '';
                var txt = rowText || '';
                var m = txt.match(/PSM\\d{6,}/i);
                if (m) sn = m[0].toUpperCase();
                if (!sn) {
                    var sm = txt.match(/[A-Z]{2,}\\d{8,14}/);
                    if (sm) sn = sm[0].toUpperCase();
                    else {
                        sm = txt.match(/\\d{9,}-\\d+/);
                        if (sm) sn = sm[0];
                    }
                }
                var im = txt.match(/Item[^0-9]{0,12}(\\d+)/i);
                if (im) item = im[1];
                if (!item) {
                    var tds = txt.split(/\\s+/);
                    for (var i = tds.length - 1; i >= 0; i--) {
                        if (/^\\d{4,}$/.test(tds[i])) { item = tds[i]; break; }
                    }
                }
                var key = sn + '|' + item;
                return (key === '|') ? '' : key;
            }
            function countCheckedAid() {
                var n = 0, aids = [];
                document.querySelectorAll('input[name="aid"]').forEach(function(cb) {
                    if ((cb.className || '').indexOf('allSel') >= 0) return;
                    if (!cb.checked) return;
                    n++;
                    aids.push(cb.value || '');
                });
                return {count: n, aids: aids};
            }
            function clearAllOmsAidSelection() {
                var cleared = 0;
                document.querySelectorAll('input[type="checkbox"].allSel, input.allSel')
                    .forEach(function(cb) {
                        if (cb.checked) {
                            try { cb.click(); } catch (e) { cb.checked = false; }
                            cleared++;
                        }
                    });
                document.querySelectorAll('input[name="aid"]').forEach(function(cb) {
                    if ((cb.className || '').indexOf('allSel') >= 0) return;
                    if (!cb.checked) return;
                    try { cb.click(); } catch (e) {
                        cb.checked = false;
                        cb.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    cleared++;
                });
                return {cleared: cleared, remaining: countCheckedAid()};
            }
            function dedupeCheckedAidKeepMarked() {
                var kept = {}, removed = 0;
                document.querySelectorAll('input[name="aid"]').forEach(function(cb) {
                    if ((cb.className || '').indexOf('allSel') >= 0) return;
                    if (!cb.checked) return;
                    var v = cb.value || '';
                    var marked = cb.getAttribute('data-oms-marker');
                    if (marked) {
                        if (kept[v]) {
                            try { cb.click(); } catch (e) { cb.checked = false; }
                            removed++;
                        } else {
                            kept[v] = true;
                        }
                        return;
                    }
                    if (kept[v]) {
                        try { cb.click(); } catch (e) { cb.checked = false; }
                        removed++;
                    } else {
                        kept[v] = true;
                    }
                });
                return {removed: removed, remaining: countCheckedAid()};
            }
        """

    def _uncheck_all_oms_row_checkboxes(self):
        """全页清除 OMS 勾选（含隐藏表/克隆表/其它页残留），避免重复寻源单号+Item。"""
        info = self.browser.driver.execute_script(
            self._OMS_SELECTION_JS
            + """
            return clearAllOmsAidSelection();
        """
        ) or {}
        cleared = int(info.get("cleared") or 0)
        remaining = info.get("remaining") or {}
        rem_n = int(remaining.get("count") or 0)
        if cleared > 0:
            logger.info(f"  已全页清除勾选: {cleared} 处")
        if rem_n > 0:
            logger.warning(
                f"  清除后仍有 {rem_n} 个 aid 为 checked，"
                f"values={remaining.get('aids', [])[:5]}"
            )
        time.sleep(0.4)
        return cleared

    def _log_oms_selection_state(self, label):
        """调试：统计全页 checked 的 aid 数量。"""
        info = self.browser.driver.execute_script(
            self._OMS_SELECTION_JS + "return countCheckedAid();"
        ) or {}
        n = int(info.get("count") or 0)
        aids = info.get("aids") or []
        primary = self.browser.driver.execute_script(
            self._OMS_SELECTION_JS + "return findPrimaryOmsTable();"
        ) or {}
        logger.info(
            f"  [{label}] 全页已勾选 aid={n}，主表可见行={primary.get('visibleRows', '?')}，"
            f"样例={aids[:6]}"
        )
        return n

    def _is_oms_duplicate_selection_info_dialog_visible(self):
        """「信息」弹窗：勾选中存在相同寻源单号与 Item（layui 常为 fixed，不能用 offsetParent）。"""
        return bool(
            self.browser.driver.execute_script("""
            function vis(el) {
                if (!el) return false;
                try {
                    var r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    var st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (parseFloat(st.opacity || '1') < 0.05) return false;
                } catch (e) { return false; }
                return true;
            }
            function isTargetDialog(txt) {
                if (!txt) return false;
                return txt.indexOf('去除勾选') >= 0
                    || (txt.indexOf('寻源单号') >= 0 && txt.indexOf('Item') >= 0)
                    || (txt.indexOf('寻源单号') >= 0 && txt.indexOf('相同') >= 0);
            }
            var layers = document.querySelectorAll(
                '.layui-layer-dialog, .layui-layer.layui-layer-dialog, .layui-layer'
            );
            for (var i = layers.length - 1; i >= 0; i--) {
                var layer = layers[i];
                if (!vis(layer)) continue;
                if (isTargetDialog(layer.innerText || layer.textContent || ''))
                    return true;
            }
            return false;
        """)
        )

    def _dismiss_oms_duplicate_selection_dialog(self, max_wait_sec=10):
        """
        点击「信息」弹窗上的「确定」，关闭相同寻源单号+Item 提示。
        Returns: 是否检测到该弹窗并已关闭。
        """
        driver = self.browser.driver
        deadline = time.time() + max_wait_sec
        saw_dialog = False
        while time.time() < deadline:
            if not self._is_oms_duplicate_selection_info_dialog_visible():
                if saw_dialog:
                    return True
                time.sleep(0.35)
                continue
            saw_dialog = True

            clicked = False
            try:
                layers = driver.find_elements(
                    By.CSS_SELECTOR,
                    ".layui-layer-dialog, .layui-layer.layui-layer-dialog",
                )
                for layer in reversed(layers):
                    try:
                        if not layer.is_displayed():
                            continue
                    except Exception:
                        continue
                    body = (layer.text or "").replace("\n", " ")
                    if "去除勾选" not in body and "寻源单号" not in body:
                        continue
                    for sel in (
                        "a.layui-layer-btn0",
                        ".layui-layer-btn0",
                        ".layui-layer-btn a",
                        ".layui-layer-btn button",
                    ):
                        for btn in layer.find_elements(By.CSS_SELECTOR, sel):
                            try:
                                if not btn.is_displayed():
                                    continue
                                label = (btn.text or btn.get_attribute("value") or "").strip()
                                if label not in ("确定", "确认", "OK") and "确定" not in label:
                                    continue
                                driver.execute_script(
                                    "arguments[0].scrollIntoView({block:'center'});", btn
                                )
                                time.sleep(0.15)
                                ActionChains(driver).move_to_element(btn).pause(0.1).click().perform()
                                clicked = True
                                logger.info("  ✓ 已点击信息弹窗「确定」(Selenium)")
                                break
                            except Exception:
                                continue
                        if clicked:
                            break
                    if clicked:
                        break
            except Exception as e:
                logger.debug(f"  Selenium 点确定失败: {e}")

            if not clicked:
                js_ok = driver.execute_script("""
                    function vis(el) {
                        if (!el) return false;
                        var r = el.getBoundingClientRect();
                        return r.width > 8 && r.height > 8;
                    }
                    function isTarget(txt) {
                        return txt.indexOf('去除勾选') >= 0
                            || (txt.indexOf('寻源单号') >= 0 && txt.indexOf('Item') >= 0);
                    }
                    var layers = document.querySelectorAll('.layui-layer-dialog, .layui-layer');
                    for (var i = layers.length - 1; i >= 0; i--) {
                        var layer = layers[i];
                        if (!vis(layer)) continue;
                        var txt = layer.innerText || '';
                        if (!isTarget(txt)) continue;
                        var btn = layer.querySelector(
                            'a.layui-layer-btn0, .layui-layer-btn0, .layui-layer-btn a'
                        );
                        if (!btn || !vis(btn)) continue;
                        try { btn.scrollIntoView({block:'center'}); } catch (e) {}
                        btn.click();
                        return true;
                    }
                    return false;
                """)
                if js_ok:
                    logger.info("  ✓ 已点击信息弹窗「确定」(JS)")
                    clicked = True

            if not clicked:
                label = self._click_oms_confirm_dialog(max_wait_sec=1, poll_interval=0.2)
                if label:
                    logger.info(f"  ✓ 已点击信息弹窗「{label}」")
                    clicked = True

            time.sleep(0.6)
            if not self._is_oms_duplicate_selection_info_dialog_visible():
                return True
            time.sleep(0.35)

        if saw_dialog:
            if not self._is_oms_duplicate_selection_info_dialog_visible():
                return True
            logger.warning("  信息弹窗仍在，未能点到「确定」按钮")
            return False
        return False

    def _selenium_click_row_checkbox_once(self, cb_el):
        """仅点击一次行复选框；已勾选则不再点（避免 toggle 取消或重复计数）。"""
        driver = self.browser.driver
        if driver.execute_script("return arguments[0].checked;", cb_el):
            return True
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", cb_el
        )
        time.sleep(0.2)
        ActionChains(driver).move_to_element(cb_el).click().perform()
        time.sleep(0.2)
        if driver.execute_script("return arguments[0].checked;", cb_el):
            return True
        try:
            driver.execute_script("arguments[0].click();", cb_el)
            time.sleep(0.15)
        except Exception:
            pass
        return bool(driver.execute_script("return arguments[0].checked;", cb_el))

    def _collect_oms_row_checkbox_targets(
        self,
        match_text,
        sourcing_no="",
        pending_only=False,
        max_rows=None,
        all_visible_filtered=False,
        match_needles=None,
    ):
        needles = [str(n).strip() for n in (match_needles or []) if str(n).strip()]
        raw = self.browser.driver.execute_script(
            self._OMS_SELECTION_JS
            + """
            var projectNeedle = arguments[0] || '';
            var sourcingNeedle = arguments[1] || '';
            var pendingOnly = arguments[2];
            var maxRows = arguments[3];
            var allVisible = arguments[4];
            var extraNeedles = arguments[5] || [];
            var minNeedleLen = arguments[6] || 4;
            var projKey = norm(projectNeedle);
            if (projKey.length > 40) projKey = projKey.slice(0, 40);
            var srcKey = norm(sourcingNeedle);
            var targets = [];
            var seenAid = {};
            var seenPair = {};
            var primary = findPrimaryOmsTable();
            var bestTable = primary.table;
            if (!bestTable) return JSON.stringify({targets: [], diag: primary});
            var root = bestTable.closest('.dataTables_scrollBody') || bestTable;
            var trs = root.querySelectorAll('tbody tr');
            for (var r = 0; r < trs.length; r++) {
                if (maxRows > 0 && targets.length >= maxRows) break;
                var tr = trs[r];
                if (!isVisibleDataRow(tr)) continue;
                var rowText = tr.textContent || '';
                if (!allVisible) {
                    var rowNorm = norm(rowText);
                    if (extraNeedles.length > 0) {
                        var hit = false;
                        for (var ni = 0; ni < extraNeedles.length; ni++) {
                            var nk = norm(extraNeedles[ni]);
                            if (nk.length >= minNeedleLen && rowNorm.indexOf(nk) >= 0) {
                                hit = true;
                                break;
                            }
                        }
                        if (!hit) continue;
                    } else {
                        if (projKey && rowNorm.indexOf(projKey) < 0) continue;
                        if (srcKey && rowNorm.indexOf(srcKey) < 0) continue;
                    }
                }
                if (pendingOnly && rowText.indexOf('待处理') < 0) continue;
                var cb = tr.querySelector('input[name="aid"]');
                if (!cb) {
                    var allCbs = tr.querySelectorAll('input[type="checkbox"]');
                    for (var c = 0; c < allCbs.length; c++) {
                        if ((allCbs[c].className || '').indexOf('allSel') === -1) {
                            cb = allCbs[c];
                            break;
                        }
                    }
                }
                if (!cb || !isVisibleEl(cb)) continue;
                var aid = cb.value || ('row_' + r);
                if (seenAid[aid]) continue;
                var pairKey = extractSourcingItemKey(rowText) || aid;
                if (!allVisible && seenPair[pairKey]) continue;
                seenAid[aid] = true;
                if (!allVisible) seenPair[pairKey] = true;
                var marker = '__oms_chk_' + targets.length;
                cb.setAttribute('data-oms-marker', marker);
                targets.push({
                    marker: marker,
                    aidValue: aid,
                    pairKey: pairKey,
                    rowPreview: (rowText || '').replace(/\\s+/g, ' ').trim().slice(0, 60)
                });
            }
            return JSON.stringify({
                targets: targets,
                visibleRows: primary.visibleRows,
                skippedDupPair: seenPair
            });
        """,
            match_text or "",
            sourcing_no or "",
            bool(pending_only),
            int(max_rows) if max_rows else 0,
            bool(all_visible_filtered),
            needles,
            int(OMS_GROUP_MATCH_MIN_LEN),
        )
        data = json.loads(raw)
        targets = data.get("targets", [])
        if targets:
            logger.info(
                f"  主表可见 {data.get('visibleRows', '?')} 行，"
                f"待勾选 {len(targets)} 行（已排除克隆表/隐藏行"
                f"{'' if all_visible_filtered else '/重复寻源单号+Item'}）"
            )
            for i, t in enumerate(targets[:5]):
                logger.info(
                    f"    [{i}] aid={t.get('aidValue')} "
                    f"pair={t.get('pairKey')} …{t.get('rowPreview', '')}"
                )
        return targets

    def _check_rows_selenium(
        self,
        match_text,
        sourcing_no="",
        pending_only=False,
        max_rows=None,
        clear_first=True,
        all_visible_filtered=False,
        match_needles=None,
    ):
        """
        在 OMS 主表中勾选匹配行。每行只点一次复选框；可先清空再选。
        all_visible_filtered=True：筛完后勾选主表当前可见的全部数据行（扩展供应商用）。
        """
        if (
            not all_visible_filtered
            and not (match_text or "").strip()
            and not (sourcing_no or "").strip()
        ):
            return 0
        if clear_first:
            self._uncheck_all_oms_row_checkboxes()
            self._log_oms_selection_state("清空后")
        targets = self._collect_oms_row_checkbox_targets(
            match_text,
            sourcing_no=sourcing_no,
            pending_only=pending_only,
            max_rows=max_rows,
            all_visible_filtered=all_visible_filtered,
            match_needles=match_needles,
        )
        if not targets:
            if all_visible_filtered:
                logger.warning("  筛后主表无可见数据行可勾选")
            else:
                hint = str(match_text)[:30]
                if match_needles:
                    hint = f"关联字段×{len(match_needles)}"
                logger.warning(f"  未在表格中找到匹配行（{hint}…）")
            return 0
        limit_note = f"，最多 {max_rows} 行" if max_rows else ""
        scope = "筛后列表全部可见行" if all_visible_filtered else "匹配行"
        logger.info(
            f"  待勾选 {len(targets)} 行{limit_note}（{scope}，每行仅点一次复选框）"
        )
        checked = 0
        for sel_attempt in range(2):
            if sel_attempt > 0:
                logger.warning(
                    "  检测到隐藏重复勾选，全页清除后重新标记并只点主表可见行…"
                )
                self._uncheck_all_oms_row_checkboxes()
                targets = self._collect_oms_row_checkbox_targets(
                    match_text,
                    sourcing_no=sourcing_no,
                    pending_only=pending_only,
                    max_rows=max_rows,
                    all_visible_filtered=all_visible_filtered,
                    match_needles=match_needles,
                )
                if not targets:
                    break
            checked = 0
            for idx, t in enumerate(targets):
                marker = t["marker"]
                aid_val = t.get("aidValue", "?")
                try:
                    cb_el = self.browser.driver.find_element(
                        By.CSS_SELECTOR, f"input[data-oms-marker='{marker}']"
                    )
                    if self._selenium_click_row_checkbox_once(cb_el):
                        logger.info(
                            f"    [{idx}] aid={aid_val} "
                            f"pair={t.get('pairKey', '')}: ✓ 已勾选"
                        )
                        checked += 1
                    else:
                        logger.error(f"    [{idx}] aid={aid_val}: ✗ 勾选失败")
                except Exception as e:
                    logger.error(f"    [{idx}] aid={aid_val}: 异常 {e}")
            dedupe = self.browser.driver.execute_script(
                self._OMS_SELECTION_JS + "return dedupeCheckedAidKeepMarked();"
            ) or {}
            if int(dedupe.get("removed") or 0) > 0:
                logger.warning(
                    f"  已取消 {dedupe.get('removed')} 处重复 aid 勾选（克隆表/隐藏行）"
                )
            global_n = self._count_checked_rows()
            if global_n <= len(targets):
                break

        self.browser.driver.execute_script("""
            document.querySelectorAll('[data-oms-marker]').forEach(function(el) {
                el.removeAttribute('data-oms-marker');
            });
        """)
        self._log_oms_selection_state("勾选完成")
        logger.info(f"  OMS 左侧复选框已勾选: {checked}/{len(targets)} 行")
        return checked

    def _count_checked_rows(self):
        """统计全页 input[name=aid] 已勾选数量（与 OMS 内部计数一致）。"""
        return int(
            self.browser.driver.execute_script(
                self._OMS_SELECTION_JS + "return (countCheckedAid().count || 0);"
            )
            or 0
        )

    def ensure_checked_for_import_reuse_page(
        self, project_name="", sourcing_no="", group_key="", items=None
    ):
        """
        导出 Excel 后同行导入：不重新做「已发送→Sourcing8ID→第三列」筛选，
        沿用当前列表页左侧已勾选的行；仅当勾选丢失时在当前可见表内尝试再勾选。
        """
        checked = self._count_checked_rows()
        if checked > 0:
            logger.info(
                f"  沿用导出 Excel 时的 OMS 列表（已勾选 {checked} 行），"
                f"跳过重新筛选，直接导入"
            )
            return checked
        logger.warning(
            "  导出后左侧勾选已丢失，尝试在当前可见列表重新勾选"
            "（不重新筛状态列）…"
        )
        n = self.check_rows_for_finalize(
            project_name, sourcing_no, group_key, items
        )
        if n > 0:
            logger.info(f"  ✓ 已在当前列表重新勾选 {n} 行")
            return n
        logger.error(
            "  当前页无已勾选行。请确认：仍在导出 Excel 时的「报价数据」页、"
            "列表有数据且左侧已勾选，勿刷新或切换标签后再导入。"
        )
        return 0

    def check_rows_for_finalize(
        self, project_name, sourcing_no="", group_key="", items=None
    ):
        """
        阶段三勾选：测试模式按寻源单号文本匹配；
        正式模式与阶段一发邮件一致——列漏斗筛完后勾选全部可见数据行。
        """
        if FINALIZE_FILTER_BY_SOURCING_NO:
            if sourcing_no:
                return self._check_rows_selenium(sourcing_no)
            return 0
        filt = self._resolve_finalize_list_filter(
            project_name, sourcing_no, group_key, items
        )
        if not filt:
            logger.warning(
                "  无法确定勾选范围（项目名/寻源/地址/梯号/系统询价号均无单一值）"
            )
            return 0
        col, _val = filt
        n = self._check_rows_selenium(
            "",
            clear_first=True,
            all_visible_filtered=True,
        )
        if n > 0:
            logger.info(f"  ✓ 已勾选筛后可见行 {n} 行（列「{col}」）")
        else:
            logger.warning(f"  筛后未找到可勾选行（列「{col}」）")
        return n

    def _click_toolbar_button(self, *labels):
        """点击工具栏上包含任一 label 的按钮。"""
        return bool(self.browser.driver.execute_script("""
            var labels = arguments[0];
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var nodes = document.querySelectorAll('button, a, input[type=button], span');
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (!isVis(el)) continue;
                var tx = norm(el.innerText || el.value || '');
                for (var j = 0; j < labels.length; j++) {
                    if (tx.indexOf(norm(labels[j])) >= 0) {
                        try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                        el.click();
                        return true;
                    }
                }
            }
            return false;
        """, list(labels)))

    def _click_menu_item_text(self, *texts, exact=False):
        """点击下拉/菜单项（可见）。exact=True 时要求文本完全匹配（避免误点「导出EXCEL」）。"""
        return bool(self.browser.driver.execute_script("""
            var texts = arguments[0];
            var exact = arguments[1];
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function raw(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var nodes = document.querySelectorAll('a, li, button, span, div');
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (!isVis(el)) continue;
                var tx = norm(el.innerText || el.textContent || '');
                var txRaw = raw(el.innerText || el.textContent || '');
                for (var j = 0; j < texts.length; j++) {
                    var want = norm(texts[j]);
                    var wantRaw = raw(texts[j]);
                    var hit = exact
                        ? (txRaw === wantRaw || tx === want)
                        : (want && tx.indexOf(want) >= 0);
                    if (hit) {
                        try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                        el.click();
                        return true;
                    }
                }
            }
            return false;
        """, list(texts), exact))

    def _click_dropdown_menu_path(self, toolbar_label, menu_path):
        """
        点击工具栏 dropdown-toggle（如「供应商报价表」），再依次点下拉菜单项。
        menu_path: ['导出', '按勾选记录导出'] 或 ['导出EXCEL']
        """
        opened = self.browser.driver.execute_script("""
            var label = arguments[0];
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var nodes = document.querySelectorAll('button, a');
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (!isVis(el)) continue;
                var tx = norm(el.innerText || el.value || '');
                if (tx.indexOf(norm(label)) < 0) continue;
                try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                el.click();
                return true;
            }
            return false;
        """, toolbar_label)
        if not opened:
            return False
        time.sleep(0.8)
        for step, item_text in enumerate(menu_path):
            is_last = step == len(menu_path) - 1
            if not self._click_menu_in_open_dropdown(item_text, exact=is_last):
                if not self._click_menu_item_text(item_text, exact=is_last):
                    return False
            time.sleep(0.6)
        return True

    def _click_menu_in_open_dropdown(self, text, exact=False):
        """仅在可见的 .dropdown-menu 内点击菜单项。"""
        hit = self.browser.driver.execute_script("""
            var want = (arguments[0] || '').replace(/\\s+/g, '');
            var exact = arguments[1];
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function raw(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var menus = document.querySelectorAll(
                '.dropdown-menu, ul[role="menu"], div[role="menu"]'
            );
            for (var mi = 0; mi < menus.length; mi++) {
                var menu = menus[mi];
                if (!isVis(menu)) continue;
                var items = menu.querySelectorAll('a, li, button, span');
                for (var j = 0; j < items.length; j++) {
                    var el = items[j];
                    if (!isVis(el)) continue;
                    var tx = norm(el.innerText || el.textContent || '');
                    var txRaw = raw(el.innerText || el.textContent || '');
                    var hit = exact
                        ? (tx === want || txRaw === arguments[0])
                        : (tx === want || tx.indexOf(want) >= 0);
                    if (hit) {
                        el.click();
                        return txRaw || tx;
                    }
                }
            }
            return '';
        """, text, exact)
        if hit:
            logger.info(f"  ✓ 下拉菜单已点击: {text} (匹配={hit})")
            return True
        return False

    # 供应商报价表下拉中唯一允许的导出项（勿改用导出EXCEL / 按搜索结果导出等）
    EXPORT_BY_CHECKED_MENU_TEXT = "导出-按勾选记录导出"

    def _ensure_oms_download_dir(self):
        """声明 OMS 页下载目录（与 Browser.start 中 prefs 一致）。"""
        dl = getattr(self.browser, "download_dir", "") or ""
        if not dl:
            return
        dl = os.path.abspath(dl)
        os.makedirs(dl, exist_ok=True)
        for cmd, params in (
            ("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": dl}),
            ("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": dl,
                "eventsEnabled": True,
            }),
        ):
            try:
                self.browser.driver.execute_cdp_cmd(cmd, params)
                return
            except Exception:
                continue

    def _click_oms_confirm_dialog(self, max_wait_sec=20, poll_interval=0.5):
        """导出/邮件等操作后常见的「确定」确认弹窗。"""
        driver = self.browser.driver
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            time.sleep(poll_interval)
            clicked = driver.execute_script("""
                var labels = ['确定', '确认', 'OK', '是'];
                function raw(el) {
                    return (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
                }
                function isVis(el) {
                    if (!el) return false;
                    try {
                        var r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) return false;
                    } catch (e) {}
                    if (el.checkVisibility) {
                        try { return el.checkVisibility(); } catch (e) {}
                    }
                    return el.offsetParent !== null;
                }
                var modals = document.querySelectorAll(
                    '.layui-layer-dialog, .layui-layer-page, .layui-layer, '
                    + '.modal, .bootbox, [role="dialog"], [class*="modal"]'
                );
                for (var mi = modals.length - 1; mi >= 0; mi--) {
                    if (!isVis(modals[mi])) continue;
                    var btns = modals[mi].querySelectorAll(
                        '.layui-layer-btn0, .layui-layer-btn1, .layui-layer-btn a, '
                        + 'button, a, input[type=button], input[type=submit]'
                    );
                    for (var j = 0; j < btns.length; j++) {
                        var tx = raw(btns[j]);
                        for (var k = 0; k < labels.length; k++) {
                            if (tx === labels[k]) {
                                btns[j].click();
                                return tx;
                            }
                        }
                    }
                }
                return '';
            """)
            if clicked:
                return clicked
            for label in ("确定", "确认", "OK"):
                try:
                    for el in driver.find_elements(
                        By.XPATH,
                        f"//button[normalize-space()='{label}'] | //a[normalize-space()='{label}']",
                    ):
                        if not el.is_displayed():
                            continue
                        ActionChains(driver).move_to_element(el).click().perform()
                        return label
                except Exception:
                    continue
        return None

    def _click_supplier_quote_export_by_checked(self):
        """
        供应商报价表 → Selenium 真实点击「导出-按勾选记录导出」。
        当前 OMS 点击后直接触发浏览器下载，不再弹出「确定」确认框。
        """
        menu_text = self.EXPORT_BY_CHECKED_MENU_TEXT
        driver = self.browser.driver
        self._ensure_oms_download_dir()

        btn_xpath = "//button[contains(normalize-space(.),'供应商报价表')]"
        try:
            btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, btn_xpath))
            )
        except TimeoutException:
            logger.warning("  未找到「供应商报价表」按钮")
            return False

        ActionChains(driver).move_to_element(btn).click().perform()
        time.sleep(0.7)

        link = None
        link_xpaths = [
            f"//div[contains(@class,'btn-group') and contains(@class,'open')]"
            f"//a[normalize-space()='{menu_text}']",
            f"//ul[contains(@class,'dropdown-menu')]"
            f"//a[normalize-space()='{menu_text}']",
            f"//a[normalize-space()='{menu_text}']",
        ]
        for xp in link_xpaths:
            try:
                link = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, xp))
                )
                if link and link.is_displayed():
                    break
                link = None
            except TimeoutException:
                continue

        if not link:
            for el in driver.find_elements(By.XPATH, f"//a[normalize-space()='{menu_text}']"):
                if el.is_displayed():
                    link = el
                    break

        if not link:
            logger.warning(f"  下拉菜单中未找到可见链接「{menu_text}」")
            return False

        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", link
        )
        time.sleep(0.25)
        ActionChains(driver).move_to_element(link).pause(0.15).click().perform()
        logger.info(f"  ✓ 已点击「{menu_text}」（直接下载，无需确认弹窗）")

        # 兼容旧版 OMS：若仍有确认框，短时轮询后点击「确定」
        confirm = self._click_oms_confirm_dialog(max_wait_sec=3, poll_interval=0.35)
        if confirm:
            logger.info(f"  ✓ 已点击导出确认弹窗「{confirm}」")
        return True

    def export_supplier_quote_excel(self):
        """
        供应商报价表 → 「导出-按勾选记录导出」（仅此一项，不点其他导出菜单）。
        须已勾选左侧复选框。
        """
        menu_text = self.EXPORT_BY_CHECKED_MENU_TEXT
        logger.info(f"  OMS: 供应商报价表 → {menu_text} …")
        checked = self._count_checked_rows()
        if checked <= 0:
            raise RuntimeError(
                "导出前未检测到已勾选的行：请确认左侧复选框已勾选后再点导出"
            )
        logger.info(f"  ✓ 当前已勾选 {checked} 行，开始导出…")

        if not self._click_supplier_quote_export_by_checked():
            self._log_visible_toolbar_buttons()
            self._log_visible_dropdown_menu_items()
            raise RuntimeError(
                f"未能真实点击「{menu_text}」"
                f"（路径：供应商报价表 → {menu_text}）"
            )

        logger.info(
            "  已触发导出，等待 Excel 下载（OMS 后台生成常需 30-90 秒，请耐心等待）…"
        )
        return True

    def _log_visible_dropdown_menu_items(self):
        try:
            items = self.browser.driver.execute_script("""
                var out = [];
                function raw(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
                document.querySelectorAll('.dropdown-menu a, .dropdown-menu li').forEach(function(el) {
                    if (el.offsetParent === null) return;
                    var tx = raw(el.innerText || el.textContent || '');
                    if (tx) out.push(tx);
                });
                return JSON.stringify(out);
            """)
            logger.warning(f"  当前可见下拉菜单项: {items}")
        except Exception:
            pass

    def _log_visible_toolbar_buttons(self):
        try:
            btns = self.browser.driver.execute_script("""
                var out = [];
                document.querySelectorAll('button, a').forEach(function(el) {
                    var txt = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (txt && el.offsetParent !== null && txt.length < 40) {
                        out.push(txt);
                    }
                });
                return JSON.stringify(out.slice(0, 25));
            """)
            logger.warning(f"  当前可见工具栏按钮(前25): {btns}")
        except Exception:
            pass

    # 下拉菜单项仅「导入」两字；弹窗标题为「导入供应商报价表」
    IMPORT_MENU_ITEM = "导入"

    def _import_dialog_present_js(self):
        """检测导入弹窗是否可见（含 iframe 内文案）。"""
        return bool(self.browser.driver.execute_script("""
            function layerVisible(el) {
                if (!el) return false;
                var st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                var r = el.getBoundingClientRect();
                return r.width > 20 && r.height > 20;
            }
            function textHasImport(t) {
                t = t || '';
                return t.indexOf('上传议价附件') >= 0
                    || t.indexOf('导入供应商报价表') >= 0;
            }
            function scanRoot(root) {
                if (!root) return false;
                if (textHasImport(root.innerText || root.textContent || '')) return true;
                var iframes = root.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    try {
                        var doc = iframes[i].contentDocument;
                        if (doc && textHasImport(doc.body ? doc.body.innerText : '')) return true;
                    } catch (e) {}
                }
                return false;
            }
            var layers = document.querySelectorAll('.layui-layer, .modal, [role="dialog"]');
            for (var j = layers.length - 1; j >= 0; j--) {
                if (layerVisible(layers[j]) && scanRoot(layers[j])) return true;
            }
            return scanRoot(document.body);
        """))

    def _wait_import_dialog(self, timeout=15):
        """等待导入弹窗出现。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._import_dialog_present_js():
                return True
            time.sleep(0.4)
        return False

    def _switch_to_import_dialog_frame(self):
        """若导入表单在 iframe 内，切入该 frame。"""
        driver = self.browser.driver
        driver.switch_to.default_content()
        for iframe in driver.find_elements(By.CSS_SELECTOR, "iframe"):
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(iframe)
                if self.browser.driver.execute_script("""
                    var t = document.body ? (document.body.innerText || '') : '';
                    return t.indexOf('上传议价附件') >= 0
                        || t.indexOf('导入供应商报价表') >= 0;
                """):
                    return True
            except Exception:
                pass
        driver.switch_to.default_content()
        return False

    def _open_import_supplier_quote_dialog(self):
        """
        打开导入弹窗（标题「导入供应商报价表」）。
        菜单路径：供应商报价表 ▼ → 导入（仅两字，精确匹配）。
        """
        menu_text = self.IMPORT_MENU_ITEM
        driver = self.browser.driver

        if self._click_dropdown_menu_path("供应商报价表", [menu_text]):
            time.sleep(1)
            if self._wait_import_dialog(10):
                logger.info(
                    f"  ✓ 已打开导入弹窗（供应商报价表 → {menu_text}）"
                )
                return True

        btn_xpath = (
            f"//button[contains(normalize-space(.),'供应商报价表')]"
        )
        try:
            btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.XPATH, btn_xpath))
            )
            ActionChains(driver).move_to_element(btn).click().perform()
            time.sleep(0.7)
            link_xpaths = [
                "//div[contains(@class,'btn-group') and contains(@class,'open')]"
                f"//a[normalize-space()='{menu_text}']",
                "//ul[contains(@class,'dropdown-menu')]"
                f"//a[normalize-space()='{menu_text}']",
            ]
            link = None
            for xp in link_xpaths:
                try:
                    link = WebDriverWait(driver, 4).until(
                        EC.element_to_be_clickable((By.XPATH, xp))
                    )
                    if link and link.is_displayed():
                        break
                    link = None
                except TimeoutException:
                    continue
            if not link:
                for el in driver.find_elements(
                    By.XPATH,
                    "//ul[contains(@class,'dropdown-menu')]"
                    f"//a[normalize-space()='{menu_text}']",
                ):
                    if el.is_displayed():
                        link = el
                        break
            if link:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", link
                )
                time.sleep(0.2)
                ActionChains(driver).move_to_element(link).pause(0.1).click().perform()
                time.sleep(1)
                if self._wait_import_dialog(10):
                    logger.info(
                        f"  ✓ 已打开导入弹窗（Selenium：供应商报价表 → {menu_text}）"
                    )
                    return True
        except TimeoutException:
            pass

        self._log_visible_dropdown_menu_items()
        self._log_visible_toolbar_buttons()
        return False

    def _mark_import_file_inputs(self):
        """
        在导入弹窗（含 iframe）内标记两个 file 输入：
        上传议价附件 → PDF；上传供应商报价表 → Excel。
        隐藏 input 也可 send_keys，无需先点绿色文件夹按钮。
        """
        driver = self.browser.driver
        driver.switch_to.default_content()

        def _run_mark():
            return driver.execute_script("""
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function layerVisible(el) {
                if (!el) return false;
                var st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                var r = el.getBoundingClientRect();
                return r.width > 20 && r.height > 20;
            }
            function findDialogRoot(doc) {
                var layers = doc.querySelectorAll('.layui-layer, .modal, [role="dialog"]');
                for (var i = layers.length - 1; i >= 0; i--) {
                    var el = layers[i];
                    if (!layerVisible(el)) continue;
                    var t = el.innerText || el.textContent || '';
                    if (t.indexOf('导入供应商报价表') >= 0
                        || t.indexOf('上传议价附件') >= 0) {
                        return el;
                    }
                }
                if ((doc.body.innerText || '').indexOf('上传议价附件') >= 0) {
                    return doc.body;
                }
                return null;
            }
            function collectInputs(root) {
                var list = [];
                if (!root) return list;
                root.querySelectorAll('input[type="file"]').forEach(function(inp) {
                    list.push(inp);
                });
                return list;
            }
            function pickByLabel(root) {
                var pdfInput = null, xlsxInput = null;
                var nodes = root.querySelectorAll(
                    'tr, .form-group, .layui-form-item, div, label, p, td'
                );
                for (var i = 0; i < nodes.length; i++) {
                    var row = nodes[i];
                    var label = norm(row.innerText || row.textContent || '');
                    var inp = row.querySelector('input[type=file]');
                    if (!inp) {
                        var parent = row.parentElement;
                        if (parent) inp = parent.querySelector('input[type=file]');
                    }
                    if (!inp) continue;
                    if (label.indexOf('议价附件') >= 0) pdfInput = inp;
                    if (label.indexOf('供应商报价表') >= 0 && label.indexOf('议价') < 0) {
                        xlsxInput = inp;
                    }
                }
                return {pdf: pdfInput, xlsx: xlsxInput};
            }
            var root = findDialogRoot(document);
            if (!root) return {ok: false, reason: 'no_dialog'};
            var picked = pickByLabel(root);
            var pdfInput = picked.pdf;
            var xlsxInput = picked.xlsx;
            var all = collectInputs(root);
            if (all.length < 2) {
                var clickables = root.querySelectorAll(
                    'button, a, .btn, [class*="upload"], i, span'
                );
                for (var ci = 0; ci < clickables.length; ci++) {
                    var btn = clickables[ci];
                    var row = btn.closest('tr, .form-group, .layui-form-item, div');
                    if (!row) continue;
                    var lbl = norm(row.innerText || row.textContent || '');
                    if (lbl.indexOf('议价附件') < 0
                        && lbl.indexOf('供应商报价表') < 0) continue;
                    try { btn.click(); } catch (e) {}
                }
                all = collectInputs(root);
                picked = pickByLabel(root);
                pdfInput = picked.pdf;
                xlsxInput = picked.xlsx;
            }
            if (!pdfInput && all.length > 0) pdfInput = all[0];
            if (!xlsxInput && all.length > 1) xlsxInput = all[1];
            if (!pdfInput || !xlsxInput) {
                return {ok: false, reason: 'inputs', count: all.length};
            }
            pdfInput.setAttribute('data-oms-import', 'pdf');
            xlsxInput.setAttribute('data-oms-import', 'xlsx');
            return {ok: true};
        """)

        in_frame = self._switch_to_import_dialog_frame()
        result = _run_mark()
        if not result or not result.get("ok"):
            driver.switch_to.default_content()
            if self._import_dialog_present_js():
                result = _run_mark()
                in_frame = False
        if not result or not result.get("ok"):
            reason = (result or {}).get("reason", "unknown")
            count = (result or {}).get("count", "?")
            driver.switch_to.default_content()
            raise RuntimeError(
                f"导入弹窗内未找到两个 file 输入框 ({reason}, count={count})"
            )
        return in_frame

    def _find_import_dialog_file_inputs(self):
        """返回 (pdf_input, xlsx_input)，必要时已切入 iframe。"""
        in_frame = self._mark_import_file_inputs()
        driver = self.browser.driver
        pdf_el = driver.find_element(By.CSS_SELECTOR, "input[data-oms-import='pdf']")
        xlsx_el = driver.find_element(By.CSS_SELECTOR, "input[data-oms-import='xlsx']")
        return pdf_el, xlsx_el, in_frame

    def _restore_from_import_frame(self, in_frame):
        if in_frame:
            self.browser.driver.switch_to.default_content()

    def _verify_import_dialog_files(self, pdf_name, xlsx_name):
        """检查弹窗文案是否已出现两个文件名。"""
        return bool(self.browser.driver.execute_script("""
            var pdf = arguments[0], xlsx = arguments[1];
            function isVis(el) {
                if (!el) return false;
                if (el.checkVisibility) return el.checkVisibility();
                return el.offsetParent !== null;
            }
            var nodes = document.querySelectorAll('.layui-layer, .modal, [role="dialog"]');
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (!isVis(el)) continue;
                var t = el.innerText || el.textContent || '';
                if (t.indexOf('上传议价附件') < 0) continue;
                return t.indexOf(pdf) >= 0 && t.indexOf(xlsx) >= 0;
            }
            return false;
        """, pdf_name, xlsx_name))

    @staticmethod
    def _oms_iframe_walk_js(inner_fn_body):
        """在 document 及所有同源 iframe 内执行 inner_fn_body(doc)，返回首个非空结果。"""
        return (
            """
            function walk(doc, depth) {
                if (!doc || depth > 12) return null;
            """
            + inner_fn_body
            + """
                var frames = doc.querySelectorAll('iframe');
                for (var f = 0; f < frames.length; f++) {
                    try {
                        var fd = frames[f].contentDocument
                            || frames[f].contentWindow.document;
                        if (fd) {
                            var sub = walk(fd, depth + 1);
                            if (sub) return sub;
                        }
                    } catch (e) {}
                }
                return null;
            }
            return walk(document, 0);
            """
        )

    _IMPORT_CONFIRM_CLICK_INNER_JS = """
            function raw(el) {
                return (el.innerText || el.textContent || el.value || '')
                    .replace(/\\s+/g, ' ').replace(/\\u00a0/g, ' ').trim();
            }
            function vis(el) {
                if (!el) return false;
                var st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                if (parseFloat(st.opacity || '1') < 0.08) return false;
                var r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4;
            }
            function isImportConfirmLayer(layer) {
                var t = raw(layer);
                if (t.indexOf('上传议价附件') >= 0) return false;
                if (t.indexOf('确认导入') >= 0) return true;
                var title = layer.querySelector('.layui-layer-title');
                var tt = title ? raw(title) : '';
                return tt === '信息' && t.indexOf('确定') >= 0;
            }
            function clickOk(layer) {
                var btn = layer.querySelector(
                    'a.layui-layer-btn0, .layui-layer-btn0'
                );
                if (btn && vis(btn)) {
                    var tx = raw(btn);
                    if (tx.indexOf('取消') < 0) {
                        try { btn.click(); return tx || '确定'; } catch (e) {}
                    }
                }
                var btns = layer.querySelectorAll(
                    '.layui-layer-btn a, .layui-layer-btn0, button, a'
                );
                for (var i = 0; i < btns.length; i++) {
                    if (!vis(btns[i])) continue;
                    var tx2 = raw(btns[i]);
                    if (tx2.indexOf('取消') >= 0) continue;
                    if (tx2 === '确定' || tx2.indexOf('确定') === 0) {
                        try { btns[i].click(); return '确定'; } catch (e2) {}
                    }
                }
                return '';
            }
            function clickConfirmInDoc(doc) {
                var layers = doc.querySelectorAll('.layui-layer');
                var ranked = [];
                for (var j = 0; j < layers.length; j++) {
                    if (!vis(layers[j]) || !isImportConfirmLayer(layers[j])) continue;
                    var z = parseInt(window.getComputedStyle(layers[j]).zIndex, 10) || 0;
                    ranked.push({z: z, el: layers[j]});
                }
                ranked.sort(function(a, b) { return b.z - a.z; });
                for (var k = 0; k < ranked.length; k++) {
                    var hit = clickOk(ranked[k].el);
                    if (hit) return hit;
                }
                return '';
            }
            var hitDoc = clickConfirmInDoc(doc);
            if (hitDoc) return hitDoc;
    """

    def _switch_frame_chain(self, chain):
        driver = self.browser.driver
        driver.switch_to.default_content()
        for iframe in chain:
            driver.switch_to.frame(iframe)

    def _enumerate_frame_chains(self, max_depth=8):
        driver = self.browser.driver
        driver.switch_to.default_content()
        queue = [[]]
        seen = {()}
        while queue:
            chain = queue.pop(0)
            key = tuple(id(f) for f in chain)
            if key in seen:
                continue
            seen.add(key)
            yield chain
            if len(chain) >= max_depth:
                continue
            self._switch_frame_chain(chain)
            try:
                for iframe in driver.find_elements(By.CSS_SELECTOR, "iframe"):
                    try:
                        queue.append(chain + [iframe])
                    except Exception:
                        pass
            except Exception:
                pass
        driver.switch_to.default_content()

    def _selenium_click_import_confirm_in_current_frame(self):
        driver = self.browser.driver
        for layer in reversed(
            driver.find_elements(By.CSS_SELECTOR, ".layui-layer")
        ):
            try:
                body = (layer.text or "").replace("\xa0", " ")
            except Exception:
                continue
            if "上传议价附件" in body:
                continue
            if "确认导入" not in body and "信息" not in body:
                continue
            for sel in ("a.layui-layer-btn0", ".layui-layer-btn0"):
                for btn in layer.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        label = (btn.text or "").replace("\xa0", " ").strip()
                        if not self._btn_label_ok(label):
                            continue
                        driver.execute_script("arguments[0].click();", btn)
                        return label or "确定"
                    except Exception:
                        continue
        return None

    def _try_click_import_confirm_ok_once(self):
        """
        单次尝试：点「信息 — 确认导入供应商报价表?」上的蓝色「确定」。
        须在主文档及所有 OMS iframe 内查找（layui 层常挂在页面 iframe 里）。
        """
        driver = self.browser.driver
        driver.switch_to.default_content()

        clicked = driver.execute_script(
            self._oms_iframe_walk_js(self._IMPORT_CONFIRM_CLICK_INNER_JS)
        )
        if clicked:
            driver.switch_to.default_content()
            return clicked

        for chain in self._enumerate_frame_chains():
            try:
                self._switch_frame_chain(chain)
                hit = self._selenium_click_import_confirm_in_current_frame()
                if hit:
                    driver.switch_to.default_content()
                    return hit
            except Exception:
                continue

        driver.switch_to.default_content()
        ok, label = self._click_confirm_save_dialog_in_context(
            max_wait_sec=0.4, poll_interval=0.08
        )
        if ok:
            return label
        return None

    def _click_post_import_confirm_dialog(self, max_wait_sec=12):
        """保存后立即轮询，点「确认导入供应商报价表?」上的「确定」。"""
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            hit = self._try_click_import_confirm_ok_once()
            if hit:
                return hit
            time.sleep(0.12)
        return None

    def _complete_import_supplier_quote_submit(self, max_wait_sec=12):
        """
        导入流程第二步：点「保存」后点击「确定」确认窗。

        Returns:
            (success: bool, detail: str)
        """
        driver = self.browser.driver
        driver.switch_to.default_content()
        logger.info("  点击确认弹窗「确定」…")
        clicked = self._click_post_import_confirm_dialog(
            max_wait_sec=max_wait_sec
        )
        if not clicked:
            try:
                ss_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "logs",
                    "import_confirm_debug.png",
                )
                driver.save_screenshot(ss_path)
                logger.info(f"  [诊断] 未找到确认弹窗，截图: {ss_path}")
            except Exception:
                pass
            return False, "no_confirm_dialog_after_save"

        logger.info(f"  ✓ 已点击保存后的确认窗「{clicked}」")
        time.sleep(2)
        return True, "confirmed"

    _IMPORT_SAVE_CLICK_JS = """
        function norm(t) {
            return (t || '').replace(/\\s+/g, '').replace(/\\u00a0/g, '').trim();
        }
        function isVis(el) {
            if (!el) return false;
            try {
                var r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return false;
            } catch (e) {}
            if (el.checkVisibility) {
                try { return el.checkVisibility(); } catch (e2) {}
            }
            var st = window.getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return false;
            return el.offsetParent !== null;
        }
        function isSaveBtn(el) {
            if (!el || !isVis(el)) return false;
            var tx = norm(el.innerText || el.value || el.textContent || '');
            if (el.id === 'submit') return true;
            return tx === '保存' || tx === '提交' || tx.indexOf('保存') >= 0;
        }
        function clickSaveInDoc(doc) {
            if (!doc) return '';
            var sub = doc.getElementById('submit');
            if (isSaveBtn(sub)) {
                try { sub.click(); return norm(sub.value || sub.innerText) || '保存'; } catch (e) {}
            }
            var btns = doc.querySelectorAll(
                'input[type=button], input[type=submit], button, a.btn, .btn'
            );
            for (var j = 0; j < btns.length; j++) {
                if (!isSaveBtn(btns[j])) continue;
                try { btns[j].click(); return norm(btns[j].value || btns[j].innerText) || '保存'; } catch (e2) {}
            }
            return '';
        }
        function isImportLayer(layer) {
            var titleEl = layer.querySelector('.layui-layer-title, .modal-title, h4');
            var title = norm(titleEl ? (titleEl.innerText || titleEl.textContent) : '');
            if (title.indexOf('导入供应商报价表') >= 0) return true;
            var t = layer.innerText || layer.textContent || '';
            return t.indexOf('上传议价附件') >= 0
                || t.indexOf('导入供应商报价表') >= 0;
        }
        function clickSaveInLayer(layer) {
            var ifr = layer.querySelector('iframe');
            if (ifr) {
                try {
                    var idoc = ifr.contentDocument || ifr.contentWindow.document;
                    var inIframe = clickSaveInDoc(idoc);
                    if (inIframe) return inIframe;
                } catch (e) {}
            }
            var btnArea = layer.querySelector('.layui-layer-btn') || layer;
            var btns = btnArea.querySelectorAll(
                'a.layui-layer-btn0, .layui-layer-btn0, .layui-layer-btn a, '
                + 'input[type=button], input[type=submit], button, a, .btn'
            );
            for (var k = 0; k < btns.length; k++) {
                if (!isSaveBtn(btns[k])) continue;
                try { btns[k].click(); return norm(btns[k].value || btns[k].innerText) || '保存'; } catch (e3) {}
            }
            return clickSaveInDoc(layer);
        }
        var hitDoc = clickSaveInDoc(document);
        if (hitDoc) return hitDoc;
        var layers = document.querySelectorAll(
            '.layui-layer, .layui-layer-iframe, .modal, [role="dialog"]'
        );
        for (var i = layers.length - 1; i >= 0; i--) {
            if (!isVis(layers[i])) continue;
            if (!isImportLayer(layers[i])) continue;
            var hit = clickSaveInLayer(layers[i]);
            if (hit) return hit;
        }
        return '';
    """

    def _selenium_click_import_save_button(self):
        """Selenium：在导入 iframe / layui 层内点 #submit 或「保存」。"""
        driver = self.browser.driver

        def _try_click(el):
            label = (
                el.get_attribute("value")
                or el.text
                or ""
            ).replace("\xa0", " ").strip()
            if el.get_attribute("id") != "submit" and "保存" not in label and label != "提交":
                return None
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )
            time.sleep(0.12)
            try:
                el.click()
            except Exception:
                ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
            return label or "保存"

        for sel in (
            "#submit",
            "input#submit.btn-primary",
            "input[type=button][id='submit']",
            "input.btn-primary[value*='保存']",
            "input[type=button][value*='保存']",
            "button[type='submit']",
        ):
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    if not el.is_displayed():
                        continue
                    hit = _try_click(el)
                    if hit:
                        return hit
            except Exception:
                continue
        return None

    def _click_import_dialog_save(self, in_frame=False):
        """
        在导入弹窗内点击「保存」。
        OMS 导入窗为 layui iframe 层，保存为 iframe 内 input#submit（非父页底栏）。
        """
        driver = self.browser.driver
        driver.switch_to.default_content()

        if in_frame and self._switch_to_import_dialog_frame():
            hit = self._selenium_click_import_save_button()
            if not hit:
                hit = driver.execute_script(self._IMPORT_SAVE_CLICK_JS) or None
            if hit:
                return hit

        driver.switch_to.default_content()
        for iframe in driver.find_elements(By.CSS_SELECTOR, "iframe"):
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(iframe)
                body = ""
                try:
                    body = driver.find_element(By.TAG_NAME, "body").text
                except Exception:
                    pass
                if (
                    "上传议价附件" not in body
                    and "导入供应商报价表" not in body
                ):
                    continue
                hit = self._selenium_click_import_save_button()
                if not hit:
                    hit = driver.execute_script(self._IMPORT_SAVE_CLICK_JS) or None
                if hit:
                    driver.switch_to.default_content()
                    return hit
            except Exception:
                continue

        driver.switch_to.default_content()
        clicked = driver.execute_script(self._IMPORT_SAVE_CLICK_JS)
        if clicked:
            return clicked

        try:
            layers = driver.find_elements(By.CSS_SELECTOR, ".layui-layer")
            for layer in reversed(layers):
                try:
                    if not layer.is_displayed():
                        continue
                except Exception:
                    continue
                title = ""
                try:
                    title_el = layer.find_element(
                        By.CSS_SELECTOR, ".layui-layer-title"
                    )
                    title = (title_el.text or "").strip()
                except Exception:
                    pass
                body = (layer.text or "").replace("\xa0", " ")
                if (
                    "导入供应商报价表" not in title
                    and "上传议价附件" not in body
                    and "导入供应商报价表" not in body
                ):
                    continue
                for iframe in layer.find_elements(By.CSS_SELECTOR, "iframe"):
                    try:
                        driver.switch_to.default_content()
                        driver.switch_to.frame(iframe)
                        hit = self._selenium_click_import_save_button()
                        driver.switch_to.default_content()
                        if hit:
                            return hit
                    except Exception:
                        driver.switch_to.default_content()
                        continue
                for sel in (
                    "#submit",
                    ".layui-layer-btn a",
                    "a.layui-layer-btn0",
                    "input[type=button]",
                ):
                    for btn in layer.find_elements(By.CSS_SELECTOR, sel):
                        try:
                            if not btn.is_displayed():
                                continue
                            label = (
                                btn.get_attribute("value")
                                or btn.text
                                or ""
                            ).replace("\xa0", " ").strip()
                            if (
                                btn.get_attribute("id") != "submit"
                                and "保存" not in label
                            ):
                                continue
                            driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});",
                                btn,
                            )
                            time.sleep(0.1)
                            try:
                                btn.click()
                            except Exception:
                                ActionChains(driver).move_to_element(
                                    btn
                                ).pause(0.1).click().perform()
                            return label or "保存"
                        except Exception:
                            continue
        except Exception:
            pass

        driver.switch_to.default_content()
        try:
            ss_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "logs",
                "import_save_debug.png",
            )
            driver.save_screenshot(ss_path)
            logger.info(f"  [诊断] 未找到导入「保存」，截图: {ss_path}")
        except Exception:
            pass
        return None

    def import_supplier_quote_files(self, pdf_path, xlsx_path, submit=True):
        """
        供应商报价表 → 导入供应商报价表 → 上传议价附件(PDF) + 供应商报价表(Excel)。
        submit=True：上传后点「保存」→ 弹出确认窗点「确定」。
        submit=False：仅选文件，不点「保存」（测试用）。
        """
        logger.info(
            "  OMS: 打开「导入供应商报价表」并上传 PDF/Excel…"
            + ("" if submit else "（测试：不点保存/确认）")
        )

        abs_pdf = os.path.abspath(pdf_path)
        abs_xlsx = os.path.abspath(xlsx_path)
        if not os.path.isfile(abs_pdf):
            raise FileNotFoundError(abs_pdf)
        if not os.path.isfile(abs_xlsx):
            raise FileNotFoundError(abs_xlsx)

        if not self._open_import_supplier_quote_dialog():
            raise RuntimeError(
                "未能打开导入弹窗"
                f"（请确认：供应商报价表 ▼ → {self.IMPORT_MENU_ITEM}）"
            )

        time.sleep(1)
        in_frame = False
        try:
            pdf_input, xlsx_input, in_frame = self._find_import_dialog_file_inputs()

            for path, inp, label in (
                (abs_pdf, pdf_input, "议价附件(PDF)"),
                (abs_xlsx, xlsx_input, "供应商报价表(Excel)"),
            ):
                try:
                    inp.send_keys(path)
                except Exception:
                    driver = self.browser.driver
                    driver.execute_script(
                        "arguments[0].style.display='block';"
                        "arguments[0].style.visibility='visible';"
                        "arguments[0].style.opacity=1;",
                        inp,
                    )
                    inp.send_keys(path)
                logger.info(f"  ✓ 已选择文件 [{label}]: {os.path.basename(path)}")
                time.sleep(0.8)

            time.sleep(0.5)
            pdf_name = os.path.basename(abs_pdf)
            xlsx_name = os.path.basename(abs_xlsx)
            if self._verify_import_dialog_files(pdf_name, xlsx_name):
                logger.info(f"  ✓ 弹窗已显示文件: PDF={pdf_name}, Excel={xlsx_name}")
            else:
                logger.warning(
                    f"  弹窗文案未检测到文件名（请目视确认两栏已显示）: "
                    f"{pdf_name}, {xlsx_name}"
                )

            if not submit:
                logger.info(
                    "  ✓ 已上传 PDF/Excel（未点「保存」）；请在弹窗核对两个附件后自行关闭"
                )
                return False

            time.sleep(0.6)
            save_btn = self._click_import_dialog_save(in_frame=in_frame)
            if not save_btn:
                raise RuntimeError("导入弹窗中未找到「保存」按钮")
            logger.info(f"  ✓ 已点击导入弹窗「{save_btn}」")
            self._restore_from_import_frame(in_frame)
            in_frame = False
            time.sleep(0.35)

            ok, detail = self._complete_import_supplier_quote_submit(
                max_wait_sec=15
            )
            if not ok:
                raise RuntimeError(
                    f"导入保存后确认/提交未完成 ({detail})；"
                    "请查看 logs/import_confirm_debug.png"
                )
            logger.info(f"  ✓ 导入完成：已保存并确认 ({detail})")
            return True
        finally:
            self._restore_from_import_frame(in_frame)

