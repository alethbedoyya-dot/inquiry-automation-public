"""
备件网「新建询价单」照片上传 — 唯一实现入口。
5 个必填视图（正视图~特写）+ 3 个选填视图（其他1~3），最多 8 张。

设计原则（避免改 A 坏 B）：
  1. 仅操作 8 个视图名：正视图、左视图、右视图、全局、特写、其他1、其他2、其他3
  2. 5 个必填始终填满（不够随机复用）；3 个选填仅当有第 6 张及以上照片时才上传
  3. popover 只认当前按钮的 aria-describedby；不认「页面上最后一个 FileUpload」
  4. 每传完一个视图必须校验该行，失败立即中止（不继续试完剩余视图再报错）
  5. 视图间 hide + 移除 body>popover；添加后仅对「上传图片」触发器 html:true 重建
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from typing import List, Optional, Sequence, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    TimeoutException,
    StaleElementReferenceException,
    ElementNotInteractableException,
)

logger = logging.getLogger(__name__)

MANDATORY_VIEW_NAMES: Tuple[str, ...] = (
    "正视图",
    "左视图",
    "右视图",
    "全局",
    "特写",
)

OPTIONAL_VIEW_NAMES: Tuple[str, ...] = (
    "其他1",
    "其他2",
    "其他3",
)

MANDATORY_COUNT = len(MANDATORY_VIEW_NAMES)       # 5
VIEW_COUNT = MANDATORY_COUNT + len(OPTIONAL_VIEW_NAMES)  # 8

# 视图间：关掉浮层并删掉 body 下孤儿 popover（不 destroy 触发器）
HIDE_AND_REMOVE_BODY_POPOVERS_JS = """
    if (window.jQuery) {
        jQuery('[data-toggle="popover"]').each(function() {
            try {
                var $el = jQuery(this);
                if ($el.data('bs.popover')) {
                    $el.popover('hide');
                }
            } catch (e) {}
        });
    }
    document.querySelectorAll('body > .popover').forEach(function(el) {
        el.remove();
    });
"""

# 上传前/视图间：只清孤儿浮层，不 destroy 页面原始 popover（避免第一条就坏掉）
PHOTO_POPOVER_CLEANUP_JS = """
    if (window.jQuery) {
        jQuery('[data-toggle="popover"]').each(function() {
            try {
                var $el = jQuery(this);
                if ($el.data('bs.popover')) {
                    $el.popover('hide');
                }
            } catch (e) {}
        });
    }
    document.querySelectorAll('body > .popover').forEach(function(el) {
        el.remove();
    });
    document.querySelectorAll('a, button, span').forEach(function(el) {
        var txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        if (txt.indexOf('上传图片') < 0) return;
        el.style.pointerEvents = 'auto';
        el.removeAttribute('disabled');
        el.removeAttribute('aria-describedby');
    });
