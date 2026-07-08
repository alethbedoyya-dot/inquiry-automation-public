"""
浏览器工具模块
提供Edge WebDriver的创建、等待、截图等辅助功能
"""
import os
import time
import logging
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    NoSuchWindowException,
    WebDriverException,
)

logger = logging.getLogger(__name__)


class Browser:
    """
    Edge浏览器封装，提供统一的WebDriver管理和操作辅助
    
    支持通过 user_data_dir 使用已有的Edge用户配置文件（免手动登录）
    """

    def __init__(self, headless=False, user_data_dir="", download_dir=""):
        self.driver = None
        self.wait = None
        self.headless = headless
        self.user_data_dir = user_data_dir  # Edge用户数据目录
        self.download_dir = download_dir

    def start(self, download_dir=None):
        """启动Edge浏览器，如果配置了 user_data_dir 则使用已有登录态"""
        options = Options()
        if self.headless:
            options.add_argument("--headless")
        # 禁用自动化检测
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        # 最大化窗口
        options.add_argument("--start-maximized")
        # 脚本结束后不随 Python 退出而关闭浏览器（便于测试时自行查看页面）
        if not self.headless:
            options.add_experimental_option("detach", True)

        # 关闭「是否保存密码 / Save your password」等内置密码管理提示
        options.add_argument(
            "--disable-features=PasswordManagerOnboarding,PasswordCheck,AutofillServerCommunication"
        )
        prefs = {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "profile.default_content_setting_values.notifications": 2,
            "autofill.profile_enabled": False,
        }

        dl = download_dir or self.download_dir
        if dl:
            dl = os.path.abspath(dl)
            os.makedirs(dl, exist_ok=True)
            prefs.update({
                "download.default_directory": dl,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            })
            self.download_dir = dl
            logger.info(f"浏览器下载目录: {dl}")

        options.add_experimental_option("prefs", prefs)

        # 如果指定了Edge用户数据目录，使用已有配置文件（保留登录态）
        if self.user_data_dir and os.path.exists(self.user_data_dir):
            options.add_argument(f"--user-data-dir={self.user_data_dir}")
            logger.info(f"使用Edge用户配置: {self.user_data_dir}")

        # Microsoft Edge WebDriver会自动查找
        self.driver = webdriver.Edge(options=options)
        self.wait = WebDriverWait(self.driver, 15)
        logger.info("Edge浏览器已启动（已禁用密码保存提示）")
        return self.driver

    def stop(self):
        """关闭浏览器"""
        if self.driver:
            self.driver.quit()
            logger.info("Edge浏览器已关闭")

    def navigate(self, url):
        """导航到指定URL"""
        self.driver.get(url)
        logger.info(f"导航到: {url}")

    def wait_for_element(self, by, value, timeout=15):
        """等待元素可见"""
        return WebDriverWait(self.driver, timeout).until(
            EC.visibility_of_element_located((by, value))
        )

    def wait_for_clickable(self, by, value, timeout=15):
        """等待元素可点击"""
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, value))
        )

    def find_element(self, by, value):
        """查找单个元素"""
        return self.driver.find_element(by, value)

    def find_elements(self, by, value):
        """查找多个元素"""
        return self.driver.find_elements(by, value)

    def safe_click(self, by, value, timeout=15):
        """安全点击 — 等待元素可点击后点击"""
        elem = self.wait_for_clickable(by, value, timeout)
        elem.click()
        return elem

    def safe_send_keys(self, by, value, text, clear_first=True, timeout=15):
        """安全输入 — 等待元素可见后输入"""
        elem = self.wait_for_element(by, value, timeout)
        if clear_first:
            elem.clear()
        elem.send_keys(text)
        return elem

    def safe_get_text(self, by, value, timeout=15):
        """安全获取文本"""
        elem = self.wait_for_element(by, value, timeout)
        return elem.text.strip()

    def is_element_present(self, by, value, timeout=3):
        """判断元素是否存在"""
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            return True
        except TimeoutException:
            return False

    def scroll_to(self, element):
        """滚动到元素"""
        self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
        time.sleep(0.5)

    def _valid_handles(self):
        try:
            return list(self.driver.window_handles)
        except (NoSuchWindowException, WebDriverException):
            return []

    def ensure_valid_window(self, preferred_handle=None):
        """
        确保当前窗口句柄有效；若已关闭则切到 preferred 或其它仍存在的标签。
        返回当前有效句柄。
        """
        handles = self._valid_handles()
        if not handles:
            raise NoSuchWindowException("所有浏览器标签页均已关闭")

        try:
            current = self.driver.current_window_handle
        except (NoSuchWindowException, WebDriverException):
            current = None

        if current and current in handles:
            return current

        if preferred_handle and preferred_handle in handles:
            self.driver.switch_to.window(preferred_handle)
            return preferred_handle

        self.driver.switch_to.window(handles[-1])
        return handles[-1]

    def find_tab_by_url_substring(self, needle, timeout=8):
        """在仍打开的标签中查找 URL 含 needle 的句柄（不区分大小写）。"""
        needle = (needle or "").lower()
        deadline = time.time() + timeout
        while time.time() < deadline:
            for handle in self._valid_handles():
                try:
                    self.driver.switch_to.window(handle)
                    url = (self.driver.current_url or "").lower()
                    if needle in url:
                        return handle
                except (NoSuchWindowException, WebDriverException):
                    continue
            time.sleep(0.35)
        return None

    def open_new_tab(self, url, timeout=20):
        """
        在新标签页打开 URL，返回新标签句柄。
        若弹窗被拦截或新标签被站点立即关闭，则回退为在当前有效标签 navigate。
        """
        self.ensure_valid_window()
        driver = self.driver
        before = self._valid_handles()
        anchor = driver.current_window_handle

        driver.execute_script("window.open(arguments[0], '_blank');", url)

        deadline = time.time() + timeout
        new_handle = None
        while time.time() < deadline:
            after = self._valid_handles()
            new_tabs = [h for h in after if h not in before]
            if new_tabs:
                new_handle = new_tabs[-1]
                try:
                    driver.switch_to.window(new_handle)
                    return new_handle
                except (NoSuchWindowException, WebDriverException):
                    new_handle = None
            time.sleep(0.35)

        logger.warning(
            "未能稳定打开新标签页（可能被拦截或登录弹窗已关闭），改在当前标签页导航"
        )
        try:
            if anchor in self._valid_handles():
                driver.switch_to.window(anchor)
            else:
                self.ensure_valid_window()
        except (NoSuchWindowException, WebDriverException):
            self.ensure_valid_window()
        driver.get(url)
        return driver.current_window_handle

    def switch_to_window_handle(self, handle, label=""):
        """切换到指定句柄；无效时抛出明确错误。"""
        self.ensure_valid_window()
        handles = self._valid_handles()
        if handle not in handles:
            raise NoSuchWindowException(
                f"目标标签页已关闭{(' (' + label + ')') if label else ''}"
            )
        self.driver.switch_to.window(handle)
        try:
            title = (self.driver.title or "")[:50]
        except Exception:
            title = ""
        if label:
            logger.info(f"切换到标签页 [{label}]: {title}")
        return handle

    def switch_to_new_tab(self):
        """切换到最新打开的标签页（打开后请先 ensure_valid_window）"""
        handles = self._valid_handles()
        if not handles:
            raise NoSuchWindowException("无可用浏览器标签页")
        self.driver.switch_to.window(handles[-1])

    def switch_to_tab(self, index):
        """切换到指定索引的标签页（0-based）"""
        handles = self._valid_handles()
        if index < len(handles):
            self.driver.switch_to.window(handles[index])
            try:
                title = (self.driver.title or "")[:50]
            except Exception:
                title = ""
            logger.info(f"切换到标签页 [{index}]: {title}")
        else:
            logger.warning(f"标签页索引 {index} 超出范围 (共{len(handles)}个)")

    def close_current_tab(self):
        """关闭当前标签页并切回第一个"""
        self.driver.close()
        self.driver.switch_to.window(self.driver.window_handles[0])

    def take_screenshot(self, name):
        """截图保存"""
        path = os.path.join(os.path.dirname(__file__), "..", "screenshots")
        os.makedirs(path, exist_ok=True)
        filepath = os.path.join(path, f"{name}_{int(time.time())}.png")
        self.driver.save_screenshot(filepath)
        logger.info(f"截图已保存: {filepath}")
        return filepath

    def wait_until_text_changes(self, by, value, original_text, timeout=60, interval=2):
        """
        等待元素文本变化（用于等待状态变化如"已完结"）
        返回新文本，超时返回None
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                elem = self.driver.find_element(by, value)
                current = elem.text.strip()
                if current != original_text:
                    return current
            except Exception:
                pass
            time.sleep(interval)
        return None
