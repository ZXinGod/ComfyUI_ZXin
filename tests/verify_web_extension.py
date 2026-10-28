"""验证：WEB_DIRECTORY 注册后，ComfyUI 能否正确找到并提供 JS 文件。

ComfyUI 在加载节点时会：
1. 检查 module.WEB_DIRECTORY
2. 用 config_parser 读取 pyproject.toml 的 [tool.comfy] web 配置
3. 把 web 目录挂到 /extensions/<project.name>/

本脚本复刻这段逻辑，确认 JS 能被前端拿到。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)

sys.path.insert(0, HERE)
from comfy_paths import require_comfy_root  # noqa: E402

COMFY = require_comfy_root()
sys.path.insert(0, COMFY)
os.chdir(COMFY)

import importlib.util

# 本包所在目录：优先取 ComfyUI 的 custom_nodes 下的实际副本，
# 没有则用源码目录本身（同样能验证 WEB_DIRECTORY 声明是否正确）
pkg_dir = os.path.join(COMFY, "custom_nodes", os.path.basename(PKG_DIR))
if not os.path.isfile(os.path.join(pkg_dir, "__init__.py")):
    pkg_dir = PKG_DIR

# 按 ComfyUI 的加载方式导入
spec = importlib.util.spec_from_file_location(
    "ComfyUI_ZXin", os.path.join(pkg_dir, "__init__.py")
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["ComfyUI_ZXin"] = pkg
spec.loader.exec_module(pkg)

print("=" * 62)
print("验证 WEB_DIRECTORY / 前端扩展注册")
print("=" * 62)

# 1) module.WEB_DIRECTORY
wd = getattr(pkg, "WEB_DIRECTORY", None)
print("\n[1] module.WEB_DIRECTORY =", wd)
assert wd, "缺少 WEB_DIRECTORY"
# ComfyUI 用 hasattr 判断是否提供前端目录；__all__ 里同时列出是官方推荐写法
assert hasattr(pkg, "WEB_DIRECTORY"), "hasattr 检查失败"
assert "WEB_DIRECTORY" in pkg.__all__, "WEB_DIRECTORY 未加入 __all__"

# 2) 目录实际存在
web_path = os.path.abspath(os.path.join(pkg_dir, wd))
print("[2] 解析后的绝对路径 =", web_path)
assert os.path.isdir(web_path), "WEB_DIRECTORY 指向的目录不存在"

# 3) 里面有 .js 文件（ComfyUI 只提供 .js）
js_files = [f for f in os.listdir(web_path) if f.endswith(".js")]
print("[3] 目录内 .js 文件 =", js_files)
assert js_files, "WEB_DIRECTORY 里没有 .js 文件"

# 4) pyproject 的 [tool.comfy] 能否被 config_parser 解析
try:
    from comfy_config import config_parser
    cfg = config_parser.extract_node_configuration(pkg_dir)
    print("[4] pyproject 解析:")
    print("    project.name =", cfg.project.name)
    print("    tool_comfy.web =", cfg.tool_comfy.web)
    print("    tool_comfy.publisher_id =", cfg.tool_comfy.publisher_id)
except Exception as e:
    print("[4] pyproject 解析失败（不影响使用）:", type(e).__name__, e)

# 5) JS 文件内容自检：确认关键 API 存在
js_path = os.path.join(web_path, js_files[0])
src = open(js_path, encoding="utf-8").read()
print("\n[5] JS 内容自检 (%s):" % js_files[0])
checks = [
    ("import app", "from" in src and "scripts/app.js" in src),
    ("registerExtension", "app.registerExtension" in src),
    ("beforeRegisterNodeDef", "beforeRegisterNodeDef" in src),
    ("onNodeCreated", "onNodeCreated" in src),
    ("addOutput", "addOutput" in src),
    ("removeOutput", "removeOutput" in src),
    ("disconnectOutput", "disconnectOutput" in src),
    ("MAX_BRANCHES 常量", "const MAX_BRANCHES" in src),
    ("上限钳制", "MAX_BRANCHES)" in src),
]
for name, ok in checks:
    print(f"    {'OK ' if ok else 'MISS'} {name}")
    assert ok, f"JS 缺少: {name}"

# 6) 前端挂载路径预期
try:
    project_name = cfg.project.name
except Exception:
    project_name = "zxin-batch-image-tools"
print("\n[6] 前端将把该 JS 挂载到: /extensions/%s/" % project_name)
print("    访问路径示例: /extensions/%s/%s" % (project_name, js_files[0]))

print("\n" + "=" * 62)
print("全部通过")
print("=" * 62)