"""

# 添加物料后：仅重建「上传图片」触发器（必须 html:true，否则弹层显示源码文本）
REINIT_UPLOAD_POPOVER_TRIGGERS_JS = """
    function uploadTriggerText(el) {
        return (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    }
    function resolvePopoverContent($el) {
        var raw = $el.attr('data-content') || '';
        if (!raw) return '';
        if (raw.charAt(0) === '#') {
            try {
                var node = document.querySelector(raw);
                if (node) return node.innerHTML || '';
            } catch (e) {}
        }
        return raw;
    }
    var n = 0;
    if (!window.jQuery) return 0;
    jQuery('[data-toggle="popover"]').each(function() {
        if (uploadTriggerText(this).indexOf('上传图片') < 0) return;
        var $el = jQuery(this);
        var content = resolvePopoverContent($el);
        var placement = $el.attr('data-placement') || 'auto';
        try {
            if ($el.data('bs.popover')) {
                $el.popover('hide');
                $el.popover('destroy');
            }
        } catch (e) {}
        $el.attr('data-html', 'true');
        try {
            $el.popover({
                html: true,
                container: 'body',
                trigger: 'click',
                placement: placement,
                content: content
            });
            n++;
        } catch (e2) {}
    });
    return n;
"""


def hide_and_remove_body_popovers(driver: WebDriver) -> None:
    """关闭 popover 并移除 body 下残留节点（单条物料 5 视图之间用）。"""
    try:
        driver.execute_script(HIDE_AND_REMOVE_BODY_POPOVERS_JS)
    except Exception:
        pass


def cleanup_photo_popover_artifacts(driver: WebDriver) -> None:
    """移除孤儿 popover、恢复上传按钮属性；不 destroy 页面已有 popover 实例。"""
    try:
        driver.execute_script(PHOTO_POPOVER_CLEANUP_JS)
    except Exception:
        pass


def reinit_upload_popover_triggers(driver: WebDriver) -> int:
    """添加物料后重建「上传图片」popover（html:true，避免弹层显示 HTML 源码）。"""
    try:
        n = driver.execute_script(REINIT_UPLOAD_POPOVER_TRIGGERS_JS)
        count = int(n or 0)
        if count:
            logger.info(f"  已重建 {count} 个「上传图片」popover（html:true）")
        return count
    except Exception:
        return 0


def deep_reset_photo_upload_popovers(driver: WebDriver) -> int:
    """添加下一条物料前：清理残留 + 仅重建上传触发器。"""
    cleanup_photo_popover_artifacts(driver)
    return reinit_upload_popover_triggers(driver)


def normalize_view_row_label(label: str) -> str:
    """首列视图名：去空白、去必填星号。"""
    return re.sub(r"\s+", "", label or "").replace("*", "")


def row_label_matches_view(row_label: str, view_name: str) -> bool:
    """首列须与视图名完全一致（禁止 contains，避免多行误匹配）。"""
    return normalize_view_row_label(row_label) == view_name


# 与 _view_row_uploaded 内 JS 规则一致；离线测试用 view_row_uploaded_from_links。
VIEW_ROW_UPLOADED_JS = """
    var view = arguments[0];
    function normLabel(s) {
        return (s || '').replace(/\\s/g, '').replace(/\\*+/g, '');
    }
    var rows = document.querySelectorAll('tr');
    for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        var cells = row.cells;
        if (!cells || !cells.length) continue;
        var label = normLabel(cells[0].innerText || cells[0].textContent || '');
        if (label !== view) continue;

        // 中间列常显示「查看 删除」（可能是 span，不一定是 <a>）
        if (cells.length > 1) {
            var mid = (cells[1].innerText || '').replace(/\\s+/g, ' ').trim();
            if (mid.indexOf('查看') >= 0 && mid.indexOf('删除') >= 0) {
                return true;
            }
        }

        var hasView = false;
        var hasDelete = false;
        var hasUploadLink = false;
        var clickables = row.querySelectorAll(
            'a, span, button, label, [onclick]'
        );
        for (var k = 0; k < clickables.length; k++) {
            var el = clickables[k];
            var r = el.getBoundingClientRect();
            if (r.width < 1 && r.height < 1) continue;
            var lt = (el.innerText || el.textContent || '')
                .replace(/\\s+/g, ' ').trim();
            if (!lt) continue;
            if (lt.indexOf('上传图片') >= 0) {
                hasUploadLink = true;
                continue;
            }
            if (lt === '查看' || lt.indexOf('查看') === 0) hasView = true;
            if (lt === '删除') hasDelete = true;
        }
        // 已上传时右侧仍保留「上传图片」，须先看「查看/删除」
        if (hasView || hasDelete) return true;
        if (hasUploadLink) return false;
        return false;
    }
    return false;
