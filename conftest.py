"""pytest 收集配置。

test_oms_flow.py / test_cart_qty_align.py 依赖本机私有凭据文件 user_config.py
（仓库只提供 user_config.example.py 模板）。文件缺失时跳过这两个模块的收集，
避免新克隆环境出现 ImportError / sys.exit 导致的 pytest INTERNALERROR；
直接以 `python test_oms_flow.py` 方式运行的行为不受影响。
"""
import importlib.util
import os

if importlib.util.find_spec("user_config") is None:
    collect_ignore = [
        os.path.join(os.path.dirname(__file__), "test_oms_flow.py"),
        os.path.join(os.path.dirname(__file__), "test_cart_qty_align.py"),
    ]
