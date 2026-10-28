"""把 ComfyUI_ZXin 打包成可在其它电脑上直接使用的 zip。

用法：
    python tools/package.py [输出目录]

默认输出到工作区根目录，文件名形如 ComfyUI_ZXin-v1.0.0.zip
"""

import os
import re
import sys
import zipfile

PKG_NAME = "ComfyUI_ZXin"
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 不进压缩包的内容
EXCLUDE_NAMES = {"__pycache__", ".git", ".idea", ".vscode", "node_modules"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".pyd", ".log", ".tmp", ".swp")

# 必须存在的文件（缺任何一个都判定为打包失败）
# 统一用正斜杠，与 collect() 产出的包内相对路径格式一致
REQUIRED = [
    "__init__.py",
    "nodes.py",
    "common.py",
    "pyproject.toml",
    "requirements.txt",
    "LICENSE",
    "README.md",
    "js/dynamic_branch_outputs.js",
    "example_workflows/ZXin_01_批量加载与预览.json",
    "example_workflows/ZXin_02_多轮自动执行.json",
    "example_workflows/ZXin_03_四路并行分发.json",
]


def read_version():
    """从 pyproject.toml 里取版本号。"""
    path = os.path.join(PKG_ROOT, "pyproject.toml")
    with open(path, encoding="utf-8") as f:
        m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else "0.0.0"


def collect():
    """收集要打包的文件，返回 (绝对路径, 包内相对路径) 列表。"""
    items = []
    for root, dirs, files in os.walk(PKG_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
        for name in sorted(files):
            if name.endswith(EXCLUDE_SUFFIX):
                continue
            if name in EXCLUDE_NAMES:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, PKG_ROOT)
            items.append((full, rel.replace(os.sep, "/")))
    return items


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(PKG_ROOT)
    os.makedirs(out_dir, exist_ok=True)

    version = read_version()
    zip_path = os.path.join(out_dir, f"{PKG_NAME}-v{version}.zip")

    items = collect()

    # 完整性检查
    rels = {rel for _, rel in items}
    missing = [r for r in REQUIRED if r not in rels]
    if missing:
        print("[✗] 缺少必需文件，无法打包：")
        for m in missing:
            print(f"    - {m}")
        return 1

    if os.path.exists(zip_path):
        os.remove(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for full, rel in items:
            # 顶层带 ComfyUI_ZXin/ 前缀，解压后直接就是正确的目录结构
            zf.write(full, f"{PKG_NAME}/{rel}")

    size_kb = os.path.getsize(zip_path) / 1024.0

    print(f"[✓] 打包完成：{zip_path}")
    print(f"    文件数：{len(items)}")
    print(f"    体积  ：{size_kb:.1f} KB")
    print(f"    版本  ：{version}")

    # 回读校验：确保中文文件名与目录结构正确
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            print(f"[✗] 压缩包损坏：{bad}")
            return 1
        names = zf.namelist()
        assert all(n.startswith(PKG_NAME + "/") for n in names), "顶层目录结构错误"
        assert f"{PKG_NAME}/__init__.py" in names, "入口文件缺失"
        assert any(n.endswith(".json") for n in names), "示例工作流缺失"
        assert any(n.endswith(".js") for n in names), "前端扩展缺失"
        # 中文名可正常读回
        cn = [n for n in names if "批量加载" in n]
        print(f"    中文文件名读回正常：{len(cn)} 个")

    print("\n部署：解压到 ComfyUI 的 custom_nodes 目录，重启 ComfyUI 即可。")
    print("详细步骤见包内「安装说明.md」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
