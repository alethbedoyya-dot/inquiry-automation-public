"""
OMS订单管理系统操作模块
负责：登录、筛选待处理单据、提取数据
"""
import time
import logging
import re
import os
import json
import shutil
import hashlib
import urllib.request
import urllib.parse
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from config import (
    OMS_HOME_URL, OMS_URL, PAGE_LOAD_TIMEOUT, ELEMENT_WAIT_TIMEOUT,
    FINALIZE_FILTER_BY_SOURCING_NO, OMS_SOURCING_COLUMN,
    OMS_ATTACHMENTS_CACHE_DIR, OMS_ATTACHMENT_HEADER_KEYWORDS,
    OMS_ATTACHMENT_DOWNLOAD_TIMEOUT, OMS_ATTACHMENT_IMAGE_EXTENSIONS,
    OMS_MAX_ATTACHMENTS_PER_ROW, OMS_ATTACHMENT_MIN_FILE_BYTES,
    OMS_SUPPLIER_SHANGHAI, OMS_SUPPLIER_ZHONGSHAN,
    OMS_SUPPLIER_EXTEND_SEARCH_KEY,
    OMS_GROUP_MATCH_MIN_LEN,
)
from utils.oms_grouping import (
    collect_group_match_needles,
    group_oms_rows,
    parse_anonymous_group_key,
    resolve_group_oms_email_filter,
)
from utils.browser import Browser
from utils.downloads import wait_for_new_image_download
from utils.image_files import finalize_downloaded_attachment, pick_distinct_photo_files
from modules.oms_finalize import OMSFinalizeMixin
from modules.oms_operations import OMSAttachmentsMixin, OMSSupplierOpsMixin

logger = logging.getLogger(__name__)

_OMS_COLUMN_FILTER_ALIASES = {
    "项目名称": ("项目名称",),
    "寻源单号": ("寻源单号", "寻源", OMS_SOURCING_COLUMN),
    "系统询价号": ("系统询价号", "系统询价价号", "系统查询价号"),
    "直发地址": ("直发地址",),
    "梯号": ("梯号", "WBS梯号"),
}


