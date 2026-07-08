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


class OMSAttachmentsMixin:
    def _normalize_project_name(project_name):
        return (project_name or "").strip() or "unknown"

    @classmethod
    def project_attachments_dir(cls, project_name):
        """本项目 OMS 附件缓存目录（与其他项目隔离，目录名含项目名哈希）。"""
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            OMS_ATTACHMENTS_CACHE_DIR,
        )
        name = cls._normalize_project_name(project_name)
        safe = re.sub(r'[<>:"/\\|?*]', "_", name[:60]).strip() or "unknown"
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        path = os.path.join(base, f"{safe}_{digest}")
        os.makedirs(path, exist_ok=True)
        return os.path.abspath(path)

    @classmethod
    def path_belongs_to_project(cls, path, project_name):
        """附件路径是否位于该项目的专属缓存目录内。"""
        if not path or not project_name:
            return False
        try:
            p = os.path.abspath(str(path))
            if not os.path.isfile(p):
                return False
            root = cls.project_attachments_dir(project_name)
            return os.path.commonpath([p, root]) == root
        except (ValueError, OSError):
            return False

    @classmethod
    def filter_attachment_paths(cls, paths, project_name):
        """只保留属于该项目的本地附件路径。"""
        out = []
        for p in paths or []:
            p = str(p or "").strip()
            if not p:
                continue
            if cls.path_belongs_to_project(p, project_name):
                ap = os.path.abspath(p)
                if ap not in out:
                    out.append(ap)
        return out

    @classmethod
    def sanitize_group_attachments(cls, project_name, items):
        """确保组内附件仅绑定当前项目，剔除跨项目或越界路径。"""
        project_name = cls._normalize_project_name(project_name)
        if project_name == "unknown":
            return items
        for item in items:
            item["attachment_project"] = project_name
            raw = list(item.get("attachments") or [])
            scoped = cls.filter_attachment_paths(raw, project_name)
            if raw and len(scoped) < len([p for p in raw if p]):
                logger.warning(
                    f"  已忽略 {len(raw) - len(scoped)} 个非本项目附件路径"
                    f"（仅允许使用「{project_name[:36]}」目录下的文件）"
                )
            item["attachments"] = scoped
            bound = (item.get("attachment_project") or "").strip()
            if bound and bound != project_name:
                item["attachments"] = []
                item["has_attachment"] = False
        return items

    @classmethod
    def _infer_has_attachment(cls, item, project_name=None):
        """根据 OMS 行字段推断是否有实物照片/附件。"""
        if item.get("has_attachment") in (True, "true", "True", 1, "1"):
            return True
        project_name = (
            project_name
            or (item.get("attachment_project") or "").strip()
            or (item.get("project_name") or "").strip()
        )
        existing = item.get("attachments") or []
        if project_name:
            if cls.filter_attachment_paths(existing, project_name):
                return True
        elif existing and any(os.path.isfile(str(p)) for p in existing):
            return True

        positive = {"有", "是", "yes", "y", "true", "1", "有附件", "有照片", "有图"}
        negative = {"无", "否", "no", "n", "false", "0", "无附件", "没有", "无照片"}

        for k, v in item.items():
            if str(k).startswith("col_"):
                continue
            kn = cls._normalize_header_name(k)
            if not any(kw in kn for kw in OMS_ATTACHMENT_HEADER_KEYWORDS):
                continue
            sv = str(v or "").strip().lower()
            if sv in {x.lower() for x in positive}:
                return True
            if sv in {x.lower() for x in negative}:
                return False
        return False

    def _attachments_cache_dir(self, project_name):
        return self.project_attachments_dir(project_name)

    def _download_url_to_file(self, url, dest_path):
        """用当前浏览器 Cookie 下载图片 URL。"""
        driver = self.browser.driver
        cookie_parts = []
        for c in driver.get_cookies():
            cookie_parts.append(f"{c['name']}={c['value']}")
        headers = {
            "Cookie": "; ".join(cookie_parts),
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if len(data) < 100:
            raise ValueError("下载内容过小，可能不是有效图片")
        with open(dest_path, "wb") as f:
            f.write(data)
        return dest_path

    def _close_attachment_dialog(self):
        """关闭附件预览/下载弹窗。"""
        try:
            self.browser.driver.execute_script("""
                var labels = ['关闭', '取消', '×', 'close'];
                function vis(el) {
                    if (!el) return false;
                    try {
                        var r = el.getBoundingClientRect();
                        return r.width > 2 && r.height > 2;
                    } catch (e) { return false; }
                }
                var roots = document.querySelectorAll(
                    '.layui-layer, .modal, [role="dialog"]'
                );
                for (var i = roots.length - 1; i >= 0; i--) {
                    if (!vis(roots[i])) continue;
                    var btns = roots[i].querySelectorAll('a, button, span, i');
                    for (var j = 0; j < btns.length; j++) {
                        var t = (btns[j].innerText || btns[j].textContent || '').trim();
                        if (labels.indexOf(t) >= 0 || t === '×') {
                            btns[j].click();
                            return true;
                        }
                    }
                }
                var closeBtn = document.querySelector('.layui-layer-close');
                if (closeBtn && vis(closeBtn)) { closeBtn.click(); return true; }
                return false;
            """)
        except Exception:
            pass
        time.sleep(0.3)

    def _mark_visible_attachment_layer(self, driver):
        """在当前 document 中标记标题含「附件」的 layui 弹层。"""
        return driver.execute_script("""
            function vis(el) {
                if (!el) return false;
                try {
                    var r = el.getBoundingClientRect();
                    return r.width > 80 && r.height > 80;
                } catch (e) { return false; }
            }
            document.querySelectorAll('[data-oms-attachment-layer]').forEach(function(el) {
                el.removeAttribute('data-oms-attachment-layer');
            });
            var layers = document.querySelectorAll('.layui-layer');
            for (var i = layers.length - 1; i >= 0; i--) {
                var layer = layers[i];
                if (!vis(layer)) continue;
                var titleEl = layer.querySelector('.layui-layer-title');
                var title = (titleEl && titleEl.textContent || '').trim();
                if (!/^附件$/.test(title) && title.indexOf('附件') < 0) continue;
                layer.setAttribute('data-oms-attachment-layer', '1');
                var iframe = layer.querySelector('iframe');
                return {
                    found: true,
                    hasInnerIframe: !!(iframe && iframe.offsetWidth > 10)
                };
            }
            return {found: false};
        """)

    def _has_attachment_table_in_context(self, driver):
        return driver.execute_script("""
            var tables = document.querySelectorAll('table');
            for (var i = 0; i < tables.length; i++) {
                var txt = tables[i].innerText || '';
                if (/附件名/.test(txt) && /上传日期/.test(txt)) return true;
            }
            return false;
        """)

    def _enter_attachment_dialog_context(self):
        """
        进入「附件」弹窗所在 document。
        只认标题为「附件」的 layui-layer；仅滚弹窗内表格，不碰背景 PSM 列表。
        """
        driver = self.browser.driver
        driver.switch_to.default_content()

        def _try_current_doc():
            meta = self._mark_visible_attachment_layer(driver)
            if not meta or not meta.get("found"):
                return False
            if meta.get("hasInnerIframe"):
                try:
                    layer_iframe = driver.find_element(
                        By.CSS_SELECTOR,
                        '.layui-layer[data-oms-attachment-layer="1"] iframe',
                    )
                    driver.switch_to.frame(layer_iframe)
                except Exception:
                    return False
            return self._has_attachment_table_in_context(driver)

        if _try_current_doc():
            return True

        page_iframes = driver.find_elements(By.CSS_SELECTOR, "iframe")
        for iframe in page_iframes:
            try:
                if not iframe.is_displayed():
                    continue
            except Exception:
                continue
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(iframe)
                if _try_current_doc():
                    return True
            except Exception:
                pass
        driver.switch_to.default_content()
        return False

    def _leave_attachment_dialog_context(self):
        driver = self.browser.driver
        try:
            driver.switch_to.default_content()
            driver.execute_script("""
                document.querySelectorAll('[data-oms-attachment-layer]').forEach(function(el) {
                    el.removeAttribute('data-oms-attachment-layer');
                });
            """)
        except Exception:
            pass

    def _selenium_scroll_attachment_table(self, driver):
        """仅在「附件」弹窗内部横向滚动，避免误滚背景 PSM 筛选表。"""
        scrolled = driver.execute_script("""
            function scrollIn(root) {
                if (!root) return 0;
                var n = 0;
                var sels = [
                    '.dataTables_scrollBody', '.dataTables_scroll',
                    '.layui-layer-content', '.table-responsive'
                ];
                for (var si = 0; si < sels.length; si++) {
                    root.querySelectorAll(sels[si]).forEach(function(el) {
                        if (el.scrollWidth > el.clientWidth + 12) {
                            el.scrollLeft = el.scrollWidth - el.clientWidth;
                            n++;
                        }
                    });
                }
                root.querySelectorAll('table').forEach(function(tbl) {
                    var wrap = tbl.closest('.dataTables_wrapper')
                        || tbl.parentElement;
                    if (wrap && wrap.scrollWidth > wrap.clientWidth + 12) {
                        wrap.scrollLeft = wrap.scrollWidth - wrap.clientWidth;
                        n++;
                    }
                });
                return n;
            }
            var n = 0;
            var layer = document.querySelector('[data-oms-attachment-layer="1"]');
            if (layer) {
                n += scrollIn(layer);
            } else {
                var tables = document.querySelectorAll('table');
                for (var i = 0; i < tables.length; i++) {
                    var txt = tables[i].innerText || '';
                    if (!/附件名/.test(txt) || !/上传日期/.test(txt)) continue;
                    var scope = tables[i].closest('.dataTables_wrapper')
                        || tables[i].closest('.layui-layer-content')
                        || tables[i].parentElement;
                    n += scrollIn(scope || tables[i]);
                    break;
                }
            }
            return n;
        """)
        if not scrolled:
            return

        logger.info(f"  附件弹窗内横向滚动 {scrolled} 个容器")
        try:
            scroll_el = None
            for sel in (".dataTables_scrollBody", ".dataTables_scroll"):
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    try:
                        if not el.is_displayed():
                            continue
                        parent = driver.execute_script(
                            "return arguments[0].closest('[data-oms-attachment-layer]') "
                            "|| (function(){"
                            "  var t=arguments[0].closest('table');"
                            "  if(!t)return null;"
                            "  var x=t.innerText||'';"
                            "  return /附件名/.test(x)&&/上传日期/.test(x)?t:null;"
                            "})();",
                            el,
                        )
                        if parent is not None or driver.execute_script(
                            "var t=arguments[0].closest('table');"
                            "if(!t)return false;"
                            "var x=t.innerText||'';"
                            "return /附件名/.test(x)&&/上传日期/.test(x);",
                            el,
                        ):
                            scroll_el = el
                            break
                    except Exception:
                        continue
                if scroll_el:
                    break
            if scroll_el:
                ActionChains(driver).move_to_element(scroll_el).click().perform()
                for _ in range(20):
                    scroll_el.send_keys(Keys.ARROW_RIGHT)
        except Exception:
            pass

    def _download_from_attachment_dialog_operation(self, cache_dir, sourcing_no=""):
        """
        在「附件」弹窗中横向滚动至「操作」列，逐一点击下载图标触发浏览器下载。
        弹窗内容常在 iframe 内；表格横向滚动条在 dataTables 容器上。
        """
        driver = self.browser.driver
        if not self._enter_attachment_dialog_context():
            logger.warning("  未定位到「附件」弹窗内的附件表格")
            return []

        downloaded = []

        def _run_dialog_download_js():
            return driver.execute_script("""
                var sourcingNo = (arguments[0] || '').trim();
                function vis(el) {
                    if (!el) return false;
                    try {
                        var r = el.getBoundingClientRect();
                        return r.width > 2 && r.height > 2;
                    } catch (e) { return false; }
                }
                function norm(s) {
                    return (s || '').replace(/\\s+/g, '');
                }
                function findScope() {
                    var layer = document.querySelector('[data-oms-attachment-layer="1"]');
                    if (layer) return layer;
                    var tables = document.querySelectorAll('table');
                    for (var i = 0; i < tables.length; i++) {
                        if (!vis(tables[i])) continue;
                        var txt = tables[i].innerText || '';
                        if (/附件名/.test(txt) && /上传日期/.test(txt)) {
                            return tables[i].closest('.dataTables_wrapper')
                                || tables[i].closest('.layui-layer-content')
                                || tables[i].parentElement || tables[i];
                        }
                    }
                    var layers = document.querySelectorAll('.layui-layer');
                    for (var j = layers.length - 1; j >= 0; j--) {
                        if (!vis(layers[j])) continue;
                        var t = layers[j].querySelector('.layui-layer-title');
                        if (t && /附件/.test(t.textContent || '')) return layers[j];
                    }
                    return document.body;
                }
                function forceHorizontalScroll(scope) {
                    var log = [], rounds = 3;
                    for (var round = 0; round < rounds; round++) {
                        var nodes = scope.querySelectorAll('*');
                        for (var n = 0; n < nodes.length; n++) {
                            var el = nodes[n];
                            if (!el || el.scrollWidth <= el.clientWidth + 12) continue;
                            var before = el.scrollLeft;
                            el.scrollLeft = el.scrollWidth - el.clientWidth;
                            if (el.scrollLeft !== before) {
                                log.push({
                                    tag: el.tagName,
                                    cls: (el.className || '').slice(0, 60),
                                    before: before,
                                    after: el.scrollLeft,
                                    max: el.scrollWidth - el.clientWidth
                                });
                            }
                            try {
                                el.dispatchEvent(new WheelEvent('wheel', {
                                    deltaX: 800, deltaY: 0, bubbles: true, cancelable: true
                                }));
                            } catch (we) {}
                        }
                    }
                    scope.querySelectorAll(
                        '.dataTables_scrollBody, .dataTables_wrapper, '
                        + '.layui-layer-content, .table-responsive, table'
                    ).forEach(function(el) {
                        if (el.scrollWidth > el.clientWidth + 12) {
                            el.scrollLeft = el.scrollWidth - el.clientWidth;
                        }
                    });
                    return log;
                }
                function isDeleteControl(el) {
                    var blob = (
                        (el.className || '') + ' ' +
                        (el.getAttribute('title') || '') + ' ' +
                        (el.getAttribute('onclick') || '') + ' ' +
                        (el.innerText || '')
                    ).toLowerCase();
                    return /删除|remove|trash|glyphicon-trash|fa-trash|icon-shanchu|icon-delete|shanchu/.test(blob);
                }
                function pickDownloadInCell(opCell) {
                    if (!opCell) return [];
                    var links = opCell.querySelectorAll('a');
                    for (var i = 0; i < links.length; i++) {
                        if (!isDeleteControl(links[i])) return [links[i]];
                    }
                    var candidates = opCell.querySelectorAll(
                        'a, button, [onclick], i, span'
                    );
                    for (var j = 0; j < candidates.length; j++) {
                        var c = candidates[j];
                        if (isDeleteControl(c)) continue;
                        if (c.tagName === 'A' || c.getAttribute('onclick')) return [c];
                        var p = c.parentElement;
                        if (p && (p.tagName === 'A' || p.getAttribute('onclick'))
                            && !isDeleteControl(p)) {
                            return [p];
                        }
                    }
                    return [];
                }
                function resolveOpColumnIndex(scope) {
                    var opIdx = -1, headerTable = null;
                    var thNodes = scope.querySelectorAll(
                        '.dataTables_scrollHead th, thead th, thead td, tr:first-child th'
                    );
                    thNodes.forEach(function(th, idx) {
                        var t = norm(th.textContent);
                        if (t === '操作' || t.indexOf('操作') === 0) {
                            opIdx = idx;
                        }
                    });
                    if (opIdx < 0) {
                        scope.querySelectorAll('table').forEach(function(tbl) {
                            if (!vis(tbl)) return;
                            var headers = [];
                            tbl.querySelectorAll('thead th, thead td').forEach(function(th) {
                                headers.push(norm(th.textContent));
                            });
                            var idx = headers.indexOf('操作');
                            if (idx >= 0 && idx > opIdx) {
                                opIdx = idx;
                                headerTable = tbl;
                            }
                        });
                    }
                    return {opIdx: opIdx, headerTable: headerTable};
                }
                function collectRows(scope, opIdx) {
                    var rows = [];
                    var bodyTables = scope.querySelectorAll(
                        '.dataTables_scrollBody table, table.dataTable, table'
                    );
                    var seen = {};
                    bodyTables.forEach(function(tbl) {
                        if (!vis(tbl) || seen[tbl]) return;
                        seen[tbl] = 1;
                        tbl.querySelectorAll('tbody tr').forEach(function(tr) {
                            if (!vis(tr)) return;
                            var rowText = tr.textContent || '';
                            if (sourcingNo && rowText.indexOf(sourcingNo) < 0) return;
                            var tds = tr.querySelectorAll('td');
                            if (!tds.length) return;
                            var cell = null;
                            if (opIdx >= 0 && tds.length > opIdx) {
                                cell = tds[opIdx];
                            } else {
                                cell = tds[tds.length - 1];
                            }
                            var picks = pickDownloadInCell(cell);
                            for (var p = 0; p < picks.length; p++) rows.push(picks[p]);
                        });
                    });
                    return rows;
                }
                var scope = findScope();
                var scrollLog = forceHorizontalScroll(scope);
                var col = resolveOpColumnIndex(scope);
                var opIdx = col.opIdx;
                if (opIdx < 0) {
                    scope.querySelectorAll('th').forEach(function(th) {
                        if (norm(th.textContent).indexOf('操作') >= 0) {
                            var tr = th.parentElement;
                            if (tr) {
                                var ths = tr.querySelectorAll('th, td');
                                for (var hi = 0; hi < ths.length; hi++) {
                                    if (ths[hi] === th) { opIdx = hi; break; }
                                }
                            }
                        }
                    });
                }
                var opHeader = null;
                scope.querySelectorAll('th').forEach(function(th) {
                    if (norm(th.textContent) === '操作' || norm(th.textContent).indexOf('操作') === 0) {
                        opHeader = th;
                    }
                });
                if (opHeader) {
                    try {
                        opHeader.scrollIntoView({block: 'nearest', inline: 'end', behavior: 'instant'});
                    } catch (e) {}
                    forceHorizontalScroll(scope);
                }
                var clickables = collectRows(scope, opIdx);
                if (!clickables.length) {
                    scope.querySelectorAll('tbody tr td:last-child').forEach(function(td) {
                        if (!vis(td)) return;
                        var picks = pickDownloadInCell(td);
                        for (var x = 0; x < picks.length; x++) clickables.push(picks[x]);
                    });
                }
                clickables.forEach(function(el, i) {
                    el.setAttribute('data-oms-att-dl', String(i));
                    try {
                        el.scrollIntoView({block: 'center', inline: 'end', behavior: 'instant'});
                    } catch (e) {}
                    forceHorizontalScroll(scope);
                });
                return JSON.stringify({
                    ok: clickables.length > 0,
                    count: clickables.length,
                    reason: clickables.length ? 'ok' : 'no_clickables',
                    diag: {
                        opIdx: opIdx,
                        scrollSteps: scrollLog.length,
                        scrollSample: scrollLog.slice(0, 4),
                        tableCount: scope.querySelectorAll('table').length,
                        rowCount: scope.querySelectorAll('tbody tr').length,
                        scopeTag: scope.tagName || '',
                        scopeCls: (scope.className || '').slice(0, 40)
                    }
                });
            """, sourcing_no or "")

        try:
            self._selenium_scroll_attachment_table(driver)
            time.sleep(0.35)
            raw = _run_dialog_download_js()
            try:
                meta = json.loads(raw or "{}")
            except json.JSONDecodeError:
                meta = {}

            count = int(meta.get("count") or 0)
            scroll_steps = (meta.get("diag") or {}).get("scrollSteps", 0)
            if count and scroll_steps:
                logger.info(f"  附件表格: JS 横向滚动 {scroll_steps} 步")

            if not count:
                self._selenium_scroll_attachment_table(driver)
                time.sleep(0.4)
                raw = _run_dialog_download_js()
                try:
                    meta = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    meta = {}
                count = int(meta.get("count") or 0)

            if not count:
                diag = meta.get("diag") or {}
                logger.warning(
                    "  附件弹窗已打开，但未找到「操作」列下载按钮"
                    f"（操作列索引={diag.get('opIdx')}, "
                    f"滚动步数={diag.get('scrollSteps')}, "
                    f"表格={diag.get('tableCount')}, 行={diag.get('rowCount')}, "
                    f"范围={diag.get('scopeTag')}.{diag.get('scopeCls')}）"
                )
                return []

            logger.info(
                f"  附件弹窗: 已横向滚动并定位 {count} 个下载按钮，开始点击…"
            )
            max_clicks = min(count, OMS_MAX_ATTACHMENTS_PER_ROW)
            for i in range(max_clicks):
                since = time.time()
                clicked = driver.execute_script("""
                    var scope = document.querySelector('[data-oms-attachment-layer="1"]');
                    if (!scope) {
                        var tables = document.querySelectorAll('table');
                        for (var i = 0; i < tables.length; i++) {
                            var txt = tables[i].innerText || '';
                            if (/附件名/.test(txt) && /上传日期/.test(txt)) {
                                scope = tables[i].closest('.dataTables_wrapper')
                                    || tables[i].parentElement;
                                break;
                            }
                        }
                    }
                    scope = scope || document.body;
                    function forceScroll(root) {
                        root.querySelectorAll(
                            '.dataTables_scrollBody, .dataTables_scroll, .table-responsive'
                        ).forEach(function(el) {
                            if (el.scrollWidth > el.clientWidth + 12) {
                                el.scrollLeft = el.scrollWidth - el.clientWidth;
                            }
                        });
                    }
                    forceScroll(scope);
                    var el = document.querySelector('[data-oms-att-dl="' + arguments[0] + '"]');
                    if (!el) return false;
                    try {
                        el.scrollIntoView({block: 'center', inline: 'end', behavior: 'instant'});
                    } catch (e) {}
                    forceScroll(scope);
                    try { el.click(); return true; } catch (e1) {
                        try {
                            el.dispatchEvent(new MouseEvent('click', {
                                bubbles: true, cancelable: true, view: window
                            }));
                            return true;
                        } catch (e2) { return false; }
                    }
                """, str(i))
                if not clicked:
                    logger.warning(f"  附件下载按钮 {i + 1} 点击失败")
                    continue
                found = wait_for_new_image_download(
                    cache_dir,
                    since_time=since,
                    timeout=OMS_ATTACHMENT_DOWNLOAD_TIMEOUT,
                )
                if found:
                    downloaded.append(found)
                    logger.info(
                        f"  ✓ 已下载 OMS 附件(操作列): {os.path.basename(found)}"
                    )
                else:
                    logger.warning(
                        f"  附件下载按钮 {i + 1} 点击后未检测到新文件"
                    )
                time.sleep(0.5)

            try:
                driver.execute_script("""
                    document.querySelectorAll('[data-oms-att-dl]').forEach(function(el) {
                        el.removeAttribute('data-oms-att-dl');
                    });
                """)
            except Exception:
                pass
        finally:
            self._leave_attachment_dialog_context()

        return downloaded

    def _collect_attachment_urls_from_dialog(self):
        """从附件弹窗收集图片 URL（按尺寸/类型打分，优先 JPG 与原图，最多 3 条候选）。"""
        raw = self.browser.driver.execute_script("""
            function vis(el) {
                if (!el) return false;
                try {
                    var r = el.getBoundingClientRect();
                    return r.width > 2 && r.height > 2;
                } catch (e) { return false; }
            }
            function isImgUrl(u) {
                if (!u || u.indexOf('javascript:') === 0) return false;
                return /\\.(jpg|jpeg|png|gif|bmp|webp)(\\?|$)/i.test(u);
            }
            function scoreImg(img) {
                var w = img.naturalWidth || img.offsetWidth || 0;
                var h = img.naturalHeight || img.offsetHeight || 0;
                if (w < 100 || h < 100) return -1;
                var src = (img.currentSrc || img.src || '').toLowerCase();
                if (/logo|icon|avatar|1x1|spacer|blank|loading/i.test(src)) return -1;
                var area = w * h;
                var bonus = /\\.jpe?g(\\?|$)/i.test(src) ? 80000 : 0;
                return area + bonus;
            }
            var scored = [];
            var layers = document.querySelectorAll(
                '.layui-layer, .modal, [role="dialog"]'
            );
            var roots = [];
            for (var li = 0; li < layers.length; li++) {
                if (vis(layers[li])) roots.push(layers[li]);
            }
            if (!roots.length) roots = [document.body];
            for (var ri = 0; ri < roots.length; ri++) {
                var root = roots[ri];
                var hint = (root.innerText || '').slice(0, 800);
                if (roots.length > 1 && !/附件|照片|图片|预览|查看|下载|实物/i.test(hint)) {
                    continue;
                }
                root.querySelectorAll('img').forEach(function(img) {
                    if (!vis(img)) return;
                    var s = img.currentSrc || img.src || '';
                    if (!isImgUrl(s)) return;
                    var sc = scoreImg(img);
                    if (sc > 0) scored.push({url: s, score: sc});
                });
                root.querySelectorAll('a[href]').forEach(function(a) {
                    if (!vis(a)) return;
                    var h = a.href || '';
                    var t = (a.innerText || a.textContent || '').trim();
                    if (!h || h.indexOf('javascript:') === 0) return;
                    if (!isImgUrl(h) && !/下载|附件|照片|原图|查看/.test(t)) return;
                    var sc = 250000 + (/\\.jpe?g/i.test(h) ? 120000 : 0);
                    scored.push({url: h, score: sc});
                });
            }
            scored.sort(function(a, b) { return b.score - a.score; });
            var seen = {}, out = [];
            for (var i = 0; i < scored.length; i++) {
                var u = scored[i].url;
                if (seen[u]) continue;
                seen[u] = 1;
                out.push(u);
            }
            var jpgOnly = out.filter(function(u) {
                return /\\.jpe?g(\\?|$)/i.test(u);
            });
            if (jpgOnly.length) out = jpgOnly;
            return JSON.stringify(out.slice(0, 3));
        """)
        try:
            return json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _is_synthetic_group_label(label):
        """无项目名时的分组键（寻源:/地址:/未命名），不能用于 OMS 行文本匹配。"""
        s = (label or "").strip()
        return s.startswith(("寻源:", "地址:", "未命名", "询价:"))

    def _mark_row_for_attachment(self, project_name, sourcing_no="", item=None):
        """
        在数据表中标记匹配行，返回 marker 或空。
        无项目名时按寻源单号+梯号+系统询价号定位，勿用「寻源:XXX」分组键去匹配行。
        """
        item = item or {}
        ladder_no = (
            (item.get("ladder_no") or item.get("梯号") or "").strip()
        )
        sys_inq = (
            (item.get("oms_system_inquiry_no") or item.get("系统询价号") or "")
            .strip()
        )
        mfg = (item.get("mfg_material_no") or item.get("工厂料号") or "").strip()
        proj = (project_name or "").strip()
        if self._is_synthetic_group_label(proj):
            proj = ""

        raw = self.browser.driver.execute_script("""
            var projectName = (arguments[0] || '').trim();
            var sourcingNo = (arguments[1] || '').trim();
            var ladderNo = (arguments[2] || '').trim();
            var sysInq = (arguments[3] || '').trim();
            var mfgNo = (arguments[4] || '').trim();
            var tables = document.querySelectorAll('table');
            var bestTable = null, bestRows = 0;
            for (var i = 0; i < tables.length; i++) {
                if (tables[i].offsetParent === null) continue;
                var trs = tables[i].querySelectorAll('tbody tr, tr');
                var count = 0;
                for (var j = 0; j < trs.length; j++) {
                    if (trs[j].querySelectorAll('td').length >= 3) count++;
                }
                if (count > bestRows) { bestRows = count; bestTable = tables[i]; }
            }
            if (!bestTable) return '';
            var trs = bestTable.querySelectorAll('tbody tr, tr');
            for (var r = 0; r < trs.length; r++) {
                var tr = trs[r];
                if (tr.getAttribute('data-oms-att-marker')) continue;
                var tds = tr.querySelectorAll('td');
                if (tds.length < 3) continue;
                var rowText = tr.textContent || '';
                if (sourcingNo && rowText.indexOf(sourcingNo) < 0) continue;
                if (sysInq && rowText.indexOf(sysInq) < 0) continue;
                if (ladderNo && rowText.indexOf(ladderNo) < 0) continue;
                if (mfgNo && rowText.indexOf(mfgNo) < 0) continue;
                if (projectName && rowText.indexOf(projectName) < 0) continue;
                if (!sourcingNo && !sysInq && !ladderNo && !mfgNo && !projectName) continue;
                var marker = '__oms_att_' + Date.now() + '_' + r;
                tr.setAttribute('data-oms-att-marker', marker);
                return marker;
            }
            return '';
        """, proj, sourcing_no or "", ladder_no, sys_inq, mfg)
        return (raw or "").strip()

    def _click_row_attachment_trigger(self, marker):
        """点击行内附件列的可点击元素，打开预览/下载。"""
        clicked = self.browser.driver.execute_script("""
            var marker = arguments[0];
            var headerKeywords = arguments[1];
            var tr = document.querySelector('tr[data-oms-att-marker="' + marker + '"]');
            if (!tr) return JSON.stringify({ok: false, reason: 'no_row'});
            var table = tr.closest('table');
            var headers = [];
            if (table) {
                var ths = table.querySelectorAll('thead th, thead td');
                for (var h = 0; h < ths.length; h++) {
                    headers.push((ths[h].textContent || '').replace(/\\s+/g, ''));
                }
            }
            function headerMatches(h) {
                for (var i = 0; i < headerKeywords.length; i++) {
                    if (h.indexOf(headerKeywords[i]) >= 0) return true;
                }
                return false;
            }
            var tds = tr.querySelectorAll('td');
            var targets = [];
            for (var c = 0; c < tds.length; c++) {
                var h = headers[c] || '';
                var cell = tds[c];
                var cellText = (cell.textContent || '').trim();
                var inAttachCol = headerMatches(h);
                var positive = /^(有|是|Y|Yes|1)$/i.test(cellText);
                if (!inAttachCol && !positive) continue;
                var els = cell.querySelectorAll('a, button, i, span, img');
                for (var e = 0; e < els.length; e++) {
                    if (els[e].offsetParent !== null) targets.push(els[e]);
                }
                if (!targets.length && (inAttachCol || positive) && cell.offsetParent !== null) {
                    targets.push(cell);
                }
            }
            if (!targets.length) {
                tr.querySelectorAll('a, img, i, [onclick]').forEach(function(el) {
                    if (el.offsetParent !== null) targets.push(el);
                });
            }
            for (var t = 0; t < targets.length; t++) {
                try {
                    targets[t].click();
                    return JSON.stringify({ok: true, index: t});
                } catch (e) {}
            }
            return JSON.stringify({ok: false, reason: 'no_clickable'});
        """, marker, list(OMS_ATTACHMENT_HEADER_KEYWORDS))
        try:
            result = json.loads(clicked or "{}")
        except json.JSONDecodeError:
            result = {}
        return result.get("ok", False)

    def _download_row_attachments(self, project_name, item, row_index, cache_dir):
        """下载单行 OMS 附件到 cache_dir，返回本地图片路径列表（最多 5 张）。"""
        if not self._infer_has_attachment(item, project_name):
            return []

        sourcing_no = (item.get("oms_sourcing_no") or "").strip()
        marker = self._mark_row_for_attachment(
            project_name, sourcing_no, item=item
        )
        if not marker:
            logger.warning(
                f"  未在 OMS 表格定位到附件行"
                f"（项目={project_name[:30]}, 寻源={sourcing_no or '—'}, "
                f"梯号={item.get('ladder_no') or item.get('梯号') or '—'}, "
                f"询价号={item.get('oms_system_inquiry_no') or item.get('系统询价号') or '—'}）"
            )
            return []

        prev_download_dir = getattr(self.browser, "download_dir", "") or ""
        self.browser.download_dir = cache_dir
        self._ensure_oms_download_dir()

        saved_paths = []
        try:
            urls = []
            temp_downloads = []
            dialog_no_downloads = False
            if self._click_row_attachment_trigger(marker):
                time.sleep(1.2)
                temp_downloads = self._download_from_attachment_dialog_operation(
                    cache_dir, sourcing_no
                )
                if not temp_downloads:
                    urls = self._collect_attachment_urls_from_dialog()
                    if not urls:
                        dialog_no_downloads = True
                self._close_attachment_dialog()
                time.sleep(0.4)

            if not temp_downloads and not urls:
                urls_raw = self.browser.driver.execute_script("""
                    var marker = arguments[0];
                    var tr = document.querySelector('tr[data-oms-att-marker="' + marker + '"]');
                    if (!tr) return JSON.stringify([]);
                    var scored = [];
                    tr.querySelectorAll('a[href], img').forEach(function(el) {
                        var u = el.href || el.currentSrc || el.src || '';
                        if (!/\\.(jpg|jpeg|png|gif|bmp|webp)(\\?|$)/i.test(u)) return;
                        var w = el.naturalWidth || el.offsetWidth || 100;
                        var h = el.naturalHeight || el.offsetHeight || 100;
                        var sc = w * h + (/\\.jpe?g/i.test(u) ? 50000 : 0);
                        scored.push({url: u, score: sc});
                    });
                    scored.sort(function(a,b){return b.score-a.score;});
                    return JSON.stringify(scored.slice(0, 5).map(function(x){return x.url;}));
                """, marker)
                try:
                    urls = json.loads(urls_raw or "[]")
                except json.JSONDecodeError:
                    urls = []

            for ui, url in enumerate(urls[:OMS_MAX_ATTACHMENTS_PER_ROW]):
                url_path = urllib.parse.urlparse(url).path.lower()
                if url_path.endswith((".jpg", ".jpeg")):
                    ext = ".jpg"
                elif url_path.endswith((".png", ".gif", ".bmp", ".webp")):
                    ext = os.path.splitext(url_path)[1]
                else:
                    ext = ".jpg"
                tmp = os.path.join(
                    cache_dir,
                    f".tmp_{sourcing_no or 'row'}_{row_index}_{ui + 1}{ext}",
                )
                try:
                    self._download_url_to_file(url, tmp)
                    temp_downloads.append(tmp)
                    logger.info(
                        f"  ✓ 已拉取附件候选 {ui + 1}/{len(urls[:3])}: "
                        f"{os.path.basename(tmp)} ({os.path.getsize(tmp)} B)"
                    )
                except Exception as e:
                    logger.warning(f"  附件 URL 下载失败: {url[:80]}… ({e})")

            if not temp_downloads and not dialog_no_downloads and self._click_row_attachment_trigger(marker):
                time.sleep(1.5)
                op_paths = self._download_from_attachment_dialog_operation(
                    cache_dir, sourcing_no
                )
                temp_downloads.extend(op_paths)
                if not temp_downloads:
                    since = time.time()
                    found = wait_for_new_image_download(
                        cache_dir,
                        since_time=since,
                        timeout=OMS_ATTACHMENT_DOWNLOAD_TIMEOUT,
                    )
                    if found:
                        temp_downloads.append(found)
                        logger.info(
                            f"  ✓ 已下载 OMS 附件(浏览器): "
                            f"{os.path.basename(found)}"
                        )
                self._close_attachment_dialog()

            picked = pick_distinct_photo_files(
                temp_downloads,
                max_count=OMS_MAX_ATTACHMENTS_PER_ROW,
                min_bytes=OMS_ATTACHMENT_MIN_FILE_BYTES,
            )
            if temp_downloads and not picked:
                logger.warning(
                    f"  附件候选 {len(temp_downloads)} 个均被过滤"
                    f"（过小或重复），请检查 OMS 弹窗中的原图"
                )

            for i, src in enumerate(picked):
                final_jpg = os.path.join(
                    cache_dir,
                    f"{sourcing_no or 'row'}_{row_index}_{i + 1}.jpg",
                )
                try:
                    saved_paths.append(
                        finalize_downloaded_attachment(src, final_jpg)
                    )
                    logger.info(
                        f"  ✓ 已保存附件: {os.path.basename(final_jpg)}"
                    )
                except Exception as e:
                    logger.warning(f"  保存附件失败: {e}")

            saved_abs = {os.path.abspath(p) for p in saved_paths}
            for tmp in temp_downloads:
                tp = os.path.abspath(tmp)
                if tp not in saved_abs and os.path.isfile(tp):
                    try:
                        os.remove(tp)
                    except OSError:
                        pass

        finally:
            self.browser.driver.execute_script("""
                document.querySelectorAll('[data-oms-att-marker]').forEach(function(el) {
                    el.removeAttribute('data-oms-att-marker');
                });
            """)
            if prev_download_dir:
                self.browser.download_dir = prev_download_dir
                self._ensure_oms_download_dir()

        return saved_paths[:OMS_MAX_ATTACHMENTS_PER_ROW]

    def download_attachments_for_group(self, project_name, items):
        """
        阶段一：为项目组内每条 OMS 行下载实物照片/附件，写入 has_attachment / attachments。
        须在 OMS 列表页、筛选结果仍可见时调用。
        """
        project_name = (project_name or "").strip()
        if not project_name:
            return items

        cache_dir = self._attachments_cache_dir(project_name)
        logger.info(f"  OMS 附件: 检查项目「{project_name[:40]}」({len(items)} 行)…")
        logger.info(f"  ★ 附件保存目录（可在资源管理器中打开查看）:")
        logger.info(f"     {cache_dir}")

        any_needed = any(
            self._infer_has_attachment(it, project_name) for it in items
        )
        if not any_needed:
            logger.info("  OMS 附件: 未识别到「有附件」行，跳过下载")
            return items

        items = self.sanitize_group_attachments(project_name, items)

        for idx, item in enumerate(items):
            item["attachment_project"] = project_name
            if not self._infer_has_attachment(item, project_name):
                item["has_attachment"] = False
                item["attachments"] = []
                continue

            existing = self.filter_attachment_paths(
                item.get("attachments") or [], project_name
            )
            if existing:
                item["has_attachment"] = True
                item["attachments"] = existing
                logger.info(
                    f"  行 {idx + 1}: 使用本项目缓存附件 {len(existing)} 张"
                )
                continue

            paths = self._download_row_attachments(
                project_name, item, idx + 1, cache_dir
            )
            paths = self.filter_attachment_paths(paths, project_name)
            item["has_attachment"] = bool(paths)
            item["attachments"] = paths
            if paths:
                item["attachment_project"] = project_name
            if not paths:
                logger.warning(
                    f"  行 {idx + 1}: 标记有附件但下载失败，备件网将回退模板照片"
                )

        total = sum(len(it.get("attachments") or []) for it in items)
        if total:
            logger.info(f"  OMS 附件: 本项目共 {total} 张已就绪")
        return items


class OMSSupplierOpsMixin:
    def _scan_email_send_ui_state_js(self):
        """返回邮件发送后页面状态 JSON（供 _complete_supplier_email_send 轮询）。"""
        return self.browser.driver.execute_script("""
            function vis(el) {
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
            function rawText(el) {
                return (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
            }
            function clickConfirmInRoot(root) {
                var btns = root.querySelectorAll(
                    '.layui-layer-btn a, .layui-layer-btn0, .layui-layer-btn1, ' +
                    'button, a.btn-primary, input[type="button"], input[type="submit"]'
                );
                for (var bi = 0; bi < btns.length; bi++) {
                    var tx = rawText(btns[bi]);
                    if (tx === '确定' || tx === '确认' || tx === 'OK' || tx === '是') {
                        btns[bi].click();
                        return tx;
                    }
                }
                return '';
            }
            // 1) layui 遮罩层确认（优先直接点击）
            var layers = document.querySelectorAll(
                '.layui-layer-dialog, .layui-layer-page, .layui-layer[type="dialog"]'
            );
            for (var li = 0; li < layers.length; li++) {
                if (!vis(layers[li])) continue;
                var clicked = clickConfirmInRoot(layers[li]);
                if (clicked) {
                    return JSON.stringify({phase: 'confirm_clicked', text: clicked});
                }
                return JSON.stringify({phase: 'confirm', text: '确定'});
            }
            var modals = document.querySelectorAll(
                '.bootbox, .modal.in, .modal.show, [role="dialog"], .sweet-alert'
            );
            for (var mi = 0; mi < modals.length; mi++) {
                if (!vis(modals[mi])) continue;
                var c2 = clickConfirmInRoot(modals[mi]);
                if (c2) return JSON.stringify({phase: 'confirm_clicked', text: c2});
            }
            // 2) 成功提示
            var hints = document.querySelectorAll('.layui-layer-msg, .toast, .alert-success');
            for (var hi = 0; hi < hints.length; hi++) {
                if (!vis(hints[hi])) continue;
                var ht = rawText(hints[hi]);
                if (/成功|已发送|发送完成|邮件已|操作成功/.test(ht)) {
                    return JSON.stringify({phase: 'success', text: ht.slice(0, 120)});
                }
            }
            var bodyTx = (document.body.innerText || '');
            if (/邮件[\\s\\S]{0,30}成功|发送[\\s\\S]{0,30}成功|操作成功/.test(bodyTx)) {
                return JSON.stringify({phase: 'success', text: 'body_hint'});
            }
            // 3) 发送进度：工具栏区域 / 小面板（勿用整页 #myTabContent 的 checkVisibility）
            var progSelectors = [
                '.layui-layer:not(.layui-layer-msg)',
                '.tab-pane.active .mt-10',
                '.tab-pane.active .f-l.toolbar',
                '#myTabContent > .tab-pane.active',
                '.dataTables_wrapper .f-l.toolbar',
                '.progress', '.progress-bar'
            ];
            for (var si = 0; si < progSelectors.length; si++) {
                var nodes = document.querySelectorAll(progSelectors[si]);
                for (var ni = 0; ni < nodes.length; ni++) {
                    var pr = nodes[ni];
                    if (!vis(pr)) continue;
                    var pt = rawText(pr);
                    if (pt.length > 600) pt = pt.slice(0, 600);
                    var m = pt.match(/(\\d{1,3})\\s*%/);
                    if (!m) continue;
                    var isBar = pr.classList && (
                        pr.classList.contains('progress') ||
                        pr.classList.contains('progress-bar')
                    );
                    if (/取消/.test(pt) || /发送/.test(pt) || /邮件/.test(pt) ||
                        /处理中|请稍/.test(pt) || isBar ||
                        pr.querySelector('.progress, .progress-bar, [class*="progress"]')) {
                        return JSON.stringify({
                            phase: 'progress',
                            pct: parseInt(m[1], 10),
                            snippet: pt.slice(0, 100),
                            where: progSelectors[si]
                        });
                    }
                }
            }
            // 4) 全文兜底：工具栏附近出现 0%…取消（OMS 常嵌在 #myTabContent 顶部）
            var tab = document.querySelector('#myTabContent, .tab-pane.active');
            if (tab) {
                var topTx = rawText(tab).slice(0, 400);
                var m2 = topTx.match(/(\\d{1,3})\\s*%/);
                if (m2 && /取消/.test(topTx)) {
                    return JSON.stringify({
                        phase: 'progress',
                        pct: parseInt(m2[1], 10),
                        snippet: topTx.slice(0, 100),
                        where: 'tab_top'
                    });
                }
            }
            return JSON.stringify({phase: 'idle'});
        """)

    def _complete_supplier_email_send(self, max_wait_sec=90):
        """
        点击「发送供应商报价邮件」菜单之后：
        1) 若有 layui/模态「确定」则点击；
        2) 若出现发送进度（0%…100%），等待完成；
        3) 识别成功提示。

        Returns:
            (success: bool, detail: str)
        """
        driver = self.browser.driver
        deadline = time.time() + max_wait_sec
        confirm_clicked = False
        saw_progress = False
        last_pct = None
        idle_since = None
        menu_clicked_at = time.time()

        while time.time() < deadline:
            time.sleep(0.35)
            raw = self._scan_email_send_ui_state_js()
            try:
                state = json.loads(raw or '{"phase":"idle"}')
            except (json.JSONDecodeError, TypeError):
                state = {"phase": "idle"}

            phase = state.get("phase", "idle")

            if phase == "confirm_clicked":
                confirm_clicked = True
                logger.info(f"  ✓ 已点击邮件确认(JS): {state.get('text', '确定')}")
                idle_since = None
                time.sleep(0.8)
                continue

            if phase == "confirm":
                label = state.get("text", "确定")
                clicked = self._click_oms_confirm_dialog(max_wait_sec=2, poll_interval=0.25)
                if not clicked:
                    try:
                        for xp in (
                            "//div[contains(@class,'layui-layer')]//a[normalize-space()='确定']",
                            "//button[normalize-space()='确定']",
                            "//a[normalize-space()='确定']",
                        ):
                            for el in driver.find_elements(By.XPATH, xp):
                                if not el.is_displayed():
                                    continue
                                ActionChains(driver).move_to_element(el).click().perform()
                                clicked = label
                                break
                            if clicked:
                                break
                    except Exception:
                        pass
                if clicked:
                    confirm_clicked = True
                    logger.info(f"  ✓ 已点击邮件确认: {clicked}")
                    idle_since = None
                    time.sleep(0.8)
                    continue
                logger.warning(f"  检测到确认框「{label}」但点击失败，继续轮询…")

            if phase == "success":
                return True, state.get("text", "success")

            if phase == "progress":
                saw_progress = True
                idle_since = None
                pct = state.get("pct")
                if pct != last_pct:
                    last_pct = pct
                    where = state.get("where", "")
                    logger.info(f"  邮件发送进度: {pct}% … ({where})")
                if pct is not None and pct >= 100:
                    time.sleep(1)
                    return True, f"progress_{pct}"
                continue

            if phase == "idle":
                if idle_since is None:
                    idle_since = time.time()
                elif saw_progress and (time.time() - idle_since) > 2:
                    return True, "progress_done"
                elif confirm_clicked and (time.time() - idle_since) > 2.5:
                    return True, "after_confirm"
                elif not saw_progress and not confirm_clicked:
                    elapsed = time.time() - menu_clicked_at
                    # 进度条常延迟 3～15s 才出现，勿 8s 就放弃
                    if elapsed > 35 and (time.time() - idle_since) > 12:
                        break

        try:
            ss_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "logs",
                "confirm_dialog_debug.png",
            )
            driver.save_screenshot(ss_path)
            logger.info(f"  [诊断] 截图已保存: {ss_path}")
            snap = self._scan_email_send_ui_state_js()
            logger.info(f"  [诊断] 结束时页面状态: {snap}")
        except Exception as ss_e:
            logger.warning(f"  [诊断] 截图失败: {ss_e}")

        if saw_progress:
            return False, f"progress_timeout_last_{last_pct}%"
        if confirm_clicked:
            return False, "confirm_clicked_no_success_hint"
        return False, "no_confirm_no_progress"

    @staticmethod
    def _is_send_email_with_attachment_menu(text):
        """
        是否为「发送供应商报价邮件(附件+报价EXCEL)」菜单项。
        排除仅「发送供应商报价邮件(报价EXCEL)」（无附件）。
        """
        t = (text or "").strip()
        if not t or "发送" not in t or "报价" not in t:
            return False
        if "发送供应商报价邮件" not in t and "发送供应商报价" not in t:
            return False
        return "附件" in t

    def _click_psm_send_email_attachment_menu(self):
        """
        点击 PSM 下拉中的「发送供应商报价邮件(附件+报价EXCEL)」。
        Returns:
            (clicked: bool, menu_text: str)
        """
        driver = self.browser.driver
        is_target = self._is_send_email_with_attachment_menu

        try:
            for menu in driver.find_elements(By.CSS_SELECTOR, ".dropdown-menu"):
                try:
                    if not menu.is_displayed():
                        continue
                except Exception:
                    continue
                for el in menu.find_elements(By.TAG_NAME, "a"):
                    try:
                        if not el.is_displayed():
                            continue
                        txt = (el.text or "").strip()
                        if not is_target(txt):
                            continue
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                            el,
                        )
                        time.sleep(0.2)
                        ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
                        return True, txt[:60]
                    except Exception:
                        continue
        except Exception:
            pass

        try:
            xpath = (
                "//div[contains(@class,'dropdown-menu')]"
                "//a[contains(normalize-space(.),'发送供应商报价邮件') and contains(.,'附件')]"
            )
            for el in driver.find_elements(By.XPATH, xpath):
                try:
                    if not el.is_displayed():
                        continue
                    txt = (el.text or "").strip()
                    if not is_target(txt):
                        continue
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                        el,
                    )
                    time.sleep(0.2)
                    ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
                    return True, txt[:60]
                except Exception:
                    continue
        except Exception:
            pass

        try:
            raw = driver.execute_script("""
                function vis(el) {
                    if (!el) return false;
                    try {
                        var r = el.getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) return false;
                    } catch (e) {}
                    return el.offsetParent !== null;
                }
                function isTarget(txt) {
                    if (!txt || txt.indexOf('发送') < 0 || txt.indexOf('报价') < 0) return false;
                    if (txt.indexOf('发送供应商报价邮件') < 0 &&
                        txt.indexOf('发送供应商报价') < 0) return false;
                    return txt.indexOf('附件') >= 0;
                }
                var dropdowns = document.querySelectorAll('.dropdown-menu');
                for (var d = 0; d < dropdowns.length; d++) {
                    if (!vis(dropdowns[d])) continue;
                    var links = dropdowns[d].querySelectorAll('a');
                    for (var i = 0; i < links.length; i++) {
                        var txt = (links[i].textContent || '').trim();
                        if (!isTarget(txt)) continue;
                        links[i].click();
                        return JSON.stringify({success: true, text: txt.substring(0, 60)});
                    }
                }
                var all = document.querySelectorAll('a, li');
                var best = null, bestTxt = '';
                for (var j = 0; j < all.length; j++) {
                    var t = (all[j].textContent || '').trim();
                    if (!isTarget(t) || !vis(all[j])) continue;
                    if (t.length > bestTxt.length) { best = all[j]; bestTxt = t; }
                }
                if (best) {
                    best.click();
                    return JSON.stringify({success: true, text: bestTxt.substring(0, 60)});
                }
                return JSON.stringify({success: false});
            """)
            result = json.loads(raw or '{"success":false}')
            if result.get("success"):
                return True, result.get("text", "")
        except Exception:
            pass

        return False, ""

    def apply_pending_sourcing_no_filter(self, sourcing_no):
        """在已筛「待处理」的列表上，按寻源单号列漏斗搜索。"""
        return self.apply_pending_column_filter("寻源单号", sourcing_no)

    def apply_pending_column_filter(self, column_name: str, value: str) -> bool:
        """在已筛「待处理 + Sourcing8ID」的列表上，按指定列漏斗搜索。"""
        return self._apply_column_tooltip_filter(
            column_name, value, list_label="OMS 待处理列表"
        )

    def apply_pending_project_name_filter(self, project_name):
        """
        在已筛「待处理」的列表上，按项目名称列漏斗搜索（扩展供应商后用）。
        不套用阶段三「已发送」前缀筛选。
        """
        return self.apply_pending_column_filter("项目名称", project_name)

    def extract_pending_group_for_project(
        self, project_name, skip_refilter=False, sourcing_no=""
    ):
        """
        扩展供应商发邮件后提取物料行。
        skip_refilter=True：不再重复「待处理→Sourcing8ID→项目名」筛选（发邮件后行可能已非待处理）。
        """
        name = (project_name or "").strip()
        sno = (sourcing_no or "").strip()
        if not sno:
            sno = parse_anonymous_group_key(name) or ""
        if not name and not sno:
            return []
        if skip_refilter:
            logger.info(
                "  发邮件后从当前 OMS 列表直接提取（跳过重复筛选）"
            )
            time.sleep(0.5)
        else:
            self.apply_filters(
                status_pending=True,
                status_sent=False,
                sourcing_filter=False,
            )
            anon_sno = parse_anonymous_group_key(name)
            if anon_sno:
                self.apply_pending_sourcing_no_filter(anon_sno)
            elif name.startswith("地址:"):
                logger.info("  无项目名分组(地址键)，从当前待处理列表直接提取")
            else:
                self.apply_pending_project_name_filter(name)
            time.sleep(1.0)
        all_data = self.extract_all_data()
        groups = self.group_by_project_name(all_data)
        for group_key, items in groups.items():
            pn = (
                (items[0].get("项目名称") if items else "")
                or (items[0].get("project_name") if items else "")
                or group_key
            )
            pn = (pn or "").strip()
            if name and (name in pn or pn in name or group_key == name):
                matched = True
            elif sno and any(
                sno in str(it.get("寻源单号") or it.get("sourcing_no") or "")
                or sno in str(it.get("oms_sourcing_no") or "")
                for it in items
            ):
                matched = True
            elif name.startswith("寻源:") and group_key.startswith("寻源:"):
                matched = name == group_key or parse_anonymous_group_key(name) == parse_anonymous_group_key(group_key)
            elif skip_refilter and items:
                matched = True
            else:
                matched = name in group_key or group_key == name
            if matched:
                detailed = [self.get_detailed_info(item) for item in items]
                project_label = pn or group_key
                detailed = self.download_attachments_for_group(
                    project_label, detailed
                )
                logger.info(
                    f"  已提取新行 {len(detailed)} 条（项目 {project_label[:40]}…）"
                )
                return detailed
        logger.warning(f"  当前列表未提取到项目: {name[:50] or sno[:50]}")
        return []

    def _find_oms_delete_reason_layer(self):
        """图一：layui「删除」弹窗（含删除原因 + 保存）。"""
        driver = self.browser.driver
        driver.switch_to.default_content()
        try:
            for layer in reversed(
                driver.find_elements(By.CSS_SELECTOR, ".layui-layer")
            ):
                try:
                    if not layer.is_displayed():
                        continue
                except Exception:
                    continue
                text = (layer.text or "").replace("\xa0", " ")
                if "删除原因" in text:
                    return layer
                try:
                    title = layer.find_element(
                        By.CSS_SELECTOR, ".layui-layer-title"
                    ).text or ""
                    if "删除" in title and "扩展供应商" not in title:
                        return layer
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _switch_to_delete_reason_iframe(self):
        driver = self.browser.driver
        driver.switch_to.default_content()
        layer = self._find_oms_delete_reason_layer()
        if not layer:
            return False
        try:
            iframe = layer.find_element(By.CSS_SELECTOR, "iframe")
            driver.switch_to.frame(iframe)
            return True
        except Exception:
            driver.switch_to.default_content()
            return False

    def _fill_delete_reason_in_current_doc(self, reason):
        driver = self.browser.driver
        return driver.execute_script(
            """
            var reason = arguments[0];
            function norm(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            var ta = document.querySelector(
                'textarea, input[type="text"]:not([type="hidden"])'
            );
            var labels = document.querySelectorAll('label, span, div, td');
            for (var i = 0; i < labels.length; i++) {
                var tx = norm(labels[i].innerText || labels[i].textContent || '');
                if (tx.indexOf('删除原因') < 0) continue;
                var box = labels[i].closest('.form-group, .row, .row.cl, tr')
                    || labels[i].parentElement;
                if (box) {
                    var found = box.querySelector(
                        'textarea, input[type="text"]:not([type="hidden"])'
                    );
                    if (found) { ta = found; break; }
                }
            }
            if (!ta) return false;
            try { ta.scrollIntoView({block:'center'}); } catch (e) {}
            ta.focus();
            ta.value = reason;
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
            return (ta.value || '').indexOf(reason) >= 0;
            """,
            reason,
        )

    def _click_delete_reason_save_in_current_doc(self):
        driver = self.browser.driver
        return driver.execute_script(
            """
            var btn = document.querySelector(
                '#submit, input#submit, button#submit'
            );
            if (btn) { btn.click(); return true; }
            var nodes = document.querySelectorAll(
                'button, input[type="button"], input[type="submit"], a.btn'
            );
            for (var i = 0; i < nodes.length; i++) {
                var tx = (nodes[i].innerText || nodes[i].value || '')
                    .replace(/\\s+/g, ' ').trim();
                if (tx === '保存' || tx.indexOf('保存') >= 0) {
                    nodes[i].click();
                    return true;
                }
            }
            return false;
            """
        )

    def _click_oms_delete_save_confirm(self, max_wait_sec=15):
        """图二：删除原因弹窗点「保存」后的「信息 — 确认保存?」→「确定」。"""
        driver = self.browser.driver
        driver.switch_to.default_content()
        per_ctx = max(5, max_wait_sec // 3)
        contexts = [("主页面", None)]
        layer = self._find_oms_delete_reason_layer()
        if layer:
            try:
                iframe = layer.find_element(By.CSS_SELECTOR, "iframe")
                contexts.append(("删除 iframe", iframe))
            except Exception:
                pass
        for ctx_name, iframe_el in contexts:
            try:
                driver.switch_to.default_content()
                if iframe_el is not None:
                    driver.switch_to.frame(iframe_el)
                ok, label = self._click_confirm_save_dialog_in_context(
                    max_wait_sec=per_ctx
                )
                if ok:
                    driver.switch_to.default_content()
                    logger.info(
                        f"  ✓ 删除原因已保存并确认 ({ctx_name}, {label})"
                    )
                    time.sleep(0.5)
                    return True
            except Exception:
                pass
            finally:
                driver.switch_to.default_content()
        return self._click_oms_confirm_dialog(max_wait_sec=5) is not None

    def _complete_oms_delete_reason_dialog(self, reason=None):
        """
        工具栏点「删除」后：
        图一 直接点「保存」（删除原因可不填）→ 图二 点「确认保存?」的「确定」。
        """
        driver = self.browser.driver
        driver.switch_to.default_content()
        deadline = time.time() + 12
        while time.time() < deadline:
            if self._find_oms_delete_reason_layer():
                break
            time.sleep(0.35)
        else:
            logger.warning("  点击删除后未出现「删除」弹窗")
            return False

        saved = False
        if self._switch_to_delete_reason_iframe():
            try:
                saved = self._click_delete_reason_save_in_current_doc()
            finally:
                driver.switch_to.default_content()

        if not saved:
            layer = self._find_oms_delete_reason_layer()
            if layer:
                try:
                    for btn in layer.find_elements(
                        By.XPATH,
                        ".//button[contains(.,'保存')] | .//input[contains(@value,'保存')]",
                    ):
                        if btn.is_displayed():
                            btn.click()
                            saved = True
                            break
                except Exception as e:
                    logger.debug(f"  删除弹窗(Selenium): {e}")

        if not saved:
            logger.warning("  未能点击删除弹窗「保存」")
            return False
        logger.info("  ✓ 已点击删除弹窗「保存」（未填删除原因）")
        time.sleep(0.5)

        if self._click_oms_delete_save_confirm(max_wait_sec=15):
            self._wait_delete_reason_layer_closed(timeout=10)
            return True
        logger.warning("  删除原因保存后未点击「确认保存?」→「确定」")
        return False

    def _wait_delete_reason_layer_closed(self, timeout=12):
        driver = self.browser.driver
        driver.switch_to.default_content()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._find_oms_delete_reason_layer() is None:
                return True
            time.sleep(0.35)
        return False

    def _click_oms_delete_toolbar(self):
        """点击工具栏红色「删除」按钮。"""
        driver = self.browser.driver
        clicked = driver.execute_script("""
            function raw(el) {
                return (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
            }
            function isVis(el) {
                if (!el) return false;
                var r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
            }
            var candidates = [];
            var nodes = document.querySelectorAll('button, a, input[type="button"]');
            for (var i = 0; i < nodes.length; i++) {
                var el = nodes[i];
                if (!isVis(el)) continue;
                var tx = raw(el);
                if (tx !== '删除') continue;
                var cls = (el.className || '') + ' ' + (el.getAttribute('class') || '');
                var style = window.getComputedStyle(el);
                var score = 0;
                if (/danger|btn-red|delete/i.test(cls)) score += 2;
                if (style && (style.backgroundColor.indexOf('rgb(217') >= 0
                    || style.backgroundColor.indexOf('rgb(220') >= 0
                    || style.backgroundColor.indexOf('rgb(255, 0') >= 0)) score += 1;
                candidates.push({el: el, score: score});
            }
            candidates.sort(function(a, b) { return b.score - a.score; });
            if (candidates.length) {
                try { candidates[0].el.scrollIntoView({block: 'center'}); } catch (e) {}
                candidates[0].el.click();
                return true;
            }
            return false;
        """)
        if clicked:
            logger.info("  ✓ 已点击工具栏「删除」")
            return True
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//button[normalize-space()='删除'] | //a[normalize-space()='删除']",
            ):
                if el.is_displayed():
                    ActionChains(driver).move_to_element(el).click().perform()
                    logger.info("  ✓ 已点击「删除」(XPath)")
                    return True
        except Exception:
            pass
        logger.warning("  未找到工具栏「删除」按钮")
        return False

    def _selenium_toggle_marked_checkboxes(self, markers, want_checked):
        """对 data-oms-marker 复选框执行勾选/取消勾选。"""
        driver = self.browser.driver
        toggled = 0
        for marker in markers:
            try:
                cb_el = driver.find_element(
                    By.CSS_SELECTOR, f"input[data-oms-marker='{marker}']"
                )
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", cb_el
                )
                time.sleep(0.2)
                is_checked = driver.execute_script(
                    "return arguments[0].checked;", cb_el
                )
                if is_checked == want_checked:
                    toggled += 1
                    continue
                if want_checked:
                    self._selenium_click_row_checkbox_once(cb_el)
                elif is_checked:
                    ActionChains(driver).move_to_element(cb_el).click().perform()
                    time.sleep(0.2)
                if (
                    driver.execute_script("return arguments[0].checked;", cb_el)
                    == want_checked
                ):
                    toggled += 1
            except Exception as e:
                logger.warning(f"  复选框 {marker} 切换失败: {e}")
        return toggled

    def _wait_extend_supplier_layer_closed(self, timeout=15):
        """扩展供应商弹窗关闭后再删列表行。"""
        self.browser.driver.switch_to.default_content()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._find_extend_supplier_layer() is None:
                return True
            time.sleep(0.35)
        logger.warning("  扩展供应商弹窗仍未关闭，继续尝试删除旧行…")
        return False

    def _apply_sent_list_filter_for_delete(
        self, project_name, sourcing_no="", group_key="", items=None
    ):
        """
        删除错供应商旧行前：已发送 → Sourcing8ID → 第三列（同阶段三/阶段一分组规则）。
        """
        name = (project_name or "").strip()
        sno = (sourcing_no or "").strip()
        logger.info(
            "  删除「已发送」旧行前重筛: 状态=已发送 → Sourcing8ID → "
            "项目名/寻源/地址/梯号/系统询价号 …"
        )
        if FINALIZE_FILTER_BY_SOURCING_NO and sno:
            ok = self.apply_sourcing_no_filter(sno)
        else:
            filt = self._resolve_finalize_list_filter(
                name, sno, group_key, items
            )
            if filt:
                col, val = filt
                ok = self.apply_finalize_column_filter(col, val)
            else:
                if not self._apply_finalize_filter_prefix():
                    logger.warning("  已发送/Sourcing8ID 筛选可能未完全生效")
                ok = False
        time.sleep(1.0)
        return ok

    def delete_sent_rows_for_project(self, project_name, sourcing_no=""):
        """
        扩展供应商后：重筛已发送列表 → 勾选旧「已发送」行 → 点红色删除 → 确定。
        不勾选「待处理」新行。

        Returns:
            bool: 是否成功（无已发送行时视为成功）
        """
        name = (project_name or "").strip()
        sno = (sourcing_no or "").strip()
        if not name and not sno:
            return False
        label = name[:40] if name else sno[:40]
        logger.info(f"  OMS：删除项目「{label}…」的「已发送」旧行（错供应商）…")
        self.browser.driver.switch_to.default_content()
        self._wait_extend_supplier_layer_closed(timeout=12)
        self._apply_sent_list_filter_for_delete(name, sno)
        time.sleep(0.5)
        self._uncheck_all_oms_row_checkboxes()
        raw = self.browser.driver.execute_script("""
            var projectName = arguments[0];
            var sourcingNo = arguments[1];
            var strictFiltered = arguments[2];
            function rowMatches(rowText) {
                if (rowText.indexOf('已发送') < 0) return false;
                if (rowText.indexOf('待处理') >= 0) return false;
                if (projectName && projectName.length > 4
                    && rowText.indexOf(projectName) >= 0) return true;
                if (sourcingNo && sourcingNo.length >= 6
                    && rowText.indexOf(sourcingNo) >= 0) return true;
                return !!strictFiltered;
            }
            document.querySelectorAll('[data-oms-marker]').forEach(function(el) {
                el.removeAttribute('data-oms-marker');
            });
            var toCheck = [], toUncheck = [];
            var tables = document.querySelectorAll('table');
            var bestTable = null, bestRows = 0;
            for (var i = 0; i < tables.length; i++) {
                if (tables[i].offsetParent !== null) {
                    var trs = tables[i].querySelectorAll('tbody tr, tr');
                    var count = 0;
                    for (var j = 0; j < trs.length; j++) {
                        if (trs[j].querySelectorAll('td').length >= 3) count++;
                    }
                    if (count > bestRows) { bestRows = count; bestTable = tables[i]; }
                }
            }
            if (!bestTable) return JSON.stringify({toCheck: [], toUncheck: []});
            var trs = bestTable.querySelectorAll('tbody tr, tr');
            for (var r = 0; r < trs.length; r++) {
                var tds = trs[r].querySelectorAll('td');
                if (tds.length < 3) continue;
                var rowText = trs[r].textContent || '';
                if (!rowMatches(rowText)) continue;
                var cb = trs[r].querySelector('input[name="aid"]');
                if (!cb) {
                    var allCbs = trs[r].querySelectorAll('input[type="checkbox"]');
                    for (var c = 0; c < allCbs.length; c++) {
                        if ((allCbs[c].className || '').indexOf('allSel') === -1) {
                            cb = allCbs[c];
                            break;
                        }
                    }
                }
                if (!cb) continue;
                var marker = '__oms_del_' + (toCheck.length + toUncheck.length);
                cb.setAttribute('data-oms-marker', marker);
                if (rowText.indexOf('已发送') >= 0) {
                    toCheck.push(marker);
                } else if (rowText.indexOf('待处理') >= 0) {
                    toUncheck.push(marker);
                }
            }
            return JSON.stringify({toCheck: toCheck, toUncheck: toUncheck});
        """, name, sno, True)
        plan = json.loads(raw)
        to_check = plan.get("toCheck") or []
        to_uncheck = plan.get("toUncheck") or []
        if not to_check:
            logger.info("  未找到「已发送」行，跳过删除（可能已删或列表未刷新）")
            self.browser.driver.execute_script("""
                document.querySelectorAll('[data-oms-marker]').forEach(function(el) {
                    el.removeAttribute('data-oms-marker');
                });
            """)
            return True
        if to_uncheck:
            self._selenium_toggle_marked_checkboxes(to_uncheck, False)
        n_check = self._selenium_toggle_marked_checkboxes(to_check, True)
        self.browser.driver.execute_script("""
            document.querySelectorAll('[data-oms-marker]').forEach(function(el) {
                el.removeAttribute('data-oms-marker');
            });
        """)
        if n_check <= 0:
            logger.error("  「已发送」行勾选失败，无法删除")
            return False
        logger.info(f"  ✓ 已勾选 {n_check} 行「已发送」记录")
        time.sleep(0.5)
        if not self._click_oms_delete_toolbar():
            return False
        time.sleep(0.6)
        if not self._complete_oms_delete_reason_dialog():
            logger.warning(
                "  删除未完成：需在弹窗点「保存」→「确认保存?」→「确定」"
            )
            return False
        time.sleep(1.0)
        logger.info(f"  ✓ 已删除 {n_check} 行「已发送」旧数据")
        return True

    def run_phase1_oms_after_extend_supplier(
        self, project_name, sourcing_no=""
    ):
        """
        扩展供应商确认后：删除「已发送」旧行 → 对「待处理」新行发供应商报价邮件。
        仅 OMS 操作；不调用备件网 run_single_inquiry，不重建询价单。

        Returns:
            tuple: (勾选行数, 邮件是否发送成功)
        """
        name = (project_name or "").strip()
        if not name and not (sourcing_no or "").strip():
            return 0, False
        if not self.delete_sent_rows_for_project(name, sourcing_no=sourcing_no):
            return 0, False
        logger.info(
            f"  扩展供应商后 → OMS 补跑（非启动器阶段一）："
            f"待处理新行发邮件（项目 {name[:40]}…）"
        )
        self.apply_filters(
            status_pending=True,
            status_sent=False,
            sourcing_filter=False,
        )
        self.apply_pending_project_name_filter(name)
        time.sleep(1.0)
        return self.send_supplier_email(name, pending_only=True)

    def send_supplier_email(
        self, project_name, pending_only=False, items=None, group_key=""
    ):
        """
        在OMS已筛选的表格中：
        1. 待处理 + Sourcing8ID，再按与分组一致的列漏斗筛选并勾选可见行
        2. 点击"PSM操作"按钮 → 选"发送供应商报价邮件（附件+报价excel）"
        3. 确认弹窗或等待发送进度完成
        
        Args:
            project_name: 项目名称，用于匹配表格行
            pending_only: True 时仅勾选状态含「待处理」的行（扩展供应商后的新行）
            items: 本组物料行（无项目名时用于推断分组列）
            group_key: oms_data 分组键（如 寻源:EHWXF202605080）
        
        Returns:
            tuple: (勾选行数, 邮件是否发送成功)
        """
        pn = (project_name or "").strip()
        group_items = list(items or [])
        gk = (group_key or "").strip()

        filt = resolve_group_oms_email_filter(pn, gk, group_items)
        if not filt:
            logger.warning(
                "  无法确定本组 OMS 筛选列（项目名与四关联字段均无单一共同值），"
                "跳过发送邮件"
            )
            return 0, False
        column_name, filter_value = filt

        # 阶段一：切回 OMS 后列表可能已变，须恢复 待处理 + Sourcing8ID
        if not pending_only:
            logger.info("  发邮件前恢复 OMS 筛选：待处理 + Sourcing8ID=当前账号")
            self.apply_filters(status_pending=True, sourcing_filter=False)
            time.sleep(1)

        scope = f"'{pn or gk or '本组'}'"
        if pending_only:
            logger.info(f"  OMS发送邮件: {scope}（仅「待处理」行）")
        logger.info(
            f"  OMS发送邮件: 按「{column_name}」="
            f"'{filter_value[:48]}{'…' if len(filter_value) > 48 else ''}' 筛选后勾选"
        )

        if not self.apply_pending_column_filter(column_name, filter_value):
            return 0, False
        time.sleep(1.2)

        checked = self._check_rows_selenium(
            "",
            pending_only=pending_only,
            clear_first=True,
            all_visible_filtered=True,
        )
        if checked == 0:
            hint = "（待处理）" if pending_only else ""
            logger.warning(
                f"  筛后未找到可勾选行（列「{column_name}」="
                f"'{filter_value[:40]}'）{hint}"
            )
            return 0, False
        time.sleep(0.8)
        
        # ================================================================
        # 步骤2: 点击"PSM操作"按钮
        # ================================================================
        psm_clicked = self.browser.driver.execute_script("""
            // 搜索整个页面，找包含"PSM操作"的可见按钮/链接
            var candidates = [];
            var allBtns = document.querySelectorAll('button, a, div[role="button"], span[role="button"]');
            for (var i = 0; i < allBtns.length; i++) {
                var el = allBtns[i];
                var txt = (el.textContent || '').trim();
                if (txt.indexOf('PSM操作') > -1 && el.offsetParent !== null) {
                    candidates.push(el.tagName + '#' + (el.id || '') + ' class=' + (el.className || ''));
                    el.click();
                    return JSON.stringify({success: true, found: candidates});
                }
            }
            
            // 备选：查找包含"操作"的按钮（可能是PSM操作的简称）
            for (var i = 0; i < allBtns.length; i++) {
                var el = allBtns[i];
                var txt = (el.textContent || '').trim();
                if (txt === 'PSM操作' && el.offsetParent !== null) {
                    el.click();
                    return JSON.stringify({success: true, found: ['exact:' + el.tagName]});
                }
            }
            
            return JSON.stringify({success: false, candidates: candidates});
        """)
        
        psm_result = json.loads(psm_clicked)
        if not psm_result.get('success'):
            logger.warning(f"  未找到'PSM操作'按钮")
            # 打印页面上所有包含"操作"的按钮文本辅助调试
            debug_btns = self.browser.driver.execute_script("""
                var btns = [];
                document.querySelectorAll('button, a').forEach(function(el) {
                    var txt = (el.textContent || '').trim();
                    if (txt && el.offsetParent !== null && txt.length < 30) {
                        btns.push(txt);
                    }
                });
                return JSON.stringify(btns.slice(0, 30));
            """)
            logger.info(f"  页面上可见按钮(前30): {debug_btns}")
            return 0, False
        
        logger.info("  ✓ 已点击PSM操作按钮")
        time.sleep(1.5)
        
        # ================================================================
        # 步骤3: 点击「发送供应商报价邮件(附件+报价EXCEL)」（勿点仅报价EXCEL项）
        # ================================================================
        menu_clicked, menu_text = self._click_psm_send_email_attachment_menu()

        if not menu_clicked:
            logger.warning(
                "  未找到「发送供应商报价邮件(附件+报价EXCEL)」菜单项"
                "（请确认 PSM 下拉已展开且含「附件」项）"
            )
            return checked, False

        if not self._is_send_email_with_attachment_menu(menu_text):
            logger.error(
                f"  ✗ 误选了非附件菜单项: {menu_text!r}；"
                f"需要「发送供应商报价邮件(附件+报价EXCEL)」"
            )
            return checked, False

        logger.info(f"  ✓ 已选择(含附件): {menu_text}")
        time.sleep(1.2)
        
        # 步骤4: 确认框 / 发送进度 / 成功提示（OMS 新版常为进度条而非「确定」）
        email_ok, email_detail = self._complete_supplier_email_send(max_wait_sec=90)
        if email_ok:
            logger.info(
                f"  ✓ 供应商报价邮件已发送 (勾选{checked}行, {email_detail})"
            )
        else:
            logger.error(
                f"  ✗ 邮件未确认发送成功 (勾选{checked}行, 原因: {email_detail})"
            )
            logger.error(
                "  请在 OMS 中人工：勾选同项目行 → PSM操作 → 发送供应商报价邮件 → "
                "等待进度完成；或查看 logs/confirm_dialog_debug.png"
            )
        time.sleep(1.5)
        
        return checked, email_ok

    @staticmethod
    def is_allowed_extend_supplier(name):
        """扩展供应商下拉仅允许上海/中国两家，排除「家用电梯」等相似项。"""
        n = (name or "").strip()
        if not n or "家用电梯" in n:
            return False
        return n in (OMS_SUPPLIER_SHANGHAI, OMS_SUPPLIER_ZHONGSHAN)

    @staticmethod
    def supplier_name_for_factory(factory_type):
        """PO/工厂 → OMS 供应商全称。"""
        if (factory_type or "").strip() == "中山":
            return OMS_SUPPLIER_ZHONGSHAN
        return OMS_SUPPLIER_SHANGHAI

    @staticmethod
    def factory_from_supplier(supplier):
        """OMS「供应商」列 → 工厂（松江/中山）。"""
        s = (supplier or "").strip()
        if not s:
            return None
        if OMS_SUPPLIER_ZHONGSHAN and OMS_SUPPLIER_ZHONGSHAN in s:
            return "中山"
        if OMS_SUPPLIER_SHANGHAI and OMS_SUPPLIER_SHANGHAI in s:
            return "松江"
        if "家用电梯" in s:
            return None
        if "中山" in s:
            return "中山"
        if "松江" in s:
            return "松江"
        if "中国" in s and "有限公司" in s:
            return "中山"
        if "上海" in s and "有限公司" in s:
            return "松江"
        return None

    def _refilter_status_sent_only_for_extend_supplier(self):
        """
        信息弹窗点「确定」后：仅重新筛选状态=已发送。
        不再重做 Sourcing8ID / 项目名称（与人工操作一致）。
        """
        logger.info("  信息弹窗后仅重新筛选：状态=已发送 …")
        ok = self._apply_status_sent_filter()
        time.sleep(1.0)
        if ok:
            logger.info("  ✓ 状态=已发送 筛选已刷新")
        else:
            logger.warning("  状态=已发送 重筛可能未生效，仍尝试继续…")
        return ok

    def _prepare_all_filtered_rows_for_extend_supplier(
        self, project_name, sourcing_no=""
    ):
        """
        筛完 已发送 + Sourcing8ID + 项目名 后，勾选当前列表全部可见数据行。
        同项目多物料（供应商与 PO 不一致）须一并扩展供应商，不能只选一行。
        """
        checked = self._check_rows_selenium(
            "",
            all_visible_filtered=True,
            clear_first=True,
        )
        if checked <= 0 and (project_name or sourcing_no):
            logger.info("  全表可见行勾选为 0，改按项目名/寻源单号匹配重试…")
            checked = self._check_rows_selenium(
                project_name,
                sourcing_no=sourcing_no,
                clear_first=False,
            )
        if checked > 0:
            logger.info(
                f"  ✓ 已勾选 {checked} 行（筛后列表内全部物料，供扩展供应商）"
            )
        return checked > 0

    def extend_supplier_for_project(
        self, project_name, target_supplier_name, sourcing_no=None
    ):
        """
        已勾选项目行后：PSM操作 → 扩展供应商 → 选目标供应商 → 保存 → 确认。

        若首次点「扩展供应商」弹出「相同寻源单号与 Item」信息框：
        点确定 → 仅重筛状态「已发送」→ 左侧全选 → 再 PSM → 扩展供应商。

        Returns:
            bool: 是否完成扩展并确认保存
        """
        project_name = (project_name or "").strip()
        target = (target_supplier_name or "").strip()
        sourcing_no = (sourcing_no or "").strip()
        if not project_name:
            logger.warning("  项目名称为空，跳过扩展供应商")
            return False
        if not self.is_allowed_extend_supplier(target):
            logger.error(f"  目标供应商非法或含家用电梯: {target!r}")
            return False

        logger.info(
            f"  OMS 扩展供应商: 项目「{project_name[:40]}…」"
            f" → 按 PO 改为 {target}（搜索「{OMS_SUPPLIER_EXTEND_SEARCH_KEY}」后精确点选）"
        )
        if not self._prepare_all_filtered_rows_for_extend_supplier(
            project_name, sourcing_no
        ):
            logger.error(f"  筛后列表无可见行可勾选，无法扩展供应商: {project_name[:50]}")
            return False
        logger.info("  打开 PSM → 扩展供应商…")
        time.sleep(0.6)

        dialog_ready = False
        refiltered_after_info = False
        for attempt in range(4):
            if not self._open_psm_dropdown():
                logger.warning(f"  未能展开 PSM 下拉 ({attempt + 1}/4)")
                time.sleep(0.8)
                continue
            if not self._click_psm_extend_supplier_menu():
                logger.warning(f"  未能点击「扩展供应商」菜单 ({attempt + 1}/4)")
                time.sleep(0.8)
                continue
            time.sleep(1.0)
            if self._dismiss_oms_duplicate_selection_dialog(max_wait_sec=12):
                if refiltered_after_info:
                    logger.error(
                        "  重筛「已发送」后仍出现信息弹窗，请人工：筛后全选物料行再扩展供应商"
                    )
                    return False
                logger.info(
                    "  OMS 信息弹窗：已点「确定」；"
                    "仅重筛状态「已发送」→ 左侧全选 → 再 PSM → 扩展供应商…"
                )
                self._refilter_status_sent_only_for_extend_supplier()
                if not self._prepare_all_filtered_rows_for_extend_supplier(
                    project_name, sourcing_no
                ):
                    logger.error("  重筛「已发送」后未能勾选列表内物料行")
                    return False
                refiltered_after_info = True
                logger.info("  ✓ 已重筛「已发送」并勾选全部物料行，再次打开 PSM…")
                time.sleep(0.6)
                continue
            if self._wait_extend_supplier_layer(timeout=10):
                dialog_ready = True
                break
            logger.warning(
                f"  扩展供应商窗口未出现，重试 PSM ({attempt + 1}/4)…"
            )
            time.sleep(0.8)
        if not dialog_ready:
            logger.error(
                "  扩展供应商弹窗未打开。"
                "若曾出现信息弹窗，请确认已筛选「已发送」并勾选筛后全部物料行。"
            )
            return False

        if not self._fill_extend_supplier_dialog(target):
            return False
        saved, confirmed_in_iframe = self._click_extend_supplier_save()
        if not saved:
            return False
        if confirmed_in_iframe:
            logger.info(f"  ✓ 扩展供应商已保存并确认: {target}")
            self._wait_extend_supplier_layer_closed(timeout=12)
            return True
        time.sleep(0.6)
        self.browser.driver.switch_to.default_content()
        if self._click_extend_supplier_save_confirm(max_wait_sec=15):
            logger.info(f"  ✓ 扩展供应商已保存并确认: {target}")
            self._wait_extend_supplier_layer_closed(timeout=12)
            return True
        confirmed = self._click_oms_confirm_dialog(max_wait_sec=8)
        if confirmed:
            logger.info(f"  ✓ 扩展供应商已保存并确认(通用确定): {target}")
            self._wait_extend_supplier_layer_closed(timeout=12)
            return True
        logger.warning(
            "  扩展供应商保存后未点击「确认保存?」弹窗的「确定」，请人工核对 OMS"
        )
        return False

    def _psm_dropdown_root_js(self):
        """返回用于定位 PSM 下拉的 JS 片段（内联在 execute_script 中）。"""
        return """
            function vis(el) {
                if (!el) return false;
                try {
                    var r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) return false;
                } catch (e) {}
                return el.offsetParent !== null;
            }
            function raw(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            function findPsmRoot() {
                var nodes = document.querySelectorAll('button, a, div[role="button"]');
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    if (!vis(el)) continue;
                    var tx = raw(el.textContent || el.value || '');
                    if (tx === 'PSM操作' || tx.indexOf('PSM操作') >= 0) {
                        return el.closest('.btn-group, .dropdown, li.dropdown, .btn-toolbar')
                            || el.parentElement;
                    }
                }
                return null;
            }
        """

    def _is_psm_menu_visible(self):
        driver = self.browser.driver
        return bool(
            driver.execute_script(
                self._psm_dropdown_root_js()
                + """
            var root = findPsmRoot();
            if (!root) return false;
            var menu = root.querySelector('.dropdown-menu');
            if (!menu || !vis(menu)) return false;
            var links = menu.querySelectorAll('a');
            for (var j = 0; j < links.length; j++) {
                if (vis(links[j]) && raw(links[j].textContent || '').length > 0)
                    return true;
            }
            return false;
        """
            )
        )

    def _open_psm_dropdown(self):
        """点击「PSM操作」并确认其下拉菜单已展开（仅认 PSM 旁的下拉，避免误点其它菜单）。"""
        driver = self.browser.driver
        if self._is_psm_menu_visible():
            logger.info("  ✓ PSM 下拉已展开")
            return True

        clicked = driver.execute_script(
            self._psm_dropdown_root_js()
            + """
            var root = findPsmRoot();
            if (!root) return false;
            var toggle = root.querySelector(
                'button[data-toggle="dropdown"], a[data-toggle="dropdown"], '
                + 'button.dropdown-toggle, a.dropdown-toggle, button, a'
            );
            if (!toggle || !vis(toggle)) return false;
            try { toggle.scrollIntoView({block: 'center'}); } catch (e) {}
            toggle.click();
            return true;
        """
        )
        if not clicked:
            logger.warning("  未找到「PSM操作」按钮")
            return False
        logger.info("  ✓ 已点击 PSM操作")
        time.sleep(1.0)
        if self._is_psm_menu_visible():
            return True
        time.sleep(0.8)
        return self._is_psm_menu_visible()

    def _click_psm_toolbar_button(self):
        """兼容旧调用：等同展开 PSM 下拉。"""
        return self._open_psm_dropdown()

    def _click_psm_extend_supplier_menu(self):
        """仅在 PSM 下拉内点击「扩展供应商」链接（与发邮件菜单同一策略）。"""
        driver = self.browser.driver
        want = "扩展供应商"

        if not self._is_psm_menu_visible():
            if not self._open_psm_dropdown():
                return False

        try:
            root = driver.execute_script(
                self._psm_dropdown_root_js() + "return findPsmRoot();"
            )
            if root:
                menu = root.find_element(By.CSS_SELECTOR, ".dropdown-menu")
                for el in menu.find_elements(By.TAG_NAME, "a"):
                    try:
                        if not el.is_displayed():
                            continue
                        txt = (el.text or "").strip()
                        if want not in txt:
                            continue
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                            el,
                        )
                        time.sleep(0.2)
                        ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
                        logger.info(f"  ✓ 已点击 PSM 菜单项: {txt[:40]}")
                        return True
                    except Exception:
                        continue
        except Exception:
            pass

        hit = driver.execute_script(
            self._psm_dropdown_root_js()
            + """
            var want = arguments[0];
            function norm(t) { return (t || '').replace(/\\s+/g, ''); }
            var root = findPsmRoot();
            if (!root) return '';
            var menu = root.querySelector('.dropdown-menu');
            if (!menu || !vis(menu)) return '';
            var links = menu.querySelectorAll('a');
            for (var j = 0; j < links.length; j++) {
                var a = links[j];
                if (!vis(a)) continue;
                var tx = raw(a.textContent || '');
                if (norm(tx).indexOf(norm(want)) < 0 && tx.indexOf(want) < 0) continue;
                try { a.scrollIntoView({block: 'center'}); } catch (e) {}
                a.click();
                return tx;
            }
            return '';
        """,
            want,
        )
        if hit:
            logger.info(f"  ✓ 已点击 PSM 菜单项(JS): {hit[:40]}")
            return True

        logger.warning("  PSM 下拉中未找到「扩展供应商」链接")
        return False

    @staticmethod
    def _extend_supplier_iframe_walk_js(inner_fn_body):
        """在 document 及同源 iframe 内执行 inner_fn_body(doc)，返回首个非空结果。"""
        return (
            """
            function vis(el) {
                if (!el) return false;
                var st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                if (parseFloat(st.opacity || '1') < 0.05) return false;
                var r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4;
            }
            function tryClick(el) {
                if (!vis(el)) return false;
                try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
                try { el.focus(); } catch (e) {}
                try { el.click(); return true; } catch (e) {}
                try {
                    var ev = {bubbles: true, cancelable: true, view: window};
                    el.dispatchEvent(new MouseEvent('mousedown', ev));
                    el.dispatchEvent(new MouseEvent('mouseup', ev));
                    el.dispatchEvent(new MouseEvent('click', ev));
                    return true;
                } catch (e2) {}
                return false;
            }
            function findExtendLayer(doc) {
                var layers = doc.querySelectorAll(
                    '.layui-layer, .modal.in, .modal.show, .modal-dialog, [role="dialog"]'
                );
                for (var i = layers.length - 1; i >= 0; i--) {
                    var el = layers[i];
                    if (!vis(el)) continue;
                    var title = el.querySelector(
                        '.layui-layer-title, .modal-title, .panel-title, h4'
                    );
                    var tTitle = title ? (title.innerText || title.textContent || '') : '';
                    if (tTitle.indexOf('扩展供应商') >= 0) return el;
                    var all = (el.innerText || el.textContent || '');
                    if (all.indexOf('扩展供应商') >= 0 && all.indexOf('保存') >= 0) return el;
                }
                return null;
            }
            function extendScopes(doc) {
                var layer = findExtendLayer(doc);
                if (!layer) return [];
                var content = layer.querySelector('.layui-layer-content') || layer;
                return [content, layer, doc];
            }
            function walk(doc, depth) {
                if (!doc || depth > 8) return null;
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

    def _find_extend_supplier_layer(self):
        """定位「扩展供应商」弹窗（layui / Bootstrap modal，含 iframe 内）。"""
        driver = self.browser.driver
        try:
            hit = driver.execute_script(
                self._extend_supplier_iframe_walk_js(
                    """
                var scopes = extendScopes(doc);
                for (var s = 0; s < scopes.length; s++) {
                    if (scopes[s]) return findExtendLayer(doc);
                }
                return null;
                """
                )
            )
            if hit:
                return hit
        except Exception:
            pass
        try:
            for layer in driver.find_elements(By.CSS_SELECTOR, ".layui-layer"):
                try:
                    if not layer.is_displayed():
                        continue
                except Exception:
                    continue
                try:
                    title_el = layer.find_element(
                        By.CSS_SELECTOR, ".layui-layer-title"
                    )
                    if "扩展供应商" in (title_el.text or ""):
                        return layer
                except Exception:
                    if "扩展供应商" in (layer.text or ""):
                        return layer
        except Exception:
            pass
        try:
            for modal in driver.find_elements(
                By.CSS_SELECTOR,
                ".modal.in, .modal.show, div.modal[style*='display: block']",
            ):
                try:
                    if not modal.is_displayed():
                        continue
                except Exception:
                    continue
                if "扩展供应商" in (modal.text or ""):
                    return modal
        except Exception:
            pass
        return None

    def _wait_extend_supplier_layer(self, timeout=12):
        try:
            WebDriverWait(self.browser.driver, timeout).until(
                lambda d: self._find_extend_supplier_layer() is not None
            )
            return True
        except TimeoutException:
            return False

    def _switch_to_extend_vendor_iframe(self):
        """扩展供应商弹窗为 layui-layer-iframe，表单在 ext_ven.html 内。"""
        driver = self.browser.driver
        driver.switch_to.default_content()
        layer = self._find_extend_supplier_layer()
        if not layer:
            return False
        try:
            iframe = layer.find_element(By.CSS_SELECTOR, "iframe")
            driver.switch_to.frame(iframe)
            return True
        except Exception:
            driver.switch_to.default_content()
            return False

    def _fill_extend_supplier_via_vendor_iframe(self, target_supplier):
        """
        ext_ven.html：#vendor_id 为 bootstrap-select(selectpicker)。
        扫描确认：须 switch_to iframe 后再操作，主文档点击无效。
        """
        driver = self.browser.driver
        search_key = OMS_SUPPLIER_EXTEND_SEARCH_KEY
        if not self._switch_to_extend_vendor_iframe():
            return False
        try:
            result = driver.execute_script(
                """
                var target = arguments[0];
                var searchKey = arguments[1];
                function norm(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
                function allowed(tx) {
                    if (!tx || tx.indexOf('家用电梯') >= 0) return false;
                    return tx === target;
                }
                function readBtnText() {
                    var bs = document.querySelector('.bootstrap-select');
                    var btn = bs ? bs.querySelector('button.dropdown-toggle') : null;
                    return btn ? norm(btn.innerText || btn.textContent || btn.title || '') : '';
                }
                function verify() {
                    var tx = readBtnText();
                    return tx === target || tx.indexOf(target) >= 0;
                }
                var sel = document.querySelector('#vendor_id');
                if (!sel) return {ok: false, step: 'no_vendor_id'};
                var optVal = null;
                for (var i = 0; i < sel.options.length; i++) {
                    var txt = norm(sel.options[i].textContent || sel.options[i].innerText || '');
                    if (allowed(txt)) {
                        optVal = sel.options[i].value;
                        break;
                    }
                }
                if (optVal !== null) {
                    for (var j = 0; j < sel.options.length; j++) {
                        sel.options[j].selected = (sel.options[j].value === optVal);
                    }
                    try {
                        if (typeof jQuery !== 'undefined' && jQuery(sel).selectpicker) {
                            jQuery(sel).selectpicker('val', optVal);
                            jQuery(sel).selectpicker('refresh');
                        }
                    } catch (e) {}
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    if (verify()) return {ok: true, step: 'selectpicker_val', text: readBtnText()};
                }
                var toggle = document.querySelector(
                    '#vendor_id + .bootstrap-select button.dropdown-toggle, '
                    + '.bootstrap-select button.dropdown-toggle'
                );
                if (toggle) toggle.click();
                var searchInp = document.querySelector(
                    '.bootstrap-select.open .bs-searchbox input, .dropdown-menu.show .bs-searchbox input'
                );
                if (searchInp) {
                    searchInp.focus();
                    searchInp.value = searchKey;
                    searchInp.dispatchEvent(new Event('input', {bubbles: true}));
                    searchInp.dispatchEvent(new Event('keyup', {bubbles: true}));
                }
                var menus = document.querySelectorAll(
                    '.bootstrap-select.open .dropdown-menu, .dropdown-menu.show'
                );
                for (var m = 0; m < menus.length; m++) {
                    var items = menus[m].querySelectorAll('li a, li span.text, li');
                    for (var k = 0; k < items.length; k++) {
                        var tx2 = norm(items[k].innerText || items[k].textContent || '');
                        if (!allowed(tx2)) continue;
                        items[k].click();
                        if (verify()) return {ok: true, step: 'click_li', text: readBtnText()};
                        return {ok: true, step: 'click_li_unverified', text: readBtnText()};
                    }
                }
                return {ok: false, step: 'pick_fail', btn: readBtnText(), optVal: optVal};
                """,
                target_supplier,
                search_key,
            )
            if result and result.get("ok"):
                logger.info(
                    f"  ✓ iframe #vendor_id 已选供应商 ({result.get('step')}): "
                    f"{result.get('text', target_supplier)[:60]}"
                )
                return True
            logger.warning(f"  iframe #vendor_id 选供应商失败: {result}")
            return False
        finally:
            driver.switch_to.default_content()

    def _click_extend_supplier_select_field(self):
        """图一→图二：点击弹窗内「请选择」那一栏（layui / bootstrap-select / iframe）。"""
        driver = self.browser.driver
        driver.switch_to.default_content()
        opened = driver.execute_script(
            self._extend_supplier_iframe_walk_js(
                """
            function clickInScope(scope) {
                function norm(t) { return (t || '').replace(/\s+/g, '').trim(); }
                function isFileWidget(el) {
                    if (!el) return false;
                    var box = el.closest('.file-input, .file-caption, .kv-fileinput-caption');
                    var cls = (el.className || '') + ' ' + (box ? box.className || '' : '');
                    var name = (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '');
                    return /file-caption|file-input|kv-fileinput|filename|attachment|upload/i.test(cls + ' ' + name);
                }
                function clickSupplierControlIn(box, why) {
                    if (!box || isFileWidget(box)) return '';
                    var controls = box.querySelectorAll(
                        '.layui-form-select, .bootstrap-select, .select2-container, '
                        + 'button.dropdown-toggle, input[placeholder*=请选择], input[value*=请选择], '
                        + '.layui-select-title, .layui-input, select'
                    );
                    for (var i = 0; i < controls.length; i++) {
                        var el = controls[i];
                        if (!vis(el) || isFileWidget(el)) continue;
                        var tx = norm(el.innerText || el.textContent || el.getAttribute('title') || el.value || el.getAttribute('placeholder') || '');
                        if (tx && tx.indexOf('上传') >= 0) continue;
                        var clickable = el.querySelector && el.querySelector('button.dropdown-toggle, .layui-select-title, input') || el;
                        if (clickable && !isFileWidget(clickable) && tryClick(clickable)) return why;
                    }
                    return '';
                }
                var labelNodes = scope.querySelectorAll('label, td, th, div, span');
                for (var n = 0; n < labelNodes.length; n++) {
                    var label = labelNodes[n];
                    var text = norm(label.innerText || label.textContent || '');
                    if (text.indexOf('扩展供应商') < 0 && text.indexOf('寻源供应商') < 0) continue;
                    var boxes = [
                        label.closest('.form-group'),
                        label.closest('tr'),
                        label.parentElement,
                        label.parentElement ? label.parentElement.parentElement : null
                    ];
                    for (var bx = 0; bx < boxes.length; bx++) {
                        var hit = clickSupplierControlIn(boxes[bx], 'label-near-supplier-control');
                        if (hit) return hit;
                    }
                    var sib = label.nextElementSibling;
                    for (var step = 0; sib && step < 4; step++, sib = sib.nextElementSibling) {
                        hit = clickSupplierControlIn(sib, 'label-next-supplier-control');
                        if (hit) return hit;
                    }
                }
                var bses = scope.querySelectorAll('.bootstrap-select');
                for (var b = 0; b < bses.length; b++) {
                    if (isFileWidget(bses[b])) continue;
                    var btn = bses[b].querySelector('button.dropdown-toggle');
                    if (!btn || isFileWidget(btn)) continue;
                    var tx = norm(btn.innerText || btn.textContent || btn.getAttribute('title') || '');
                    if (tx && tx.indexOf('请选择') < 0 && tx.indexOf('供应商') < 0) continue;
                    if (tryClick(btn)) return 'bootstrap-select-supplier';
                }
                var lays = scope.querySelectorAll('.layui-form-select');
                for (var l = 0; l < lays.length; l++) {
                    var title = lays[l].querySelector('.layui-select-title');
                    if (title && tryClick(title)) return 'layui-select-title';
                    var inp = lays[l].querySelector('input, .layui-input');
                    if (inp && tryClick(inp)) return 'layui-input';
                    if (tryClick(lays[l])) return 'layui-form-select';
                }
                var inputs = scope.querySelectorAll('input');
                for (var i = 0; i < inputs.length; i++) {
                    var ph = (inputs[i].getAttribute('placeholder') || '');
                    var v = (inputs[i].value || '');
                    if (ph.indexOf('请选择') >= 0 || v.indexOf('请选择') >= 0) {
                        if (tryClick(inputs[i])) return 'input-请选择';
                    }
                }
                var s2 = scope.querySelector('.select2-selection');
                if (s2 && tryClick(s2)) return 'select2';
                var nodes = scope.querySelectorAll(
                    'div, span, button, a, td, .layui-input'
                );
                for (var c = 0; c < nodes.length; c++) {
                    var el = nodes[c];
                    var tx = (el.innerText || el.textContent || '').replace(/\\s+/g, '').trim();
                    if (tx !== '请选择') continue;
                    var r = el.getBoundingClientRect();
                    if (r.width < 60 || r.height < 12) continue;
                    if (tryClick(el)) return 'text-请选择';
                    var box = el.closest(
                        '.layui-form-select, .bootstrap-select, .select2-container, .input-group'
                    );
                    if (box && tryClick(box)) return '请选择-box';
                }
                return '';
            }
            var scopes = extendScopes(doc);
            for (var s = 0; s < scopes.length; s++) {
                var hit = clickInScope(scopes[s]);
                if (hit) return hit;
            }
            return null;
                """
            )
        )
        if opened:
            logger.info(f"  ✓ 已点击「扩展供应商」栏（{opened}）")
            return True

        xpaths = [
            "//div[contains(@class,'layui-layer')]"
            "[.//*[contains(@class,'layui-layer-title') and contains(.,'扩展供应商')]]"
            "//div[contains(@class,'layui-select-title')]",
            "//div[contains(@class,'layui-layer')]"
            "[.//*[contains(@class,'layui-layer-title') and contains(.,'扩展供应商')]]"
            "//input[contains(@placeholder,'请选择')]",
            "//div[contains(@class,'layui-layer')]"
            "[.//*[contains(@class,'layui-layer-title') and contains(.,'扩展供应商')]]"
            "//button[contains(@class,'dropdown-toggle')]",
            "//div[contains(@class,'layui-layer')]"
            "[.//*[contains(@class,'layui-layer-title') and contains(.,'扩展供应商')]]"
            "//*[normalize-space(text())='请选择']",
        ]
        for xp in xpaths:
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    try:
                        if not el.is_displayed():
                            continue
                    except Exception:
                        continue
                    ActionChains(driver).move_to_element(el).pause(0.1).click().perform()
                    logger.info(f"  ✓ 已点击「扩展供应商」栏 (XPath)")
                    return True
            except Exception:
                continue

        try:
            layer = self._find_extend_supplier_layer()
            if layer:
                for iframe in layer.find_elements(By.CSS_SELECTOR, "iframe"):
                    try:
                        driver.switch_to.frame(iframe)
                        hit = driver.execute_script(
                            """
                            function vis(el) {
                                if (!el) return false;
                                var st = window.getComputedStyle(el);
                                if (st.display === 'none' || st.visibility === 'hidden') return false;
                                var r = el.getBoundingClientRect();
                                return r.width > 4 && r.height > 4;
                            }
                            function norm(t) { return (t || '').replace(/\s+/g, '').trim(); }
                            function isFileWidget(el) {
                                if (!el) return false;
                                var box = el.closest('.file-input, .file-caption, .kv-fileinput-caption');
                                var cls = (el.className || '') + ' ' + (box ? box.className || '' : '');
                                var name = (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '');
                                return /file-caption|file-input|kv-fileinput|filename|attachment|upload/i.test(cls + ' ' + name);
                            }
                            function tryClick(el) {
                                if (!vis(el) || isFileWidget(el)) return false;
                                try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
                                try { el.focus(); } catch (e) {}
                                try { el.click(); return true; } catch (e) {}
                                return false;
                            }
                            function clickSupplierControlIn(box) {
                                if (!box || isFileWidget(box)) return '';
                                var controls = box.querySelectorAll(
                                    '.layui-form-select, .bootstrap-select, .select2-container, '
                                    + 'button.dropdown-toggle, input[placeholder*=请选择], input[value*=请选择], '
                                    + '.layui-select-title, .layui-input, select'
                                );
                                for (var i = 0; i < controls.length; i++) {
                                    var el = controls[i];
                                    if (!vis(el) || isFileWidget(el)) continue;
                                    var clickable = el.querySelector && el.querySelector('button.dropdown-toggle, .layui-select-title, input') || el;
                                    if (tryClick(clickable)) return 'iframe-label-near';
                                }
                                return '';
                            }
                            var labels = document.querySelectorAll('label, td, th, div, span');
                            for (var n = 0; n < labels.length; n++) {
                                var label = labels[n];
                                var text = norm(label.innerText || label.textContent || '');
                                if (text.indexOf('扩展供应商') < 0 && text.indexOf('寻源供应商') < 0) continue;
                                var boxes = [label.closest('.form-group'), label.closest('tr'), label.parentElement,
                                    label.parentElement ? label.parentElement.parentElement : null];
                                for (var b = 0; b < boxes.length; b++) {
                                    var h = clickSupplierControlIn(boxes[b]);
                                    if (h) return h;
                                }
                            }
                            return '';
                            """
                        )
                        if hit:
                            driver.switch_to.default_content()
                            logger.info(
                                f"  ✓ 已点击「扩展供应商」栏 ({hit})"
                            )
                            return True
                        driver.switch_to.default_content()
                    except Exception:
                        driver.switch_to.default_content()
        except Exception:
            driver.switch_to.default_content()

        debug = driver.execute_script(
            self._extend_supplier_iframe_walk_js(
                """
            var layer = findExtendLayer(doc);
            if (!layer) return {found:false};
            var scope = layer.querySelector('.layui-layer-content') || layer;
            return {
                found: true,
                layui: scope.querySelectorAll('.layui-form-select').length,
                bootstrap: scope.querySelectorAll('.bootstrap-select').length,
                inputs: scope.querySelectorAll('input').length,
                iframes: scope.querySelectorAll('iframe').length
            };
                """
            )
        )
        logger.warning(
            f"  未能点击「扩展供应商」栏（请选择）；页面探测: {debug}"
        )
        return False

    def _open_extend_supplier_dropdown(self, layer=None):
        """图一→图二：点击「扩展供应商/寻源供应商」栏。"""
        return self._click_extend_supplier_select_field()

    def _type_extend_supplier_search(self, layer, search_key):
        """图一展开后：在下拉面板顶部白色搜索框输入配置的供应商关键字。"""
        driver = self.browser.driver
        driver.switch_to.default_content()

        def type_active_input():
            try:
                active = driver.switch_to.active_element
                tag = (active.tag_name or "").lower()
                ph = (active.get_attribute("placeholder") or "")
                val = (active.get_attribute("value") or "")
                if tag != "input" or "请选择" in ph:
                    return False
                active.click()
                active.send_keys(Keys.CONTROL, "a")
                if val:
                    active.send_keys(Keys.BACKSPACE)
                active.send_keys(search_key)
                time.sleep(0.4)
                if search_key in (active.get_attribute("value") or ""):
                    logger.info(
                        f"  ✓ 已在图一展开后的顶部搜索框输入: {search_key} (active input)"
                    )
                    return True
            except Exception:
                return False
            return False

        # 你截图中的正确位置是展开面板顶部输入框；点击主栏后焦点通常已在这里。
        for _ in range(5):
            if type_active_input():
                return True
            time.sleep(0.2)

        typed = driver.execute_script(
            """
            var KEY = arguments[0];
            """
            + self._extend_supplier_iframe_walk_js(
                """
            var key = KEY;
            function norm(t) { return (t || '').replace(/\s+/g, ' ').trim(); }
            function rectScore(el) {
                var r = el.getBoundingClientRect();
                if (!r || r.width < 20 || r.height < 10) return -1;
                return (r.top * 10000) + r.left;
            }
            function isFileUploadField(el) {
                if (!el) return false;
                var box = el.closest('.file-input, .file-caption, .kv-fileinput-caption, .input-group');
                var cls = (el.className || '') + ' ' + (box ? box.className || '' : '');
                var name = (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '');
                return /file-caption|file-input|kv-fileinput|filename|attachment|upload/i.test(cls + ' ' + name);
            }
            function isMainSelectField(el) {
                var ph = (el.getAttribute('placeholder') || '');
                var val = (el.value || '');
                var cls = (el.className || '');
                var txt = norm(el.innerText || el.textContent || '');
                return isFileUploadField(el) || ph.indexOf('请选择') >= 0 || val.indexOf('请选择') >= 0
                    || txt === '请选择' || cls.indexOf('layui-unselect') >= 0;
            }
            function fireTextEvents(el) {
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keydown', {key: key[0] || '', bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {key: key[key.length - 1] || '', bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }
            function typeIn(el) {
                if (!vis(el) || isMainSelectField(el)) return false;
                try {
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    el.focus();
                    el.click();
                    if (el.isContentEditable) {
                        el.textContent = key;
                        fireTextEvents(el);
                        return norm(el.innerText || el.textContent).indexOf(key) >= 0;
                    }
                    if ('value' in el) {
                        el.value = '';
                        fireTextEvents(el);
                        el.value = key;
                        fireTextEvents(el);
                        return (el.value || '').indexOf(key) >= 0;
                    }
                } catch (e) {}
                return false;
            }
            function candidatesIn(scope) {
                return Array.prototype.slice.call(scope.querySelectorAll([
                    '.xm-select-dropdown input',
                    '.xm-search input',
                    '.xm-input',
                    '.xm-select input',
                    '.bs-searchbox input',
                    '.dropdown-menu input',
                    '.select2-search__field',
                    '.layui-form-select.layui-form-selected input',
                    '.layui-form-select dl input',
                    '[contenteditable="true"]',
                    'input[placeholder*="搜索"]',
                    'input[type="search"]',
                    'input[type="text"]',
                    'input:not([type])'
                ].join(',')));
            }
            function bestSearchInput(scope) {
                var inputs = candidatesIn(scope).filter(function(el) {
                    return vis(el) && !isMainSelectField(el) && rectScore(el) >= 0;
                });
                inputs.sort(function(a, b) {
                    var ac = (a.className || '') + ' ' + (a.getAttribute('placeholder') || '');
                    var bc = (b.className || '') + ' ' + (b.getAttribute('placeholder') || '');
                    var ap = /xm|search|搜索|bs-searchbox|select2-search/.test(ac) ? -100000000 : 0;
                    var bp = /xm|search|搜索|bs-searchbox|select2-search/.test(bc) ? -100000000 : 0;
                    if (/file-caption|file-input|kv-fileinput/i.test(ac)) ap += 1000000000;
                    if (/file-caption|file-input|kv-fileinput/i.test(bc)) bp += 1000000000;
                    return (ap + rectScore(a)) - (bp + rectScore(b));
                });
                return inputs.length ? inputs[0] : null;
            }
            function typeInScope(scope) {
                var panels = Array.prototype.slice.call(scope.querySelectorAll([
                    '.xm-select-dropdown',
                    '.xm-select-parent .xm-select-dropdown',
                    '.layui-form-select.layui-form-selected',
                    '.layui-form-select.layui-form-selected dl',
                    '.layui-form-select.layui-form-selected .layui-anim',
                    '.bootstrap-select.open .dropdown-menu',
                    '.dropdown-menu.show',
                    '.select2-container--open',
                    '.select2-dropdown'
                ].join(','))).filter(vis);
                panels.sort(function(a, b) { return rectScore(a) - rectScore(b); });
                for (var p = 0; p < panels.length; p++) {
                    var panelInput = bestSearchInput(panels[p]);
                    if (panelInput && typeIn(panelInput)) return 'open-dropdown-search:' + panelInput.tagName + '.' + panelInput.className;
                }
                var scopeInput = bestSearchInput(scope);
                if (scopeInput && typeIn(scopeInput)) return 'visible-search:' + scopeInput.tagName + '.' + scopeInput.className;
                return '';
            }
            var scopes = extendScopes(doc);
            for (var s = 0; s < scopes.length; s++) {
                var hit = typeInScope(scopes[s]);
                if (hit) return hit;
            }
            var docHit = typeInScope(doc);
            if (docHit) return docHit;
            return null;
                """
            ),
            search_key,
        )
        if typed:
            logger.info(f"  ✓ 已在图一展开后的顶部搜索框输入: {search_key} ({typed})")
            return True

        try:
            candidates = layer.find_elements(
                By.CSS_SELECTOR,
                ".xm-select-dropdown input, .xm-search input, .xm-input, "
                ".bs-searchbox input, .dropdown-menu input, .select2-search__field, "
                ".layui-form-select.layui-form-selected input[type='text'], "
                ".layui-form-select.layui-form-selected input:not([type]), "
                ".bootstrap-select.open input[type='text'], input[type='search'], "
                "input[placeholder*='搜索'], input[type='text'], input:not([type])",
            )
            for inp in candidates:
                try:
                    if not inp.is_displayed():
                        continue
                    ph = (inp.get_attribute("placeholder") or "")
                    val = (inp.get_attribute("value") or "")
                    cls = (inp.get_attribute("class") or "")
                    if "请选择" in ph or "请选择" in val or "layui-unselect" in cls:
                        continue
                    inp.click()
                    inp.send_keys(Keys.CONTROL, "a")
                    inp.send_keys(search_key)
                    time.sleep(0.3)
                    if search_key in (inp.get_attribute("value") or ""):
                        logger.info(
                            f"  ✓ 已在图一展开后的顶部搜索框输入: {search_key} (Selenium)"
                        )
                        return True
                except Exception:
                    continue
        except Exception:
            pass

        try:
            debug = driver.execute_script(
                self._extend_supplier_iframe_walk_js(
                    """
                var items = [];
                var nodes = doc.querySelectorAll('input, [contenteditable="true"], textarea');
                for (var i = 0; i < nodes.length && items.length < 30; i++) {
                    var el = nodes[i];
                    if (!vis(el)) continue;
                    var r = el.getBoundingClientRect();
                    items.push({
                        tag: el.tagName,
                        type: el.getAttribute('type') || '',
                        cls: (el.className || '').toString().slice(0, 80),
                        ph: el.getAttribute('placeholder') || '',
                        val: el.value || el.innerText || el.textContent || '',
                        x: Math.round(r.left),
                        y: Math.round(r.top),
                        w: Math.round(r.width),
                        h: Math.round(r.height)
                    });
                }
                return JSON.stringify(items);
                    """
                )
            )
            logger.warning(f"  未输入到搜索框；当前可见输入框摘要: {debug}")
        except Exception as e:
            logger.warning(f"  未输入到搜索框；探测输入框失败: {e}")
        return False

    def _blur_extend_supplier_selection(self, layer, target_supplier):
        """图三→图四：点选 PO 对应供应商后，点击弹窗空白处收起下拉，主栏显示已选全称。"""
        driver = self.browser.driver
        driver.execute_script(
            """
            var root = arguments[0];
            function tryClick(el) {
                if (!el) return false;
                try { el.click(); return true; } catch (e) {}
                return false;
            }
            var title = root.querySelector('.layui-layer-title, .modal-title, h4');
            if (title && tryClick(title)) return 'title';
            var pad = root.querySelector('.layui-layer-content, .modal-body, .layui-layer');
            if (pad && tryClick(pad)) return 'content';
            try { document.body.click(); } catch (e) {}
            return 'body';
            """,
            layer,
        )
        time.sleep(0.5)
        shown = driver.execute_script(
            """
            var root = arguments[0];
            var target = arguments[1];
            function norm(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            var boxes = root.querySelectorAll(
                '.layui-form-select .layui-input, input, .select2-selection__rendered'
            );
            for (var i = 0; i < boxes.length; i++) {
                var tx = norm(boxes[i].value || boxes[i].innerText || boxes[i].textContent);
                if (tx === target) return tx;
            }
            var all = (root.innerText || root.textContent || '');
            return all.indexOf(target) >= 0 ? target : '';
            """,
            layer,
            target_supplier,
        )
        if shown:
            logger.info(f"  ✓ 主栏已显示供应商（图四）: {shown}")
            return True
        logger.warning(
            f"  点空白后主栏未显示目标供应商，请核对: {target_supplier}"
        )
        return False

    def _fill_extend_supplier_dialog(self, target_supplier):
        """
        扩展供应商弹窗（图一至图四）：
        1. 点「扩展供应商」栏展开下拉（图二）
        2. 在下拉顶部空白搜索框输入配置的供应商关键字
        3. 点选 target_supplier（由 PO 工厂决定，非写死中国）
        4. 点弹窗空白处确认，主栏显示全称（图四）
        """
        driver = self.browser.driver
        layer = self._find_extend_supplier_layer()
        if not layer:
            logger.warning("  未找到「扩展供应商」弹窗")
            return False
        if not self.is_allowed_extend_supplier(target_supplier):
            logger.error(f"  目标供应商非法: {target_supplier!r}")
            return False

        logger.info(
            f"  扩展供应商弹窗：将选 PO 对应供应商 → {target_supplier}"
        )

        if self._fill_extend_supplier_via_vendor_iframe(target_supplier):
            return True

        dropdown_open = False
        for click_try in range(3):
            if not self._click_extend_supplier_select_field():
                if click_try >= 2:
                    logger.warning("  未能点击「扩展供应商」栏（图一→图二）")
                    return False
                time.sleep(0.5)
                continue
            time.sleep(0.7)
            driver.switch_to.default_content()
            dropdown_open = driver.execute_script(
                self._extend_supplier_iframe_walk_js(
                    """
                var scopes = extendScopes(doc);
                for (var s = 0; s < scopes.length; s++) {
                    var scope = scopes[s];
                    if (scope.querySelectorAll(
                        '.layui-form-select.layui-form-selected dl dd, '
                        + '.bootstrap-select.open .dropdown-menu li, '
                        + '.select2-container--open .select2-results__option'
                    ).length) return true;
                }
                return doc.querySelectorAll(
                    '.layui-form-select.layui-form-selected, .bootstrap-select.open'
                ).length > 0;
                    """
                )
            )
            if dropdown_open:
                break
            logger.info(
                f"  点击后下拉未展开，重试点击「扩展供应商」栏 ({click_try + 2}/3)…"
            )
        if not dropdown_open:
            logger.warning("  已点击但下拉列表未展开，仍尝试输入搜索…")

        search_key = OMS_SUPPLIER_EXTEND_SEARCH_KEY
        if not self._type_extend_supplier_search(layer, search_key):
            logger.warning("  未找到下拉内空白搜索框（图二）")
            return False
        time.sleep(1.0)

        driver.switch_to.default_content()
        picked = driver.execute_script(
            """
            var TARGET = arguments[0];
            """
            + self._extend_supplier_iframe_walk_js(
                """
            var target = TARGET;
            function norm(t) { return (t || '').replace(/\\s+/g, ' ').trim(); }
            function allowed(tx) {
                if (!tx || tx.indexOf('家用电梯') >= 0) return false;
                return tx === target;
            }
            function pickIn(scope) {
                var nodes = scope.querySelectorAll(
                    'dd, li, .layui-form-select dl dd, .select2-results__option, '
                    + '.dropdown-menu li, .dropdown-menu li a, ul li, a, span.text'
                );
                for (var i = 0; i < nodes.length; i++) {
                    var el = nodes[i];
                    if (!vis(el)) continue;
                    var tx = norm(el.innerText || el.textContent || '');
                    if (!allowed(tx)) continue;
                    try { el.scrollIntoView({block:'center'}); } catch (e) {}
                    el.click();
                    return tx;
                }
                return '';
            }
            var scopes = extendScopes(doc);
            for (var s = 0; s < scopes.length; s++) {
                var hit = pickIn(scopes[s]);
                if (hit) return hit;
            }
            var openSel = doc.querySelectorAll(
                '.layui-form-select.layui-form-selected, .select2-container--open, '
                + '.bootstrap-select.open'
            );
            for (var j = 0; j < openSel.length; j++) {
                hit = pickIn(openSel[j]);
                if (hit) return hit;
            }
            return pickIn(doc);
                """
            ),
            target_supplier,
        )
        if not picked:
            logger.warning(
                f"  下拉中未找到 PO 对应供应商: {target_supplier} "
                f"（已搜「{search_key}」，排除家用电梯）"
            )
            return False
        logger.info(f"  ✓ 已点选供应商（图三）: {picked}")

        if not self._blur_extend_supplier_selection(layer, target_supplier):
            return False
        return True

    @staticmethod
    def _btn_label_ok(label):
        """layui 按钮文案可能含 &nbsp;、全角空格。"""
        t = (label or "").replace("\xa0", " ").replace("\u3000", " ").strip()
        if not t or "取消" in t:
            return False
        return t == "确定" or t == "确认" or t.startswith("确定")

    def _click_confirm_save_dialog_in_context(
        self, max_wait_sec=10, poll_interval=0.35
    ):
        """
        在当前 frame（主文档或 ext_ven iframe）查找「信息 — 确认保存?」并点「确定」。
        扫描条件：同时含「确定」「取消」，且含「确认保存/信息/保存」之一。
        """
        driver = self.browser.driver
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                layers = driver.find_elements(
                    By.CSS_SELECTOR,
                    ".layui-layer-dialog, .layui-layer.layui-layer-dialog, "
                    ".layui-layer-page, .layui-layer[type='dialog'], "
                    "div.layui-layer:not(.layui-layer-iframe)",
                )
                ranked = []
                for layer in layers:
                    try:
                        if not layer.is_displayed():
                            continue
                    except Exception:
                        continue
                    body = (layer.text or "").replace("\xa0", " ")
                    if "确定" not in body or "取消" not in body:
                        continue
                    if not any(
                        k in body
                        for k in (
                            "确认导入供应商报价表",
                            "确认保存",
                            "确认",
                            "保存",
                            "信息",
                        )
                    ):
                        continue
                    try:
                        z = int(layer.value_of_css_property("z-index") or "0")
                    except Exception:
                        z = 0
                    ranked.append((z, layer))
                ranked.sort(key=lambda x: x[0], reverse=True)
                for _z, layer in ranked:
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
                                label = (btn.text or btn.get_attribute(
                                    "value"
                                ) or "").replace("\xa0", " ")
                                if not self._btn_label_ok(label):
                                    continue
                                driver.execute_script(
                                    "arguments[0].scrollIntoView({block:'center'});",
                                    btn,
                                )
                                time.sleep(0.12)
                                try:
                                    btn.click()
                                except Exception:
                                    ActionChains(driver).move_to_element(
                                        btn
                                    ).pause(0.1).click().perform()
                                return True, (label.strip() or "确定")
                            except Exception:
                                continue
            except Exception:
                pass

            hit = driver.execute_script(
                """
                function raw(el) {
                    return (el.innerText || el.textContent || el.value || '')
                        .replace(/\\s+/g, ' ').replace(/\\u00a0/g, ' ').trim();
                }
                function isVis(el) {
                    if (!el) return false;
                    var st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (parseFloat(st.opacity || '1') < 0.08) return false;
                    var r = el.getBoundingClientRect();
                    return r.width > 4 && r.height > 4;
                }
                function isConfirmSaveLayer(layer) {
                    var all = raw(layer);
                    if (all.indexOf('确定') < 0 || all.indexOf('取消') < 0) return false;
                    if (all.indexOf('确认导入供应商报价表') >= 0) return true;
                    if (all.indexOf('确认保存') >= 0) return true;
                    if (all.indexOf('信息') >= 0 && all.indexOf('保存') >= 0) return true;
                    if (all.indexOf('信息') >= 0 && all.indexOf('确定') >= 0) return true;
                    return false;
                }
                function clickOk(layer) {
                    var btns = layer.querySelectorAll(
                        '.layui-layer-btn a, a.layui-layer-btn0, .layui-layer-btn0, '
                        + '.layui-layer-btn button, button, a.btn-primary'
                    );
                    for (var j = 0; j < btns.length; j++) {
                        var btx = raw(btns[j]);
                        if (!btx || btx.indexOf('取消') >= 0) continue;
                        if (btx === '确定' || btx === '确认' || btx.indexOf('确定') === 0) {
                            try { btns[j].scrollIntoView({block:'center'}); } catch (e) {}
                            try { btns[j].click(); return btx; } catch (e2) {}
                            try {
                                var ev = {bubbles: true, cancelable: true, view: window};
                                btns[j].dispatchEvent(new MouseEvent('mousedown', ev));
                                btns[j].dispatchEvent(new MouseEvent('mouseup', ev));
                                btns[j].dispatchEvent(new MouseEvent('click', ev));
                                return btx;
                            } catch (e3) {}
                        }
                    }
                    return '';
                }
                var layers = document.querySelectorAll(
                    '.layui-layer-dialog, .layui-layer-page, .layui-layer[type="dialog"], '
                    + '.layui-layer:not(.layui-layer-iframe)'
                );
                var ranked = [];
                for (var i = 0; i < layers.length; i++) {
                    if (!isVis(layers[i]) || !isConfirmSaveLayer(layers[i])) continue;
                    var z = parseInt(window.getComputedStyle(layers[i]).zIndex, 10) || 0;
                    ranked.push({z: z, el: layers[i]});
                }
                ranked.sort(function(a, b) { return b.z - a.z; });
                for (var k = 0; k < ranked.length; k++) {
                    var hit = clickOk(ranked[k].el);
                    if (hit) return hit;
                }
                return '';
                """
            )
            if hit:
                return True, hit
        return False, ""

    def _click_extend_supplier_save_confirm(self, max_wait_sec=18, poll_interval=0.35):
        """
        保存后「信息 — 确认保存?」可能在主文档，也可能在 ext_ven.html iframe 内。
        """
        driver = self.browser.driver
        driver.switch_to.default_content()
        per_ctx = max(6, max_wait_sec // 3)
        contexts = [("主页面", None)]
        layer = self._find_extend_supplier_layer()
        if layer:
            try:
                iframe = layer.find_element(By.CSS_SELECTOR, "iframe")
                contexts.append(("ext_ven iframe", iframe))
            except Exception:
                pass

        for ctx_name, iframe_el in contexts:
            try:
                driver.switch_to.default_content()
                if iframe_el is not None:
                    driver.switch_to.frame(iframe_el)
                ok, label = self._click_confirm_save_dialog_in_context(
                    max_wait_sec=per_ctx, poll_interval=poll_interval
                )
                if ok:
                    driver.switch_to.default_content()
                    logger.info(
                        f"  ✓ 已点击「确认保存?」弹窗的「确定」({ctx_name}, {label})"
                    )
                    time.sleep(0.5)
                    return True
            except Exception as e:
                logger.debug(f"  确认保存弹窗扫描({ctx_name})异常: {e}")
            finally:
                driver.switch_to.default_content()

        try:
            debug = driver.execute_script(
                """
                var out = {main: [], iframe: []};
                function snap(doc, key) {
                    var layers = doc.querySelectorAll('.layui-layer, .modal, .bootbox');
                    for (var i = 0; i < layers.length && out[key].length < 8; i++) {
                        var el = layers[i];
                        var r = el.getBoundingClientRect();
                        if (r.width < 4) continue;
                        var tx = (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
                        if (!tx) continue;
                        out[key].push({
                            cls: (el.className || '').toString().slice(0, 80),
                            z: window.getComputedStyle(el).zIndex,
                            text: tx
                        });
                    }
                }
                snap(document, 'main');
                var iframes = document.querySelectorAll('iframe');
                for (var f = 0; f < iframes.length; f++) {
                    try {
                        var fd = iframes[f].contentDocument;
                        if (fd) snap(fd, 'iframe');
                    } catch (e) {}
                }
                return JSON.stringify(out);
                """
            )
            logger.warning(f"  未匹配到「确认保存?」弹窗，页面层摘要: {debug}")
        except Exception:
            pass
        return False

    def _click_extend_supplier_save(self):
        """
        扩展供应商弹窗内点击「保存」（ext_ven.html iframe #submit）。
        返回 (是否点到保存, 是否已在 iframe 内点到「确认保存?」→「确定」)。
        """
        driver = self.browser.driver
        if self._switch_to_extend_vendor_iframe():
            try:
                clicked = driver.execute_script(
                    """
                    var btn = document.querySelector('#submit, input#submit, button#submit');
                    if (btn) { btn.click(); return (btn.value || btn.innerText || '').trim(); }
                    var nodes = document.querySelectorAll('button, input[type=button]');
                    for (var i = 0; i < nodes.length; i++) {
                        var tx = (nodes[i].innerText || nodes[i].value || '').replace(/\\s+/g, ' ').trim();
                        if (tx.indexOf('保存') >= 0) { nodes[i].click(); return tx; }
                    }
                    return '';
                    """
                )
                if clicked:
                    logger.info("  ✓ 已点击扩展供应商「保存」(iframe #submit)")
                    time.sleep(0.4)
                    ok, _ = self._click_confirm_save_dialog_in_context(
                        max_wait_sec=8
                    )
                    driver.switch_to.default_content()
                    if ok:
                        logger.info(
                            "  ✓ 保存后已在 iframe 内点击「确认保存?」→「确定」"
                        )
                    return True, bool(ok)
            except Exception:
                pass
            finally:
                driver.switch_to.default_content()

        layer = self._find_extend_supplier_layer()
        if not layer:
            return False, False
        clicked = driver.execute_script(
            """
            var root = arguments[0];
            var labels = ['保存', 'Save'];
            function raw(el) {
                return (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
            }
            var btns = root.querySelectorAll('button, a, input[type=button], input[type=submit]');
            for (var i = 0; i < btns.length; i++) {
                var tx = raw(btns[i]);
                for (var k = 0; k < labels.length; k++) {
                    if (tx === labels[k]) {
                        btns[i].click();
                        return tx;
                    }
                }
            }
            return '';
            """,
            layer,
        )
        if clicked:
            logger.info("  ✓ 已点击扩展供应商「保存」")
            return True, False
        try:
            for el in layer.find_elements(
                By.XPATH, ".//button[contains(.,'保存')]"
            ):
                if el.is_displayed():
                    el.click()
                    logger.info("  ✓ 已点击扩展供应商「保存」(XPath)")
                    return True, False
        except Exception:
            pass
        logger.warning("  未找到扩展供应商弹窗「保存」按钮")
        return False, False

