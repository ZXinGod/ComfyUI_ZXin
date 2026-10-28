"""生成 ComfyUI_ZXin 的示例工作流 JSON。

按 ComfyUI 工作流格式输出到 example_workflows/ 目录，
用户可在「工作流 → 浏览模板」中直接导入使用。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)
OUT_DIR = os.path.join(PKG_DIR, "example_workflows")
os.makedirs(OUT_DIR, exist_ok=True)

VERSION = 0.4


def node(nid, ntype, pos, size, order, inputs=None, outputs=None,
         widgets=None, title=None, color=None):
    n = {
        "id": nid,
        "type": ntype,
        "pos": list(pos),
        "size": {"0": size[0], "1": size[1]},
        "flags": {},
        "order": order,
        "mode": 0,
        "inputs": inputs or [],
        "outputs": outputs or [],
        "properties": {"Node name for S&R": ntype, "cnr_id": "zxin-batch-image-tools"},
    }
    if widgets is not None:
        n["widgets_values"] = widgets
    if title:
        n["title"] = title
    if color:
        n["color"] = color
    return n


def inp(name, itype, link=None, slot=0):
    return {"name": name, "type": itype, "link": link, "slot_index": slot}


def out(name, otype, links=None, slot=0):
    return {"name": name, "type": otype, "links": links or [], "slot_index": slot}


def build_workflow():
    """基础链路：加载器 → 分发器 → 保存器 → 预览"""
    nodes = []
    links = []

    # 节点 1：批量图像加载器
    loader_id = 1
    nodes.append(node(
        loader_id, "ZXIN_BatchImageLoader", [-620, 60], [420, 640], 0,
        inputs=[],
        outputs=[
            out("图片列表", "IMAGE", [1]),
            out("文件夹预览图", "IMAGE", [2]),
            out("文件名列表", "STRING", [3]),
            out("加载数量", "INT", None),
        ],
        widgets=[
            "input",          # 加载来源
            "*",              # 文件名匹配
            "按名称升序",      # 排序方式
            0,                # 随机种子
            0,                # 最多加载张数
            "仅CPU",          # 载入设备
            True,             # 生成预览拼贴
            4,                # 预览列数
            192,              # 预览单格大小
            "",               # 自定义路径
        ],
        title="① 批量图像加载器（选好文件夹，看预览图）",
        color="#223",
    ))

    # 节点 2：批次分发器
    dist_id = 2
    nodes.append(node(
        dist_id, "ZXIN_BatchImageDistributor", [-100, 60], [400, 260], 1,
        inputs=[inp("图片列表", "IMAGE", 1)],
        outputs=[
            out("批次列表", "IMAGE", [4]),
            out("分批清单", "STRING", None),
            out("批次数", "INT", None),
        ],
        widgets=[
            4,                                        # 每批输出张数
            False,                                    # 丢弃不足一批的余数
        ],
        title="② 批次分发器（每批几张 → 自动跑几轮）",
        color="#232",
    ))

    # 节点 3：批量保存器
    saver_id = 3
    nodes.append(node(
        saver_id, "ZXIN_BatchImageSaver", [420, -40], [400, 300], 2,
        inputs=[
            inp("图片列表", "IMAGE", 4),
            inp("文件名列表", "STRING", 3),
        ],
        outputs=[out("图像批次", "IMAGE", None), out("已保存文件", "STRING", None)],
        widgets=[
            "ZXinBatch",     # 文件名前缀
            "沿用源文件名",   # 命名方式
            None,            # 文件名列表（已连线）
            True,            # 写入元数据
        ],
        title="③ 批量保存器（沿用源文件名）",
        color="#322",
    ))

    # 节点 4：内置预览（看文件夹拼贴）
    prev_id = 4
    nodes.append(node(
        prev_id, "PreviewImage", [-100, 400], [400, 460], 3,
        inputs=[inp("images", "IMAGE", 2)],
        outputs=[],
        widgets=[],
        title="文件夹预览图（先看看里面有什么）",
    ))

    # links: [link_id, origin_node, origin_slot, target_node, target_slot, type]
    links.append([1, loader_id, 0, dist_id, 0, "IMAGE"])       # 批次 → 分发器
    links.append([2, loader_id, 1, prev_id, 0, "IMAGE"])        # 预览图 → PreviewImage
    links.append([3, loader_id, 2, saver_id, 1, "STRING"])      # 文件名列表 → 保存器
    links.append([4, dist_id, 0, saver_id, 0, "IMAGE"])        # 第1批 → 保存器

    groups = [
        {
            "title": "步骤1：选文件夹 → 看预览",
            "bounding": [-660, -60, 520, 780],
            "color": "#3f789e",
            "font_size": 24,
            "flags": {},
        },
        {
            "title": "步骤2：设每批几张（下游会自动跑几轮）",
            "bounding": [-140, -60, 520, 420],
            "color": "#a1309b",
            "font_size": 24,
            "flags": {},
        },
        {
            "title": "步骤3：保存",
            "bounding": [380, -80, 480, 380],
            "color": "#8A8",
            "font_size": 24,
            "flags": {},
        },
    ]

    return {
        "last_node_id": 4,
        "last_link_id": 4,
        "nodes": nodes,
        "links": links,
        "groups": groups,
        "config": {},
        "extra": {},
        "version": VERSION,
    }


def build_multi_round_workflow():
    """多轮执行链路：加载器 → 分发器 → 标注器 → 保存器（含预览）"""
    nodes = []
    links = []

    loader_id = 1
    nodes.append(node(
        loader_id, "ZXIN_BatchImageLoader", [-660, 40], [420, 640], 0,
        inputs=[],
        outputs=[
            out("图片列表", "IMAGE", [1]),
            out("文件夹预览图", "IMAGE", [2]),
            out("文件名列表", "STRING", [5]),
            out("加载数量", "INT", None),
        ],
        widgets=[
            "input", "*", "按名称升序", 0, 0,
            "仅CPU", True, 4, 192, "",
        ],
        title="① 批量图像加载器",
        color="#223",
    ))

    dist_id = 2
    nodes.append(node(
        dist_id, "ZXIN_BatchImageDistributor", [-160, 40], [400, 260], 1,
        inputs=[inp("图片列表", "IMAGE", 1)],
        outputs=[
            out("批次列表", "IMAGE", [3]),
            out("分批清单", "STRING", None),
            out("批次数", "INT", None),
        ],
        widgets=[4, False],
        title="② 批次分发器（每批4张 → 20张图自动跑5轮）",
        color="#232",
    ))

    ann_id = 3
    nodes.append(node(
        ann_id, "ZXIN_BatchImageAnnotator", [340, -60], [400, 320], 2,
        inputs=[
            inp("图片列表", "IMAGE", 3),
            inp("文件名列表", "STRING", 6),
        ],
        outputs=[out("图像批次", "IMAGE", [4])],
        widgets=[
            "序号和文件名", "左下角", 20, "#FFFFFF", "#000000",
            None,   # 文件名列表（已连线）
        ],
        title="④ 批量角标标注器（每张打上编号和文件名）",
        color="#223",
    ))

    saver_id = 4
    nodes.append(node(
        saver_id, "ZXIN_BatchImageSaver", [340, 340], [400, 300], 3,
        inputs=[
            inp("图片列表", "IMAGE", 4),
            inp("文件名列表", "STRING", 5),
        ],
        outputs=[out("图像批次", "IMAGE", None), out("已保存文件", "STRING", None)],
        widgets=["ZXinBatch", "沿用源文件名", None, True],
        title="③ 批量保存器",
        color="#322",
    ))

    prev_id = 5
    nodes.append(node(
        prev_id, "PreviewImage", [-160, 380], [400, 460], 4,
        inputs=[inp("images", "IMAGE", 2)],
        outputs=[],
        widgets=[],
        title="文件夹预览图",
    ))

    links.append([1, loader_id, 0, dist_id, 0, "IMAGE"])
    links.append([2, loader_id, 1, prev_id, 0, "IMAGE"])
    links.append([3, dist_id, 0, ann_id, 0, "IMAGE"])
    links.append([4, ann_id, 0, saver_id, 0, "IMAGE"])
    links.append([5, loader_id, 2, saver_id, 1, "STRING"])
    links.append([6, loader_id, 2, ann_id, 1, "STRING"])

    # 「文件名列表」输出同时连给保存器与标注器，需在源输出槽回指两条 link
    for n in nodes:
        if n["id"] == loader_id:
            n["outputs"][2]["links"] = [5, 6]

    groups = [
        {
            "title": "① 选文件夹 + 看预览图",
            "bounding": [-700, -60, 520, 780],
            "color": "#3f789e",
            "font_size": 24,
            "flags": {},
        },
        {
            "title": "② 设每批几张：下游自动按轮次跑，只需点一次运行",
            "bounding": [-200, -60, 520, 420],
            "color": "#a1309b",
            "font_size": 24,
            "flags": {},
        },
        {
            "title": "③④ 标注 + 保存",
            "bounding": [300, -100, 500, 800],
            "color": "#8A8",
            "font_size": 24,
            "flags": {},
        },
    ]

    return {
        "last_node_id": 5,
        "last_link_id": 6,
        "nodes": nodes,
        "links": links,
        "groups": groups,
        "config": {},
        "extra": {},
        "version": VERSION,
    }


def build_four_slot_workflow():
    """四槽并行：一批图片分到 4 个槽，可分别接 4 个不同节点。"""
    nodes = []
    links = []

    loader_id = 1
    nodes.append(node(
        loader_id, "ZXIN_BatchImageLoader", [-660, 40], [420, 560], 0,
        inputs=[],
        outputs=[
            out("图片列表", "IMAGE", [1]),
            out("文件夹预览图", "IMAGE", [2]),
            out("文件名列表", "STRING", None),
            out("加载数量", "INT", None),
        ],
        widgets=[
            "input", "*", "按名称升序", 0, 0,
            "仅CPU", True, 4, 192, "",
        ],
        title="① 批量图像加载器",
        color="#223",
    ))

    dist_id = 2
    nodes.append(node(
        dist_id, "ZXIN_BatchImageSplitter", [-160, 40], [400, 260], 1,
        inputs=[inp("图片列表", "IMAGE", 1)],
        outputs=[
            out("分支1", "IMAGE", [3]),
            out("分支2", "IMAGE", [4]),
            out("分支3", "IMAGE", [5]),
            out("分支4", "IMAGE", [6]),
            out("分支5", "IMAGE", None),
            out("分支6", "IMAGE", None),
            out("分支7", "IMAGE", None),
            out("分支8", "IMAGE", None),
            out("分支9", "IMAGE", None),
            out("分支10", "IMAGE", None),
            out("分支11", "IMAGE", None),
            out("分支12", "IMAGE", None),
            out("分流清单", "STRING", None),
        ],
        widgets=[4],
        title="③ 并行分发器（分支数可调 1~12，端口自动增减）",
        color="#232",
    ))

    # 4 个 PreviewImage，分别代表 4 条并行分支
    prev_ids = [3, 4, 5, 6]
    titles = ["分支A：接不同节点", "分支B：接不同节点",
              "分支C：接不同节点", "分支D：接不同节点"]
    for slot, pid in enumerate(prev_ids):
        nodes.append(node(
            pid, "PreviewImage", [340, -180 + slot * 380], [400, 340], slot + 2,
            inputs=[inp("images", "IMAGE", 3 + slot)],
            outputs=[],
            widgets=[],
            title=titles[slot],
        ))

    # 预览图单独接一个 PreviewImage
    prev_folder = 7
    nodes.append(node(
        prev_folder, "PreviewImage", [-160, 380], [400, 440], 6,
        inputs=[inp("images", "IMAGE", 2)],
        outputs=[],
        widgets=[],
        title="文件夹预览图",
    ))

    links.append([1, loader_id, 0, dist_id, 0, "IMAGE"])
    links.append([2, loader_id, 1, prev_folder, 0, "IMAGE"])
    for slot, pid in enumerate(prev_ids):
        links.append([3 + slot, dist_id, slot, pid, 0, "IMAGE"])

    groups = [
        {
            "title": "① 选文件夹 + 看预览图",
            "bounding": [-700, -60, 520, 700],
            "color": "#3f789e",
            "font_size": 24,
            "flags": {},
        },
        {
            "title": "③ 分支1~4 分别接 4 个不同节点（分支数可调，端口自动增减）",
            "bounding": [-200, -60, 520, 400],
            "color": "#a1309b",
            "font_size": 24,
            "flags": {},
        },
        {
            "title": "四条并行分支（把 PreviewImage 换成你的实际节点即可）",
            "bounding": [300, -220, 500, 1560],
            "color": "#8A8",
            "font_size": 24,
            "flags": {},
        },
    ]

    return {
        "last_node_id": 7,
        "last_link_id": 6,
        "nodes": nodes,
        "links": links,
        "groups": groups,
        "config": {},
        "extra": {},
        "version": VERSION,
    }


def main():
    files = {
        "ZXin_01_批量加载与预览.json": build_workflow(),
        "ZXin_02_多轮自动执行.json": build_multi_round_workflow(),
        "ZXin_03_四路并行分发.json": build_four_slot_workflow(),
    }
    for name, data in files.items():
        path = os.path.join(OUT_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已生成: {name}  ({len(data['nodes'])} 节点, {len(data['links'])} 连线)")


if __name__ == "__main__":
    main()