class OMSModule(OMSAttachmentsMixin, OMSSupplierOpsMixin, OMSFinalizeMixin):
    """
    OMS订单管理系统操作模块
    
    流程：
    1. 先访问首页让登录态生效（复用Edge cookie）
    2. 跳转到PSM筛选直链
    3. 筛选状态为"待处理"
    4. Sourcing8ID 条件输入 OMS 账号（OMS_USERNAME）
    （阶段一不再按 Sourcing 关键字筛选；阶段三按项目名筛选后仍会筛 Sourcing）
    6. 提取待处理数据，按梯号分组
    """

    SELECTORS = {
        # 筛选区域 — Bootstrap tooltip 点击展开
        # 点击表头 <a class="tooltip-show tooltip_xxx"> 触发 tooltip，
        # tooltip 内嵌表单，包含 Bootstrap Select（selectpicker）和搜索按钮。
        "status_filter_anchor": (By.CSS_SELECTOR, "a[class*='tooltip_table_state']"),
        # 主表为 tooltip_table_sourcing；dTable 为另一套表（常不可见）
        "sourcing_filter_anchor": (By.CSS_SELECTOR,
            "a.tooltip-show.tooltip_table_sourcing, "
            "a[class*='tooltip_table_sourcing']:not([class*='sourcing_id'])"),
        
        # tooltip 出现后，表单元素有固定 ID
        "status_operator_id": "operator_table_state",
        "status_value_id": "seaValue_table_state",
        "sourcing_operator_id": "operator_table_sourcing",
        "sourcing_value_id": "seaValue_table_sourcing",
        
        # tooltip 内的搜索按钮: <button>搜索</button>
        "tooltip_search_btn": (By.XPATH,
            "//div[contains(@class,'tooltip') and contains(@style,'display') and not(contains(@style,'none'))]"
            "//form//button[contains(text(),'搜索')]"),
        
        # PSM操作按钮和菜单（表格上方工具栏）
        "psm_operation_btn": (By.XPATH,
            "//button[contains(text(),'PSM操作')] | //a[contains(text(),'PSM操作')]"),
        "send_email_menu_item": (By.XPATH,
            "//div[contains(@class,'dropdown-menu')]"
            "//a[contains(.,'发送供应商报价邮件') and contains(.,'附件')]"
        ),
        "confirm_btn": (By.XPATH,
            "//button[contains(text(),'确定')] | //button[contains(@class,'confirm')]"
            " | //*[contains(@class,'modal')]//button[contains(text(),'确定')]"),
        
        # 结果列表
        "result_table": (By.XPATH, "//table[contains(@class,'table')] | //table[@id='resultTable']"),
        "table_rows": (By.XPATH, ".//tbody/tr"),
        
        # 分页
        "next_page_btn": (By.XPATH, "//a[contains(text(),'下一页')] | //li[contains(@class,'next')]/a"),
    }

    def __init__(self, browser: Browser, user_config: dict):
        self.browser = browser
        self.sourcing_keyword = user_config.get("SOURCING_KEYWORD", "")
        self.oms_username = user_config.get("OMS_USERNAME", "")
        self.oms_password = user_config.get("OMS_PASSWORD", "")

    def _is_on_login_page(self):
        """检测当前是否在OMS登录页面"""
        current_url = self.browser.driver.current_url
        if "/login" in current_url.lower():
            return True
        # 检查页面源码中是否有登录表单特征（不限于前500字符）
        try:
            page_source = self.browser.driver.page_source
            if "登录" in page_source[:2000] and ("password" in page_source[:2000].lower() or "密码" in page_source[:2000]):
                return True
        except Exception:
            pass
        return False

    def _is_logged_in(self):
        """检测是否已成功登录OMS（当前页面不是登录页即为已登录）"""
        try:
            return not self._is_on_login_page()
        except Exception:
            return False

    def open_and_login(self):
        """
        打开OMS：
        先访问首页让登录态生效（复用Edge cookie），
        如果跳转到登录页则自动填账号密码登录，
        登录成功后再跳转到PSM直链。
        """
        logger.info("打开OMS系统...")
        
        # 先访问首页 — 如果Edge已登录，会自动携带cookie
        self.browser.driver.get(OMS_HOME_URL)
        time.sleep(4)
        
        current_url = self.browser.driver.current_url
        logger.info(f"当前URL: {current_url}")
        
        if self._is_on_login_page():
            logger.info("需要登录OMS...")
            
            # 尝试自动登录
            auto_logged_in = False
            if self.oms_username and self.oms_password:
                try:
                    username_inputs = self.browser.driver.find_elements(
                        By.XPATH, "//input[@type='text' or @type='email' or @name='username']")
                    password_inputs = self.browser.driver.find_elements(
                        By.XPATH, "//input[@type='password']")
                    
                    if username_inputs and password_inputs:
                        username_inputs[0].clear()
                        username_inputs[0].send_keys(self.oms_username)
                        password_inputs[0].clear()
                        password_inputs[0].send_keys(self.oms_password)
                        password_inputs[0].send_keys(Keys.ENTER)
                        time.sleep(5)
                        
                        # 验证登录是否成功
                        if self._is_logged_in():
                            logger.info("  ✓ OMS自动登录成功")
                            auto_logged_in = True
                        else:
                            logger.warning("  ⚠ 自动登录未成功（账号密码可能不正确），请手动登录")
                    else:
                        logger.warning("  ⚠ 未找到登录表单，请手动登录")
                except Exception as e:
                    logger.warning(f"  ⚠ 自动登录异常: {e}")
            
            if not auto_logged_in:
                if not self.oms_username or not self.oms_password:
                    logger.warning("  ⚠ OMS密码未配置，请在 user_config.py 填写 OMS_PASSWORD")
                logger.warning("  >>> 请手动在浏览器中登录OMS，然后回到终端按Enter继续...")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    logger.warning("  (非交互模式，等待10秒后继续...)")
                    time.sleep(10)
                time.sleep(2)
                
                # 手动登录后验证
                if not self._is_logged_in():
                    logger.warning("  ⚠ 似乎仍未登录，继续尝试...")
        else:
            logger.info("  ✓ OMS已登录（使用Edge缓存的登录态）")
        
        # 跳转到PSM直链
        logger.info("导航到PSM筛选页面...")
        self.browser.driver.get(OMS_URL)
        time.sleep(4)
        logger.info("OMS系统已就绪")

    def _click_tooltip_anchor(self, anchor_selector, column_name=None):
        """
        点击表头tooltip锚点，等待tooltip出现
        如果CSS选择器失败且提供了column_name，则按列名在<th>中查找
        """
        try:
            anchor = self.browser.wait_for_clickable(*anchor_selector, timeout=ELEMENT_WAIT_TIMEOUT)
            anchor.click()
            time.sleep(0.8)
            return True
        except Exception as e:
            logger.warning(f"  ⚠ 点击tooltip锚点失败 ({anchor_selector}): {e}")
            # 如果提供了列名，按列名查找
            if column_name:
                return self._find_tooltip_anchor_by_column_name(column_name)
            # 没有列名时，打印页面上所有tooltip class帮助调试
            try:
                result = self.browser.driver.execute_script("""
                    var links = document.querySelectorAll('a[class*="tooltip"]');
                    var found = [];
                    for (var i = 0; i < links.length; i++) {
                        found.push(links[i].className + ' visible=' + (links[i].offsetParent !== null));
                    }
                    return JSON.stringify(found);
                """)
                logger.info(f"  页面上所有tooltip class: {result}")
            except Exception as e2:
                logger.warning(f"  JS查找失败: {e2}")
            return False

    def _find_tooltip_anchor_by_column_name(self, column_name):
        """
        根据列名在<th>中精确查找tooltip锚点（textContent.trim() === column_name）
        先精确匹配，找不到再用包含匹配（排除末尾带_id的列）
        """
        result = self.browser.driver.execute_script(f"""
            var ths = document.querySelectorAll('th');
            var name = '{column_name}';
            // 第一遍：精确匹配
            for (var i = 0; i < ths.length; i++) {{
                if (ths[i].textContent.trim() === name) {{
                    var links = ths[i].querySelectorAll('a[class*="tooltip"]');
                    if (links.length > 0) {{
                        links[0].click();
                        return 'exact th[' + i + ']: ' + links[0].className;
                    }}
                }}
            }}
            // 第二遍：包含匹配但排除 ID 后缀
            for (var i = 0; i < ths.length; i++) {{
                var txt = ths[i].textContent.trim();
                if (txt.indexOf(name) > -1 && txt.indexOf(name + ' ID') === -1 && txt.indexOf(name + '_ID') === -1) {{
                    var links = ths[i].querySelectorAll('a[class*="tooltip"]');
                    if (links.length > 0) {{
                        links[0].click();
                        return 'contains th[' + i + ']: ' + links[0].className;
                    }}
                }}
            }}
            return 'not_found';
        """)
        logger.info(f"  按列名'{column_name}'查找: {result}")
        time.sleep(0.8)
        return 'th[' in str(result) and 'not_found' not in str(result)

    def _click_tooltip_anchor_by_column_name(self, column_name):
        """按列名点击表头漏斗（apply_project_name_filter 使用）。"""
        return self._find_tooltip_anchor_by_column_name(column_name)

    def _click_tooltip_search(self):
        """点击tooltip内的「搜索」按钮"""
        # 用JS直接操作，避免选择器问题
        result = self.browser.driver.execute_script("""
            var tooltips = document.querySelectorAll('.tooltip');
            for (var i = 0; i < tooltips.length; i++) {
                var t = tooltips[i];
                if (t.offsetParent !== null || t.style.display !== 'none') {
                    var btns = t.querySelectorAll('button');
                    for (var j = 0; j < btns.length; j++) {
                        if (btns[j].textContent.indexOf('搜索') > -1) {
                            btns[j].click();
                            return true;
                        }
                    }
                }
            }
            return false;
        """)
        if result:
            logger.info("  ✓ 已点击工具提示搜索")
            time.sleep(3)
        else:
            logger.warning("  未找到工具提示搜索按钮")
            time.sleep(1)

    def apply_filters(
        self,
        status_pending=True,
        status_sent=False,
        sourcing_filter=False,
        supplier_id=None,
    ):
        """
        应用筛选条件：
        1. 状态=待处理 或 已发送（二选一；阶段三可两者都跳过）
        2. Sourcing=关键字（可选；阶段一默认关闭）
        3. Sourcing8ID=OMS 账号
        4. 供应商ID=指定值（可选；阶段一可配置多轮）
        """
        logger.info("应用筛选条件...")
        
        # ================================================================
        # 1) 状态筛选
        # ================================================================
        if status_sent:
            self._apply_status_sent_filter()
        elif status_pending:
            self._apply_status_pending_filter()
        else:
            logger.info("  跳过状态列筛选")

        if sourcing_filter:
            # ================================================================
            # 2) 筛选Sourcing = 用户关键字
            # ================================================================
            self._apply_sourcing_keyword_filter()
        else:
            logger.info("  跳过 Sourcing 关键字筛选")

        # ================================================================
        # 3) 筛选 Sourcing8ID = OMS 账号
        # ================================================================
        self._apply_sourcing8id_filter()

        # ================================================================
        # 4) 筛选供应商ID = 指定值（可选；阶段一两轮用）
        # ================================================================
        if supplier_id:
            self._apply_supplier_id_filter(supplier_id)

    def _set_status_filter_value(self, status_label):
        """
        在状态列漏斗 tooltip 内选中指定状态（待处理 / 已发送）。
        #seaValue_table_state 为多选 selectpicker；须写原生 option + refresh，不能误设 value=0。
        返回是否已目视可验证地选中（非「没有选中任何项」）。
        """
        logger.info(f"  点击状态列表头漏斗（{status_label}）…")
        if not self._click_tooltip_anchor(self.SELECTORS["status_filter_anchor"]):
            logger.warning("  未能打开状态列筛选 tooltip")
            return False
        time.sleep(0.5)

        driver = self.browser.driver
        label = (status_label or "").strip()

        # ① 原生 select + selectpicker（优先，适配多选）
        result = driver.execute_script(
            """
            var want = arguments[0];
            function visibleTooltip() {
                var tooltips = document.querySelectorAll('.tooltip');
                for (var i = 0; i < tooltips.length; i++) {
                    var t = tooltips[i];
                    var st = window.getComputedStyle(t);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    var r = t.getBoundingClientRect();
                    if (r.width > 8 && r.height > 8) return t;
                }
                return null;
            }
            function readValueBtnText(t) {
                var bsBtns = t.querySelectorAll('.bootstrap-select button.dropdown-toggle');
                var valueBtn = bsBtns.length >= 2 ? bsBtns[1] : (bsBtns[0] || null);
                return valueBtn ? (valueBtn.innerText || valueBtn.textContent || '').trim() : '';
            }
            function verify(t) {
                var tx = readValueBtnText(t);
                if (!tx || tx.indexOf('没有选中任何项') >= 0) return false;
                return tx.indexOf(want) >= 0;
            }
            var t = visibleTooltip();
            if (!t) return {ok: false, step: 'no_tooltip'};
            var sel = t.querySelector('#seaValue_table_state');
            if (!sel) return {ok: false, step: 'no_select'};
            var matchedVals = [];
            for (var i = 0; i < sel.options.length; i++) {
                sel.options[i].selected = false;
            }
            for (var i = 0; i < sel.options.length; i++) {
                var opt = sel.options[i];
                var txt = (opt.textContent || opt.innerText || '').trim();
                if (txt.indexOf(want) >= 0) {
                    opt.selected = true;
                    matchedVals.push(opt.value);
                }
            }
            if (!matchedVals.length) return {ok: false, step: 'no_option'};
            try {
                if (typeof jQuery !== 'undefined' && jQuery(sel).selectpicker) {
                    jQuery(sel).selectpicker('val', matchedVals);
                    jQuery(sel).selectpicker('refresh');
                }
            } catch (e) {}
            sel.dispatchEvent(new Event('change', {bubbles: true}));
            if (verify(t)) {
                return {ok: true, step: 'native', text: readValueBtnText(t)};
            }
            return {ok: false, step: 'native_unverified', text: readValueBtnText(t)};
            """,
            label,
        )
        if result and result.get("ok"):
            logger.info(f"  ✓ 状态已选中「{label}」（{result.get('text', '')[:40]}）")
            self._click_tooltip_search()
            return True

        logger.info(
            f"    原生 select 未验证成功 ({result})，尝试点击下拉项…"
        )

        # ② 展开值下拉，点击 li（兼容部分页面未刷新 selectpicker 的情况）
        driver.execute_script(
            """
            var tooltips = document.querySelectorAll('.tooltip');
            for (var i = 0; i < tooltips.length; i++) {
                var t = tooltips[i];
                var st = window.getComputedStyle(t);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                var bsBtns = t.querySelectorAll('.bootstrap-select button.dropdown-toggle');
                var valueBtn = bsBtns.length >= 2 ? bsBtns[1] : bsBtns[0];
                if (valueBtn) valueBtn.click();
            }
            """
        )
        time.sleep(0.8)
        clicked = driver.execute_script(
            """
            var want = arguments[0];
            var menus = document.querySelectorAll('.bootstrap-select.open .dropdown-menu');
            for (var i = 0; i < menus.length; i++) {
                var items = menus[i].querySelectorAll('li a, li span.text, li');
                for (var j = 0; j < items.length; j++) {
                    var tx = (items[j].textContent || '').trim();
                    if (tx.indexOf(want) >= 0) {
                        items[j].click();
                        return true;
                    }
                }
            }
            return false;
            """,
            label,
        )
        time.sleep(0.5)
        verified = driver.execute_script(
            """
            var want = arguments[0];
            var tooltips = document.querySelectorAll('.tooltip');
            for (var i = 0; i < tooltips.length; i++) {
                var t = tooltips[i];
                var st = window.getComputedStyle(t);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                var bsBtns = t.querySelectorAll('.bootstrap-select button.dropdown-toggle');
                var valueBtn = bsBtns.length >= 2 ? bsBtns[1] : bsBtns[0];
                var tx = valueBtn ? (valueBtn.innerText || valueBtn.textContent || '').trim() : '';
                if (tx && tx.indexOf('没有选中任何项') < 0 && tx.indexOf(want) >= 0) return tx;
            }
            return '';
            """,
            label,
        )
        if clicked and verified:
            logger.info(f"  ✓ 状态已选中「{label}」（下拉点击: {verified[:40]}）")
            self._click_tooltip_search()
            return True

        logger.error(
            f"  ✗ 状态「{label}」未真正选中（界面上可能仍显示「没有选中任何项」）；"
            f" 请勿点搜索，否则列表会为空"
        )
        return False

    def _apply_status_pending_filter(self):
        """筛选状态=待处理。"""
        try:
            if self._set_status_filter_value("待处理"):
                logger.info("  ✓ 状态筛选完成（待处理）")
            else:
                logger.warning("  状态=待处理 未选中，跳过后续搜索")
        except Exception as e:
            logger.warning(f"  状态筛选失败: {e}，尝试跳过")

    def _apply_status_sent_filter(self):
        """筛选状态=已发送（阶段一完成后行状态多为已发送）。"""
        try:
            return self._set_status_filter_value("已发送")
        except Exception as e:
            logger.warning(f"  已发送状态筛选失败: {e}")
            return False

    def _apply_sourcing_keyword_filter(self):
        """仅 Sourcing 关键字筛选。"""
        if not self.sourcing_keyword:
            logger.error("  ❌ 未配置 SOURCING_KEYWORD！请在 user_config.py 中填写你的名字关键字")
            raise ValueError("SOURCING_KEYWORD 不能为空，请在 user_config.py 中配置")
        
        try:
            logger.info(f"  点击Sourcing列表头漏斗...")
            click_ok = self._find_tooltip_anchor_by_column_name("Sourcing")
            if not click_ok:
                click_ok = self._click_tooltip_anchor(
                    self.SELECTORS["sourcing_filter_anchor"], column_name="Sourcing"
                )
            if not click_ok:
                logger.warning("  ⚠ 无法打开Sourcing筛选tooltip，跳过Sourcing筛选")
                return
            
            # 用JS在tooltip中找到Sourcing输入框并填入关键字
            sourcing_ok = self.browser.driver.execute_script(f"""
                var tooltips = document.querySelectorAll('.tooltip');
                for (var i = 0; i < tooltips.length; i++) {{
                    var t = tooltips[i];
                    if (t.offsetParent !== null || t.style.display !== 'none') {{
                        // 找 textarea 或 input text
                        var inp = t.querySelector('textarea');
                        if (!inp) {{
                            var inputs = t.querySelectorAll('input');
                            for (var j = 0; j < inputs.length; j++) {{
                                if (inputs[j].type === 'text' || inputs[j].type === '' || !inputs[j].type) {{
                                    if (inputs[j].offsetParent !== null) {{
                                        inp = inputs[j];
                                        break;
                                    }}
                                }}
                            }}
                        }}
                        if (inp) {{
                            inp.focus();
                            inp.value = '{self.sourcing_keyword}';
                            var evt = new Event('input', {{bubbles: true}});
                            inp.dispatchEvent(evt);
                            return 'input_set';
                        }}
                    }}
                }}
                return 'not_found';
            """)
            
            if sourcing_ok == 'input_set':
                logger.info(f"  ✓ Sourcing设置为'{self.sourcing_keyword}'")
            else:
                logger.warning(f"  未找到Sourcing输入框，尝试备用方案")
                # 备用：直接用JS搜索页面上所有可见input/textbox
                self.browser.driver.execute_script(f"""
                    var tooltips = document.querySelectorAll('.tooltip');
                    for (var i = 0; i < tooltips.length; i++) {{
                        var t = tooltips[i];
                        if (t.offsetParent !== null) {{
                            var all = t.querySelectorAll('input, textarea');
                            for (var j = 0; j < all.length; j++) {{
                                if (all[j].type !== 'hidden' && all[j].offsetParent !== null) {{
                                    all[j].value = '{self.sourcing_keyword}';
                                    return;
                                }}
                            }}
                        }}
                    }}
                """)
            
            self._click_tooltip_search()
            logger.info("  ✓ Sourcing筛选完成")
            
        except Exception as e:
            logger.warning(f"  Sourcing筛选失败: {e}")

    def _apply_sourcing8id_filter(self):
        """按 Sourcing8ID 列筛选，值为 user_config 中的 OMS_USERNAME。"""
        account = (self.oms_username or "").strip()
        if not account:
            logger.warning("  ⚠ 未配置 OMS_USERNAME，跳过 Sourcing8ID 筛选")
            return
        try:
            logger.info(f"  点击 Sourcing8ID 列表头漏斗（账号={account}）...")
            if not self._click_tooltip_anchor_by_column_name("Sourcing8ID"):
                logger.warning("  ⚠ 未找到 Sourcing8ID 列筛选漏斗，跳过 Sourcing8ID 筛选")
                return
            if not self._fill_active_tooltip_and_search(account):
                logger.warning("  Sourcing8ID tooltip 内未找到可填写的输入框")
                return
            logger.info(f"  ✓ Sourcing8ID 筛选完成（{account}）")
        except Exception as e:
            logger.warning(f"  Sourcing8ID 筛选失败: {e}")

    def _apply_supplier_id_filter(self, supplier_id):
        """按供应商ID列筛选，值为传入的 supplier_id。"""
        sid = (supplier_id or "").strip()
        if not sid:
            logger.warning("  ⚠ 未提供供应商ID，跳过供应商ID筛选")
            return
        try:
            logger.info(f"  点击供应商ID列表头漏斗（{sid}）...")
            if not self._click_tooltip_anchor_by_column_name("供应商ID"):
                logger.warning("  ⚠ 未找到供应商ID列筛选漏斗，跳过供应商ID筛选")
                return
            if not self._fill_active_tooltip_and_search(sid):
                logger.warning("  供应商ID tooltip 内未找到可填写的输入框")
                return
            logger.info(f"  ✓ 供应商ID筛选完成（{sid}）")
        except Exception as e:
            logger.warning(f"  供应商ID筛选失败: {e}")

    @staticmethod
    def _normalize_header_name(name):
        return re.sub(r"\s+", "", str(name or "").strip())

    def _header_column_index(self, headers, *canonical_names):
        """在表头数组中找列下标（精确匹配优先，避免列错位读到数量「1」等）。"""
        targets = {self._normalize_header_name(n) for n in canonical_names}
        for i, h in enumerate(headers or []):
            hn = self._normalize_header_name(h)
            if hn in targets:
                return i
        for i, h in enumerate(headers or []):
            hn = str(h or "").strip()
            if hn in canonical_names:
                return i
        return None

    @staticmethod
    def _cell_text(row_cells, index):
        if index is None or index < 0 or index >= len(row_cells):
            return ""
        return str(row_cells[index] or "").strip()

    @staticmethod
    def _is_plausible_sourcing_no(value):
        """排除数量 1、行号等误读为寻源单号的情况。"""
        s = str(value or "").strip()
        if not s:
            return False
        if re.fullmatch(r"\d{1,2}(\.0+)?", s):
            return False
        if re.match(r"^PSM[\w\-]*", s, re.I):
            return True
        if re.match(r"^CSSL[\w\-/]*", s, re.I):
            return True
        return len(s) >= 5 and re.search(r"[A-Za-z]", s)

    def _build_row_data(self, headers, row_cells, row_meta=None):
        """按表头名称对齐单元格，关键列单独映射。"""
        row_data = {}
        for i, txt in enumerate(row_cells):
            if not txt:
                continue
            row_data[f"col_{i}"] = str(txt).strip()
            if headers and i < len(headers):
                h = str(headers[i] or "").strip()
                if h:
                    row_data[h] = str(txt).strip()

        sn_idx = self._header_column_index(headers, OMS_SOURCING_COLUMN, "寻源单号")
        sn = self._cell_text(row_cells, sn_idx)
        if sn:
            row_data[OMS_SOURCING_COLUMN] = sn
            row_data["oms_sourcing_no"] = sn

        mfg_idx = self._header_column_index(headers, "工厂料号", "mfg_material")
        mfg = self._cell_text(row_cells, mfg_idx)
        if mfg:
            row_data["工厂料号"] = mfg
            row_data["mfg_material_no"] = mfg

        if row_meta and row_meta.get("has_attachment"):
            row_data["has_attachment"] = True

        return row_data

    @staticmethod

    def _resolve_sourcing_no(self, item):
        """从一行 OMS 数据中取寻源单号（优先表头「寻源单号」列，过滤误读）。"""
        candidates = []
        for key in (OMS_SOURCING_COLUMN, "寻源单号", "oms_sourcing_no"):
            v = (item.get(key) or "").strip()
            if v:
                candidates.append(v)
        for k, v in item.items():
            if k.startswith("col_"):
                continue
            sk = str(k).strip()
            sv = str(v or "").strip()
            if not sv:
                continue
            if sk == OMS_SOURCING_COLUMN or sk == "寻源单号":
                candidates.insert(0, sv)
            elif (
                "寻源" in sk
                and "单号" in sk
                and "项" not in sk
                and "次数" not in sk
                and "ID" not in sk.upper()
            ):
                candidates.append(sv)
        for c in candidates:
            if self._is_plausible_sourcing_no(c):
                return c
        return ""

    def extract_all_data(self):
        """从搜索结果表格提取所有行数据（含翻页），纯JS提取避免选择器/文本问题"""
        all_data = []
        page = 1
        
        while True:
            logger.info(f"  读取第 {page} 页数据...")
            try:
                # 先等待页面稳定（表格数据加载完成）
                time.sleep(1.5)
                
                # 纯JS提取：找页面主数据表格（跳过表头行，提取tbody/tr/td）
                js_result = self.browser.driver.execute_script("""
                    // 找到所有可见的 table，选行数最多的（数据表格）
                    var tables = document.querySelectorAll('table');
                    var bestTable = null;
                    var bestRows = 0;
                    for (var i = 0; i < tables.length; i++) {
                        if (tables[i].offsetParent !== null) {
                            var trs = tables[i].querySelectorAll('tbody tr, tr');
                            var dataRows = 0;
                            for (var j = 0; j < trs.length; j++) {
                                var tds = trs[j].querySelectorAll('td');
                                if (tds.length >= 3) {
                                    var hasText = false;
                                    for (var k = 0; k < tds.length; k++) {
                                        if (tds[k].textContent.trim()) { hasText = true; break; }
                                    }
                                    if (hasText) dataRows++;
                                }
                            }
                            if (dataRows > bestRows) {
                                bestRows = dataRows;
                                bestTable = tables[i];
                            }
                        }
                    }
                    
                    if (!bestTable) return JSON.stringify({found: false, reason: 'no_table'});
                    
                    // 提取表头
                    var headers = [];
                    var ths = bestTable.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td');
                    for (var h = 0; h < ths.length; h++) {
                        headers.push(ths[h].textContent.trim());
                    }
                    
                    // 提取数据行（跳过纯th的行）
                    var attachKw = ['附件','实物照片','实物图','实物','照片','图片'];
                    var rows = [];
                    var trs = bestTable.querySelectorAll('tbody tr, tr');
                    for (var r = 0; r < trs.length; r++) {
                        var tds = trs[r].querySelectorAll('td');
                        if (tds.length >= 3) {
                            var rowData = [];
                            for (var c = 0; c < tds.length; c++) {
                                rowData.push(tds[c].textContent.trim());
                            }
                            // 只保留有实际内容的行
                            var hasContent = false;
                            for (var c = 0; c < rowData.length; c++) {
                                if (rowData[c]) { hasContent = true; break; }
                            }
                            if (!hasContent) continue;
                            var hasAttachment = false;
                            for (var c = 0; c < tds.length; c++) {
                                var h = (headers[c] || '').replace(/\\s+/g, '');
                                var matchH = false;
                                for (var ak = 0; ak < attachKw.length; ak++) {
                                    if (h.indexOf(attachKw[ak]) >= 0) { matchH = true; break; }
                                }
                                if (!matchH) continue;
                                var cell = tds[c];
                                var txt = (cell.textContent || '').trim();
                                if (/^(有|是|Y|Yes|1)$/i.test(txt)) hasAttachment = true;
                                if (cell.querySelector('a, img, i, button, [onclick]')) {
                                    hasAttachment = true;
                                }
                            }
                            rows.push({cells: rowData, has_attachment: hasAttachment});
                        }
                    }
                    
                    return JSON.stringify({found: true, headers: headers, rows: rows, totalRows: rows.length});
                """)
                
                result = json.loads(js_result)
                
                if not result.get('found'):
                    logger.warning(f"  JS未找到数据表格: {result.get('reason', 'unknown')}")
                    break
                
                rows_data = result.get('rows', [])
                headers = result.get('headers', [])
                logger.info(f"  第{page}页: 表头{len(headers)}列, 数据{len(rows_data)}行")
                
                for row_entry in rows_data:
                    if isinstance(row_entry, dict):
                        row_cells = row_entry.get("cells") or []
                        row_meta = {
                            "has_attachment": bool(row_entry.get("has_attachment")),
                        }
                    else:
                        row_cells = row_entry
                        row_meta = None
                    row_data = self._build_row_data(headers, row_cells, row_meta)
                    if row_data:
                        sn = row_data.get(OMS_SOURCING_COLUMN) or row_data.get(
                            "oms_sourcing_no", ""
                        )
                        if sn:
                            logger.info(f"    寻源单号: {sn}")
                        all_data.append(row_data)
                        
            except Exception as e:
                logger.warning(f"  读取第 {page} 页失败: {e}")
                break
            
            # 翻页
            try:
                next_btn = self.browser.wait_for_clickable(
                    *self.SELECTORS["next_page_btn"], timeout=3
                )
                if next_btn.is_enabled() and next_btn.is_displayed():
                    next_btn.click()
                    time.sleep(2)
                    page += 1
                else:
                    break
            except:
                break
        
        logger.info(f"  共提取 {len(all_data)} 条数据")
        return all_data

    def group_by_ladder(self, data):
        """按梯号分组（兼容旧逻辑；阶段一请用 group_by_project_name）。"""
        groups = {}
        for item in data:
            ladder = item.get("梯号", "") or item.get("ladder_no", "")
            if not ladder:
                ladder = "unknown"
            if ladder not in groups:
                groups[ladder] = []
            groups[ladder].append(item)
        return groups

    def group_by_project_name(self, data):
        """
        按项目名称分组。
        无项目名时：直发地址、梯号、寻源单号、系统询价号任一相同则并为一组（并查集）。
        """
        return group_oms_rows(data)

    def get_detailed_info(self, item):
        """提取物料详细信息（点击详情或解析已有字段）"""
        # 提取数量（优先中文"数量"，其次可能的英文字段）
        quantity = item.get("数量", "")
        if not quantity:
            quantity = item.get("qty", "") or item.get("quantity", "") or item.get("Qty", "")
        
        sys_inq = ""
        for key in (
            "系统询价号",
            "系统询价价号",
            "系统查询价号",
            "oms_system_inquiry_no",
        ):
            sys_inq = (item.get(key) or "").strip()
            if sys_inq:
                break

        detail = {
            "project_name": item.get("项目名称", "") or item.get("project_name", ""),
            "ladder_no": item.get("梯号", "") or item.get("ladder_no", ""),
            "oms_system_inquiry_no": sys_inq,
            "material_desc": item.get("物料描述", "") or item.get("material_desc", ""),
            "mfg_material_no": (
                item.get("工厂料号", "")
                or item.get("mfg_material_no", "")
                or item.get("mfg_material", "")
            ).strip(),
            "supplier": (
                item.get("供应商", "")
                or item.get("supplier", "")
                or item.get("采购员", "")
            ),
            "sourcing": item.get("Sourcing", "") or item.get("sourcing", ""),
            "oms_sourcing_no": self._resolve_sourcing_no(item),
            "quantity": str(quantity).strip() if quantity else "",
            "oms_remark": (
                item.get("备注", "")
                or item.get("oms_remark", "")
                or item.get("remark", "")
            ).strip(),
        }

        # 报价单/收货人所需 OMS 列（表头可能略有差异，尽量透传）
        for k, v in item.items():
            if v is None or str(v).strip() == "":
                continue
            sk = str(k).strip()
            sv = str(v).strip()
            if "技术员" in sk and "联系" not in sk:
                detail["技术员"] = sv
            elif "技术员联系方式" in sk or ("技术" in sk and "联系" in sk):
                detail["技术员联系方式"] = sv
            elif "直发" in sk and "地址" in sk:
                detail["直发地址"] = sv
            elif "系统询价" in sk and "号" in sk:
                detail["oms_system_inquiry_no"] = sv
                detail["系统询价号"] = sv
            elif sk == "工厂料号" or ("工厂" in sk and "料号" in sk):
                detail["mfg_material_no"] = sv
            elif sk in ("地址", "收货地址", "直发地址") and "直发" not in sk:
                detail.setdefault("地址", sv)
            elif "供应商" in sk and "报价" not in sk:
                detail["供应商"] = sv
                detail["supplier"] = sv
            elif sk == "备注" or sk.endswith("备注"):
                detail["oms_remark"] = sv
            elif "出口木箱" in sk:
                detail["export_wooden_box"] = sv

        if not detail["oms_sourcing_no"]:
            detail["oms_sourcing_no"] = self._resolve_sourcing_no(item)

        pn = (detail.get("project_name") or "").strip()
        detail["attachment_project"] = (
            (item.get("attachment_project") or "").strip() or pn
        )
        detail["has_attachment"] = self._infer_has_attachment(item, pn)
        att = self.filter_attachment_paths(item.get("attachments") or [], pn)
        if item.get("attachments") and not att and pn:
            logger.warning(
                f"  附件路径不属于项目「{pn[:36]}」，已丢弃（禁止跨项目复用）"
            )
        detail["attachments"] = att
        
        for k, v in item.items():
            if k.startswith("col_"):
                val = str(v).strip()
                if val and val not in detail.values():
                    if re.search(r'[\u4e00-\u9fff]+', val) and len(val) > 2:
                        if not detail.get("material_desc"):
                            detail["material_desc"] = val
                    if re.match(r'^[A-Z0-9\-_./]+$', val) and len(val) >= 3:
                        if not detail.get("ladder_no"):
                            detail["ladder_no"] = val
        
        return detail

    def run(self, sourcing_filter=False, status_pending=True, status_sent=False, supplier_id=None):
        """
        执行OMS完整流程，返回按项目名称分组的数据（dict 键为项目名）。
        sourcing_filter: 是否按 SOURCING_KEYWORD 筛 Sourcing 列（阶段一默认 False）。
        status_sent: 为 True 时筛「已发送」（阶段二 OMS 列表用），否则默认筛待处理。
        supplier_id: 可选，按供应商ID列筛选（阶段一可配置多轮）。
        """
        logger.info("=" * 50)
        logger.info("OMS阶段：筛选待处理单据")
        logger.info("=" * 50)
        
        self.open_and_login()
        self.apply_filters(
            status_pending=status_pending,
            status_sent=status_sent,
            sourcing_filter=sourcing_filter,
            supplier_id=supplier_id,
        )
        
        all_data = self.extract_all_data()
        groups = self.group_by_project_name(all_data)
        
        # 为每组提取详细信息，并下载有附件行的实物照片
        result = {}
        for project_key, items in groups.items():
            detailed = [self.get_detailed_info(item) for item in items]
            project_name = (
                (detailed[0].get("project_name") if detailed else "")
                or project_key
            )
            detailed = self.download_attachments_for_group(project_name, detailed)
            result[project_key] = detailed
        
        logger.info(f"OMS阶段完成，共 {len(result)} 组待处理（按项目名称）")
        return result

    def refilter_and_extract(self, supplier_id):
        """
        阶段一两轮中的第二轮：不重新登录，只重新筛选 + 提取数据。
        前提：浏览器已在 OMS PSM 列表页（由调用方保证）。
        supplier_id: 供应商ID值。
        返回格式与 run() 一致：Dict[str, List[Dict]]（键为项目名/分组键）。
        """
        if not supplier_id:
            logger.warning("  refilter_and_extract: 未提供 supplier_id，跳过")
            return {}

        logger.info("=" * 50)
        logger.info(f"OMS阶段（第二轮）：筛选供应商ID={supplier_id}")
        logger.info("=" * 50)

        self.apply_filters(
            status_pending=True,
            status_sent=False,
            sourcing_filter=False,
            supplier_id=supplier_id,
        )

        all_data = self.extract_all_data()
        groups = self.group_by_project_name(all_data)

        result = {}
        for project_key, items in groups.items():
            detailed = [self.get_detailed_info(item) for item in items]
            project_name = (
                (detailed[0].get("project_name") if detailed else "")
                or project_key
            )
            detailed = self.download_attachments_for_group(project_name, detailed)
            result[project_key] = detailed

        logger.info(f"OMS第二轮完成，共 {len(result)} 组（供应商ID={supplier_id}）")
        return result

    def prepare_finalize_session(self):
        """
        阶段三：仅登录 OMS 到列表页，**不**自动筛「待处理」。
        每条记录在导出/导入前调用 apply_project_name_filter(项目名称)。
        """
        logger.info("=" * 50)
        logger.info("OMS阶段三：登录列表（不限待处理；按项目名称逐条筛选）")
        logger.info("=" * 50)
        self.open_and_login()
        if FINALIZE_FILTER_BY_SOURCING_NO:
            logger.info(
                "  提示【测试模式】：每条将按「寻源单号」列漏斗筛选（不按项目名称）；"
                "测试结束后请在 config.py 将 FINALIZE_FILTER_BY_SOURCING_NO 改回 False。"
            )
        else:
            logger.info(
                "  提示：每条将按顺序筛选 OMS 列表："
                "①状态=已发送 → ②Sourcing8ID=登录账号 → "
                "③项目名/寻源单号/直发地址/梯号/系统询价号（与阶段一分组规则一致）；"
                "若筛不出行请核对状态/账号/分组字段是否与列表一致。"
            )

    def _fill_active_tooltip_and_search(self, value):
        """在已打开的列筛选 tooltip 中填入关键字并点搜索。"""
        filled = self.browser.driver.execute_script(
            """
            var val = arguments[0];
            var tooltips = document.querySelectorAll('.tooltip');
            for (var i = 0; i < tooltips.length; i++) {
                var t = tooltips[i];
                if (t.offsetParent === null && (t.style.display === 'none' || !t.style.display)) continue;
                var inps = t.querySelectorAll(
                    'textarea, input[type=text], input:not([type=hidden]):not([type=checkbox])'
                );
                for (var j = 0; j < inps.length; j++) {
                    var inp = inps[j];
                    if (!inp.offsetParent && inp.offsetWidth === 0) continue;
                    inp.focus();
                    inp.value = val;
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            return false;
            """,
            value,
        )
        if not filled:
            return False
        self._click_tooltip_search()
        time.sleep(1.5)
        return True

    def _apply_finalize_filter_prefix(self):
        """
        阶段三 OMS 列表公共前缀筛选（每条记录导出/导入前调用）：
        ① 状态 = 已发送  ② Sourcing8ID = OMS 登录账号（user_config.OMS_USERNAME）
        """
        logger.info("  阶段三 OMS 筛选 [1/3] 状态=已发送 …")
        if not self._apply_status_sent_filter():
            logger.error(
                "  状态=已发送 未生效，后续筛选可能得到空表；"
                "请人工在状态列选中「已发送」后点搜索"
            )
        logger.info("  阶段三 OMS 筛选 [2/3] Sourcing8ID=登录账号 …")
        self._apply_sourcing8id_filter()
        return True

    def apply_sourcing_no_filter(self, sourcing_no):
        """
        阶段三【测试模式】：已发送 → Sourcing8ID → 「寻源单号」列漏斗（不按项目名称）。
        """
        no = (sourcing_no or "").strip()
        if not no:
            logger.warning("  寻源单号为空，无法筛选 OMS")
            return False
        try:
            self._apply_finalize_filter_prefix()
            logger.info(f"  阶段三 OMS 筛选 [3/3] 寻源单号: {no}")
            click_ok = False
            for col in ("寻源单号", "寻源", "Sour No", "sour_no"):
                if self._click_tooltip_anchor_by_column_name(col):
                    click_ok = True
                    break
            if not click_ok:
                logger.warning("  未找到「寻源单号」列筛选漏斗")
                return False
            if not self._fill_active_tooltip_and_search(no):
                logger.warning("  寻源单号 tooltip 内未找到可填写的输入框")
                return False
            logger.info("  ✓ 已按寻源单号筛选并搜索")
            return True
        except Exception as e:
            logger.warning(f"  按寻源单号筛选失败: {e}")
            return False

    @staticmethod
    def _finalize_filter_items(project_name, sourcing_no, group_key, items):
        """供 resolve_group_oms_email_filter 使用的行上下文（与阶段一一致）。"""
        rows = list(items or [])
        if rows:
            return rows
        stub = {}
        pn = (project_name or "").strip()
        sn = (sourcing_no or "").strip()
        gk = (group_key or "").strip()
        if pn:
            stub["project_name"] = pn
        if sn:
            stub["oms_sourcing_no"] = sn
        if gk:
            stub["group_key"] = gk
        return [stub] if stub else []

    def _resolve_finalize_list_filter(
        self, project_name, sourcing_no="", group_key="", items=None
    ):
        """阶段三列漏斗：(列名, 筛选值)，规则同阶段一 resolve_group_oms_email_filter。"""
        return resolve_group_oms_email_filter(
            (project_name or "").strip(),
            (group_key or "").strip(),
            self._finalize_filter_items(
                project_name, sourcing_no, group_key, items
            ),
        )

    def _apply_column_tooltip_filter(self, column_name, value, list_label="OMS"):
        """在已套用列表前缀筛选后，按指定列漏斗搜索。"""
        col = (column_name or "").strip()
        val = (value or "").strip()
        if not col or not val:
            logger.warning(f"  列名或筛选值为空，无法按「{col}」筛选")
            return False
        aliases = _OMS_COLUMN_FILTER_ALIASES.get(col, (col,))
        try:
            logger.info(
                f"  {list_label}按「{col}」筛选: "
                f"{val[:50]}{'…' if len(val) > 50 else ''}"
            )
            for alias in aliases:
                if not self._click_tooltip_anchor_by_column_name(alias):
                    continue
                if self._fill_active_tooltip_and_search(val):
                    logger.info(f"  ✓ {list_label}已按「{alias}」搜索")
                    return True
            logger.warning(f"  未找到「{col}」列筛选漏斗或搜索未生效")
            return False
        except Exception as e:
            logger.warning(f"  {list_label}{col} 筛选失败: {e}")
            return False

    def apply_finalize_column_filter(self, column_name, value):
        """阶段三：已发送 + Sourcing8ID + 指定列漏斗。"""
        col = (column_name or "").strip()
        val = (value or "").strip()
        if not col or not val:
            logger.warning(f"  列名或筛选值为空，无法按「{col}」筛选")
            return False
        self._apply_finalize_filter_prefix()
        return self._apply_column_tooltip_filter(
            col, val, list_label="阶段三 OMS 列表"
        )

    def apply_finalize_list_filter(
        self, project_name, sourcing_no="", group_key="", items=None
    ):
        """
        阶段三列表筛选：测试期按寻源单号；
        正式按 已发送→Sourcing8ID→（项目名/寻源/地址/梯号/系统询价号，同阶段一）。
        """
        if FINALIZE_FILTER_BY_SOURCING_NO:
            return self.apply_sourcing_no_filter(sourcing_no)
        filt = self._resolve_finalize_list_filter(
            project_name, sourcing_no, group_key, items
        )
        if not filt:
            logger.warning(
                "  无法确定阶段三 OMS 筛选列（与阶段一分组规则一致："
                "项目名 / 寻源单号 / 直发地址 / 梯号 / 系统询价号）"
            )
            return False
        col, val = filt
        logger.info(
            f"  阶段三 OMS 筛选 [3/3] {col}: "
            f"{val[:50]}{'…' if len(val) > 50 else ''}"
        )
        return self.apply_finalize_column_filter(col, val)

    def apply_project_name_filter(self, project_name):
        """
        阶段三（正式）：已发送 → Sourcing8ID(登录账号) → 项目名称漏斗搜索。
        不再使用 SOURCING_KEYWORD 列筛选。
        """
        name = (project_name or "").strip()
        if not name:
            logger.warning("  项目名称为空，无法按项目名称筛选 OMS")
            return False
        return self.apply_finalize_column_filter("项目名称", name)
