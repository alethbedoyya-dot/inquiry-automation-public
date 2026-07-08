"""
环境检查和初始化脚本
在运行主程序前执行，确保所有依赖就绪
"""
import subprocess
import sys
import os

# Windows CMD控制台兼容处理
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def ok(msg):
    print(f"[OK] {msg}")

def fail(msg):
    print(f"[FAIL] {msg}")

def warn(msg):
    print(f"[WARN] {msg}")

def check_python():
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 8):
        fail("需要Python 3.8+")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    return True

def check_selenium():
    try:
        import selenium
        ok(f"selenium {selenium.__version__}")
        return True
    except ImportError:
        fail("selenium未安装")
        return False


def check_phase3_deps():
    """阶段三填表依赖。"""
    ok_all = True
    for mod, pip_name in (("openpyxl", "openpyxl"), ("pdfplumber", "pdfplumber")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            ok(f"{mod} {ver}".strip())
        except ImportError:
            fail(f"{mod} 未安装（阶段三需要）")
            print(f"   请执行: python -m pip install {pip_name}")
            ok_all = False
    return ok_all


def check_quote_output_root():
    try:
        from config import QUOTE_OUTPUT_ROOT
        if os.path.isdir(QUOTE_OUTPUT_ROOT):
            ok(f"归档目录存在: {QUOTE_OUTPUT_ROOT}")
        else:
            warn(f"归档根目录尚不存在: {QUOTE_OUTPUT_ROOT}")
            warn("  阶段三运行时会自动创建当日子文件夹")
        return True
    except Exception as e:
        warn(f"无法读取 QUOTE_OUTPUT_ROOT: {e}")
        return True

def check_edge():
    import shutil
    import glob as _glob
    
    # 多个可能的Edge路径
    possible_paths = [
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    ]
    
    edge = shutil.which("msedge") or shutil.which("MicrosoftEdge")
    if not edge:
        for p in possible_paths:
            if os.path.exists(p):
                edge = p
                break
    
    if edge:
        ok(f"Edge浏览器: {edge}")
        try:
            result = subprocess.run(
                ['powershell', '-Command',
                 f'(Get-Item "{edge}").VersionInfo.FileVersion'],
                capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip():
                print(f"   Edge版本: {result.stdout.strip()}")
        except:
            pass
        return True
    else:
        warn("未在PATH中找到Edge，但Selenium Manager会自动查找")
        return True  # Selenium 4+ 自带 Selenium Manager 会自动查找浏览器

def check_webdriver():
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.service import Service
        ok("Selenium Manager将自动管理Edge WebDriver")
        return True
    except Exception as e:
        warn(f"WebDriver检查: {e}")
        return True

def check_user_config():
    from utils.user_config_store import (
        config_paths,
        is_config_ready,
        read_user_config_file,
    )

    user_config_path, _example_path = config_paths()
    info = read_user_config_file(user_config_path)
    if not is_config_ready(info):
        warn("尚未配置账号（无 user_config.py 或内容为空）")
        warn("首次运行 启动询价单自动化.bat 或 python main.py 时将在终端提示录入并自动保存")
        return True
    ok(f"user_config.py 已配置 (OMS={info['OMS_USERNAME']})")
    return True

def _pip_install(*packages):
    """uv/PEP668 环境下优先使用 --break-system-packages。"""
    base = [sys.executable, "-m", "pip", "install", *packages]
    for extra in ("--break-system-packages", "--user"):
        try:
            subprocess.check_call(
                base + [extra],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False


def install_selenium():
    print("正在安装 selenium...")
    if _pip_install("selenium"):
        ok("selenium 安装成功")
        return True
    fail("selenium 安装失败")
    return False


def install_phase3_deps():
    print("正在安装 openpyxl、pdfplumber...")
    if _pip_install("openpyxl", "pdfplumber"):
        ok("阶段三依赖安装成功")
        return True
    fail("阶段三依赖安装失败，请手动: pip install openpyxl pdfplumber --break-system-packages")
    return False

def main():
    print("=" * 50)
    print("询价单自动化 - 环境检查")
    print("=" * 50)
    
    all_ok = True
    
    if not check_python():
        all_ok = False
    
    if not check_selenium():
        if not install_selenium():
            all_ok = False
        else:
            import selenium
            ok(f"selenium {selenium.__version__}")
    
    if not check_edge():
        all_ok = False
    
    check_webdriver()

    print("---")
    if not check_phase3_deps():
        if not install_phase3_deps():
            all_ok = False
        elif not check_phase3_deps():
            all_ok = False
    check_quote_output_root()
    
    print("---")
    if not check_user_config():
        all_ok = False
    
    print("=" * 50)
    if all_ok:
        print("[OK] 环境就绪！可以运行 main.py")
        print()
        print("运行方式:")
        print("  python main.py                  # 阶段一")
        print("  python main.py --resume         # 阶段二")
        print("  python main.py --finalize       # 阶段三")
        print("  python main.py --step-by-step   # 逐步确认模式")
        print("  python test_quote_fill.py       # 阶段三填表单元测试")
    else:
        print("[FAIL] 环境检查未通过，请先解决上述问题")
    
    return all_ok

if __name__ == "__main__":
    main()
