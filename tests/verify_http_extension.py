"""启动 ComfyUI 并检查前端扩展是否被正确注册与提供。

关键验证点：
1. /extensions 列表里是否包含我的 JS
2. JS 能否通过 HTTP 访问
3. ComfyClass 名是否正确
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from comfy_paths import find_comfy_root  # noqa: E402

COMFY = find_comfy_root()
if not COMFY:
    print("[✗] 未找到 ComfyUI 安装目录，请设置环境变量 COMFY_ROOT")
    sys.exit(1)

# 用当前解释器起服务（--cpu 避免 GPU 占用）
PY = sys.executable
PORT = int(os.environ.get("ZXIN_TEST_PORT", "8191"))
LOG_FILE = os.path.join(os.getcwd(), "comfy_http_test.log")

# 用 Windows 的 python 直接起服务（--cpu 避免 GPU 占用）
proc = subprocess.Popen(
    [PY, "-s", "main.py", "--cpu", "--port", str(PORT), "--disable-auto-launch"],
    cwd=COMFY,
    stdout=open(LOG_FILE, "w", encoding="utf-8", errors="replace"),
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
)

base = f"http://127.0.0.1:{PORT}"
ok = False
try:
    for _ in range(120):
        time.sleep(2)
        try:
            with urllib.request.urlopen(base + "/extensions", timeout=5) as r:
                exts = json.load(r)
            ok = True
            break
        except Exception:
            if proc.poll() is not None:
                break

    if not ok:
        print("服务未能启动，日志尾部：")
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                print(f.read()[-3000:])
        except Exception as e:
            print("读取日志失败:", e)
        sys.exit(1)

    print("=" * 66)
    print("/extensions 列表中与 ZXin 相关的条目")
    print("=" * 66)
    mine = [e for e in exts if "ZXin" in e or "zxin" in e.lower()]
    for e in mine:
        print("  ", e)
    if not mine:
        print("  ✗ 没有找到我的扩展！")
        print("  全部条目(前20):")
        for e in exts[:20]:
            print("    ", e)

    # 尝试直接访问 JS
    print("\n" + "=" * 66)
    print("直接访问 JS 文件")
    print("=" * 66)
    for url in mine:
        full = base + url
        try:
            with urllib.request.urlopen(full, timeout=5) as r:
                content = r.read().decode("utf-8", "replace")
            print(f"  ✓ {url}  ({len(content)} 字节)")
            # 检查关键内容
            for kw in ["registerExtension", "beforeRegisterNodeDef",
                       "ZXIN_BatchImageSplitter", "MAX_BRANCHES"]:
                print(f"      {'含' if kw in content else '缺'} {kw}")
        except Exception as e:
            print(f"  ✗ {url}  {e}")

    # 检查 object_info 里的 ComfyClass / 输出
    print("\n" + "=" * 66)
    print("object_info 中的节点输出")
    print("=" * 66)
    with urllib.request.urlopen(base + "/object_info", timeout=15) as r:
        allinfo = json.load(r)
    hits = {k: v for k, v in allinfo.items() if k.startswith("ZXIN_")}
    print("  ZXIN 节点数:", len(hits))
    for k, v in hits.items():
        print(f"    {k}: {v.get('display_name')}")
        print(f"       output_name: {v.get('output_name')}")
    if "ZXIN_BatchImageSplitter" in hits:
        v = hits["ZXIN_BatchImageSplitter"]
        print("  分发器输出端口数:", len(v.get("output_name", [])))
        print("  分发器输出名:", v.get("output_name"))

finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
