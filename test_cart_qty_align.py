"""购物车数量对齐 — 独立回归测试（登录 → 查看购物车 → 对齐为 OMS 数量）"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SPAREPARTS_URL
import user_config
from utils.browser import Browser
from modules.spareparts import SparePartsModule

MATERIAL_HINT = "油压缓冲器 tkOB10B"
TARGET_QTY = 1

# 四行轿壁样例：python test_cart_qty_align.py --four-lines
FOUR_LINE_GROUP_KEY = "寻源:EHWXF202605080"


def _load_four_line_oms():
    path = os.path.join(os.path.dirname(__file__), "oms_data.json")
    with open(path, encoding="utf-8") as f:
        groups = json.load(f)
    items = groups.get(FOUR_LINE_GROUP_KEY) or []
    descs, qtys = [], []
    for it in items:
        descs.append((it.get("material_desc") or "").strip())
        q = str(it.get("quantity", "1") or "1").strip()
        try:
            qtys.append(int(float(q)))
        except (ValueError, TypeError):
            qtys.append(1)
    return descs, qtys


def main():
    four_lines = "--four-lines" in sys.argv
    browser = Browser(headless=False)
    browser.start()
    try:
        browser.navigate(SPAREPARTS_URL)
        sp = SparePartsModule(browser, vars(user_config))
        sp.login()
        sp.navigate_to_cart()

        if four_lines:
            line_descs, line_qtys = _load_four_line_oms()
            if len(line_descs) < 2:
                print("FAIL: oms_data.json 中未找到四行样例数据")
                return 1
            material_count = len(line_descs)
            cart_target = SparePartsModule.resolve_cart_quantity_target_for_align(
                None, line_qtys
            )
            print(f"OK: 四行对齐测试 material_count={material_count} qtys={line_qtys}")
        else:
            line_descs = [MATERIAL_HINT]
            line_qtys = [TARGET_QTY]
            material_count = 1
            cart_target = TARGET_QTY
            if not sp._cart_contains_material_hint(MATERIAL_HINT):
                print("FAIL: 购物车中未找到测试物料，请先保留该行或运行 --resume 加购")
                return 1
            print("OK: 购物车中已有测试物料，开始对齐数量…")

        sp.ensure_cart_quantity_aligned(
            cart_quantity_target=cart_target,
            material_count=material_count,
            cart_line_quantities=line_qtys,
            cart_line_material_descs=line_descs,
        )
        print("PASS: 购物车数量已与 OMS 对齐:", line_qtys)
        return 0
    except Exception as e:
        print("FAIL:", e)
        return 1
    finally:
        print("（测试结束，浏览器保持打开 15 秒便于目视确认）")
        import time
        time.sleep(15)
        # browser.stop()  # 保留打开便于对照


if __name__ == "__main__":
    sys.exit(main())
