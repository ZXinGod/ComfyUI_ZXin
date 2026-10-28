"""ComfyUI_ZXin —— 图像批处理与分发节点集。

5 个节点，覆盖一条完整链路：
1. 批量图像加载器   目录扫描 → 图片列表（保留原始尺寸）+ 文件夹预览
2. 批次分发器       每批 N 张 → 批次列表，驱动下游自动逐批执行（只按一次运行）
3. 并行分发器       图片轮流分到 N 个分支（输出端口随分支数自动增减）
4. 批量保存器       逐张落盘 + PNG 元数据
5. 批量角标标注器   给每张图叠加编号/文件名角标

所有参数名、输出名、提示语均为中文，直接显示在 ComfyUI 编辑界面。
"""

import os
import re

import numpy as np
import torch
from PIL import Image, PngImagePlugin

from .common import (
    alpha_to_mask,
    device_dtype,
    draw_label,
    files_signature,
    find_font,
    list_image_files,
    load_images_varlist,
    load_pil,
    make_contact_sheet,
    parse_json_list,
    pil_to_tensor,
    probe_size,
    resize_to,
    resolve_root,
    root_choices,
    safe_json,
    sort_files,
    tensor_to_pil,
)

CATEGORY = "ZXin/图像批处理"


def _sanitize(name):
    """把文件名清理成安全的文件名字符。"""
    name = os.path.basename(name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    return name or "image"


def _unique_path(folder, filename):
    """避免覆盖同名文件，返回可用的绝对路径。"""
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        path = os.path.join(folder, f"{stem}_{i:03d}{ext}")
        if not os.path.exists(path):
            return path
        i += 1


def _flatten_images(图片列表):
    """把各种形态的图像输入摊平成「单图张量列表」。

    支持三种输入：
    - 单个张量 [B,H,W,C]      → 拆成 B 张
    - 张量列表 [tensor, ...]   → 每项再按 batch 拆开
    - 嵌套列表 [[tensor,...]]  → 递归摊平

    每张图保持各自原始尺寸。
    """
    if 图片列表 is None:
        return []
    if isinstance(图片列表, torch.Tensor):
        if 图片列表.ndim != 4:
            raise ValueError("输入必须是 [批次, 高, 宽, 通道] 的 IMAGE 张量")
        return [图片列表[i:i + 1] for i in range(图片列表.shape[0])]

    out = []
    for item in (图片列表 or []):
        if isinstance(item, torch.Tensor):
            if item.ndim != 4:
                raise ValueError("列表元素必须是 [批次, 高, 宽, 通道] 的 IMAGE 张量")
            out.extend(item[i:i + 1] for i in range(item.shape[0]))
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, torch.Tensor):
                    if sub.ndim != 4:
                        raise ValueError("列表元素必须是 [批次, 高, 宽, 通道] 的 IMAGE 张量")
                    out.extend(sub[i:i + 1] for i in range(sub.shape[0]))
    return out


