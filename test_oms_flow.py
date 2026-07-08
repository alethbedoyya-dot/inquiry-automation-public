"""
OMS 自动化测试脚本 — 三步验证：勾选 → PSM操作 → 确认弹窗
每步截图 + JS验证，输出 PASS/FAIL 结果
用法: python test_oms_flow.py
"""
import os
import sys
import json
import time
import logging
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

from utils.browser import Browser
from modules.oms import OMSModule

try:
    from user_config import *
except ImportError:
    logger.error("请先创建 user_config.py")
    sys.exit(1)

USER_CONFIG = {
    "SOURCING_KEYWORD": SOURCING_KEYWORD if 'SOURCING_KEYWORD' in dir() else "",
    "OMS_USERNAME": OMS_USERNAME if 'OMS_USERNAME' in dir() else "",
    "OMS_PASSWORD": OMS_PASSWORD if 'OMS_PASSWORD' in dir() else "",
}

TEST_DIR = os.path.join(os.path.dirname(__file__), 'test_results')
os.makedirs(TEST_DIR, exist_ok=True)

results = []  # 收集所有测试结果


def report(step, passed, detail=""):
    status = "✅ PASS" if passed else "❌ FAIL"
    msg = f"  [{status}] {step}"
    if detail:
        msg += f" — {detail}"
    logger.info(msg)
    results.append({"step": step, "passed": passed, "detail": detail})
    return passed


def screenshot(driver, name):
    path = os.path.join(TEST_DIR, f"{name}.png")
    driver.save_screenshot(path)
    logger.info(f"  📸 截图: {path}")
    return path


