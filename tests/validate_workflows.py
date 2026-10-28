"""校验示例工作流 JSON 的连线一致性（模拟前端导入时的校验）。"""

import glob
import json
import os
import sys

# 默认取本包内的 example_workflows/，不依赖任何绝对路径
HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)

WF_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PKG_DIR, "example_workflows")

if not os.path.isdir(WF_DIR):
    print(f"[✗] 工作流目录不存在：{WF_DIR}")
    sys.exit(1)

failed = False
for path in sorted(glob.glob(os.path.join(WF_DIR, "*.json"))):
    print("=" * 60)
    print(os.path.basename(path))
    print("=" * 60)
    d = json.load(open(path, encoding="utf-8"))
    nodes = {n["id"]: n for n in d["nodes"]}
    links = d["links"]
    errors = []

    link_ids = [l[0] for l in links]
    if len(link_ids) != len(set(link_ids)):
        errors.append("link id 重复")

    for lid, on, os_, tn, ts, ltype in links:
        if on not in nodes:
            errors.append(f"link {lid}: 源节点 {on} 不存在")
            continue
        if tn not in nodes:
            errors.append(f"link {lid}: 目标节点 {tn} 不存在")
            continue

        tgt_inputs = nodes[tn].get("inputs", [])
        if ts >= len(tgt_inputs):
            errors.append(f"link {lid}: 目标节点 {tn} 输入槽 {ts} 越界")
        else:
            ti = tgt_inputs[ts]
            if ti.get("link") != lid:
                errors.append(f"link {lid}: 目标输入槽 link 不匹配")
            if ti.get("type") != ltype:
                errors.append(f"link {lid}: 类型不匹配 {ti.get('type')} vs {ltype}")

        src_outputs = nodes[on].get("outputs", [])
        if os_ >= len(src_outputs):
            errors.append(f"link {lid}: 源节点 {on} 输出槽 {os_} 越界")
        elif lid not in (src_outputs[os_].get("links") or []):
            errors.append(f"link {lid}: 源输出槽未回指 link")

    orders = sorted(n["order"] for n in d["nodes"])
    if orders != list(range(len(d["nodes"]))):
        errors.append(f"order 不连续: {orders}")

    if errors:
        failed = True
        print("  发现问题:")
        for e in errors:
            print("   -", e)
    else:
        print(f"  OK  {len(d['nodes'])} 节点 / {len(links)} 连线 全部一致")
        for n in d["nodes"]:
            print(f"    [{n['id']}] {n['type']}  「{n.get('title', '')}」")
    print()

sys.exit(1 if failed else 0)
