"""
询价单自动化 — 主流程编排脚本

三阶段自动化流程（审批由人工确认后继续）:

  阶段一: OMS筛选 → 备件网新建所有询价单 → 保存结果 → 停止（等待人工确认审批完成）
  阶段二: 加载结果 → 检查审批 → 购物车 → 生成报价单
  阶段三: 导出PDF/Excel → 填表 → OMS导入（python main.py --finalize）

使用方式:
  【阶段一】python main.py              创建所有询价单（含OMS筛选）
  【阶段一】python main.py --skip-oms    跳过OMS，用缓存数据创建询价单
  【阶段二】python main.py --resume      人工确认审批完成后，继续购物车→报价单
        （中山：自动用 oms_data.json 补全收货人/直发地址；仍缺时新开 OMS 标签拉取一次）
  python main.py --step-by-step         逐步执行，每步需确认
  python main.py --headless             无头模式运行

每个使用者请在 user_config.py 中填写自己的信息！
"""

import sys
import os
import copy
import logging
import time
import argparse

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    OMS_URL, OMS_HOME_URL, SPAREPARTS_URL,
    SONGJIANG_CONFIG, ZHONGSHAN_CONFIG,
    DEFAULT_FACTORY, TEMPLATE_PHOTOS_DIR,
    DEFAULT_PROJECT_SPLIT_COUNT,
    FINALIZE_IMPORT_SUBMIT,
    OMS_ATTACHMENT_CACHE_RETENTION_DAYS,
    MANUAL_RECOVERY_ON_ERROR,
    OMS_GROUP_CONFIRM_UNCERTAIN,
    OMS_PHASE1_SUPPLIER_IDS,
)
from utils.oms_grouping import format_group_summary
from utils.browser import Browser
from utils.output_paths import get_quote_output_dir, target_paths, date_folder_name
from utils.downloads import wait_and_archive
from utils.attachment_cache import cleanup_stale_oms_attachment_caches
from utils.manual_intervention import SkipCurrentGroup, AbortRun
from utils.logging_setup import setup_logging
from utils.inquiry_store import (
    INQUIRY_LAST_FILE,
    INQUIRY_RESULTS_FILE,
    SESSIONS_EXCEL,
    _backup_inquiry_results_if_needed,
    _cleanup_old_session_backups,
    _load_inquiries_from_file,
    _log_results_to_sessions_excel,
    _update_sessions_excel_for_inquiries,
    load_inquiry_results,
    load_oms_data,
    merge_oms_data_group,
    replace_inquiry_results,
    resolve_inquiry_results_path,
    save_finalize_results,
    save_inquiry_last_archive,
    save_inquiry_results,
    save_oms_data,
)
from modules.oms import OMSModule
from modules.spareparts import SparePartsModule
from modules.quote_fill import fill_excel_from_pdf


def load_user_config(force_setup=False, setup_only=False):
    """
    加载 user_config.py；首次运行或配置不完整时在终端交互录入并自动保存。
    setup_only: 仅保存配置（--setup-config），不启动后续自动化。
    """
    from utils.user_config_store import ensure_user_config

    return ensure_user_config(force_setup=force_setup, setup_only=setup_only)




logger = logging.getLogger(__name__)