def main():
    browser = Browser()
    browser.start()
    driver = browser.driver

    try:
        logger.info("=" * 60)
        logger.info(" OMS 自动化验证测试")
        logger.info("=" * 60)

        # ---------------------------------------------------------------
        # 步骤0: 登录 + 筛选
        # ---------------------------------------------------------------
        logger.info("\n[步骤0] 登录并筛选...")
        oms = OMSModule(browser, USER_CONFIG)
        oms.open_and_login()
        oms.apply_filters()
        time.sleep(3)
        screenshot(driver, "00_after_filter")
        report("登录+筛选完成", True, "已到达数据表格页面")

        # ---------------------------------------------------------------
        # 步骤1: 扫描表格 — 验证复选框存在且可交互
        # ---------------------------------------------------------------
        logger.info("\n[步骤1] 扫描表格复选框...")
        scan = driver.execute_script("""
            var tables = document.querySelectorAll('table');
            var best = null, bestRows = 0;
            for (var i = 0; i < tables.length; i++) {
                if (tables[i].offsetParent !== null) {
                    var trs = tables[i].querySelectorAll('tbody tr, tr');
                    var count = 0;
                    for (var j = 0; j < trs.length; j++) {
                        if (trs[j].querySelectorAll('td').length >= 3) count++;
                    }
                    if (count > bestRows) { bestRows = count; best = tables[i]; }
                }
            }
            if (!best) return JSON.stringify({error: 'no_table'});

            var trs = best.querySelectorAll('tbody tr, tr');
            var totalDataRows = 0;
            var checkboxes = [];
            var firstRowText = '';

            for (var r = 0; r < trs.length; r++) {
                var tds = trs[r].querySelectorAll('td');
                if (tds.length < 3) continue;
                totalDataRows++;

                var cb = trs[r].querySelector('input[type="checkbox"]');
                if (cb) {
                    checkboxes.push({
                        rowIndex: r,
                        name: cb.name,
                        value: cb.value,
                        checked: cb.checked,
                        visible: cb.offsetParent !== null
                    });
                }

                if (totalDataRows === 1) {
                    firstRowText = trs[r].textContent.substring(0, 200);
                }
            }

            return JSON.stringify({
                totalDataRows: totalDataRows,
                checkboxCount: checkboxes.length,
                checkboxes: checkboxes.slice(0, 3),
                firstRowText: firstRowText
            });
        """)
        info = json.loads(scan)

        if info.get('error'):
            report("表格扫描", False, info['error'])
            return

        total_rows = info.get('totalDataRows', 0)
        cb_count = info.get('checkboxCount', 0)
        logger.info(f"  表格: {total_rows} 数据行, {cb_count} 个复选框")
        for cb in info.get('checkboxes', []):
            logger.info(f"    行[{cb['rowIndex']}] name={cb['name']} value={cb['value']} checked={cb['checked']} visible={cb['visible']}")

        report(
            "表格复选框存在",
            cb_count > 0 and total_rows > 0,
            f"{cb_count}个复选框/{total_rows}行"
        )

        first_row = info.get('firstRowText', '')

        # ---------------------------------------------------------------
        # 步骤2: 勾选第一行 — 验证 checked 状态变为 true
        # ---------------------------------------------------------------
        logger.info("\n[步骤2] 勾选第一行...")
        screenshot(driver, "01_before_check")

        check_result = driver.execute_script("""
            var tables = document.querySelectorAll('table');
            var best = null, bestRows = 0;
            for (var i = 0; i < tables.length; i++) {
                if (tables[i].offsetParent !== null) {
                    var trs = tables[i].querySelectorAll('tbody tr, tr');
                    var count = 0;
                    for (var j = 0; j < trs.length; j++) {
                        if (trs[j].querySelectorAll('td').length >= 3) count++;
                    }
                    if (count > bestRows) { bestRows = count; best = tables[i]; }
                }
            }
            if (!best) return JSON.stringify({error: 'no_table'});

            var trs = best.querySelectorAll('tbody tr, tr');
            for (var r = 0; r < trs.length; r++) {
                var tds = trs[r].querySelectorAll('td');
                if (tds.length < 3) continue;

                // 找复选框（优先name=aid，其次跳过全选框）
                var cb = trs[r].querySelector('input[name="aid"]');
                if (!cb) {
                    var allCbs = trs[r].querySelectorAll('input[type="checkbox"]');
                    for (var c = 0; c < allCbs.length; c++) {
                        if (allCbs[c].className.indexOf('allSel') === -1) {
                            cb = allCbs[c];
                            break;
                        }
                    }
                }
                if (!cb) continue;

                var before = cb.checked;

                // forceCheck 四步
                try { cb.scrollIntoView({block: 'center', behavior: 'instant'}); } catch(e) {}
                try { cb.click(); } catch(e) {}
                if (!cb.checked) {
                    try {
                        var evt = new MouseEvent('click', {bubbles: true, cancelable: true, view: window, button: 0, buttons: 1});
                        cb.dispatchEvent(evt);
                    } catch(e) {}
                }
                if (!cb.checked) {
                    try { cb.dispatchEvent(new Event('change', {bubbles: true})); } catch(e) {}
                }
                if (!cb.checked) {
                    cb.checked = true;
                    try { cb.dispatchEvent(new Event('change', {bubbles: true})); } catch(e) {}
                }

                return JSON.stringify({
                    before: before,
                    after: cb.checked,
                    name: cb.name,
                    value: cb.value,
                    selected: cb.checked,
                    rowText: trs[r].textContent.substring(0, 100)
                });
            }
            return JSON.stringify({error: 'no_checkbox_found'});
        """)
        chk = json.loads(check_result)

        if chk.get('error'):
            report("勾选操作", False, chk['error'])
        else:
            passed = chk.get('after', False) == True
            logger.info(f"  before={chk['before']} → after={chk['after']} (value={chk['value']})")
            screenshot(driver, "02_after_check")
            report(
                "勾选复选框",
                passed,
                f"name={chk.get('name')} value={chk.get('value')} checked={chk.get('after')}"
            )

        # ---------------------------------------------------------------
        # 步骤3: 点击 PSM操作 按钮
        # ---------------------------------------------------------------
        logger.info("\n[步骤3] 点击 PSM操作 按钮...")
        psm = driver.execute_script("""
            var all = document.querySelectorAll('button, a');
            for (var i = 0; i < all.length; i++) {
                if ((all[i].textContent || '').trim().indexOf('PSM操作') > -1 && all[i].offsetParent !== null) {
                    all[i].click();
                    return JSON.stringify({success: true, tag: all[i].tagName, text: all[i].textContent.trim()});
                }
            }
            return JSON.stringify({success: false});
        """)
        psm_r = json.loads(psm)
        time.sleep(1.5)
        screenshot(driver, "03_after_psm_click")
        report("PSM操作按钮点击", psm_r.get('success', False), psm_r.get('text', '未找到'))

        # ---------------------------------------------------------------
        # 步骤4: 点击「发送供应商报价邮件(附件+报价EXCEL)」（勿点仅报价EXCEL）
        # ---------------------------------------------------------------
        logger.info("\n[步骤4] 点击菜单项(附件+报价EXCEL)...")
        menu = driver.execute_script("""
            function isTarget(txt) {
                if (!txt || txt.indexOf('发送') < 0 || txt.indexOf('报价') < 0) return false;
                if (txt.indexOf('发送供应商报价邮件') < 0 &&
                    txt.indexOf('发送供应商报价') < 0) return false;
                return txt.indexOf('附件') >= 0;
            }
            var menus = document.querySelectorAll('.dropdown-menu, ul[role="menu"], div[role="menu"]');
            for (var i = 0; i < menus.length; i++) {
                if (menus[i].offsetParent === null) continue;
                var items = menus[i].querySelectorAll('a, li, button');
                for (var j = 0; j < items.length; j++) {
                    var txt = (items[j].textContent || '').trim();
                    if (isTarget(txt)) {
                        items[j].click();
                        return JSON.stringify({success: true, text: txt.substring(0, 60)});
                    }
                }
            }
            return JSON.stringify({success: false});
        """)
        menu_r = json.loads(menu)
        time.sleep(2)
        screenshot(driver, "04_after_menu_click")
        report("菜单项点击", menu_r.get('success', False), menu_r.get('text', '未找到菜单项'))

        # ---------------------------------------------------------------
        # 步骤5: 确认弹窗 — 等待并点击确定
        # ---------------------------------------------------------------
        logger.info("\n[步骤5] 确认弹窗...")
        confirm_success = False
        for attempt in range(15):
            time.sleep(0.5)
            cfm = driver.execute_script("""
                var modals = document.querySelectorAll('.modal, .layui-layer, .dialog, [role="dialog"], [class*="modal"], [class*="dialog"], [class*="confirm"]');
                for (var i = 0; i < modals.length; i++) {
                    var m = modals[i];
                    if (m.offsetParent === null) continue;
                    var btns = m.querySelectorAll('button, a');
                    for (var j = 0; j < btns.length; j++) {
                        var txt = (btns[j].textContent || '').trim();
                        if (txt === '确定' || txt === '确认') {
                            btns[j].click();
                            return JSON.stringify({success: true, text: txt, attempt: arguments[0]});
                        }
                    }
                }
                // 全局搜索
                var all = document.querySelectorAll('button');
                for (var k = 0; k < all.length; k++) {
                    var txt = (all[k].textContent || '').trim();
                    if (txt === '确定' || txt === '确认') {
                        if (all[k].offsetParent !== null) {
                            all[k].click();
                            return JSON.stringify({success: true, text: txt, fallback: true, attempt: arguments[0]});
                        }
                    }
                }
                return JSON.stringify({success: false, attempt: arguments[0]});
            """, attempt)
            cfm_r = json.loads(cfm)
            if cfm_r.get('success'):
                confirm_success = True
                logger.info(f"  第{attempt+1}次尝试成功: {cfm_r.get('text')}")
                break

        time.sleep(1)
        screenshot(driver, "05_after_confirm")
        report("确认弹窗点击", confirm_success,
               f"第{attempt+1}次尝试" if confirm_success else f"全部{attempt+1}次失败")

        # ---------------------------------------------------------------
        # 最终汇总
        # ---------------------------------------------------------------
        total = len(results)
        passed = sum(1 for r in results if r['passed'])
        failed = total - passed

        logger.info("\n" + "=" * 60)
        logger.info(f" 测试结果: {passed}/{total} 通过, {failed} 失败")
        logger.info("=" * 60)
        for r in results:
            icon = "✅" if r['passed'] else "❌"
            logger.info(f"  {icon} {r['step']}: {r['detail']}")

        # 保存 JSON 结果
        result_path = os.path.join(TEST_DIR, f"result_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump({
                "total": total,
                "passed": passed,
                "failed": failed,
                "results": results,
                "screenshots_dir": TEST_DIR
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"\n结果已保存: {result_path}")

        return passed == total  # True = 全通过

    finally:
        logger.info("\n按 Enter 关闭浏览器...")
        try:
            input()
        except EOFError:
            time.sleep(2)
        browser.stop()


if __name__ == "__main__":
    all_pass = main()
    sys.exit(0 if all_pass else 1)