# =====================================================================
# 1. 批量图像加载器（含文件夹图片预览）
# =====================================================================
class ZXIN_BatchImageLoader:
    """扫描目录并把多张图片加载成一个 IMAGE 批次。

    同时输出一张**文件夹缩略图拼贴**，一眼看清这个文件夹里有哪些图。
    ComfyUI 的 IMAGE 批次要求所有图尺寸一致，本节点会自动统一尺寸。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "加载来源": (root_choices(),),
                "文件名匹配": ("STRING", {"default": "*", "multiline": False}),
                "排序方式": (
                    ["按名称升序", "按名称降序", "按修改时间新到旧", "按修改时间旧到新", "随机"],
                    {"default": "按名称升序"},
                ),
                "随机种子": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF}),
                "最多加载张数": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1}),
                "载入设备": (["跟随系统", "仅CPU"], {"default": "跟随系统"}),
                "生成预览拼贴": ("BOOLEAN", {"default": True}),
                "预览列数": ("INT", {"default": 4, "min": 1, "max": 12, "step": 1}),
                "预览单格大小": ("INT", {"default": 192, "min": 64, "max": 768, "step": 8}),
            },
            "optional": {
                "自定义路径": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "STRING", "INT")
    RETURN_NAMES = ("图片列表", "文件夹预览图", "文件名列表", "加载数量")
    FUNCTION = "load_batch"
    CATEGORY = CATEGORY
    OUTPUT_IS_LIST = (True, False, False, False)
    DESCRIPTION = (
        "从 input/output 目录或自定义路径批量加载图像，**保留每张图的原始尺寸**，"
        "以列表输出。「文件夹预览图」会显示这个文件夹里的图片拼贴，方便确认内容。"
    )
    SEARCH_ALIASES = ["batch load image", "load images batch", "批量加载图片", "批量图像加载", "文件夹预览"]

    # 下拉选项中文 → 内部标识
    _SORT_MAP = {
        "按名称升序": "name_asc",
        "按名称降序": "name_desc",
        "按修改时间新到旧": "mtime_desc",
        "按修改时间旧到新": "mtime_asc",
        "随机": "random",
    }
    _SORT_MAP = {
        "按名称升序": "name_asc",
        "按名称降序": "name_desc",
        "按修改时间新到旧": "mtime_desc",
        "按修改时间旧到新": "mtime_asc",
        "随机": "random",
    }

    @classmethod
    def IS_CHANGED(cls, 加载来源, 文件名匹配, 排序方式, 随机种子, 最多加载张数,
                   载入设备, 生成预览拼贴, 预览列数, 预览单格大小, 自定义路径=""):
        # 目录内容可能在不改变控件的情况下发生变化，所以对文件列表取指纹
        try:
            root = resolve_root(加载来源, 自定义路径)
            paths = sort_files(list_image_files(root, 文件名匹配),
                               cls._SORT_MAP.get(排序方式, "name_asc"), 随机种子)
            return files_signature(paths[:最多加载张数] if 最多加载张数 else paths)
        except Exception:
            return float("NaN")

    def load_batch(self, 加载来源, 文件名匹配, 排序方式, 随机种子, 最多加载张数,
                   载入设备, 生成预览拼贴, 预览列数, 预览单格大小, 自定义路径=""):
        root = resolve_root(加载来源, 自定义路径)
        if not root or not os.path.isdir(root):
            raise ValueError(f"目录不存在或不可访问：{root or '(空)'}")

        sort_key = self._SORT_MAP.get(排序方式, "name_asc")
        paths = sort_files(list_image_files(root, 文件名匹配), sort_key, 随机种子)
        if 最多加载张数 and 最多加载张数 > 0:
            paths = paths[:最多加载张数]
        if not paths:
            raise ValueError(f"目录 {root} 下没有匹配「{文件名匹配}」的图像文件")

        dev, dt = device_dtype()
        if 载入设备 == "仅CPU":
            dev = torch.device("cpu")

        # 保留每张图的原始尺寸，各自独立成张量
        images, names, skipped = load_images_varlist(paths, dev, dt)

        if not images:
            raise ValueError("没有可成功解码的图像")
        if skipped:
            print(f"[ZXIN 批量图像加载器] 跳过 {len(skipped)} 个无法解码的文件：{skipped[:5]}")

        # 文件夹缩略图拼贴（预览用）
        preview = torch.zeros((1, 64, 64, 3), dtype=dt, device=dev)
        if 生成预览拼贴:
            sheet, _layout = make_contact_sheet(
                paths, cols=预览列数, cell=预览单格大小, label=True
            )
            if sheet is not None:
                preview = pil_to_tensor(sheet, dev, dt)

        return (images, preview, safe_json(names), len(names))


# =====================================================================
# 2. 批次分发器
# =====================================================================
class ZXIN_BatchImageDistributor:
    """把图片列表按「每批输出张数」拆成多批，以**列表**输出。

    这是「按一次运行按钮自动跑完所有批次」的关键节点：
    输出为列表后，下游节点会被 ComfyUI 自动执行与批次数相同的轮次，
    每轮收到一批图片，跑完这一轮再自动接下一轮。

    例：目录内 20 张图，「每批输出张数」= 4 → 输出 5 批 → 下游自动跑 5 轮。

    **每批内部允许图片尺寸不同**：加载器保留原始尺寸，本节点按列表分组，
    不会把不同尺寸的图强行拼接。下游节点逐个接收，每轮互不影响。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图片列表": ("IMAGE",),
                "每批输出张数": ("INT", {"default": 4, "min": 1, "max": 1024, "step": 1}),
            },
            "optional": {
                "丢弃不足一批的余数": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "INT")
    RETURN_NAMES = ("批次列表", "分批清单", "批次数")
    FUNCTION = "distribute"
    CATEGORY = CATEGORY
    INPUT_IS_LIST = True
    # 只有图像输出为列表（驱动下游自动逐批执行）；
    # 清单与计数保持普通单值，避免下游收到被拆散的整数/字符串
    OUTPUT_IS_LIST = (True, False, False)
    DESCRIPTION = (
        "把图片列表每「每批输出张数」张分成一批，输出批次列表。"
        "下游节点会自动按批次数逐轮执行（20张图/每批4张=自动跑5轮），全程只点一次运行。"
        "**批内图片尺寸可以不同**，每张图保持原始尺寸。"
    )
    SEARCH_ALIASES = ["distribute batch", "batch split groups", "批次分发", "批量轮次", "分批"]

    def distribute(self, 图片列表, 每批输出张数, 丢弃不足一批的余数=False):
        items = _flatten_images(图片列表)

        每批输出张数 = int(每批输出张数[0]) if isinstance(每批输出张数, list) else int(每批输出张数)
        丢弃不足一批的余数 = (丢弃不足一批的余数[0] if isinstance(丢弃不足一批的余数, list)
                          else 丢弃不足一批的余数)

        size = max(1, 每批输出张数)

        if not items:
            manifest = safe_json({"模式": "列表自动逐批", "每批张数": size,
                                  "批次数": 0, "总张数": 0, "各批图片序号": []})
            return ([], manifest, 0)

        total = len(items)

        # 列表模式：按每批 N 张分组，组内尺寸可以不同
        batches = [items[i:i + size] for i in range(0, total, size)]

        if 丢弃不足一批的余数 and batches and len(batches[-1]) < size:
            batches = batches[:-1]

        # 每批若组内尺寸一致，可合并为一个张量（下游处理更高效）；
        # 尺寸不一致则保持为列表元素逐个传递
        normalized = []
        for batch in batches:
            if not batch:
                normalized.append([])
                continue
            shapes = {tuple(t.shape[1:]) for t in batch}
            if len(shapes) == 1:
                # 同尺寸：拼成 [N,H,W,C]，下游按张量处理
                normalized.append([torch.cat(batch, dim=0)])
            else:
                # 变尺寸：保持独立，逐张传递
                normalized.append(batch)

        manifest = safe_json({
            "模式": "列表自动逐批",
            "每批张数": size,
            "批次数": len(batches),
            "总张数": total,
            "各批图片序号": [[i * size, min((i + 1) * size, total) - 1]
                        for i in range(len(batches))],
        })
        return (normalized, manifest, len(batches))


