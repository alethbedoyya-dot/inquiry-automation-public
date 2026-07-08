"""user_config.py 的读取、校验与首次运行交互保存。"""
import os
import sys
import importlib.util
import getpass


REQUIRED_KEYS = ("OMS_USERNAME", "SPAREPARTS_USERNAME", "SPAREPARTS_PASSWORD")
OPTIONAL_KEYS = ("OMS_PASSWORD", "EDGE_USER_DATA_DIR", "SOURCING_KEYWORD")
DEPLOYMENT_KEYS = (
    "OMS_HOME_URL",
    "OMS_URL",
    "SPAREPARTS_URL",
    "SPAREPARTS_HOST_KEYWORD",
    "OMS_SUPPLIER_SHANGHAI",
    "OMS_SUPPLIER_ZHONGSHAN",
    "OMS_SUPPLIER_EXTEND_SEARCH_KEY",
    "OMS_SUPPLIER_ID_SONGJIANG",
    "OMS_SUPPLIER_ID_ZHONGSHAN",
    "OMS_PHASE1_SUPPLIER_IDS",
    "SONGJIANG_FACTORY_NAME",
    "ZHONGSHAN_FACTORY_NAME",
)


def config_paths(base_dir=None):
    base_dir = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return (
        os.path.join(base_dir, "user_config.py"),
        os.path.join(base_dir, "user_config.example.py"),
    )


