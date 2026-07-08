"""Mixin helpers split from modules.spareparts."""

import json
import logging
import os
import random
import re
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import (
    DEFAULT_ELEVATOR_SUPPLIER,
    DEFAULT_FACTORY,
    MAX_WAIT_APPROVAL,
    POLL_INTERVAL,
    SPAREPARTS_URL,
)
from utils.spareparts_photo_upload import (
    PhotoUploadNeedsManualHelp,
    SparepartsPhotoUploader,
    deep_reset_photo_upload_popovers,
    hide_and_remove_body_popovers,
)

logger = logging.getLogger(__name__)


class SparePartsCartMixin:
    def _cart_count_material_rows(self):
        """购物车物料行数（与 listCartMaterialRows / 【诊断】一致）"""
        try:
            rows = self.driver.execute_script(
                self._CART_QTY_JS_CORE + "return listCartMaterialRows();"
            )
            return len(rows) if isinstance(rows, list) else 0
        except Exception:
            return 0

    @staticmethod
    def _cart_resolve_quantity_target(cart_quantity_target, line_quantities=None):
        """汇总目标件数：优先显式 total，否则对 oms_data 逐行 quantity 求和。"""
        if cart_quantity_target is not None:
            try:
                t = int(float(str(cart_quantity_target).strip()))
                if t >= 1:
                    return t
            except (ValueError, TypeError):
                pass
        if line_quantities:
            total = 0
            for q in line_quantities:
                try:
                    total += int(float(str(q).strip()))
                except (ValueError, TypeError):
                    total += 1
            if total >= 1:
                return total
        return None

    @staticmethod
    def _cart_normalize_line_quantities(line_quantities):
        out = []
        for q in line_quantities or []:
            try:
                out.append(int(float(str(q).strip())))
            except (ValueError, TypeError):
                out.append(1)
        return out

    @classmethod
    def _cart_describe_quantity_align_goal(
        cls,
        cart_quantity_target=None,
        line_quantities=None,
        line_material_descriptions=None,
        material_count=None,
    ):
        """生成日志/报错用文案：强调「逐行数量」而非「物料条数」。"""
        line_qty = cls._cart_normalize_line_quantities(line_quantities)
        total = cls._cart_resolve_quantity_target(cart_quantity_target, line_qty)
        n_lines = len(line_qty)
        n_mats = material_count if material_count else n_lines
        if n_lines >= 1:
            parts = [f"共 {n_mats} 条物料，各行 OMS 数量={line_qty}"]
            if total is not None and n_lines > 1:
                parts.append(f"（汇总件数={total}，非单行目标）")
            return "；".join(parts)
        if total is not None:
            return f"单行/汇总目标件数={total}"
        return "未提供 OMS 数量"

    @classmethod
    def _cart_quantity_align_failure_message(
        cls,
        cart_quantity_target=None,
        line_quantities=None,
        line_material_descriptions=None,
        material_count=None,
    ):
        goal = cls._cart_describe_quantity_align_goal(
            cart_quantity_target,
            line_quantities,
            line_material_descriptions,
            material_count,
        )
        return (
            f"购物车各物料「数量」未能与 OMS 逐行对齐（{goal}），"
            f"已中止生成报价单（请在购物车页按各行改好数量后点「更新购物车」）"
        )

    def _cart_log_rows_snapshot(self):
        """诊断：打印购物车每行产品描述与数量列读数。"""
        try:
            rows = self.driver.execute_script(
                self._CART_QTY_JS_CORE + "return listCartMaterialRows();"
            )
            if not isinstance(rows, list):
                return
            logger.info(f"  【诊断】购物车主表 {len(rows)} 行物料：")
            for i, r in enumerate(rows):
                extra = ""
                if r.get("qty") is None:
                    extra = (
                        f" 数量列={'有' if r.get('hasQtyCell') else '未找到'}"
                        f" cell=[{r.get('qtyCell', '')}]"
                    )
                logger.info(
                    f"    行{i}: 数量={r.get('qty')} "
                    f"key={r.get('key', '')} | "
                    f"{str(r.get('desc', ''))[:55]}{extra}"
                )
        except Exception as e:
            logger.debug(f"购物车快照失败: {e}")

    def _cart_check_row_count_for_align(
        self, row_count, material_count, line_material_descriptions=None
    ):
        """行数校验：少于 OMS 必拦；多于 OMS 且描述均可匹配时仅警告。"""
        if not material_count or material_count <= 0:
            return
        line_descs = [
            str(d or "").strip()
            for d in (line_material_descriptions or [])
            if str(d or "").strip()
        ]
        if row_count < material_count:
            self._cart_log_rows_snapshot()
            raise RuntimeError(
                f"购物车物料行数({row_count})少于 OMS 物料数({material_count})，"
                f"请先补齐或重新 --resume 加入全部物料。"
            )
        if row_count == material_count:
            return
        all_present = (
            self._cart_all_oms_lines_present(line_descs) if line_descs else False
        )
        if all_present:
            logger.warning(
                f"  购物车行数({row_count})多于 OMS({material_count})，"
                f"但已找齐全部 OMS 物料描述，将仅对齐匹配的 {material_count} 行"
            )
            return
        self._cart_log_rows_snapshot()
        raise RuntimeError(
            f"购物车物料行数({row_count})与 OMS 物料数({material_count})不一致，"
            f"且未能找齐全部 OMS 物料，请清空购物车后重新 --resume。"
        )

    def _cart_all_oms_lines_present(self, line_material_descriptions):
        """购物车是否已包含 OMS 每条物料描述对应的行。"""
        line_desc = [
            str(d or "").strip()
            for d in (line_material_descriptions or [])
            if str(d or "").strip()
        ]
        if not line_desc:
            return False
        try:
            return bool(
                self.driver.execute_script(
                    self._CART_QTY_JS_CORE
                    + """
                    var descs = arguments[0];
                    var used = {};
                    for (var i = 0; i < descs.length; i++) {
                        if (!findCartRowByDesc(descs[i], used)) return false;
                    }
                    return true;
                    """,
                    line_desc,
                )
            )
        except Exception:
            return False

    def _cart_align_qty_by_product_desc(self, line_quantities, line_material_descriptions):
        """
        核心逻辑：OMS 每条物料描述 → 找购物车对应行 → 只改数量列。
        """
        line_qty = self._cart_normalize_line_quantities(line_quantities)
        line_desc = [str(d or "").strip() for d in (line_material_descriptions or [])]
        if not line_qty or not line_desc or len(line_qty) != len(line_desc):
            return {"ok": False, "reason": "bad_args"}
        try:
            res = self.driver.execute_script(
                self._CART_QTY_JS_CORE
                + """
                var lineQty = arguments[0];
                var lineDesc = arguments[1];
                var detail = [];
                var used = {};
                for (var li = 0; li < lineDesc.length; li++) {
                    var want = parseInt(lineQty[li], 10) || 1;
                    var row = findCartRowByDesc(lineDesc[li], used);
                    if (!row) {
                        return {
                            ok: false,
                            reason: 'no_match',
                            line: li,
                            desc: (lineDesc[li] || '').substring(0, 60)
                        };
                    }
                    var adj = adjustQtyOnRow(row, want);
                    detail.push({
                        line: li,
                        desc: (lineDesc[li] || '').substring(0, 60),
                        before: adj.before,
                        want: want,
                        after: adj.after !== undefined ? adj.after : adj.cur,
                        ok: adj.ok,
                        mode: adj.mode || adj.reason || ''
                    });
                    if (!adj.ok) {
                        return {ok: false, reason: adj.reason || 'adjust_fail', line: li, detail: detail};
                    }
                }
                return {ok: true, mode: 'by_product_desc', detail: detail};
                """,
                list(line_qty),
                list(line_desc),
            )
            if isinstance(res, dict) and res.get("ok"):
                for row in res.get("detail") or []:
                    logger.info(
                        f"    [{row.get('line')}] 改前={row.get('before')} → "
                        f"OMS={row.get('want')} 改后={row.get('after')} | "
                        f"{str(row.get('desc', ''))[:50]}"
                    )
            return res if isinstance(res, dict) else {"ok": False, "reason": "bad_result"}
        except Exception as e:
            logger.debug(f"按产品描述对齐数量失败: {e}")
            return {"ok": False, "reason": "exception", "error": str(e)}

    def _cart_verify_qty_by_product_desc(self, line_quantities, line_material_descriptions):
        """回读校验：每条 OMS 描述对应购物车行的数量列是否等于 OMS quantity。"""
        line_qty = self._cart_normalize_line_quantities(line_quantities)
        line_desc = [str(d or "").strip() for d in (line_material_descriptions or [])]
        if not line_qty or not line_desc or len(line_qty) != len(line_desc):
            return False, []
        try:
            res = self.driver.execute_script(
                self._CART_QTY_JS_CORE
                + """
                var lineQty = arguments[0];
                var lineDesc = arguments[1];
                var fails = [];
                var used = {};
                for (var li = 0; li < lineDesc.length; li++) {
                    var want = parseInt(lineQty[li], 10) || 1;
                    var row = findCartRowByDesc(lineDesc[li], used);
                    if (!row) {
                        fails.push({
                            line: li,
                            desc: (lineDesc[li] || '').substring(0, 50),
                            want: want,
                            got: null,
                            reason: 'no_row'
                        });
                        continue;
                    }
                    var got = readQtyFromRow(row);
                    if (got !== want) {
                        fails.push({
                            line: li,
                            desc: (lineDesc[li] || '').substring(0, 50),
                            want: want,
                            got: got,
                            reason: 'qty_mismatch'
                        });
                    }
                }
                return {ok: fails.length === 0, fails: fails};
                """,
                list(line_qty),
                list(line_desc),
            )
            if isinstance(res, dict) and res.get("ok"):
                logger.info(
                    f"  ✓ 购物车逐行数量回读校验通过: {list(line_qty)}"
                )
                return True, []
            fails = (res or {}).get("fails") or []
            self._last_cart_qty_fails = fails
            for f in fails:
                logger.warning(
                    f"  行{f.get('line')}: OMS={f.get('want')} 购物车={f.get('got')} "
                    f"({f.get('reason')}) | {f.get('desc', '')[:40]}"
                )
            if not fails:
                logger.warning(
                    f"  购物车逐行数量回读不一致，期望各行={list(line_qty)}"
                )
            return False, fails
        except Exception:
            return False, []

    @staticmethod
    def resolve_cart_quantity_target_for_align(cart_quantity_target, line_quantities=None):
        """多行 OMS 逐条对齐时不传汇总 target，避免兜底 JS 把总件数当单行目标。"""
        line_qty = SparePartsCartMixin._cart_normalize_line_quantities(line_quantities or [])
        if len(line_qty) > 1:
            return None
        return SparePartsCartMixin._cart_resolve_quantity_target(
            cart_quantity_target, line_qty or None
        )

    def _cart_align_per_line_with_oms(self, line_arg, desc_arg):
        """多行：仅 _CART_QTY_JS_CORE 按描述对齐，失败则 Selenium 兜底（不走旧版列索引 JS）。"""
        logger.info(
            f"  购物车逐行对齐 OMS quantity: {line_arg}（{len(line_arg)} 条物料）"
        )
        simple = self._cart_align_qty_by_product_desc(line_arg, desc_arg)
        if isinstance(simple, dict) and simple.get("ok"):
            self._cart_click_update_cart_if_present()
            time.sleep(2.0)
            self._cart_close_update_success_dialog_if_present()
            ok, _ = self._cart_verify_qty_by_product_desc(line_arg, desc_arg)
            if ok:
                return True
            logger.warning("  按产品描述改数量后回读仍不一致")
        elif isinstance(simple, dict):
            logger.warning(f"  按产品描述对齐数量: {simple}")
        self._cart_log_rows_snapshot()
        fb = self._cart_force_line_quantity_selenium_fallback(
            None,
            material_hint=desc_arg[0] if desc_arg else None,
            line_quantities=line_arg,
            line_material_descriptions=desc_arg,
        )
        if fb:
            self._cart_click_update_cart_if_present()
            time.sleep(2.0)
            self._cart_close_update_success_dialog_if_present()
            ok, _ = self._cart_verify_qty_by_product_desc(line_arg, desc_arg)
            return ok
        return False

    def _cart_align_line_quantities_with_oms(
        self,
        cart_quantity_target,
        material_count=None,
        line_quantities=None,
        line_material_descriptions=None,
    ):
        """
        购物车数量与 OMS 对齐：
        1) 用表头定位「数量」列，只在该单元格（或行内）读数、点加减/三角、写 input；
        2) 用「产品描述」列文案与 OMS 物料描述做子串匹配，把 OMS 每行数量挂到正确购物车行；
        3) 无描述或未匹配满时，回退为与 line_quantities 同序或按总件数均分。
        """
        line_arg = line_quantities if line_quantities else None
        desc_arg = line_material_descriptions if line_material_descriptions else None
        use_per_line = bool(
            line_arg
            and desc_arg
            and len(line_arg) == len(desc_arg)
            and len(line_arg) >= 1
        )
        if use_per_line:
            return self._cart_align_per_line_with_oms(line_arg, desc_arg)

        target = self._cart_resolve_quantity_target(cart_quantity_target, line_arg)
        if not target:
            logger.info(
                "  未提供 OMS 数量（汇总或 oms_data 逐行 quantity），跳过购物车数量对齐"
            )
            return True
        if line_arg:
            logger.info(f"  购物车对齐 OMS 逐行数量: {line_arg}")
        res = self.driver.execute_script(
            """
            var target = parseInt(arguments[0], 10) || 0;
            var lineQty = arguments[1];
            var lineDesc = arguments[2];
            if (!Array.isArray(lineQty)) lineQty = null;
            if (!Array.isArray(lineDesc)) lineDesc = null;
            if (target < 1) return {ok: false, reason: 'bad_target'};
            var QTY_INP_SEL = 'input[type=number], input[type=text], input[type=tel], input[name*="Qty"], input[name*="qty"], '
                + 'input[id*="Qty"], input[id*="qty"], input[id*="amount"], input[id*="Amount"]';
            var BROAD_INP_SEL = 'input:not([type=checkbox]):not([type=hidden]):not([type=radio]):not([type=file])'
                + ':not([type=button]):not([type=submit]):not([type=reset]):not([type=image])';
            function norm(s) {
                return (s || '').replace(/\\s+/g, '').toLowerCase();
            }
            function cellHasQtySpinner(el) {
                if (!el || el.nodeType !== 1) return false;
                if (el.querySelector('input[type=number], input[type=text], input[type=tel]')) return true;
                var n = 0;
                var bs = el.querySelectorAll('button, a, span, i');
                for (var bi = 0; bi < bs.length; bi++) {
                    var r = bs[bi].getBoundingClientRect();
                    if (r.width > 0 && r.width < 100 && r.height > 0 && r.height < 100) n++;
                }
                return n >= 2;
            }
            function rowHasPriceMarker(el) {
                if (!el) return false;
                if (cellHasQtySpinner(el)) return false;
                var tx = el.innerText || el.textContent || '';
                return tx.indexOf('￥') >= 0 || tx.indexOf('¥') >= 0 || /\\d{1,3},\\d{3}/.test(tx);
            }
            function parseQtyFromText(tx) {
                if (!tx) return null;
                var s = (tx || '').replace(/\\s+/g, '');
                if (!s) return null;
                if (s.indexOf('￥') >= 0 || s.indexOf('¥') >= 0) {
                    s = s.split(/￥|¥/)[0];
                }
                if (/^\\d{1,4}$/.test(s)) return parseInt(s, 10);
                var best = null;
                var re = /(?:^|[^\\d.,])(\\d{1,4})(?![\\d])/g;
                var m;
                while ((m = re.exec(s)) !== null) {
                    var n = parseInt(m[1], 10);
                    if (n >= 1 && n <= 9999) best = n;
                }
                return best;
            }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function findCartTable() {
                var tables = document.querySelectorAll('table');
                var best = null, bestScore = -1;
                for (var ti = 0; ti < tables.length; ti++) {
                    var tbl = tables[ti];
                    if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                    var score = 0;
                    var rows = tbl.querySelectorAll('tbody tr');
                    if (!rows.length) rows = tbl.querySelectorAll('tr');
                    for (var ri = 0; ri < rows.length; ri++) {
                        var row = rows[ri];
                        if (row.querySelector('th')) continue;
                        var tx = (row.innerText || '').replace(/\\s+/g, '');
                        if (tx.length < 8) continue;
                        if (/[A-Za-z\\u4e00-\\u9fff]/.test(tx)) score += 2;
                        if (row.querySelector(QTY_INP_SEL) || row.querySelector(BROAD_INP_SEL)) score += 3;
                    }
                    if (score > bestScore) { bestScore = score; best = tbl; }
                }
                return best;
            }
            function getDataRows(tbl) {
                var rows = tbl.querySelectorAll('tbody tr');
                if (!rows.length) rows = tbl.querySelectorAll('tr');
                return rows;
            }
            function inferQtyColFromDataRows(tbl, descIx) {
                var counts = {};
                var rows = getDataRows(tbl);
                for (var ri = 0; ri < rows.length; ri++) {
                    var row = rows[ri];
                    if (row.querySelector('th')) continue;
                    var tx = (row.innerText || '').replace(/\\s+/g, '');
                    if (tx.length < 8) continue;
                    var tds = row.querySelectorAll('td');
                    for (var ci = 0; ci < tds.length; ci++) {
                        if (descIx >= 0 && ci === descIx) continue;
                        var td = tds[ci];
                        if (rowHasPriceMarker(td)) continue;
                        var spinN = countSpinnerClickables(td);
                        if (spinN >= 2) {
                            counts[ci] = (counts[ci] || 0) + 12;
                            continue;
                        }
                        if (td.querySelector(QTY_INP_SEL) && spinN >= 1) {
                            counts[ci] = (counts[ci] || 0) + 4;
                            continue;
                        }
                        var t = (td.textContent || '').replace(/\\s+/g, '');
                        if (/^\\d{1,4}$/.test(t)) counts[ci] = (counts[ci] || 0) + 2;
                    }
                }
                var bestIx = -1, bestN = 0;
                for (var k in counts) {
                    if (counts[k] > bestN) { bestN = counts[k]; bestIx = parseInt(k, 10); }
                }
                return bestIx;
            }
            function findColumnIndexInThead(tbl, kws) {
                function scanCells(cells, exactFirst) {
                    if (exactFirst) {
                        for (var i = 0; i < cells.length; i++) {
                            var t = (cells[i].textContent || '').replace(/\\s+/g, '');
                            for (var k = 0; k < kws.length; k++) {
                                if (t === kws[k]) return i;
                            }
                        }
                    }
                    for (var i = 0; i < cells.length; i++) {
                        var t = (cells[i].textContent || '').replace(/\\s+/g, '');
                        for (var k = 0; k < kws.length; k++) {
                            if (t.indexOf(kws[k]) >= 0) return i;
                        }
                    }
                    return -1;
                }
                var heads = tbl.querySelectorAll('thead tr');
                for (var tr = 0; tr < heads.length; tr++) {
                    var ix = scanCells(heads[tr].querySelectorAll('th, td'), true);
                    if (ix >= 0) return ix;
                }
                var bodyRows = tbl.querySelectorAll('tbody tr');
                for (var br = 0; br < bodyRows.length && br < 3; br++) {
                    if (bodyRows[br].querySelector('th')) {
                        var ix2 = scanCells(bodyRows[br].querySelectorAll('th, td'), true);
                        if (ix2 >= 0) return ix2;
                    }
                }
                return -1;
            }
            function findQtyInputs(qtyRoot) {
                var all = qtyRoot.querySelectorAll(QTY_INP_SEL);
                if (!all.length) all = qtyRoot.querySelectorAll(BROAD_INP_SEL);
                var vis = null, hid = null, any = null;
                for (var qi = 0; qi < all.length; qi++) {
                    var el = all[qi];
                    any = any || el;
                    if (isVis(el)) vis = vis || el;
                    else hid = hid || el;
                }
                return { vis: vis, hid: hid, any: any };
            }
            function readQtyFromSpinnerCell(td) {
                if (!td || rowHasPriceMarker(td)) return null;
                if (countSpinnerClickables(td) < 2 && !findQtyInputs(td).any) return null;
                var f = findQtyInputs(td);
                var inp = f.vis || f.hid || f.any;
                if (inp) {
                    var v = parseInt(inp.value, 10);
                    if (!isNaN(v) && v >= 0 && v <= 999) return v;
                }
                var raw = (td.innerText || td.textContent || '');
                var spinM = raw.match(/<\\s*(\\d{1,4})\\s*>/);
                if (spinM) return parseInt(spinM[1], 10);
                var t = raw.replace(/\\s+/g, '');
                if (/^\\d{1,4}$/.test(t)) return parseInt(t, 10);
                return null;
            }
            function readQtyFromCell(td) {
                return readQtyFromSpinnerCell(td);
            }
            function countSpinnerClickables(td) {
                var n = 0;
                var cand = td.querySelectorAll('button, a, span, div, i, b, strong');
                for (var i = 0; i < cand.length; i++) {
                    if (!isVis(cand[i])) continue;
                    var r = cand[i].getBoundingClientRect();
                    if (r.width > 0 && r.width < 100 && r.height > 0 && r.height < 100) n++;
                }
                return n;
            }
            function findQtyCellInRow(row) {
                if (!row) return null;
                var tds = row.querySelectorAll('td');
                var withSpinner = null, withInp = null;
                for (var ci = 0; ci < tds.length; ci++) {
                    var td = tds[ci];
                    if (rowHasPriceMarker(td)) continue;
                    var inp = findQtyInputs(td).any;
                    var spin = countSpinnerClickables(td) >= 2;
                    if (inp && spin) return td;
                    if (spin && !withSpinner) withSpinner = td;
                    if (inp && !withInp) withInp = td;
                }
                return withSpinner || withInp || null;
            }
            function resolveQtyRoot(qtyRoot, rowOpt) {
                if (rowOpt) {
                    var cell = findQtyCellInRow(rowOpt);
                    if (cell) return cell;
                }
                return qtyRoot;
            }
            function clickSpinnerDelta(td, dir) {
                if (!td) return false;
                var clickables = [];
                var cand = td.querySelectorAll('button, a, span, div, i, b, strong');
                for (var i = 0; i < cand.length; i++) {
                    var el = cand[i];
                    if (!isVis(el)) continue;
                    var r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.width > 100 || r.height > 100) continue;
                    clickables.push({ el: el, left: r.left, tx: (el.textContent || '').trim() });
                }
                if (!clickables.length) return false;
                clickables.sort(function(a, b) { return a.left - b.left; });
                for (var j = 0; j < clickables.length; j++) {
                    var tx = clickables[j].tx;
                    var cls = (clickables[j].el.className || '').toString().toLowerCase();
                    if (dir < 0) {
                        if (tx === '<' || tx === '－' || tx === '-' || tx === '《' || tx === '◀'
                            || cls.indexOf('left') >= 0 || cls.indexOf('minus') >= 0 || cls.indexOf('prev') >= 0) {
                            try { clickables[j].el.click(); } catch (e) {}
                            return true;
                        }
                    } else {
                        if (tx === '>' || tx === '+' || tx === '》' || tx === '▶'
                            || cls.indexOf('right') >= 0 || cls.indexOf('plus') >= 0 || cls.indexOf('next') >= 0) {
                            try { clickables[j].el.click(); } catch (e) {}
                            return true;
                        }
                    }
                }
                var pick = dir < 0 ? clickables[0].el : clickables[clickables.length - 1].el;
                try { pick.click(); } catch (e) {}
                return true;
            }
            function pickQuantityInput(root) {
                if (!root || root.nodeType !== 1) return null;
                var sels = [
                    'input[type=number]', 'input[type=text]', 'input[type=tel]',
                    'input[name*="Qty"], input[name*="qty"], input[id*="Qty"], input[id*="qty"]'
                ];
                for (var si = 0; si < sels.length; si++) {
                    var list = root.querySelectorAll(sels[si]);
                    for (var i = 0; i < list.length; i++) {
                        var el = list[i];
                        if (!el || el.nodeType !== 1) continue;
                        var tp = (el.type || '').toLowerCase();
                        if (tp === 'checkbox' || tp === 'hidden' || tp === 'radio' || tp === 'file') continue;
                        if (typeof el.value === 'undefined') continue;
                        return el;
                    }
                }
                return null;
            }
            function safeSetInputValue(el, v) {
                if (!el || el.nodeType !== 1) return false;
                try { if (typeof el.focus === 'function') el.focus(); } catch (e) {}
                try {
                    if ('value' in el) el.value = String(v);
                    else return false;
                } catch (e2) { return false; }
                if (typeof el.dispatchEvent === 'function') {
                    try {
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    } catch (e3) {}
                }
                return true;
            }
            function setQtyOnInput(inp, v) {
                var el = inp && inp.nodeType === 1 ? inp : null;
                if (!el) el = pickQuantityInput(inp);
                return safeSetInputValue(el, v);
            }
            function getTd(row, colIx) {
                if (colIx < 0) return null;
                var tds = row.querySelectorAll('td');
                if (colIx >= tds.length) return null;
                return tds[colIx];
            }
            function readQty(qtyRoot, rowOpt) {
                var row = rowOpt || (qtyRoot && qtyRoot.tagName === 'TR' ? qtyRoot : (qtyRoot && qtyRoot.closest ? qtyRoot.closest('tr') : null));
                if (qtyRoot && qtyRoot.tagName === 'TD') {
                    var qtd = readQtyFromSpinnerCell(qtyRoot);
                    if (qtd !== null) return qtd;
                }
                if (row) {
                    var cell = findQtyCellInRow(row);
                    if (cell) {
                        var qc = readQtyFromSpinnerCell(cell);
                        if (qc !== null) return qc;
                    }
                }
                return null;
            }
            function resolveQtyCellForRow(row, qtyIx, descIx) {
                var cell = findQtyCellInRow(row);
                if (cell) return cell;
                if (qtyIx >= 0 && qtyIx !== descIx) {
                    var cand = getTd(row, qtyIx);
                    if (cand && countSpinnerClickables(cand) >= 2) return cand;
                }
                return null;
            }
            function descKey(s) {
                var n = norm(s);
                var side = n.indexOf('后侧轿壁') >= 0 ? '后侧'
                    : (n.indexOf('后轿壁') >= 0 ? '后' : '');
                var aw = (n.match(/aw\\d*=\\d+/gi) || []).slice().sort().join('');
                var ah = (n.match(/ah\\d*=\\d+/gi) || []).slice().sort().join('');
                if (side || aw || ah) return side + '|' + aw + '|' + ah;
                return n.length >= 16 ? n.slice(0, 16) : n;
            }
            function descLooseMatch(cd, ld) {
                if (!cd || !ld) return false;
                if (cd.indexOf(ld) >= 0 || ld.indexOf(cd) >= 0) return true;
                var kc = descKey(cd), kl = descKey(ld);
                if (kc && kl && kc === kl) return true;
                var a = cd.replace(/后侧轿壁/g, '后轿壁');
                var b = ld.replace(/后侧轿壁/g, '后轿壁');
                if (a.indexOf(b) >= 0 || b.indexOf(a) >= 0) return true;
                var ka = a.length >= 10 ? a.slice(0, 10) : a;
                var kb = b.length >= 10 ? b.slice(0, 10) : b;
                return ka.length >= 6 && kb.length >= 6 && (a.indexOf(kb) >= 0 || b.indexOf(ka) >= 0);
            }
            function alignRowByMaterialHint(hint, want) {
                hint = norm(hint || '');
                if (!hint || hint.length < 3) return {ok: false, reason: 'no_hint'};
                var tables = document.querySelectorAll('table');
                for (var ti = 0; ti < tables.length; ti++) {
                    var tbl = tables[ti];
                    if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                    var rows = getDataRows(tbl);
                    for (var ri = 0; ri < rows.length; ri++) {
                        var row = rows[ri];
                        if (row.querySelector('th')) continue;
                        var rowTx = norm(row.innerText || '');
                        if (!descLooseMatch(rowTx, hint)) continue;
                        var qcell = findQtyCellInRow(row) || row;
                        var before = readQty(qcell, row);
                        var rr = adjustQtyRoot(qcell, want, row);
                        var after = readQty(qcell, row);
                        return Object.assign(
                            {},
                            rr,
                            {
                                ok: rr.ok && after === want,
                                mode: 'material_row',
                                rows: 1,
                                detail: [{
                                    j: 0,
                                    desc: (row.innerText || '').trim().substring(0, 60),
                                    before: before,
                                    want: want,
                                    after: after,
                                    ok: rr.ok && after === want,
                                    step: rr.mode || ''
                                }]
                            },
                            rr
                        );
                    }
                }
                return {ok: false, reason: 'no_material_row'};
            }
            function clickDelta(qtyRoot, dir, rowOpt) {
                var cell = rowOpt ? findQtyCellInRow(rowOpt) : null;
                if (!cell && qtyRoot && qtyRoot.tagName === 'TD') cell = qtyRoot;
                if (cell && clickSpinnerDelta(cell, dir)) return true;
                var roots = [];
                if (cell) roots.push(cell);
                if (qtyRoot) roots.push(qtyRoot);
                if (rowOpt && roots.indexOf(rowOpt) < 0) roots.push(rowOpt);
                for (var ri = 0; ri < roots.length; ri++) {
                    var root = roots[ri];
                    if (!root) continue;
                var cand = root.querySelectorAll(
                    'button, a, span, i, div, img, svg, b, strong'
                );
                for (var i = 0; i < cand.length; i++) {
                    var el = cand[i];
                    if (!isVis(el)) continue;
                    var tx = (el.textContent || '').trim();
                    var cls = (el.className || '').toString().toLowerCase();
                    var title = ((el.getAttribute && el.getAttribute('title')) || '').toLowerCase();
                    if (dir < 0) {
                        if (tx === '-' || tx === '－' || tx === '<' || tx === '《' || tx === '◀'
                            || cls.indexOf('minus') >= 0 || cls.indexOf('decrease') >= 0
                            || cls.indexOf('reduce') >= 0 || cls.indexOf('chevron-left') >= 0
                            || cls.indexOf('caret-left') >= 0 || cls.indexOf('arrow-l') >= 0
                            || cls.indexOf('glyphicon') >= 0 && cls.indexOf('left') >= 0
                            || title.indexOf('减') >= 0) {
                            try { el.click(); } catch(e) {}
                            return true;
                        }
                    } else {
                        if (tx === '+' || tx === '＋' || tx === '>' || tx === '》' || tx === '▶'
                            || cls.indexOf('plus') >= 0 || cls.indexOf('increase') >= 0
                            || cls.indexOf('chevron-right') >= 0 || cls.indexOf('caret-right') >= 0
                            || cls.indexOf('arrow-r') >= 0
                            || title.indexOf('加') >= 0) {
                            try { el.click(); } catch(e) {}
                            return true;
                        }
                    }
                }
                for (var i = 0; i < cand.length; i++) {
                    var el = cand[i], r = el.getBoundingClientRect(), cls = (el.className || '').toString().toLowerCase();
                    if (r.width > 0 && r.width < 56 && r.height > 0 && r.height < 56) {
                        if (dir < 0 && (cls.indexOf('left') >= 0 || cls.indexOf('prev') >= 0 || cls.indexOf('l-') >= 0)) {
                            try { el.click(); } catch(e) {}
                            return true;
                        }
                        if (dir > 0 && (cls.indexOf('right') >= 0 || cls.indexOf('next') >= 0 || cls.indexOf('r-') >= 0)) {
                            try { el.click(); } catch(e) {}
                            return true;
                        }
                    }
                }
                }
                return false;
            }
            function writeQty(qtyRoot, v, rowOpt) {
                var root = resolveQtyRoot(qtyRoot, rowOpt);
                var el = pickQuantityInput(root);
                if (!el && rowOpt) el = pickQuantityInput(rowOpt);
                if (el && safeSetInputValue(el, v)) return true;
                var f = findQtyInputs(root);
                if (f.vis && setQtyOnInput(f.vis, v)) return true;
                if (f.hid && setQtyOnInput(f.hid, v)) return true;
                if (f.any && setQtyOnInput(f.any, v)) return true;
                return false;
            }
            function adjustQtyRoot(qtyRoot, want, rowOpt) {
                function sleepMs(ms) {
                    var t0 = Date.now();
                    while (Date.now() - t0 < ms) {}
                }
                var activeRoot = resolveQtyRoot(qtyRoot, rowOpt);
                var before0 = readQty(activeRoot, rowOpt);
                var steps = 0;
                while (steps < 120) {
                    var q = readQty(activeRoot, rowOpt);
                    if (q === null) return {ok: false, reason: 'read_qty'};
                    if (q === want) return {ok: true, before: before0, final: q};
                    if (q > want) {
                        if (clickDelta(activeRoot, -1, rowOpt)) {
                            sleepMs(300);
                            var qClick = readQty(activeRoot, rowOpt);
                            if (qClick === want) return {ok: true, before: before0, final: qClick, mode: 'click'};
                            if (qClick !== null && qClick !== q) {
                                steps++;
                                continue;
                            }
                        }
                        if (writeQty(activeRoot, want, rowOpt)) {
                            sleepMs(180);
                            var qAfter = readQty(activeRoot, rowOpt);
                            if (qAfter === want) return {ok: true, before: before0, final: qAfter, mode: 'write'};
                        }
                        return {ok: false, reason: 'dec_fail', cur: q};
                    } else {
                        if (clickDelta(activeRoot, 1, rowOpt)) {
                            sleepMs(300);
                            var qClick2 = readQty(activeRoot, rowOpt);
                            if (qClick2 === want) return {ok: true, before: before0, final: qClick2, mode: 'click'};
                            if (qClick2 !== null && qClick2 !== q) {
                                steps++;
                                continue;
                            }
                        }
                        if (writeQty(activeRoot, want, rowOpt)) {
                            sleepMs(180);
                            var qAfter2 = readQty(activeRoot, rowOpt);
                            if (qAfter2 === want) return {ok: true, before: before0, final: qAfter2, mode: 'write'};
                        }
                        return {ok: false, reason: 'inc_fail', cur: q};
                    }
                    steps++;
                }
                return {ok: false, reason: 'max_steps'};
            }
            function buildWants(nRows, descTexts, lineDesc, lineQty, total, singleRow) {
                if (singleRow) {
                    // 购物车可能把多条 OMS 物料合并成 1 行，此时应对齐汇总数量，
                    // 不能只取第一条 OMS 数量，否则会一直与 OMS 总件数不一致。
                    return [total];
                }
                var wants = [];
                var j, i;
                if (!lineQty || lineQty.length < 1) return null;
                if (lineDesc && lineDesc.length === lineQty.length) {
                    for (j = 0; j < nRows; j++) wants[j] = -1;
                    var usedI = {};
                    for (j = 0; j < nRows; j++) {
                        var cd = norm(descTexts[j] || '');
                        for (i = 0; i < lineDesc.length; i++) {
                            if (usedI[i]) continue;
                            var ld = norm(lineDesc[i] || '');
                            if (!ld || !cd) continue;
                            if (descLooseMatch(cd, ld)) {
                                wants[j] = parseInt(lineQty[i], 10) || 1;
                                usedI[i] = true;
                                break;
                            }
                        }
                    }
                    var nextI = 0;
                    for (j = 0; j < nRows; j++) {
                        if (wants[j] >= 1) continue;
                        while (nextI < lineQty.length && usedI[nextI]) nextI++;
                        if (nextI < lineQty.length) {
                            wants[j] = parseInt(lineQty[nextI], 10) || 1;
                            usedI[nextI] = true;
                            nextI++;
                        } else wants[j] = 1;
                    }
                    return wants;
                }
                if (lineQty.length === nRows) {
                    for (j = 0; j < nRows; j++) wants.push(parseInt(lineQty[j], 10) || 1);
                    return wants;
                }
                if (total < nRows) return null;
                var base = Math.floor(total / nRows), rem = total % nRows;
                for (j = 0; j < nRows; j++) wants.push(base + (j < rem ? 1 : 0));
                return wants;
            }
            if (lineDesc && lineDesc.length === 1 && lineQty && lineQty.length >= 1) {
                var wantOne = parseInt(lineQty[0], 10) || 1;
                var mrEarly = alignRowByMaterialHint(lineDesc[0], wantOne);
                if (mrEarly.ok) {
                    return {
                        ok: true,
                        mode: 'material_hint_early',
                        rows: mrEarly.rows || 1,
                        qtyColIndex: null,
                        descColIndex: null,
                        detail: mrEarly.detail || []
                    };
                }
            }
            var tbl = findCartTable();
            if (!tbl) return {ok: false, reason: 'no_table'};
            var descIx = findColumnIndexInThead(tbl, ['产品描述', '物料描述', '描述', '产品']);
            var qtyIx = findColumnIndexInThead(tbl, [
                '数量', '數量', 'Qty', 'QTY', 'qty',
                '采购数量', '订货数量', '购买数量', '数量(件)', '件数'
            ]);
            if (qtyIx < 0) qtyIx = inferQtyColFromDataRows(tbl, descIx);
            function qtyRootForRow(row, qtyCell, qtyIx, descIx) {
                return resolveQtyCellForRow(row, qtyIx, descIx) || qtyCell || null;
            }
            var pack = [];
            var tbodyRows = getDataRows(tbl);
            for (var ri = 0; ri < tbodyRows.length; ri++) {
                var row = tbodyRows[ri];
                if (row.querySelector('th')) continue;
                var hasCb = row.querySelector('input[type=checkbox]');
                var dcell = descIx >= 0 ? getTd(row, descIx) : null;
                var dtext = dcell ? (dcell.innerText || '').trim() : (row.innerText || '').trim().substring(0, 120);
                if (norm(dtext).length < 4) {
                    if (!lineDesc || !lineDesc.length) continue;
                    var hint0 = norm(lineDesc[0] || '');
                    var rowN0 = norm(row.innerText || '');
                    if (!hint0 || rowN0.indexOf(hint0.slice(0, 8)) < 0) continue;
                }
                if (!hasCb) continue;
                if (!isVis(row)) continue;
                var qtyRoot = qtyRootForRow(row, null, qtyIx, descIx);
                if (!qtyRoot) continue;
                pack.push({ row: row, qtyRoot: qtyRoot, descText: dtext });
            }
            if (pack.length < 1) {
                if (lineDesc && lineDesc.length) {
                    var hintWant = (lineQty && lineQty.length >= 1)
                        ? (parseInt(lineQty[0], 10) || 1) : target;
                    var mr = alignRowByMaterialHint(lineDesc[0], hintWant);
                    if (mr.ok) return {ok: true, mode: 'material_hint_only', detail: [mr]};
                }
                return {ok: false, reason: 'no_rows'};
            }
            var descTexts = [];
            for (var p = 0; p < pack.length; p++) descTexts.push(pack[p].descText);
            var wants = buildWants(
                pack.length,
                descTexts,
                lineDesc,
                lineQty,
                target,
                pack.length === 1
            );
            if (!wants) return {ok: false, reason: 'no_wants'};
            var detail = [];
            for (var j = 0; j < pack.length; j++) {
                var before = readQty(pack[j].qtyRoot, pack[j].row);
                var rr = adjustQtyRoot(pack[j].qtyRoot, wants[j], pack[j].row);
                var after = readQty(pack[j].qtyRoot, pack[j].row);
                detail.push({
                    j: j,
                    desc: (pack[j].descText || '').substring(0, 60),
                    qtyCol: qtyIx,
                    before: before,
                    want: wants[j],
                    after: after,
                    ok: rr.ok,
                    step: rr.mode || ''
                });
                if (!rr.ok) {
                    if (lineDesc && lineDesc.length) {
                        var mr2 = alignRowByMaterialHint(lineDesc[0], wants[j]);
                        if (mr2.ok) {
                            detail[detail.length - 1].ok = true;
                            detail[detail.length - 1].step = 'material_hint';
                            continue;
                        }
                    }
                    return {ok: false, reason: 'adjust_fail', row: j, detail: detail, sub: rr};
                }
            }
            return {
                ok: true,
                mode: pack.length === 1 ? 'single' : (lineDesc ? 'matched_desc' : 'per_line_or_dist'),
                rows: pack.length,
                qtyColIndex: qtyIx,
                descColIndex: descIx,
                final: after,
                detail: detail
            };
        """,
            target,
            line_arg,
            desc_arg,
        )
        if isinstance(res, dict) and res.get("ok"):
            logger.info(
                f"  ✓ 购物车数量已与 OMS 对齐: 模式={res.get('mode')} 汇总={target} "
                f"行数={res.get('rows', '')} 表头「数量」列索引={res.get('qtyColIndex')} "
                f"「产品描述」列索引={res.get('descColIndex')}"
            )
            for row in res.get("detail") or []:
                logger.info(
                    f"    行{row.get('j')}: 改前={row.get('before')} → 目标={row.get('want')} "
                    f"改后={row.get('after')} | {str(row.get('desc', ''))[:50]}"
                )
            self._cart_click_update_cart_if_present()
            if self._cart_verify_quantity_aligned(
                target, desc_arg, line_quantities=line_arg
            ):
                return True
            logger.warning("  购物车数量更新后回读仍不一致，继续使用真实按钮/输入框兜底")
        if isinstance(res, dict):
            logger.warning(f"  购物车数量对齐(JS): {res}")
        return self._cart_force_line_quantity_selenium_fallback(
            target,
            material_hint=(
                line_material_descriptions[0]
                if line_material_descriptions
                else None
            ),
            line_quantities=line_arg,
            line_material_descriptions=desc_arg,
        )

    def _cart_click_update_cart_if_present(self):
        """若购物车页存在「更新购物车」按钮，点击一次让数量变更落库。"""
        try:
            clicked = bool(
                self.driver.execute_script(
                    """
                function isVis(el) {
                    if (!el) return false;
                    var r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }
                function label(el) {
                    return ((el.value || el.textContent || el.innerText || '') + '')
                        .replace(/\\s+/g, '').toLowerCase();
                }
                var nodes = document.querySelectorAll('button, a, input[type=button], input[type=submit]');
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    if (!isVis(el)) continue;
                    var t = label(el);
                    var idc = ((el.id || '') + ' ' + (el.name || '') + ' ' + (el.className || '')).toLowerCase();
                    var isUpdateCart =
                        t.indexOf('更新购物车') >= 0 ||
                        t.indexOf('updatecart') >= 0 ||
                        idc.indexOf('updatecart') >= 0 ||
                        idc.indexOf('cartupdate') >= 0;
                    var isGenerate =
                        t.indexOf('生成报价') >= 0 ||
                        t.indexOf('报价单') >= 0 ||
                        t.indexOf('quotation') >= 0;
                    if (isUpdateCart && !isGenerate) {
                        try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                        el.click();
                        return true;
                    }
                }
                return false;
                """
                )
            )
            if clicked:
                time.sleep(2.0)
                logger.info("  ✓ 已点击「更新购物车」保存数量")
                self._cart_close_update_success_dialog_if_present()
            return clicked
        except Exception as e:
            logger.debug(f"点击更新购物车失败（忽略）: {e}")
            return False

    def _cart_close_update_success_dialog_if_present(self):
        """关闭「更新购物车成功」提示弹窗，避免遮挡后续生成报价单。"""
        try:
            for _ in range(8):
                closed = bool(
                    self.driver.execute_script(
                        """
                    function isVis(el) {
                        if (!el) return false;
                        var r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }
                    function textOf(el) {
                        return ((el.value || el.textContent || el.innerText || '') + '')
                            .replace(/\\s+/g, '');
                    }
                    var containers = document.querySelectorAll(
                        '.modal, .modal-dialog, .modal-content, .layui-layer, .bootbox, [role=dialog], .dialog'
                    );
                    for (var i = 0; i < containers.length; i++) {
                        var box = containers[i];
                        if (!isVis(box)) continue;
                        var tx = textOf(box);
                        if (tx.indexOf('更新购物车成功') < 0 && tx.indexOf('Updateshoppingcartsuccess') < 0) continue;
                        var btns = box.querySelectorAll('button, a, input[type=button], input[type=submit], .close');
                        for (var j = 0; j < btns.length; j++) {
                            var b = btns[j];
                            if (!isVis(b)) continue;
                            var bt = textOf(b);
                            var cls = ((b.className || '') + '').toLowerCase();
                            if (bt.indexOf('关闭') >= 0 || bt === '×' || bt === 'x' || cls.indexOf('close') >= 0) {
                                try { b.click(); } catch(e) {}
                                return true;
                            }
                        }
                    }
                    var all = document.querySelectorAll('button, a, input[type=button], input[type=submit], .close');
                    for (var k = 0; k < all.length; k++) {
                        var el = all[k];
                        if (!isVis(el)) continue;
                        var t = textOf(el);
                        var cls2 = ((el.className || '') + '').toLowerCase();
                        var inDialog = el.closest('.modal, .modal-dialog, .modal-content, .layui-layer, .bootbox, [role=dialog], .dialog');
                        if (!inDialog) continue;
                        if (t.indexOf('关闭') >= 0 || t === '×' || t === 'x' || cls2.indexOf('close') >= 0) {
                            try { el.click(); } catch(e2) {}
                            return true;
                        }
                    }
                    return false;
                    """
                    )
                )
                if closed:
                    time.sleep(0.5)
                    logger.info("  ✓ 已关闭「更新购物车成功」提示")
                    return True
                time.sleep(0.25)
            return False
        except Exception as e:
            logger.debug(f"关闭更新购物车提示失败（忽略）: {e}")
            return False

    def _cart_verify_per_line_quantities(
        self, line_quantities, line_material_descriptions
    ):
        """逐行校验：每条 OMS 物料描述对应购物车行 quantity 是否一致。"""
        ok, _ = self._cart_verify_qty_by_product_desc(
            line_quantities, line_material_descriptions
        )
        return ok

    def _cart_verify_line_quantities_by_cart_order(self, line_quantities):
        """无描述匹配时：按购物车表行顺序与 OMS 逐行数量列表比对。"""
        line_qty = self._cart_normalize_line_quantities(line_quantities)
        if not line_qty:
            return False
        try:
            ok = bool(
                self.driver.execute_script(
                    self._CART_QTY_JS_CORE
                    + """
                var wants = arguments[0];
                var got = [];
                var tables = document.querySelectorAll('table');
                for (var ti = 0; ti < tables.length; ti++) {
                    var tbl = tables[ti];
                    if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                    var rows = tbl.querySelectorAll('tbody tr');
                    for (var ri = 0; ri < rows.length; ri++) {
                        var row = rows[ri];
                        if (row.querySelector('th')) continue;
                        if (!row.querySelector('input[type=checkbox]')) continue;
                        var tx = (row.innerText || '').replace(/\\s+/g, '');
                        if (tx.length < 8) continue;
                        var q = readQtyFromRow(row);
                        if (q !== null) got.push(q);
                    }
                }
                if (got.length !== wants.length) return false;
                for (var j = 0; j < wants.length; j++) {
                    if (parseInt(got[j], 10) !== parseInt(wants[j], 10)) return false;
                }
                return true;
                """,
                    line_qty,
                )
            )
            if ok:
                logger.info(
                    f"  ✓ 购物车按行序数量回读校验通过: {line_qty}"
                )
            else:
                logger.warning(
                    f"  购物车按行序数量回读不一致，期望各行={line_qty}"
                )
            return ok
        except Exception:
            return False

    def _cart_verify_quantity_aligned(
        self,
        target,
        line_material_descriptions=None,
        line_quantities=None,
    ):
        """对齐后回读：多行 OMS 时逐行校验；否则兼容单行=总件数。"""
        line_qty = self._cart_normalize_line_quantities(line_quantities)
        line_desc = [
            str(d or "").strip()
            for d in (line_material_descriptions or [])
        ]
        if line_qty and line_desc and len(line_qty) == len(line_desc):
            if self._cart_verify_per_line_quantities(line_qty, line_desc):
                return True
            # 备件网有时会把多条 OMS 物料合并为购物车单行；这种情况下应允许单行=汇总数量。
            if len(line_qty) > 1:
                logger.info("  购物车逐行校验未通过，尝试按合并单行汇总数量校验")
            else:
                return False
        elif line_qty and len(line_qty) > 1:
            if self._cart_verify_line_quantities_by_cart_order(line_qty):
                return True
            logger.info("  购物车按行序校验未通过，尝试按合并单行汇总数量校验")
        hint = ""
        if line_material_descriptions:
            hint = str(line_material_descriptions[0] or "")[:40]
        try:
            ok = bool(
                self.driver.execute_script(
                    """
                var target = parseInt(arguments[0], 10);
                function norm(s) { return (s || '').replace(/\\s+/g, '').toLowerCase(); }
                var hint = norm(arguments[1] || '');
                function cellHasQtySpinner(el) {
                    if (!el || el.nodeType !== 1) return false;
                    if (el.querySelector('input[type=number], input[type=text], input[type=tel]')) return true;
                    var n = 0;
                    var bs = el.querySelectorAll('button, a, span, i');
                    for (var bi = 0; bi < bs.length; bi++) {
                        var r = bs[bi].getBoundingClientRect();
                        if (r.width > 0 && r.width < 100 && r.height > 0 && r.height < 100) n++;
                    }
                    return n >= 2;
                }
                function rowHasPrice(el) {
                    if (cellHasQtySpinner(el)) return false;
                    var tx = el.innerText || el.textContent || '';
                    return tx.indexOf('￥') >= 0 || tx.indexOf('¥') >= 0 || /\\d{1,3},\\d{3}/.test(tx);
                }
                function rowQtyStrict(row) {
                    var QTY_SEL = 'input[type=number], input[type=text], input[type=tel], input[name*="Qty"], input[name*="qty"], '
                        + 'input[id*="Qty"], input[id*="qty"], input[id*="amount"], input[id*="Amount"]';
                    var BROAD = 'input:not([type=checkbox]):not([type=hidden]):not([type=radio]):not([type=file])'
                        + ':not([type=button]):not([type=submit]):not([type=reset]):not([type=image])';
                    var tds = row.querySelectorAll('td');
                    for (var ci = 0; ci < tds.length; ci++) {
                        if (rowHasPrice(tds[ci])) continue;
                        var inps = tds[ci].querySelectorAll(QTY_SEL);
                        if (!inps.length) inps = tds[ci].querySelectorAll(BROAD);
                        for (var ii = 0; ii < inps.length; ii++) {
                            var v = parseInt(inps[ii].value, 10);
                            if (!isNaN(v) && v >= 0) return v;
                        }
                        var tx = (tds[ci].innerText || '').replace(/</g,' ').replace(/>/g,' ').replace(/\\s+/g,' ').trim();
                        var m = tx.match(/\\b(\\d{1,4})\\b/);
                        if (m && tds[ci].querySelectorAll('button, a').length >= 1) return parseInt(m[1], 10);
                    }
                    return null;
                }
                var key = hint.length >= 8 ? hint.slice(0, 8) : hint;
                var tables = document.querySelectorAll('table');
                for (var ti = 0; ti < tables.length; ti++) {
                    var tbl = tables[ti];
                    if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                    var rows = tbl.querySelectorAll('tbody tr');
                    for (var ri = 0; ri < rows.length; ri++) {
                        var row = rows[ri];
                        if (row.querySelector('th')) continue;
                        var rowN = norm(row.innerText || '');
                        if (rowN.length < 8) continue;
                        if (key && rowN.indexOf(key) < 0) continue;
                        var q = rowQtyStrict(row);
                        if (q === target) return true;
                    }
                }
                return false;
                """,
                    int(target),
                    hint,
                )
            )
            if ok:
                logger.info(f"  ✓ 购物车数量回读校验: {target}")
            else:
                logger.warning(f"  购物车数量仍未等于 OMS 目标 {target}，请人工核对")
            return ok
        except Exception:
            return False

    def _cart_js_force_qty_product_table(self, target, material_hint=None):
        """购物车表：在匹配物料行或首行物料的数量 input 中直接写入 target。"""
        try:
            t = int(target)
        except (TypeError, ValueError):
            return None
        hint = str(material_hint or "")[:60]
        return self.driver.execute_script(
            """
            var target = parseInt(arguments[0], 10);
            var hint = (arguments[1] || '').replace(/\\s+/g, '').toLowerCase();
            var key = hint.length >= 8 ? hint.slice(0, 8) : hint;
            function norm(s) { return (s || '').replace(/\\s+/g, '').toLowerCase(); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var QTY_SEL = 'input[type=number], input[type=text], input[type=tel], input[name*=\"Qty\"], input[name*=\"qty\"], '
                + 'input[id*=\"Qty\"], input[id*=\"qty\"], input[id*=\"amount\"], input[id*=\"Amount\"]';
            var BROAD_SEL = 'input:not([type=checkbox]):not([type=hidden]):not([type=radio]):not([type=file])'
                + ':not([type=button]):not([type=submit]):not([type=reset]):not([type=image])';
            function setInp(el, target) {
                if (!el || el.nodeType !== 1 || !('value' in el)) return NaN;
                try { if (typeof el.focus === 'function') el.focus(); } catch (e) {}
                el.value = String(target);
                if (typeof el.dispatchEvent === 'function') {
                    try {
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    } catch (e2) {}
                }
                return parseInt(el.value, 10);
            }
            function collectInputs(row) {
                var list = [];
                var inps = row.querySelectorAll(QTY_SEL);
                if (!inps.length) inps = row.querySelectorAll(BROAD_SEL);
                for (var ii = 0; ii < inps.length; ii++) {
                    if (inps[ii]) list.push(inps[ii]);
                }
                return list;
            }
            function findQtyTd(row) {
                var tds = row.querySelectorAll('td');
                for (var ci = 0; ci < tds.length; ci++) {
                    var td = tds[ci];
                    var tx = td.innerText || '';
                    var hasSpin = td.querySelector('input[type=number], input[type=text], input[type=tel]');
                    var btnN = 0;
                    var bs = td.querySelectorAll('button, a, span');
                    for (var bi = 0; bi < bs.length; bi++) {
                        var r = bs[bi].getBoundingClientRect();
                        if (r.width > 0 && r.width < 100) btnN++;
                    }
                    if (!hasSpin && btnN < 2 && (tx.indexOf('￥') >= 0 || tx.indexOf('¥') >= 0 || /\\d{1,3},\\d{3}/.test(tx))) continue;
                    var inps = td.querySelectorAll(BROAD_SEL);
                    if (inps.length) return { td: td, inps: inps };
                    var btns = td.querySelectorAll('button, a, span');
                    var vis = 0;
                    for (var bi = 0; bi < btns.length; bi++) {
                        var r = btns[bi].getBoundingClientRect();
                        if (r.width > 0 && r.width < 100) vis++;
                    }
                    if (vis >= 2) return { td: td, inps: inps };
                }
                return null;
            }
            var tables = document.querySelectorAll('table');
            for (var ti = 0; ti < tables.length; ti++) {
                var tbl = tables[ti];
                if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                var rows = tbl.querySelectorAll('tbody tr');
                var matched = [], fallback = [];
                for (var ri = 0; ri < rows.length; ri++) {
                    var row = rows[ri];
                    if (row.querySelector('th')) continue;
                    var rowN = norm(row.innerText || '');
                    if (rowN.length < 8) continue;
                    var block = findQtyTd(row);
                    var inps = block ? block.inps : collectInputs(row);
                    if (!inps.length) continue;
                    if (key && rowN.indexOf(key) >= 0) matched = matched.concat(inps);
                    else fallback = fallback.concat(inps);
                }
                var pool = matched.length ? matched : fallback;
                if (pool.length >= 1) {
                    var el = pool[0];
                    var rb = setInp(el, target);
                    return { ok: rb === target, readback: rb, rows: pool.length, matched: matched.length > 0 };
                }
            }
            return { ok: false, reason: 'no_qty_input' };
        """,
            t,
            hint,
        )

    def _cart_js_spinner_adjust_to_target(self, target, material_hint=None):
        """按物料描述找到购物车行，用 < / > 或输入框将数量调到 target。"""
        try:
            t = int(target)
        except (TypeError, ValueError):
            return None
        hint = str(material_hint or "")[:120]
        return self.driver.execute_script(
            """
            var target = parseInt(arguments[0], 10);
            var hint = norm(arguments[1] || '');
            function norm(s) { return (s || '').replace(/\\s+/g, '').toLowerCase(); }
            function descKey(s) {
                var n = norm(s);
                var side = n.indexOf('后侧轿壁') >= 0 ? '后侧'
                    : (n.indexOf('后轿壁') >= 0 ? '后' : '');
                var aw = (n.match(/aw\\d*=\\d+/gi) || []).slice().sort().join('');
                var ah = (n.match(/ah\\d*=\\d+/gi) || []).slice().sort().join('');
                if (side || aw || ah) return side + '|' + aw + '|' + ah;
                return n.length >= 16 ? n.slice(0, 16) : n;
            }
            function descLooseMatch(cd, ld) {
                if (!cd || !ld) return false;
                if (cd.indexOf(ld) >= 0 || ld.indexOf(cd) >= 0) return true;
                var kc = descKey(cd), kl = descKey(ld);
                if (kc && kl && kc === kl) return true;
                var a = cd.replace(/后侧轿壁/g, '后轿壁');
                var b = ld.replace(/后侧轿壁/g, '后轿壁');
                return a.indexOf(b) >= 0 || b.indexOf(a) >= 0;
            }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function cellHasSpinner(td) {
                if (td.querySelector('input[type=number], input[type=text], input[type=tel]')) return true;
                var n = 0, bs = td.querySelectorAll('button, a, span, i');
                for (var bi = 0; bi < bs.length; bi++) {
                    var r = bs[bi].getBoundingClientRect();
                    if (r.width > 0 && r.width < 100 && r.height > 0) n++;
                }
                return n >= 2;
            }
            function rowHasPrice(td) {
                if (cellHasSpinner(td)) return false;
                var tx = td.innerText || '';
                return tx.indexOf('￥') >= 0 || tx.indexOf('¥') >= 0 || /\\d{1,3},\\d{3}/.test(tx);
            }
            function findQtyCell(row) {
                var tds = row.querySelectorAll('td');
                for (var ci = 0; ci < tds.length; ci++) {
                    var td = tds[ci];
                    if (rowHasPrice(td)) continue;
                    var inps = td.querySelectorAll(
                        'input:not([type=checkbox]):not([type=hidden]):not([type=radio]):not([type=file]):not([type=button]):not([type=submit]):not([type=reset]):not([type=image])'
                    );
                    var btns = 0;
                    var bs = td.querySelectorAll('button, a, span');
                    for (var bi = 0; bi < bs.length; bi++) {
                        var r = bs[bi].getBoundingClientRect();
                        if (r.width > 0 && r.width < 100) btns++;
                    }
                    if (inps.length || btns >= 2) return td;
                }
                return null;
            }
            function readCell(td) {
                var inps = td.querySelectorAll('input[type=number], input[type=text], input[type=tel]');
                for (var ii = 0; ii < inps.length; ii++) {
                    var v = parseInt(inps[ii].value, 10);
                    if (!isNaN(v) && v >= 0 && v <= 999) return v;
                }
                var raw = td.innerText || '';
                var spinM = raw.match(/<\\s*(\\d{1,4})\\s*>/);
                if (spinM) return parseInt(spinM[1], 10);
                return null;
            }
            function writeCell(td, v) {
                var el = null;
                var inps = td.querySelectorAll('input[type=number], input[type=text], input[type=tel]');
                for (var ii = 0; ii < inps.length; ii++) {
                    if (inps[ii] && inps[ii].nodeType === 1 && typeof inps[ii].value !== 'undefined') {
                        el = inps[ii]; break;
                    }
                }
                if (!el) return false;
                if (!safeSetInputValue(el, v)) return false;
                var rb = parseInt(el.value, 10);
                return rb === v;
            }
            function safeSetInputValue(el, v) {
                if (!el || el.nodeType !== 1) return false;
                try { if (typeof el.focus === 'function') el.focus(); } catch (e) {}
                try { if ('value' in el) el.value = String(v); else return false; } catch (e2) { return false; }
                if (typeof el.dispatchEvent === 'function') {
                    try {
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    } catch (e3) {}
                }
                return true;
            }
            function clickSpin(td, dir) {
                var clickables = [];
                var cand = td.querySelectorAll('button, a, span, div, i, b, strong');
                for (var i = 0; i < cand.length; i++) {
                    if (!isVis(cand[i])) continue;
                    var r = cand[i].getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.width > 100) continue;
                    clickables.push({
                        el: cand[i], left: r.left,
                        tx: (cand[i].textContent || '').trim(),
                        cls: (cand[i].className || '').toString().toLowerCase()
                    });
                }
                if (!clickables.length) return false;
                clickables.sort(function(a,b){ return a.left - b.left; });
                for (var j = 0; j < clickables.length; j++) {
                    var tx = clickables[j].tx;
                    var cls = clickables[j].cls;
                    if (dir < 0) {
                        if (tx === '<' || tx === '-' || tx === '－' || cls.indexOf('minus') >= 0 || cls.indexOf('left') >= 0) {
                            try { clickables[j].el.click(); } catch (e) {}
                            return true;
                        }
                    } else {
                        if (tx === '>' || tx === '+' || cls.indexOf('plus') >= 0 || cls.indexOf('right') >= 0) {
                            try { clickables[j].el.click(); } catch (e) {}
                            return true;
                        }
                    }
                }
                var pick = dir < 0 ? clickables[0].el : clickables[clickables.length - 1].el;
                try { pick.click(); } catch (e) {}
                return true;
            }
            var tables = document.querySelectorAll('table');
            for (var ti = 0; ti < tables.length; ti++) {
                var tbl = tables[ti];
                if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                var rows = tbl.querySelectorAll('tbody tr');
                if (!rows.length) rows = tbl.querySelectorAll('tr');
                for (var ri = 0; ri < rows.length; ri++) {
                    var row = rows[ri];
                    if (row.querySelector('th')) continue;
                    var rowN = norm(row.innerText || '');
                    if (rowN.length < 8) continue;
                    if (hint && !descLooseMatch(rowN, hint)) continue;
                    var td = findQtyCell(row);
                    if (!td) return {ok: false, reason: 'no_qty_cell'};
                    var before = readCell(td);
                    var steps = 0;
                    while (steps < 120) {
                        var q = readCell(td);
                        if (q === null) return {ok: false, reason: 'read_qty', before: before};
                        if (q === target) return {ok: true, before: before, after: q};
                        if (q > target) {
                            if (!clickSpin(td, -1) && !writeCell(td, target))
                                return {ok: false, reason: 'dec_fail', before: before, cur: q};
                        } else {
                            if (!clickSpin(td, 1) && !writeCell(td, target))
                                return {ok: false, reason: 'inc_fail', before: before, cur: q};
                        }
                        var t0 = Date.now();
                        while (Date.now() - t0 < 280) {}
                        steps++;
                    }
                    return {ok: false, reason: 'max_steps', before: before};
                }
            }
            return {ok: false, reason: 'no_row'};
            """,
            t,
            hint,
        )

    def _cart_force_line_quantity_selenium_fallback(
        self,
        target,
        material_hint=None,
        line_quantities=None,
        line_material_descriptions=None,
    ):
        """购物车 /Cart/ShoppingCart 等：在数量输入框中直接写入目标值（JS 点箭头失败时）。"""
        lq = line_quantities or []
        ld = line_material_descriptions or []
        if lq and ld and len(lq) == len(ld):
            aligned = self._cart_align_qty_by_product_desc(lq, ld)
            if isinstance(aligned, dict) and aligned.get("ok"):
                self._cart_click_update_cart_if_present()
                ok, _ = self._cart_verify_qty_by_product_desc(lq, ld)
                if ok:
                    return True
            any_ok = False
            for qty, desc in zip(lq, ld):
                q = int(qty) if qty is not None else 1
                d = str(desc or "").strip()
                sr = self._cart_js_spinner_adjust_to_target(q, material_hint=d)
                if isinstance(sr, dict) and sr.get("ok"):
                    any_ok = True
                    continue
                jr = self._cart_js_force_qty_product_table(q, material_hint=d)
                if isinstance(jr, dict) and jr.get("ok"):
                    any_ok = True
            if any_ok:
                self._cart_click_update_cart_if_present()
            ok, _ = self._cart_verify_qty_by_product_desc(lq, ld)
            return ok
        if not target or int(target) < 1:
            return False
        t = int(target)
        sr = self._cart_js_spinner_adjust_to_target(t, material_hint=material_hint)
        if isinstance(sr, dict) and sr.get("ok"):
            logger.info(
                f"  ✓ 购物车数量(spinner): {t}（改前={sr.get('before')} 改后={sr.get('after')}）"
            )
            self._cart_click_update_cart_if_present()
            return self._cart_verify_quantity_aligned(
                t, [material_hint] if material_hint else None
            )
        if isinstance(sr, dict) and sr.get("reason"):
            logger.info(f"  购物车数量(spinner): {sr}")
        jr = self._cart_js_force_qty_product_table(t, material_hint=material_hint)
        if isinstance(jr, dict) and jr.get("ok"):
            logger.info(f"  ✓ 购物车数量(JS 强制): {t}（回读={jr.get('readback')}）")
            self._cart_click_update_cart_if_present()
            return self._cart_verify_quantity_aligned(
                t, [material_hint] if material_hint else None
            )
        if isinstance(jr, dict):
            logger.info(f"  购物车数量(JS 强制): {jr}")
        tstr = str(t)
        xps = [
             "//table[contains(.,'产品描述')]//tbody//tr//td[contains(.,'数量') or .//button]//input[not(@type='checkbox')][not(@type='hidden')][not(@type='button')][not(@type='submit')][not(@type='reset')][not(@type='image')]",
            "//table[contains(.,'产品描述')]//tbody//tr//input[@type='number']",
            "//table[contains(.,'产品描述')]//tbody//tr//input[@type='text']",
            "//table[contains(.,'产品描述')]//tbody//tr//input[contains(@name,'Qty') or contains(@name,'qty')]",
            "//table[contains(.,'产品描述')]//tbody//tr//input[contains(@id,'Qty') or contains(@id,'qty')]",
             "//table[contains(.,'产品描述')]//tbody//tr//input[not(@type='checkbox')][not(@type='hidden')][not(@type='radio')][not(@type='button')][not(@type='submit')][not(@type='reset')][not(@type='image')]",
        ]
        for xp in xps:
            try:
                for el in self.driver.find_elements(By.XPATH, xp):
                    try:
                        self.driver.execute_script(
                            """
                            var el = arguments[0], v = arguments[1];
                            if (!el || el.nodeType !== 1 || !('value' in el)) return false;
                            el.value = String(v);
                            if (typeof el.dispatchEvent === 'function') {
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                            return true;
                            """,
                            el,
                            tstr,
                        )
                        if el.is_displayed():
                            el.click()
                            time.sleep(0.08)
                            el.clear()
                            el.send_keys(tstr)
                        el.send_keys(Keys.TAB)
                        time.sleep(0.25)
                        rb = el.get_attribute("value") or ""
                        if str(rb).strip() in (tstr, str(int(t))):
                            logger.info(f"  ✓ 购物车数量(Selenium 已写入为 {tstr}) 回读={rb}")
                            self._cart_click_update_cart_if_present()
                            return self._cart_verify_quantity_aligned(
                                t, [material_hint] if material_hint else None
                            )
                    except Exception:
                        continue
            except Exception:
                continue
        return self._cart_verify_quantity_aligned(
            t, [material_hint] if material_hint else None
        )

    def cart_select_products_and_generate_quotation(
        self,
        material_count=None,
        cart_quantity_target=None,
        cart_line_quantities=None,
        cart_line_material_descs=None,
    ):
        """
        购物车页：核对行数与 OMS 物料条数 → 将各物料「数量」与 OMS 汇总数量对齐
        → 勾选「产品描述」左侧行复选框 → 点「生成报价单」。
        """
        if not self._is_on_cart_page():
            raise RuntimeError("当前不在购物车页，无法生成报价单")

        row_count = self._cart_count_material_rows()
        logger.info(f"  购物车物料行数: {row_count}")
        self._cart_check_row_count_for_align(
            row_count, material_count, cart_line_material_descs
        )

        align_target = self._cart_resolve_quantity_target(
            cart_quantity_target, cart_line_quantities
        )
        qty_ok = self._cart_align_line_quantities_with_oms(
            cart_quantity_target,
            material_count,
            cart_line_quantities,
            cart_line_material_descs,
        )
        if align_target and not qty_ok:
            raise RuntimeError(
                self._cart_quantity_align_failure_message(
                    cart_quantity_target,
                    cart_line_quantities,
                    cart_line_material_descs,
                    material_count,
                )
            )
        time.sleep(1.0)
        self._cart_close_update_success_dialog_if_present()

        selected = self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function check(el) {
                if (!el.checked) {
                    el.click();
                    el.checked = true;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
                return true;
            }
            var tables = document.querySelectorAll('table');
            for (var ti = 0; ti < tables.length; ti++) {
                var tbl = tables[ti];
                if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                var rows = tbl.querySelectorAll('tbody tr');
                var any = false;
                for (var ri = 0; ri < rows.length; ri++) {
                    var row = rows[ri];
                    if (row.querySelector('th')) continue;
                    var cbs = row.querySelectorAll('td input[type=checkbox]');
                    if (!cbs.length) continue;
                    var cb = cbs[0];
                    if (isVis(cb)) {
                        check(cb);
                        any = true;
                    }
                }
                return any;
            }
            return false;
        """)
        if not selected:
            raise RuntimeError("未找到「产品描述」表左侧行复选框")
        logger.info("  ✓ 已勾选产品描述左侧行复选框")
        self._cart_close_update_success_dialog_if_present()

        gen_ok = self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function inSidebar(el) {
                return !!(el.closest('aside, nav, .sidebar, #sidebar, .left-menu, .menu-sidebar'));
            }
            function lab(el) {
                return ((el.value || el.textContent || el.innerText || '') + '').replace(/\\s+/g, '');
            }
            function attrs(el) {
                return [
                    el.id || '',
                    el.name || '',
                    el.className || '',
                    el.getAttribute('onclick') || '',
                    el.getAttribute('href') || ''
                ].join(' ').toLowerCase();
            }
            var nodes = document.querySelectorAll('input, button, a');
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (inSidebar(el) || !isVis(el)) continue;
                var t = lab(el);
                if (t.indexOf('生成报价单') >= 0) {
                    try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                    el.click();
                    return true;
                }
            }
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (inSidebar(el) || !isVis(el)) continue;
                var a = attrs(el);
                if ((a.indexOf('quotation') >= 0 || a.indexOf('quote') >= 0)
                    && a.indexOf('delete') < 0 && a.indexOf('clear') < 0) {
                    try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                    el.click();
                    return true;
                }
            }
            var table = null;
            var tables = document.querySelectorAll('table');
            for (var ti = 0; ti < tables.length; ti++) {
                if ((tables[ti].innerText || '').indexOf('产品描述') >= 0) {
                    table = tables[ti];
                    break;
                }
            }
            if (table) {
                var tr = table.getBoundingClientRect();
                var cand = [];
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    if (inSidebar(el) || !isVis(el)) continue;
                    var r = el.getBoundingClientRect();
                    if (r.top <= tr.bottom + 8) continue;
                    if (r.left > window.innerWidth * 0.75) continue;
                    var t = lab(el);
                    var a = attrs(el);
                    if (t.indexOf('清空') >= 0 || t.indexOf('更新') >= 0 || t.indexOf('删除') >= 0) continue;
                    if (a.indexOf('clear') >= 0 || a.indexOf('update') >= 0 || a.indexOf('delete') >= 0) continue;
                    cand.push({el: el, left: r.left, top: r.top});
                }
                cand.sort(function(a, b) {
                    if (Math.abs(a.top - b.top) > 10) return a.top - b.top;
                    return a.left - b.left;
                });
                if (cand.length >= 2) {
                    var pick = cand[cand.length - 1].el;
                    try { pick.scrollIntoView({block: 'center'}); } catch(e) {}
                    pick.click();
                    return true;
                }
            }
            return false;
        """)
        if not gen_ok:
            raise RuntimeError("未找到「生成报价单」按钮")
        logger.info("  ✓ 已点击「生成报价单」")
        time.sleep(2.5)

