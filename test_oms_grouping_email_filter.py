"""OMS 发邮件前列筛选 — 离线回归（无需浏览器）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.oms_grouping import resolve_group_oms_email_filter


class TestResolveGroupOmsEmailFilter(unittest.TestCase):
    def test_project_name(self):
        items = [{"project_name": " 某项目 ", "oms_sourcing_no": "E1"}]
        self.assertEqual(
            resolve_group_oms_email_filter("", "某项目", items),
            ("项目名称", "某项目"),
        )

    def test_sourcing_group_key(self):
        items = [
            {"project_name": "", "oms_sourcing_no": "EHWXF202605080"},
            {"project_name": "", "oms_sourcing_no": "EHWXF202605080"},
        ]
        self.assertEqual(
            resolve_group_oms_email_filter(
                "", "寻源:EHWXF202605080", items
            ),
            ("寻源单号", "EHWXF202605080"),
        )

    def test_system_inquiry_from_key(self):
        items = [{"oms_system_inquiry_no": "260501486-2"}]
        self.assertEqual(
            resolve_group_oms_email_filter("", "询价:260501486-2", items),
            ("系统询价号", "260501486-2"),
        )

    def test_ladder_single_value(self):
        items = [
            {"ladder_no": "E/30073860.026", "oms_sourcing_no": ""},
            {"ladder_no": "E/30073860.026", "oms_sourcing_no": ""},
        ]
        self.assertEqual(
            resolve_group_oms_email_filter("", "未命名_E/30073860.026_4行", items),
            ("梯号", "E/30073860.026"),
        )

    def test_address_from_items_not_truncated_key(self):
        addr = "河南省洛阳市洛龙区开元大道世贸中心D座28层"
        items = [{"直发地址": addr, "project_name": ""}]
        self.assertEqual(
            resolve_group_oms_email_filter("", f"地址:{addr[:10]}…", items),
            ("直发地址", addr),
        )

    def test_no_single_discriminant_returns_none(self):
        items = [
            {"ladder_no": "A", "oms_sourcing_no": "S1"},
            {"ladder_no": "B", "oms_sourcing_no": "S2"},
        ]
        self.assertIsNone(resolve_group_oms_email_filter("", "", items))


if __name__ == "__main__":
    unittest.main(verbosity=2)