"""


def view_row_uploaded_from_links(
    view_name: str,
    row_label: str,
    link_texts: Sequence[str],
    *,
    status_cell_text: str = "",
) -> bool:
    """
    离线镜像：该行是否已实际上传照片。
    须首列含 view_name；中间列或操作区有「查看」「删除」即视为已上传；
    上传成功后右侧仍可能有「上传图片」，不能据此判未上传。
    """
    if not row_label_matches_view(row_label, view_name):
        return False
    mid = re.sub(r"\s+", " ", (status_cell_text or "").strip())
    if mid and "查看" in mid and "删除" in mid:
        return True
    texts = [
        re.sub(r"\s+", " ", (t or "").strip())
        for t in (link_texts or [])
        if (t or "").strip()
    ]
    if any(t == "查看" or t == "删除" for t in texts):
        return True
    if any("上传图片" in t for t in texts):
        return False
    return False


class PhotoUploadNeedsManualHelp(Exception):
    """照片过小、上传被拒或自动化无法完成 5 视图上传。"""

    def __init__(
        self,
        message,
        *,
        small_files=None,
        view_name=None,
        photo_path=None,
    ):
        super().__init__(message)
        self.small_files = list(small_files or [])
        self.view_name = view_name or ""
        self.photo_path = photo_path or ""


def format_upload_exc(exc: BaseException, stage: str = "") -> str:
    name = type(exc).__name__
    msg = str(exc).strip()
    if not msg:
        msg = str(getattr(exc, "msg", "") or "").strip()
    detail = f"{name}: {msg}" if msg else name
    return f"{stage} — {detail}" if stage else detail


def prepare_valid_photo_paths(
    photos: Sequence[str],
    *,
    min_bytes: int,
) -> List[str]:
    """校验路径、格式、体积；返回绝对路径列表（可能已自动增强）。"""
    from utils.image_files import (
        ensure_photos_min_bytes,
        find_photos_below_min_bytes,
        format_file_size,
        is_jpg_file,
        prepare_photo_for_upload,
    )

    valid: List[str] = []
    for p in photos or []:
        if not p or not os.path.exists(p):
            continue
        try:
            if is_jpg_file(p):
                valid.append(os.path.abspath(p))
            else:
                valid.append(prepare_photo_for_upload(p))
        except Exception as e:
            logger.warning(f"  跳过无效照片 {p}: {e}")

    if not valid:
        raise PhotoUploadNeedsManualHelp(
            "没有可上传的有效照片（路径无效或格式不支持）"
        )

    boosted = ensure_photos_min_bytes(valid, min_bytes)
    if boosted:
        logger.info(
            f"  已自动增强 {boosted} 张照片至 ≥ {format_file_size(min_bytes)}"
        )

    small = find_photos_below_min_bytes(valid, min_bytes)
    if small:
        lines = [
            f"{os.path.basename(p)} ({format_file_size(sz)})" for p, sz in small
        ]
        raise PhotoUploadNeedsManualHelp(
            f"备件网要求单张照片 ≥ {format_file_size(min_bytes)}，"
            f"以下文件过小: {', '.join(lines)}",
            small_files=small,
        )
    return valid


def assign_photos_to_views(valid_photos: Sequence[str]) -> Tuple[List[str], List[str]]:
    """
    照片分配到视图。
    5 个必填始终填满；3 个选填仅当照片 > 5 张时触发。
    返回 (照片路径列表, 对应视图名列表)。
    """
    paths = list(valid_photos)
    if not paths:
        raise ValueError("valid_photos 为空")

    def _fill(count: int, offset: int) -> List[str]:
        result: List[str] = []
        for i in range(count):
            idx = offset + i
            if idx < len(paths):
                result.append(paths[idx])
            else:
                result.append(random.choice(paths))
        return result

    assignments = _fill(MANDATORY_COUNT, offset=0)
    view_names = list(MANDATORY_VIEW_NAMES)

    if len(paths) > MANDATORY_COUNT:
        assignments.extend(_fill(len(OPTIONAL_VIEW_NAMES), offset=MANDATORY_COUNT))
        view_names.extend(OPTIONAL_VIEW_NAMES)

    return assignments, view_names


class SparepartsPhotoUploader:
    """备件网照片上传器（最多 8 视图）— 请只通过 upload_all() 调用。"""

    POPOVER_WAIT = 12
    UPLOAD_DONE_WAIT = 25

    def __init__(self, driver: WebDriver):
        self.driver = driver

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def upload_all(self, photos: Sequence[str]) -> None:
        from config import SPAREPARTS_MIN_PHOTO_BYTES
        from utils.image_files import format_file_size

        logger.info(f"  上传照片: 共 {len(photos or [])} 张 → 填充最多 {VIEW_COUNT} 个视图")
        valid = prepare_valid_photo_paths(photos, min_bytes=SPAREPARTS_MIN_PHOTO_BYTES)
        logger.info(f"  有效照片: {len(valid)} 张")

        assignments, view_names = assign_photos_to_views(valid)
        for i, view in enumerate(view_names):
            logger.info(f"    [{view}] ← {os.path.basename(assignments[i])}")

        self._prepare_page()
        for i, view_name in enumerate(view_names):
            self._upload_one_view(view_name, assignments[i])

        missing = [v for v in view_names if not self._view_row_uploaded(v)]
        if missing:
            raise PhotoUploadNeedsManualHelp(
                "以下视图仍未上传成功: " + ", ".join(missing),
                view_name=",".join(missing),
            )
        logger.info(f"  ✓ {len(view_names)} 个视图均已上传并校验通过")
        time.sleep(0.3)

    # ------------------------------------------------------------------
    # 页面准备
    # ------------------------------------------------------------------
    def _prepare_page(self) -> None:
        self._dismiss_hint_dialog()
        cleanup_photo_popover_artifacts(self.driver)
        time.sleep(0.15)

    def _dismiss_hint_dialog(self) -> None:
        try:
            closed = self.driver.execute_script(
                """
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
                    if (closeBtn) { closeBtn.click(); return true; }
                }
                return false;
                """
            )
            if closed:
                time.sleep(0.35)
        except Exception:
            pass

    def _hide_all_popovers(self) -> None:
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            body.click()
            time.sleep(0.15)
        except Exception:
            pass
        hide_and_remove_body_popovers(self.driver)
        time.sleep(0.2)

    # ------------------------------------------------------------------
    # 单视图上传（失败即抛，不继续下一个）
    # ------------------------------------------------------------------
    def _upload_one_view(self, view_name: str, photo_path: str) -> None:
        if self._view_row_uploaded(view_name):
            logger.info(f"      ✓ [{view_name}] 已有照片，跳过")
            return

        last_exc: Optional[BaseException] = None
        for attempt in (1, 2, 3):
            try:
                self._dismiss_hint_dialog()
                self._hide_all_popovers()

                upload_link = self._find_upload_link(view_name)
                if upload_link is None:
                    raise PhotoUploadNeedsManualHelp(
                        f"页面上找不到 [{view_name}] 行的「上传图片」按钮",
                        view_name=view_name,
                        photo_path=photo_path,
                    )

                pop_el = self._open_popover(upload_link)
                logger.info(f"      ✓ 已打开 [{view_name}] 上传弹窗 (第{attempt}次)")
                self._send_file_and_click_upload(pop_el, photo_path)
                logger.info(
                    f"      ✓ 已选择 {os.path.basename(photo_path)} 并点击上传"
                )
                self._hide_all_popovers()
                self._wait_view_done(view_name, photo_path)
                logger.info(f"      ✓ [{view_name}] 上传完成")
                self._hide_all_popovers()
                time.sleep(0.35)
                return
            except PhotoUploadNeedsManualHelp:
                self._hide_all_popovers()
                raise
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"      [{view_name}] 第{attempt}次尝试失败: "
                    f"{format_upload_exc(e, '上传')}"
                )
                self._hide_all_popovers()
                time.sleep(0.4)

        raise PhotoUploadNeedsManualHelp(
            f"[{view_name}] 自动上传失败（已重试 3 次）: "
            f"{format_upload_exc(last_exc or Exception('unknown'), '')}",
            view_name=view_name,
            photo_path=photo_path,
        )

    def _find_upload_link(self, view_name: str) -> Optional[WebElement]:
        """
        在首列标签与 view_name 完全一致的那一行内找「上传图片」。
        不用 contains(indexOf)：整表/多行共用 DOM 时会把第一个上传按钮当成目标。
        """
        try:
            el = self.driver.execute_script(
                """
                var view = arguments[0];
                function normLabel(s) {
                    return (s || '').replace(/\\s/g, '').replace(/\\*+/g, '');
                }
                var rows = document.querySelectorAll('tr');
                for (var i = 0; i < rows.length; i++) {
                    var row = rows[i];
                    var cells = row.cells;
                    if (!cells || !cells.length) continue;
                    var label = normLabel(cells[0].innerText || cells[0].textContent || '');
                    if (label !== view) continue;
                    var clickables = row.querySelectorAll('a, span, button, label');
                    for (var k = 0; k < clickables.length; k++) {
                        var node = clickables[k];
                        var txt = (node.innerText || node.textContent || '')
                            .replace(/\\s+/g, ' ').trim();
                        if (txt.indexOf('上传图片') < 0) continue;
                        var r = node.getBoundingClientRect();
                        if (r.width < 1 || r.height < 1) continue;
                        return node;
                    }
                }
                return null;
                """,
                view_name,
            )
            if el is not None:
                logger.debug(f"      [{view_name}] 定位到该行「上传图片」触发器")
            return el
        except Exception:
            return None

    def _open_popover(self, upload_link: WebElement) -> WebElement:
        self.driver.execute_script(
            """
            var el = arguments[0];
            el.scrollIntoView({block:'center'});
            el.style.pointerEvents = 'auto';
            el.removeAttribute('disabled');
            el.removeAttribute('aria-describedby');
            """,
            upload_link,
        )
        time.sleep(0.15)
        try:
            upload_link.click()
        except (ElementNotInteractableException, StaleElementReferenceException):
            self.driver.execute_script("arguments[0].click();", upload_link)

        def ready(_driver):
            pop = self._popover_for_link(upload_link)
            return pop is not None

        try:
            WebDriverWait(self.driver, self.POPOVER_WAIT).until(ready)
        except TimeoutException:
            self.driver.execute_script(
                """
                var el = arguments[0];
                if (window.jQuery) {
                    try { jQuery(el).popover('show'); } catch (e) {}
                    try { jQuery(el).trigger('click'); } catch (e) {}
                }
                """,
                upload_link,
            )
            WebDriverWait(self.driver, 4).until(ready)
        pop = self._popover_for_link(upload_link)
        if pop is None:
            raise TimeoutException(
                f"「上传图片」已点击，但未出现绑定 popover（无 aria-describedby 或不可见）"
            )
        return pop

    def _popover_for_link(self, upload_link: WebElement) -> Optional[WebElement]:
        """只认当前按钮的 aria-describedby；同行几何兜底。绝不使用「最后一个 popover」。"""
        pop_id = (upload_link.get_attribute("aria-describedby") or "").strip()
        if pop_id:
            try:
                pop = self.driver.find_element(By.ID, pop_id)
                if pop.is_displayed() and self._has_file_input(pop):
                    return pop
            except Exception:
                pass

        try:
            pop = self.driver.execute_script(
                """
                var link = arguments[0];
                var tr = link.closest('tr');
                if (!tr) return null;
                var rt = tr.getBoundingClientRect();
                var pops = document.querySelectorAll('body > .popover');
                for (var i = pops.length - 1; i >= 0; i--) {
                    var p = pops[i];
                    var st = window.getComputedStyle(p);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    if (!p.querySelector('input[type=file], #FileUpload')) continue;
                    var rp = p.getBoundingClientRect();
                    if (Math.abs(rt.top - rp.top) > 220) continue;
                    return p;
                }
                return null;
                """,
                upload_link,
            )
            if pop and self._has_file_input(pop):
                return pop
        except Exception:
            pass
        return None

    @staticmethod
    def _has_file_input(pop_el: WebElement) -> bool:
        try:
            if pop_el.find_elements(
                By.CSS_SELECTOR, "input[type='file'], #FileUpload"
            ):
                return True
            # deep reset 误配 html:false 时弹层只有源码文本，没有可交互控件
            raw = (pop_el.text or "").strip()
            if raw and ("<input" in raw or "<form" in raw or "UploadContentDiv" in raw):
                return False
            return False
        except Exception:
            return False

    def _send_file_and_click_upload(self, pop_el: WebElement, photo_path: str) -> None:
        abs_path = os.path.normpath(os.path.abspath(photo_path))
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(abs_path)

        file_input = pop_el.find_element(
            By.CSS_SELECTOR, "input[type='file'], #FileUpload"
        )
        self.driver.execute_script(
            """
            var inp = arguments[0];
            inp.removeAttribute('disabled');
            inp.style.setProperty('display', 'block', 'important');
            inp.style.setProperty('visibility', 'visible', 'important');
            inp.style.setProperty('opacity', '1', 'important');
            inp.style.setProperty('position', 'relative', 'important');
            inp.style.width = '1px';
            inp.style.height = '1px';
            """,
            file_input,
        )
        file_input.send_keys(abs_path)
        self.driver.execute_script(
            """
            var inp = arguments[0];
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            """,
            file_input,
        )
        if not (file_input.get_attribute("value") or "").strip():
            raise RuntimeError(
                f"文件未绑定到选择框: {os.path.basename(abs_path)}"
            )
        time.sleep(0.3)

        upload_btn = None
        for sel in (
            "#btnUpload",
            'input[type="button"][value*="上传"]',
            'input[type="submit"][value*="上传"]',
        ):
            found = pop_el.find_elements(By.CSS_SELECTOR, sel)
            if found:
                upload_btn = found[0]
                break
        if upload_btn is None:
            raise RuntimeError("popover 内未找到「上传」按钮")

        self.driver.execute_script(
            """
            var btn = arguments[0];
            btn.removeAttribute('disabled');
            btn.style.pointerEvents = 'auto';
            try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
            """,
            upload_btn,
        )
        clicked = False
        try:
            upload_btn.click()
            clicked = True
        except (
            ElementNotInteractableException,
            StaleElementReferenceException,
        ):
            pass
        if not clicked:
            self.driver.execute_script(
                """
                var btn = arguments[0];
                btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                btn.click();
                if (window.jQuery) {
                    try { jQuery(btn).trigger('click'); } catch (e) {}
                }
                """,
                upload_btn,
            )

        time.sleep(0.55)

    def _wait_view_done(self, view_name: str, photo_path: str) -> None:
        basename = os.path.basename(photo_path)
        deadline = time.time() + self.UPLOAD_DONE_WAIT
        while time.time() < deadline:
            if self._view_row_uploaded(view_name):
                return
            if self._page_reports_upload_error():
                raise PhotoUploadNeedsManualHelp(
                    f"[{view_name}] 备件网提示上传失败（{basename}）",
                    view_name=view_name,
                    photo_path=photo_path,
                )
            time.sleep(0.35)

        raise TimeoutException(
            f"[{view_name}] 点击上传后 {self.UPLOAD_DONE_WAIT}s 内"
            f"行内未出现「查看」或「删除」（文件 {basename}）"
        )

    def _view_row_uploaded(self, view_name: str) -> bool:
        try:
            return bool(
                self.driver.execute_script(VIEW_ROW_UPLOADED_JS, view_name)
            )
        except Exception:
            return False

    def _missing_mandatory_views(self) -> List[str]:
        return [v for v in MANDATORY_VIEW_NAMES if not self._view_row_uploaded(v)]

    def _page_reports_upload_error(self) -> bool:
        try:
            return bool(
                self.driver.execute_script(
                    """
                    var chunks = [];
                    var nodes = document.querySelectorAll(
                        '.popover-content, .UploadContentDiv, .popover, '
                        + '.layui-layer-content, [class*="error"], [class*="alert"]'
                    );
                    for (var i = 0; i < nodes.length; i++) {
                        if (!nodes[i]) continue;
                        var r = nodes[i].getBoundingClientRect();
                        if (r.width < 2 || r.height < 2) continue;
                        chunks.push(nodes[i].innerText || '');
                    }
                    var text = chunks.join(' ').replace(/\\s+/g, ' ');
                    var pats = [
                        /100\\s*k/i, /100kb/i, /小于\\s*100/,
                        /文件.{0,8}(过小|太小|小于)/i,
                        /不允许上传/, /上传失败/, /请重新上传/
                    ];
                    for (var j = 0; j < pats.length; j++) {
                        if (pats[j].test(text)) return true;
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False
