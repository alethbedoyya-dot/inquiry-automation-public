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


class SparePartsQuotationMixin:
    def _wait_for_quotation_edit_form(self, timeout=25):
        """等待进入报价单编辑页（含发货方式/保存等）"""
        end = time.time() + timeout
        while time.time() < end:
            ok = self.driver.execute_script("""
                var t = document.body.innerText || '';
                if (t.indexOf('发货方式') >= 0) return true;
                if (t.indexOf('收货地址') >= 0 || t.indexOf('收货') >= 0) return true;
                if (t.indexOf('保存') >= 0 && t.indexOf('订价') >= 0) return true;
                if (t.indexOf('保存') >= 0 && (t.indexOf('报价') >= 0 || t.indexOf('保存报价单') >= 0)) return true;
                return false;
            """)
            if ok:
                return True
            time.sleep(0.4)
        return False

    def _js_quotation_select_option_keyword(self, *keywords):
        """在可见的 select 中选第一个 option 文案包含任一 keyword 的项"""
        return bool(
            self.driver.execute_script(
                """
            var kws = arguments[0];
            function matchOpt(opt) {
                var t = (opt.text || opt.innerText || '').replace(/\\s+/g, '');
                for (var k = 0; k < kws.length; k++) {
                    if (t.indexOf(kws[k]) >= 0) return true;
                }
                return false;
            }
            var sels = document.querySelectorAll('select');
            for (var i = 0; i < sels.length; i++) {
                var s = sels[i];
                var r = s.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                var opts = s.options;
                for (var j = 0; j < opts.length; j++) {
                    if (matchOpt(opts[j])) {
                        s.selectedIndex = j;
                        s.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
            }
            return false;
        """,
                list(keywords),
            )
        )

    def _js_quotation_fill_visible_address(self, text):
        """填写第一个可见的地址类输入框"""
        return bool(
            self.driver.execute_script(
                """
            var txt = arguments[0];
            var els = document.querySelectorAll('input, textarea');
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                if (el.type === 'hidden' || el.type === 'checkbox' || el.type === 'radio') continue;
                var r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                var ph = (el.placeholder || '') + (el.name || '') + (el.id || '');
                if (ph.indexOf('地址') >= 0 || ph.toLowerCase().indexOf('address') >= 0) {
                    el.focus();
                    el.value = txt;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            return false;
        """,
                text,
            )
        )

    def _js_quotation_click_save(self):
        """点击保存/保存报价单/保存订价单（含 div[role=button]、span 等）"""
        return bool(
            self.driver.execute_script("""
            var nodes = document.querySelectorAll(
                'button, input[type=button], input[type=submit], a, div[role=button], span');
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                var r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                var t = ((el.value || '') + (el.textContent || el.innerText || '')).replace(/\\s+/g, '');
                if (t.indexOf('保存订价单') >= 0 || t.indexOf('保存报价单') >= 0
                    || (t.indexOf('保存') >= 0 && t.indexOf('订价') >= 0)
                    || (t.indexOf('保存') >= 0 && t.indexOf('报价') >= 0)) {
                    try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                    try { el.click(); } catch(e2) {}
                    return true;
                }
            }
            return false;
        """)
        )

    # ========================================================================
    # 5. 生成报价单
    # ========================================================================

    def _songjiang_fill_address_orderpreview(self):
        """
        松江 OrderPreview：两个「请选择」下拉均选上海市；txtAddress 详细地址不填。
        """
        logger.info("  松江 OrderPreview：省/市下拉选「上海市」，详细地址留空")
        prov_hints = self._zhongshan_province_hint_list("上海市")
        city_hints = self._zhongshan_city_hint_list("上海市")
        ok_p = self._orderpreview_select_province(prov_hints)
        if ok_p:
            self._zhongshan_wait_ddl_city_populated(
                city_hints=city_hints, min_options=2, timeout=22.0
            )
        ok_c = self._orderpreview_select_city(city_hints) if city_hints else False
        try:
            addr_el = self.driver.find_element(By.ID, "txtAddress")
            self.driver.execute_script("arguments[0].value='';", addr_el)
            addr_el.clear()
        except Exception:
            pass
        detail = ""
        try:
            detail = (
                self.driver.find_element(By.ID, "txtAddress").get_attribute("value")
                or ""
            ).strip()
        except Exception:
            pass
        if detail:
            logger.warning(f"  松江：详细地址框应留空，当前仍有「{detail[:20]}」，已尝试清空")
        if ok_p and ok_c:
            logger.info("  ✓ 松江收货地址: 省/市=上海市，详细地址留空")
        else:
            logger.warning(f"  松江省/市下拉: 省={ok_p} 市={ok_c}")
        return ok_p and ok_c and not detail

    def _js_orderpreview_delivery_self_pickup_state(self):
        """OrderPreview：读取发货方式区当前选中项文案，并判断是否已为「到工厂自提」。"""
        try:
            return (
                self.driver.execute_script(
                    """
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function rectCenterY(r) { return (r.top + r.bottom) / 2; }
            function labelAfterRadio(inp) {
                if (!inp) return '';
                var n = inp.nextElementSibling;
                if (n && (n.tagName || '').toUpperCase() === 'LABEL') {
                    return (n.textContent || '').trim();
                }
                if (inp.id) {
                    var lb = document.querySelector('label[for="' + inp.id + '"]');
                    if (lb) return (lb.textContent || '').trim();
                }
                return (inp.value || '').trim();
            }
            function zsShipSelectedLabel() {
                var pick = document.getElementById('rbtnShipMethod_106');
                if (pick && pick.checked) return labelAfterRadio(pick) || '到工厂自提';
                var box = document.getElementById('rbtnShipMethod');
                if (box) {
                    var radios = box.querySelectorAll('input[type=radio]');
                    for (var i = 0; i < radios.length; i++) {
                        var inp = radios[i];
                        if (!inp.checked || !vis(inp)) continue;
                        return labelAfterRadio(inp);
                    }
                }
                return '';
            }
            function selfPickupOk(label) {
                var sel = norm(label || '');
                if (!sel) return false;
                if (sel.indexOf('整车') >= 0 && sel.indexOf('到工厂自提') < 0) return false;
                return sel.indexOf('到工厂自提') >= 0;
            }
            function headingRect(keyword) {
                var best = null, bestArea = 1e12;
                var cells = document.querySelectorAll('td, label, th, span, div, p');
                for (var i = 0; i < cells.length; i++) {
                    var tx = (cells[i].textContent || '').replace(/\\s+/g, ' ').trim();
                    if (tx.indexOf(keyword) < 0) continue;
                    if (tx.length > 80) continue;
                    var r = cells[i].getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    var area = r.width * r.height;
                    if (area < bestArea) { bestArea = area; best = r; }
                }
                return best;
            }
            function labelForRadio(inp) {
                if (!inp) return '';
                if (inp.id) {
                    var lb = document.querySelector('label[for="' + inp.id + '"]');
                    if (lb) return (lb.textContent || '').trim();
                }
                var p = inp.parentElement;
                for (var d = 0; d < 6 && p; d++, p = p.parentElement) {
                    var kids = p.children || [];
                    var parts = [];
                    for (var k = 0; k < kids.length; k++) {
                        var tx = (kids[k].textContent || '').trim();
                        if (tx && tx.length <= 24) parts.push(tx);
                    }
                    if (parts.length === 1) return parts[0];
                    if (parts.length > 1 && parts.join('').length <= 24) return parts.join('');
                }
                var n = inp.nextElementSibling;
                if (n) return (n.textContent || '').trim();
                return (inp.value || '').trim();
            }
            function selectedDeliveryLabel() {
                var head = headingRect('发货方式');
                var hy = head ? rectCenterY(head) : null;
                var radios = document.querySelectorAll('input[type=radio]');
                for (var j = 0; j < radios.length; j++) {
                    var inp = radios[j];
                    if (!inp.checked || !vis(inp)) continue;
                    var ir = inp.getBoundingClientRect();
                    if (hy !== null && Math.abs(rectCenterY(ir) - hy) > 55) continue;
                    return labelForRadio(inp);
                }
                return '';
            }
            function textRects(word) {
                var out = [];
                var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                var n;
                while ((n = walker.nextNode())) {
                    var raw = n.nodeValue || '';
                    var ix = raw.indexOf(word);
                    while (ix >= 0) {
                        try {
                            var range = document.createRange();
                            range.setStart(n, ix);
                            range.setEnd(n, ix + word.length);
                            var r = range.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) out.push(r);
                        } catch (e) {}
                        ix = raw.indexOf(word, ix + word.length);
                    }
                }
                return out;
            }
            function clickDeliveryExact(word) {
                var head = headingRect('发货方式');
                var hy = head ? rectCenterY(head) : null;
                var rects = textRects(word);
                var radios = Array.prototype.slice.call(document.querySelectorAll('input[type=radio]'));
                for (var ri = 0; ri < rects.length; ri++) {
                    var tr = rects[ri];
                    if (hy !== null && Math.abs(rectCenterY(tr) - hy) > 50) continue;
                    for (var j = 0; j < radios.length; j++) {
                        var inp = radios[j];
                        if (!vis(inp)) continue;
                        var ir = inp.getBoundingClientRect();
                        if (Math.abs(rectCenterY(ir) - rectCenterY(tr)) > 12) continue;
                        var dx = tr.left - ir.right;
                        if (dx < -8 || dx > 72) continue;
                        var lbl = norm(labelForRadio(inp));
                        if (lbl.indexOf(norm(word)) < 0) continue;
                        if (lbl.indexOf('整车') >= 0 && lbl.indexOf('到工厂自提') < 0) continue;
                        try { inp.scrollIntoView({block: 'center'}); } catch (e0) {}
                        if (!inp.checked) inp.click();
                        inp.checked = true;
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }
            function selfPickupOk(label) {
                var sel = norm(label || selectedDeliveryLabel());
                if (!sel) return false;
                if (sel.indexOf('整车') >= 0 && sel.indexOf('到工厂自提') < 0) return false;
                return sel.indexOf('到工厂自提') >= 0;
            }
            var selected = zsShipSelectedLabel() || selectedDeliveryLabel();
            return {
                selected: selected,
                ok: selfPickupOk(selected)
            };
            """
                )
                or {}
            )
        except Exception:
            return {}

    def _js_orderpreview_click_delivery_self_pickup(self):
        """点击「到工厂自提」单选并返回点击后状态。"""
        try:
            return (
                self.driver.execute_script(
                    """
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function rectCenterY(r) { return (r.top + r.bottom) / 2; }
            function headingRect(keyword) {
                var best = null, bestArea = 1e12;
                var cells = document.querySelectorAll('td, label, th, span, div, p');
                for (var i = 0; i < cells.length; i++) {
                    var tx = (cells[i].textContent || '').replace(/\\s+/g, ' ').trim();
                    if (tx.indexOf(keyword) < 0) continue;
                    if (tx.length > 80) continue;
                    var r = cells[i].getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    var area = r.width * r.height;
                    if (area < bestArea) { bestArea = area; best = r; }
                }
                return best;
            }
            function labelForRadio(inp) {
                if (!inp) return '';
                if (inp.id) {
                    var lb = document.querySelector('label[for="' + inp.id + '"]');
                    if (lb) return (lb.textContent || '').trim();
                }
                var p = inp.parentElement;
                for (var d = 0; d < 6 && p; d++, p = p.parentElement) {
                    var kids = p.children || [];
                    var parts = [];
                    for (var k = 0; k < kids.length; k++) {
                        var tx = (kids[k].textContent || '').trim();
                        if (tx && tx.length <= 24) parts.push(tx);
                    }
                    if (parts.length === 1) return parts[0];
                }
                var n = inp.nextElementSibling;
                if (n) return (n.textContent || '').trim();
                return (inp.value || '').trim();
            }
            function selectedDeliveryLabel() {
                var head = headingRect('发货方式');
                var hy = head ? rectCenterY(head) : null;
                var radios = document.querySelectorAll('input[type=radio]');
                for (var j = 0; j < radios.length; j++) {
                    var inp = radios[j];
                    if (!inp.checked || !vis(inp)) continue;
                    var ir = inp.getBoundingClientRect();
                    if (hy !== null && Math.abs(rectCenterY(ir) - hy) > 55) continue;
                    return labelForRadio(inp);
                }
                return '';
            }
            function textRects(word) {
                var out = [];
                var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                var n;
                while ((n = walker.nextNode())) {
                    var raw = n.nodeValue || '';
                    var ix = raw.indexOf(word);
                    while (ix >= 0) {
                        try {
                            var range = document.createRange();
                            range.setStart(n, ix);
                            range.setEnd(n, ix + word.length);
                            var r = range.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) out.push(r);
                        } catch (e) {}
                        ix = raw.indexOf(word, ix + word.length);
                    }
                }
                return out;
            }
            function selfPickupOk(label) {
                var sel = norm(label || '');
                if (!sel) return false;
                if (sel.indexOf('整车') >= 0 && sel.indexOf('到工厂自提') < 0) return false;
                return sel.indexOf('到工厂自提') >= 0;
            }
            function clickZsSelfPickupRadio() {
                var el = document.getElementById('rbtnShipMethod_106');
                if (!el) return false;
                try { el.scrollIntoView({block: 'center'}); } catch (e0) {}
                if (!el.checked) el.click();
                el.checked = true;
                try {
                    if (typeof ShipMethodChangeZS === 'function') ShipMethodChangeZS(106);
                } catch (e1) {}
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('input', {bubbles: true}));
                return !!el.checked;
            }
            function zsShipSelectedLabel() {
                var pick = document.getElementById('rbtnShipMethod_106');
                if (pick && pick.checked) {
                    var n = pick.nextElementSibling;
                    return n ? (n.textContent || '').trim() : '到工厂自提';
                }
                var box = document.getElementById('rbtnShipMethod');
                if (box) {
                    var radios = box.querySelectorAll('input[type=radio]');
                    for (var i = 0; i < radios.length; i++) {
                        if (radios[i].checked) return labelForRadio(radios[i]);
                    }
                }
                return '';
            }
            if (clickZsSelfPickupRadio()) {
                var sel0 = zsShipSelectedLabel();
                return { clicked: true, selected: sel0, ok: selfPickupOk(sel0) };
            }
            var word = '到工厂自提';
            var head = headingRect('发货方式');
            var hy = head ? rectCenterY(head) : null;
            var rects = textRects(word);
            var radios = Array.prototype.slice.call(document.querySelectorAll('input[type=radio]'));
            var clicked = false;
            for (var ri = 0; ri < rects.length; ri++) {
                var tr = rects[ri];
                if (hy !== null && Math.abs(rectCenterY(tr) - hy) > 50) continue;
                for (var j = 0; j < radios.length; j++) {
                    var inp = radios[j];
                    if (!vis(inp)) continue;
                    var ir = inp.getBoundingClientRect();
                    if (Math.abs(rectCenterY(ir) - rectCenterY(tr)) > 12) continue;
                    var dx = tr.left - ir.right;
                    if (dx < -8 || dx > 72) continue;
                    var lbl = norm(labelForRadio(inp));
                    if (lbl.indexOf(norm(word)) < 0) continue;
                    if (lbl.indexOf('整车') >= 0 && lbl.indexOf('到工厂自提') < 0) continue;
                    try { inp.scrollIntoView({block: 'center'}); } catch (e0) {}
                    if (!inp.checked) inp.click();
                    inp.checked = true;
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    clicked = true;
                    break;
                }
                if (clicked) break;
            }
            var selected = zsShipSelectedLabel() || selectedDeliveryLabel();
            return { clicked: clicked, selected: selected, ok: selfPickupOk(selected) };
            """
                )
                or {}
            )
        except Exception:
            return {}

    def _songjiang_orderpreview_delivery_self_pickup_ok(self):
        st = self._js_orderpreview_delivery_self_pickup_state()
        return bool(st.get("ok"))

    def _songjiang_select_delivery_self_pickup_orderpreview(self):
        """OrderPreview：精确选「到工厂自提」，并读回校验（避免误点「整车」）。"""
        for attempt in range(2):
            st = self._js_orderpreview_delivery_self_pickup_state()
            if st.get("ok"):
                logger.info(
                    f"  ✓ 松江发货方式: 到工厂自提（当前: {st.get('selected', '')}）"
                )
                return True
            self._js_orderpreview_click_delivery_self_pickup()
            time.sleep(0.35)
            st2 = self._js_orderpreview_delivery_self_pickup_state()
            if st2.get("ok"):
                logger.info(
                    f"  ✓ 松江发货方式: 到工厂自提（当前: {st2.get('selected', '')}）"
                )
                return True
            if attempt == 0:
                logger.warning(
                    f"  松江发货方式第 {attempt + 1} 次未选到「到工厂自提」"
                    f"（当前: {st2.get('selected', '') or '未识别'}），重试…"
                )
        logger.warning(
            "  松江发货方式未能选为「到工厂自提」，请人工核对（勿选整车）"
        )
        return False

    def _songjiang_orderpreview_required_state(self):
        """松江 OrderPreview 保存前必填项状态。"""
        try:
            base = self._zhongshan_orderpreview_required_state() or {}
        except Exception:
            base = {}
        try:
            extra = self.driver.execute_script(
                """
            function val(id) {
                var el = document.getElementById(id);
                return el ? (el.value || '').trim() : '';
            }
            function selectLabel(id) {
                var s = document.getElementById(id);
                if (!s) return '';
                try {
                    return (s.options[s.selectedIndex] && s.options[s.selectedIndex].text || '').trim();
                } catch (e) { return (s.value || '').trim(); }
            }
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function recipientOk() {
                var ln = val('txtLastName');
                var fn = val('txtFirstName');
                var tel = val('txtTel');
                var compact = function(s) { return (s || '').replace(/\\s+/g, ''); };
                var lnC = compact(ln);
                var fnC = compact(fn);
                var telC = compact(tel);
                var telDig = telC.replace(/\\D/g, '');
                var blob = lnC + fnC + telC;
                if (telDig.length >= 8 && (lnC || fnC)) return true;
                if (/1\\d{10}/.test(blob)) return true;
                if (lnC && lnC.replace(/\\D/g, '').length >= 8) return true;
                return false;
            }
            var prov = norm(selectLabel('ddlProvince'));
            var city = norm(selectLabel('ddlCity'));
            var addressOk = prov.indexOf('上海') >= 0 && city.indexOf('上海') >= 0 && !val('txtAddress');
            return { recipient: recipientOk(), address: addressOk };
            """
            ) or {}
        except Exception:
            extra = {}
        delivery_ok = self._songjiang_orderpreview_delivery_self_pickup_ok()
        return {
            "recipient": bool(extra.get("recipient")),
            "address": bool(extra.get("address")),
            "delivery": delivery_ok,
            "project": bool(base.get("project")),
            "material_other": bool(base.get("material_other")),
        }

    def _songjiang_orderpreview_refill_required(
        self, project_name, ladder_no, oms_recipient=""
    ):
        """保存前复核：项目名称联动可能清空收货人/地址/发货方式，缺则补填。"""
        state = self._songjiang_orderpreview_required_state()
        missing = [k for k, v in (state or {}).items() if not v]
        if not missing:
            logger.info("  ✓ 松江 OrderPreview 保存前必填项复核通过")
            return True

        logger.warning(f"  松江 OrderPreview 保存前缺字段: {missing}，开始补填")
        if not state.get("project"):
            self._fill_quotation_project_name_extended(project_name, ladder_no)
            time.sleep(0.6)

        recipient = (oms_recipient or "").strip()
        state = self._songjiang_orderpreview_required_state()
        if not state.get("recipient"):
            if recipient:
                self._zhongshan_fill_recipient_orderpreview(recipient)
            else:
                logger.warning("  松江：无收货人(技术员+联系方式)可补填")
            time.sleep(0.3)

        state = self._songjiang_orderpreview_required_state()
        if not state.get("address"):
            if not self._songjiang_fill_address_orderpreview():
                return False
            time.sleep(0.4)

        state = self._songjiang_orderpreview_required_state()
        if not state.get("delivery"):
            if not self._songjiang_select_delivery_self_pickup_orderpreview():
                return False
            time.sleep(0.3)

        state = self._songjiang_orderpreview_required_state()
        if not state.get("material_other"):
            self._zhongshan_check_material_purchase_other()
            time.sleep(0.3)

        state2 = self._songjiang_orderpreview_required_state()
        missing2 = [k for k, v in (state2 or {}).items() if not v]
        if missing2:
            logger.warning(f"  松江 OrderPreview 补填后仍缺: {missing2}")
            return False
        logger.info("  ✓ 松江 OrderPreview 补填后必填项复核通过")
        return True

    def _songjiang_save_quotation(self):
        """保存订价单：优先 JS（兼容 div[role=button]），失败再 Selenium。"""
        time.sleep(0.4)
        if self._js_quotation_click_save():
            time.sleep(2)
            logger.info("  ✓ 报价单已保存(JS)")
            return
        try:
            self.browser.safe_click(*self.SELECTORS["quotation_save_btn"], timeout=12)
            time.sleep(2)
            logger.info("  ✓ 报价单已保存")
        except Exception as e:
            logger.warning(f"  保存报价单失败: {e}")
            if self._js_quotation_click_save():
                time.sleep(2)
                logger.info("  ✓ 报价单已保存(JS补救)")
                return
            raise

    def generate_songjiang_quotation(
        self, project_name, ladder_no, oms_recipient=None
    ):
        """
        生成松江报价单
        
        松江规则（PPT第13页）：
        - 收货人：OMS 技术员 + 联系方式
        - 只要是松江工厂报的价，不管是否直发，都要填「到工厂自提」
        - 收货地址：省/市选上海市；详细地址(txtAddress)不填（OrderPreview）
        - 写项目名/梯号
        - 点"其他"
        - 点"保存"
        """
        logger.info(f"生成松江报价单: {project_name}/{ladder_no}")
        recipient = (oms_recipient or "").strip()

        if self._zhongshan_is_order_preview_page():
            logger.info("  检测到 OrderPreview 表单，使用松江专用逻辑")
            logger.info("  [松江 1/5] 项目名称（先填，避免联动清空后续字段）")
            self._fill_quotation_project_name_extended(project_name, ladder_no)
            time.sleep(0.6)

            logger.info("  [松江 2/5] 收货人（技术员+联系方式）")
            if recipient:
                if not self._zhongshan_fill_recipient_orderpreview(recipient):
                    raise RuntimeError(
                        "松江报价单：收货人/电话填写失败，请核对 OMS 技术员字段。"
                    )
            else:
                logger.warning(
                    "  松江：未提供 oms_recipient（技术员+联系方式），"
                    "报价页收货人将为空"
                )
            time.sleep(0.45)

            logger.info("  [松江 3/5] 收货地址（省/市=上海市，详细地址留空）")
            if not self._songjiang_fill_address_orderpreview():
                raise RuntimeError(
                    "松江报价单：OrderPreview 省/市未选为上海市，或详细地址框未清空。"
                )
            time.sleep(0.5)

            logger.info("  [松江 4/5] 发货方式 → 到工厂自提")
            if not self._songjiang_select_delivery_self_pickup_orderpreview():
                raise RuntimeError(
                    "松江报价单：发货方式未能选为「到工厂自提」（当前可能仍为整车），已中止保存。"
                )
            time.sleep(0.35)

            logger.info("  [松江 5/5] 物料购买类别 → 其他；保存前复核")
            self._zhongshan_check_material_purchase_other()
            time.sleep(0.35)

            if not self._songjiang_orderpreview_refill_required(
                project_name, ladder_no, recipient
            ):
                raise RuntimeError(
                    "松江报价单：保存前必填项（收货人/地址/发货方式/项目/物料类别）未通过复核。"
                )

            self._songjiang_save_quotation()
            return self.get_quotation_number()
        
        # 发货方式 → 工厂自提
        try:
            delivery_dropdown = self.browser.wait_for_clickable(
                *self.SELECTORS["quotation_delivery_method"], timeout=10
            )
            delivery_dropdown.click()
            time.sleep(0.5)
            # 选择"工厂自提"
            self_pickup = self.driver.find_element(
                By.XPATH, "//option[contains(text(),'自提') or contains(text(),'工厂自提')]"
            )
            self_pickup.click()
            logger.info("  ✓ 发货方式: 工厂自提")
        except Exception as e:
            logger.warning(f"  发货方式设置失败: {e}")
            if self._js_quotation_select_option_keyword("自提", "工厂自提"):
                logger.info("  ✓ 发货方式(JS补救): 自提/工厂自提")

        # 收货地址 → 上海市（填两个就行）
        try:
            address_input = self.browser.wait_for_element(
                *self.SELECTORS["quotation_address"], timeout=10
            )
            address_input.clear()
            address_input.send_keys("上海市")
            logger.info("  ✓ 收货地址: 上海市")
        except Exception as e:
            logger.warning(f"  地址填写失败: {e}")
            if self._js_quotation_fill_visible_address("上海市"):
                logger.info("  ✓ 收货地址(JS补救): 上海市")

        # 填写项目名/梯号
        self._fill_quotation_project_info(project_name, ladder_no)

        # 点"其他"
        try:
            self.browser.safe_click(*self.SELECTORS["quotation_other_btn"], timeout=10)
            logger.info("  ✓ 已点击'其他'")
        except:
            logger.info("  (无'其他'按钮或已跳过)")

        # 点"保存"
        try:
            self.browser.safe_click(*self.SELECTORS["quotation_save_btn"], timeout=10)
            time.sleep(2)
            logger.info("  ✓ 报价单已保存")
        except Exception as e:
            logger.warning(f"  保存报价单失败: {e}")
            if self._js_quotation_click_save():
                time.sleep(2)
                logger.info("  ✓ 报价单已保存(JS补救)")
            else:
                raise

        return self.get_quotation_number()

    # bootstrap-select 隐藏原生 select：须 selectpicker('val') 才能联动并更新 UI
    _JS_BOOTSTRAP_SELECT_APPLY = """
        function bootstrapSelectApply(sel, hints) {
            if (!sel || !sel.options || !hints || !hints.length) return false;
            function skipOptionText(tx) {
                tx = (tx || '').replace(/\\s+/g, '');
                return !tx || tx.indexOf('请选择') >= 0;
            }
            function applyIndex(j) {
                var optVal = sel.options[j].value;
                sel.selectedIndex = j;
                sel.value = optVal;
                try {
                    if (typeof jQuery !== 'undefined') {
                        var $s = jQuery(sel);
                        if ($s.data('selectpicker')) {
                            $s.selectpicker('val', optVal);
                            $s.trigger('changed.bs.select');
                            return true;
                        }
                    }
                } catch (e) {}
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                sel.dispatchEvent(new Event('input', {bubbles: true}));
                try {
                    if (typeof jQuery !== 'undefined' && jQuery(sel).selectpicker) {
                        jQuery(sel).selectpicker('refresh');
                        jQuery(sel).trigger('changed.bs.select');
                    }
                } catch (e2) {}
                return true;
            }
            for (var hi = 0; hi < hints.length; hi++) {
                var h = (hints[hi] || '').replace(/\\s+/g, '');
                if (!h) continue;
                for (var j = 0; j < sel.options.length; j++) {
                    var tx = (sel.options[j].text || '').replace(/\\s+/g, '');
                    var val = (sel.options[j].value || '').replace(/\\s+/g, '');
                    if (skipOptionText(tx)) continue;
                    if (tx.indexOf(h) >= 0 || val.indexOf(h) >= 0) return applyIndex(j);
                }
                if (h.length >= 2) {
                    var sh = h.slice(0, 2);
                    for (var j = 0; j < sel.options.length; j++) {
                        var tx2 = (sel.options[j].text || '').replace(/\\s+/g, '');
                        if (skipOptionText(tx2)) continue;
                        if (tx2.indexOf(sh) >= 0) return applyIndex(j);
                    }
                }
            }
            return false;
        }
        var sel = document.getElementById(arguments[0]);
        return bootstrapSelectApply(sel, arguments[1] || []);
    """

    _JS_BOOTSTRAP_SELECT_LABEL = """
        var sel = document.getElementById(arguments[0]);
        if (!sel || sel.selectedIndex < 0) return '';
        return (sel.options[sel.selectedIndex].text || '').trim();
    """

    def _js_bootstrap_select_apply_hints(self, element_id, hints):
        if not hints:
            return False
        try:
            return bool(
                self.driver.execute_script(
                    self._JS_BOOTSTRAP_SELECT_APPLY, element_id, hints
                )
            )
        except Exception:
            return False

    def _js_bootstrap_select_selected_label(self, element_id):
        try:
            return (
                self.driver.execute_script(
                    self._JS_BOOTSTRAP_SELECT_LABEL, element_id
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _zhongshan_region_select_ok(self, element_id):
        """省/市下拉已选且非「请选择」。"""
        label = self._js_bootstrap_select_selected_label(element_id)
        return bool(label) and "请选择" not in label

    def _parse_zhongshan_region_hints(self, direct_addr):
        """
        从直发地址推断省、市关键字（用于前两格下拉），OMS 不写省名时按地名推断。
        例：郑州市荥阳市万山湖校区 → 河南、郑州（选下拉时常用「河南省」「郑州市」）。
        """
        if not direct_addr or not str(direct_addr).strip():
            return None, None
        s = str(direct_addr).strip()

        # 无「省」前缀的常见直发地址：郑州/荥阳/万山湖校区等 → 河南省 + 郑州市
        if "荥阳" in s or "万山湖" in s or re.match(r"^郑州市", s):
            return "河南", "郑州"

        m = re.match(r"^(.+?省)(.+?市)", s)
        if m:
            prov = m.group(1).replace("省", "").strip()
            city = m.group(2).replace("市", "").strip()
            return prov or None, city or None

        m2 = re.match(r"^(.+?市)", s)
        if m2:
            city_full = m2.group(1)
            city_core = city_full.replace("市", "").strip()
            city_guess = {
                "郑州": ("河南", "郑州"),
                "荥阳": ("河南", "郑州"),
                "中山": ("广东", "中山"),
                "上海": ("上海", "上海"),
                "广州": ("广东", "广州"),
                "深圳": ("广东", "深圳"),
                "北京": ("北京", "北京"),
                "天津": ("天津", "天津"),
                "重庆": ("重庆", "重庆"),
                "杭州": ("浙江", "杭州"),
                "南京": ("江苏", "南京"),
                "苏州": ("江苏", "苏州"),
                "武汉": ("湖北", "武汉"),
                "成都": ("四川", "成都"),
                "西安": ("陕西", "西安"),
            }
            for k, pair in city_guess.items():
                if k in city_core:
                    return pair[0], pair[1]
            return None, city_core or None
        return None, None

    def _zhongshan_province_hint_list(self, prov):
        """省：下拉常见「河南省」「河南」。"""
        if not prov:
            return []
        p = str(prov).strip()
        if p == "河南":
            return ["河南省", "河南"]
        if p in ("北京", "天津", "上海", "重庆"):
            return [p + "市", p]
        out = [p + "省", p]
        if p.endswith("省"):
            out.insert(0, p)
        seen = set()
        res = []
        for x in out:
            if x and x not in seen:
                seen.add(x)
                res.append(x)
        return res

    def _zhongshan_city_hint_list(self, city):
        """市：常见「郑州市」「郑州」。"""
        if not city:
            return []
        c = str(city).strip()
        out = [c + "市", c] if not c.endswith("市") else [c, c.replace("市", "")]
        seen = set()
        res = []
        for x in out:
            if x and x not in seen:
                seen.add(x)
                res.append(x)
        return res

    def _js_zhongshan_apply_province_city_hints(self, province_hints, city_hints):
        """
        在「收货地址」区域内识别省/市两个下拉（按 option 是否含「省」打分），
        再按多组 hint 依次匹配 option 文案。
        """
        return self.driver.execute_script(
            """
            var provHints = arguments[0] || [];
            var cityHints = arguments[1] || [];
            function applySelOption(sel, j) {
                var optVal = sel.options[j].value;
                sel.selectedIndex = j;
                sel.value = optVal;
                try {
                    if (typeof jQuery !== 'undefined') {
                        var $s = jQuery(sel);
                        if ($s.data('selectpicker')) {
                            $s.selectpicker('val', optVal);
                            $s.trigger('changed.bs.select');
                            return true;
                        }
                    }
                } catch (e) {}
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                sel.dispatchEvent(new Event('input', {bubbles: true}));
                try {
                    if (typeof jQuery !== 'undefined' && jQuery(sel).selectpicker) {
                        jQuery(sel).selectpicker('refresh');
                        jQuery(sel).trigger('changed.bs.select');
                    }
                } catch (e2) {}
                return true;
            }
            function collectRegionSelects(root) {
                var s = root.querySelectorAll('select'), out = [];
                for (var i = 0; i < s.length; i++) {
                    var el = s[i];
                    if (!el || !el.options || el.options.length < 2) continue;
                    out.push(el);
                }
                return out;
            }
            function findRootForCell(cell) {
                var tr = cell.closest('tr');
                if (tr) {
                    var scan = tr;
                    for (var step = 0; step < 5 && scan; step++) {
                        if (collectRegionSelects(scan).length >= 2) return scan;
                        scan = scan.nextElementSibling;
                    }
                }
                var el = cell;
                for (var d = 0; d < 14 && el; d++) {
                    el = el.parentElement;
                    if (!el) break;
                    if (collectRegionSelects(el).length >= 2) return el;
                }
                return null;
            }
            function scoreProvinceSelect(sel) {
                var sc = 0;
                for (var j = 0; j < sel.options.length && j < 50; j++) {
                    var t = sel.options[j].text || '';
                    if (t.indexOf('省') >= 0 || t.indexOf('自治区') >= 0) sc++;
                }
                return sc;
            }
            function tryHints(sel, hints) {
                for (var hi = 0; hi < hints.length; hi++) {
                    var h = (hints[hi] || '').replace(/\\s+/g, '');
                    if (!h) continue;
                    for (var j = 0; j < sel.options.length; j++) {
                        var tx = (sel.options[j].text || '').replace(/\\s+/g, '');
                        var val = (sel.options[j].value || '').replace(/\\s+/g, '');
                        if (tx.indexOf('请选择') >= 0 && !val) continue;
                        if (tx.indexOf(h) >= 0 || val.indexOf(h) >= 0) {
                            return applySelOption(sel, j);
                        }
                    }
                    if (h.length >= 2) {
                        var sh = h.slice(0, 2);
                        for (var j = 0; j < sel.options.length; j++) {
                            var tx = (sel.options[j].text || '').replace(/\\s+/g, '');
                            if (tx.indexOf('请选择') >= 0) continue;
                            if (tx.indexOf(sh) >= 0) {
                                return applySelOption(sel, j);
                            }
                        }
                    }
                }
                return false;
            }
            var cells = document.querySelectorAll('td, label, th, span, div');
            for (var i = 0; i < cells.length; i++) {
                if ((cells[i].textContent || '').indexOf('收货地址') < 0) continue;
                var root = findRootForCell(cells[i]);
                if (!root) continue;
                var arr = collectRegionSelects(root);
                if (arr.length < 2) continue;
                var pi = 0, pj = 1;
                if (scoreProvinceSelect(arr[1]) > scoreProvinceSelect(arr[0])) {
                    pi = 1;
                    pj = 0;
                }
                var okP = tryHints(arr[pi], provHints);
                var okC = false;
                if (okP) okC = tryHints(arr[pj], cityHints);
                if (!okC) {
                    var perms = [[pi, pj], [pj, pi]];
                    for (var t = 0; t < perms.length; t++) {
                        var a = perms[t][0], b = perms[t][1];
                        okP = tryHints(arr[a], provHints);
                        if (!okP) continue;
                        okC = tryHints(arr[b], cityHints);
                        if (okP && okC) break;
                    }
                }
                return { ok: okP && okC, okP: okP, okC: okC, nSelect: arr.length };
            }
            return { ok: false, reason: 'no_root' };
        """,
            province_hints,
            city_hints,
        )

    def _zhongshan_fill_recipient_line(self, line):
        """姓（收货人）：合并后的技术员 + 联系方式"""
        if not line or not str(line).strip():
            logger.warning("  OMS 未提供收货人(技术员+联系方式)，跳过姓(收货人)")
            return False
        text = str(line).strip()
        xpaths = [
            "//*[contains(normalize-space(.),'收货人')]/following::input[not(@type='hidden')][1]",
            "//*[contains(normalize-space(.),'姓') and contains(normalize-space(.),'收货')]/following::input[not(@type='hidden')][1]",
            "//td[contains(.,'收货人')]//following::input[not(@type='hidden')][1]",
            "//label[contains(.,'收货人')]/following::input[not(@type='hidden')][1]",
        ]
        for xp in xpaths:
            try:
                el = self.driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    el.clear()
                    el.send_keys(text)
                    logger.info("  ✓ 姓(收货人): 已填写")
                    return True
            except Exception:
                continue
        ok = self.driver.execute_script(
            """
            var txt = arguments[0];
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var cells = document.querySelectorAll('td, label, th, span, div');
            for (var i = 0; i < cells.length; i++) {
                var lab = (cells[i].textContent || '').replace(/\\s+/g, '');
                if (lab.indexOf('收货人') < 0 && !(lab.indexOf('姓') >= 0 && lab.indexOf('收货') >= 0)) continue;
                var tr = cells[i].closest('tr');
                var root = tr || cells[i].parentElement;
                if (!root) continue;
                var inp = root.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea');
                if (inp && vis(inp)) {
                    inp.focus();
                    inp.value = txt;
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            return false;
        """,
            text,
        )
        if ok:
            logger.info("  ✓ 姓(收货人): 已填写(JS)")
        else:
            logger.warning("  未能自动定位姓(收货人)输入框")
        return bool(ok)

    def _js_zhongshan_find_address_container(self):
        """
        定位「收货地址」整块表单：可能在同一 tr，也可能省市 select 在下一行（div/table 布局）。
        返回 true 表示已找到且容器内至少有 2 个带选项的 select（含 bootstrap-select 隐藏的原生 select）。
        """
        return bool(
            self.driver.execute_script("""
            function countRegionSelects(root) {
                var s = root.querySelectorAll('select'), n = 0;
                for (var i = 0; i < s.length; i++) {
                    var el = s[i];
                    if (el && el.options && el.options.length >= 2) n++;
                }
                return n;
            }
            var cells = document.querySelectorAll('td, label, th, span, div');
            for (var i = 0; i < cells.length; i++) {
                var raw = (cells[i].textContent || '');
                if (raw.indexOf('收货地址') < 0) continue;
                var tr = cells[i].closest('tr');
                if (tr) {
                    var scan = tr;
                    for (var step = 0; step < 4 && scan; step++) {
                        if (countRegionSelects(scan) >= 2) return true;
                        scan = scan.nextElementSibling;
                    }
                }
                var el = cells[i];
                for (var d = 0; d < 12 && el; d++) {
                    el = el.parentElement;
                    if (!el) break;
                    if (countRegionSelects(el) >= 2) return true;
                }
            }
            return false;
        """)
        )

    def _js_zhongshan_set_address_select_in_container(self, select_index, hint):
        """在含「收货地址」的表格行或父级容器内，设置第 idx 个 select（含隐藏的 bootstrap-select 原生层）。"""
        if not hint:
            return False
        return bool(
            self.driver.execute_script(
                """
            var idx = arguments[0], hint = (arguments[1] || '').replace(/\\s+/g, '');
            function applySelOption(sel, j) {
                var optVal = sel.options[j].value;
                sel.selectedIndex = j;
                sel.value = optVal;
                try {
                    if (typeof jQuery !== 'undefined') {
                        var $s = jQuery(sel);
                        if ($s.data('selectpicker')) {
                            $s.selectpicker('val', optVal);
                            $s.trigger('changed.bs.select');
                            return true;
                        }
                    }
                } catch (e) {}
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                sel.dispatchEvent(new Event('input', {bubbles: true}));
                try {
                    if (typeof jQuery !== 'undefined' && jQuery(sel).selectpicker) {
                        jQuery(sel).selectpicker('refresh');
                        jQuery(sel).trigger('changed.bs.select');
                    }
                } catch (e2) {}
                return true;
            }
            function setSel(sel, h) {
                if (!sel || !h) return false;
                for (var j = 0; j < sel.options.length; j++) {
                    var tx = (sel.options[j].text || '').replace(/\\s+/g, '');
                    if (tx.indexOf('请选择') >= 0) continue;
                    if (tx.indexOf(h) >= 0) return applySelOption(sel, j);
                }
                if (h.length >= 2) {
                    var shortH = h.slice(0, 2);
                    for (var j = 0; j < sel.options.length; j++) {
                        var tx = (sel.options[j].text || '').replace(/\\s+/g, '');
                        if (tx.indexOf('请选择') >= 0) continue;
                        if (tx.indexOf(shortH) >= 0) return applySelOption(sel, j);
                    }
                }
                return false;
            }
            function collectRegionSelects(root) {
                var s = root.querySelectorAll('select'), out = [];
                for (var i = 0; i < s.length; i++) {
                    var el = s[i];
                    if (!el || !el.options || el.options.length < 2) continue;
                    out.push(el);
                }
                return out;
            }
            function findRootForCell(cell) {
                var tr = cell.closest('tr');
                if (tr) {
                    var scan = tr;
                    for (var step = 0; step < 4 && scan; step++) {
                        if (collectRegionSelects(scan).length >= 2) return scan;
                        scan = scan.nextElementSibling;
                    }
                }
                var el = cell;
                for (var d = 0; d < 12 && el; d++) {
                    el = el.parentElement;
                    if (!el) break;
                    if (collectRegionSelects(el).length >= 2) return el;
                }
                return null;
            }
            var cells = document.querySelectorAll('td, label, th, span, div');
            for (var i = 0; i < cells.length; i++) {
                if ((cells[i].textContent || '').indexOf('收货地址') < 0) continue;
                var root = findRootForCell(cells[i]);
                if (!root) continue;
                var arr = collectRegionSelects(root);
                if (arr.length <= idx || !arr[idx]) continue;
                return setSel(arr[idx], hint);
            }
            return false;
        """,
                int(select_index),
                str(hint).strip(),
            )
        )

    def _js_zhongshan_fill_address_detail_in_container(self, full_addr):
        """在收货地址区域内填写详细地址（优先宽文本框，避免误填省市检索框）。"""
        return bool(
            self.driver.execute_script(
                """
            var full = arguments[0] || '';
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function collectRegionSelects(root) {
                var s = root.querySelectorAll('select'), out = [];
                for (var i = 0; i < s.length; i++) {
                    var el = s[i];
                    if (!el || !el.options || el.options.length < 2) continue;
                    out.push(el);
                }
                return out;
            }
            function findRootForCell(cell) {
                var tr = cell.closest('tr');
                if (tr) {
                    var scan = tr;
                    for (var step = 0; step < 4 && scan; step++) {
                        if (collectRegionSelects(scan).length >= 2) return scan;
                        scan = scan.nextElementSibling;
                    }
                }
                var el = cell;
                for (var d = 0; d < 12 && el; d++) {
                    el = el.parentElement;
                    if (!el) break;
                    if (collectRegionSelects(el).length >= 2) return el;
                }
                return null;
            }
            var cells = document.querySelectorAll('td, label, th, span, div');
            for (var i = 0; i < cells.length; i++) {
                if ((cells[i].textContent || '').indexOf('收货地址') < 0) continue;
                var root = findRootForCell(cells[i]);
                if (!root) continue;
                var cand = root.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea');
                var best = null, bestW = 0;
                for (var k = 0; k < cand.length; k++) {
                    var inp = cand[k];
                    if (!vis(inp)) continue;
                    var ph = ((inp.placeholder || '') + (inp.name || '') + (inp.id || '')).toLowerCase();
                    if (ph.indexOf('省') >= 0 && ph.indexOf('搜索') >= 0) continue;
                    var w = inp.getBoundingClientRect().width;
                    if (w > bestW) { bestW = w; best = inp; }
                }
                if (best && bestW >= 120) {
                    best.focus();
                    best.value = full;
                    best.dispatchEvent(new Event('input', {bubbles: true}));
                    best.dispatchEvent(new Event('change', {bubbles: true}));
                    best.dispatchEvent(new Event('blur', {bubbles: true}));
                    return true;
                }
                for (var k = cand.length - 1; k >= 0; k--) {
                    if (vis(cand[k])) {
                        cand[k].focus();
                        cand[k].value = full;
                        cand[k].dispatchEvent(new Event('input', {bubbles: true}));
                        cand[k].dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
            }
            return false;
        """,
                str(full_addr).strip(),
            )
        )

    def _zhongshan_fill_address_sequential(self, direct_addr):
        """
        中山报价单收货地址：严格按 省 → 等待联动 → 市 → 等待 → 详细地址。
        成功返回 True；失败 False（调用方应中止后续发货方式，避免控件连锁未启用）。
        """
        if not direct_addr or not str(direct_addr).strip():
            logger.warning("  OMS 无直发地址")
            return False
        full = str(direct_addr).strip()
        prov, city = self._parse_zhongshan_region_hints(full)
        prov_hints = self._zhongshan_province_hint_list(prov)
        city_hints = self._zhongshan_city_hint_list(city)
        logger.info(f"  地址推断: 省={prov} → 候选{prov_hints}，市={city} → 候选{city_hints}")

        if not self._js_zhongshan_find_address_container():
            logger.warning("  未定位到收货地址区域（含下拉），尝试通用地址框")
            if self._js_quotation_fill_visible_address(full):
                logger.info(f"  ✓ 收货地址(通用框): {full}")
                return True
            return False

        logger.info("  收货地址 [1-2/3] 选择省、市（按下拉 option 自动识别省/市框）")
        reg = self._js_zhongshan_apply_province_city_hints(prov_hints, city_hints)
        if isinstance(reg, dict):
            logger.info(f"  省/市下拉: {reg}")
        ok_p = bool(isinstance(reg, dict) and reg.get("okP"))
        ok_c = bool(isinstance(reg, dict) and reg.get("okC"))
        if not (ok_p and ok_c):
            logger.warning("  智能省/市匹配未一次成功，尝试按顺序 0=省 1=市")
            for h in prov_hints:
                if self._js_zhongshan_set_address_select_in_container(0, h):
                    ok_p = True
                    break
            time.sleep(1.25)
            for h in city_hints:
                if self._js_zhongshan_set_address_select_in_container(1, h):
                    ok_c = True
                    break
            time.sleep(1.05)
        else:
            time.sleep(1.15)

        if prov_hints and not ok_p:
            logger.warning("  省份下拉未选中，后续发货方式可能不可用")
        if city_hints and not ok_c:
            logger.warning("  城市下拉未选中，后续发货方式可能不可用")

        logger.info("  收货地址 [3/3] 填写详细地址")
        okd = self._js_zhongshan_fill_address_detail_in_container(full)
        if not okd:
            if self._js_quotation_fill_visible_address(full):
                logger.info(f"  ✓ 收货地址(通用框补救): {full}")
                okd = True
        else:
            time.sleep(0.35)
            try:
                el = self.driver.find_element(
                    By.XPATH,
                    "//*[contains(.,'收货地址')]/ancestor::*[.//input][1]//input[not(@type='checkbox')][not(@type='hidden')][last()]",
                )
                if el.is_displayed() and len(full) > 5:
                    el.clear()
                    el.send_keys(full)
            except Exception:
                pass
            logger.info(f"  ✓ 收货地址详细: {full}")

        if prov_hints and not ok_p:
            return False
        if city_hints and not ok_c:
            return False
        return bool(okd)

    def _js_orderpreview_pick_shipping_ld_vs_express(self):
        """
        OrderPreview：中山发货方式必须实选比较。
        规则：
        1. 分别点击「零担」和「快递（汽车）」；
        2. 每次点击后读取页面价格构成里的「运费」与「总计」；
        3. 运费不是大于 0 的数字（0、空白、待报价、读不到）直接排除；
        4. 若只有一个方式运费有效，选这个方式；
        5. 若两个方式运费都有效，再比较总计，选总计最低者。
        """

        def click_kind(kind):
            return self.driver.execute_script(
                """
            var kind = arguments[0];
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function isLtl(t) {
                t = norm(t);
                return t.indexOf('零担') >= 0 && t.indexOf('非零担') < 0;
            }
            function isExpressCar(t) {
                t = norm(t);
                if (t.indexOf('快递（汽车）') >= 0 || t.indexOf('快递(汽车)') >= 0) return true;
                return t.indexOf('快递') >= 0 && t.indexOf('汽车') >= 0;
            }
            function matches(t) {
                if (kind === 'ltl') return isLtl(t);
                return isExpressCar(t);
            }
            function clickInput(inp) {
                if (inp) {
                    try {
                        try { inp.scrollIntoView({block: 'center'}); } catch (e0) {}
                        if (!inp.checked) inp.click();
                        inp.checked = true;
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        return inp.checked;
                    } catch (e1) {}
                }
                return false;
            }
            function findHeadingRect() {
                var nodes = document.querySelectorAll('td, th, div, span, label, p');
                for (var i = 0; i < nodes.length; i++) {
                    if ((nodes[i].textContent || '').indexOf('发货方式') < 0) continue;
                    var r = nodes[i].getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return r;
                }
                return null;
            }
            var head = findHeadingRect();
            var radios = Array.prototype.slice.call(document.querySelectorAll('input[type=radio]'));
            var labelNodes = Array.prototype.slice.call(document.querySelectorAll('label, span, td, div, p'));
            var textHits = [];
            for (var i = 0; i < labelNodes.length; i++) {
                var el = labelNodes[i];
                if (!vis(el)) continue;
                var tx = norm(el.textContent || '');
                if (!matches(tx)) continue;
                var lr = el.getBoundingClientRect();
                if (head && lr.top < head.bottom - 8) continue;
                if (tx.indexOf('商品列表') >= 0 || tx.indexOf('订单情况') >= 0) continue;
                textHits.push({el: el, rect: lr, text: tx});
            }
            textHits.sort(function(a, b) {
                return a.rect.top - b.rect.top || a.rect.left - b.rect.left;
            });
            for (var h = 0; h < textHits.length; h++) {
                var hit = textHits[h];
                var contained = hit.el.querySelector && hit.el.querySelector('input[type=radio]');
                if (contained && clickInput(contained)) {
                    return { ok: true, kind: kind, label: hit.text, mode: 'contained_radio' };
                }
                if (hit.el.htmlFor) {
                    var byFor = document.getElementById(hit.el.htmlFor);
                    if (byFor && byFor.type === 'radio' && clickInput(byFor)) {
                        return { ok: true, kind: kind, label: hit.text, mode: 'for_radio' };
                    }
                }
                var best = null, bestScore = 1e9;
                for (var rix = 0; rix < radios.length; rix++) {
                    var inp = radios[rix];
                    var rr = inp.getBoundingClientRect();
                    if (rr.width <= 0 || rr.height <= 0) continue;
                    if (head && rr.top < head.bottom - 8) continue;
                    var sameLine = Math.abs((rr.top + rr.bottom) / 2 - (hit.rect.top + hit.rect.bottom) / 2);
                    if (sameLine > 22) continue;
                    var dx = hit.rect.left - rr.right;
                    if (dx < -12 || dx > 120) continue;
                    var score = sameLine * 10 + Math.abs(dx);
                    if (score < bestScore) { best = inp; bestScore = score; }
                }
                if (best && clickInput(best)) {
                    return { ok: true, kind: kind, label: hit.text, mode: 'nearby_radio' };
                }
            }
            var inputs = document.querySelectorAll('input[type=radio]');
            for (var j = 0; j < inputs.length; j++) {
                var inp2 = inputs[j];
                var p = inp2.parentElement;
                var tx2 = norm((inp2.value || '') + ' ' + (p ? p.textContent || '' : ''));
                if (!matches(tx2)) continue;
                if (clickInput(inp2)) return { ok: true, kind: kind, label: tx2, mode: 'radio_parent_text' };
            }
            return { ok: false, reason: 'no_candidate', kind: kind };
            """,
                kind,
            )

        def read_price_state(kind, label):
            return self.driver.execute_script(
                """
            var kind = arguments[0], label = arguments[1] || '';
            function parseMoney(s) {
                if (s == null) return null;
                s = String(s).replace(/,/g, '');
                var m = s.match(/-?\\d+(?:\\.\\d+)?/);
                if (!m) return null;
                var v = parseFloat(m[0]);
                return isNaN(v) ? null : v;
            }
            function lines() {
                return (document.body.innerText || '')
                    .split(/[\\r\\n]+/)
                    .map(function(x) { return (x || '').replace(/\\s+/g, ' ').trim(); })
                    .filter(Boolean);
            }
            function valueAfterKeyword(keyword, preferLast) {
                var vals = [];
                var pending = false;
                var ls = lines();
                for (var i = 0; i < ls.length; i++) {
                    var line = ls[i];
                    var ix = line.indexOf(keyword);
                    if (ix < 0) continue;
                    var rest = line.slice(ix + keyword.length);
                    if (rest.indexOf('待报价') >= 0) {
                        pending = true;
                        vals.push(null);
                        continue;
                    }
                    var v = parseMoney(rest);
                    if (v != null) vals.push(v);
                }
                if (!vals.length) {
                    var tx = (document.body.innerText || '').replace(/\\s+/g, ' ');
                    var re = new RegExp(keyword + '\\\\s*[:：]?\\\\s*(待报价|[￥¥]?\\\\s*-?\\\\d[\\\\d,]*(?:\\\\.\\\\d+)?)', 'g');
                    var m;
                    while ((m = re.exec(tx)) !== null) {
                        if ((m[1] || '').indexOf('待报价') >= 0) {
                            pending = true;
                            vals.push(null);
                        } else {
                            var vv = parseMoney(m[1]);
                            if (vv != null) vals.push(vv);
                        }
                    }
                }
                var nums = vals.filter(function(v) { return typeof v === 'number' && !isNaN(v); });
                if (!nums.length) {
                    return { value: null, pending: pending };
                }
                return { value: preferLast ? nums[nums.length - 1] : nums[0], pending: pending };
            }
            var freightPack = valueAfterKeyword('运费', false);
            var totalPack = valueAfterKeyword('总计', true);
            if (totalPack.value == null) totalPack = valueAfterKeyword('总价', true);
            if (totalPack.value == null) totalPack = valueAfterKeyword('总金额', true);
            var freight = freightPack.value;
            var total = totalPack.value;
            var freightOk = freight != null && freight > 0;
            return {
                kind: kind,
                label: label,
                freight: freight,
                total: total,
                valid: freightOk,
                freight_ok: freightOk,
                freight_pending: freightPack.pending,
                rejected_bad_freight: !freightOk,
                rejected_zero_freight: freight != null && freight <= 0
            };
            """,
                kind,
                label,
            )

        checked = []
        for kind in ("ltl", "express_car"):
            clicked = click_kind(kind)
            if not isinstance(clicked, dict) or not clicked.get("ok"):
                checked.append(
                    {
                        "kind": kind,
                        "ok": False,
                        "reason": clicked.get("reason") if isinstance(clicked, dict) else "click_failed",
                    }
                )
                continue
            time.sleep(1.2)
            state = read_price_state(kind, clicked.get("label", ""))
            state["ok"] = True
            checked.append(state)

        valid = [x for x in checked if x.get("freight_ok")]
        if not valid:
            return {
                "ok": False,
                "reason": "no_shipping_with_positive_freight",
                "checked": checked,
            }

        if len(valid) == 1:
            pick = valid[0]
        else:
            with_total = [
                x for x in valid
                if x.get("total") is not None
            ]
            if len(with_total) < 2:
                return {
                    "ok": False,
                    "reason": "positive_freight_but_total_missing",
                    "checked": checked,
                }
            pick = min(with_total, key=lambda x: float(x.get("total", 1e18)))
        final_click = click_kind(pick["kind"])
        time.sleep(0.6)
        if not isinstance(final_click, dict) or not final_click.get("ok"):
            return {
                "ok": False,
                "reason": "final_click_failed",
                "checked": checked,
                "pick": pick,
            }

        label = "零担" if pick["kind"] == "ltl" else "快递（汽车）"
        return {
            "ok": True,
            "label": label,
            "mode": "orderpreview_compare_freight_total",
            "freight": pick.get("freight"),
            "total": pick.get("total"),
            "checked": checked,
        }

    def _js_zhongshan_select_cheaper_ld_or_express_car(self):
        """
        中山：在发货方式相关下拉中，比较「零担」与「快递（汽车）」展示价，选较低。
        含隐藏 bootstrap-select（不仅限可见 select）。
        """
        return self.driver.execute_script(
            """
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function lastPrice(t) {
                var nums = (t + '').match(/\\d[\\d,]*\\.?\\d*/g);
                if (!nums || !nums.length) return null;
                var v = parseFloat(nums[nums.length - 1].replace(/,/g, ''));
                return isNaN(v) ? null : v;
            }
            function isLtl(t) {
                t = (t || '').replace(/\\s+/g, '');
                return t.indexOf('零担') >= 0 && t.indexOf('非零担') < 0;
            }
            function isExpressCar(t) {
                t = (t || '').replace(/\\s+/g, '');
                if (t.indexOf('快递（汽车）') >= 0 || t.indexOf('快递(汽车)') >= 0) return true;
                if (t.indexOf('快递') >= 0 && t.indexOf('汽车') >= 0) return true;
                return false;
            }
            function isRegionSelect(sel) {
                var id = (sel.id || '').toLowerCase();
                if (id.indexOf('province') >= 0 || id.indexOf('city') >= 0) return true;
                if (id === 'ddlsex' || id.indexOf('currency') >= 0) return true;
                return false;
            }
            function applyPick(s, pick) {
                var optVal = s.options[pick].value;
                s.selectedIndex = pick;
                s.value = optVal;
                try {
                    if (typeof jQuery !== 'undefined') {
                        var $s = jQuery(s);
                        if ($s.data('selectpicker')) {
                            $s.selectpicker('val', optVal);
                            $s.trigger('changed.bs.select');
                            return true;
                        }
                    }
                } catch (e) {}
                s.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            var sels = document.querySelectorAll('select');
            for (var si = 0; si < sels.length; si++) {
                var s = sels[si];
                if (isRegionSelect(s)) continue;
                var ldIx = -1, exIx = -1;
                var ldPr = 1e18, exPr = 1e18;
                for (var j = 0; j < s.options.length; j++) {
                    var tx = s.options[j].text || '';
                    if (isLtl(tx)) {
                        var p = lastPrice(tx);
                        if (p != null && p < ldPr) {
                            ldPr = p;
                            ldIx = j;
                        } else if (ldIx < 0) {
                            ldIx = j;
                        }
                    }
                    if (isExpressCar(tx)) {
                        var p2 = lastPrice(tx);
                        if (p2 != null && p2 < exPr) {
                            exPr = p2;
                            exIx = j;
                        } else if (exIx < 0) {
                            exIx = j;
                        }
                    }
                }
                if (ldIx < 0 && exIx < 0) continue;
                if (ldIx >= 0 && exIx >= 0) {
                    var pick = ldPr <= exPr ? ldIx : exIx;
                    applyPick(s, pick);
                    return { ok: true, label: (s.options[pick].text || '').trim() };
                }
                if (ldIx >= 0 && exIx < 0) {
                    applyPick(s, ldIx);
                    return { ok: true, label: (s.options[ldIx].text || '').trim(), partial: true };
                }
                if (exIx >= 0 && ldIx < 0) {
                    applyPick(s, exIx);
                    return { ok: true, label: (s.options[exIx].text || '').trim(), partial: true };
                }
            }
            return { ok: false };
        """
        )

    def _js_try_click_visible_shipping_select(self):
        """点击页面上最可能是「发货方式」的可见 native select（option 含零担/快递/自提）"""
        return bool(
            self.driver.execute_script("""
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            var sels = document.querySelectorAll('select');
            for (var i = 0; i < sels.length; i++) {
                var s = sels[i];
                if (!vis(s)) continue;
                var hit = false;
                for (var j = 0; j < s.options.length; j++) {
                    var t = (s.options[j].text || '') + '';
                    if (t.indexOf('零担') >= 0 || t.indexOf('快递') >= 0 || t.indexOf('自提') >= 0) {
                        hit = true;
                        break;
                    }
                }
                if (!hit) continue;
                try { s.scrollIntoView({block: 'center'}); } catch(e) {}
                try {
                    s.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    s.click();
                } catch(e2) {}
                return true;
            }
            return false;
        """)
        )

    def _zhongshan_pick_delivery_ld_vs_express_car(self):
        if self._zhongshan_is_order_preview_page():
            op_res = self._js_orderpreview_pick_shipping_ld_vs_express()
            if isinstance(op_res, dict) and op_res.get("ok"):
                logger.info(
                    f"  ✓ 发货方式(OrderPreview): {op_res.get('label', '')} "
                    f"运费={op_res.get('freight')} 总计={op_res.get('total')}"
                )
                checked = op_res.get("checked") or []
                for item in checked:
                    logger.info(
                        f"    试算 {item.get('kind')}: 运费={item.get('freight')} "
                        f"总计={item.get('total')} freight_ok={item.get('freight_ok')} "
                        f"pending={item.get('freight_pending')} "
                        f"bad_freight={item.get('rejected_bad_freight')}"
                    )
                return True
            if isinstance(op_res, dict):
                logger.warning(f"  发货方式(OrderPreview)实选比价失败: {op_res}")
                return False

        res = self._js_zhongshan_select_cheaper_ld_or_express_car()
        if isinstance(res, dict) and res.get("ok"):
            logger.info(f"  ✓ 发货方式(比价): {res.get('label', '')}")
            if res.get("partial"):
                logger.warning("  仅找到零担或快递（汽车）之一，已选可用项")
            return True

        self._js_try_click_visible_shipping_select()
        time.sleep(0.45)
        res = self._js_zhongshan_select_cheaper_ld_or_express_car()
        if isinstance(res, dict) and res.get("ok"):
            logger.info(f"  ✓ 发货方式(比价,二次): {res.get('label', '')}")
            if res.get("partial"):
                logger.warning("  仅找到零担或快递（汽车）之一，已选可用项")
            return True

        try:
            delivery_dropdown = self.browser.wait_for_clickable(
                *self.SELECTORS["quotation_delivery_method"], timeout=8
            )
            delivery_dropdown.click()
            time.sleep(0.45)
        except Exception as e:
            logger.warning(f"  打开发货方式下拉失败: {e}")

        res = self._js_zhongshan_select_cheaper_ld_or_express_car()
        if isinstance(res, dict) and res.get("ok"):
            logger.info(f"  ✓ 发货方式(比价,Selenium后): {res.get('label', '')}")
            if res.get("partial"):
                logger.warning("  仅找到零担或快递（汽车）之一，已选可用项")
            return True

        if self._js_quotation_select_option_keyword("零担"):
            logger.info("  ✓ 发货方式(JS补救): 零担")
            return True
        if self._js_quotation_select_option_keyword("快递（汽车）", "快递(汽车)"):
            logger.info("  ✓ 发货方式(JS补救): 快递（汽车）")
            return True
        if self._js_quotation_select_option_keyword("快递"):
            logger.info("  ✓ 发货方式(JS补救): 快递")
            return True
        logger.warning("  未能设置发货方式(零担/快递（汽车）)")
        return False

    def _zhongshan_check_material_purchase_other(self):
        ok = self.driver.execute_script("""
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function textRects(word) {
                var out = [];
                var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                var n;
                while ((n = walker.nextNode())) {
                    var raw = n.nodeValue || '';
                    var ix = raw.indexOf(word);
                    while (ix >= 0) {
                        try {
                            var range = document.createRange();
                            range.setStart(n, ix);
                            range.setEnd(n, ix + word.length);
                            var r = range.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                out.push({rect: r, text: word});
                            }
                        } catch (e) {}
                        ix = raw.indexOf(word, ix + word.length);
                    }
                }
                return out;
            }
            function rectCenterY(r) {
                return (r.top + r.bottom) / 2;
            }
            function pickMaterialHeadingRect() {
                var heads = textRects('物料购买类别');
                if (!heads.length) heads = textRects('购买类别');
                var best = null;
                for (var i = 0; i < heads.length; i++) {
                    var r = heads[i].rect;
                    if (r.left < 200 || r.top < 400) continue;
                    if (!best || r.top < best.top) best = r;
                }
                return best;
            }
            function clickRightmostCheckboxOnMaterialRow() {
                var hr = pickMaterialHeadingRect();
                if (!hr) return false;
                var hy = rectCenterY(hr);
                var checks = Array.prototype.slice.call(document.querySelectorAll('input[type=checkbox]'));
                var rowChecks = [];
                for (var i = 0; i < checks.length; i++) {
                    var c = checks[i], cr = c.getBoundingClientRect();
                    if (cr.width <= 0 || cr.height <= 0) continue;
                    var cy = rectCenterY(cr);
                    if (Math.abs(cy - hy) > 24) continue;
                    if (cr.left <= hr.left + 40) continue;
                    rowChecks.push(c);
                }
                rowChecks.sort(function(a, b) {
                    return a.getBoundingClientRect().left - b.getBoundingClientRect().left;
                });
                if (!rowChecks.length) return false;
                return clickInput(rowChecks[rowChecks.length - 1]);
            }
            function clickInput(inp) {
                if (!inp) return false;
                try {
                    try { inp.scrollIntoView({block: 'center'}); } catch (e0) {}
                    if (!inp.checked) inp.click();
                    inp.checked = true;
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    return inp.checked;
                } catch (e) {
                    return false;
                }
            }
            var heading = pickMaterialHeadingRect();
            var checks = Array.prototype.slice.call(document.querySelectorAll('input[type=checkbox], input[type=radio]'));
            function clickNearestToRect(rect) {
                var best = null, bestScore = 1e9;
                for (var ci = 0; ci < checks.length; ci++) {
                    var c = checks[ci], cr = c.getBoundingClientRect();
                    if (cr.width <= 0 || cr.height <= 0) continue;
                    var sameLine = Math.abs((cr.top + cr.bottom) / 2 - (rect.top + rect.bottom) / 2);
                    if (sameLine > 18) continue;
                    var dx = rect.left - cr.right;
                    if (dx < -10 || dx > 90) continue;
                    var score = sameLine * 10 + Math.abs(dx);
                    if (score < bestScore) { best = c; bestScore = score; }
                }
                return best ? clickInput(best) : false;
            }
            var exactRects = textRects('其他');
            if (heading) {
                var hy2 = rectCenterY(heading);
                exactRects = exactRects.filter(function(item) {
                    return Math.abs(rectCenterY(item.rect) - hy2) <= 24;
                });
            }
            exactRects.sort(function(a, b) {
                return a.rect.left - b.rect.left;
            });
            for (var er = 0; er < exactRects.length; er++) {
                var rr = exactRects[er].rect;
                if (heading && Math.abs((rr.top + rr.bottom) / 2 - (heading.top + heading.bottom) / 2) > 45) continue;
                if (clickNearestToRect(rr)) return true;
            }
            if (clickRightmostCheckboxOnMaterialRow()) return true;

            var nodes = document.querySelectorAll('label, span, td, div, p');
            var hits = [];
            for (var k = 0; k < nodes.length; k++) {
                var el = nodes[k];
                if (!vis(el)) continue;
                var tx = norm(el.textContent || '');
                if (tx !== '其他' && tx.indexOf('其他') < 0) continue;
                var r = el.getBoundingClientRect();
                if (heading && Math.abs((r.top + r.bottom) / 2 - (heading.top + heading.bottom) / 2) > 40) continue;
                hits.push({el: el, rect: r, text: tx});
            }
            hits.sort(function(a, b) {
                return Math.abs(a.rect.top - (heading ? heading.top : a.rect.top))
                    - Math.abs(b.rect.top - (heading ? heading.top : b.rect.top))
                    || a.rect.left - b.rect.left;
            });
            for (var hi = 0; hi < hits.length; hi++) {
                var hit = hits[hi];
                var contained = hit.el.querySelector && hit.el.querySelector('input[type=checkbox], input[type=radio]');
                if (contained && clickInput(contained)) return true;
                if (hit.el.htmlFor) {
                    var byFor = document.getElementById(hit.el.htmlFor);
                    if (byFor && clickInput(byFor)) return true;
                }
                if (clickNearestToRect(hit.rect)) return true;
            }
            return false;
        """)
        if ok:
            logger.info("  ✓ 物料购买类别: 其他")
            return True
        try:
            self.browser.safe_click(*self.SELECTORS["quotation_other_btn"], timeout=4)
            logger.info("  ✓ 物料购买类别: 其他(按钮)")
            return True
        except Exception:
            logger.warning("  未能勾选物料购买类别「其他」")
            return False

    def _fill_quotation_project_name_extended(self, project_name, ladder_no):
        """项目名称：多路径尝试，与 OMS 一致"""
        display = (project_name or "").strip() or (ladder_no or "").strip()
        if not display:
            return
        xpaths = [
            "//*[contains(normalize-space(.),'项目名称')]/following::input[not(@type='hidden')][1]",
            "//input[@name='projectName']",
            "//input[contains(@placeholder,'项目')]",
            "//textarea[contains(@placeholder,'项目')]",
        ]
        for xp in xpaths:
            try:
                el = self.driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    current = (el.get_attribute("value") or "").strip()
                    if current != display:
                        el.clear()
                        el.send_keys(display)
                        try:
                            el.send_keys(Keys.TAB)
                        except Exception:
                            pass
                        # OrderPreview 会在项目名称 change/blur 后刷新部分必填项，
                        # 调用方应在此之后再填写收货地址/发货方式。
                        time.sleep(0.8)
                    logger.info(f"  ✓ 项目名称: {display}")
                    return
            except Exception:
                continue
        self._fill_quotation_project_info(project_name, ladder_no)

    def _zhongshan_is_order_preview_page(self):
        """
        报价单「订单预览」页 (URL 常含 /Order/OrderPreview/)：
        省 ddlProvince、市 ddlCity、bootstrap-select 隐藏原生 select，与旧版表格收货地址不同。
        """
        try:
            return bool(
                self.driver.execute_script(
                    "return !!(document.getElementById('ddlProvince') "
                    "&& document.getElementById('ddlCity') "
                    "&& document.getElementById('txtAddress'));"
                )
            )
        except Exception:
            return False

    def _zhongshan_bp_set_select_hints(self, element_id, hints):
        """按 option 文案匹配设置 select（含 bootstrap-select selectpicker('val')）。"""
        return self._js_bootstrap_select_apply_hints(element_id, hints)

    def _js_orderpreview_trigger_select_change(self, element_id):
        """触发省/市下拉的 change 与页面可能注册的联动函数（加载 ddlCity 等）。"""
        try:
            self.driver.execute_script(
                """
            var sel = document.getElementById(arguments[0]);
            if (!sel) return;
            try {
                if (typeof jQuery !== 'undefined') {
                    var $s = jQuery(sel);
                    $s.trigger('change');
                    $s.trigger('changed.bs.select');
                }
            } catch (e) {}
            try {
                if (typeof sel.onchange === 'function') sel.onchange.call(sel);
            } catch (e2) {}
            sel.dispatchEvent(new Event('change', {bubbles: true}));
            sel.dispatchEvent(new Event('input', {bubbles: true}));
            var names = [
                'ProvinceChange', 'provinceChange', 'ddlProvinceChange',
                'CityChange', 'LoadCity', 'loadCity', 'bindCity', 'GetCity',
                'GetCityList', 'ChangeProvince', 'changeProvince'
            ];
            for (var i = 0; i < names.length; i++) {
                if (typeof window[names[i]] === 'function') {
                    try { window[names[i]](); } catch (e3) {}
                }
            }
            """,
                element_id,
            )
        except Exception:
            pass

    def _js_orderpreview_click_bootstrap_select_option(self, element_id, hints):
        """点击 bootstrap-select 可见按钮并在浮层中选 option（联动常依赖 UI 点击）。"""
        if not hints:
            return False
        try:
            return bool(
                self.driver.execute_script(
                    """
            var id = arguments[0], hints = arguments[1] || [];
            var sel = document.getElementById(id);
            if (!sel || typeof jQuery === 'undefined') return false;
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function matchText(t) {
                t = norm(t);
                if (!t || t.indexOf('请选择') >= 0) return false;
                for (var i = 0; i < hints.length; i++) {
                    var h = norm(hints[i]);
                    if (!h) continue;
                    if (t.indexOf(h) >= 0) return true;
                    if (h.length >= 2 && t.indexOf(h.slice(0, 2)) >= 0) return true;
                }
                return false;
            }
            var $s = jQuery(sel);
            var wrap = $s.closest('.bootstrap-select');
            if (!wrap.length) wrap = $s.parent('.bootstrap-select');
            if (!wrap.length) return false;
            var btn = wrap.find('button.dropdown-toggle, .dropdown-toggle').first();
            if (!btn.length) return false;
            try { btn[0].scrollIntoView({block: 'center'}); } catch (e) {}
            btn.trigger('click');
            var menu = wrap.find('.dropdown-menu.open, .dropdown-menu.show, .dropdown-menu');
            var items = menu.find('li');
            for (var j = 0; j < items.length; j++) {
                var li = items[j];
                if (li.classList && li.classList.contains('disabled')) continue;
                var tx = norm(li.textContent || '');
                if (!matchText(tx)) continue;
                var a = li.querySelector('a');
                if (a) { a.click(); return true; }
                li.click();
                return true;
            }
            return false;
            """,
                    element_id,
                    hints,
                )
            )
        except Exception:
            return False

    def _js_orderpreview_city_ready(self, city_hints=None):
        """ddlCity 是否已有除「请选择」外的选项（可选：是否含目标市关键字）。"""
        try:
            return bool(
                self.driver.execute_script(
                    """
            var hints = arguments[0] || [];
            var s = document.getElementById('ddlCity');
            if (!s || !s.options) return false;
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            var real = 0;
            for (var j = 0; j < s.options.length; j++) {
                var tx = norm(s.options[j].text || '');
                if (!tx || tx.indexOf('请选择') >= 0) continue;
                real++;
                if (!hints.length) continue;
                for (var i = 0; i < hints.length; i++) {
                    var h = norm(hints[i]);
                    if (!h) continue;
                    if (tx.indexOf(h) >= 0) return true;
                    if (h.length >= 2 && tx.indexOf(h.slice(0, 2)) >= 0) return true;
                }
            }
            return real > 0;
            """,
                    city_hints or [],
                )
            )
        except Exception:
            return False

    def _zhongshan_wait_ddl_city_populated(
        self, city_hints=None, min_options=2, timeout=25.0
    ):
        """选省后 ddlCity 由联动/Ajax 加载；可等待出现目标市选项。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._js_orderpreview_city_ready(city_hints):
                try:
                    n = self.driver.execute_script(
                        "var s=document.getElementById('ddlCity');"
                        "return (s && s.options) ? s.options.length : 0;"
                    )
                    if city_hints or (n and int(n) >= min_options):
                        return True
                except Exception:
                    return True
            time.sleep(0.4)
        logger.warning("  ddlCity 在超时内选项仍不足（请确认省已选对）")
        return False

    def _orderpreview_select_province(self, prov_hints):
        """OrderPreview 选省：API + 触发联动 + 可见下拉点击。"""
        if not prov_hints:
            return False
        ok = self._zhongshan_bp_set_select_hints("ddlProvince", prov_hints)
        self._js_orderpreview_trigger_select_change("ddlProvince")
        time.sleep(0.35)
        if self._js_orderpreview_click_bootstrap_select_option("ddlProvince", prov_hints):
            logger.info("  ✓ ddlProvince(可见下拉点击)")
            ok = True
        self._js_orderpreview_trigger_select_change("ddlProvince")
        time.sleep(0.25)
        if not ok:
            ok = self._zhongshan_bp_set_select_hints("ddlProvince", prov_hints)
            self._js_orderpreview_trigger_select_change("ddlProvince")
        return ok and self._zhongshan_region_select_ok("ddlProvince")

    def _orderpreview_select_city(self, city_hints):
        """OrderPreview 选市：等待选项出现后 API + 可见下拉点击。"""
        if not city_hints:
            return False
        ok = self._zhongshan_bp_set_select_hints("ddlCity", city_hints)
        if not ok or not self._zhongshan_region_select_ok("ddlCity"):
            if self._js_orderpreview_click_bootstrap_select_option("ddlCity", city_hints):
                logger.info("  ✓ ddlCity(可见下拉点击)")
                ok = True
            self._js_orderpreview_trigger_select_change("ddlCity")
            time.sleep(0.2)
            ok = self._zhongshan_bp_set_select_hints("ddlCity", city_hints) or ok
        return ok and self._zhongshan_region_select_ok("ddlCity")

    def _orderpreview_fill_txt_address(self, full):
        """填写 txtAddress 并校验回读（未选省市时控件可能被禁用或清空）。"""
        full = str(full or "").strip()
        if not full:
            return False
        try:
            ok = bool(
                self.driver.execute_script(
                    """
                var full = arguments[0];
                var el = document.getElementById('txtAddress');
                if (!el) return false;
                try { el.removeAttribute('disabled'); el.removeAttribute('readonly'); } catch (e) {}
                el.focus();
                el.value = full;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                return (el.value || '').trim().length >= 3;
                """,
                    full,
                )
            )
            if ok:
                return True
        except Exception:
            pass
        try:
            addr_el = self.driver.find_element(By.ID, "txtAddress")
            self.driver.execute_script(
                "arguments[0].removeAttribute('disabled');"
                "arguments[0].removeAttribute('readonly');",
                addr_el,
            )
            addr_el.click()
            addr_el.clear()
            addr_el.send_keys(full)
            addr_el.send_keys(Keys.TAB)
            time.sleep(0.25)
            val = (addr_el.get_attribute("value") or "").strip()
            return len(val) >= 3 and (full in val or val in full or val[:8] in full)
        except Exception as e:
            logger.warning(f"  OrderPreview txtAddress 填写失败: {e}")
            return False

    def _zhongshan_split_recipient_name_phone(self, line):
        """收货人一行 → (姓名去空格, 电话)。"""
        if not line or not str(line).strip():
            return "", ""
        s = str(line).strip()
        phone = ""
        m = re.search(r"(1\d{10}\b|[\d\-\s]{8,})", s)
        if m:
            phone = re.sub(r"\s+", "", m.group(0))
            s = (s[: m.start()] + s[m.end() :]).strip()
        name = re.sub(r"\s+", "", s)
        return name, phone

    def _zhongshan_fill_recipient_orderpreview(self, line):
        """OrderPreview：完整收货人姓名填 txtLastName，txtFirstName 留空，电话填 txtTel。"""
        name, phone = self._zhongshan_split_recipient_name_phone(line)
        if not name and not phone:
            logger.warning("  OrderPreview：收货人为空，跳过")
            return False
        try:
            if name:
                el = self.browser.wait_for_element(By.ID, "txtLastName", timeout=8)
                el.clear()
                el.send_keys(name)
                logger.info(f"  ✓ OrderPreview 姓(完整收货人): {name}")
            try:
                el = self.driver.find_element(By.ID, "txtFirstName")
                el.clear()
                logger.info("  ✓ OrderPreview 名: 已留空")
            except Exception:
                pass
            if phone:
                el = self.driver.find_element(By.ID, "txtTel")
                el.clear()
                el.send_keys(phone)
                logger.info("  ✓ OrderPreview 电话已填")
            return bool(name or phone)
        except Exception as e:
            logger.warning(f"  OrderPreview 收货人填写失败: {e}")
            return False

    def _zhongshan_orderpreview_address_parts_state(self):
        """OrderPreview 收货地址分项：省/市/详细地址是否已有有效值。"""
        try:
            return self.driver.execute_script(
                """
            function val(id) {
                var el = document.getElementById(id);
                return el ? (el.value || '').trim() : '';
            }
            function selectOk(id) {
                var s = document.getElementById(id);
                if (!s) return false;
                var tx = '';
                try { tx = (s.options[s.selectedIndex] && s.options[s.selectedIndex].text || '').trim(); } catch(e) {}
                var v = (s.value || '').trim();
                return !!(v || tx) && tx.indexOf('请选择') < 0 && v.indexOf('请选择') < 0;
            }
            return {
                province: selectOk('ddlProvince'),
                city: selectOk('ddlCity'),
                detail: !!val('txtAddress')
            };
            """
            ) or {}
        except Exception:
            return {}

    def _zhongshan_fill_address_orderpreview_trust_page(self, direct_addr=""):
        """
        人工暂停后续填：仅补全页面上仍缺的省/市/详细地址（不覆盖已有项）。
        direct_addr 仅用于推断缺失的省/市/详细，不回头读 OMS。
        """
        parts = self._zhongshan_orderpreview_address_parts_state()
        if parts.get("province") and parts.get("city") and parts.get("detail"):
            logger.info("  ✓ 收货地址已由人工填写完整，跳过地址自动填")
            return True

        detail_on_page = ""
        try:
            detail_on_page = (
                self.driver.find_element(By.ID, "txtAddress").get_attribute("value")
                or ""
            ).strip()
        except Exception:
            pass

        hint_text = (direct_addr or "").strip() or detail_on_page
        if not hint_text:
            logger.warning("  页面地址不完整且无直发地址文本可推断，无法自动补全省/市")
            return bool(parts.get("detail"))

        full = hint_text
        prov, city = self._parse_zhongshan_region_hints(full)
        prov_hints = self._zhongshan_province_hint_list(prov)
        city_hints = self._zhongshan_city_hint_list(city)
        logger.info(
            f"  地址分项补全: 省={parts.get('province')} 市={parts.get('city')} "
            f"详细={parts.get('detail')} 推断={prov}/{city}"
        )

        ok_p = bool(parts.get("province"))
        ok_c = bool(parts.get("city"))
        ok_detail = bool(parts.get("detail"))

        if not ok_p and prov_hints:
            ok_p = self._orderpreview_select_province(prov_hints)
            if ok_p:
                self._zhongshan_wait_ddl_city_populated(
                    city_hints=city_hints, min_options=2, timeout=28.0
                )
        if not ok_c and city_hints:
            if not ok_p and not parts.get("province"):
                logger.warning("  市下拉无法补全：省未选中")
            else:
                ok_c = self._orderpreview_select_city(city_hints)

        if not ok_detail:
            detail_src = (direct_addr or "").strip() or detail_on_page
            if detail_src:
                ok_detail = self._orderpreview_fill_txt_address(detail_src)

        if ok_p and ok_c and ok_detail:
            logger.info("  ✓ 地址分项补全完成")
            return True
        logger.warning(
            f"  地址分项仍不完整: 省={ok_p} 市={ok_c} 详细={ok_detail}"
        )
        return False

    def _zhongshan_fill_address_orderpreview(self, direct_addr):
        """OrderPreview：ddlProvince → 等待 ddlCity → ddlCity → txtAddress。"""
        if not direct_addr or not str(direct_addr).strip():
            logger.warning("  OrderPreview：直发地址为空")
            return False
        full = str(direct_addr).strip()
        prov, city = self._parse_zhongshan_region_hints(full)
        prov_hints = self._zhongshan_province_hint_list(prov)
        city_hints = self._zhongshan_city_hint_list(city)
        logger.info(f"  OrderPreview 地址推断: 省={prov} 候选{prov_hints} 市={city} 候选{city_hints}")

        ok_p = ok_c = True
        if prov_hints:
            ok_p = self._orderpreview_select_province(prov_hints)
            if ok_p:
                logger.info(
                    f"  ✓ ddlProvince: {self._js_bootstrap_select_selected_label('ddlProvince')}"
                )
            else:
                logger.warning("  ddlProvince 未匹配到选项，请核对省名与页面下拉文案")
            if not self._zhongshan_wait_ddl_city_populated(
                city_hints=city_hints, min_options=2, timeout=28.0
            ):
                logger.warning("  等待市下拉失败，尝试再次点击省的下拉…")
                self._js_orderpreview_click_bootstrap_select_option(
                    "ddlProvince", prov_hints
                )
                self._js_orderpreview_trigger_select_change("ddlProvince")
                self._zhongshan_wait_ddl_city_populated(
                    city_hints=city_hints, min_options=2, timeout=12.0
                )
        else:
            logger.warning("  未能从直发地址推断省份")
            ok_p = False

        if city_hints:
            ok_c = self._orderpreview_select_city(city_hints)
            if ok_c:
                logger.info(
                    f"  ✓ ddlCity: {self._js_bootstrap_select_selected_label('ddlCity')}"
                )
            else:
                logger.warning("  ddlCity 未匹配到选项（可能省未触发联动）")
        elif prov:
            ok_c = False

        ok_detail = False
        if ok_p and ok_c:
            ok_detail = self._orderpreview_fill_txt_address(full)
            if ok_detail:
                logger.info("  ✓ OrderPreview 详细地址 txtAddress 已填写并校验")
            else:
                logger.warning(
                    "  txtAddress 写入后回读失败（常见原因：省/市未在页面上真正选中）"
                )
        else:
            logger.warning("  省/市未就绪，跳过详细地址填写")

        if not (ok_p and ok_c and ok_detail):
            logger.warning(
                f"  OrderPreview 地址未完成: 省={ok_p} 市={ok_c} 详细={ok_detail}"
            )
            return False
        return True

    def _zhongshan_orderpreview_required_state(self):
        """读取 OrderPreview 保存前几个关键必填项是否仍然有效。"""
        try:
            return self.driver.execute_script(
                """
            function val(id) {
                var el = document.getElementById(id);
                return el ? (el.value || '').trim() : '';
            }
            function selectOk(id) {
                var s = document.getElementById(id);
                if (!s) return false;
                var tx = '';
                try { tx = (s.options[s.selectedIndex] && s.options[s.selectedIndex].text || '').trim(); } catch(e) {}
                var v = (s.value || '').trim();
                return !!(v || tx) && tx.indexOf('请选择') < 0 && v.indexOf('请选择') < 0;
            }
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            function vis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function textRects(word) {
                var out = [];
                var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                var n;
                while ((n = walker.nextNode())) {
                    var raw = n.nodeValue || '';
                    var ix = raw.indexOf(word);
                    while (ix >= 0) {
                        try {
                            var range = document.createRange();
                            range.setStart(n, ix);
                            range.setEnd(n, ix + word.length);
                            var r = range.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) out.push(r);
                        } catch (e) {}
                        ix = raw.indexOf(word, ix + word.length);
                    }
                }
                return out;
            }
            function rectCenterY(r) {
                return (r.top + r.bottom) / 2;
            }
            function rightmostMaterialCheckboxChecked() {
                var heads = textRects('物料购买类别');
                if (!heads.length) heads = textRects('购买类别');
                if (!heads.length) return null;
                heads.sort(function(a, b) { return a.top - b.top || a.left - b.left; });
                var hr = heads[0];
                var hy = rectCenterY(hr);
                var checks = Array.prototype.slice.call(document.querySelectorAll('input[type=checkbox]'));
                var rowChecks = [];
                for (var i = 0; i < checks.length; i++) {
                    var c = checks[i], cr = c.getBoundingClientRect();
                    if (cr.width <= 0 || cr.height <= 0) continue;
                    if (Math.abs(rectCenterY(cr) - hy) > 24) continue;
                    if (cr.left <= hr.right - 5) continue;
                    rowChecks.push(c);
                }
                rowChecks.sort(function(a, b) {
                    return a.getBoundingClientRect().left - b.getBoundingClientRect().left;
                });
                if (!rowChecks.length) return null;
                return !!rowChecks[rowChecks.length - 1].checked;
            }
            function headingRect(keyword) {
                var cells = document.querySelectorAll('td, label, th, span, div, p');
                for (var i = 0; i < cells.length; i++) {
                    if ((cells[i].textContent || '').indexOf(keyword) < 0) continue;
                    var r = cells[i].getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return r;
                }
                return null;
            }
            function checkedNearText(keyword, targetWords, inputSelector) {
                var head = headingRect(keyword);
                var nodes = document.querySelectorAll('label, span, td, div, p');
                var inputs = Array.prototype.slice.call(document.querySelectorAll(inputSelector));
                for (var tw = 0; tw < targetWords.length; tw++) {
                    var rects = textRects(targetWords[tw]);
                    for (var ri = 0; ri < rects.length; ri++) {
                        var tr = rects[ri];
                        if (head && keyword === '发货方式' && tr.top < head.bottom - 8) continue;
                        if (head && keyword !== '发货方式'
                            && Math.abs((tr.top + tr.bottom) / 2 - (head.top + head.bottom) / 2) > 45) continue;
                        for (var ij = 0; ij < inputs.length; ij++) {
                            var exactInp = inputs[ij];
                            if (!exactInp.checked) continue;
                            var er = exactInp.getBoundingClientRect();
                            if (er.width <= 0 || er.height <= 0) continue;
                            var exactLine = Math.abs((er.top + er.bottom) / 2 - (tr.top + tr.bottom) / 2);
                            if (exactLine > 18) continue;
                            var exactDx = tr.left - er.right;
                            if (exactDx >= -10 && exactDx <= 90) return true;
                        }
                    }
                }
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    if (!vis(el)) continue;
                    var tx = norm(el.textContent || '');
                    var hit = false;
                    for (var w = 0; w < targetWords.length; w++) {
                        if (tx.indexOf(targetWords[w]) >= 0) { hit = true; break; }
                    }
                    if (!hit) continue;
                    var lr = el.getBoundingClientRect();
                    if (head && lr.top < head.bottom - 8 && keyword === '发货方式') continue;
                    if (head && keyword !== '发货方式'
                        && Math.abs((lr.top + lr.bottom) / 2 - (head.top + head.bottom) / 2) > 45) continue;
                    var contained = el.querySelector && el.querySelector(inputSelector);
                    if (contained && contained.checked) return true;
                    if (el.htmlFor) {
                        var byFor = document.getElementById(el.htmlFor);
                        if (byFor && byFor.checked) return true;
                    }
                    for (var j = 0; j < inputs.length; j++) {
                        var inp = inputs[j];
                        if (!inp.checked) continue;
                        var ir = inp.getBoundingClientRect();
                        if (ir.width <= 0 || ir.height <= 0) continue;
                        var sameLine = Math.abs((ir.top + ir.bottom) / 2 - (lr.top + lr.bottom) / 2);
                        if (sameLine > 22) continue;
                        var dx = lr.left - ir.right;
                        if (dx >= -12 && dx <= 140) return true;
                    }
                }
                return false;
            }
            function projectOk() {
                var nodes = document.querySelectorAll('input, textarea');
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    var labelText = '';
                    var p = el;
                    for (var d = 0; d < 4 && p; d++, p = p.parentElement) {
                        labelText += ' ' + (p.textContent || '');
                    }
                    var ph = el.getAttribute('placeholder') || '';
                    var nm = (el.name || '') + ' ' + (el.id || '');
                    if (norm(labelText).indexOf('项目名称') >= 0
                        || norm(ph).indexOf('项目') >= 0
                        || nm.toLowerCase().indexOf('project') >= 0) {
                        return !!(el.value || '').trim();
                    }
                }
                return false;
            }
            function deliveryOk() {
                if (checkedNearText('发货方式', ['零担', '快递'], 'input[type=radio], input[type=checkbox]')) {
                    return true;
                }
                var cells = document.querySelectorAll('td, label, th, span, div, p');
                for (var i = 0; i < cells.length; i++) {
                    if ((cells[i].textContent || '').indexOf('发货方式') < 0) continue;
                    var root = cells[i].closest('tr') || cells[i].closest('table') || cells[i].parentElement || cells[i];
                    var inputs = root.querySelectorAll('input[type=radio], input[type=checkbox]');
                    for (var j = 0; j < inputs.length; j++) {
                        var tx = norm(inputs[j].value || inputs[j].parentElement && inputs[j].parentElement.textContent || '');
                        if ((tx.indexOf('零担') >= 0 || tx.indexOf('快递') >= 0) && inputs[j].checked) return true;
                    }
                    var selected = root.querySelector('option:checked');
                    if (selected) {
                        var st = norm(selected.textContent || selected.value || '');
                        if ((st.indexOf('零担') >= 0 || st.indexOf('快递') >= 0) && st.indexOf('请选择') < 0) return true;
                    }
                }
                return false;
            }
            function materialOtherOk() {
                var exact = rightmostMaterialCheckboxChecked();
                if (exact !== null) return exact;
                if (checkedNearText('物料购买类别', ['其他'], 'input[type=radio], input[type=checkbox]')) {
                    return true;
                }
                var cells = document.querySelectorAll('td, label, th, span, div');
                for (var i = 0; i < cells.length; i++) {
                    var t = cells[i].textContent || '';
                    if (t.indexOf('物料购买类别') < 0 && t.indexOf('购买类别') < 0) continue;
                    var root = cells[i].closest('tr') || cells[i].closest('table') || cells[i].parentElement || cells[i];
                    var inputs = root.querySelectorAll('input[type=radio], input[type=checkbox]');
                    for (var j = 0; j < inputs.length; j++) {
                        var label = norm(inputs[j].value || inputs[j].parentElement && inputs[j].parentElement.textContent || '');
                        if (label.indexOf('其他') >= 0 && inputs[j].checked) return true;
                    }
                }
                return false;
            }
            /* 收货人：以下任一即可 — ①姓一栏写「姓名+电话」②姓/名/电话分栏 ③姓+电话（名可空） */
            function recipientOk() {
                var ln = val('txtLastName');
                var fn = val('txtFirstName');
                var tel = val('txtTel');
                var compact = function(s) { return (s || '').replace(/\\s+/g, ''); };
                var lnC = compact(ln);
                var fnC = compact(fn);
                var telC = compact(tel);
                var telDig = telC.replace(/\\D/g, '');
                var blob = lnC + fnC + telC;
                if (telDig.length >= 8 && (lnC || fnC)) return true;
                if (/1\\d{10}/.test(blob)) return true;
                if (lnC && lnC.replace(/\\D/g, '').length >= 8) return true;
                return false;
            }
            return {
                recipient: recipientOk(),
                address: selectOk('ddlProvince') && selectOk('ddlCity') && !!val('txtAddress'),
                delivery: deliveryOk(),
                project: projectOk(),
                material_other: materialOtherOk()
            };
            """
            )
        except Exception:
            return {}

    def _zhongshan_orderpreview_refill_required(
        self,
        project_name,
        ladder_no,
        direct,
        recipient,
        after_manual_pause=False,
    ):
        """保存前复核 OrderPreview 必填项，被项目名称联动清空时立即补填。"""
        state = self._zhongshan_orderpreview_required_state()
        missing = [k for k, v in (state or {}).items() if not v]
        if not missing:
            logger.info("  ✓ OrderPreview 保存前必填项复核通过")
            return True

        logger.warning(f"  OrderPreview 保存前发现字段被清空/未选中: {missing}，开始补填")

        # 若项目名称本身缺失，先填项目名称；它可能再次触发清空，所以之后还要补其它项。
        if not state.get("project"):
            self._fill_quotation_project_name_extended(project_name, ladder_no)
            time.sleep(0.6)

        state = self._zhongshan_orderpreview_required_state()
        if not state.get("recipient"):
            if after_manual_pause and not recipient:
                if not (self._zhongshan_orderpreview_required_state() or {}).get(
                    "recipient"
                ):
                    return False
            else:
                self._zhongshan_fill_recipient_orderpreview(recipient)
            time.sleep(0.25)
        if not state.get("address"):
            if after_manual_pause:
                ok_addr = self._zhongshan_fill_address_orderpreview_trust_page(direct)
            else:
                ok_addr = self._zhongshan_fill_address_orderpreview(direct)
            if not ok_addr:
                return False
            time.sleep(0.35)
        if not state.get("delivery"):
            self._zhongshan_pick_delivery_ld_vs_express_car()
            time.sleep(0.25)
        if not state.get("material_other"):
            self._zhongshan_check_material_purchase_other()
            time.sleep(0.25)

        state2 = self._zhongshan_orderpreview_required_state()
        missing2 = [k for k, v in (state2 or {}).items() if not v]
        if missing2:
            if missing2 == ["delivery"]:
                logger.warning(
                    "  OrderPreview 发货方式复核未识别到选中项，但页面已尝试完成发货方式选择，继续保存"
                )
                return True
            logger.warning(f"  OrderPreview 保存前复核仍缺字段: {missing2}")
            return False
        logger.info("  ✓ OrderPreview 补填后必填项复核通过")
        return True

    def open_quotation_edit_form(
        self,
        material_count=None,
        cart_quantity_target=None,
        cart_line_quantities=None,
        cart_line_material_descs=None,
    ):
        """进入购物车并打开报价编辑页（OrderPreview），不填写表单。"""
        self.navigate_to_cart()
        self.cart_select_products_and_generate_quotation(
            material_count,
            cart_quantity_target=cart_quantity_target,
            cart_line_quantities=cart_line_quantities,
            cart_line_material_descs=cart_line_material_descs,
        )
        ok = self._wait_for_quotation_edit_form(timeout=25)
        if not ok:
            logger.warning("  未在超时内检测到报价编辑表单，仍可能需人工核对页面")
        return ok

    def generate_zhongshan_quotation(
        self,
        project_name,
        ladder_no,
        oms_address=None,
        oms_recipient=None,
        oms_direct_address=None,
        after_manual_pause=False,
    ):
        """
        中山报价单：
        - 姓(收货人)：OMS 技术员 + 技术员联系方式（oms_recipient）
        - 收货地址：直发地址；省/市下拉 + 第三格完整地址
        - 发货方式：零担 vs 快递（汽车）比价选低
        - 项目名称；物料购买类别选「其他」；保存订价单

        若当前为 **Order/OrderPreview/** 页面（存在 ddlProvince/ddlCity/txtAddress），
        则走专用分支，不再依赖旧版「表格收货地址」扫描。
        """
        logger.info(f"生成中山报价单: {project_name}/{ladder_no}")

        direct = (oms_direct_address or oms_address or "").strip()
        recipient = (oms_recipient or "").strip()
        is_op = self._zhongshan_is_order_preview_page()
        if is_op:
            logger.info("  检测到 OrderPreview 表单 (ddlProvince/ddlCity)，使用专用填写逻辑")
        if after_manual_pause:
            logger.info("  中山续填模式：以页面已填内容为准，仅补全仍空的必填项")

        if is_op:
            # OrderPreview 填写项目名称时会触发表单联动，可能清空收货人/地址/发货方式。
            # 因此项目名称必须先填，随后再填其它必填项，保存前再复核一次。
            logger.info("  [中山 1/6] 项目名称（先填，避免后续字段被清空）")
            self._fill_quotation_project_name_extended(project_name, ladder_no)
            time.sleep(0.6)

            logger.info("  [中山 2/6] 收货人")
            state0 = self._zhongshan_orderpreview_required_state() or {}
            if after_manual_pause and state0.get("recipient"):
                logger.info("  ✓ 收货人/电话已由人工填写，跳过")
            elif after_manual_pause and not recipient:
                if not state0.get("recipient"):
                    raise RuntimeError(
                        "中山报价单：人工暂停后收货人/电话仍为空，请填写后按 Enter 再继续。"
                        "可接受：姓一栏写「姓名+电话」、或姓/名/电话分栏、或姓+电话（名可空）。"
                    )
            else:
                self._zhongshan_fill_recipient_orderpreview(recipient)
            time.sleep(0.45)

            logger.info("  [中山 3/6] 收货地址（须先完成，否则发货方式不可用）")
            if after_manual_pause:
                ok_addr = self._zhongshan_fill_address_orderpreview_trust_page(direct)
            else:
                ok_addr = self._zhongshan_fill_address_orderpreview(direct)
            if not ok_addr:
                raise RuntimeError(
                    "中山报价单：收货地址（省/市/详细）填写失败，已中止后续发货方式与保存。"
                    "请确认直发地址文本或页面结构。"
                )
            time.sleep(0.65)

            logger.info("  [中山 4/6] 发货方式（零担 / 快递（汽车）比价）")
            self._zhongshan_pick_delivery_ld_vs_express_car()
            time.sleep(0.4)

            logger.info("  [中山 5/6] 物料购买类别 → 其他")
            self._zhongshan_check_material_purchase_other()
            time.sleep(0.35)

            if not self._zhongshan_orderpreview_refill_required(
                project_name,
                ladder_no,
                direct,
                recipient,
                after_manual_pause=after_manual_pause,
            ):
                raise RuntimeError(
                    "中山报价单：OrderPreview 保存前必填项仍有空白，已中止保存。"
                    "请核对收货人、收货地址、发货方式、项目名称、物料购买类别。"
                )
        else:
            # 旧版表单保持原流程。
            logger.info("  [中山 1/6] 收货人")
            if after_manual_pause and recipient:
                self._zhongshan_fill_recipient_line(recipient)
            elif not after_manual_pause:
                self._zhongshan_fill_recipient_line(recipient)
            elif not self._zhongshan_fill_recipient_line(recipient):
                logger.info("  旧版表单：假定收货人已由人工填写")
            time.sleep(0.45)

            logger.info("  [中山 2/6] 收货地址（须先完成，否则发货方式不可用）")
            if after_manual_pause:
                ok_addr = self._zhongshan_fill_address_sequential(direct) if direct else True
            else:
                ok_addr = self._zhongshan_fill_address_sequential(direct)
            if not ok_addr:
                raise RuntimeError(
                    "中山报价单：收货地址（省/市/详细）填写失败，已中止后续发货方式与保存。"
                    "请确认直发地址文本或页面结构。"
                )
            time.sleep(0.65)

            logger.info("  [中山 3/6] 发货方式（零担 / 快递（汽车）比价）")
            self._zhongshan_pick_delivery_ld_vs_express_car()
            time.sleep(0.4)

            logger.info("  [中山 4/6] 项目名称")
            self._fill_quotation_project_name_extended(project_name, ladder_no)
            time.sleep(0.35)

            logger.info("  [中山 5/6] 物料购买类别 → 其他")
            self._zhongshan_check_material_purchase_other()
            time.sleep(0.35)

        logger.info("  [中山 6/6] 保存订价单")
        time.sleep(0.45)
        try:
            if self._js_quotation_click_save():
                time.sleep(2)
                logger.info("  ✓ 已点击保存订价单/保存报价单(JS)")
            else:
                self.browser.safe_click(*self.SELECTORS["quotation_save_btn"], timeout=12)
                time.sleep(2)
                logger.info("  ✓ 已点击保存订价单/保存报价单")
        except Exception as e:
            logger.warning(f"  保存订价单失败: {e}")
            if self._js_quotation_click_save():
                time.sleep(2)
                logger.info("  ✓ 已保存(JS补救)")
            else:
                raise

        return self.get_quotation_number()

    def _fill_quotation_project_info(self, project_name, ladder_no):
        """在报价单中填写项目信息"""
        # 项目名或梯号，视表单字段而定
        display = project_name if project_name else ladder_no
        info_fields = [
            (By.XPATH, "//input[@name='projectName'] | //input[@placeholder='项目名称']"),
            (By.XPATH, "//input[@name='remark'] | //textarea[@placeholder='备注']"),
        ]
        for by, value in info_fields:
            try:
                elem = self.browser.wait_for_element(by, value, timeout=3)
                if not elem.get_attribute("value"):
                    elem.send_keys(display)
                break
            except:
                continue

    def _find_option(self, keyword):
        """查找下拉选项中包含关键字的option元素"""
        options = self.driver.find_elements(By.TAG_NAME, "option")
        for opt in options:
            if keyword in opt.text:
                return opt
        return None

    def _extract_price(self, text):
        """从文本中提取价格数字"""
        match = re.search(r'[\d,]+\.?\d*', text)
        if match:
            return float(match.group().replace(',', ''))
        return None

    def get_quotation_number(self):
        """从保存成功页或当前页提取报价单号。"""
        no = self._extract_quotation_no_js()
        if no:
            logger.info(f"  ✓ 报价单号: {no}")
            return no
        logger.warning("无法自动提取报价单号")
        return None

    def _extract_quotation_no_js(self):
        try:
            found = self.driver.execute_script("""
                function pickFromText(t) {
                    if (!t) return '';
                    var patterns = [
                        /报价单[号编\\s：:]*([A-Z]{1,3}\\d{6,}[A-Z0-9]*)/i,
                        /订价单[号编\\s：:]*([A-Z]{1,3}\\d{6,}[A-Z0-9]*)/i,
                        /Quotation[\\s#：:]*([A-Z0-9]{6,})/i,
                        /\\b(SP\\d{6,}[A-Z0-9]*)\\b/i,
                        /\\b(R\\d{6,}[A-Z0-9]*)\\b/i,
                        /\\b([A-Z]{1,3}\\d{8,}[A-Z0-9]*)\\b/
                    ];
                    for (var i = 0; i < patterns.length; i++) {
                        var m = t.match(patterns[i]);
                        if (m && m[1]) return m[1];
                    }
                    return '';
                }
                var url = location.href || '';
                var um = url.match(/[?&](?:quotation|quote|order)[Nn]o=([A-Z0-9]+)/i);
                if (um) return um[1];
                var body = document.body.innerText || '';
                var fromBody = pickFromText(body);
                if (fromBody) return fromBody;
                var nodes = document.querySelectorAll('span, div, label, td, h1, h2, h3, strong, b');
                for (var j = 0; j < nodes.length; j++) {
                    var tx = (nodes[j].innerText || '').trim();
                    if (tx.indexOf('报价单') >= 0 || tx.indexOf('订价') >= 0) {
                        var v = pickFromText(tx);
                        if (v) return v;
                    }
                }
                return '';
            """)
            if found:
                return str(found).strip()
        except Exception:
            pass
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            for pat in (
                r"报价单[号编\s：:]*([A-Z]{1,3}\d{6,}[A-Z0-9]*)",
                r"订价单[号编\s：:]*([A-Z]{1,3}\d{6,}[A-Z0-9]*)",
                r"\b(SP\d{6,}[A-Z0-9]*)\b",
                r"\b(R\d{6,}[A-Z0-9]*)\b",
                r"\b([A-Z]{1,3}\d{8,}[A-Z0-9]*)\b",
            ):
                m = re.search(pat, body_text, re.I)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def _js_open_order_quotation_menu(self):
        """备件 → 订单/报价单（或 订单\\报价单）。"""
        for label in ("订单/报价单", "订单报价", "订单\\报价单", "订单"):
            self._js_click_menu_link("备件", visible_only=False)
            time.sleep(0.6)
            if self._js_click_menu_link(label, visible_only=True):
                time.sleep(2)
                return True
            if self._js_click_menu_link(label, visible_only=False):
                time.sleep(2)
                return True
        return False

    def navigate_to_order_quotation_list(self):
        logger.info("导航到: 备件 → 订单/报价单")
        if self._js_open_order_quotation_menu():
            logger.info("  ✓ 已进入订单/报价单")
            return
        try:
            self.browser.safe_click(*self.SELECTORS["order_quote_menu"], timeout=8)
            time.sleep(2)
        except Exception as e:
            raise RuntimeError(f"无法进入订单/报价单: {e}")

    def _search_quotation_no(self, quotation_no):
        """订单/报价单列表：在「报价单号」框填入单号 → 点击搜索。"""
        logger.info(f"搜索报价单号: {quotation_no}")
        result = self.driver.execute_script("""
            var no = arguments[0];
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function setInput(inp) {
                if (!inp || !isVis(inp)) return false;
                inp.focus();
                inp.value = no;
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            function findQuoteInput() {
                var ids = ['txtQuoteNo', 'txtQuotationNo', 'quoteNo', 'quotationNo'];
                for (var i = 0; i < ids.length; i++) {
                    var el = document.getElementById(ids[i]);
                    if (setInput(el)) return true;
                }
                var nodes = document.querySelectorAll('label, span, td, th');
                for (var li = 0; li < nodes.length; li++) {
                    var lb = (nodes[li].textContent || '').replace(/\\s+/g, '');
                    if (lb.indexOf('报价单号') < 0) continue;
                    var parent = nodes[li].parentElement;
                    if (!parent) continue;
                    var inp = parent.querySelector(
                        'input[type=text], input:not([type=hidden]):not([type=checkbox])'
                    );
                    if (setInput(inp)) return true;
                    var row = nodes[li].closest('tr, .form-group, .row, div');
                    if (row) {
                        inp = row.querySelector(
                            'input[type=text], input:not([type=hidden]):not([type=checkbox])'
                        );
                        if (setInput(inp)) return true;
                    }
                }
                var inputs = document.querySelectorAll(
                    'input[type=text], input[type=search], input:not([type])'
                );
                for (var j = 0; j < inputs.length; j++) {
                    var el = inputs[j];
                    var ph = (el.placeholder || '').toLowerCase();
                    var idn = ((el.id || '') + ' ' + (el.name || '')).toLowerCase();
                    if (ph.indexOf('报价单') >= 0 || ph.indexOf('报价') >= 0
                        || idn.indexOf('quotation') >= 0 || idn.indexOf('quote') >= 0) {
                        if (setInput(el)) return true;
                    }
                }
                return false;
            }
            if (!findQuoteInput()) return 'no_input';
            var btn = document.getElementById('btnSearch');
            if (btn && isVis(btn)) {
                btn.click();
                return 'ok';
            }
            var btns = document.querySelectorAll(
                'button, a, input[type=button], input[type=submit]'
            );
            for (var k = 0; k < btns.length; k++) {
                var tx = (btns[k].innerText || btns[k].value || '').replace(/\\s+/g, '');
                if (tx === '搜索' || tx.indexOf('搜索') >= 0
                    || tx.toLowerCase().indexOf('search') >= 0) {
                    btns[k].click();
                    return 'ok';
                }
            }
            return 'no_search_btn';
        """, quotation_no)
        time.sleep(2.5)
        if result == "ok":
            logger.info(f"  ✓ 已填入报价单号并点击搜索: {quotation_no}")
            return True
        if result == "no_input":
            raise RuntimeError(
                f"未找到「报价单号」搜索框，无法搜索 {quotation_no}"
            )
        raise RuntimeError(f"已填入报价单号但未找到「搜索」按钮: {quotation_no}")

    def _click_quotation_row_print(self, quotation_no, project_name=None):
        """
        搜索后按报价单号定位行，点击该行操作列「打印」。
        报价单号唯一；多行命中时可用项目名称二次筛选。
        """
        qno = (quotation_no or "").strip()
        if not qno:
            raise RuntimeError("缺少报价单号，无法在列表中定位报价单行")
        pname = (project_name or "").strip()

        clicked = self.driver.execute_script("""
            var quoteNo = arguments[0];
            var projectName = arguments[1];
            function compact(t) { return (t || '').replace(/\\s+/g, ''); }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function inSidebar(el) {
                return !!(el.closest('aside, nav, .sidebar, #sidebar, .left-menu, .menu-sidebar'));
            }
            function quoteMatchesCell(cellText, qn) {
                var ct = compact(cellText);
                if (!ct || !qn) return false;
                if (ct === qn || ct.indexOf(qn) >= 0 || qn.indexOf(ct) >= 0) return true;
                var base = qn.replace(/V\\d+$/i, '');
                if (base && base !== qn && (ct === base || ct.indexOf(base) >= 0 || base.indexOf(ct) >= 0)) {
                    return true;
                }
                return false;
            }
            function rowHasQuoteNo(row) {
                var qn = compact(quoteNo);
                if (!qn) return false;
                var cells = row.querySelectorAll('td');
                for (var ci = 0; ci < cells.length; ci++) {
                    if (quoteMatchesCell(cells[ci].textContent || '', qn)) return true;
                }
                return quoteMatchesCell(row.textContent || '', qn);
            }
            function rowHasProject(row) {
                if (!projectName) return false;
                var text = row.textContent || '';
                if (text.indexOf(projectName) >= 0) return true;
                var key = compact(projectName).substring(0, Math.min(12, projectName.length));
                return key && compact(text).indexOf(key) >= 0;
            }
            function isPrintControl(el) {
                if (!el || inSidebar(el)) return false;
                var t = compact(el.innerText || el.textContent || el.value || '');
                return t.indexOf('打印') >= 0;
            }
            function clickPrintInRow(row) {
                var links = row.querySelectorAll('a.GridHyperLink, a, button, input[type=button]');
                for (var j = 0; j < links.length; j++) {
                    if (!isPrintControl(links[j]) || !isVis(links[j])) continue;
                    try { links[j].scrollIntoView({block: 'center'}); } catch(e) {}
                    links[j].click();
                    return true;
                }
                return false;
            }
            var rows = document.querySelectorAll('table tbody tr');
            var matched = [];
            for (var i = 0; i < rows.length; i++) {
                var row = rows[i];
                if (inSidebar(row)) continue;
                if (!rowHasQuoteNo(row)) continue;
                matched.push(row);
            }
            if (matched.length === 0) {
                return 'no_row';
            }
            var target = null;
            if (matched.length === 1) {
                target = matched[0];
            } else if (projectName) {
                for (var mi = 0; mi < matched.length; mi++) {
                    if (rowHasProject(matched[mi])) {
                        target = matched[mi];
                        break;
                    }
                }
            }
            if (!target) target = matched[0];
            if (clickPrintInRow(target)) return 'ok';
            return 'no_print';
        """, qno, pname)

        if clicked == "ok":
            logger.info(f"  ✓ 已按报价单号点击操作列「打印」: {qno}")
            time.sleep(2.5)
            return True

        if clicked == "no_row":
            raise RuntimeError(
                f"搜索后未找到报价单号「{qno}」对应的列表行"
            )
        raise RuntimeError(
            f"已找到报价单号「{qno}」对应行，但操作列无「打印」按钮"
        )

    def _ensure_cdp_download_dir(self):
        """iframe 内报表导出时，用 CDP 再次声明下载目录（prefs 对 iframe 常不生效）。"""
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
                self.driver.execute_cdp_cmd(cmd, params)
                logger.info(f"  ✓ CDP 下载目录: {dl}")
                return
            except Exception:
                continue

    def close_print_page(self):
        """下载等待结束后关闭打印页并回到列表。"""
        self._leave_print_page()

    def _switch_to_print_page(self, handles_before):
        """
        点击「打印」后切换到报表页：优先新标签，其次含 Export 提示的 iframe。
        返回是否已离开列表页上下文。
        """
        list_handle = self.driver.current_window_handle
        self._print_page_list_handle = list_handle
        self._print_page_handle = None
        self._print_page_in_iframe = False

        deadline = time.time() + 15
        new_handle = None
        while time.time() < deadline:
            for h in self.driver.window_handles:
                if h not in handles_before:
                    new_handle = h
                    break
            if new_handle:
                break
            time.sleep(0.4)

        if new_handle:
            self.driver.switch_to.window(new_handle)
            self._print_page_handle = new_handle
            time.sleep(2.5)
            logger.info(
                f"  ✓ 已切换到打印页标签: "
                f"{(self.driver.title or '')[:50]} | {self.driver.current_url[:80]}"
            )
            return True

        if self._try_switch_print_iframe():
            self._print_page_in_iframe = True
            return True

        logger.warning("  未检测到新标签/报表 iframe，仍在当前页尝试查找 Export…")
        return False

    def _try_switch_print_iframe(self):
        """若报表嵌在 iframe，切入第一个含 Export/导出 提示的 frame。"""
        self.driver.switch_to.default_content()
        frames = self.driver.find_elements(By.TAG_NAME, "iframe")
        for idx, frame in enumerate(frames):
            try:
                self.driver.switch_to.default_content()
                self.driver.switch_to.frame(frame)
                hint = self.driver.execute_script("""
                    function label(el) {
                        if (!el) return '';
                        return [
                            el.innerText, el.textContent, el.value,
                            el.getAttribute('title'), el.getAttribute('aria-label'),
                            el.getAttribute('alt')
                        ].filter(Boolean).join(' ').replace(/\\s+/g, '');
                    }
                    var nodes = document.querySelectorAll(
                        'button, a, span, div, li, [role=button], [class*="Tool"], [class*="tool"]'
                    );
                    for (var i = 0; i < nodes.length; i++) {
                        var tx = label(nodes[i]).toLowerCase();
                        if (tx.indexOf('export') >= 0 || tx.indexOf('导出') >= 0
                            || tx.indexOf('pdf') >= 0) {
                            return true;
                        }
                    }
                    return false;
                """)
                if hint:
                    logger.info(f"  ✓ 打印报表在 iframe[{idx}] 内")
                    return True
            except Exception:
                pass
        self.driver.switch_to.default_content()
        return False

    def _leave_print_page(self):
        """导出后关闭打印标签并回到列表页。"""
        list_handle = getattr(self, "_print_page_list_handle", None)
        print_handle = getattr(self, "_print_page_handle", None)
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass
        if print_handle and print_handle != list_handle:
            try:
                if self.driver.current_window_handle == print_handle:
                    self.driver.close()
            except Exception:
                pass
        if list_handle:
            try:
                if list_handle in self.driver.window_handles:
                    self.driver.switch_to.window(list_handle)
            except Exception:
                pass
        for attr in ("_print_page_list_handle", "_print_page_handle", "_print_page_in_iframe"):
            if hasattr(self, attr):
                delattr(self, attr)

    def _log_print_page_diagnostic(self):
        """Export 失败时记录当前页可见控件，便于 scan_print_page 对照。"""
        try:
            self.driver.switch_to.default_content()
            if getattr(self, "_print_page_in_iframe", False):
                frames = self.driver.find_elements(By.TAG_NAME, "iframe")
                for frame in frames:
                    try:
                        self.driver.switch_to.frame(frame)
                        break
                    except Exception:
                        self.driver.switch_to.default_content()
            info = self.driver.execute_script("""
                function label(el) {
                    if (!el) return '';
                    return [
                        el.innerText, el.textContent, el.value,
                        el.getAttribute('title'), el.getAttribute('aria-label')
                    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().slice(0, 60);
                }
                function isVis(el) {
                    if (!el) return false;
                    var r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }
                var items = [];
                var nodes = document.querySelectorAll(
                    'button, a, span, div, li, input[type=button], [role=button], [class*="sti"], [class*="Tool"]'
                );
                for (var i = 0; i < nodes.length && items.length < 40; i++) {
                    var el = nodes[i];
                    if (!isVis(el)) continue;
                    var tx = label(el);
                    if (!tx) continue;
                    var low = tx.toLowerCase();
                    if (low.indexOf('export') >= 0 || low.indexOf('导出') >= 0
                        || low.indexOf('pdf') >= 0 || low.indexOf('打印') >= 0
                        || low.indexOf('print') >= 0) {
                        items.push({
                            tag: el.tagName,
                            text: tx,
                            cls: (el.className || '').toString().slice(0, 80)
                        });
                    }
                }
                return {
                    url: location.href,
                    title: document.title || '',
                    hints: items
                };
            """)
            logger.error(
                "  打印页诊断(含 export/pdf/打印 关键字控件): "
                f"{json.dumps(info, ensure_ascii=False)[:1200]}"
            )
            self.browser.take_screenshot("print_page_export_fail")
        except Exception as e:
            logger.error(f"  打印页诊断失败: {e}")

    def _reenter_print_iframe(self):
        """导出前确保仍在报表 iframe 内。"""
        if not getattr(self, "_print_page_in_iframe", False):
            return
        self.driver.switch_to.default_content()
        frames = self.driver.find_elements(By.TAG_NAME, "iframe")
        for frame in frames:
            try:
                self.driver.switch_to.frame(frame)
                return
            except Exception:
                self.driver.switch_to.default_content()

    def _click_report_export_toolbar(self):
        """
        报表查看器工具栏：Export 为软盘图标（常无文字，靠 title/alt）。
        先点 Export，再点下拉中的 PDF。
        """
        self._reenter_print_iframe()
        clicked = self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function clickTarget(el) {
                if (!el) return false;
                try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                try { el.click(); return true; } catch(e) {}
                try {
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return true;
                } catch(e2) {}
                return false;
            }
            function exportHint(el) {
                if (!el) return '';
                return [
                    el.getAttribute('title'), el.getAttribute('alt'),
                    el.getAttribute('aria-label'), el.getAttribute('id')
                ].filter(Boolean).join(' ').toLowerCase();
            }
            function findExportControl() {
                var nodes = document.querySelectorAll(
                    'a[title], a img, img[alt], input[type="image"], '
                    + '[id*="Export"], [id*="export"]'
                );
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    var hint = exportHint(el);
                    if (hint.indexOf('export') < 0 && hint.indexOf('导出') < 0) continue;
                    if (!isVis(el)) continue;
                    var clickEl = el;
                    if (el.tagName === 'IMG') {
                        clickEl = el.closest('a') || el.parentElement || el;
                    }
                    return clickEl;
                }
                return null;
            }
            var btn = findExportControl();
            if (!btn) return false;
            return clickTarget(btn) ? 'export' : false;
        """)
        if clicked:
            logger.info("  ✓ 已点击报表工具栏 Export 图标")
            time.sleep(0.8)
            return True

        xpaths = [
            "//a[contains(translate(@title,'EXPORT','export'),'export')]",
            "//a[contains(translate(@title,'EXPORT','export'),'导出')]",
            "//img[contains(translate(@alt,'EXPORT','export'),'export')]/ancestor::a[1]",
            "//input[contains(translate(@alt,'EXPORT','export'),'export')]",
        ]
        for xp in xpaths:
            try:
                el = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, xp))
                )
                ActionChains(self.driver).move_to_element(el).pause(0.2).click(el).perform()
                logger.info("  ✓ 已点击 Export（XPath 兜底）")
                time.sleep(0.8)
                return True
            except TimeoutException:
                continue
        return False

    def _click_report_pdf_menu_item(self):
        """下拉菜单中精确点击文本为 PDF 的项（避免点到 Excel 或整块菜单）。"""
        self._reenter_print_iframe()
        pdf_clicked = self.driver.execute_script("""
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function norm(t) {
                return (t || '').replace(/\\s+/g, ' ').trim();
            }
            function clickTarget(el) {
                if (!el) return false;
                try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                try { el.click(); return true; } catch(e) {}
                try {
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return true;
                } catch(e2) {}
                return false;
            }
            var anchors = document.querySelectorAll('a');
            for (var i = 0; i < anchors.length; i++) {
                var a = anchors[i];
                if (!isVis(a)) continue;
                if (norm(a.innerText || a.textContent).toUpperCase() !== 'PDF') continue;
                if (clickTarget(a)) return 'pdf_a';
            }
            var nodes = document.querySelectorAll('span, div, li, td');
            for (var j = 0; j < nodes.length; j++) {
                var el = nodes[j];
                if (!isVis(el)) continue;
                if (norm(el.innerText || el.textContent).toUpperCase() !== 'PDF') continue;
                if (el.children.length > 2) continue;
                var link = el.closest('a') || el.querySelector('a');
                if (clickTarget(link || el)) return 'pdf_node';
            }
            return false;
        """)
        if pdf_clicked:
            logger.info(f"  ✓ 已点击下拉菜单 PDF（{pdf_clicked}）")
            time.sleep(2)
            return True

        pdf_xpaths = [
            "//a[normalize-space()='PDF']",
            "//a[normalize-space(translate(.,'pdf','PDF'))='PDF']",
            "//*[normalize-space()='PDF' and (self::a or self::span)]",
        ]
        for xp in pdf_xpaths:
            try:
                el = WebDriverWait(self.driver, 4).until(
                    EC.element_to_be_clickable((By.XPATH, xp))
                )
                ActionChains(self.driver).move_to_element(el).pause(0.15).click(el).perform()
                logger.info("  ✓ 已点击 PDF（XPath 兜底）")
                time.sleep(2)
                return True
            except TimeoutException:
                continue
        return False

    def _export_print_page_pdf(self):
        if not self._click_report_export_toolbar():
            self._log_print_page_diagnostic()
            raise RuntimeError("订单打印页未找到 Export/导出 按钮（报表工具栏软盘图标）")
        if self._click_report_pdf_menu_item():
            return True
        logger.warning("  首次点击 PDF 失败，重试：再次打开 Export 菜单…")
        self._click_report_export_toolbar()
        if self._click_report_pdf_menu_item():
            return True
        self._log_print_page_diagnostic()
        raise RuntimeError("Export 下拉菜单中未找到或无法点击 PDF 项")

    def export_quotation_pdf(self, quotation_no, project_name=None):
        """
        阶段三：订单/报价单 → 报价单号搜索 → 按报价单号定行 → 操作列「打印」→ Export → PDF。
        浏览器下载目录需在 Browser.start(download_dir=...) 中指定。
        下载完成前勿调用 close_print_page()，以免切走 iframe 导致下载中断。
        """
        self.navigate_to_order_quotation_list()
        self._search_quotation_no(quotation_no)
        handles_before = self.driver.window_handles[:]
        self._click_quotation_row_print(quotation_no, project_name)
        self._switch_to_print_page(handles_before)
        self._ensure_cdp_download_dir()
        self._export_print_page_pdf()
        return True

    def download_pdf(self, sourcing_no=None):
        """已废弃：请使用 export_quotation_pdf(quotation_no)。"""
        logger.warning("download_pdf 已废弃，请使用 export_quotation_pdf")
        return False

    def download_excel(self):
        """备件网侧 Excel（非 OMS）；保留占位。"""
        logger.info("下载报价Excel...")
        try:
            self.browser.safe_click(*self.SELECTORS["download_excel"], timeout=10)
            time.sleep(2)
            logger.info("  ✓ Excel下载已触发")
        except Exception as e:
            logger.error(f"  Excel下载失败: {e}")
            return False
        return True

    # ========================================================================
    # 主流程入口
    # ========================================================================
    def run_single_inquiry(self, project_name, ladder_no, items, photos=None, remark=None):
        """
        【第一阶段】执行单组物料的询价单创建:
        新建询价单 → 逐条添加 → 提交 → 返回询价单号

        审批等待环节已移除 — 改为人工确认后通过 --resume 模式继续。

        参数:
            project_name: 项目名称
            ladder_no: 梯号
            items: 物料列表，每项含 material_desc、quantity，可选 material_no(OMS工厂料号)
            photos: 照片路径列表
            remark: OMS「备注」列内容

        返回: {
            "inquiry_no": str,
            "project_name": str,
            "ladder_no": str,
            "success": bool,
            "material_count": int
        }
        """
        result = {
            "project_name": project_name,
            "ladder_no": ladder_no,
            "inquiry_no": None,
            "success": False,
            "material_count": len(items) if items else 0
        }
        
        # 创建询价单（内部自动逐条添加+最后提交）
        inquiry_no = self.create_inquiry(
            project_name=project_name,
            ladder_no=ladder_no,
            items=items,
            photos=photos,
            remark=remark,
        )
        
        if inquiry_no:
            result["inquiry_no"] = inquiry_no
            result["success"] = True
        elif (
            self._is_on_new_inquiry_form_page()
            and self._inquiry_form_has_material_rows()
        ):
            logger.error(
                f"询价单未成功提交（页面仍在编辑态，物料表有行）: {ladder_no}。"
                f"不会继续 OMS 发邮件。"
            )
            result["success"] = False
        elif self._is_on_inquiry_list_page():
            logger.warning(
                f"询价单可能已提交但未能提取单号: {ladder_no}，流程继续"
            )
            result["inquiry_no"] = ""
            result["success"] = True
        else:
            logger.warning(
                f"未能提取询价单号且无法确认提交状态: {ladder_no}，"
                f"为安全起见不继续 OMS 发邮件"
            )
            result["success"] = False

        return result

    def ensure_cart_quantity_aligned(
        self,
        cart_quantity_target,
        material_count=None,
        cart_line_quantities=None,
        cart_line_material_descs=None,
    ):
        """进入「查看购物车」页，将行数量与 OMS 对齐。"""
        line_qty_norm = self._cart_normalize_line_quantities(
            cart_line_quantities or []
        )
        target = self._cart_resolve_quantity_target(
            cart_quantity_target, cart_line_quantities
        )
        if not target and not line_qty_norm:
            return True
        self.navigate_to_cart()
        row_count = self._cart_count_material_rows()
        self._cart_check_row_count_for_align(
            row_count, material_count, cart_line_material_descs
        )
        logger.info(
            "  购物车页核对数量（"
            + self._cart_describe_quantity_align_goal(
                cart_quantity_target,
                cart_line_quantities,
                cart_line_material_descs,
                material_count,
            )
            + "）…"
        )
        qty_ok = self._cart_align_line_quantities_with_oms(
            cart_quantity_target,
            material_count,
            cart_line_quantities,
            cart_line_material_descs,
        )
        if not qty_ok:
            self._cart_log_rows_snapshot()
            extra = ""
            fails = getattr(self, "_last_cart_qty_fails", None) or []
            if fails:
                parts = []
                for f in fails[:4]:
                    if f.get("reason") == "no_row":
                        parts.append(
                            f"第{f.get('line')}条在购物车找不到对应行"
                        )
                    else:
                        parts.append(
                            f"第{f.get('line')}条 OMS={f.get('want')} "
                            f"购物车={f.get('got')}"
                        )
                extra = "（" + "；".join(parts) + "）"
            raise RuntimeError(
                self._cart_quantity_align_failure_message(
                    cart_quantity_target,
                    cart_line_quantities,
                    cart_line_material_descs,
                    material_count,
                ).replace("已中止生成报价单", "请改好后点「更新购物车」")
                + extra
            )
        return True

    def _cart_contains_material_hint(self, material_hint):
        """购物车列表中是否已有含该物料描述片段的行。"""
        if not material_hint or len(str(material_hint).strip()) < 3:
            return False
        try:
            return bool(
                self.driver.execute_script(
                    """
                var hint = (arguments[0] || '').replace(/\\s+/g, '').toLowerCase();
                if (hint.length > 10) hint = hint.slice(0, 10);
                var tables = document.querySelectorAll('table');
                for (var ti = 0; ti < tables.length; ti++) {
                    var tbl = tables[ti];
                    if ((tbl.innerText || '').indexOf('产品描述') < 0) continue;
                    var rows = tbl.querySelectorAll('tbody tr');
                    for (var ri = 0; ri < rows.length; ri++) {
                        var row = rows[ri];
                        if (row.querySelector('th')) continue;
                        var tx = (row.innerText || '').replace(/\\s+/g, '').toLowerCase();
                        if (tx.length < 8) continue;
                        if (tx.indexOf(hint) >= 0) return true;
                    }
                }
                return false;
                """,
                    str(material_hint).strip()[:60],
                )
            )
        except Exception:
            return False

    def resume_single_inquiry(
        self,
        inquiry_no,
        project_name,
        ladder_no,
        material_desc=None,
        cart_quantity_target=None,
        material_count=None,
        cart_line_quantities=None,
        cart_line_material_descs=None,
    ):
        """
        【第二阶段】续接单个询价单流程:
        单次检查审批 → (已完结则)查看 → 全选 → 加入购物车 → 查看购物车并核对数量

        参数:
            inquiry_no: 询价单号
            project_name: 项目名称（供日志用）
            ladder_no: 梯号（供日志用）
            cart_quantity_target: OMS 数量（加入购物车后于购物车页对齐）

        返回: {
            "inquiry_no": str,
            "project_name": str,
            "ladder_no": str,
            "approved": bool,
            "success": bool
        }
        """
        result = {
            "inquiry_no": inquiry_no,
            "project_name": project_name,
            "ladder_no": ladder_no,
            "approved": False,
            "success": False
        }
        
        # 单次检查审批状态
        approved = self.check_approval_once(inquiry_no)
        result["approved"] = approved
        
        if not approved:
            logger.warning(f"询价单 {inquiry_no} ({ladder_no}) 尚未完结，跳过")
            return result
        
        # 查看 → 展开备注 → 勾选 → 加入购物车 → 查看购物车核对数量
        try:
            line_descs = [
                str(d or "").strip()
                for d in (cart_line_material_descs or [])
                if str(d or "").strip()
            ]
            hint = (material_desc or "").strip()
            if not line_descs and hint:
                line_descs = [hint]
            skipped_add = False
            if line_descs:
                try:
                    self.navigate_to_cart()
                    row_count = self._cart_count_material_rows()
                    all_present = self._cart_all_oms_lines_present(line_descs)
                    need_count = material_count or len(line_descs)
                    if (
                        all_present
                        and row_count == need_count
                        and row_count >= len(line_descs)
                    ):
                        logger.info(
                            f"  购物车已有全部 {len(line_descs)} 条 OMS 物料"
                            f"（共 {row_count} 行），跳过重复加购"
                        )
                        skipped_add = True
                    elif row_count > 0 and not all_present:
                        logger.warning(
                            f"  购物车有 {row_count} 行，但未找全 OMS "
                            f"{len(line_descs)} 条物料，将重新加入购物车"
                            f"（若重复请先手动清空购物车）"
                        )
                    elif row_count > need_count:
                        logger.warning(
                            f"  购物车行数({row_count})多于 OMS({need_count})，"
                            f"请先清空购物车再 --resume"
                        )
                except Exception:
                    pass
            if not skipped_add:
                self.view_inquiry(inquiry_no)
                self.select_all_and_add_to_cart()
                logger.info(f"✓ 询价单 {inquiry_no} ({ladder_no}) 已加入购物车")
            else:
                self.ensure_detail_po_factory(inquiry_no)
            line_qty_norm = self._cart_normalize_line_quantities(
                cart_line_quantities or []
            )
            if line_qty_norm or self._cart_resolve_quantity_target(
                cart_quantity_target, cart_line_quantities
            ):
                self.ensure_cart_quantity_aligned(
                    cart_quantity_target,
                    material_count=material_count,
                    cart_line_quantities=cart_line_quantities,
                    cart_line_material_descs=cart_line_material_descs,
                )
            result["success"] = True
            if self.last_detail_po_factory:
                result["po_factory"] = self.last_detail_po_factory
        except Exception as e:
            logger.error(
                f"询价单 {inquiry_no} ({ladder_no}) 加入购物车/数量核对失败: {e}"
            )
            raise

        return result

    def run_quotation(
        self,
        factory_type,
        project_name,
        ladder_no,
        oms_address=None,
        material_count=None,
        oms_recipient=None,
        oms_direct_address=None,
        cart_quantity_target=None,
        cart_line_quantities=None,
        cart_line_material_descs=None,
        quotation_form_already_open=False,
        zhongshan_after_manual_pause=False,
    ):
        """
        生成报价单：先进入购物车 → 勾选物料 → 生成报价单 → 再填发货/地址/保存。

        factory_type: "松江" or "中山"
        quotation_form_already_open: 报价编辑页已打开（中山人工暂停后续填）
        zhongshan_after_manual_pause: 中山续填模式，只信页面并补全缺失地址分项
        """
        if not quotation_form_already_open:
            self.open_quotation_edit_form(
                material_count,
                cart_quantity_target=cart_quantity_target,
                cart_line_quantities=cart_line_quantities,
                cart_line_material_descs=cart_line_material_descs,
            )

        if factory_type == "松江":
            quotation_no = self.generate_songjiang_quotation(
                project_name,
                ladder_no,
                oms_recipient=(oms_recipient or "").strip(),
            )
        elif factory_type == "中山":
            quotation_no = self.generate_zhongshan_quotation(
                project_name,
                ladder_no,
                oms_address=oms_address,
                oms_recipient=oms_recipient,
                oms_direct_address=oms_direct_address,
                after_manual_pause=zhongshan_after_manual_pause,
            )
        else:
            logger.error(f"未知工厂类型: {factory_type}")
            return None

        if quotation_no:
            logger.info(f"报价单生成成功: {quotation_no}")

        return quotation_no

