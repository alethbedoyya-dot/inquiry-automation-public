"""
配置文件 — 询价单自动化系统
=============================================
系统通用配置（URL、选择器、工厂规则等）
个人信息请填写 user_config.py，不要填在这里！
"""

import importlib.util
import os
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent


def _load_local_config():
    path = _PROJECT_ROOT / "user_config.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_local_user_config", path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LOCAL_CONFIG = _load_local_config()


def _local_value(name, default):
    """Allow private values from env vars or ignored user_config.py."""
    env_val = os.environ.get(name)
    if env_val not in (None, ""):
        return env_val
    if _LOCAL_CONFIG and hasattr(_LOCAL_CONFIG, name):
        val = getattr(_LOCAL_CONFIG, name)
        if val not in (None, ""):
            return val
    return default


def _local_list(name, default):
    value = _local_value(name, default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [item for item in list(value or []) if item]


# ============================================================
# 系统URL（全公司统一，无需修改）
# ============================================================
OMS_HOME_URL = _local_value("OMS_HOME_URL", "https://example-oms.local")
OMS_URL = _local_value("OMS_URL", f"{OMS_HOME_URL}/path/to/oms/list")
SPAREPARTS_URL = _local_value("SPAREPARTS_URL", "https://example-spareparts.local/login")
SPAREPARTS_HOST_KEYWORD = _local_value(
    "SPAREPARTS_HOST_KEYWORD",
    "example-spareparts.local",
)

# ============================================================
# 询价单填写默认值（全公司统一，无需修改）
# ============================================================
DEFAULT_ELEVATOR_SUPPLIER = _local_value("DEFAULT_ELEVATOR_SUPPLIER", "其他")
DEFAULT_FACTORY = _local_value("DEFAULT_FACTORY", "松江")

# OMS「供应商」列与 PO 工厂对应（扩展供应商时仅允许下列两项，排除「家用电梯」）
OMS_SUPPLIER_SHANGHAI = _local_value("OMS_SUPPLIER_SHANGHAI", "Supplier A")
OMS_SUPPLIER_ZHONGSHAN = _local_value("OMS_SUPPLIER_ZHONGSHAN", "Supplier B")
OMS_SUPPLIER_EXTEND_SEARCH_KEY = _local_value("OMS_SUPPLIER_EXTEND_SEARCH_KEY", "Supplier")
# 删除弹窗「删除原因」可不填，直接点保存；若 OMS 强制校验可在此填写
OMS_DELETE_REASON_DEFAULT = ""

# 阶段一：供应商ID列筛选（可配置为多轮；公开仓库默认留空）
OMS_SUPPLIER_ID_SONGJIANG = _local_value("OMS_SUPPLIER_ID_SONGJIANG", "")
OMS_SUPPLIER_ID_ZHONGSHAN = _local_value("OMS_SUPPLIER_ID_ZHONGSHAN", "")
# 阶段一按此列表顺序逐轮筛选；空列表或不配置则跳过供应商ID筛选
OMS_PHASE1_SUPPLIER_IDS = _local_list(
    "OMS_PHASE1_SUPPLIER_IDS",
    [OMS_SUPPLIER_ID_SONGJIANG, OMS_SUPPLIER_ID_ZHONGSHAN],
)
DEFAULT_PRICE_VALIDITY = "三个月"  # 价格有效期
DEFAULT_DELIVERY_SELF_PICKUP = "工厂自提"  # 松江默认发货方式

# ============================================================
# 报价单类型配置（全公司统一，无需修改）
# ============================================================
SONGJIANG_CONFIG = {
    "factory_name": _local_value("SONGJIANG_FACTORY_NAME", "Factory A"),
    "delivery_method": "工厂自提",
    "address_rule": "只填上海市（无视OMS地址）",
    "address": "上海市",
}

ZHONGSHAN_CONFIG = {
    "factory_name": _local_value("ZHONGSHAN_FACTORY_NAME", "Factory B"),
    "delivery_method": "零担/快递（选低价）",
    "address_rule": "OMS有地址则填OMS地址，无则问EH负责人",
}

# ============================================================
# 模板照片（OMS无照片时自动使用）
# ============================================================
# 把固定模板照片放在 assets/photos/ 目录下，程序会自动扫描使用
TEMPLATE_PHOTOS_DIR = "assets/photos"

# 阶段一：从 OMS 列表行下载实物照片/附件（备件网询价单上传用）
OMS_ATTACHMENTS_CACHE_DIR = "cache/oms_attachments"
OMS_ATTACHMENT_HEADER_KEYWORDS = ("附件", "实物照片", "实物图", "照片", "图片", "实物")
OMS_ATTACHMENT_DOWNLOAD_TIMEOUT = 45
OMS_ATTACHMENT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
# 每行 OMS 附件弹窗可有多张（备件网 5 视图：前 N 张按序，其余随机）
# 下载后由 pick_distinct_photo_files 去重、过滤过小缩略图，最多保留 8 张
OMS_MAX_ATTACHMENTS_PER_ROW = 8
OMS_ATTACHMENT_MIN_FILE_BYTES = 8000
# 附件缓存保留天数：自各项目子文件夹内「最后文件更新时间」起满 N 天后删除；0=不自动清理
OMS_ATTACHMENT_CACHE_RETENTION_DAYS = 14

# 备件网「上传图片」单文件最小体积（小于此值会被系统拒绝，需人工放大）
SPAREPARTS_MIN_PHOTO_BYTES = 100 * 1024  # 100KB

# 任意步骤报错时是否暂停并交人工处理（同一次运行内可继续）
MANUAL_RECOVERY_ON_ERROR = True

# ============================================================
# 超时与等待配置（秒）
# ============================================================
PAGE_LOAD_TIMEOUT = 30
ELEMENT_WAIT_TIMEOUT = 15
POLL_INTERVAL = 5  # 轮询等待审批状态间隔
MAX_WAIT_APPROVAL = 600  # 最大等待审批时间（10分钟）

# ============================================================
# 第三阶段：报价单 PDF/Excel 归档与填表
# ============================================================
QUOTE_OUTPUT_ROOT = "../报价单"  # 项目上级目录的「报价单」文件夹，分发时自动带上
OMS_SOURCING_COLUMN = "寻源单号"
PRICE_VALIDITY_MONTHS = 3
DOWNLOAD_WAIT_TIMEOUT = 90
DEFAULT_PROJECT_SPLIT_COUNT = 1

# 阶段三 OMS 列表筛选（False：已发送→Sourcing8ID→项目名称；True=测试按寻源单号）
FINALIZE_FILTER_BY_SOURCING_NO = False

# 阶段二若需进 OMS 列表（如补抓），筛「已发送」（阶段一仍用「待处理」）
PHASE2_OMS_FILTER_STATUS_SENT = True

# 阶段三 OMS 导入：True=上传后点「保存」→ 确认窗点「确认」；False=只上传，人工操作
FINALIZE_IMPORT_SUBMIT = True

# sessions/ 备份文件保留天数（超期自动删除）；0=不自动清理
SESSIONS_RETENTION_DAYS = 14

# 无项目名时 OMS 分组：直发地址/梯号/寻源单号/系统询价号任一相同则并查集合并
OMS_GROUP_MATCH_MIN_LEN = 4  # 表格行勾选时关联字段最短匹配长度
OMS_GROUP_CONFIRM_UNCERTAIN = True  # 间接合并等不确定分组时暂停请操作员确认