# =====================================================================
# 3. 并行分发器（可调分支数）
# =====================================================================
class ZXIN_BatchImageSplitter:
    """把图片列表轮流分到多个分支输出，每个分支接一个不同的节点。

    分支数由「分支数」控件决定（1~12），输出端口会**随控件自动增减**：
    分支1 = 第 1、1+分支数、1+2×分支数… 张，分支2 = 第 2、2+分支数… 张，以此类推。

    每个分支的图**各自保持原始尺寸**，可以接到不同节点，
    也可以接到同一个节点的多个 image 输入口。
    """

    # 后端固定最大分支数。前端 JS 扩展会根据「分支数」控件
    # 动态显示/隐藏输出槽，保证可见端口数与实际生效数一致。
    MAX_BRANCHES = 12

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图片列表": ("IMAGE",),
                "分支数": ("INT", {"default": 4, "min": 1, "max": cls.MAX_BRANCHES, "step": 1}),
            },
        }

    RETURN_TYPES = tuple(["IMAGE"] * 12 + ["STRING"])
    RETURN_NAMES = tuple([f"分支{i + 1}" for i in range(12)] + ["分流清单"])
    FUNCTION = "split_branches"
    CATEGORY = CATEGORY
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = tuple([True] * 12 + [False])
    DESCRIPTION = (
        "把图片轮流分到多个分支输出，每个分支接一个不同的节点。"
        "**输出端口会随「分支数」自动增减**（1~12 路）。"
        "每张图保持原始尺寸。"
    )
    SEARCH_ALIASES = ["split into branches", "parallel images", "并行分发", "多路分流"]

    def split_branches(self, 图片列表, 分支数):
        items = _flatten_images(图片列表)

        分支数 = int(分支数[0]) if isinstance(分支数, list) else int(分支数)
        branches = max(1, min(分支数, self.MAX_BRANCHES))

        if not items:
            manifest = safe_json({"模式": "多路并行", "分支数": branches,
                                  "总张数": 0, "各分支图片序号": {}})
            return tuple([[] for _ in range(self.MAX_BRANCHES)] + [manifest])

        total = len(items)
        groups = [[] for _ in range(branches)]
        for i in range(total):
            groups[i % branches].append(i)

        outs = []
        for g in range(self.MAX_BRANCHES):
            if g < branches:
                outs.append([items[i] for i in groups[g]])
            else:
                outs.append([])

        manifest = safe_json({
            "模式": "多路并行",
            "分支数": branches,
            "总张数": total,
            "各分支图片序号": {f"分支{g + 1}": groups[g] for g in range(branches)},
        })
        return tuple(outs + [manifest])


