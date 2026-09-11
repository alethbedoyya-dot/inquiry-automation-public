"""
备件网照片上传 — 离线回归（无需浏览器）。

  python test_spareparts_photo_upload.py

覆盖：照片路径/体积、5 视图分配、异常文案、上传模块契约。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SPAREPARTS_MIN_PHOTO_BYTES
from utils.spareparts_photo_upload import (
    MANDATORY_COUNT,
    MANDATORY_VIEW_NAMES,
    OPTIONAL_VIEW_NAMES,
    REINIT_UPLOAD_POPOVER_TRIGGERS_JS,
    VIEW_COUNT,
    HIDE_AND_REMOVE_BODY_POPOVERS_JS,
    assign_photos_to_views,
    cleanup_photo_popover_artifacts,
    deep_reset_photo_upload_popovers,
    format_upload_exc,
    hide_and_remove_body_popovers,
    normalize_view_row_label,
    prepare_valid_photo_paths,
    reinit_upload_popover_triggers,
    row_label_matches_view,
    SparepartsPhotoUploader,
    view_row_uploaded_from_links,
)
from utils.image_files import (
    ensure_photos_min_bytes,
    find_photos_below_min_bytes,
    format_file_size,
)


class TestUploadExcFormat(unittest.TestCase):
    def test_timeout_has_readable_message(self):
        from selenium.common.exceptions import TimeoutException

        exc = TimeoutException()
        text = format_upload_exc(exc, "等待行内上传标志")
        self.assertIn("TimeoutException", text)
        self.assertIn("等待行内上传标志", text)


class TestAssignPhotos(unittest.TestCase):
    def test_mandatory_only_when_photos_le_five(self):
        paths = ["a.jpg", "b.jpg", "c.jpg"]
        assignments, view_names = assign_photos_to_views(paths)
        self.assertEqual(len(assignments), MANDATORY_COUNT)
        self.assertEqual(view_names, list(MANDATORY_VIEW_NAMES))
        self.assertEqual(assignments[:3], paths)
        self.assertIn(assignments[3], paths)
        self.assertIn(assignments[4], paths)

    def test_optional_views_added_when_photos_gt_five(self):
        paths = [f"p{i}.jpg" for i in range(8)]
        assignments, view_names = assign_photos_to_views(paths)
        self.assertEqual(len(assignments), VIEW_COUNT)
        self.assertEqual(
            view_names,
            list(MANDATORY_VIEW_NAMES) + list(OPTIONAL_VIEW_NAMES),
        )
        self.assertEqual(assignments, paths)

    def test_empty_photos_rejected(self):
        with self.assertRaises(ValueError):
            assign_photos_to_views([])

    def test_mandatory_view_names_fixed(self):
        self.assertEqual(
            MANDATORY_VIEW_NAMES,
            ("正视图", "左视图", "右视图", "全局", "特写"),
        )


class TestViewRowLabelMatch(unittest.TestCase):
    def test_normalize_strips_star_and_spaces(self):
        self.assertEqual(normalize_view_row_label("左视图 *"), "左视图")

    def test_exact_match_only(self):
        self.assertTrue(row_label_matches_view("左视图 *", "左视图"))
        self.assertFalse(row_label_matches_view("正视图 *", "左视图"))
        self.assertFalse(row_label_matches_view("正视图", "左视图"))

    def test_front_row_not_counted_as_left_uploaded(self):
        """正视图已上传时，不能用 contains 把该行当成左视图。"""
        self.assertFalse(
            view_row_uploaded_from_links(
                "左视图",
                "正视图 *",
                ["查看", "删除"],
                status_cell_text="查看 删除",
            )
        )


class TestViewRowUploadedDetection(unittest.TestCase):
    def test_upload_link_means_not_done(self):
        self.assertFalse(
            view_row_uploaded_from_links("正视图", "正视图", ["上传图片"])
        )

    def test_placeholder_img_with_upload_link_not_done(self):
        self.assertFalse(
            view_row_uploaded_from_links(
                "正视图", "正视图", ["上传图片", "照片样例"]
            )
        )

    def test_sample_link_with_view_text_not_done(self):
        self.assertFalse(
            view_row_uploaded_from_links(
                "左视图", "左视图", ["查看样例", "上传图片"]
            )
        )

    def test_view_delete_links_mean_done(self):
        self.assertTrue(
            view_row_uploaded_from_links(
                "正视图", "正视图", ["查看", "删除"]
            )
        )

    def test_view_only_link_mean_done(self):
        self.assertTrue(
            view_row_uploaded_from_links("全局", "全局", ["查看"])
        )

    def test_uploaded_row_still_has_upload_button(self):
        """上传成功后右侧仍保留「上传图片」，不能误判为未上传。"""
        self.assertTrue(
            view_row_uploaded_from_links(
                "正视图",
                "正视图 *",
                ["上传图片", "照片样例"],
                status_cell_text="查看 删除",
            )
        )

    def test_status_cell_view_delete(self):
        self.assertTrue(
            view_row_uploaded_from_links(
                "左视图",
                "左视图",
                [],
                status_cell_text="查看 删除",
            )
        )


class TestUploaderContract(unittest.TestCase):
    def test_public_api(self):
        self.assertTrue(callable(SparepartsPhotoUploader.upload_all))
        self.assertEqual(SparepartsPhotoUploader.POPOVER_WAIT, 12)
        self.assertTrue(callable(deep_reset_photo_upload_popovers))
        self.assertTrue(callable(cleanup_photo_popover_artifacts))
        self.assertTrue(callable(reinit_upload_popover_triggers))
        self.assertTrue(callable(hide_and_remove_body_popovers))
        self.assertIn("html: true", REINIT_UPLOAD_POPOVER_TRIGGERS_JS)
        self.assertIn("上传图片", REINIT_UPLOAD_POPOVER_TRIGGERS_JS)
        self.assertIn(".remove()", HIDE_AND_REMOVE_BODY_POPOVERS_JS)


class TestOmsCachePhotos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        oms_path = os.path.join(os.path.dirname(__file__), "oms_data.json")
        if not os.path.isfile(oms_path):
            cls.paths = []
            return
        with open(oms_path, encoding="utf-8") as f:
            data = json.load(f)
        paths = []
        for items in data.values():
            for it in items:
                for p in it.get("attachments") or []:
                    if p and os.path.isfile(p):
                        paths.append(os.path.abspath(p))
        cls.paths = list(dict.fromkeys(paths))

    def test_cache_files_exist(self):
        if not self.paths:
            self.skipTest("无 oms_data.json 或 attachments 为空")
        self.assertGreater(len(self.paths), 0)

    def test_prepare_and_assign(self):
        if not self.paths:
            self.skipTest("无附件路径")
        ensure_photos_min_bytes(self.paths, SPAREPARTS_MIN_PHOTO_BYTES)
        small = find_photos_below_min_bytes(
            self.paths, SPAREPARTS_MIN_PHOTO_BYTES
        )
        if small:
            self.fail(
                f"{len(small)} 张仍 < {format_file_size(SPAREPARTS_MIN_PHOTO_BYTES)}"
            )
        valid = prepare_valid_photo_paths(
            self.paths[:3], min_bytes=SPAREPARTS_MIN_PHOTO_BYTES
        )
        assigned = assign_photos_to_views(valid)
        self.assertEqual(len(assigned), VIEW_COUNT)
        for p in assigned:
            self.assertTrue(os.path.isfile(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