class InquiryAutomation:
    """
    询价单自动化主控制器
    
    协调OMS和备件网两个模块完成完整的询价单制作流程。
    
    双阶段工作流：
      【第一阶段】python main.py           → OMS筛选 → 创建所有询价单 → 保存结果 → 停止
      【第二阶段】python main.py --resume  → 加载结果 → 检查审批 → 购物车 → 报价单
      【第三阶段】python main.py --finalize → PDF/Excel 归档、填表、OMS 导入
    """

    def __init__(
        self,
        user_config,
        step_by_step=False,
        headless=False,
        skip_oms=False,
        resume=False,
        finalize=False,
        inquiry_results_path=None,
        close_browser_on_exit=False,
        date_folder=None,
        project_split_count=None,
        finalize_only=None,
        import_submit=None,
        manual_recovery_on_error=True,
    ):
        self.user_config = user_config
        self.step_by_step = step_by_step
        self.manual_recovery_on_error = manual_recovery_on_error
        self.skip_oms = skip_oms
        self.resume = resume
        self.finalize = finalize
        self.date_folder = date_folder or date_folder_name()
        self.project_split_count = project_split_count or DEFAULT_PROJECT_SPLIT_COUNT
        self.finalize_only = finalize_only
        # None=用 config.FINALIZE_IMPORT_SUBMIT；--import-submit 强制提交
        self.import_submit = (
            FINALIZE_IMPORT_SUBMIT if import_submit is None else import_submit
        )
        # 默认 False：跑完后不自动关 Edge，便于对照页面排查；需要自动关闭时加 --close-browser
        self.close_browser_on_exit = close_browser_on_exit
        # 阶段二加载/写回的询价单 JSON（默认 inquiry_results.json，可用 --inquiry-results 覆盖）
        self.inquiry_results_path = inquiry_results_path or INQUIRY_RESULTS_FILE
        self.browser = Browser(headless=headless, user_data_dir=user_config.get("EDGE_USER_DATA_DIR", ""))
        self.oms = OMSModule(self.browser, user_config)
        self.spareparts = None  # 将在备件网打开后初始化
        self._oms_window_handle = None
        self._spareparts_window_handle = None
        self._oms_groups_file_cache = None
        self._oms_live_groups = None
        self._oms_live_fetch_attempted = False
        self.results = []
        self.error_collector = None  # 由 setup_logging 设置

    def confirm_step(self, step_name):
        """逐步执行模式下等待用户确认"""
        if self.step_by_step:
            try:
                input(f"\n>>> 下一步: {step_name} — 按 Enter 继续...")
            except (EOFError, KeyboardInterrupt):
                logger.info(f"  (非交互模式，自动继续: {step_name})")

    def _confirm_oms_grouping_if_needed(self, group_key, items):
        """
        无项目名且自动分组不确定时，请操作员核对是否为同一组。
        [Enter]/继续=确认同组；[s]=跳过本组；[q]=退出。
        """
        if not OMS_GROUP_CONFIRM_UNCERTAIN:
            return
        if not items or not any(it.get("group_needs_review") for it in items):
            return

        logger.info("")
        logger.info("=" * 72)
        logger.info("【请确认 OMS 分组】程序无法完全确定以下行是否属于同一项目")
        logger.info(format_group_summary(items, group_key))
        logger.info("")
        logger.info("  请在 OMS 列表中核对；若确认为同一组：")
        logger.info("    [Enter] 继续 — 按此分组创建询价单并发送邮件")
        logger.info("    [s]     跳过 — 不处理本组，继续下一组")
        logger.info("    [q]     退出 — 结束本次运行")
        logger.info("=" * 72)
        try:
            raw = input("\n>>> [Enter]确认同组 / s跳过 / q退出: ")
        except (EOFError, KeyboardInterrupt):
            logger.info("  (非交互环境，按确认同组继续)")
            return

        from utils.manual_intervention import parse_operator_choice, CHOICE_ABORT, CHOICE_SKIP_GROUP

        choice = parse_operator_choice(raw, allow_inquiry_full=False)
        if choice == CHOICE_ABORT:
            from utils.manual_intervention import AbortRun
            raise AbortRun("操作员终止运行（OMS 分组确认）")
        if choice == CHOICE_SKIP_GROUP:
            raise SkipCurrentGroup(f"操作员跳过 OMS 分组: {group_key}")
        logger.info("  ✓ 已确认本组为同一项目，继续执行")

    def _prompt_manual_recovery(
        self,
        step_title: str,
        exc: BaseException,
        *,
        extra_hints: list | None = None,
    ) -> str:
        """
        任意步骤出错后的统一人工介入提示。
        返回: step_done | retry | skip_group | abort
        """
        from utils.manual_intervention import (
            format_exception_brief,
            format_exception_detail,
            parse_operator_choice,
            CHOICE_ABORT,
            CHOICE_RETRY,
            CHOICE_SKIP_GROUP,
            CHOICE_STEP_DONE,
        )

        logger.info("")
        logger.info("=" * 72)
        logger.info(f"【需人工处理】{step_title}")
        logger.info(f"  原因: {format_exception_brief(exc)}")
        detail = format_exception_detail(exc)
        if detail and detail != format_exception_brief(exc):
            for line in detail.splitlines()[:12]:
                logger.info(f"    {line}")
        for hint in extra_hints or []:
            logger.info(f"  · {hint}")
        logger.info("")
        logger.info("  ① 先在浏览器里把当前这一步做完")
        logger.info("  ② 再选下面一项（启动器点同名按钮即可）：")
        logger.info("     [Enter] 继续 — 程序接下去自动做后面步骤")
        logger.info("     [r]     重试 — 自动再执行一次本步")
        logger.info("     [s]     跳过 — 不做本项目，换下一组")
        logger.info("     [q]     退出 — 结束本次运行")
        logger.info("=" * 72)
        try:
            raw = input("\n>>> [Enter]继续 / r重试 / s跳过 / q退出: ")
        except (EOFError, KeyboardInterrupt):
            logger.info("  (非交互环境，按「继续」处理)")
            return CHOICE_STEP_DONE

        choice = parse_operator_choice(raw, allow_inquiry_full=False)
        if choice == CHOICE_ABORT:
            raise AbortRun(f"操作员终止运行（步骤: {step_title}）")
        if choice == CHOICE_SKIP_GROUP:
            raise SkipCurrentGroup(f"操作员跳过（步骤: {step_title}）")
        if choice == CHOICE_RETRY:
            return CHOICE_RETRY
        return CHOICE_STEP_DONE

    @staticmethod
    def _step_or_raise(step_title: str, func, *, detail_key: str = "error"):
        """
        执行 func；若返回 dict 且 success 为 False，抛出异常以触发人工暂停。
        （避免下层只 logger.error 不 raise，导致绕过 _run_with_manual_recovery）
        """
        result = func()
        if isinstance(result, dict) and "success" in result and not result.get("success"):
            detail = (result.get(detail_key) or result.get("resume_error") or "").strip()
            msg = f"{step_title} 未能自动完成"
            if detail:
                msg = f"{msg}: {detail}"
            raise RuntimeError(msg)
        return result

    def _run_with_manual_recovery(
        self,
        step_title: str,
        func,
        *,
        recover_on_done=None,
        extra_hints: list | None = None,
        max_attempts: int = 12,
    ):
        """
        执行 func()；失败且开启人工恢复时暂停，由操作员处理后重试或继续。
        recover_on_done: 操作员选「继续」时调用，返回替代 func() 的结果。
        """
        from utils.manual_intervention import (
            ManualInterventionNeeded,
            CHOICE_RETRY,
            CHOICE_STEP_DONE,
        )

        attempts = 0
        while attempts < max_attempts:
            try:
                return func()
            except (SkipCurrentGroup, AbortRun):
                raise
            except Exception as exc:
                if not self.manual_recovery_on_error:
                    raise
                if isinstance(exc, ManualInterventionNeeded):
                    pause_exc = exc
                else:
                    pause_exc = exc
                choice = self._prompt_manual_recovery(
                    step_title,
                    pause_exc,
                    extra_hints=extra_hints,
                )
                if choice == CHOICE_RETRY:
                    attempts += 1
                    logger.info(f"  重试「{step_title}」({attempts}/{max_attempts})…")
                    continue
                if choice == CHOICE_STEP_DONE:
                    if recover_on_done:
                        return recover_on_done()
                    logger.warning(
                        f"  「{step_title}」无恢复回调，假定人工已完成，返回 None"
                    )
                    return None
        raise RuntimeError(f"「{step_title}」重试次数已达上限 {max_attempts}")

    def _pause_spareparts_photo_upload(self, exc):
        """备件网照片专用：统一「继续」菜单，自动判断已提交或仅补传照片。"""
        from utils.image_files import format_file_size
        from config import SPAREPARTS_MIN_PHOTO_BYTES
        from utils.manual_intervention import CHOICE_RETRY

        hints = [
            f"照片需 ≥ {format_file_size(SPAREPARTS_MIN_PHOTO_BYTES)}",
            "程序会先自动以 JPEG 质量 100% 增强过小文件；仍不足时再手工改 cache 或页面传图",
            "若出现「提示 / 正视图没有上传」：先点弹窗「关闭」，否则会挡住「上传图片」；"
            "补传后点继续；程序不会自动关该提示框",
            "仍无法点「上传图片」时按 r：程序会清理残留浮层后重试自动上传",
            "启动器：▶继续 / 停止；终端：[Enter]继续 r重试 s跳过 q退出",
        ]
        if getattr(exc, "view_name", ""):
            hints.append(f"出错视图: {exc.view_name}")
        if getattr(exc, "small_files", None):
            for path, sz in exc.small_files:
                hints.append(
                    f"过小文件: {os.path.basename(path)} ({format_file_size(sz)})"
                )
        choice = self._prompt_manual_recovery(
            "备件网照片上传",
            exc,
            extra_hints=hints,
        )
        if choice == CHOICE_RETRY:
            return "retry"
        if self.spareparts:
            inquiry_no = (
                self.spareparts.continue_inquiry_creation_after_manual().strip()
            )
            if inquiry_no:
                return "inquiry_done"
        return "skip_uploads"

    def _recover_inquiry_created(
        self,
        *,
        project_name,
        ladder_no,
        items,
    ):
        """创建询价单：人工点「继续」后自动识别单号或补点提交。"""
        inquiry_no = ""
        if self.spareparts:
            inquiry_no = (
                self.spareparts.continue_inquiry_creation_after_manual().strip()
            )
        if not inquiry_no:
            try:
                inquiry_no = input(
                    "\n>>> 若已在备件网提交，请填询价单号（回车=跳过本项目）: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                pass
        if not inquiry_no:
            logger.error("  未获取询价单号，跳过本项目")
            raise SkipCurrentGroup()
        logger.info(f"  ✓ 继续流程，询价单号: {inquiry_no}")
        return {
            "project_name": project_name,
            "ladder_no": ladder_no,
            "inquiry_no": inquiry_no,
            "success": True,
            "material_count": len(items) if items else 0,
            "manual_recovery": True,
        }

    def _run_oms_send_email_step(
        self, project_name: str, items=None, group_key: str = ""
    ):
        """阶段一：OMS 发送供应商邮件（可包在人工恢复内）。"""
        if self._oms_window_handle:
            self.browser.switch_to_window_handle(self._oms_window_handle, "OMS")
        else:
            self.browser.switch_to_tab(0)
        time.sleep(1)
        rows_processed, email_ok = self.oms.send_supplier_email(
            project_name,
            items=items,
            group_key=group_key,
        )
        if self._spareparts_window_handle:
            self.browser.switch_to_window_handle(
                self._spareparts_window_handle, "备件网"
            )
        else:
            self.browser.switch_to_tab(1)
        time.sleep(1)
        if rows_processed > 0 and not email_ok:
            raise RuntimeError(
                f"OMS 已勾选 {rows_processed} 行但邮件未确认发出，请人工补发"
            )
        return {"rows_processed": rows_processed, "email_ok": email_ok}

    def _recover_oms_email_done(self, **_kwargs):
        logger.info("  ✓ 假定 OMS 发邮件已由人工完成")
        return {"rows_processed": 0, "email_ok": True, "manual_recovery": True}

    def _recover_resume_cart_after_manual(self, inquiry):
        """阶段二：人工在购物车/加购完成后继续（假定页面已处理好）。"""
        inquiry_no = inquiry.get("inquiry_no", "")
        logger.info(
            f"  ✓ 假定询价单 {inquiry_no} 购物车步骤已由人工完成，继续生成报价单"
        )
        out = {
            "inquiry_no": inquiry_no,
            "project_name": inquiry.get("project_name", ""),
            "ladder_no": inquiry.get("ladder_no", ""),
            "approved": True,
            "success": True,
            "manual_recovery": True,
        }
        if self.spareparts and getattr(
            self.spareparts, "last_detail_po_factory", None
        ):
            out["po_factory"] = self.spareparts.last_detail_po_factory
        return out

    def _recover_quotation_after_manual(self, inquiry):
        """阶段二：人工完成报价单后取报价单号。"""
        q = (inquiry.get("quotation_no") or "").strip()
        if not q and self.spareparts and hasattr(
            self.spareparts, "get_quotation_number"
        ):
            q = (self.spareparts.get_quotation_number() or "").strip()
        if not q:
            try:
                q = input(
                    "\n>>> 报价单号（已在备件网生成后填写，回车跳过本询价单）: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                pass
        if not q:
            raise SkipCurrentGroup()
        logger.info(f"  ✓ 人工完成报价单，单号: {q}")
        return q

    def _teardown_browser(self):
        """结束会话：默认保留浏览器窗口，由用户自行关闭后再敲下一条命令。"""
        if self.close_browser_on_exit:
            try:
                self.browser.stop()
            except Exception:
                pass
            return
        if getattr(self.browser, "driver", None):
            logger.info("")
            logger.info("=" * 60)
            logger.info("  流程已结束，Edge 浏览器保持打开（便于对照页面排查）。")
            logger.info("  请在浏览器中查看无误后，自行关闭 Edge 窗口。")
            logger.info("  关闭后终端会出现新的命令提示符，再输入下一条 python 命令即可。")
            logger.info("=" * 60)

    def run(self):
        """
        执行自动化流程。
        
        根据模式分发到不同子流程：
        - --resume:  第二阶段 — 从 inquiry_results.json 加载数据，检查审批→购物车→报价单
        - 普通/skip-oms:  第一阶段 — OMS筛选→创建询价单→保存结果
        """
        if self.finalize:
            self._run_finalize_phase()
        elif self.resume:
            self._run_resume_phase()
        else:
            self._run_create_phase()

    def _process_oms_groups(self, oms_groups, round_label=""):
        """
        处理 OMS 分组数据：逐组创建备件网询价单 + OMS 发送供应商报价邮件。
        返回本轮创建的 inquiry_results 列表。
        """
        prefix = f"[{round_label}] " if round_label else ""
        inquiry_results = []

        for group_key, items in oms_groups.items():
            try:
                self._confirm_oms_grouping_if_needed(group_key, items)
            except SkipCurrentGroup:
                logger.warning(f"  {prefix}已跳过 OMS 分组: {group_key}")
                continue

            logger.info("\n" + "=" * 50)
            project_name = self._extract_project_name(items)
            attachment_label = project_name or group_key
            ladder_no = self._extract_primary_ladder_no(items)
            ladder_note = self._format_group_ladder_note(items)
            logger.info(
                f"{prefix}处理项目: {project_name or f'（无项目名，分组 {group_key}）'} "
                f"({ladder_note}, {len(items)} 条物料)"
            )
            logger.info("=" * 50)

            items = self.oms.sanitize_group_attachments(
                attachment_label, items
            )
            factory_type = self._determine_factory(items)
            oms_address = self._get_oms_address(items)
            oms_direct = self._get_oms_direct_address(items) or oms_address
            oms_recipient = self._build_oms_recipient_line(items)

            inquiry_items = []
            for item in items:
                desc = item.get("material_desc", "")
                qty = self._normalize_quantity(item.get("quantity", ""))
                mfg_no = (
                    item.get("mfg_material_no")
                    or item.get("工厂料号")
                    or ""
                ).strip()
                line_ladder = (
                    item.get("ladder_no") or item.get("梯号") or ""
                ).strip()
                line_photos = self._collect_photos([item], attachment_label)
                line_remark = self._extract_item_remark(item)
                inquiry_items.append({
                    "material_desc": desc,
                    "quantity": qty,
                    "material_no": mfg_no,
                    "ladder_no": line_ladder,
                    "photos": line_photos,
                    "remark": line_remark,
                })

            confirm_ladder = ladder_note or ladder_no or "—"
            self.confirm_step(
                f"{prefix}创建询价单: 项目={project_name or '（无，按条填梯号）'}, "
                f"{confirm_ladder}"
            )

            try:
                inquiry_result = self._run_with_manual_recovery(
                    f"{prefix}创建询价单: {project_name} (梯号 {ladder_no})",
                    lambda: self._step_or_raise(
                        f"创建询价单: {project_name} (梯号 {ladder_no})",
                        lambda: self.spareparts.run_single_inquiry(
                            project_name=project_name,
                            ladder_no=ladder_no,
                            items=inquiry_items,
                        ),
                    ),
                    recover_on_done=lambda **_: self._recover_inquiry_created(
                        project_name=project_name,
                        ladder_no=ladder_no,
                        items=inquiry_items,
                    ),
                    extra_hints=[
                        "常见：照片<100KB、找不到「新建询价单」、提交超时",
                        "处理完后 [Enter] 继续；启动器点 ▶ 继续",
                    ],
                )
            except SkipCurrentGroup:
                logger.warning(f"  {prefix}已跳过项目 {project_name}")
                continue

            if not inquiry_result:
                logger.error(f"{prefix}项目 {project_name} 询价单创建失败，跳过")
                continue

            inquiry_no = inquiry_result["inquiry_no"]

            # 保存结果供 --resume 阶段使用
            oms_supplier = self._extract_oms_supplier(items)
            inquiry_results.append({
                "inquiry_no": inquiry_no,
                "ladder_no": ladder_no,
                "project_name": project_name,
                "oms_group_key": group_key,
                "factory_type": factory_type,
                "oms_supplier": oms_supplier,
                "oms_address": oms_address,
                "oms_direct_address": oms_direct,
                "oms_recipient": oms_recipient,
                "material_count": len(items),
                "oms_sourcing_no": self._extract_oms_sourcing_no(items),
                "project_split_count": DEFAULT_PROJECT_SPLIT_COUNT,
            })

            label = project_name or group_key or ladder_no
            logger.info(f"{prefix}项目 {label} 询价单创建完成！单号: {inquiry_no}")

            # 立即回到OMS，发送供应商报价邮件
            if not self.skip_oms:
                logger.info(f"  {prefix}切回OMS标签页，发送供应商报价邮件...")
                try:
                    email_outcome = self._run_with_manual_recovery(
                        f"OMS 发送供应商邮件: {label}",
                        lambda pn=project_name, grp_items=items, gk=group_key: (
                            self._run_oms_send_email_step(
                                pn, items=grp_items, group_key=gk
                            )
                        ),
                        recover_on_done=self._recover_oms_email_done,
                        extra_hints=[
                            "请在 OMS 中手工勾选行并发送邮件后点继续",
                        ],
                    )
                    rows_processed = (email_outcome or {}).get(
                        "rows_processed", 0
                    )
                    email_ok = (email_outcome or {}).get("email_ok", False)
                    if rows_processed > 0 and email_ok:
                        logger.info(
                            f"  ✓ OMS已勾选并发送邮件 {rows_processed} 行"
                        )
                    elif rows_processed > 0:
                        logger.warning(
                            f"  ⚠ OMS已勾选 {rows_processed} 行但邮件未确认发出"
                        )
                    else:
                        logger.warning(f"  ⚠ OMS未找到匹配记录")
                except SkipCurrentGroup:
                    logger.warning(
                        f"  {prefix}已跳过 OMS 邮件步骤（项目 {project_name} 询价单已保存）"
                    )
                except Exception as e:
                    if self.manual_recovery_on_error:
                        logger.error(f"  ✗ OMS发送邮件失败: {e}")
                    else:
                        raise

                logger.info(f"  {prefix}切回备件网标签页，继续下一组...")

        return inquiry_results

    def _run_create_phase(self):
        """
        【第一阶段】OMS筛选 → 备件网登录 → 创建所有询价单 → 保存 inquiry_results.json → 停止
        
        审批等待由人工完成，确认状态='已完结'后运行 python main.py --resume 继续。
        
        在线模式下，按供应商ID分两轮筛选：
          可按 config.OMS_PHASE1_SUPPLIER_IDS 配置多轮供应商 ID 筛选。
        """
        logger.info("=" * 60)
        logger.info("  询价单自动化系统 — 第一阶段：创建询价单")
        if self.skip_oms:
            logger.info("  (跳过OMS模式: 从 oms_data.json 加载数据)")
        logger.info("=" * 60)

        try:
            # 清理过期备份 + 自动备份之前未完结的待办数据（防止被本次覆盖）
            if not self.skip_oms:
                _cleanup_old_session_backups()
                _backup_inquiry_results_if_needed()
            # ============================================================
            # 阶段A：获取OMS数据（在线/离线）
            # ============================================================
            if self.skip_oms:
                logger.info("\n[SKIP] 阶段A：跳过OMS筛选，加载缓存数据")
                oms_groups = load_oms_data()
                if not oms_groups:
                    logger.error("未找到 oms_data.json！请先运行一次完整流程生成数据。")
                    return
            else:
                self.confirm_step("启动浏览器 -> 打开OMS系统")
                self.browser.start()

                # ── 第一轮：按配置的第一个供应商 ID 筛选 ──
                supplier_ids = list(OMS_PHASE1_SUPPLIER_IDS) if OMS_PHASE1_SUPPLIER_IDS else []
                if not supplier_ids:
                    # 未配置供应商ID列表，退化为不筛选供应商ID
                    logger.info("\n🔍 阶段A：OMS系统 - 筛选待处理单据（未启用供应商ID筛选）")
                    oms_groups = self.oms.run()
                    if not oms_groups:
                        logger.warning("OMS中无待处理单据！流程结束。")
                        return
                    save_oms_data(oms_groups)
                else:
                    first_id = supplier_ids[0]
                    logger.info(f"\n🔍 阶段A-第一轮：OMS系统 - 筛选待处理单据（供应商ID={first_id}）")
                    oms_groups = self.oms.run(supplier_id=first_id)

                    if not oms_groups:
                        logger.warning(f"OMS第一轮（供应商ID={first_id}）无待处理单据")
                        oms_groups = {}

                    save_oms_data(oms_groups)

            # 汇总显示
            logger.info("\nOMS 数据概览:")
            total_items = 0
            for group_key, items in oms_groups.items():
                total_items += len(items)
                logger.info(f"  项目 '{group_key}': {len(items)} 条物料")
            logger.info(f"  共 {len(oms_groups)} 组 / {total_items} 条物料\n")

            # 第一轮为空时，先尝试第二轮（不打开备件网，避免 OMS 标签被覆盖）
            skip_first_round = False
            if not self.skip_oms and not oms_groups and OMS_PHASE1_SUPPLIER_IDS and len(OMS_PHASE1_SUPPLIER_IDS) > 1:
                second_id = OMS_PHASE1_SUPPLIER_IDS[1]
                logger.info(f"🔍 第一轮无数据，直接尝试第二轮（供应商ID={second_id}）")
                oms_groups = self.oms.refilter_and_extract(second_id)
                if oms_groups:
                    save_oms_data(oms_groups)
                    skip_first_round = True
                    logger.info(f"  第二轮共 {len(oms_groups)} 组\n")
                else:
                    logger.warning(f"  第二轮也无待处理单据，流程结束。")
                    return

            if not oms_groups:
                logger.warning("OMS中无待处理单据！流程结束。")
                return

            review_groups = sum(
                1
                for _k, its in oms_groups.items()
                if its and any(x.get("group_needs_review") for x in its)
            )
            if review_groups:
                logger.warning(
                    f"  有 {review_groups} 组无项目名且需人工确认是否为同一项目"
                )

            if self.skip_oms:
                self.confirm_step("加载OMS缓存 -> 启动浏览器并登录备件网")
                self.browser.start()

            # ============================================================
            # 阶段B：备件网 — 登录
            # ============================================================
            self.confirm_step("打开备件网并登录")
            logger.info("\n🔧 阶段B：备件网 - 登录系统")

            if not self.skip_oms:
                try:
                    self._oms_window_handle = self.browser.driver.current_window_handle
                except Exception:
                    self._oms_window_handle = None
                self._spareparts_window_handle = self.browser.open_new_tab(
                    SPAREPARTS_URL
                )
            else:
                self.browser.navigate(SPAREPARTS_URL)
                try:
                    self._spareparts_window_handle = (
                        self.browser.driver.current_window_handle
                    )
                except Exception:
                    self._spareparts_window_handle = None

            self.spareparts = SparePartsModule(self.browser, self.user_config)
            self.spareparts.manual_photo_pause_callback = (
                self._pause_spareparts_photo_upload
            )
            try:
                self.spareparts._wait_spareparts_host_loaded()
            except Exception as e:
                logger.warning(f"  备件网页面加载异常，重试导航: {e}")
                self.browser.ensure_valid_window(self._spareparts_window_handle)
                self.browser.navigate(SPAREPARTS_URL)
                try:
                    self._spareparts_window_handle = (
                        self.browser.driver.current_window_handle
                    )
                except Exception:
                    pass
            self.spareparts.login()

            self.confirm_step("备件网登录完成 -> 开始新建询价单")

            # ============================================================
            # 阶段C：逐组创建询价单（仅创建，不等待审批）
            # ============================================================
            logger.info("\n📝 阶段C：逐组创建询价单（将保存结果供 --resume 续接）")

            if skip_first_round:
                # 第一轮为空，直接处理第二轮数据
                second_id = OMS_PHASE1_SUPPLIER_IDS[1]
                inquiry_results = self._process_oms_groups(
                    oms_groups, f"第二轮({second_id})"
                )
            else:
                # ── 第一轮：处理已获取的 OMS 分组 ──
                first_round_label = ""
                if not self.skip_oms and OMS_PHASE1_SUPPLIER_IDS:
                    first_round_label = f"第一轮({OMS_PHASE1_SUPPLIER_IDS[0]})"
                inquiry_results = self._process_oms_groups(oms_groups, first_round_label)

                # ── 第二轮（仅在线模式 + 配置了多供应商ID）──
                if not self.skip_oms and OMS_PHASE1_SUPPLIER_IDS and len(OMS_PHASE1_SUPPLIER_IDS) > 1:
                    second_id = OMS_PHASE1_SUPPLIER_IDS[1]
                    logger.info(f"\n🔍 阶段A-第二轮：OMS系统 - 筛选待处理单据（供应商ID={second_id}）")

                    oms_handle = self._oms_window_handle
                    if oms_handle:
                        try:
                            self.browser.driver.switch_to.window(oms_handle)
                        except Exception:
                            logger.warning("  OMS 标签已关闭，无法执行第二轮筛选")
                            oms_handle = None

                    if oms_handle:
                        oms_groups_2 = self.oms.refilter_and_extract(second_id)
                        if oms_groups_2:
                            existing = load_oms_data() or {}
                            existing.update(oms_groups_2)
                            save_oms_data(existing)

                            second_label = f"第二轮({second_id})"
                            results_2 = self._process_oms_groups(oms_groups_2, second_label)
                            inquiry_results.extend(results_2)
                        else:
                            logger.info(f"  OMS第二轮（供应商ID={second_id}）无待处理单据，跳过")
                    else:
                        logger.warning("  跳过第二轮：无法访问 OMS 标签")

            # 保存查询结果供 --resume 使用
            if inquiry_results:
                save_inquiry_results(inquiry_results)
                _log_results_to_sessions_excel(inquiry_results)
            else:
                logger.warning("没有成功创建任何询价单，不保存结果文件")

        except AbortRun as e:
            logger.warning(f"\n操作员终止: {e}")
        except KeyboardInterrupt:
            logger.warning("\n用户中断流程")
        except Exception as e:
            logger.error(f"流程异常: {e}", exc_info=True)
        finally:
            if self.error_collector:
                self.error_collector.print_summary()
            self._teardown_browser()

    def _run_resume_phase(self):
        """
        【第二阶段 --resume】从 inquiry_results.json 加载询价单结果 →
        逐一检查审批状态 → 已完结的加入购物车 → 生成报价单
        
        未完结的询价单会被跳过，需等待审批完成后再次运行 --resume。
        """
        logger.info("=" * 60)
        logger.info("  询价单自动化系统 — 第二阶段：审批→购物车→报价单")
        logger.info("=" * 60)

        # 加载第一阶段保存的询价单结果（路径可含子目录）
        ir_path = os.path.abspath(self.inquiry_results_path)
        inquiry_data = load_inquiry_results(ir_path)
        if inquiry_data is None:
            logger.error(f"未找到询价单结果文件: {ir_path}")
            logger.info("  常见原因：")
            logger.info("    ① 尚未跑阶段一：请先执行  python main.py  生成 inquiry_results.json")
            logger.info("    ② OMS 已非「待处理」无法重做阶段一：请用手工 JSON 续跑（见下）")
            logger.info("    ③ 上次阶段二已全部跑完：可使用 inquiry_last.json 或重跑阶段一")
            logger.info("  续跑方式：")
            logger.info("    · 直接 python main.py --resume（会自动找 inquiry_results.json / inquiry_last.json）")
            logger.info("    · 或复制 inquiry_results.example.json 为 inquiry_results.json 后填写")
            return
        if len(inquiry_data) == 0:
            logger.error(f"{ir_path} 存在，但 inquiries 列表为空，无法继续。")
            logger.info("  请按 inquiry_results.example.json 补全后重试。")
            return

        self._resume_initial_inquiries = copy.deepcopy(inquiry_data)
        pending_inquiries = inquiry_data  # 待处理的询价单列表
        
        try:
            self.browser.start()
            self.browser.navigate(SPAREPARTS_URL)
            self.spareparts = SparePartsModule(self.browser, self.user_config)
            self.spareparts.manual_photo_pause_callback = (
                self._pause_spareparts_photo_upload
            )
            self.spareparts.login()
            self._resume_reset_oms_enrichment_state()

            logger.info(f"\n共 {len(pending_inquiries)} 组询价单待检查审批状态")
            logger.info("-" * 40)
            
            still_pending = []  # 仍然未完结的

            for i, inquiry in enumerate(pending_inquiries, 1):
                inquiry_no = inquiry["inquiry_no"]
                ladder_no = inquiry["ladder_no"]
                project_name = inquiry["project_name"]
                factory_type = inquiry["factory_type"]
                oms_address = inquiry.get("oms_address", "")
                material_count = inquiry.get("material_count", 0)

                logger.info(f"\n[{i}/{len(pending_inquiries)}] 询价单 {inquiry_no} ({ladder_no})")

                # 用 resume_single_inquiry 完成: 检查审批 → 查看 → 加入购物车
                oms_group_key = inquiry.get("oms_group_key", "")
                line_descs_pre = (
                    self._get_oms_line_material_descs(
                        project_name, ladder_no, oms_group_key=oms_group_key
                    )
                    or []
                )
                material_hint = line_descs_pre[0] if line_descs_pre else ""
                cart_qty_pre = inquiry.get("oms_cart_total_quantity")
                if cart_qty_pre is None:
                    cart_qty_pre = self._get_oms_cart_total_quantity(
                        project_name, ladder_no, oms_group_key=oms_group_key
                    )
                if cart_qty_pre is None and material_count == 1:
                    cart_qty_pre = 1
                cart_line_qtys_pre = self._get_oms_line_quantities(
                    project_name, ladder_no, oms_group_key=oms_group_key
                )
                cart_line_descs_pre = line_descs_pre
                cart_qty_align = (
                    SparePartsModule.resolve_cart_quantity_target_for_align(
                        cart_qty_pre, cart_line_qtys_pre
                    )
                )
                try:
                    resume_result = self._run_with_manual_recovery(
                        f"审批/购物车: 询价单 {inquiry_no}",
                        lambda: self._step_or_raise(
                            f"审批/购物车: 询价单 {inquiry_no}",
                            lambda: self.spareparts.resume_single_inquiry(
                                inquiry_no=inquiry_no,
                                project_name=project_name,
                                ladder_no=ladder_no,
                                material_desc=material_hint,
                                cart_quantity_target=cart_qty_align,
                                material_count=material_count,
                                cart_line_quantities=cart_line_qtys_pre,
                                cart_line_material_descs=cart_line_descs_pre,
                            ),
                        ),
                        recover_on_done=lambda **_: self._recover_resume_cart_after_manual(
                            inquiry
                        ),
                        extra_hints=[
                            "请在备件网完成：审批已完结、物料在购物车、各行数量与 OMS 一致",
                            "改数量后务必点「更新购物车」",
                            "完成后 [Enter] 继续；[r] 重试自动对齐；启动器点 ▶ 继续",
                            "[s] 跳过本询价单；[q] 退出整场运行",
                        ],
                    )
                except SkipCurrentGroup:
                    still_pending.append(inquiry)
                    continue

                if not resume_result["approved"]:
                    logger.info(f"  → 尚未完结，保留到下次 --resume")
                    still_pending.append(inquiry)
                    continue

                # 工厂类型：备注 PO（详情页 ➕ 展开）> inquiry_results / OMS 供应商
                json_factory = factory_type
                po_factory = resume_result.get("po_factory")
                if po_factory:
                    if po_factory != json_factory:
                        logger.warning(
                            f"  OMS 供应商/工厂({json_factory})与备注 PO({po_factory})不一致"
                        )
                        target_supplier = OMSModule.supplier_name_for_factory(
                            po_factory
                        )
                        self.confirm_step(
                            f"OMS 扩展供应商（备注 PO={po_factory}）\n"
                            f"  项目: {project_name[:50]}…\n"
                            f"  目标: {target_supplier}"
                        )
                        if not self._extend_oms_supplier_for_po(
                            project_name,
                            inquiry.get("oms_sourcing_no", ""),
                            target_supplier,
                            inquiry=inquiry,
                        ):
                            inquiry["resume_error"] = (
                                "OMS 扩展供应商失败：PO 与 OMS 供应商不一致"
                            )
                            still_pending.append(inquiry)
                            continue
                        inquiry["oms_supplier"] = target_supplier
                        inquiry["factory_type"] = po_factory
                        factory_type = po_factory
                        logger.info(
                            f"  ✓ 已按 PO 扩展 OMS 供应商为: {target_supplier}；"
                            f"待处理新行已发邮件；备件网阶段二继续（不重建询价单）"
                        )
                    else:
                        factory_type = po_factory
                        inquiry["factory_type"] = po_factory
                else:
                    logger.info(
                        f"  工厂沿用 inquiry_results/OMS: {factory_type}"
                        f"（未能从备注 PO 识别，请检查 ➕ 备注栏）"
                    )

                # 已完结且已加入购物车 → 生成报价单
                self.confirm_step(f"询价单 {inquiry_no} 已完结 -> 生成报价单 (工厂: {factory_type})")

                try:
                    def _run_quotation_step():
                        cart_qty = inquiry.get("oms_cart_total_quantity")
                        if cart_qty is not None:
                            try:
                                cart_qty = int(float(str(cart_qty).strip()))
                            except (ValueError, TypeError):
                                cart_qty = None
                        oms_group_key = inquiry.get("oms_group_key", "")
                        if cart_qty is None:
                            cart_qty = self._get_oms_cart_total_quantity(
                                project_name,
                                ladder_no,
                                oms_group_key=oms_group_key,
                            )
                        if cart_qty is None and material_count == 1:
                            cart_qty = 1

                        cart_line_qtys = self._get_oms_line_quantities(
                            project_name,
                            ladder_no,
                            oms_group_key=oms_group_key,
                        )
                        cart_line_descs = self._get_oms_line_material_descs(
                            project_name,
                            ladder_no,
                            oms_group_key=oms_group_key,
                        )
                        if cart_line_qtys:
                            logger.info(
                                f"  购物车数量逐行对齐(来自 oms_data): {cart_line_qtys}"
                            )
                        if cart_line_descs:
                            logger.info(
                                f"  购物车行与 OMS 物料描述匹配用: "
                                f"{len(cart_line_descs)} 条"
                            )
                        cart_qty_align = (
                            SparePartsModule.resolve_cart_quantity_target_for_align(
                                cart_qty, cart_line_qtys
                            )
                        )

                        direct_for_quote = (
                            inquiry.get("oms_direct_address", "").strip()
                            or oms_address
                        )
                        zhongshan_missing = (
                            self._zhongshan_phase1_missing_fields(inquiry)
                            if factory_type == "中山"
                            else []
                        )

                        if zhongshan_missing:
                            self.spareparts.open_quotation_edit_form(
                                material_count=material_count,
                                cart_quantity_target=cart_qty_align,
                                cart_line_quantities=cart_line_qtys,
                                cart_line_material_descs=cart_line_descs,
                            )
                            self._pause_zhongshan_manual_fill(
                                inquiry, zhongshan_missing
                            )
                            return self.spareparts.run_quotation(
                                factory_type=factory_type,
                                project_name=project_name,
                                ladder_no=ladder_no,
                                oms_address=oms_address,
                                material_count=material_count,
                                oms_recipient=inquiry.get("oms_recipient", ""),
                                oms_direct_address=direct_for_quote,
                                cart_quantity_target=cart_qty_align,
                                cart_line_quantities=cart_line_qtys,
                                cart_line_material_descs=cart_line_descs,
                                quotation_form_already_open=True,
                                zhongshan_after_manual_pause=True,
                            )
                        return self.spareparts.run_quotation(
                            factory_type=factory_type,
                            project_name=project_name,
                            ladder_no=ladder_no,
                            oms_address=oms_address,
                            material_count=material_count,
                            oms_recipient=inquiry.get("oms_recipient", ""),
                            oms_direct_address=direct_for_quote,
                            cart_quantity_target=cart_qty_align,
                            cart_line_quantities=cart_line_qtys,
                            cart_line_material_descs=cart_line_descs,
                        )

                    quotation_no = self._run_with_manual_recovery(
                        f"生成报价单: {inquiry_no} ({factory_type})",
                        _run_quotation_step,
                        recover_on_done=lambda **_: self._recover_quotation_after_manual(
                            inquiry
                        ),
                        extra_hints=[
                            "可在备件网手工完成报价单填写并生成单号",
                            "完成后 [Enter] 继续；启动器点 ▶ 继续",
                        ],
                    )
                    if not quotation_no:
                        quotation_no = self.spareparts.get_quotation_number()
                    if not (quotation_no or "").strip():

                        def _read_quotation_no_or_raise():
                            q = ""
                            if self.spareparts:
                                q = (
                                    self.spareparts.get_quotation_number() or ""
                                ).strip()
                            if not q:
                                raise RuntimeError(
                                    "仍未读到报价单号，请在备件网保存报价单后"
                                    "选 [r] 重试或 [Enter] 继续"
                                )
                            return q

                        quotation_no = self._run_with_manual_recovery(
                            f"报价单号缺失: {inquiry_no}",
                            _read_quotation_no_or_raise,
                            recover_on_done=lambda **_: self._recover_quotation_after_manual(
                                inquiry
                            ),
                            extra_hints=[
                                "请在备件网保存报价单后，再点继续",
                            ],
                        )
                    inquiry["quotation_no"] = quotation_no
                    if quotation_no:
                        try:
                            pending_path = os.path.abspath(INQUIRY_RESULTS_FILE)
                            existing = load_inquiry_results(pending_path) or []
                            for inv in existing:
                                if inv.get("inquiry_no") == inquiry_no:
                                    inv["quotation_no"] = quotation_no
                                    break
                            else:
                                existing.append(dict(inquiry))
                            save_inquiry_results(existing, pending_path)
                        except Exception as ex:
                            logger.debug(f"写回 quotation_no 到 JSON 失败: {ex}")

                    self.results.append({
                        "ladder_no": ladder_no,
                        "project_name": project_name,
                        "factory_type": factory_type,
                        "inquiry_no": inquiry_no,
                        "quotation_no": quotation_no,
                        "material_count": material_count,
                        "success": True,
                    })
                    logger.info(f"  ✓ 报价单: {quotation_no}")

                except Exception as e:
                    logger.error(f"  报价单生成失败: {e}")
                    still_pending.append(inquiry)

            # 更新 inquiry_results.json（移除已处理的，保留未完结的）
            if still_pending:
                logger.info(f"\n{'=' * 60}")
                logger.info(f"  仍有 {len(still_pending)} 组询价单未完结:")
                for p in still_pending:
                    logger.info(f"    - {p['inquiry_no']} ({p['ladder_no']})")
                logger.info(f"  请等待审批完成后再次运行: python main.py --resume")
                logger.info(f"{'=' * 60}")
                pending_path = os.path.abspath(INQUIRY_RESULTS_FILE)
                replace_inquiry_results(still_pending, pending_path)
                if pending_path != ir_path:
                    logger.info(f"  未完结列表已写入: {pending_path}")
            else:
                # 用当前 inquiry_data（含扩展供应商、工厂等循环内更新）
                arch_inquiries = copy.deepcopy(inquiry_data)
                by_no = {r["inquiry_no"]: r for r in self.results if r.get("inquiry_no")}
                for inv in arch_inquiries:
                    r = by_no.get(inv.get("inquiry_no"))
                    if r:
                        inv["quotation_no"] = r.get("quotation_no")
                try:
                    save_inquiry_last_archive(arch_inquiries)
                    save_inquiry_results([])
                    _update_sessions_excel_for_inquiries(arch_inquiries)
                    logger.info(
                        f"  已清空 {INQUIRY_RESULTS_FILE}（待办已归档至 {INQUIRY_LAST_FILE}）"
                    )
                    logger.info(f"  本次加载自: {ir_path}")
                except Exception as e:
                    logger.warning(f"写入 {INQUIRY_LAST_FILE} 失败: {e}")

            # 打印汇总
            if self.results:
                self._print_summary()

        except AbortRun as e:
            logger.warning(f"\n操作员终止: {e}")
        except KeyboardInterrupt:
            logger.warning("\n用户中断流程")
        except Exception as e:
            logger.error(f"流程异常: {e}", exc_info=True)
        finally:
            if self.error_collector:
                self.error_collector.print_summary()
            self._teardown_browser()

    def _run_finalize_phase(self):
        """
        【第三阶段 --finalize】备件网导出 PDF → OMS 导出 Excel → 填表 → OMS 导入。
        """
        logger.info("=" * 60)
        logger.info("  询价单自动化系统 — 第三阶段：PDF/Excel 归档与导入")
        logger.info("=" * 60)

        ir_path = os.path.abspath(self.inquiry_results_path)
        inquiry_data = load_inquiry_results(ir_path)
        if inquiry_data is None:
            ir_path = resolve_inquiry_results_path()
            inquiry_data = load_inquiry_results(ir_path)
        if not inquiry_data:
            logger.error("未找到可处理的询价单 JSON，请先完成阶段二。")
            return

        output_dir = get_quote_output_dir(self.date_folder)
        logger.info(f"归档目录: {output_dir}")

        only_fill = self.finalize_only == "fill"
        only_pdf = self.finalize_only == "pdf"
        only_excel = self.finalize_only == "excel"
        only_import = self.finalize_only == "import"
        if not self.import_submit:
            logger.info(
                "  【测试模式】OMS 导入仅上传 PDF/Excel，不点「保存」及确认窗「确认」；"
                "请在浏览器中人工完成"
            )
        oms_handle = None
        spare_handle = None
        driver = None

        try:
            if not only_fill:
                self.browser.download_dir = output_dir
                self.browser.start(download_dir=output_dir)
                driver = self.browser.driver

                if only_excel or only_import:
                    if only_import:
                        step_hint = (
                            "仅导入 PDF/Excel（测试：不保存/确认）"
                            if not self.import_submit
                            else "仅导入 PDF/Excel（保存+确认）"
                        )
                    else:
                        step_hint = "仅导出 Excel 并归档"
                    self.confirm_step(f"打开 OMS（{step_hint}）")
                    self.browser.navigate(OMS_URL)
                    self.oms.prepare_finalize_session()
                    oms_handle = driver.current_window_handle
                elif only_pdf:
                    self.confirm_step("打开备件网（仅导出 PDF 并归档）")
                    self.browser.navigate(SPAREPARTS_URL)
                    self.spareparts = SparePartsModule(self.browser, self.user_config)
                    self.spareparts.login()
                    spare_handle = driver.current_window_handle
                else:
                    self.browser.navigate(SPAREPARTS_URL)
                    self.spareparts = SparePartsModule(self.browser, self.user_config)
                    self.spareparts.login()

                    self.confirm_step(
                        "打开 OMS 新标签（不筛待处理；每条将按「项目名称」列漏斗搜索）"
                    )
                    before = driver.window_handles[:]
                    driver.execute_script("window.open(arguments[0], '_blank');", OMS_URL)
                    time.sleep(1.5)
                    after = driver.window_handles
                    oms_handle = [h for h in after if h not in before][-1]
                    driver.switch_to.window(oms_handle)
                    self.oms.prepare_finalize_session()
                    spare_handle = before[0]

            for i, inquiry in enumerate(inquiry_data, 1):
                inquiry_no = inquiry.get("inquiry_no", "")
                project_name = inquiry.get("project_name", "")
                factory_type = inquiry.get("factory_type", DEFAULT_FACTORY)
                quotation_no = (inquiry.get("quotation_no") or "").strip()
                sourcing_no = (inquiry.get("oms_sourcing_no") or "").strip()

                if not sourcing_no:
                    self._enrich_inquiry_sourcing_no(inquiry)
                    sourcing_no = (inquiry.get("oms_sourcing_no") or "").strip()

                split = int(inquiry.get("project_split_count") or self.project_split_count or 1)
                oms_filt = self._oms_finalize_filter_kwargs(inquiry)
                filter_label = self._finalize_filter_label(
                    oms_filt["project_name"],
                    oms_filt["group_key"],
                    sourcing_no,
                )

                logger.info(f"\n[{i}/{len(inquiry_data)}] 询价单 {inquiry_no} 寻源 {sourcing_no}")

                if not sourcing_no:
                    inquiry["finalize_status"] = "failed"
                    inquiry["finalize_error"] = (
                        "缺少寻源单号：请在 inquiry JSON 填写 oms_sourcing_no，"
                        "或确保 oms_data.json 含「寻源单号」列（勿依赖 OMS 待处理列表补抓）"
                    )
                    logger.error(f"  {inquiry['finalize_error']}")
                    continue
                needs_pdf = not only_excel
                if needs_pdf and not (inquiry_no or "").strip():
                    inquiry["finalize_status"] = "failed"
                    inquiry["finalize_error"] = (
                        "缺少系统询价号：请在 inquiry JSON 填写 inquiry_no"
                    )
                    logger.error(f"  {inquiry['finalize_error']}")
                    continue
                needs_quotation = not only_fill and not only_excel and not only_import
                if not quotation_no and needs_quotation:
                    inquiry["finalize_status"] = "failed"
                    inquiry["finalize_error"] = "缺少报价单号"
                    logger.error("  缺少报价单号，跳过")
                    continue

                _, pdf_path, xlsx_path = target_paths(
                    sourcing_no, self.date_folder, pdf_base=quotation_no
                )

                try:
                    if only_fill:
                        if not os.path.isfile(pdf_path) or not os.path.isfile(xlsx_path):
                            raise FileNotFoundError(
                                f"缺少归档文件 pdf={pdf_path} xlsx={xlsx_path}"
                            )
                        self.confirm_step(f"仅填表: {sourcing_no}")
                        fill_excel_from_pdf(
                            pdf_path, xlsx_path,
                            factory_type=factory_type,
                            project_split_count=split,
                        )
                        inquiry["finalize_status"] = "fill_ok"
                        inquiry["pdf_path"] = pdf_path
                        inquiry["xlsx_path"] = xlsx_path
                        continue

                    if only_pdf:
                        self.confirm_step(
                            f"备件网导出 PDF: {quotation_no} → {pdf_path}（系统询价号）"
                        )
                        driver.switch_to.window(spare_handle)
                        since = time.time()
                        try:
                            self.spareparts.export_quotation_pdf(
                                quotation_no, project_name=project_name
                            )
                            wait_and_archive(output_dir, pdf_path, since_time=since)
                        finally:
                            self.spareparts.close_print_page()
                        inquiry["finalize_status"] = "pdf_ok"
                        inquiry["pdf_path"] = pdf_path
                        logger.info(f"  ✓ PDF 已归档: {pdf_path}")
                        continue

                    if only_excel:
                        self.confirm_step(
                            f"OMS 导出 Excel: {filter_label} / {sourcing_no}"
                        )
                        driver.switch_to.window(oms_handle)
                        self.oms.apply_finalize_list_filter(**oms_filt)
                        checked = self.oms.check_rows_for_finalize(**oms_filt)
                        if checked <= 0:
                            raise RuntimeError(
                                f"OMS 未勾选到行（寻源 {sourcing_no}）"
                            )
                        since = time.time()
                        self.oms.export_supplier_quote_excel()
                        wait_and_archive(output_dir, xlsx_path, since_time=since)
                        inquiry["finalize_status"] = "excel_ok"
                        inquiry["xlsx_path"] = xlsx_path
                        logger.info(f"  ✓ Excel 已归档: {xlsx_path}")
                        continue

                    if only_import:
                        if not os.path.isfile(pdf_path) or not os.path.isfile(xlsx_path):
                            raise FileNotFoundError(
                                f"缺少归档文件 pdf={pdf_path} xlsx={xlsx_path}"
                            )
                        submit_label = (
                            "保存→确认" if self.import_submit else "测试：仅上传"
                        )
                        self.confirm_step(
                            f"OMS 导入 PDF+Excel ({submit_label}): {sourcing_no}"
                        )
                        driver.switch_to.window(oms_handle)
                        self.oms.apply_finalize_list_filter(**oms_filt)
                        checked = self.oms.check_rows_for_finalize(**oms_filt)
                        if checked <= 0:
                            raise RuntimeError(
                                f"OMS 未勾选到行（寻源 {sourcing_no}）"
                            )
                        self.oms.import_supplier_quote_files(
                            pdf_path, xlsx_path, submit=self.import_submit
                        )
                        inquiry.pop("finalize_error", None)
                        inquiry["finalize_status"] = (
                            "ok" if self.import_submit else "import_dry_ok"
                        )
                        inquiry["pdf_path"] = pdf_path
                        inquiry["xlsx_path"] = xlsx_path
                        logger.info(
                            f"  ✓ 导入步骤完成 ({submit_label}): {sourcing_no}"
                        )
                        continue

                    self.confirm_step(f"备件网导出 PDF: {quotation_no}")
                    driver.switch_to.window(spare_handle)
                    since = time.time()
                    try:
                        self.spareparts.export_quotation_pdf(
                            quotation_no, project_name=project_name
                        )
                        wait_and_archive(output_dir, pdf_path, since_time=since)
                    finally:
                        self.spareparts.close_print_page()

                    self.confirm_step(
                        f"OMS：已发送→Sourcing8ID→分组列 筛选并勾选 → 导出 Excel\n"
                        f"  {filter_label}\n"
                        f"  寻源: {sourcing_no}"
                    )
                    driver.switch_to.window(oms_handle)
                    self.oms.apply_finalize_list_filter(**oms_filt)
                    checked = self.oms.check_rows_for_finalize(**oms_filt)
                    if checked <= 0:
                        raise RuntimeError(
                            f"OMS 未勾选到行（寻源 {sourcing_no}）"
                        )
                    since = time.time()
                    self.oms.export_supplier_quote_excel()
                    wait_and_archive(output_dir, xlsx_path, since_time=since)

                    self.confirm_step(f"填写 Excel: {sourcing_no}")
                    fill_excel_from_pdf(
                        pdf_path, xlsx_path,
                        factory_type=factory_type,
                        project_split_count=split,
                    )

                    self.confirm_step(
                        f"OMS 导入（沿用导出 Excel 同一页勾选）: {sourcing_no}"
                    )
                    driver.switch_to.window(oms_handle)
                    checked = self.oms.ensure_checked_for_import_reuse_page(**oms_filt)
                    if checked <= 0:
                        raise RuntimeError(
                            "OMS 导入前未勾选到任何行：请确认仍在导出 Excel 时的列表页，"
                            "且左侧已勾选；若页面已刷新，请重新跑本组阶段三或手工勾选后"
                            "使用 --finalize-only import"
                        )
                    self.oms.import_supplier_quote_files(
                        pdf_path, xlsx_path, submit=self.import_submit
                    )

                    inquiry.pop("finalize_error", None)
                    inquiry["finalize_status"] = (
                        "ok" if self.import_submit else "import_dry_ok"
                    )
                    inquiry["pdf_path"] = pdf_path
                    inquiry["xlsx_path"] = xlsx_path
                    logger.info(f"  ✓ 第三阶段完成: {sourcing_no}")

                except Exception as e:
                    inquiry["finalize_status"] = "failed"
                    inquiry["finalize_error"] = str(e)
                    logger.error(f"  本组第三阶段失败: {e}", exc_info=True)

            save_finalize_results(inquiry_data)
            ok_statuses = ("ok", "fill_ok")
            all_finalize_ok = bool(inquiry_data) and all(
                (inv.get("finalize_status") or "") in ok_statuses
                for inv in inquiry_data
            )
            if all_finalize_ok:
                try:
                    save_inquiry_last_archive(inquiry_data)
                    save_inquiry_results([])
                    _update_sessions_excel_for_inquiries(inquiry_data)
                    logger.info(
                        f"  阶段三全部成功，已更新 {INQUIRY_LAST_FILE} 并清空待办 JSON"
                    )
                except Exception as e:
                    logger.warning(f"写入 {INQUIRY_LAST_FILE} 失败: {e}")
            else:
                failed = [
                    inv
                    for inv in inquiry_data
                    if (inv.get("finalize_status") or "") not in ok_statuses
                ]
                if failed:
                    save_inquiry_results(failed)
                    logger.info(
                        f"  阶段三未全部成功：未覆盖 {INQUIRY_LAST_FILE}；"
                        f"  {len(failed)} 条待重试已写入 {INQUIRY_RESULTS_FILE}"
                    )
                else:
                    logger.info(
                        f"  阶段三未全部成功，未覆盖 {INQUIRY_LAST_FILE}"
                    )
            logger.info(f"\n归档目录: {output_dir}")

        except KeyboardInterrupt:
            logger.warning("\n用户中断流程")
        except Exception as e:
            logger.error(f"第三阶段异常: {e}", exc_info=True)
        finally:
            if inquiry_data:
                try:
                    save_finalize_results(inquiry_data)
                except Exception:
                    pass
            if self.error_collector:
                self.error_collector.print_summary()
            self._teardown_browser()

    def _resume_reset_oms_enrichment_state(self):
        """阶段二开始时重置：OMS 文件缓存、现场拉取结果只尝试一次。"""
        self._oms_groups_file_cache = None
        self._oms_live_groups = None
        self._oms_live_fetch_attempted = False

    @staticmethod
    def _zhongshan_phase1_missing_fields(inquiry):
        """
        阶段一写入 inquiry 的中山必填项是否缺失（仅看 JSON，不读 OMS、不补 oms_data）。
        返回缺失项列表：recipient / direct_address。
        """
        missing = []
        if not (inquiry.get("oms_recipient") or "").strip():
            missing.append("recipient")
        if not (inquiry.get("oms_direct_address") or "").strip():
            missing.append("direct_address")
        return missing

    def _pause_zhongshan_manual_fill(self, inquiry, missing):
        """报价页已打开；阻塞至操作员在页面填完并按 Enter（同一次运行内继续）。"""
        inquiry_no = inquiry.get("inquiry_no", "")
        project_name = (inquiry.get("project_name") or "")[:60]
        lines = []
        if "recipient" in missing:
            lines.append(
                "  · 技术员 / 技术员联系方式（阶段一未提取到）"
                " → 报价页任选其一：姓一栏写「姓名+电话」；或姓/名/电话分栏；或姓+电话"
            )
        if "direct_address" in missing:
            lines.append(
                "  · 直发地址（阶段一未提取到）"
                " → 请在报价页填写「收货地址」；若只填了详细地址，"
                "程序将自动补全省/市"
            )
        logger.info("")
        logger.info("=" * 72)
        logger.info("【需人工处理】中山报价单 — PO 识别为中山，阶段一 OMS 数据缺失")
        logger.info(f"  询价单: {inquiry_no}")
        logger.info(f"  项目: {project_name}")
        for line in lines:
            logger.info(line)
        logger.info("  说明:")
        logger.info("    1. 可在 OMS 补全列（便于以后重跑阶段一）；本程序续跑不会再去读 OMS。")
        logger.info("    2. 请在已打开的「生成报价单 / OrderPreview」页面完成填写。")
        logger.info("    3. 完成后回到本窗口按 Enter，程序将继续（无需重跑阶段一）。")
        logger.info("=" * 72)
        try:
            input("\n>>> 已在报价页面填写完毕？按 Enter 继续程序… ")
        except (EOFError, KeyboardInterrupt):
            logger.info("  (非交互环境，自动继续)")
        logger.info("")

    @staticmethod
    def _lookup_oms_group_items(
        groups, project_name="", ladder_no="", oms_group_key=""
    ):
        """
        从 oms_data 分组 dict 取物料行列表。
        优先项目名 → 阶段一 oms_group_key（如 寻源:xxx）→ 梯号（旧版键）。
        """
        if not groups:
            return None
        pn = (project_name or "").strip()
        if pn and pn in groups:
            return groups[pn]
        gk = (oms_group_key or "").strip()
        if gk and gk in groups:
            return groups[gk]
        ln = (ladder_no or "").strip()
        if ln and ln in groups:
            return groups[ln]
        return None

    def _enrich_inquiry_from_oms_data_file(self, inquiry):
        """
        用阶段一保存的 oms_data.json 按项目名（或梯号）补全 inquiry 中的 oms_recipient / oms_direct_address。
        解决旧版 inquiry_results 无这两列、或阶段一未写入 JSON 的情况。
        """
        if self._oms_groups_file_cache is None:
            self._oms_groups_file_cache = load_oms_data() or {}
        groups = self._oms_groups_file_cache
        items = self._lookup_oms_group_items(
            groups,
            project_name=inquiry.get("project_name", ""),
            ladder_no=inquiry.get("ladder_no", ""),
        )
        if not items:
            return
        if not (inquiry.get("oms_recipient") or "").strip():
            line = self._build_oms_recipient_line(items)
            if line:
                inquiry["oms_recipient"] = line
                logger.info(f"  已从 oms_data.json 补全收货人(技术员+联系方式)")
        if not (inquiry.get("oms_direct_address") or "").strip():
            direct = self._get_oms_direct_address(items)
            if direct:
                inquiry["oms_direct_address"] = direct
                logger.info(f"  已从 oms_data.json 补全直发地址")

    def _apply_oms_groups_to_inquiry(self, inquiry, groups):
        """将 OMS 分组 dict（项目名 -> 物料行列表）合并到单条 inquiry。"""
        if not groups:
            return
        items = self._lookup_oms_group_items(
            groups,
            project_name=inquiry.get("project_name", ""),
            ladder_no=inquiry.get("ladder_no", ""),
        )
        if not items:
            return
        if not (inquiry.get("oms_recipient") or "").strip():
            line = self._build_oms_recipient_line(items)
            if line:
                inquiry["oms_recipient"] = line
                logger.info(f"  已从 OMS 现场数据补全收货人")
        if not (inquiry.get("oms_direct_address") or "").strip():
            direct = self._get_oms_direct_address(items)
            if direct:
                inquiry["oms_direct_address"] = direct
                logger.info(f"  已从 OMS 现场数据补全直发地址")

    def _open_oms_tab_and_run_filter(self):
        """
        在当前备件网标签之外新开 OMS 标签，执行 OMS 筛选与提取（含 Sourcing 关键字），然后关闭该标签并切回备件网。
        若该行在 OMS 已非「待处理」，可能仍拿不到数据。
        """
        driver = self.browser.driver
        before = driver.window_handles[:]
        spare_handle = before[0]
        logger.info("  正在打开 OMS 新标签页以补全中山报价字段…")
        driver.execute_script("window.open(arguments[0], '_blank');", OMS_HOME_URL)
        time.sleep(1.5)
        after = driver.window_handles
        new_tabs = [h for h in after if h not in before]
        if not new_tabs:
            logger.error("  未能打开 OMS 新标签页")
            return None
        oms_handle = new_tabs[-1]
        driver.switch_to.window(oms_handle)
        try:
            from config import PHASE2_OMS_FILTER_STATUS_SENT

            return self.oms.run(
                sourcing_filter=True,
                status_pending=not PHASE2_OMS_FILTER_STATUS_SENT,
                status_sent=PHASE2_OMS_FILTER_STATUS_SENT,
            )
        except Exception as e:
            logger.error(f"  OMS 筛选/提取失败: {e}", exc_info=True)
            return None
        finally:
            try:
                driver.close()
            except Exception:
                pass
            time.sleep(0.4)
            try:
                driver.switch_to.window(spare_handle)
            except Exception:
                if driver.window_handles:
                    driver.switch_to.window(driver.window_handles[0])

    def _ensure_zhongshan_oms_fields(self, inquiry):
        """
        中山生成报价单前：先读 oms_data.json；仍缺则本阶段仅一次新开 OMS 执行 run() 再合并。
        """
        self._enrich_inquiry_from_oms_data_file(inquiry)
        if not self._zhongshan_oms_fields_incomplete(inquiry):
            return
        if self._oms_live_groups is not None:
            self._apply_oms_groups_to_inquiry(inquiry, self._oms_live_groups)
            if not self._zhongshan_oms_fields_incomplete(inquiry):
                return
        if self._oms_live_fetch_attempted:
            logger.warning(
                "  中山报价仍缺收货人或直发地址，且本阶段已尝试过 OMS 现场拉取；"
                "若该行已非「待处理」，请保留 oms_data.json 或重跑阶段一。"
            )
            return
        self._oms_live_fetch_attempted = True
        self.confirm_step(
            "打开 OMS 新标签补抓技术员/直发地址（需仍为待处理；完成后将自动回到备件网）"
        )
        self._oms_live_groups = self._open_oms_tab_and_run_filter() or {}
        self._apply_oms_groups_to_inquiry(inquiry, self._oms_live_groups)
        if self._zhongshan_oms_fields_incomplete(inquiry):
            logger.warning(
                "  中山 OMS 字段仍不完整。可检查：1) oms_data.json 是否含该梯号  "
                "2) OMS 列表是否仍有该待处理行  3) 表头是否含技术员/直发地址列"
            )

    # ====================================================================
    # 辅助方法
    # ====================================================================
    @staticmethod
    def _extract_oms_sourcing_no(items):
        """从 OMS 物料条目取寻源单号（须像 PSM…，排除误读的 1/2）。"""
        from modules.oms import OMSModule

        for item in items:
            sn = (item.get("oms_sourcing_no") or item.get("寻源单号") or "").strip()
            if sn and OMSModule._is_plausible_sourcing_no(sn):
                return sn
        return ""

    def _enrich_inquiry_sourcing_no(self, inquiry):
        """从 oms_data.json 按项目名（或梯号）补全 oms_sourcing_no。"""
        from modules.oms import OMSModule

        existing = (inquiry.get("oms_sourcing_no") or "").strip()
        if existing and OMSModule._is_plausible_sourcing_no(existing):
            return
        groups = load_oms_data()
        items = self._lookup_oms_group_items(
            groups,
            project_name=inquiry.get("project_name", ""),
            ladder_no=inquiry.get("ladder_no", ""),
        )
        if not items:
            return
        sn = self._extract_oms_sourcing_no(items)
        if sn:
            inquiry["oms_sourcing_no"] = sn
            logger.info(f"  已从 oms_data 补全寻源单号: {sn}")

    def _oms_finalize_filter_kwargs(self, inquiry):
        """
        阶段三 OMS 筛选/勾选参数，列选择规则与阶段一 resolve_group_oms_email_filter 一致。
        """
        project_name = inquiry.get("project_name", "")
        sourcing_no = (inquiry.get("oms_sourcing_no") or "").strip()
        group_key = (inquiry.get("oms_group_key") or "").strip()
        if self._oms_groups_file_cache is None:
            try:
                self._oms_groups_file_cache = load_oms_data() or {}
            except Exception:
                self._oms_groups_file_cache = {}
        items = self._lookup_oms_group_items(
            self._oms_groups_file_cache,
            project_name=project_name,
            ladder_no=inquiry.get("ladder_no", ""),
            oms_group_key=group_key,
        )
        if items is None:
            items = [{
                "project_name": project_name,
                "oms_sourcing_no": sourcing_no,
                "ladder_no": inquiry.get("ladder_no", ""),
                "直发地址": (
                    inquiry.get("oms_direct_address")
                    or inquiry.get("oms_address")
                    or ""
                ),
                "oms_system_inquiry_no": inquiry.get("inquiry_no", ""),
            }]
        return {
            "project_name": project_name,
            "sourcing_no": sourcing_no,
            "group_key": group_key,
            "items": items,
        }

    @staticmethod
    def _finalize_filter_label(project_name, group_key, sourcing_no):
        if (project_name or "").strip():
            pn = project_name.strip()
            return f"项目: {pn[:40]}{'…' if len(pn) > 40 else ''}"
        if (group_key or "").strip():
            return f"分组: {group_key}"
        return f"寻源: {sourcing_no}"

    def _extract_project_name(self, items):
        """提取项目名称"""
        for item in items:
            name = item.get("project_name", "")
            if name and name.strip():
                return name.strip()
        # 如果没有项目名称，返回空，后面会用梯号代替
        return ""

    @staticmethod
    def _extract_primary_ladder_no(items):
        """组内第一个非空梯号（仅作 JSON/日志兜底；备件网按每条物料各自的梯号填写）。"""
        for item in items or []:
            l = (item.get("ladder_no") or item.get("梯号") or "").strip()
            if l:
                return l
        return ""

    @staticmethod
    def _format_group_ladder_note(items):
        """汇总组内梯号，用于日志与确认提示。"""
        ladders = []
        for item in items or []:
            l = (item.get("ladder_no") or item.get("梯号") or "").strip()
            if l and l not in ladders:
                ladders.append(l)
        if not ladders:
            return "梯号 —"
        if len(ladders) == 1:
            return f"梯号 {ladders[0]}"
        return f"梯号 {len(ladders)} 个: {', '.join(ladders)}（逐条填写）"

    def _normalize_quantity(self, qty):
        """
        规范化单个数量值为整数字符串

        OMS显示如 "12.000"（三位小数），需转成 "12"，
        否则填入表单时小数点被去掉会变成 12000。
        """
        if qty and str(qty).strip():
            try:
                return str(int(float(str(qty).strip())))
            except (ValueError, TypeError):
                return str(qty).strip()
        return ""

    def _collect_photos(self, items, project_name):
        """收集当前项目的 OMS 附件照片；禁止复用其他项目缓存目录中的文件。"""
        from modules.oms import OMSModule

        project_name = (project_name or "").strip()
        photos = []
        for item in items:
            bound = (item.get("attachment_project") or "").strip()
            if bound and project_name and bound != project_name:
                logger.warning(
                    f"  跳过他项附件（归属项目与当前「{project_name[:30]}」不一致）"
                )
                continue
            row_paths = []
            for att in item.get("attachments") or []:
                p = str(att or "").strip()
                if not p or p in photos:
                    continue
                if project_name and OMSModule.path_belongs_to_project(p, project_name):
                    ap = os.path.abspath(p)
                    photos.append(ap)
                    row_paths.append(ap)
                elif project_name:
                    logger.warning(
                        f"  跳过非本项目附件: {os.path.basename(p)}"
                    )
                elif os.path.isfile(p):
                    ap = os.path.abspath(p)
                    photos.append(ap)
                    row_paths.append(ap)
            if row_paths:
                continue
            if not item.get("has_attachment"):
                continue

        if photos:
            from utils.image_files import pick_distinct_photo_files

            photos = pick_distinct_photo_files(photos, max_count=8)
            if photos:
                logger.info(
                    f"  使用本项目 OMS 附件 {len(photos)} 张（去重后）"
                )
                for i, p in enumerate(photos, 1):
                    logger.info(f"    照片{i}: {os.path.basename(p)}")
                logger.info(
                    f"  查看目录: {OMSModule.project_attachments_dir(project_name)}"
                )
                return photos
            logger.warning(
                "  OMS 标记有附件但未得到有效图片，改用模板照片"
            )

        return self._load_template_photos()

    def _load_template_photos(self):
        """OMS 无附件 / 附件无效时，使用 assets/photos 固定模板图。"""
        from utils.image_files import load_template_photo_paths

        template_dir = os.path.join(
            os.path.dirname(__file__), TEMPLATE_PHOTOS_DIR
        )
        photos = load_template_photo_paths(template_dir)
        if photos:
            logger.info(f"  OMS无可用附件，使用 {len(photos)} 张模板照片")
            for i, p in enumerate(photos, 1):
                logger.info(f"    模板{i}: {os.path.basename(p)}")
        else:
            logger.warning(f"  模板照片目录不存在或为空: {template_dir}")
        return photos

    @staticmethod
    def _extract_item_remark(item):
        """单条 OMS 物料的备注（含出口木箱包装标识）。"""
        if not item:
            return ""
        remark = (
            item.get("oms_remark")
            or item.get("备注")
            or item.get("remark")
            or ""
        ).strip()
        # 检查是否需要出口木箱包装
        export_box = (
            item.get("export_wooden_box")
            or item.get("是否需要出口木箱包装")
            or ""
        ).strip().upper()
        if export_box == "Y":
            box_note = "需要出口木箱包装"
            if remark:
                remark = f"{remark}；{box_note}"
            else:
                remark = box_note
        return remark

    @staticmethod
    def _extract_oms_remark(items):
        """OMS「备注」列 → 备件网新建询价单备注栏（组内第一条非空）。"""
        for item in items or []:
            remark = InquiryAutomation._extract_item_remark(item)
            if remark:
                return remark
        return ""

    @staticmethod
    def _extract_oms_supplier(items):
        """阶段一 OMS「供应商」列映射到本地工厂类型。"""
        for item in items or []:
            supplier = (
                item.get("供应商", "")
                or item.get("supplier", "")
                or ""
            ).strip()
            if supplier:
                return supplier
        return ""

    def _extend_oms_supplier_for_po(
        self, project_name, sourcing_no, target_supplier, inquiry=None
    ):
        """
        阶段二内嵌 OMS 补跑：扩展供应商 → 删已发送 → 待发处理行发邮件。
        不运行 _run_create_phase / 不点启动器「阶段一」；不重建备件网询价单。
        """
        driver = self.browser.driver
        before = driver.window_handles[:]
        spare_handle = before[0]
        logger.info("  打开 OMS 标签页以扩展供应商…")
        driver.execute_script("window.open(arguments[0], '_blank');", OMS_HOME_URL)
        time.sleep(1.5)
        after = driver.window_handles
        new_tabs = [h for h in after if h not in before]
        if not new_tabs:
            logger.error("  未能打开 OMS 新标签页")
            return False
        oms_handle = new_tabs[-1]
        driver.switch_to.window(oms_handle)
        try:
            if self.oms is None:
                self.oms = OMSModule(self.browser, self.user_config)
            self.oms.open_and_login()
            from config import PHASE2_OMS_FILTER_STATUS_SENT

            if PHASE2_OMS_FILTER_STATUS_SENT:
                if inquiry:
                    oms_filt = self._oms_finalize_filter_kwargs(inquiry)
                    self.oms.apply_finalize_list_filter(**oms_filt)
                else:
                    self.oms.apply_finalize_list_filter(
                        project_name, sourcing_no or ""
                    )
            else:
                self.oms.apply_filters(
                    status_pending=True,
                    status_sent=False,
                    sourcing_filter=True,
                )
            if not self.oms.extend_supplier_for_project(
                project_name, target_supplier, sourcing_no=sourcing_no or ""
            ):
                return False

            self.confirm_step(
                "扩展供应商已完成 → 删除「已发送」旧行 → OMS 发邮件（补跑）\n"
                "说明：无需再点启动器「阶段一」；不会在备件网新建询价单；"
                "阶段二报价仍用原 inquiry_no"
            )
            rows_checked, email_ok = (
                self.oms.run_phase1_oms_after_extend_supplier(
                    project_name, sourcing_no=sourcing_no or ""
                )
            )
            if rows_checked <= 0 or not email_ok:
                logger.error(
                    f"  待处理新行发邮件失败: 勾选={rows_checked}, "
                    f"邮件确认={email_ok}"
                )
                return False

            pending_items = self.oms.extract_pending_group_for_project(
                project_name,
                skip_refilter=True,
                sourcing_no=sourcing_no or "",
            )
            if pending_items:
                merge_oms_data_group(project_name, pending_items)
                if inquiry is not None:
                    inquiry["oms_supplier"] = self._extract_oms_supplier(
                        pending_items
                    )
                    for key in (
                        "系统询价号",
                        "系统查询价号",
                        "系统询价价号",
                    ):
                        for it in pending_items:
                            v = (it.get(key) or "").strip()
                            if v:
                                inquiry["oms_system_inquiry_no"] = v
                                break
                    inquiry["oms_extend_phase1_done"] = True
                logger.info(
                    f"  ✓ 待处理新行已发邮件并更新 oms_data（勾选 {rows_checked} 行）"
                )
            else:
                logger.warning(
                    "  邮件已发但未重新提取待处理行到 oms_data.json，请人工核对"
                )
            return True
        except Exception as e:
            logger.error(f"  OMS 扩展供应商/OMS补跑异常: {e}", exc_info=True)
            return False
        finally:
            try:
                driver.close()
            except Exception:
                pass
            time.sleep(0.4)
            try:
                driver.switch_to.window(spare_handle)
            except Exception:
                if driver.window_handles:
                    driver.switch_to.window(driver.window_handles[0])

    def _determine_factory(self, items):
        """
        阶段一写入 inquiry_results 的 factory_type：依据 OMS「供应商」列。
        供应商全称由本地配置提供；这里仅返回工厂类型。
        阶段二生成报价单时以备件网备注 PO 为准（见 spareparts po_factory），优先级高于本字段。
        """
        supplier = self._extract_oms_supplier(items)
        factory = OMSModule.factory_from_supplier(supplier)
        return factory if factory else DEFAULT_FACTORY

    def _get_oms_direct_address(self, items):
        """OMS 直发地址（优先直发地址列）"""
        for item in items:
            for key in ("直发地址", "直发", "收货地址", "地址"):
                v = item.get(key, "")
                if v and str(v).strip() and len(str(v).strip()) > 2:
                    return str(v).strip()
        return ""

    def _build_oms_recipient_line(self, items):
        """
        姓（收货人）：技术员 + 技术员联系方式。
        若名字与电话在同一列，或只有一列有值，则合并为一行。
        """
        for item in items:
            t = (item.get("技术员") or "").strip()
            c = (item.get("技术员联系方式") or "").strip()
            if t and c:
                return f"{t} {c}".strip()
            if t:
                return t
            if c:
                return c
        return ""

    def _get_oms_address(self, items):
        """从OMS数据中提取地址"""
        for item in items:
            addr = item.get("address", "")
            if addr and addr.strip():
                return addr.strip()
        return ""

    def _get_oms_cart_total_quantity(
        self, project_name="", ladder_no="", oms_group_key=""
    ):
        """从 oms_data.json 汇总各物料 quantity（购物车数量对齐用）。"""
        groups = load_oms_data() or {}
        items = self._lookup_oms_group_items(
            groups, project_name, ladder_no, oms_group_key=oms_group_key
        )
        if not items:
            return None
        total = 0
        for item in items:
            q = str(item.get("quantity", "1") or "1").strip()
            try:
                total += int(float(q))
            except (ValueError, TypeError):
                total += 1
        return total if total > 0 else None

    def _get_oms_line_quantities(
        self, project_name="", ladder_no="", oms_group_key=""
    ):
        """
        从 oms_data.json 读取每条物料数量（顺序与 OMS 分组一致），
        供购物车多行时与各行数量一一对应（避免仅「总行数=总件数」的旧启发式失效）。
        """
        groups = load_oms_data() or {}
        items = self._lookup_oms_group_items(
            groups, project_name, ladder_no, oms_group_key=oms_group_key
        )
        if not items:
            return None
        out = []
        for item in items:
            q = str(item.get("quantity", "1") or "1").strip()
            try:
                out.append(int(float(q)))
            except (ValueError, TypeError):
                out.append(1)
        return out if out else None

    def _get_oms_line_material_descs(
        self, project_name="", ladder_no="", oms_group_key=""
    ):
        """从 oms_data.json 读取每条物料描述（顺序与数量列一致），用于购物车行匹配。"""
        groups = load_oms_data() or {}
        items = self._lookup_oms_group_items(
            groups, project_name, ladder_no, oms_group_key=oms_group_key
        )
        if not items:
            return None
        out = []
        for item in items:
            d = (item.get("material_desc") or "").strip()
            out.append(d)
        return out if out else None

    def _print_summary(self):
        """打印处理结果汇总"""
        logger.info("\n" + "=" * 60)
        logger.info("  处理结果汇总")
        logger.info("=" * 60)
        
        for i, r in enumerate(self.results, 1):
            logger.info(f"  {i}. 梯号: {r['ladder_no']}")
            logger.info(f"     项目: {r['project_name']}")
            logger.info(f"     工厂: {r['factory_type']}")
            logger.info(f"     询价单号: {r['inquiry_no']}")
            logger.info(f"     报价单号: {r['quotation_no']}")
            logger.info(f"     物料数: {r['material_count']}")
            logger.info("")
        
        logger.info(f"  总计处理: {len(self.results)} 组")
        
        # 保存到结果文件
        result_path = os.path.join(
            os.path.dirname(__file__),
            f"results_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        with open(result_path, 'w', encoding='utf-8') as f:
            f.write("询价单自动化处理结果\n")
            f.write(f"执行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n")
            for i, r in enumerate(self.results, 1):
                f.write(f"{i}. 梯号:{r['ladder_no']} 项目:{r['project_name']} "
                        f"询价单:{r['inquiry_no']} 报价单:{r['quotation_no']} "
                        f"工厂:{r['factory_type']}\n")
            f.write(f"\n总计: {len(self.results)} 组\n")
        
        logger.info(f"  结果已保存到: {result_path}")


def main():
    # Windows 终端默认 GBK 编码会导致部分中文乱码，强制切换为 UTF-8
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="询价单自动化系统",
        epilog="""
使用方式:
  【阶段一】python main.py              创建所有询价单（含OMS筛选）
  【阶段一】python main.py --skip-oms    跳过OMS，用缓存数据创建询价单
  【阶段二】python main.py --resume      人工确认审批完成后，继续购物车→报价单
  【阶段三】python main.py --finalize    PDF/Excel 归档、填表、OMS 导入
  【改账号】python main.py --setup-config  重新录入 OMS/备件网账号并保存
        """
    )
    parser.add_argument(
        "--setup-config",
        action="store_true",
        help="交互录入 OMS/备件网账号并保存到 user_config.py（首次运行也会自动提示）",
    )
    parser.add_argument("--step-by-step", action="store_true",
                        help="逐步执行模式，每步需手动确认")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（不显示浏览器窗口）")
    parser.add_argument("--skip-oms", action="store_true",
                        help="跳过OMS筛选阶段，从 oms_data.json 加载缓存数据直接进入备件网流程")
    parser.add_argument("--resume", action="store_true",
                        help="续接模式：从 inquiry_results.json 继续审批→购物车→报价单；中山时可用 oms_data.json 或新开 OMS 补全收货人/直发地址")
    parser.add_argument(
        "--inquiry-results",
        metavar="PATH",
        default=None,
        help="指定询价单 JSON（默认自动选择；--finalize 时若 results 缺报价单号会用 last）",
    )
    parser.add_argument(
        "--close-browser",
        action="store_true",
        help="流程结束后自动关闭 Edge（默认不关闭，便于测试时对照页面）",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="第三阶段：导出 PDF/Excel、填表、OMS 导入（需阶段二已完成）",
    )
    parser.add_argument(
        "--date-folder",
        metavar="M.D",
        default=None,
        help="归档子文件夹名，默认当天如 5.18",
    )
    parser.add_argument(
        "--split",
        type=int,
        default=None,
        metavar="N",
        help="包装费/运费分摊项目数（两项目填 2）",
    )
    parser.add_argument(
        "--finalize-only",
        choices=["pdf", "excel", "fill", "import"],
        default=None,
        help="分步测试：pdf/excel/fill/import；import=仅OMS导入PDF+Excel",
    )
    parser.add_argument(
        "--import-submit",
        action="store_true",
        help="OMS 导入：上传后点保存，再在确认窗点确认（默认见 config.FINALIZE_IMPORT_SUBMIT）",
    )
    parser.add_argument(
        "--no-manual-recovery",
        action="store_true",
        help="出错时不暂停交人工处理，直接报错退出（默认开启人工恢复）",
    )
    parser.add_argument(
        "--sync-excel",
        nargs="?",
        const="",
        default=None,
        help="将 inquiry_results.json（或指定 JSON）同步到 sessions/询价单记录.xlsx",
    )
    args = parser.parse_args()
    
    # --sync-excel：将 JSON 数据补录到 Excel
    if args.sync_excel is not None:
        path = args.sync_excel if args.sync_excel else resolve_inquiry_results_path()
        inquiries = _load_inquiries_from_file(path)
        if not inquiries:
            print(f"[ERROR] {path} 中无数据")
            sys.exit(1)
        _log_results_to_sessions_excel(inquiries)
        _update_sessions_excel_for_inquiries(inquiries)
        print(f"✓ 已将 {len(inquiries)} 条数据同步到 {SESSIONS_EXCEL}")
        sys.exit(0)
    
    # --resume 与 --skip-oms 互斥检查
    if args.resume and args.skip_oms:
        print("[ERROR] --resume 和 --skip-oms 不能同时使用")
        print("  --resume 是第二阶段（审批→购物车→报价单），需要完整账号登录")
        sys.exit(1)
    if args.finalize and (args.resume or args.skip_oms):
        print("[ERROR] --finalize 不能与 --resume / --skip-oms 同时使用")
        sys.exit(1)
    
    error_collector = setup_logging()

    if OMS_ATTACHMENT_CACHE_RETENTION_DAYS > 0:
        cleanup_stale_oms_attachment_caches(
            OMS_ATTACHMENT_CACHE_RETENTION_DAYS,
            project_root=os.path.dirname(os.path.abspath(__file__)),
        )

    # 加载用户个人信息（首次运行终端录入并保存）
    user_config = load_user_config(
        force_setup=args.setup_config,
        setup_only=args.setup_config,
    )
    if args.setup_config:
        return

    inquiry_path = args.inquiry_results
    if (args.resume or args.finalize) and not inquiry_path:
        inquiry_path = resolve_inquiry_results_path(
            for_finalize=bool(args.finalize)
        )

    automation = InquiryAutomation(
        user_config=user_config,
        step_by_step=args.step_by_step,
        headless=args.headless,
        skip_oms=args.skip_oms,
        resume=args.resume,
        finalize=args.finalize,
        inquiry_results_path=inquiry_path,
        close_browser_on_exit=args.close_browser,
        date_folder=args.date_folder,
        project_split_count=args.split,
        finalize_only=args.finalize_only,
        import_submit=True if args.import_submit else None,
        manual_recovery_on_error=(
            MANUAL_RECOVERY_ON_ERROR and not args.no_manual_recovery
        ),
    )
    automation.error_collector = error_collector
    automation.run()


if __name__ == "__main__":
    main()