# =====================================================================
# 4. 批量保存器
# =====================================================================
class ZXIN_BatchImageSaver:
    """把批次中的每张图单独保存为文件，并写入 PNG 元数据。

    支持沿用加载器输出的文件名，让保存后的文件名与源文件一一对应。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图片列表": ("IMAGE",),
                "文件名前缀": ("STRING", {"default": "ZXinBatch", "multiline": False}),
                "命名方式": (
                    ["按序号", "沿用源文件名", "前缀加源文件名"],
                    {"default": "按序号"},
                ),
            },
            "optional": {
                "文件名列表": ("STRING", {"default": "", "multiline": False}),
                "写入元数据": ("BOOLEAN", {"default": True}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图片列表", "已保存文件")
    FUNCTION = "save_batch"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True
    DESCRIPTION = "将图片逐张保存到 output 目录，可沿用源文件名，并写入 prompt 等 PNG 元数据。"
    SEARCH_ALIASES = ["save batch images", "batch save png", "批量保存图片", "批量落盘"]

    _NAMING_MAP = {
        "按序号": "index",
        "沿用源文件名": "original",
        "前缀加源文件名": "prefix_original",
    }

    def save_batch(self, 图片列表, 文件名前缀, 命名方式, 文件名列表="",
                   写入元数据=True, prompt=None, extra_pnginfo=None):
        # 兼容列表输入：把列表里的图片摊平成待保存项
        tensors = _flatten_images(图片列表)

        if not tensors:
            raise ValueError("没有可保存的图片")

        if isinstance(文件名前缀, list):
            文件名前缀 = 文件名前缀[0]
        if isinstance(命名方式, list):
            命名方式 = 命名方式[0]
        if isinstance(文件名列表, list):
            文件名列表 = 文件名列表[0] if 文件名列表 else ""
        if isinstance(写入元数据, list):
            写入元数据 = 写入元数据[0] if 写入元数据 else False

        naming = self._NAMING_MAP.get(命名方式, "index")
        first = tensors[0]

        try:
            import folder_paths
            output_dir = folder_paths.get_output_directory()
            full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                文件名前缀, output_dir, first.shape[2], first.shape[1]
            )
        except Exception:
            output_dir = os.path.join(os.getcwd(), "output")
            full_folder = output_dir
            filename = _sanitize(文件名前缀)
            counter = 1
            subfolder = ""

        os.makedirs(full_folder, exist_ok=True)

        orig_names = [str(x) for x in parse_json_list(文件名列表)]

        disable_metadata = False
        try:
            from comfy.cli_args import args
            disable_metadata = bool(getattr(args, "disable_metadata", False))
        except Exception:
            pass

        saved = []
        for i, tensor in enumerate(tensors):
            arr = np.clip(tensor[0].cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

            if naming == "original" and i < len(orig_names):
                out_name = _sanitize(orig_names[i])
                if not out_name.lower().endswith(".png"):
                    out_name = os.path.splitext(out_name)[0] + ".png"
            elif naming == "prefix_original" and i < len(orig_names):
                stem = os.path.splitext(_sanitize(orig_names[i]))[0]
                out_name = f"{filename}_{stem}.png"
            else:
                out_name = f"{filename}_{counter + i:05d}.png"

            path = _unique_path(full_folder, out_name)

            meta = None
            if 写入元数据 and not disable_metadata:
                meta = PngImagePlugin.PngInfo()
                if prompt is not None:
                    meta.add_text("prompt", safe_json(prompt))
                if extra_pnginfo is not None:
                    for key in extra_pnginfo:
                        meta.add_text(str(key), safe_json(extra_pnginfo[key]))

            img.save(path, pnginfo=meta)
            saved.append(path)

        rel = [os.path.relpath(p, output_dir) for p in saved]
        print(f"[ZXIN 批量保存器] 已保存 {len(saved)} 张到 {full_folder}")
        return (tensors, safe_json(rel))


# =====================================================================
# 5. 批量角标标注器
# =====================================================================
class ZXIN_BatchImageAnnotator:
    """给批次中的每张图叠加编号/文件名角标，用于核对分发结果。

    角标以 PIL 绘制，支持中文文件名。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图片列表": ("IMAGE",),
                "标签内容": (
                    ["序号和文件名", "仅序号", "仅文件名", "不添加"],
                    {"default": "序号和文件名"},
                ),
                "标签位置": (
                    ["左上角", "右上角", "左下角", "右下角"],
                    {"default": "左下角"},
                ),
                "字号": ("INT", {"default": 20, "min": 8, "max": 200, "step": 1}),
                "文字颜色": ("STRING", {"default": "#FFFFFF", "multiline": False}),
                "背景颜色": ("STRING", {"default": "#000000", "multiline": False}),
            },
            "optional": {
                "文件名列表": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图片列表",)
    FUNCTION = "annotate"
    CATEGORY = CATEGORY
    DESCRIPTION = "为批次中每张图添加编号/文件名角标，便于核对批量处理与分发的对应关系。"
    SEARCH_ALIASES = ["batch annotate label", "add index text images", "批量打标", "批次编号标注"]

    _MODE_MAP = {
        "序号和文件名": "index_filename",
        "仅序号": "index",
        "仅文件名": "filename",
        "不添加": "none",
    }
    _POS_MAP = {
        "左上角": "top_left",
        "右上角": "top_right",
        "左下角": "bottom_left",
        "右下角": "bottom_right",
    }

    @staticmethod
    def _hex_to_rgb(value, default):
        value = (value or "").strip()
        if not value.startswith("#"):
            return default
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return default
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default

    def annotate(self, 图片列表, 标签内容, 标签位置, 字号, 文字颜色, 背景颜色, 文件名列表=""):
        # 兼容列表输入：摊平后逐张标注，保持各自原始尺寸
        tensors = _flatten_images(图片列表)

        if not tensors:
            raise ValueError("没有可标注的图片")

        if isinstance(标签内容, list):
            标签内容 = 标签内容[0]
        if isinstance(标签位置, list):
            标签位置 = 标签位置[0]
        if isinstance(字号, list):
            字号 = 字号[0]
        if isinstance(文字颜色, list):
            文字颜色 = 文字颜色[0]
        if isinstance(背景颜色, list):
            背景颜色 = 背景颜色[0]
        if isinstance(文件名列表, list):
            文件名列表 = 文件名列表[0] if 文件名列表 else ""

        mode = self._MODE_MAP.get(标签内容, "index_filename")
        if mode == "none":
            return (tensors,)

        position = self._POS_MAP.get(标签位置, "bottom_left")
        orig_names = [str(x) for x in parse_json_list(文件名列表)]
        font = find_font(int(字号))
        fg = self._hex_to_rgb(文字颜色, (255, 255, 255))
        bg = self._hex_to_rgb(背景颜色, (0, 0, 0))

        out = []
        for i, tensor in enumerate(tensors):
            pil = tensor_to_pil(tensor[0]).convert("RGB")
            h, w = pil.size[1], pil.size[0]

            if mode == "index":
                text = str(i)
            elif mode == "filename":
                text = os.path.splitext(orig_names[i])[0] if i < len(orig_names) else str(i)
            else:
                stem = os.path.splitext(orig_names[i])[0] if i < len(orig_names) else ""
                text = f"{i} {stem}".strip() if stem else str(i)

            try:
                bbox = font.getbbox(text)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = font.getsize(text)

            margin = max(2, int(字号 * 0.25))
            bw, bh = tw + margin * 2, th + margin * 2

            if position == "top_left":
                xy = (0, 0)
            elif position == "top_right":
                xy = (max(0, w - bw), 0)
            elif position == "bottom_right":
                xy = (max(0, w - bw), max(0, h - bh))
            else:
                xy = (0, max(0, h - bh))

            draw_label(pil, text, xy, font, fill=fg, bg=bg)
            out.append(pil_to_tensor(pil, tensor.device, tensor.dtype))

        return (out,)
