"""
归档路径工具单元测试。
运行: python test_output_paths.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.output_paths import safe_filename, target_paths, date_folder_name


class TestOutputPaths(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(safe_filename('CSSL/2026*test'), "CSSL_2026_test")

    def test_date_folder_name(self):
        from datetime import datetime
        self.assertEqual(date_folder_name(datetime(2026, 5, 18)), "5.18")

    def test_target_paths(self):
        root = os.path.join(os.path.dirname(__file__), "_test_out")
        folder, pdf, xlsx = target_paths(
            "CSSL202605021", date_label="5.18", root=root
        )
        self.assertTrue(pdf.endswith("CSSL202605021.pdf"))
        self.assertTrue(xlsx.endswith("CSSL202605021.xlsx"))
        self.assertIn("5.18", folder)

    def test_target_paths_pdf_system_inquiry_no(self):
        root = os.path.join(os.path.dirname(__file__), "_test_out")
        _, pdf, xlsx = target_paths(
            "CSSL202605021",
            date_label="5.18",
            root=root,
            pdf_base="R26101519V00",
        )
        self.assertTrue(pdf.endswith("R26101519V00.pdf"))
        self.assertTrue(xlsx.endswith("CSSL202605021.xlsx"))


if __name__ == "__main__":
    unittest.main()
