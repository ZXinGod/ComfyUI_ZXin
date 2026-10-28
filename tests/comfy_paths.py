"""跨机器的 ComfyUI 路径探测。

测试脚本不应硬编码任何人的绝对路径，否则别人 clone 后无法运行。
本模块按以下顺序定位 ComfyUI：

1. 环境变量 ``COMFY_ROOT``
2. 从本文件位置向上查找（包被放在 ``<ComfyUI>/custom_nodes/<包名>/`` 时）
3. 常见安装位置的自动扫描
4. 系统 PATH 中的 ``python``（此时 ComfyUI 已作为包安装）

找不到时抛出 :class:`ComfyNotFound`，由调用方给出清晰提示。
"""

import os
import sys

__all__ = ["ComfyNotFound", "find_comfy_root", "require_comfy_root"]


class ComfyNotFound(RuntimeError):
    """未找到 ComfyUI 安装目录。"""


def _looks_like_comfy_root(path):
    """判断某个目录是否是 ComfyUI 主目录。

    不能只看 ``nodes.py`` —— 本节点包自己也有同名文件，会被误判。
    判定条件：存在 ``comfy/`` 子包，且有 ``folder_paths.py`` 或 ``main.py``。
    """
    if not path or not os.path.isdir(path):
        return False
    has_comfy_pkg = os.path.isdir(os.path.join(path, "comfy"))
    has_entry = (
        os.path.isfile(os.path.join(path, "folder_paths.py"))
        or os.path.isfile(os.path.join(path, "main.py"))
        or os.path.isfile(os.path.join(path, "execution.py"))
    )
    return has_comfy_pkg and has_entry


def _from_package_location():
    """从本文件位置向上推断 ComfyUI 根目录。

    常见情形是包被放在 ``<ComfyUI>/custom_nodes/<包名>/``，
    往上找到名为 ``custom_nodes`` 的目录，它的上一级就是 ComfyUI 根。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(6):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        if os.path.basename(cur).lower() == "custom_nodes":
            if _looks_like_comfy_root(parent):
                return parent
        cur = parent

    # 退化路径：tests/ -> 包名/ -> custom_nodes/ -> <ComfyUI>/
    for up in (3, 4):
        candidate = here
        for _ in range(up):
            candidate = os.path.dirname(candidate)
        if _looks_like_comfy_root(candidate):
            return candidate
    return None


def _from_parent_dirs():
    """从当前工作目录向上查找（ComfyUI 通常是 cwd）。"""
    cur = os.path.abspath(os.getcwd())
    for _ in range(6):
        if _looks_like_comfy_root(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _scan_common_locations():
    """扫描常见安装位置（较慢，仅在快速路径都失败时使用）。

    覆盖两种常见形态：
    - ``<任意目录>/ComfyUI``            （pip 版 / git clone 版）
    - ``<任意目录>/<xxx>_portable/ComfyUI`` （Windows 便携版，多套一层）

    限制扫描深度，避免遍历整块磁盘。
    """
    roots = []

    for base in (os.path.expanduser("~"), "C:\\", "D:\\", "E:\\", "/"):
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for name in entries:
            full = os.path.join(base, name)
            if not os.path.isdir(full):
                continue
            # 形态一：目录本身就是 ComfyUI
            if _looks_like_comfy_root(full):
                roots.append(full)
                continue
            # 形态二/三：便携版外层，往里找 1~2 层
            for depth in (1, 2):
                found = _scan_depth(full, depth)
                if found:
                    roots.append(found)
                    break

    for c in roots:
        if _looks_like_comfy_root(c):
            return c
    return None


def _scan_depth(root, depth):
    """在 root 下递归 depth 层，找 ComfyUI 根目录。"""
    if depth <= 0:
        return None
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    for name in entries:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if _looks_like_comfy_root(path):
            return path
        found = _scan_depth(path, depth - 1)
        if found:
            return found
    return None


def find_comfy_root():
    """返回 ComfyUI 主目录路径；找不到则返回 ``None``。"""
    env = os.environ.get("COMFY_ROOT")
    if env and _looks_like_comfy_root(env):
        return env

    for finder in (_from_package_location, _from_parent_dirs, _scan_common_locations):
        found = finder()
        if found:
            return found

    # ComfyUI 作为 pip 包安装（ComfyUI 在 site-packages 里）
    try:
        import comfy  # noqa: F401
        return os.path.dirname(os.path.dirname(os.path.abspath(comfy.__file__)))
    except Exception:
        pass

    return None


def require_comfy_root():
    """返回 ComfyUI 主目录；找不到时抛出带排查指引的异常。"""
    root = find_comfy_root()
    if root:
        return root
    raise ComfyNotFound(
        "未找到 ComfyUI 安装目录。\n"
        "请用环境变量指定后重试，例如：\n"
        "    set COMFY_ROOT=D:/path/to/ComfyUI   (Windows CMD)\n"
        "    export COMFY_ROOT=/path/to/ComfyUI (Linux/macOS)\n"
        "或在本机 ComfyUI 目录下运行本脚本。"
    )


def comfy_root_from_sys_path():
    """给 sys.path 用：确保 ComfyUI 目录可被 import。"""
    root = find_comfy_root()
    if root and root not in sys.path:
        sys.path.insert(0, root)
    return root