def _safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def read_user_config_file(path):
    """从已有 user_config.py 读取；文件不存在返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("user_config", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    info = {
        "SOURCING_KEYWORD": getattr(mod, "SOURCING_KEYWORD", "") or "",
        "OMS_USERNAME": (getattr(mod, "OMS_USERNAME", "") or "").strip(),
        "OMS_PASSWORD": getattr(mod, "OMS_PASSWORD", "") or "",
        "SPAREPARTS_USERNAME": (getattr(mod, "SPAREPARTS_USERNAME", "") or "").strip(),
        "SPAREPARTS_PASSWORD": getattr(mod, "SPAREPARTS_PASSWORD", "") or "",
        "EDGE_USER_DATA_DIR": getattr(mod, "EDGE_USER_DATA_DIR", "") or "",
    }
    for key in DEPLOYMENT_KEYS:
        info[key] = getattr(mod, key, "")
    return info


def missing_required_keys(info):
    if not info:
        return list(REQUIRED_KEYS)
    missing = []
    if not (info.get("OMS_USERNAME") or "").strip():
        missing.append("OMS_USERNAME")
    if not (info.get("SPAREPARTS_USERNAME") or "").strip():
        missing.append("SPAREPARTS_USERNAME")
    if not (info.get("SPAREPARTS_PASSWORD") or "").strip():
        missing.append("SPAREPARTS_PASSWORD")
    return missing


def is_config_ready(info):
    return not missing_required_keys(info)


def _prompt_line(label, default="", secret=False):
    default = (default or "").strip()
    hint = f" [{default}]" if default else ""
    while True:
        try:
            if secret:
                raw = getpass.getpass(f"  {label}{hint}: ")
            else:
                raw = input(f"  {label}{hint}: ")
        except (EOFError, KeyboardInterrupt):
            _safe_print("\n  已取消。")
            raise SystemExit(1) from None
        text = (raw or default).strip()
        if text or not label.endswith("(必填)"):
            return text
        _safe_print("  此项不能为空，请重新输入。")


def prompt_and_build_config(existing=None, *, reconfigure=False):
    """交互式录入 OMS / 备件网账号。"""
    existing = existing or {}
    _safe_print("")
    _safe_print("=" * 60)
    if reconfigure:
        _safe_print("  重新配置账号 — 用户名直接回车可沿用当前值，密码请重新输入")
    else:
        _safe_print("  首次配置 — 请输入你的账号（将保存到本目录 user_config.py）")
        _safe_print("  下次运行将自动使用，无需再改配置文件。")
    _safe_print("  说明: 密码以明文保存在本机 user_config.py，请勿分享该文件。")
    _safe_print("=" * 60)
    oms_user = _prompt_line(
        "OMS 用户名 (必填)",
        existing.get("OMS_USERNAME", ""),
    )
    oms_pwd = _prompt_line(
        "OMS 密码 (可选，直接回车表示留空/手工登录)",
        "",
    )
    sp_user = _prompt_line(
        "备件网用户名 (必填)",
        existing.get("SPAREPARTS_USERNAME", ""),
    )
    sp_pwd = _prompt_line(
        "备件网密码 (必填)",
        "",
    )
    if not sp_pwd:
        _safe_print("  备件网密码不能为空。")
        return prompt_and_build_config(existing)
    info = {
        "SOURCING_KEYWORD": "",
        "OMS_USERNAME": oms_user,
        "OMS_PASSWORD": oms_pwd,
        "SPAREPARTS_USERNAME": sp_user,
        "SPAREPARTS_PASSWORD": sp_pwd,
        "EDGE_USER_DATA_DIR": (existing.get("EDGE_USER_DATA_DIR") or "").strip(),
    }
    for key in DEPLOYMENT_KEYS:
        if key in existing:
            info[key] = existing.get(key)
    return info


def write_user_config_file(path, info):
    """写入 user_config.py（UTF-8）。"""
    info = info or {}
    body = f'''# -*- coding: utf-8 -*-
"""
个人配置 — 由程序自动生成/更新，请勿提交到共享仓库。
"""

OMS_USERNAME = {info.get("OMS_USERNAME", "")!r}
OMS_PASSWORD = {info.get("OMS_PASSWORD", "")!r}

SPAREPARTS_USERNAME = {info.get("SPAREPARTS_USERNAME", "")!r}
SPAREPARTS_PASSWORD = {info.get("SPAREPARTS_PASSWORD", "")!r}

EDGE_USER_DATA_DIR = {info.get("EDGE_USER_DATA_DIR", "")!r}

# 已废弃，留空即可
SOURCING_KEYWORD = {info.get("SOURCING_KEYWORD", "")!r}

# Private deployment overrides. Public config.py defaults are placeholders.
OMS_HOME_URL = {info.get("OMS_HOME_URL", "")!r}
OMS_URL = {info.get("OMS_URL", "")!r}
SPAREPARTS_URL = {info.get("SPAREPARTS_URL", "")!r}
SPAREPARTS_HOST_KEYWORD = {info.get("SPAREPARTS_HOST_KEYWORD", "")!r}

OMS_SUPPLIER_SHANGHAI = {info.get("OMS_SUPPLIER_SHANGHAI", "")!r}
OMS_SUPPLIER_ZHONGSHAN = {info.get("OMS_SUPPLIER_ZHONGSHAN", "")!r}
OMS_SUPPLIER_EXTEND_SEARCH_KEY = {info.get("OMS_SUPPLIER_EXTEND_SEARCH_KEY", "")!r}
OMS_SUPPLIER_ID_SONGJIANG = {info.get("OMS_SUPPLIER_ID_SONGJIANG", "")!r}
OMS_SUPPLIER_ID_ZHONGSHAN = {info.get("OMS_SUPPLIER_ID_ZHONGSHAN", "")!r}
OMS_PHASE1_SUPPLIER_IDS = {info.get("OMS_PHASE1_SUPPLIER_IDS", [])!r}

SONGJIANG_FACTORY_NAME = {info.get("SONGJIANG_FACTORY_NAME", "")!r}
ZHONGSHAN_FACTORY_NAME = {info.get("ZHONGSHAN_FACTORY_NAME", "")!r}
'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def ensure_user_config(base_dir=None, force_setup=False, quiet=False, setup_only=False):
    """
    保证 user_config.py 可用：缺失则交互录入并保存；已完整则直接加载。

    setup_only: 仅录入/保存后返回（供 --setup-config 使用，不继续跑自动化）。

    Returns:
        dict: 用户配置
    """
    cfg_path, _example = config_paths(base_dir)
    existing = read_user_config_file(cfg_path)
    reconfigure = bool(force_setup and existing and is_config_ready(existing))
    if force_setup or not is_config_ready(existing):
        info = prompt_and_build_config(
            existing or {},
            reconfigure=reconfigure,
        )
        write_user_config_file(cfg_path, info)
        _safe_print(f"\n  [OK] 已保存到: {cfg_path}")
        if not quiet:
            _safe_print("  下次运行阶段一/二/三将自动使用上述账号。")
            if setup_only:
                _safe_print("  配置完成，可直接关闭本窗口。")
                if sys.platform == "win32":
                    try:
                        input("\n  按 Enter 关闭窗口…")
                    except (EOFError, KeyboardInterrupt):
                        pass
            else:
                _safe_print("")
        return info
    if not quiet:
        _safe_print(f"[OK] 已加载用户配置: OMS={existing['OMS_USERNAME']}")
    return existing
