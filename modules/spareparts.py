"""
备件网模块
负责：登录、新建询价单、提交、等待审批、购物车、生成报价单
"""
import time
import logging
import random
import re
import os
import json
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from config import (
    SPAREPARTS_URL, DEFAULT_ELEVATOR_SUPPLIER, DEFAULT_FACTORY,
    POLL_INTERVAL, MAX_WAIT_APPROVAL, SPAREPARTS_HOST_KEYWORD,
    OMS_SUPPLIER_SHANGHAI, OMS_SUPPLIER_ZHONGSHAN,
)
from utils.browser import Browser
from utils.spareparts_photo_upload import (
    PhotoUploadNeedsManualHelp,
    SparepartsPhotoUploader,
    deep_reset_photo_upload_popovers,
    hide_and_remove_body_popovers,
)
from modules.spareparts_cart import SparePartsCartMixin
from modules.spareparts_quotation import SparePartsQuotationMixin

logger = logging.getLogger(__name__)


class SparePartsModule(SparePartsCartMixin, SparePartsQuotationMixin):
    """
    备件网操作模块
    
    完整流程：
    1. 登录备件网
    2. 备件 → 新建询价单 → 填写信息 → 添加 → 提交 → 获取询价单号
    3. 等待审批 → 状态变为"已完结"
    4. 查看 → 全选 → 加号 → 加入购物车
    5. 查看购物车 → 生成报价单（松江/中山不同处理）
    6. 下载PDF + Excel
    """

    # 购物车数量：按「产品描述」匹配行，只读写数量列（不扫价格/描述里的数字）
    _CART_QTY_JS_CORE = r"""
    var QTY_INP_SEL = 'input[type=number], input[type=text], input[type=tel], input[name*="Qty"], input[name*="qty"], '
        + 'input[id*="Qty"], input[id*="qty"], input[id*="amount"], input[id*="Amount"]';
    var BROAD_INP_SEL = 'input:not([type=checkbox]):not([type=hidden]):not([type=radio]):not([type=file])'
        + ':not([type=button]):not([type=submit]):not([type=reset]):not([type=image])';
    function norm(s) { return (s || '').replace(/\s+/g, '').toLowerCase(); }
    function descKey(s) {
        var n = norm(s);
        var side = n.indexOf('后侧轿壁') >= 0 ? '后侧'
            : (n.indexOf('后轿壁') >= 0 ? '后' : '');
        var aw = (n.match(/aw\d*=\d+/gi) || []).slice().sort().join('');
        var ah = (n.match(/ah\d*=\d+/gi) || []).slice().sort().join('');
        var mfg = (n.match(/[a-z]?\\d{5,}/gi) || []).filter(function(x) {
            return x.length >= 5 && x.indexOf('2545') < 0;
        }).sort().join('');
        if (side || aw || ah || mfg) return side + '|' + aw + '|' + ah + '|' + mfg;
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
        return false;
    }
    function isVis(el) {
        if (!el) return false;
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function isPriceCell(td) {
        if (!td || td.nodeType !== 1) return false;
        var cls = (td.className || '').toString();
        if (/price/i.test(cls)) return true;
        if (td.querySelector('.lblPriceZS, .lblPrice, .CurrencyUnitZS, .CurrencyUnit')) return true;
        var tx = td.innerText || td.textContent || '';
        return tx.indexOf('￥') >= 0 || tx.indexOf('¥') >= 0 || /\d{1,3},\d{3}/.test(tx);
    }
    function isQtyCell(td) {
        if (!td || td.nodeType !== 1) return false;
        var cls = (td.className || '').toString();
        if (/qty/i.test(cls)) return true;
        return !!td.querySelector('.txtQtyZS, .txtQty, input.txtBox');
    }
    function countSpinnerClickables(el) {
        if (!el || el.nodeType !== 1) return 0;
        var n = 0;
        var bs = el.querySelectorAll('button, input[type=button], a');
        for (var bi = 0; bi < bs.length; bi++) {
            if (!isVis(bs[bi])) continue;
            var r = bs[bi].getBoundingClientRect();
            if (r.width > 0 && r.width < 100 && r.height > 0 && r.height < 100) n++;
        }
        return n;
    }
    function findQtyInputs(root) {
        if (!root || root.nodeType !== 1) return { vis: null, hid: null, any: null };
        var qtyInp = root.querySelector('.txtQtyZS, .txtQty');
        if (qtyInp) {
            return {
                vis: isVis(qtyInp) ? qtyInp : null,
                hid: isVis(qtyInp) ? null : qtyInp,
                any: qtyInp
            };
        }
        var all = root.querySelectorAll(QTY_INP_SEL);
        if (!all.length) all = root.querySelectorAll(BROAD_INP_SEL);
        var vis = null, hid = null, any = null;
        for (var qi = 0; qi < all.length; qi++) {
            var el = all[qi];
            if (!el || el.type === 'checkbox' || el.type === 'hidden' || el.type === 'button') continue;
            any = any || el;
            if (isVis(el)) vis = vis || el;
            else hid = hid || el;
        }
        return { vis: vis, hid: hid, any: any };
    }
    function cellHasQtySpinner(el) {
        if (!el || el.nodeType !== 1) return false;
        if (isPriceCell(el)) return false;
        if (isQtyCell(el)) return true;
        if (findQtyInputs(el).any) return true;
        return countSpinnerClickables(el) >= 2;
    }
    function rowHasPriceMarker(el) {
        return isPriceCell(el);
    }
    function cartTableForRow(row) {
        if (!row) return null;
        var tbl = row.closest('table');
        if (tbl && (tbl.innerText || '').indexOf('产品描述') >= 0) return tbl;
        return null;
    }
    function findQtyCellInRow(row) {
        if (!row) return null;
        var tds = row.querySelectorAll('td');
        for (var qi = 0; qi < tds.length; qi++) {
            if (isQtyCell(tds[qi])) return tds[qi];
        }
        var tbl = cartTableForRow(row);
        var qtyIx = -1;
        if (tbl) {
            qtyIx = findColumnIndexInThead(tbl, [
                '数量', '數量', 'Qty', 'QTY', 'qty',
                '采购数量', '订货数量', '购买数量', '数量(件)', '件数'
            ]);
        }
        if (qtyIx >= 0 && qtyIx < tds.length) {
            var qtd = tds[qtyIx];
            if (!isPriceCell(qtd)) return qtd;
        }
        var withSpinner = null, withInp = null;
        for (var ci = 0; ci < tds.length; ci++) {
            var td = tds[ci];
            if (isPriceCell(td)) continue;
            var hasInp = findQtyInputs(td).any;
            var spinN = countSpinnerClickables(td);
            if (hasInp) return td;
            if (spinN >= 2 && !withSpinner) withSpinner = td;
            if (hasInp && !withInp) withInp = td;
        }
        return withSpinner || withInp || null;
    }
    function readQtyFromCell(cell) {
        if (!cell || rowHasPriceMarker(cell)) return null;
        var f = findQtyInputs(cell);
        var inp = f.vis || f.hid || f.any;
        if (inp) {
            var raw = String(inp.value == null ? '' : inp.value).replace(/[^0-9]/g, '');
            if (raw) {
                var v = parseInt(raw, 10);
                if (!isNaN(v) && v >= 0 && v <= 999) return v;
            }
        }
        var spinN = countSpinnerClickables(cell);
        if (spinN < 1 && !inp) return null;
        var text = (cell.innerText || cell.textContent || '');
        var spinM = text.match(/[<\u003c\u300a\u25c0\u25c2\u2212\-]\s*(\d{1,4})\s*[>\u003e\u300b\u25b6\u25b8\+\uFF0B]/);
        if (!spinM) spinM = text.match(/(\d{1,4})\s*[>\u003e\u300b\u25b6\u25b8]/);
        if (spinM) {
            var v2 = parseInt(spinM[1], 10);
            if (!isNaN(v2) && v2 >= 0 && v2 <= 999) return v2;
        }
        var t = text.replace(/\s+/g, '');
        if (/^\d{1,4}$/.test(t)) return parseInt(t, 10);
        return null;
    }
    function readQtyFromRow(row) {
        var cell = findQtyCellInRow(row);
        if (!cell) return null;
        return readQtyFromCell(cell);
    }
    function safeSetInputValue(el, v) {
        if (!el || el.nodeType !== 1) return false;
        try { if (typeof el.focus === 'function') el.focus(); } catch (e) {}
        try {
            if (typeof el.select === 'function') el.select();
            else if (typeof el.setSelectionRange === 'function') {
                var len = String(el.value || '').length;
                el.setSelectionRange(0, len);
            }
        } catch (eSel) {}
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
    function writeQtyToRow(row, v) {
        var cell = findQtyCellInRow(row);
        if (!cell) return false;
        var f = findQtyInputs(cell);
        var inp = f.vis || f.hid || f.any;
        if (inp && safeSetInputValue(inp, v)) return true;
        return false;
    }
    function getDescCellText(row, descIx) {
        var tds = row.querySelectorAll('td');
        for (var pi = 0; pi < tds.length; pi++) {
            var cls = (tds[pi].className || '').toString();
            if (/partname/i.test(cls)) {
                var ptx = (tds[pi].innerText || '').trim();
                if (ptx.length >= 4) return ptx;
                var link = tds[pi].querySelector('a');
                if (link) {
                    var ltx = (link.textContent || link.innerText || '').trim();
                    if (ltx.length >= 4) return ltx;
                }
            }
        }
        if (descIx >= 0) {
            if (descIx < tds.length) {
                var tx = (tds[descIx].innerText || '').trim();
                if (tx.length >= 4) return tx;
            }
        }
        return (row.innerText || '').trim();
    }
    function findColumnIndexInThead(tbl, kws) {
        var heads = tbl.querySelectorAll('thead tr');
        for (var tr = 0; tr < heads.length; tr++) {
            var cells = heads[tr].querySelectorAll('th, td');
            for (var i = 0; i < cells.length; i++) {
                var t = (cells[i].textContent || '').replace(/\s+/g, '');
                for (var k = 0; k < kws.length; k++) {
                    if (t.indexOf(kws[k]) >= 0) return i;
                }
            }
        }
        return -1;
    }
    function isRowVisible(row) {
        if (!row) return false;
        var r = row.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }
    function scoreCartTable(tbl) {
        var score = 0;
        var descIx = findColumnIndexInThead(tbl, ['产品描述', '物料描述', '描述', '产品']);
        var rows = tbl.querySelectorAll('tbody tr');
        for (var ri = 0; ri < rows.length; ri++) {
            var row = rows[ri];
            if (row.querySelector('th')) continue;
            if (!isRowVisible(row)) continue;
            var desc = getDescCellText(row, descIx);
            if (norm(desc).length < 4) continue;
            score += 2;
            if (row.querySelector('td.QtyZS input.txtQtyZS, td.Qty input.txtQty')) score += 12;
            if (findQtyCellInRow(row)) score += 5;
            if (readQtyFromRow(row) !== null) score += 8;
        }
        var tr = tbl.getBoundingClientRect();
        if (tr.width > 0 && tr.height > 0) score += 10;
        return score;
    }
    function findBestCartTable() {
        var tables = document.querySelectorAll('table');
        var best = null, bestScore = -1;
        for (var ti = 0; ti < tables.length; ti++) {
            var tbl = tables[ti];
            if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
            var sc = scoreCartTable(tbl);
            if (sc > bestScore) { bestScore = sc; best = tbl; }
        }
        return best;
    }
    function iterCartTables() {
        var out = [];
        var best = findBestCartTable();
        if (best) out.push(best);
        var tables = document.querySelectorAll('table');
        for (var ti = 0; ti < tables.length; ti++) {
            var tbl = tables[ti];
            if (tbl === best) continue;
            if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
            out.push(tbl);
        }
        return out;
    }
    function findCartRowByDesc(lineDesc, usedKeys) {
        usedKeys = usedKeys || {};
        var ld = norm(lineDesc || '');
        var tables = iterCartTables();
        for (var ti = 0; ti < tables.length; ti++) {
            var tbl = tables[ti];
            var descIx = findColumnIndexInThead(tbl, ['产品描述', '物料描述', '描述', '产品']);
            var rows = tbl.querySelectorAll('tbody tr');
            for (var ri = 0; ri < rows.length; ri++) {
                var row = rows[ri];
                if (row.querySelector('th')) continue;
                if (!isRowVisible(row)) continue;
                var cd = norm(getDescCellText(row, descIx));
                if (cd.length < 4) continue;
                var rowKey = ti + '-' + ri + '-' + cd.slice(0, 24);
                if (usedKeys[rowKey]) continue;
                if (!descLooseMatch(cd, ld)) continue;
                usedKeys[rowKey] = true;
                return row;
            }
        }
        return null;
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
            clickables.push({ el: el, left: r.left, tx: (el.textContent || '').trim(), cls: (el.className || '').toString().toLowerCase() });
        }
        if (!clickables.length) return false;
        clickables.sort(function(a, b) { return a.left - b.left; });
        for (var j = 0; j < clickables.length; j++) {
            var tx = clickables[j].tx, cls = clickables[j].cls;
            if (dir < 0) {
                if (tx === '<' || tx === '-' || tx === '－' || tx === '《' || tx === '◀'
                    || cls.indexOf('minus') >= 0 || cls.indexOf('left') >= 0 || cls.indexOf('prev') >= 0) {
                    try { clickables[j].el.click(); } catch (e) {}
                    return true;
                }
            } else {
                if (tx === '>' || tx === '+' || tx === '》' || tx === '▶'
                    || cls.indexOf('plus') >= 0 || cls.indexOf('right') >= 0 || cls.indexOf('next') >= 0) {
                    try { clickables[j].el.click(); } catch (e) {}
                    return true;
                }
            }
        }
        var pick = dir < 0 ? clickables[0].el : clickables[clickables.length - 1].el;
        try { pick.click(); } catch (e2) {}
        return true;
    }
    function adjustQtyOnRow(row, want) {
        var cell = findQtyCellInRow(row);
        if (!cell) return {ok: false, reason: 'no_qty_cell'};
        var before = readQtyFromRow(row);
        if (before === want) return {ok: true, before: before, after: want, mode: 'ok'};
        writeQtyToRow(row, want);
        var afterWrite = readQtyFromRow(row);
        if (afterWrite === want) return {ok: true, before: before, after: afterWrite, mode: 'write'};
        var steps = 0;
        while (steps < 100) {
            var q = readQtyFromRow(row);
            if (q === want) return {ok: true, before: before, after: q, mode: 'spin'};
            if (q === null) {
                if (!clickSpinnerDelta(cell, want >= 1 ? 1 : -1)) {
                    return {ok: false, reason: 'read_fail', before: before};
                }
                var tNull = Date.now();
                while (Date.now() - tNull < 280) {}
                steps++;
                continue;
            }
            if (q > want) {
                if (!clickSpinnerDelta(cell, -1)) {
                    writeQtyToRow(row, want);
                    var aw = readQtyFromRow(row);
                    if (aw === want) return {ok: true, before: before, after: aw, mode: 'write2'};
                    return {ok: false, reason: 'dec_fail', before: before, cur: q};
                }
            } else {
                if (!clickSpinnerDelta(cell, 1)) {
                    writeQtyToRow(row, want);
                    var aw2 = readQtyFromRow(row);
                    if (aw2 === want) return {ok: true, before: before, after: aw2, mode: 'write2'};
                    return {ok: false, reason: 'inc_fail', before: before, cur: q};
                }
            }
            var t0 = Date.now();
            while (Date.now() - t0 < 280) {}
            steps++;
        }
        return {ok: false, reason: 'max_steps', before: before, cur: readQtyFromRow(row)};
    }
    function listCartMaterialRows() {
        var out = [];
        var tbl = findBestCartTable();
        if (!tbl) return out;
        var descIx = findColumnIndexInThead(tbl, ['产品描述', '物料描述', '描述', '产品']);
        var rows = tbl.querySelectorAll('tbody tr');
        for (var ri = 0; ri < rows.length; ri++) {
            var row = rows[ri];
            if (row.querySelector('th')) continue;
            if (!isRowVisible(row)) continue;
            var desc = getDescCellText(row, descIx);
            if (norm(desc).length < 4) continue;
            var qCell = findQtyCellInRow(row);
            out.push({
                desc: desc.substring(0, 80),
                qty: readQtyFromRow(row),
                key: descKey(norm(desc)),
                hasQtyCell: !!qCell,
                qtyCell: qCell ? (qCell.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40) : ''
            });
        }
        return out;
    }
    """

    # --- 页面元素选择器（需根据实际页面调整）---
    SELECTORS = {
        # 登录（通用选择器，适配域账号登录表单）
        "domain_login_link": (
            By.XPATH,
            "//a[contains(.,'域账号登') or contains(.,'使用域账号')]",
        ),
        "login_username": (By.XPATH, "//input[@type='text' or @placeholder='用户名' or @name='username' or contains(@id,'UserName') or contains(@id,'user')]"),
        "login_password": (By.XPATH, "//input[@type='password' or contains(@id,'Password') or contains(@id,'password')]"),
        "login_btn": (By.XPATH, "//input[@type='submit' or @value='登 录' or @value='登录'] | //button[contains(text(),'登录')]"),
        
        # 导航
        "menu_spareparts": (By.XPATH, "//a[contains(text(),'备件')]"),
        "sub_new_inquiry": (By.XPATH, "//a[contains(text(),'新建询价单')]"),
        
        # 新建询价单表单（基于 discover_selectors 扫描结果）
        "inquiry_project_name":   (By.ID, "txtProjectName"),
        "inquiry_project_no_name": (By.ID, "lblProjectNo"),  # 项目号（WBS号），填梯号（selectors_report确认ID为lblProjectNo）
        "inquiry_ladder_no":      (By.ID, "txtLiftNo"),
        "inquiry_material_desc":  (By.ID, "txtMaterialDescribe"),
        "inquiry_material_no":    (By.ID, "txtMaterialNo"),
        "inquiry_amount":         (By.ID, "txtAmount"),
        "inquiry_remark":         (By.ID, "txtRemark"),
        "inquiry_category_link":  (By.ID, "linkChooseCatagory"),
        "inquiry_upload_image":   (By.XPATH, "//a[contains(text(),'上传图片')]"),
        "inquiry_upload_attachment": (By.XPATH, "//a[contains(text(),'上传附件')]"),
        "inquiry_photo_sample":   (By.XPATH, "//a[contains(text(),'照片样例')]"),
        
        # 上传图片弹窗（每个视图点击后弹出独立弹窗，弹窗内仅1套 FileUpload + btnUpload）
        "dialog_file_input":   (By.ID, "FileUpload"),
        "dialog_upload_btn":   (By.ID, "btnUpload"),
        
        # 按钮（注意：备件网的按钮是 <input type="button/submit"> 不是 <button>）
        "inquiry_add_btn":        (By.ID, "btnAddItem"),
        "inquiry_save_btn":       (By.ID, "btnSave"),
        "inquiry_submit_btn":     (By.ID, "btnSubmitRequisition"),
        "inquiry_save_req_btn":   (By.ID, "btnSaveRequisition"),
        
        # 状态单选
        "inquiry_status_normal":  (By.ID, "RadNormalStatus"),
        "inquiry_status_urgent":  (By.ID, "RadUrgentStatus"),
        
        # 询价单列表/查看（selectors_report: 搜索框 id=txtRequisitionNo，按钮 id=btnSearch，无 placeholder）
        "menu_inquiry_list": (By.XPATH, "//a[normalize-space()='询价单']"),
        "inquiry_list_tab_current": (By.XPATH, "//a[normalize-space()='当前询价单']"),
        "inquiry_search_input": (By.ID, "txtRequisitionNo"),
        "inquiry_search_btn": (By.ID, "btnSearch"),
        "inquiry_status_cell": (By.XPATH, "//td[contains(text(),'已完结')] | //span[contains(text(),'已完结')]"),
        "inquiry_view_btn": (
            By.XPATH,
            "//a[contains(@class,'GridHyperLink') and contains(.,'查看')]",
        ),
        
        # 询价单详情页（查看后进入的物料明细页）
        "detail_select_all": (By.XPATH, "//input[@type='checkbox' and contains(@id,'selectAll')] | //th//input[@type='checkbox']"),
        "detail_plus_btn": (By.XPATH, "//button[contains(text(),'+')] | //span[contains(text(),'+')] | //i[contains(@class,'plus')]"),
        "detail_add_to_cart": (
            By.XPATH,
            "//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add to cart')] | "
            "//button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add to cart')] | "
            "//a[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'add to cart')] | "
            "//button[contains(text(),'加入购物车')] | //a[contains(text(),'加入购物车')] | "
            "//input[contains(@value,'加入购物车')]",
        ),
        
        # 购物车（加购后需点左侧「查看购物车>>」进入真正购物车页）
        "menu_cart": (By.XPATH, "//a[contains(text(),'购物车')] | //span[contains(text(),'购物车')]"),
        "menu_view_cart": (By.XPATH, "//a[contains(.,'查看购物车')]"),
        
        # 报价单
        "quotation_delivery_method": (
            By.XPATH,
            "//td[contains(.,'发货方式')]//select | //th[contains(.,'发货方式')]//following::select[1] | "
            "//label[contains(.,'发货方式')]/following::select[1] | "
            "//span[contains(.,'发货方式')]/ancestor::div[1]//select | "
            "//select[contains(@name,'delivery')] | //select[contains(@id,'delivery')]",
        ),
        "quotation_address": (By.XPATH, "//input[@name='address'] | //input[@placeholder='地址'] | //textarea[@name='address']"),
        "quotation_other_btn": (By.XPATH, "//button[contains(text(),'其他')]"),
        "quotation_save_btn": (
            By.XPATH,
            "//button[contains(text(),'保存')] | //button[contains(text(),'保存报价单')] | "
            "//button[contains(text(),'保存订价单')] | "
            "//input[@type='button' and (contains(@value,'保存') or contains(@value,'订价'))]",
        ),
        "quotation_no_display": (By.XPATH, "//span[contains(@class,'quotation-no')] | //div[contains(text(),'报价单号')]"),
        
        # 下载
        "order_quote_menu": (By.XPATH, "//a[contains(text(),'订单')] | //a[contains(text(),'订单报价')] | //span[contains(text(),'订单/报价')]"),
        "print_btn": (By.XPATH, "//button[contains(text(),'打印')] | //a[contains(text(),'打印')]"),
        "download_excel": (By.XPATH, "//button[contains(text(),'导出')] | //a[contains(text(),'导出')] | //button[contains(text(),'下载')]"),
    }

    def __init__(self, browser: Browser, user_config: dict):
        """
        browser: Browser实例
        user_config: 来自 user_config.py 的个人信息字典，包含:
            - spareparts_username: 备件网用户名
            - spareparts_password: 备件网密码
        """
        self.browser = browser
        self.driver = browser.driver
        self.spareparts_username = user_config.get("SPAREPARTS_USERNAME", "")
        self.spareparts_password = user_config.get("SPAREPARTS_PASSWORD", "")
        self.inquiry_numbers = []  # 记录生成的询价单号
        self.last_detail_po_factory = None  # 详情页备注 PO 下解析的工厂（中山/松江）
        self._last_cart_qty_fails = []  # 最近一次购物车数量校验失败明细
        # 主流程注入：照片上传需人工介入时回调，返回 retry | skip_uploads | inquiry_done
        self.manual_photo_pause_callback = None

    # ========================================================================
    # 1. 登录
    # ========================================================================
    def _recover_spareparts_window(self):
        """当前标签已关闭时，尝试切到仍打开的备件网标签，否则在有效标签上打开登录页。"""
        found = self.browser.find_tab_by_url_substring(
            SPAREPARTS_HOST_KEYWORD, timeout=3
        )
        if found:
            logger.info("  已切换到仍打开的备件网标签页")
            return
        self.browser.ensure_valid_window()
        logger.info("  未找到备件网标签，在当前标签导航到登录页…")
        self.driver.get(SPAREPARTS_URL)

    def _wait_spareparts_host_loaded(self, timeout=25):
        """window.open 后新标签可能仍是 about:blank，须等备件网域名出现。"""
        self.browser.ensure_valid_window()

        def _host_ready(driver):
            try:
                self.browser.ensure_valid_window()
            except Exception:
                return False
            return SPAREPARTS_HOST_KEYWORD.lower() in (driver.current_url or "").lower()

        try:
            WebDriverWait(self.driver, timeout).until(_host_ready)
        except TimeoutException:
            logger.info("  备件网标签页未自动打开，主动导航到登录页…")
            try:
                self.browser.ensure_valid_window()
                self.driver.get(SPAREPARTS_URL)
            except Exception:
                self._recover_spareparts_window()
            WebDriverWait(self.driver, timeout).until(_host_ready)
        except Exception as e:
            err = str(e).lower()
            if "no such window" in err or "web view not found" in err:
                logger.warning(f"  备件网标签已关闭，尝试恢复: {e}")
                self._recover_spareparts_window()
                WebDriverWait(self.driver, timeout).until(_host_ready)
            else:
                raise

    def _is_on_login_page(self):
        url = (self.driver.current_url or "").lower()
        if "/account/login" in url or url.rstrip("/").endswith("/login"):
            return True
        if self.browser.is_element_present(*self.SELECTORS["domain_login_link"], timeout=2):
            return True
        if self.browser.is_element_present(
            By.XPATH,
            "//*[contains(.,'Sign in with Microsoft') or contains(.,'用户登录')]",
            timeout=1,
        ):
            return True
        return False

    def _is_spareparts_logged_in(self):
        if self._is_on_login_page():
            return False
        if self.browser.is_element_present(*self.SELECTORS["sub_new_inquiry"], timeout=2):
            return True
        if self.browser.is_element_present(*self.SELECTORS["menu_spareparts"], timeout=2):
            return True
        url = (self.driver.current_url or "").lower()
        return (
            SPAREPARTS_HOST_KEYWORD.lower() in url
            and "/login" not in url
            and "/account/login" not in url
            and "permissiondenied" not in url
        )

    def _click_domain_account_login(self):
        """点击左侧「使用域账号登录>>」切换到域账号表单。"""
        try:
            link = self.driver.find_element(*self.SELECTORS["domain_login_link"])
            self.driver.execute_script("arguments[0].click();", link)
            time.sleep(1.5)
            logger.info("  ✓ 已点击「使用域账号登录」")
            return True
        except Exception as e:
            logger.warning(f"  未找到域账号登录入口: {e}")
            return False

    def _wait_login_success(self, timeout=25):
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: self._is_spareparts_logged_in() and not self._is_on_login_page()
            )
            return True
        except TimeoutException:
            return False

    def login(self):
        """
        登录备件网（使用 user_config 中的账号密码）

        注意：调用此方法前，main.py 已经在新标签页打开了备件网首页。
        不能仅凭 URL 不含 login 判断已登录（新标签 about:blank 会误判）。
        """
        logger.info("登录备件网...")
        self.browser.ensure_valid_window()

        if not self.spareparts_username or not self.spareparts_password:
            logger.error(
                "❌ 备件网账号密码未配置！请在 user_config.py 中填写 "
                "SPAREPARTS_USERNAME 和 SPAREPARTS_PASSWORD"
            )
            raise ValueError("备件网账号密码不能为空")

        self._wait_spareparts_host_loaded()

        current_url = (self.driver.current_url or "").lower()
        if "permissiondenied" in current_url:
            logger.info("  检测到拒绝访问页，导航到登录页面…")
            self.driver.get(SPAREPARTS_URL)
            time.sleep(2)
            self._wait_spareparts_host_loaded()

        if self._is_spareparts_logged_in():
            logger.info("✓ 备件网已登录（检测到业务菜单）")
            return

        if not self._is_on_login_page():
            logger.info("  当前不在登录页，导航到登录页…")
            self.driver.get(SPAREPARTS_URL)
            time.sleep(2)
            self._wait_spareparts_host_loaded()

        self._click_domain_account_login()

        if not self.browser.is_element_present(*self.SELECTORS["login_password"], timeout=8):
            self._click_domain_account_login()
            time.sleep(1)

        if not self.browser.is_element_present(*self.SELECTORS["login_password"], timeout=5):
            logger.error("❌ 未出现域账号登录表单，请检查页面是否为备件网登录页")
            raise RuntimeError("备件网登录页未加载域账号表单")

        logger.info("填写域账号密码并登录…")

        try:
            self.browser.safe_send_keys(
                *self.SELECTORS["login_username"], self.spareparts_username, timeout=10
            )
            self.browser.safe_send_keys(
                *self.SELECTORS["login_password"], self.spareparts_password, timeout=5
            )
            self.browser.safe_click(*self.SELECTORS["login_btn"], timeout=5)

            if not self._wait_login_success():
                current_url = (self.driver.current_url or "").lower()
                logger.error(f"❌ 备件网登录失败！当前仍停留在: {current_url}")
                raise RuntimeError("备件网登录失败，账号密码可能不正确")

            logger.info("✓ 备件网登录成功")
        except (RuntimeError, ValueError):
            raise
        except Exception as e:
            logger.error(f"❌ 备件网登录异常: {e}")
            raise RuntimeError(f"备件网登录失败: {e}") from e

    # ========================================================================
    # 2. 新建询价单
    # ========================================================================
    def _is_on_new_inquiry_form_page(self):
        """是否在「新建询价单」表单页（#txtProjectName 可见）。"""
        try:
            el = self.driver.find_element(By.ID, "txtProjectName")
            return el.is_displayed()
        except Exception:
            return False

    def _inquiry_form_has_material_rows(self):
        """表单下方物料表是否已有行（已点过「添加」）。"""
        try:
            return bool(
                self.driver.execute_script("""
                    var tables = document.querySelectorAll('table');
                    for (var i = 0; i < tables.length; i++) {
                        var txt = tables[i].innerText || '';
                        if (!/物料描述/.test(txt) || !/物料号/.test(txt)) continue;
                        var rows = tables[i].querySelectorAll('tbody tr');
                        for (var r = 0; r < rows.length; r++) {
                            var rt = (rows[r].innerText || '').replace(/\\s+/g, '');
                            if (rt.length > 8) return true;
                        }
                    }
                    return false;
                """)
            )
        except Exception:
            return False

    def _wait_submit_button_ready(self, timeout=30):
        """等待「提交询价单」按钮可点击。"""
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("""
                var btn = document.getElementById('btnSubmitRequisition');
                if (!btn) return false;
                var r = btn.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return false;
                if (btn.disabled) return false;
                var cls = (btn.className || '').toLowerCase();
                if (cls.indexOf('disabled') >= 0) return false;
                return true;
            """)
        )

    def _wait_inquiry_submit_complete(self, timeout=45):
        """提交后：离开编辑草稿态，或出现询价单号/列表页。"""
        end = time.time() + timeout
        while time.time() < end:
            if self._is_on_inquiry_list_page():
                return True
            inquiry_no = self.get_inquiry_number()
            if inquiry_no:
                return True
            if self._is_on_new_inquiry_form_page():
                if not self._inquiry_form_has_material_rows():
                    time.sleep(0.5)
                    continue
                time.sleep(0.8)
            else:
                return True
            time.sleep(0.6)
        return False

    def _submit_inquiry_form(self):
        """点击「提交询价单」并等待提交结果（不等待照片列 Loading）。"""
        self._wait_submit_button_ready()

        logger.info("  点击'提交询价单'...")
        btn = self.driver.find_element(By.ID, "btnSubmitRequisition")
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                btn,
            )
        except Exception:
            btn.click()
        time.sleep(1.5)

        if not self._wait_inquiry_submit_complete():
            if self._inquiry_form_has_material_rows():
                raise TimeoutException(
                    "已点击「提交询价单」，但页面仍停留在未提交的编辑态"
                )
        logger.info("  ✓ 询价单已提交")

    def navigate_to_new_inquiry(self):
        """导航到新建询价单页面"""
        if not self._is_spareparts_logged_in():
            logger.warning("  当前未登录备件网，先执行登录…")
            self.login()
        if not self._is_spareparts_logged_in():
            raise RuntimeError(
                "备件网未登录，找不到「新建询价单」。"
                "请先点「使用域账号登录」并完成域账号登录。"
            )

        if self._is_on_new_inquiry_form_page() and not self._inquiry_form_has_material_rows():
            logger.info("✓ 已在新建询价单页面（空白表单）")
            return

        if self._is_on_new_inquiry_form_page() and self._inquiry_form_has_material_rows():
            logger.warning(
                "  检测到未提交的询价单草稿（物料表已有行），尝试补点「提交询价单」…"
            )
            try:
                self._submit_inquiry_form()
                time.sleep(1)
                if not self._inquiry_form_has_material_rows():
                    logger.info("  ✓ 草稿已补交成功")
                    return
            except Exception as e:
                logger.warning(f"  补交提交失败: {e}，将尝试重新打开新建页…")

        logger.info("导航到: 备件 → 新建询价单")
        last_err = None
        for attempt in range(4):
            if self._is_on_new_inquiry_form_page() and not self._inquiry_form_has_material_rows():
                logger.info("✓ 已进入新建询价单页面")
                return

            self._js_click_menu_link("备件", visible_only=False)
            time.sleep(0.8)
            if self._js_click_menu_link("新建询价单", visible_only=True):
                time.sleep(2)
                if self._is_on_new_inquiry_form_page():
                    logger.info(f"✓ 已进入新建询价单页面（JS 菜单，第 {attempt + 1} 次）")
                    return

            self._js_click_menu_link("备件", visible_only=False)
            time.sleep(0.4)
            if self._js_click_menu_link("新建询价单", visible_only=False):
                time.sleep(2)
                if self._is_on_new_inquiry_form_page():
                    logger.info(f"✓ 已进入新建询价单页面（宽松匹配，第 {attempt + 1} 次）")
                    return

            try:
                self.browser.safe_click(*self.SELECTORS["menu_spareparts"], timeout=5)
                time.sleep(0.8)
                self.browser.safe_click(*self.SELECTORS["sub_new_inquiry"], timeout=8)
                time.sleep(2)
                if self._is_on_new_inquiry_form_page():
                    logger.info(f"✓ 已进入新建询价单页面（Selenium，第 {attempt + 1} 次）")
                    return
            except Exception as e:
                last_err = e
            time.sleep(1)

        msg = (
            "无法进入「新建询价单」页面。"
            "常见原因：上一张询价单未提交，页面仍停在编辑态。"
            "请先在备件网手动点「提交询价单」或刷新后再运行。"
        )
        logger.error(f"无法找到'新建询价单'入口: {last_err or msg}")
        raise RuntimeError(msg) from last_err

    def _fill_material_no(self, material_no):
        """填写物料号（OMS 工厂料号）；为空则不填。"""
        no = (material_no or "").strip()
        if not no:
            return
        try:
            self.browser.safe_send_keys(*self.SELECTORS["inquiry_material_no"], no)
            logger.info(f"  ✓ 物料号(工厂料号): {no}")
        except Exception as e:
            logger.warning(f"  物料号填写失败: {e}")

    def _load_default_template_photos(self):
        """每条物料无 OMS 附件时使用的模板照片（与 main._load_template_photos 一致）。"""
        from config import TEMPLATE_PHOTOS_DIR
        from utils.image_files import load_template_photo_paths

        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            TEMPLATE_PHOTOS_DIR,
        )
        return load_template_photo_paths(base)

    def _resolve_inquiry_line_photos(self, item, template_fallback=None):
        """
        解析本条物料应上传的照片。
        优先 item['photos']；为空或文件不存在时回退模板图（每条都要传，不能只传第一条）。
        """
        out = []
        for p in item.get("photos") or []:
            ap = os.path.abspath(str(p))
            if os.path.isfile(ap) and ap not in out:
                out.append(ap)
        if out:
            return out
        tpl = list(template_fallback or [])
        if not tpl:
            tpl = self._load_default_template_photos()
        if tpl:
            logger.info(
                f"  本条 OMS 无可用附件，使用 {len(tpl)} 张模板照片上传"
            )
        else:
            logger.warning(
                "  本条无照片且无模板（请检查 assets/photos/）"
            )
        return tpl

    @staticmethod
    def _resolve_item_ladder_no(item, default_ladder_no=""):
        """单条物料的梯号（优先 item 内字段）。"""
        if not item:
            return (default_ladder_no or "").strip()
        return (
            (item.get("ladder_no") or item.get("梯号") or default_ladder_no or "")
            .strip()
        )

    def _fill_inquiry_header_fields(self, project_name, ladder_no):
        """
        填写询价单表头：项目名称、梯号、项目号(WBS)。
        无项目名称时，项目名称栏填本条物料的梯号。
        """
        ladder_no = (ladder_no or "").strip()
        pn = (project_name or "").strip()
        display_name = pn or ladder_no
        logger.info(
            f"  表头: 项目名称={display_name!r}, 梯号={ladder_no!r}"
        )

        try:
            self.browser.safe_send_keys(
                *self.SELECTORS["inquiry_project_name"], display_name
            )
            logger.info(f"  ✓ 项目名称: {display_name}")
        except Exception as e:
            logger.warning(f"  项目名称填写失败: {e}")

        try:
            lift_ok = self.driver.execute_script(
                """
                var val = arguments[0];
                var el = document.querySelector('#txtLiftNo');
                if (!el) return 'not_found';
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return (el.value || '').trim() === val ? 'ok' : 'mismatch';
            """,
                ladder_no,
            )
            if lift_ok == "ok":
                logger.info(f"  ✓ 梯号(WBS梯号): {ladder_no}")
            else:
                self.browser.safe_send_keys(
                    *self.SELECTORS["inquiry_ladder_no"], ladder_no
                )
                logger.info(f"  ✓ 梯号(WBS梯号)(备用): {ladder_no}")
        except Exception as e:
            logger.warning(f"  梯号填写失败: {e}")

        try:
            result = self.driver.execute_script(
                """
                var el = document.querySelector('#lblProjectNo');
                if (!el) return 'not_found';
                el.focus();
                el.value = arguments[0];
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return 'ok';
            """,
                ladder_no,
            )
            if result == "ok":
                logger.info(f"  ✓ 项目号(WBS号): {ladder_no}")
            else:
                logger.warning("  ⚠ 项目号(WBS号)元素未找到，备用 safe_send_keys")
                self.browser.safe_send_keys(
                    *self.SELECTORS["inquiry_project_no_name"], ladder_no
                )
                logger.info(f"  ✓ 项目号(WBS号)(备用): {ladder_no}")
        except Exception as e:
            logger.warning(f"  项目号(WBS号)填写失败: {e}")

    def _fill_inquiry_row_before_material(self, project_name, ladder_no):
        """
        每条物料录入前填写表头（含梯号）。
        点击「添加」后表单会清空，第 2 条起也必须重填，不能因梯号与上一条相同而跳过。
        """
        self._fill_inquiry_header_fields(project_name, ladder_no)
        try:
            self._select_elevator_supplier_other()
        except Exception as e:
            logger.warning(f"  原电梯供应商选择失败: {e}")

    def fill_inquiry_form(
        self,
        project_name,
        ladder_no,
        material_desc,
        photos=None,
        quantity=None,
        material_no=None,
        remark=None,
    ):
        """
        填写询价单表单
        
        参数:
            project_name: 项目名称（无则用梯号代替）
            ladder_no: 梯号
            material_desc: 物料描述
            photos: 实物照片路径列表（正视图、左视图等）
            quantity: 数量（来自OMS的"数量"列）
            material_no: 物料号（来自 OMS「工厂料号」；空则跳过）
            remark: 备注（来自 OMS「备注」列；空则跳过）
        """
        logger.info(f"填写询价单: 项目={project_name}, 梯号={ladder_no}")

        self._fill_inquiry_row_before_material(project_name, ladder_no)

        # 物料描述
        try:
            self.browser.safe_send_keys(*self.SELECTORS["inquiry_material_desc"], material_desc)
            logger.info(f"  ✓ 物料描述: {material_desc[:50]}...")
        except Exception as e:
            logger.warning(f"  物料描述填写失败: {e}")

        # 物料号（OMS 工厂料号，有值才填）
        self._fill_material_no(material_no)

        # 数量（来自OMS）
        if quantity is not None and str(quantity).strip():
            try:
                self.browser.safe_send_keys(*self.SELECTORS["inquiry_amount"], str(quantity).strip())
                logger.info(f"  ✓ 数量: {quantity}")
            except Exception as e:
                logger.warning(f"  数量填写失败: {e}")

        # 备注（来自 OMS「备注」列）
        if remark and str(remark).strip():
            try:
                self.browser.safe_send_keys(
                    *self.SELECTORS["inquiry_remark"], str(remark).strip()
                )
                preview = str(remark).strip()
                if len(preview) > 60:
                    preview = preview[:60] + "…"
                logger.info(f"  ✓ 备注: {preview}")
            except Exception as e:
                logger.warning(f"  备注填写失败: {e}")

        # 上传实物照片（过小/被拒时可人工介入后继续）
        if photos:
            photo_state = self._upload_photos_with_manual_fallback(photos)
            if photo_state == "inquiry_done":
                return "inquiry_done"
            if photo_state == "skip_uploads":
                logger.info(
                    "  已跳过自动传图（假定操作员已在页面手工上传）"
                )
        else:
            logger.warning("  (无照片可上传，将触发校验提示或人工介入)")
        return "ok"

    def _upload_photos_with_manual_fallback(self, photos):
        """上传照片；遇过小/被拒时走人工暂停回调。"""
        while True:
            try:
                self._upload_photos(photos)
                return "ok"
            except PhotoUploadNeedsManualHelp as exc:
                if not self.manual_photo_pause_callback:
                    raise
                action = self.manual_photo_pause_callback(exc)
                if action == "retry":
                    logger.info("  按操作员选择：重试自动上传照片…")
                    continue
                if action == "skip_uploads":
                    logger.info("  按操作员选择：跳过自动上传（假定已在页面手工传图）")
                    return "skip_uploads"
                if action == "inquiry_done":
                    logger.info("  按操作员选择：整单已由人工提交")
                    return "inquiry_done"
                logger.warning(f"  未知回调结果 {action!r}，按 skip_uploads 处理")
                return "skip_uploads"

    def _select_elevator_supplier_other(self):
        """
        选择"原电梯供应商"下拉框为"其他"
        
        策略（按优先级）:
        1. 查找"原电梯供应商"所在区域内的隐藏 <select>，直接设置 value + 派发 change 事件
        2. 如果是 Bootstrap-select 组件，调用 .selectpicker('val', ...) API
        3. 兜底: 模拟点击 dropdown-toggle → 点击菜单项
        """
        logger.info("  选择原电梯供应商: 其他")
        try:
            # ===== 策略1+2: 直接操控底层 <select> =====
            result = self.driver.execute_script("""
                var TARGET_TEXT = '原电梯供应商';
                var TARGET_VALUE = '其他';
                
                // ---------- 辅助：在指定容器内查找包含目标文本的 <select> ----------
                function findSelectNearLabel(root) {
                    // 方式A: 父级链上存在 <select>
                    var parent = root;
                    for (var d = 0; d < 8; d++) {
                        if (!parent) break;
                        var sel = parent.querySelector('select');
                        if (sel) return sel;
                        parent = parent.parentElement;
                    }
                    // 方式B: 同级/兄弟容器中的 <select>
                    var sibling = root.nextElementSibling || root.previousElementSibling;
                    if (sibling) {
                        var sel2 = sibling.querySelector('select');
                        if (sel2) return sel2;
                        // 也许在更远的兄弟里
                        var allSiblings = root.parentElement ? root.parentElement.children : [];
                        for (var s = 0; s < allSiblings.length; s++) {
                            var sel3 = allSiblings[s].querySelector('select');
                            if (sel3) return sel3;
                        }
                    }
                    return null;
                }
                
                // ---------- 第1步: 定位标签 ----------
                var labelEl = null;
                var allElements = document.querySelectorAll('label, span, td, th, div');
                for (var i = 0; i < allElements.length; i++) {
                    var el = allElements[i];
                    if (el.children.length === 0 && el.textContent.trim().indexOf(TARGET_TEXT) >= 0) {
                        labelEl = el;
                        break;
                    }
                }
                if (!labelEl) return 'label_not_found';
                
                // ---------- 第2步: 找到关联的 <select> ----------
                var select = findSelectNearLabel(labelEl);
                
                // 如果没找到，扩大搜索：在整个 form/body 中找所有 select，匹配邻近文本
                if (!select) {
                    var allSelects = document.querySelectorAll('select');
                    for (var j = 0; j < allSelects.length; j++) {
                        var row = allSelects[j].closest('tr, .form-group, .row, .col, .form-row');
                        if (row && row.textContent.indexOf(TARGET_TEXT) >= 0) {
                            select = allSelects[j];
                            break;
                        }
                    }
                }
                
                if (!select) return 'select_not_found';
                
                var selectId = select.id || select.name || '(anonymous)';
                
                // ---------- 第3步: 找到 value='其他' 的 option ----------
                var targetOption = null;
                var options = select.options || select.querySelectorAll('option');
                for (var k = 0; k < options.length; k++) {
                    var opt = options[k];
                    var optText = (opt.textContent || opt.text || opt.label || '').trim();
                    if (optText === TARGET_VALUE) {
                        targetOption = opt;
                        break;
                    }
                }
                
                if (!targetOption) {
                    // 列出所有选项用于调试
                    var allOpts = [];
                    for (var m = 0; m < options.length; m++) {
                        allOpts.push(options[m].textContent.trim());
                    }
                    return 'option_not_found|options:[' + allOpts.join(',') + ']|select:' + selectId;
                }
                
                var newValue = targetOption.value;
                
                // ---------- 第4步: 设置值 ----------
                // 先尝试 Bootstrap-select API
                if (typeof $ !== 'undefined' && $(select).data('selectpicker')) {
                    try {
                        $(select).selectpicker('val', newValue);
                        return 'bootstrap_select_api|id=' + selectId + '|val=' + newValue;
                    } catch(bsErr) {
                        // 失败则回退
                    }
                }
                
                // 直接设置原生 select.value
                select.value = newValue;
                
                // 派发事件
                select.dispatchEvent(new Event('change', {bubbles: true}));
                select.dispatchEvent(new Event('input', {bubbles: true}));
                
                // 如果有 jQuery，通过 jQuery 触发
                if (typeof $ !== 'undefined') {
                    try { $(select).trigger('change'); } catch(e) {}
                    try { $(select).trigger('changed.bs.select'); } catch(e) {}
                }
                
                return 'direct_set|id=' + selectId + '|val=' + newValue;
            """)
            
            logger.info(f"    → {result}")
            
            if result.startswith('label_not_found'):
                logger.warning("    ⚠ 未找到'原电梯供应商'标签，跳过")
                return
            
            if result.startswith('select_not_found'):
                logger.warning("    ⚠ 未找到关联的 <select> 元素，尝试兜底点击方案")
                self._select_elevator_supplier_other_fallback()
                return
                
            if result.startswith('option_not_found'):
                logger.warning(f"    ⚠ 下拉选项中未找到'其他': {result}")
                self._select_elevator_supplier_other_fallback()
                return
            
            # 成功 — 等待一下让页面消化
            time.sleep(0.5)
            
            if result.startswith('bootstrap_select_api'):
                logger.info(f"    ✓ 通过 Bootstrap-select API 选择'其他'")
            else:
                logger.info(f"    ✓ 直接设置 <select> 值为'其他'")
                
        except Exception as e:
            logger.warning(f"    ⚠ 原电梯供应商选择异常: {e}")
            try:
                self._select_elevator_supplier_other_fallback()
            except Exception:
                pass

    def _select_elevator_supplier_other_fallback(self):
        """
        兜底方案: 模拟点击 dropdown-toggle → 点击下拉菜单中的"其他"
        （保留原点击逻辑作为最后手段）
        """
        logger.info("    [兜底] 尝试点击下拉菜单选择'其他'")
        result = self.driver.execute_script("""
            var allElements = document.querySelectorAll('*');
            for (var i = 0; i < allElements.length; i++) {
                var el = allElements[i];
                if (el.children.length === 0 && el.textContent.indexOf('原电梯供应商') >= 0) {
                    var parent = el.parentElement;
                    for (var depth = 0; depth < 5; depth++) {
                        if (!parent) break;
                        var toggle = parent.querySelector('.dropdown-toggle, [data-toggle="dropdown"]');
                        if (toggle) {
                            toggle.click();
                            return 'clicked';
                        }
                        parent = parent.parentElement;
                    }
                }
            }
            return 'not_found';
        """)
        
        if result == 'not_found':
            logger.warning("    ⚠ [兜底] 未找到下拉按钮")
            return
        
        time.sleep(1.0)
        
        clicked = self.driver.execute_script("""
            var menus = document.querySelectorAll('.dropdown-menu');
            for (var i = 0; i < menus.length; i++) {
                var menu = menus[i];
                if (menu.offsetParent !== null) {
                    var items = menu.querySelectorAll('a, li, span');
                    for (var j = 0; j < items.length; j++) {
                        if (items[j].textContent.trim() === '其他') {
                            // 模拟完整的鼠标事件序列
                            items[j].dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            items[j].dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                            items[j].click();
                            return 'ok';
                        }
                    }
                }
            }
            return 'option_not_found';
        """)
        
        if clicked == 'ok':
            time.sleep(0.3)
            logger.info(f"    ✓ [兜底] 已点击'其他'")
        else:
            logger.warning(f"    ⚠ [兜底] 未找到'其他'选项")

    def _close_all_popovers(self):
        """关闭 popover 并移除 body 下残留节点（点「添加」前用，轻量清理）。"""
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            body.click()
            time.sleep(0.2)
        except Exception:
            pass
        hide_and_remove_body_popovers(self.driver)
        time.sleep(0.25)

    def _dismiss_spareparts_hint_dialog(self):
        """关闭 layui「正视图没有上传」等提示，避免挡住上传区（自动上传前调用）。"""
        try:
            closed = self.driver.execute_script("""
                var layers = document.querySelectorAll(
                    '.layui-layer-dialog, .layui-layer'
                );
                for (var i = layers.length - 1; i >= 0; i--) {
                    var layer = layers[i];
                    var txt = (layer.innerText || '').replace(/\\s+/g, ' ');
                    if (txt.indexOf('提示') < 0 && txt.indexOf('正视图') < 0
                        && txt.indexOf('没有上传') < 0) {
                        continue;
                    }
                    var closeBtn = layer.querySelector(
                        '.layui-layer-close, .layui-layer-btn0, a'
                    );
                    if (closeBtn) {
                        closeBtn.click();
                        return true;
                    }
                }
                return false;
            """)
            if closed:
                time.sleep(0.4)
        except Exception:
            pass

    def _upload_photos(self, photos):
        """
        上传 5 个必填视图照片。实现集中在 utils.spareparts_photo_upload（勿在此重复改逻辑）。
        """
        SparepartsPhotoUploader(self.driver).upload_all(photos)

    def continue_inquiry_creation_after_manual(self):
        """
        人工介入后继续创建询价单：
        - 页面已有询价单号 → 视为已提交
        - 否则若物料表已有行 → 自动点「添加」「提交」
        返回询价单号或空字符串。
        """
        inquiry_no = (self.get_inquiry_number() or "").strip()
        if inquiry_no:
            logger.info(f"  ✓ 页面已有询价单号: {inquiry_no}")
            return inquiry_no
        if self._inquiry_form_has_material_rows():
            logger.info("  物料已在表中，程序继续点「提交询价单」…")
            try:
                self._submit_inquiry_form()
            except Exception as e:
                logger.warning(f"  自动提交失败: {e}，请确认是否已在页面手工提交")
        elif self._is_on_new_inquiry_form_page():
            logger.info("  表单已填未添加，程序继续点「添加」「提交」…")
            try:
                self.add_and_submit()
            except Exception as e:
                logger.warning(f"  自动添加/提交失败: {e}")
        inquiry_no = (self.get_inquiry_number() or "").strip()
        if inquiry_no:
            logger.info(f"  ✓ 提交后询价单号: {inquiry_no}")
        return inquiry_no

    def _peek_inquiry_hint_dialog_text(self):
        """
        检测备件网「提示」类弹窗（如：正视图没有上传）。
        返回弹窗正文摘要；无弹窗返回空字符串。不自动关闭。
        """
        try:
            raw = self.driver.execute_script(
                """
                function vis(el) {
                    if (!el) return false;
                    var r = el.getBoundingClientRect();
                    return r.width > 8 && r.height > 8;
                }
                var layers = document.querySelectorAll(
                    '.layui-layer-dialog, .layui-layer, .modal, .modal-dialog, [role="dialog"]'
                );
                for (var i = layers.length - 1; i >= 0; i--) {
                    var layer = layers[i];
                    if (!vis(layer)) continue;
                    var txt = (layer.innerText || layer.textContent || '')
                        .replace(/\\s+/g, ' ').trim();
                    if (!txt) continue;
                    if (txt.indexOf('提示') >= 0 || txt.indexOf('正视图') >= 0
                        || txt.indexOf('没有上传') >= 0
                        || (txt.indexOf('上传') >= 0 && txt.length < 120)) {
                        return txt.slice(0, 200);
                    }
                }
                return '';
                """
            )
            return (raw or "").strip()
        except Exception:
            return ""

    def _pause_on_inquiry_hint_dialog(self, context=""):
        """
        出现校验提示弹窗时暂停，交人工处理（不自动点关闭）。
        通过 manual_photo_pause_callback 与启动器/终端菜单衔接。
        返回: ok | retry | inquiry_done
        """
        hint = self._peek_inquiry_hint_dialog_text()
        if not hint:
            return "ok"

        logger.error(
            f"  备件网校验提示（{context}）: {hint} — 已暂停，请人工处理"
        )
        exc = PhotoUploadNeedsManualHelp(
            f"备件网提示: {hint}",
            view_name=context,
        )
        if not self.manual_photo_pause_callback:
            raise exc

        action = self.manual_photo_pause_callback(exc)
        if action == "retry":
            logger.info("  操作员选择重试本条物料填写…")
            return "retry"
        if action == "inquiry_done":
            return "inquiry_done"
        if self._peek_inquiry_hint_dialog_text():
            logger.warning(
                "  提示弹窗仍在页面上，请手工关闭/修正后点继续或选重试"
            )
            return "retry"
        logger.info(
            "  操作员选择继续（假定已在页面处理提示并补全表单）"
        )
        return "ok"

    def _count_inquiry_material_rows(self) -> int:
        """表单下方物料明细表已有行数（用于判断「添加」是否成功）。"""
        try:
            n = self.driver.execute_script("""
                var count = 0;
                var tables = document.querySelectorAll('table');
                for (var i = 0; i < tables.length; i++) {
                    var txt = tables[i].innerText || '';
                    if (txt.indexOf('物料描述') < 0 || txt.indexOf('物料号') < 0) {
                        continue;
                    }
                    var rows = tables[i].querySelectorAll('tbody tr');
                    for (var r = 0; r < rows.length; r++) {
                        var rt = (rows[r].innerText || '').replace(/\\s+/g, '');
                        if (rt.length > 8) count++;
                    }
                    return count;
                }
                return 0;
            """)
            return int(n or 0)
        except Exception:
            return 0

    def _click_add(self):
        """点击'添加'按钮（仅添加，不提交）"""
        logger.info("  点击'添加'...")
        try:
            self.browser.safe_click(*self.SELECTORS["inquiry_add_btn"], timeout=10)
            time.sleep(1)
            logger.info("  ✓ 已添加")
        except Exception as e:
            logger.error(f"  添加失败: {e}")
            raise

    def _click_add_resolving_upload_hint(self, context: str) -> str:
        """
        点击「添加」；若出现「正视图没有上传」等提示则暂停，处理后再重试添加。
        返回: ok | retry | inquiry_done
        """
        rows_before = self._count_inquiry_material_rows()
        while True:
            self._close_all_popovers()
            self._click_add()
            pause = self._pause_on_inquiry_hint_dialog(context)
            if pause == "retry":
                return "retry"
            if pause == "inquiry_done":
                return "inquiry_done"
            rows_after = self._count_inquiry_material_rows()
            if rows_after > rows_before:
                self._prepare_next_inquiry_line_after_add()
                return "ok"
            if self._peek_inquiry_hint_dialog_text():
                continue
            logger.info("  添加后物料表行数未增加，重试点击「添加」…")
            rows_before = rows_after

    def _prepare_next_inquiry_line_after_add(self):
        """添加一条物料后清理残留浮层/遮罩，确保下一条可点击「上传图片」。"""
        self._close_all_popovers()
        try:
            self.driver.execute_script("""
                document.querySelectorAll('.modal-backdrop, .layui-layer-shade').forEach(function(el) {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                });
            """)
        except Exception:
            pass
        deep_reset_photo_upload_popovers(self.driver)
        time.sleep(0.35)

    def add_and_submit(self):
        """点击'添加' → 直接点击'提交询价单'（不等待照片列 Loading）"""
        pause = self._click_add_resolving_upload_hint("点击添加后")
        if pause == "retry":
            raise RuntimeError("添加失败：照片校验未通过，请补传后重试")
        time.sleep(0.5)
        try:
            self._submit_inquiry_form()
        except Exception as e:
            logger.error(f"  提交失败: {e}")
            raise

    def get_inquiry_number(self):
        """从页面上提取系统生成的询价单号"""
        try:
            # 常见的几种显示方式
            xpath_options = [
                "//*[contains(text(),'询价单号')]/following-sibling::*",
                "//span[contains(@class,'inquiry-no')]",
                "//div[contains(text(),'单号')]//span",
                "//div[contains(@class,'success')]//b",
            ]
            for xpath in xpath_options:
                try:
                    elem = self.browser.driver.find_element(By.XPATH, xpath)
                    text = elem.text.strip()
                    if text and len(text) > 3:
                        # 尝试提取数字/字母组合的询价单号
                        match = re.search(r'[A-Z0-9]+[-_]?\d+', text)
                        if match:
                            return match.group()
                        return text
                except:
                    continue
            
            # 如果自动提取失败，尝试从页面全文查找
            page_text = self.browser.driver.find_element(By.TAG_NAME, "body").text
            match = re.search(r'[A-Z]{1,3}\d{6,}', page_text)
            if match:
                return match.group()
        except Exception as e:
            logger.warning(f"自动提取询价单号失败: {e}")
        
        return None

    def create_inquiry(self, project_name, ladder_no, items, photos=None, remark=None):
        """
        完整的创建询价单流程 — 支持多条物料逐条添加

        逻辑：
          - 导航到新建询价单页面
          - 每条物料使用各自的 ladder_no（items[].ladder_no）；无项目名时项目名称=该条梯号
          - 每条物料：表头+供应商+物料+数量+备注+上传照片（与第一条相同，不可省略）
          - 点击「添加」后表单清空，第 2 条起须全部重填（含正视图等照片）
          - 最后一条添加完后 → 提交询价单

        参数:
            project_name: 项目名称
            ladder_no: 组级默认梯号（item 未带 ladder_no 时兜底）
            items: 物料列表，每项含 material_desc、quantity、ladder_no、photos、remark(可选)
            photos: [兼容] 仅当 item 未带 photos 且为第一条时使用
            remark: [兼容] 仅当 item 未带 remark 且为第一条时使用
        返回: 询价单号 或 None
        """
        logger.info("-" * 40)
        ladders = [
            self._resolve_item_ladder_no(it, ladder_no)
            for it in (items or [])
        ]
        ladders = [x for x in ladders if x]
        ladder_summary = (
            ", ".join(dict.fromkeys(ladders)) if ladders else ladder_no
        )
        logger.info(
            f"创建询价单: 共 {len(items)} 条物料"
            f"（梯号: {ladder_summary or '—'}）"
        )
        logger.info("-" * 40)

        self.navigate_to_new_inquiry()
        template_photos = self._load_default_template_photos()

        for idx, item in enumerate(items):
            material_desc = item.get("material_desc", "")
            quantity = item.get("quantity", "")
            material_no = item.get("material_no", "")
            item_ladder = self._resolve_item_ladder_no(item, ladder_no)
            item_remark = (item.get("remark") or "").strip()
            if not item_remark and idx == 0 and remark:
                item_remark = (remark or "").strip()
            is_last = (idx == len(items) - 1)

            if idx > 0:
                time.sleep(0.8)

            line_ok = False
            while not line_ok:
                item_photos = self._resolve_inquiry_line_photos(
                    item, template_photos
                )
                if not item_photos and photos and idx == 0:
                    item_photos = list(photos)

                logger.info(
                    f"  填写第 {idx+1}/{len(items)} 条物料"
                    f"（梯号 {item_ladder or '—'}, 照片 {len(item_photos)} 张）..."
                )

                photo_state = self.fill_inquiry_form(
                    project_name=project_name,
                    ladder_no=item_ladder,
                    material_desc=material_desc,
                    photos=item_photos,
                    quantity=quantity,
                    material_no=material_no,
                    remark=item_remark,
                )
                if photo_state == "inquiry_done":
                    inquiry_no = self.get_inquiry_number()
                    if inquiry_no:
                        logger.info(f"✓ 询价单已由人工提交！单号: {inquiry_no}")
                        self.inquiry_numbers.append(inquiry_no)
                    else:
                        logger.warning(
                            "⚠ 人工已提交但未自动提取询价单号，请人工记录"
                        )
                    return inquiry_no

                if is_last:
                    pause = self._click_add_resolving_upload_hint(
                        f"第{idx+1}条点击添加后"
                    )
                    if pause == "retry":
                        continue
                    if pause == "inquiry_done":
                        inquiry_no = self.get_inquiry_number()
                        if inquiry_no:
                            self.inquiry_numbers.append(inquiry_no)
                        return inquiry_no
                    time.sleep(0.5)
                    try:
                        self._submit_inquiry_form()
                    except Exception as e:
                        logger.error(f"  提交失败: {e}")
                        raise
                else:
                    pause = self._click_add_resolving_upload_hint(
                        f"第{idx+1}条点击添加后"
                    )
                    if pause == "retry":
                        continue
                    if pause == "inquiry_done":
                        inquiry_no = self.get_inquiry_number()
                        if inquiry_no:
                            self.inquiry_numbers.append(inquiry_no)
                        return inquiry_no

                line_ok = True

        inquiry_no = self.get_inquiry_number()
        if inquiry_no:
            logger.info(f"✓ 询价单创建成功！单号: {inquiry_no}")
            self.inquiry_numbers.append(inquiry_no)
        else:
            logger.warning("⚠ 未能自动提取询价单号，请手动记录")

        return inquiry_no

    def _fill_material_and_quantity(self, material_desc, quantity, material_no=None):
        """仅填写物料描述、物料号(若有)、数量（用于同一询价单的第 2~N 条物料）"""
        try:
            self.browser.safe_send_keys(*self.SELECTORS["inquiry_material_desc"], material_desc)
            logger.info(f"  ✓ 物料描述: {material_desc[:50]}...")
        except Exception as e:
            logger.warning(f"  物料描述填写失败: {e}")

        self._fill_material_no(material_no)

        if quantity and str(quantity).strip():
            try:
                self.browser.safe_send_keys(*self.SELECTORS["inquiry_amount"], str(quantity).strip())
                logger.info(f"  ✓ 数量: {quantity}")
            except Exception as e:
                logger.warning(f"  数量填写失败: {e}")

    # ========================================================================
    # 3. 等待审批
    # ========================================================================
    def wait_for_approval(self, inquiry_no, max_wait=MAX_WAIT_APPROVAL):
        """
        等待询价单审批完成（状态变为'已完结'）
        
        Li, Lanying 审批后状态变为"已完结"
        
        参数:
            inquiry_no: 询价单号
            max_wait: 最大等待时间（秒）
        返回: bool 是否已完结
        """
        logger.info(f"等待询价单 {inquiry_no} 审批完成...")
        
        # 导航到询价单列表
        self.navigate_to_inquiry_list()
        
        # 搜索该询价单
        self._search_inquiry(inquiry_no)
        
        deadline = time.time() + max_wait
        
        while time.time() < deadline:
            # 检查状态
            try:
                # 查找状态列
                status_xpath = "//td[contains(text(),'已完结')]"
                if self.browser.is_element_present(By.XPATH, status_xpath, timeout=2):
                    logger.info(f"✓ 询价单 {inquiry_no} 已完结！")
                    return True
                
                # 查找其他可能的状态显示
                page_text = self.driver.find_element(By.TAG_NAME, "body").text
                if "已完结" in page_text and inquiry_no in page_text:
                    logger.info(f"✓ 询价单 {inquiry_no} 已完结！")
                    return True
                    
            except Exception:
                pass
            
            remaining = int(deadline - time.time())
            logger.info(f"  状态未完结，{remaining}秒后重试...")
            time.sleep(POLL_INTERVAL)
            
            # 刷新页面
            self.driver.refresh()
            time.sleep(2)
            self._search_inquiry(inquiry_no)
        
        logger.warning(f"⚠ 超时：询价单 {inquiry_no} 在 {max_wait}秒 内未完结")
        return False

    def check_approval_once(self, inquiry_no):
        """
        单次检查询价单是否已完结（不轮询等待，立即返回结果）
        
        用于 --resume 模式：人工确认审批完成后，批量检查所有询价单状态。
        
        参数:
            inquiry_no: 询价单号
        返回: bool 是否已完结
        """
        logger.info(f"检查询价单 {inquiry_no} 审批状态...")

        try:
            self.navigate_to_inquiry_list()
        except Exception as e:
            logger.error(f"无法进入询价单列表，无法检查审批: {e}")
            return False

        if not self._search_inquiry(inquiry_no):
            logger.error(f"搜索询价单 {inquiry_no} 失败，无法检查审批")
            return False
        
        try:
            status_xpath = f"//tr[contains(.,'{inquiry_no}')]//td[contains(.,'已完结')]"
            if self.browser.is_element_present(By.XPATH, status_xpath, timeout=5):
                logger.info(f"  ✓ 询价单 {inquiry_no} 已完结！")
                return True

            status_xpath = "//td[contains(text(),'已完结')]"
            if self.browser.is_element_present(By.XPATH, status_xpath, timeout=3):
                logger.info(f"  ✓ 询价单 {inquiry_no} 已完结！")
                return True
            
            page_text = self.driver.find_element(By.TAG_NAME, "body").text
            if "已完结" in page_text and inquiry_no in page_text:
                logger.info(f"  ✓ 询价单 {inquiry_no} 已完结！")
                return True
            
            logger.info(f"  ✗ 询价单 {inquiry_no} 尚未完结，请等待审批完成后重新运行 --resume")
            return False
        except Exception as e:
            logger.warning(f"  检查审批状态异常: {e}")
            return False

    def _wait_first_visible(self, by, value, timeout=10):
        """
        在 find_elements 结果中取第一个 is_displayed 的节点。
        解决：同一 placeholder 的 input 在 DOM 中有多份（隐藏模板 / 其它区域），
        presence 命中隐藏节点而 visibility 等待失败的问题。
        """
        end = time.time() + timeout
        while time.time() < end:
            for el in self.driver.find_elements(by, value):
                try:
                    if el.is_displayed():
                        return el
                except Exception:
                    continue
            time.sleep(0.25)
        raise TimeoutException(f"无可见元素: {by}={value!r}")

    def _is_on_inquiry_list_page(self):
        """是否已在询价单列表页（#txtRequisitionNo 可见）"""
        try:
            el = self.driver.find_element(By.ID, "txtRequisitionNo")
            return el.is_displayed()
        except Exception:
            return False

    def _js_click_menu_link(self, label: str, visible_only: bool = False) -> bool:
        """点击侧栏菜单 <a>，文案精确等于 label"""
        return bool(self.driver.execute_script("""
            var label = arguments[0];
            var visibleOnly = arguments[1];
            function norm(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var nodes = document.querySelectorAll('a');
            for (var i = 0; i < nodes.length; i++) {
                if (norm(nodes[i].textContent) !== label) continue;
                if (visibleOnly && !isVis(nodes[i])) continue;
                try { nodes[i].scrollIntoView({block: 'center', behavior: 'instant'}); } catch(e) {}
                nodes[i].click();
                return true;
            }
            return false;
        """, label, visible_only))

    def _js_click_exact_menu_link(self, label: str) -> bool:
        return self._js_click_menu_link(label, visible_only=False)

    def _js_click_inquiry_list_link(self):
        return self._js_click_menu_link("询价单", visible_only=True)

    def _js_open_inquiry_list_menu(self) -> bool:
        """
        备件 → 询价单（纯 JS，避免 Selenium stale element）。
        先展开「备件」，再点击可见的「询价单」子菜单。
        """
        for attempt in range(4):
            if self._is_on_inquiry_list_page():
                return True

            self._js_click_menu_link("备件", visible_only=False)
            time.sleep(0.8)

            if self._js_click_menu_link("询价单", visible_only=True):
                time.sleep(2)
                if self._is_on_inquiry_list_page():
                    logger.info(f"  ✓ JS 菜单导航成功（第 {attempt + 1} 次）")
                    return True

            # 子菜单可能未展开：再点一次备件后点不限制可见性的询价单
            self._js_click_menu_link("备件", visible_only=False)
            time.sleep(0.5)
            if self._js_click_menu_link("询价单", visible_only=False):
                time.sleep(2)
                if self._is_on_inquiry_list_page():
                    logger.info(f"  ✓ JS 菜单导航成功（宽松匹配，第 {attempt + 1} 次）")
                    return True

            time.sleep(1)

        return self._is_on_inquiry_list_page()

    def _ensure_inquiry_list_tab(self):
        """列表页内切换到「当前询价单」标签（若存在）"""
        try:
            tab = self._wait_first_visible(
                *self.SELECTORS["inquiry_list_tab_current"], timeout=3
            )
            self.driver.execute_script("arguments[0].click();", tab)
            time.sleep(0.5)
        except TimeoutException:
            pass

    def navigate_to_inquiry_list(self):
        """
        导航到询价单列表：备件 → 询价单 →（可选）当前询价单。
        列表搜索框为 #txtRequisitionNo（无 placeholder）。
        """
        if self._is_on_inquiry_list_page():
            logger.info("✓ 已在询价单列表页")
            self._ensure_inquiry_list_tab()
            return

        logger.info("导航到: 备件 → 询价单")

        if self._js_open_inquiry_list_menu():
            self._ensure_inquiry_list_tab()
            logger.info("✓ 已进入询价单列表（#txtRequisitionNo 可见）")
            return

        raise RuntimeError("未进入询价单列表页（无法打开 备件→询价单 或缺少 #txtRequisitionNo）")

    def _search_inquiry(self, inquiry_no):
        """
        询价单列表：在 #txtRequisitionNo 填入单号 → 点击 #btnSearch。
        返回是否搜索动作执行成功（不保证表格一定有结果行）。
        """
        logger.info(f"搜索询价单: {inquiry_no}")
        self._ensure_inquiry_list_tab()

        ok = self.driver.execute_script("""
            var no = arguments[0];
            var inp = document.getElementById('txtRequisitionNo');
            if (!inp) return false;
            inp.focus();
            inp.value = no;
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            var btn = document.getElementById('btnSearch');
            if (!btn) return false;
            btn.click();
            return true;
        """, inquiry_no)

        if not ok:
            try:
                search_input = self._wait_first_visible(
                    *self.SELECTORS["inquiry_search_input"], timeout=10
                )
                search_input.clear()
                search_input.send_keys(inquiry_no)
                btn = self._wait_first_visible(
                    *self.SELECTORS["inquiry_search_btn"], timeout=10
                )
                self.driver.execute_script("arguments[0].click();", btn)
                ok = True
            except Exception as e:
                logger.warning(f"搜索询价单失败: {e}")
                return False

        time.sleep(2)
        logger.info(f"  ✓ 已提交搜索: {inquiry_no}")
        return True

    def _is_on_inquiry_detail_page(self):
        """是否在询价单详情页（含物料分类/全选，且不在列表搜索页）"""
        return bool(self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var search = document.getElementById('txtRequisitionNo');
            if (search && isVis(search)) return false;
            var body = document.body.innerText || '';
            if (body.indexOf('物料分类') >= 0) return true;
            if (body.indexOf('当前询价单') >= 0 && body.indexOf('历史询价单') >= 0) return false;
            var hasSelectAll = body.indexOf('全选') >= 0;
            var hasAddCart = false;
            var nodes = document.querySelectorAll('input[type=button], input[type=submit], button, a');
            for (var i = 0; i < nodes.length; i++) {
                var lb = ((nodes[i].value || '') + (nodes[i].textContent || '')).toLowerCase();
                if (lb.indexOf('add to cart') >= 0 || lb.indexOf('加入购物车') >= 0) {
                    hasAddCart = true;
                    break;
                }
            }
            return hasSelectAll && hasAddCart;
        """))

    def _wait_for_inquiry_detail_page(self, timeout=15):
        """等待进入询价单详情页"""
        end = time.time() + timeout
        while time.time() < end:
            if self._is_on_inquiry_detail_page():
                return True
            time.sleep(0.5)
        return False

    def _click_inquiry_row_view(self, inquiry_no):
        """
        在搜索结果主表格中，点击操作列 GridHyperLink「修改/查看」。
        严禁匹配侧栏「查看购物车>>」等含「购物车」的链接。
        """
        clicked = self.driver.execute_script("""
            var no = arguments[0];
            function norm(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function inSidebar(el) {
                return !!(el.closest('aside, nav, .sidebar, #sidebar, .left-menu, .menu-sidebar'));
            }
            function isOpViewLink(a) {
                if (!a || inSidebar(a)) return false;
                if ((a.className || '').indexOf('GridHyperLink') < 0) return false;
                var t = norm(a.textContent).replace(/\\s+/g, '');
                if (t.indexOf('购物车') >= 0) return false;
                return t.indexOf('查看') >= 0;
            }
            var rows = document.querySelectorAll('table tbody tr');
            for (var i = 0; i < rows.length; i++) {
                var row = rows[i];
                if ((row.textContent || '').indexOf(no) < 0) continue;
                if (inSidebar(row)) continue;
                var links = row.querySelectorAll('a.GridHyperLink');
                for (var j = 0; j < links.length; j++) {
                    if (!isOpViewLink(links[j]) || !isVis(links[j])) continue;
                    try { links[j].scrollIntoView({block: 'center'}); } catch(e) {}
                    links[j].click();
                    return true;
                }
            }
            return false;
        """, inquiry_no)

        if not clicked:
            view_xpath = (
                f"//table//tbody//tr[contains(.,'{inquiry_no}')]"
                f"//a[contains(@class,'GridHyperLink') and contains(.,'查看') and not(contains(.,'购物车'))]"
            )
            try:
                view_el = self._wait_first_visible(By.XPATH, view_xpath, timeout=8)
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                    view_el,
                )
                clicked = True
            except Exception as e:
                logger.error(f"无法在列表操作列找到「查看」: {e}")
                return False

        if not self._wait_for_inquiry_detail_page(timeout=15):
            logger.error(
                f"点击「查看」后未进入详情页（当前可能仍在列表页或误开购物车页）。"
                f" URL={self.driver.current_url[:100]}"
            )
            return False

        logger.info(f"  ✓ 已进入询价单 {inquiry_no} 详情页")
        return True

    # ========================================================================
    # 4. 查看 → 购物车
    # ========================================================================
    def view_inquiry(self, inquiry_no):
        """搜索询价单 → 点击操作列「查看」→ 确认进入详情页"""
        logger.info(f"查看询价单 {inquiry_no}")

        if self._is_on_inquiry_detail_page():
            logger.info("  ✓ 已在询价单详情页，跳过重复查看")
            return

        if not self._is_on_inquiry_list_page():
            self.navigate_to_inquiry_list()
        if not self._search_inquiry(inquiry_no):
            raise RuntimeError(f"搜索询价单 {inquiry_no} 失败")
        if not self._click_inquiry_row_view(inquiry_no):
            raise RuntimeError(f"无法打开询价单 {inquiry_no} 详情页")

    def _click_detail_material_plus(self):
        """详情页：点击「物料分类」列下的 ➕ 展开备注等信息（仅限详情物料表）"""
        if not self._is_on_inquiry_detail_page():
            raise RuntimeError("当前不在询价单详情页，无法展开物料备注")

        clicked = self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function isPlus(el) {
                var t = (el.textContent || '').replace(/\\s+/g, '').trim();
                var cls = (el.className || '').toLowerCase();
                if (t === '+' || t === '＋') return true;
                if (cls.indexOf('plus') >= 0 || cls.indexOf('expand') >= 0) return true;
                if (el.tagName === 'IMG' && (el.src || '').toLowerCase().indexOf('plus') >= 0) return true;
                return false;
            }
            var tables = document.querySelectorAll('table');
            for (var ti = 0; ti < tables.length; ti++) {
                var table = tables[ti];
                var txt = table.textContent || '';
                if (txt.indexOf('物料分类') < 0 || txt.indexOf('全选') < 0) continue;
                var cells = table.querySelectorAll('tbody td, tr td');
                for (var ci = 0; ci < cells.length; ci++) {
                    var cell = cells[ci];
                    var nodes = cell.querySelectorAll('a, span, i, img, button, div');
                    for (var ni = 0; ni < nodes.length; ni++) {
                        if (isPlus(nodes[ni]) && isVis(nodes[ni])) {
                            nodes[ni].click();
                            return true;
                        }
                    }
                }
            }
            return false;
        """)
        if clicked:
            time.sleep(1.5)
            logger.info("  ✓ 已点击物料分类下的 ➕ 展开备注")
        else:
            logger.warning("  未找到物料分类 ➕，尝试继续（可能已展开）")
        return bool(clicked)

    def _read_detail_remark_po_factory(self):
        """
        读取展开后备注栏全文，从 PO 段落判断工厂：中山 / 松江。
        返回 '中山' | '松江' | None
        """
        remark_text = self.driver.execute_script("""
            var tables = document.querySelectorAll('table');
            for (var i = 0; i < tables.length; i++) {
                var t = tables[i].textContent || '';
                if (t.indexOf('备注') >= 0 || t.indexOf('PO') >= 0) return t;
            }
            return (document.body.innerText || '').substring(0, 8000);
        """) or ""

        factory = None
        po_section = remark_text
        m = re.search(r"PO[:\s：]*([\s\S]{0,500})", remark_text, re.IGNORECASE)
        if m:
            po_section = m.group(1)

        zhongshan_supplier = (OMS_SUPPLIER_ZHONGSHAN or "").strip()
        shanghai_supplier = (OMS_SUPPLIER_SHANGHAI or "").strip()

        if zhongshan_supplier and zhongshan_supplier in po_section:
            factory = "中山"
        elif shanghai_supplier and shanghai_supplier in po_section:
            factory = "松江"
        elif re.search(r"中山|中国", po_section):
            factory = "中山"
        elif re.search(r"松江|上海", po_section):
            factory = "松江"
        elif zhongshan_supplier and zhongshan_supplier in remark_text:
            factory = "中山"
        elif shanghai_supplier and shanghai_supplier in remark_text:
            factory = "松江"

        self.last_detail_po_factory = factory
        if factory:
            logger.info(f"  ✓ 备注 PO 工厂识别: {factory}")
        else:
            logger.warning("  未能从备注 PO 识别工厂（中山/松江），将沿用 inquiry_results 中的 factory_type")
        return factory

    def _select_detail_row_checkboxes(self):
        """详情页：勾选「全选」表头下方的物料行复选框"""
        if not self._is_on_inquiry_detail_page():
            raise RuntimeError("当前不在询价单详情页，无法勾选物料")

        selected = self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function checkBox(el) {
                if (!el.checked) {
                    el.click();
                    el.checked = true;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('click', {bubbles: true}));
                }
                return true;
            }
            var tables = document.querySelectorAll('table');
            for (var ti = 0; ti < tables.length; ti++) {
                var table = tables[ti];
                var txt = table.textContent || '';
                if (txt.indexOf('全选') < 0 && txt.indexOf('物料') < 0) continue;
                var boxes = table.querySelectorAll('tbody input[type=checkbox]');
                if (!boxes.length) {
                    var rows = table.querySelectorAll('tr');
                    for (var ri = 0; ri < rows.length; ri++) {
                        if ((rows[ri].textContent || '').indexOf('全选') >= 0) continue;
                        var rowBoxes = rows[ri].querySelectorAll('input[type=checkbox]');
                        for (var rb = 0; rb < rowBoxes.length; rb++) {
                            if (isVis(rowBoxes[rb])) {
                                checkBox(rowBoxes[rb]);
                                return true;
                            }
                        }
                    }
                } else {
                    for (var bi = 0; bi < boxes.length; bi++) {
                        if (isVis(boxes[bi])) {
                            checkBox(boxes[bi]);
                            return true;
                        }
                    }
                }
            }
            var any = document.querySelectorAll('table input[type=checkbox]');
            for (var k = 0; k < any.length; k++) {
                var parent = any[k].closest('th');
                if (parent) continue;
                if (isVis(any[k])) {
                    checkBox(any[k]);
                    return true;
                }
            }
            return false;
        """)
        if selected:
            logger.info("  ✓ 已勾选物料行复选框")
        else:
            raise RuntimeError("未找到全选下方的物料行复选框")
        return True

    def _click_add_to_cart_button(self):
        """详情页：点击 add to cart / 加入购物车 按钮（排除侧栏链接）"""
        if not self._is_on_inquiry_detail_page():
            raise RuntimeError("当前不在询价单详情页，无法加入购物车")

        clicked = self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function inSidebar(el) {
                return !!(el.closest('aside, nav, .sidebar, #sidebar, .left-menu, .menu-sidebar'));
            }
            function label(el) {
                return ((el.value || el.textContent || el.innerText || '') + '')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            var nodes = document.querySelectorAll(
                'input[type=button], input[type=submit], button, a'
            );
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (inSidebar(el) || !isVis(el)) continue;
                var t = label(el);
                if (t.indexOf('购物车') >= 0 && t.indexOf('add to cart') < 0) continue;
                if (t.indexOf('add to cart') >= 0 || t === 'add to cart' || t.indexOf('加入购物车') >= 0) {
                    try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                    el.click();
                    return true;
                }
            }
            var byId = document.getElementById('btnAddToCart') ||
                       document.getElementById('btnAddCart') ||
                       document.querySelector('[id*="AddToCart" i], [id*="AddCart" i]');
            if (byId && isVis(byId) && !inSidebar(byId)) {
                byId.click();
                return true;
            }
            return false;
        """)
        if not clicked:
            try:
                btn = self._wait_first_visible(*self.SELECTORS["detail_add_to_cart"], timeout=8)
                self.driver.execute_script("arguments[0].click();", btn)
                clicked = True
            except Exception as e:
                raise RuntimeError(f"未找到 add to cart / 加入购物车 按钮: {e}") from e

        time.sleep(2)
        logger.info("  ✓ 已点击 add to cart / 加入购物车")
        return True

    def select_all_and_add_to_cart(self):
        """
        询价单详情页（用户实际操作顺序）：
        1. 物料分类下点击 ➕ 展开
        2. 读取备注栏 PO 工厂信息（中山/松江）
        3. 勾选全选下方的物料行复选框
        4. 点击 add to cart
        """
        logger.info("详情页：展开备注 → 勾选物料 → 加入购物车...")
        time.sleep(1)

        if not self._is_on_inquiry_detail_page():
            raise RuntimeError("未在询价单详情页，请先成功点击列表中的「查看」")

        self._click_detail_material_plus()
        self._read_detail_remark_po_factory()
        self._select_detail_row_checkboxes()
        self._click_add_to_cart_button()

    def ensure_detail_po_factory(self, inquiry_no):
        """
        读取详情页备注 PO 工厂（中山/松江）。购物车已有物料跳过加购时也会调用。
        """
        if self.last_detail_po_factory:
            return self.last_detail_po_factory
        try:
            self.view_inquiry(inquiry_no)
            self._click_detail_material_plus()
            return self._read_detail_remark_po_factory()
        except Exception as e:
            logger.warning(f"  读取备注 PO 工厂失败: {e}")
            return None

    def _is_on_cart_page(self):
        """是否在购物车列表页（含产品描述/数量等）"""
        return bool(self.driver.execute_script("""
            var b = document.body.innerText || '';
            if (b.indexOf('产品描述') < 0) return false;
            if (b.indexOf('数量') < 0 && b.indexOf('數量') < 0) return false;
            return true;
        """))

    def _js_click_view_cart_sidebar(self):
        """点击左侧「查看购物车」链接（含 >> 变体）"""
        return bool(self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var as = document.querySelectorAll('a');
            for (var i = 0; i < as.length; i++) {
                var t = (as[i].textContent || '').replace(/\\s+/g, '');
                if (t.indexOf('查看购物车') < 0) continue;
                if (!isVis(as[i])) continue;
                try { as[i].scrollIntoView({block: 'center'}); } catch(e) {}
                as[i].click();
                return true;
            }
            return false;
        """))

    def navigate_to_cart(self):
        """加购后：点击左侧「查看购物车」进入购物车页（不是仅点「购物车」菜单）"""
        logger.info("进入购物车（左侧「查看购物车」）...")
        if self._is_on_cart_page():
            logger.info("✓ 已在购物车页面")
            return

        if not self._js_click_view_cart_sidebar():
            try:
                self.browser.safe_click(*self.SELECTORS["menu_view_cart"], timeout=8)
            except Exception:
                logger.warning("  未找到「查看购物车」，尝试点击「购物车」菜单…")
                self.browser.safe_click(*self.SELECTORS["menu_cart"], timeout=10)

        time.sleep(2)
        if not self._is_on_cart_page():
            raise RuntimeError("未进入购物车页面：请点击左侧「查看购物车」或检查页面是否加载完成")
        if "ShoppingCart" in (self.driver.current_url or ""):
            time.sleep(1.2)
        logger.info("✓ 已进入购物车页面")
