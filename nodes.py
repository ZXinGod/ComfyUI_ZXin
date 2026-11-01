"""ComfyUI_ZXin —— 图像批处理与分发节点集。

5 个节点，覆盖一条完整链路：
1. 批量图像加载器   目录扫描 → 图片列表（保留原始尺寸）+ 文件夹预览
2. 批次分发器       每批 N 张 → 批次列表，驱动下游自动逐批执行（只按一次运行）
3. 并行分发器       图片轮流分到 N 个分支（输出端口随分支数自动增减）
4. 批量保存器       逐张落盘 + PNG 元数据
5. 批量角标标注器   给每张图叠加编号/文件名角标

关于「一批里有不同尺寸的图」
    ComfyUI 的 IMAGE 是 [批次,高,宽,通道] 张量，一个端口一次只能收一个张量，
    而张量要求批内高宽一致。所以批内尺寸不一致时有三种取舍（见分发器的
    「变尺寸处理方式」）：逐张单独执行（完整保留原始尺寸，一批 N 张跑 N 轮）、
    留白统一、等比裁切。批内尺寸一致时不受影响，仍是一轮 N 张。

关于列表语义（关键）
    OUTPUT_IS_LIST=True 时，返回列表的【每个元素就是下游执行一轮时拿到的值】。
    因此元素必须是张量本身，不能再多套一层 list —— 否则下游标准节点
    （KSampler / VAEEncode / SaveImage 等）会收到 Python list 而报错。
    清单、计数这类非图像输出保持普通单值，避免被拆散。

所有参数名、输出名、提示语均为中文，直接显示在 ComfyUI 编辑界面。
"""

import os
import re

import numpy as np
import torch
from PIL import Image, PngImagePlugin

from .common import (
    available_memory_mb,
    cached_list_images,
    device_dtype,
    draw_label,
    estimate_memory_mb,
    files_signature,
    find_font,
    load_images_varlist,
    make_contact_sheet,
    parse_json_list,
    pil_to_tensor,
    plan_chunk,
    probe_size,
    resolve_root,
    resize_to,
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


_UI_SUPPORT = {"result": None}


def supports_result_ui():
    """探测当前 ComfyUI 是否支持 {"ui":..., "result":...} 的返回形式。

    ComfyUI 0.42 的 execution.get_output_from_returns 支持该写法，
    老版本只认 tuple。探测失败时一律走安全的 tuple 返回。
    """
    if _UI_SUPPORT["result"] is None:
        ok = False
        try:
            import inspect

            import execution
            src = inspect.getsource(execution.get_output_from_returns)
            ok = "'result' in r" in src or '"result" in r' in src
        except Exception:
            ok = False
        _UI_SUPPORT["result"] = ok
    return _UI_SUPPORT["result"]


# =====================================================================
# 惰性分批加载：服务端游标 + 前端自动排队
# =====================================================================
# ComfyUI 的 IMAGE 端口只收 float32 张量，加载器返回 N 张就必然同时驻留
# N 张，「延迟到用时再解码」在架构上做不到。唯一有效的省内存手段是
# **一次只解码一小批**，剩下的靠多次执行推进。
#
# 推进方式：加载器内部维护游标，每执行完一批就通过 WebSocket 发
# zxin.lazy_continue 事件，前端收到后自动再排一次队，直到全部跑完。
# 游标以节点 ID（UNIQUE_ID）为键，多个加载器节点互不干扰。

_LAZY_AUTO = "自动（超内存阈值才惰性）"
_LAZY_OFF = "全部载入（不惰性）"
_LAZY_ON = "惰性分批"

_LAZY_STATE = {}
_LAZY_STATE_MAX = 32
_LAZY_PUSH_WARNED = {"done": False}

# 预估总量超过可用资源的这个比例 → 启用惰性分批
_LAZY_TRIGGER_RATIO = 0.5
# 单批预算占可用资源的比例。比触发阈值更保守：
# ComfyUI 本身还要常驻模型权重，不能让加载器把空闲资源吃干
_LAZY_BUDGET_RATIO = 0.25


def _lazy_push(node_id, payload):
    """通知前端「还有下一批」，驱动自动排队。失败时降级为手动重复运行。

    只在第一次失败时打印提示 —— 每批都刷一条警告会淹没有用的进度信息。
    """
    try:
        from server import PromptServer

        PromptServer.instance.send_sync("zxin.lazy_continue", payload)
        return True
    except Exception as e:
        if not _LAZY_PUSH_WARNED["done"]:
            _LAZY_PUSH_WARNED["done"] = True
            print(f"[ZXIN 批量图像加载器] 自动续跑通知不可用，将改为手动重复运行：{e}")
        return False


def _lazy_get(key):
    st = _LAZY_STATE.get(key)
    if st is None and len(_LAZY_STATE) >= _LAZY_STATE_MAX:
        # 简单淘汰：丢掉最早插入的键，防止长期运行后无限增长
        for k in list(_LAZY_STATE)[:_LAZY_STATE_MAX // 2]:
            _LAZY_STATE.pop(k, None)
    return st


def _progress_push(node_id, count, progress, total=0):
    """把「加载数量 / 加载进度」实时推给前端，在节点上直接显示。

    与 _lazy_push 分开：这条只负责显示，失败不影响主流程，
    所以异常一律静默吞掉，绝不因前端不可用而中断加载。
    """
    try:
        from server import PromptServer

        PromptServer.instance.send_sync("zxin.loader_progress", {
            "node": str(node_id or ""),
            "count": int(count or 0),
            "total": int(total or 0),
            "progress": str(progress or ""),
        })
    except Exception:
        pass


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
                "随机种子": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF,
                                   "control_after_generate": True}),
                "最多加载张数": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1}),
                "载入设备": (["跟随系统", "仅CPU"], {"default": "跟随系统"}),
                "生成预览拼贴": ("BOOLEAN", {"default": True}),
                "预览列数": ("INT", {"default": 4, "min": 1, "max": 12, "step": 1}),
                "预览单格大小": ("INT", {"default": 192, "min": 64, "max": 768, "step": 8}),
                # ↓ 新增控件一律追加在末尾：前端 widgets_values 按位置赋值，
                #   插在中间会让旧工作流的控件值整体错位
                "含子文件夹": ("BOOLEAN", {"default": False}),
                "限制最大边长": ("INT", {
                    "default": 0, "min": 0, "max": 16384, "step": 8,
                    "tooltip": "0 = 完全保留原始尺寸；大于 0 时把长边等比缩放到不超过该值，"
                               "用于避免超大目录一次性载入撑爆内存。",
                }),
                # ↓ 惰性分批控件（v1.2 新增，仍追加在末尾）
                "加载模式": ([_LAZY_AUTO, _LAZY_OFF, _LAZY_ON],
                             {"default": _LAZY_AUTO,
                              "tooltip": "「自动」= 预估内存超过可用内存一半时才分批；"
                                         "「惰性分批」= 无论大小都分批；"
                                         "「全部载入」= 一次性载入（旧行为）。"}),
                "单批内存上限": ("INT", {
                    "default": 0, "min": 0, "max": 262144, "step": 256,
                    "tooltip": "单位 MB。0 = 自动取当前可用内存的一半作为单批预算；"
                               "大于 0 时按该值切分每批张数。仅在分批生效时使用。",
                }),
                # ↓ 预览张数上限（追加在末尾，破坏性最小位置）
                "预览最多显示张数": ("INT", {
                    "default": 0, "min": 0, "max": 4096, "step": 1,
                    "tooltip": "文件夹预览拼贴最多显示多少张。0 = 与实际加载张数一致。"
                               "拼贴是单张超长图，张数过多会占用大量显存，"
                               "此时可在此限制预览张数以控制开销（不影响实际加载数量）。",
                }),
            },
            "optional": {
                "自定义路径": ("STRING", {"default": "", "multiline": False}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "STRING", "INT", "STRING")
    RETURN_NAMES = ("图片列表", "文件夹预览图", "文件名列表", "加载数量", "加载进度")
    FUNCTION = "load_batch"
    CATEGORY = CATEGORY
    OUTPUT_IS_LIST = (True, False, False, False, False)
    DESCRIPTION = (
        "从 input/output 目录或自定义路径批量加载图像，**保留每张图的原始尺寸**，"
        "以列表输出。「文件夹预览图」会显示这个文件夹里的图片拼贴，方便确认内容。"
        "可勾选「含子文件夹」递归扫描，也可用「限制最大边长」控制内存开销。"
        "目录过大时（预估内存超过可用内存一半）自动切换为**惰性分批**："
        "每次只解码一小批，跑完自动续排下一批，直到全部处理完。"
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

    @classmethod
    def VALIDATE_INPUTS(cls, 加载来源, 自定义路径="", 文件名匹配="*", 限制最大边长=0, **kwargs):
        """在排队阶段就校验路径，避免跑到一半才报「目录不存在」。"""
        if 加载来源 not in root_choices():
            return f"「加载来源」已失效（{加载来源}）。若为新建目录，请重启 ComfyUI 后重试。"
        if 自定义路径 and 自定义路径.strip():
            path = os.path.abspath(os.path.expanduser(自定义路径.strip()))
            if not os.path.isdir(path):
                return f"自定义路径不是有效目录：{path}"
        else:
            root = resolve_root(加载来源, 自定义路径)
            if not root or not os.path.isdir(root):
                return f"目录不存在或不可访问：{root or '(空)'}"
        return True

    @staticmethod
    def _lazy_avail_mb(device=None):
        """估算可用于图像张量的资源（MB）。

        张量放在 CUDA 上时必须按**显存**算 —— 拿系统内存当预算会严重高估，
        直接导致显存 OOM。取不到就退回系统内存探测。
        """
        if device is not None and getattr(device, "type", "") == "cuda":
            try:
                free, _total = torch.cuda.mem_get_info(device)
                return float(free) / (1024.0 * 1024.0)
            except Exception:
                pass
        return available_memory_mb()

    @classmethod
    def _lazy_plan(cls, paths, 加载模式, 单批内存上限=0, device=None):
        """决定是否走惰性分批，返回 (惰性开关, 块大小, 预估总MB, 预算MB)。"""
        total = len(paths)

        if 加载模式 == _LAZY_OFF:
            return False, total, 0.0, 0.0

        avail = cls._lazy_avail_mb(device)
        # 预算：用户指定优先；否则取可用资源的 _LAZY_BUDGET_RATIO
        budget = float(单批内存上限) if 单批内存上限 and 单批内存上限 > 0 \
            else avail * _LAZY_BUDGET_RATIO
        budget = max(0.0, budget)

        chunk, total_mb, _avg = plan_chunk(paths, budget)
        chunk = max(1, min(total, chunk))   # 至少 1 张，否则永远推不动

        if 加载模式 == _LAZY_ON:
            return True, chunk, total_mb, budget

        # 自动：预估总量超过可用资源的一半才分批
        lazy = avail > 0 and total_mb > avail * _LAZY_TRIGGER_RATIO
        return lazy, (chunk if lazy else total), total_mb, budget

    @classmethod
    def IS_CHANGED(cls, 加载来源, 文件名匹配, 排序方式, 随机种子, 最多加载张数,
                   载入设备, 生成预览拼贴, 预览列数, 预览单格大小, 自定义路径="",
                   含子文件夹=False, 限制最大边长=0, 加载模式=_LAZY_AUTO,
                   单批内存上限=0, 预览最多显示张数=0, **kwargs):
        # 目录内容可能在不改变控件的情况下发生变化，所以对文件列表取指纹
        try:
            root = resolve_root(加载来源, 自定义路径)
            paths = sort_files(cached_list_images(root, 文件名匹配, 含子文件夹),
                               cls._SORT_MAP.get(排序方式, "name_asc"), 随机种子)
            if 最多加载张数 and 最多加载张数 > 0:
                paths = paths[:最多加载张数]
            sig = files_signature(paths)

            # 与执行函数用同一套判定（含设备），否则可能出现
            # 「IS_CHANGED 认为不用分批、执行时却分批」的错位
            dev, _dt = device_dtype()
            if 载入设备 == "仅CPU":
                dev = torch.device("cpu")
            lazy, _chunk, _total_mb, _budget = cls._lazy_plan(paths, 加载模式, 单批内存上限, dev)
            if not lazy:
                return sig

            # 分批模式下把游标并入指纹：每推进一批返回值就变，强制重新执行
            key = str(kwargs.get("unique_id") or "global")
            st = _lazy_get(key)
            if st and st.get("finished"):
                # 上一轮已跑完，用户再次点击时从头开始
                _LAZY_STATE.pop(key, None)
                return sig + "#reset"
            return "%s#%d" % (sig, int((st or {}).get("cursor", 0)))
        except Exception:
            return float("NaN")

    def load_batch(self, 加载来源, 文件名匹配, 排序方式, 随机种子, 最多加载张数,
                   载入设备, 生成预览拼贴, 预览列数, 预览单格大小, 自定义路径="",
                   含子文件夹=False, 限制最大边长=0, 加载模式=_LAZY_AUTO,
                   单批内存上限=0, 预览最多显示张数=0, unique_id=None):
        """加载一批图片。惰性模式下只解码 [cursor, cursor+块大小) 这一段。"""
        root = resolve_root(加载来源, 自定义路径)
        if not root or not os.path.isdir(root):
            raise ValueError(f"目录不存在或不可访问：{root or '(空)'}")

        sort_key = self._SORT_MAP.get(排序方式, "name_asc")
        paths = sort_files(cached_list_images(root, 文件名匹配, 含子文件夹), sort_key, 随机种子)
        if 最多加载张数 and 最多加载张数 > 0:
            paths = paths[:最多加载张数]
        if not paths:
            raise ValueError(f"目录 {root} 下没有匹配「{文件名匹配}」的图像文件")

        dev, dt = device_dtype()
        if 载入设备 == "仅CPU":
            dev = torch.device("cpu")

        # ---- 惰性分批：决定本次解码哪一段 ----
        total = len(paths)
        lazy, chunk, total_mb, budget = self._lazy_plan(paths, 加载模式, 单批内存上限, dev)

        key = str(unique_id or "global")
        sig = files_signature(paths)
        st = _lazy_get(key)
        if not st or st.get("sig") != sig or st.get("finished"):
            st = {"sig": sig, "cursor": 0, "total": total, "finished": False}

        if lazy:
            start = max(0, min(int(st.get("cursor", 0)), total))
            end = min(total, start + max(1, int(chunk)))
        else:
            start, end = 0, total
        if start >= end:  # 兜底：游标越界就从头开始
            start, end = 0, min(total, max(1, end - start or chunk or total))

        # 保留每张图的原始尺寸，各自独立成张量
        # 预览需要前若干张缩略图，顺带在解码时一并生成，避免二次解码
        # 预取数量与拼贴上限一致：少了会导致拼贴时重新解码（等于白做惰性），
        # 多了则白白占用内存
        if 生成预览拼贴:
            if 预览最多显示张数 and 预览最多显示张数 > 0:
                thumb_limit = min(end - start, int(预览最多显示张数))
            else:
                thumb_limit = end - start
        else:
            thumb_limit = 0
        images, names, skipped, thumbs = load_images_varlist(
            paths, dev, dt, max_side=限制最大边长,
            thumb_limit=thumb_limit, thumb_size=预览单格大小,
            start=start, count=(end - start),
        )

        if not images:
            raise ValueError("没有可成功解码的图像")
        if skipped:
            print(f"[ZXIN 批量图像加载器] 跳过 {len(skipped)} 个无法解码的文件：{skipped[:5]}")

        actual_mb = estimate_memory_mb([(t.shape[2], t.shape[1]) for t in images])
        shapes = {tuple(t.shape[1:3]) for t in images}

        # ---- 推进游标，必要时通知前端续跑 ----
        st["cursor"] = end
        st["total"] = total
        st["finished"] = end >= total
        _LAZY_STATE[key] = st

        if lazy:
            batch_total = max(1, (total + max(1, chunk) - 1) // max(1, chunk))
            batch_no = min(batch_total, (start // max(1, chunk)) + 1)
            print(
                f"[ZXIN 批量图像加载器] 惰性分批：第 {batch_no}/{batch_total} 批，"
                f"本批 {len(images)} 张（{len(shapes)} 种尺寸），进度 {end}/{total}，"
                f"本批约 {actual_mb:.0f} MB / 预算 {budget:.0f} MB（总量预估 {total_mb:.0f} MB）"
            )
            if not st["finished"]:
                _lazy_push(key, {
                    "node": key,
                    "done": end,
                    "total": total,
                    "remaining": total - end,
                    "batch": batch_no,
                    "batch_total": batch_total,
                })
                progress = (
                    f"惰性分批：已加载 {end}/{total} 张（第 {batch_no}/{batch_total} 批，"
                    f"每批 {chunk} 张）。剩余 {total - end} 张将自动续跑。"
                )
            else:
                progress = (
                    f"惰性分批：已加载 {end}/{total} 张（第 {batch_no}/{batch_total} 批），"
                    f"全部完成。再次点击运行将从头开始。"
                )
        else:
            print(
                f"[ZXIN 批量图像加载器] 已加载 {len(images)} 张（{len(shapes)} 种尺寸），"
                f"设备 {dev}，占用约 {actual_mb:.0f} MB"
            )
            progress = f"全部载入：{len(images)} 张一次载入完成，占用约 {actual_mb:.0f} MB"

        # 文件夹缩略图拼贴（预览用）
        # 惰性模式下只对【本批】生成：传全部 paths 会让 make_contact_sheet
        # 为没有预载缩略图的图片重新解码，那就等于把整批都解了，白做惰性
        preview = torch.zeros((1, 64, 64, 3), dtype=dt, device=dev)
        if 生成预览拼贴:
            # 预览张数上限 = min(本批实际张数, 用户设定值)
            # 不能写死上限，否则会出现「设了加载 100 张、预览只有 64 张」
            batch_count = len(paths[start:end])
            if 预览最多显示张数 and 预览最多显示张数 > 0:
                sheet_limit = min(batch_count, int(预览最多显示张数))
            else:
                sheet_limit = batch_count
            sheet, _layout = make_contact_sheet(
                paths[start:end], cols=预览列数, cell=预览单格大小, label=True,
                max_items=sheet_limit, preloaded=thumbs
            )
            if sheet is not None:
                preview = pil_to_tensor(sheet, dev, dt)

        # 实时把数量与进度推到前端，直接显示在节点上
        _progress_push(key, len(images), progress, total=total)

        # 保持元组返回：OUTPUT_IS_LIST 的多轮调度依赖这个形状，
        # 改成 {"ui":..., "result":...} 会破坏下游与既有调用方
        return (images, preview, safe_json(names), len(images), progress)


# =====================================================================
# 2. 批次分发器
# =====================================================================
class ZXIN_BatchImageDistributor:
    """把图片列表按「每批输出张数」拆成多批，以**列表**输出。

    这是「按一次运行按钮自动跑完所有批次」的关键节点：
    输出为列表后，下游节点会被 ComfyUI 自动执行与批次数相同的轮次，
    每轮收到一批图片，跑完这一轮再自动接下一轮。

    例：目录内 20 张图，「每批输出张数」= 4 → 输出 5 批 → 下游自动跑 5 轮。

    **每批内部允许图片尺寸不同**。由于一个 IMAGE 端口一次只能接收一个张量，
    而张量要求批内 H/W 一致，所以批内尺寸不同时有三种处理方式（见
    「变尺寸处理方式」）：
    - 逐张单独执行（默认）：保留原始尺寸，一批 N 张变成 N 轮，每轮 1 张
    - 留白统一：等比缩放后补黑边到批内最大尺寸，仍是一轮 N 张
    - 等比裁切：裁到批内最小尺寸，仍是一轮 N 张
    """

    # 批内尺寸不一致时的处理策略
    VARSIZE_EACH = "逐张单独执行（保留原始尺寸）"
    VARSIZE_PAD = "留白统一到批内最大尺寸"
    VARSIZE_COVER = "等比裁切到批内最小尺寸"
    VARSIZE_MODES = [VARSIZE_EACH, VARSIZE_PAD, VARSIZE_COVER]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图片列表": ("IMAGE",),
                "每批输出张数": ("INT", {"default": 4, "min": 1, "max": 1024, "step": 1}),
            },
            "optional": {
                "丢弃不足一批的余数": ("BOOLEAN", {"default": False}),
                # ↓ 追加在末尾，避免旧工作流的 widgets_values 错位
                "变尺寸处理方式": (cls.VARSIZE_MODES, {
                    "default": cls.VARSIZE_EACH,
                    "tooltip": "批内图片尺寸不一致时如何处理。默认「逐张单独执行」"
                               "完全保留原始尺寸，代价是一批 N 张会跑 N 轮。",
                }),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "INT")
    RETURN_NAMES = ("批次列表", "分批清单", "执行轮数")
    FUNCTION = "distribute"
    CATEGORY = CATEGORY
    INPUT_IS_LIST = True
    # 只有图像输出为列表（驱动下游自动逐批执行）；
    # 清单与计数保持普通单值，避免下游收到被拆散的整数/字符串
    OUTPUT_IS_LIST = (True, False, False)
    DESCRIPTION = (
        "把图片列表每「每批输出张数」张分成一批，输出批次列表。"
        "下游节点会自动逐轮执行（20张图/每批4张=自动跑5轮），全程只点一次运行。"
        "**批内图片尺寸可以不同**：默认逐张单独执行，完整保留原始尺寸；"
        "也可选择留白/裁切统一尺寸以保持一轮多张。"
    )
    SEARCH_ALIASES = ["distribute batch", "batch split groups", "批次分发", "批量轮次", "分批"]

    @staticmethod
    def _pick(value, default=0):
        """标量输入在 INPUT_IS_LIST 下会被包成列表，这里统一取值。"""
        if isinstance(value, list):
            value = value[0] if value else default
        return value

    def distribute(self, 图片列表, 每批输出张数, 丢弃不足一批的余数=False,
                   变尺寸处理方式=None):
        items = _flatten_images(图片列表)

        每批输出张数 = self._pick(每批输出张数, 4)
        丢弃不足一批的余数 = bool(self._pick(丢弃不足一批的余数, False))
        var_mode = self._pick(变尺寸处理方式, self.VARSIZE_EACH) or self.VARSIZE_EACH

        size = max(1, int(每批输出张数))

        empty_manifest = {
            "模式": "列表自动逐批", "每批张数": size, "分组批次数": 0,
            "执行轮数": 0, "总张数": 0, "变尺寸处理方式": var_mode, "各批": [],
        }
        if not items:
            return ([], safe_json(empty_manifest), 0)

        total = len(items)

        # 按每批 N 张分组，组内尺寸可以不同
        batches = [items[i:i + size] for i in range(0, total, size)]

        if 丢弃不足一批的余数 and batches and len(batches[-1]) < size:
            batches = batches[:-1]

        # 输出列表的【每个元素 = 下游执行一轮时拿到的值】。
        # 因此这里必须是「张量」，不能再多套一层 list —— 否则下游
        # 标准节点（KSampler / VAEEncode / SaveImage 等）会收到 Python list 而报错。
        normalized = []
        batch_info = []

        for bi, batch in enumerate(batches):
            if not batch:
                continue
            start = bi * size
            shapes = {tuple(t.shape[1:]) for t in batch}

            if len(shapes) == 1:
                # 同尺寸：拼成一个 [N,H,W,C] 张量，下游一轮拿到 N 张
                merged = torch.cat(batch, dim=0) if len(batch) > 1 else batch[0]
                normalized.append(merged)
                batch_info.append({
                    "批号": bi + 1,
                    "图片序号": list(range(start, min(start + size, total))),
                    "张数": len(batch),
                    "执行轮数": 1,
                    "尺寸": [f"{int(batch[0].shape[2])}x{int(batch[0].shape[1])}"],
                })
                continue

            # ---- 批内尺寸不一致 ----
            if var_mode == self.VARSIZE_EACH:
                # 逐张：每轮一张，完整保留各自原始尺寸
                normalized.extend(batch)
                batch_info.append({
                    "批号": bi + 1,
                    "图片序号": list(range(start, min(start + size, total))),
                    "张数": len(batch),
                    "执行轮数": len(batch),
                    "尺寸": [f"{int(t.shape[2])}x{int(t.shape[1])}" for t in batch],
                })
            else:
                # 统一尺寸后仍拼成一个张量，保持一轮 N 张
                if var_mode == self.VARSIZE_PAD:
                    tw = max(int(t.shape[2]) for t in batch)
                    th = max(int(t.shape[1]) for t in batch)
                    fit = "contain"
                else:
                    tw = min(int(t.shape[2]) for t in batch)
                    th = min(int(t.shape[1]) for t in batch)
                    fit = "cover"
                fitted = []
                for t in batch:
                    pil = tensor_to_pil(t[0])
                    fitted.append(pil_to_tensor(resize_to(pil, tw, th, fit), t.device, t.dtype))
                normalized.append(torch.cat(fitted, dim=0))
                batch_info.append({
                    "批号": bi + 1,
                    "图片序号": list(range(start, min(start + size, total))),
                    "张数": len(batch),
                    "执行轮数": 1,
                    "尺寸": [f"{tw}x{th}(统一)"],
                })

        manifest = safe_json({
            "模式": "列表自动逐批",
            "每批张数": size,
            "分组批次数": len(batches),
            "执行轮数": len(normalized),
            "总张数": total,
            "变尺寸处理方式": var_mode,
            "各批": batch_info,
        })
        return (normalized, manifest, len(normalized))


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
    # 显式声明接收/输出列表，不再依赖引擎的隐式展开行为
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = (True, False)
    DESCRIPTION = "将图片逐张保存到 output 目录，可沿用源文件名，并写入 prompt 等 PNG 元数据。"
    SEARCH_ALIASES = ["save batch images", "batch save png", "批量保存图片", "批量落盘"]

    _NAMING_MAP = {
        "按序号": "index",
        "沿用源文件名": "original",
        "前缀加源文件名": "prefix_original",
    }

    @staticmethod
    def _pick(value, default=""):
        if isinstance(value, list):
            return value[0] if value else default
        return value

    def save_batch(self, 图片列表, 文件名前缀, 命名方式, 文件名列表="",
                   写入元数据=True, prompt=None, extra_pnginfo=None):
        # 列表输入：把列表里的图片摊平成待保存项（各自保持原始尺寸）
        tensors = _flatten_images(图片列表)

        if not tensors:
            raise ValueError("没有可保存的图片")

        文件名前缀 = self._pick(文件名前缀, "ZXinBatch")
        命名方式 = self._pick(命名方式, "按序号")
        文件名列表 = self._pick(文件名列表, "")
        写入元数据 = bool(self._pick(写入元数据, True))
        if isinstance(prompt, list):
            prompt = prompt[0] if prompt else None
        if isinstance(extra_pnginfo, list):
            extra_pnginfo = extra_pnginfo[0] if extra_pnginfo else None

        naming = self._NAMING_MAP.get(命名方式, "index")

        try:
            import folder_paths
            output_dir = folder_paths.get_output_directory()
        except Exception:
            output_dir = os.path.join(os.getcwd(), "output")

        def save_dir_for(w, h):
            """按图片自身尺寸取保存目录，变尺寸时各图进各自的 %width%x%height% 目录。"""
            try:
                import folder_paths
                full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                    文件名前缀, output_dir, w, h
                )
                return full_folder, filename, int(counter)
            except Exception:
                return output_dir, _sanitize(文件名前缀), 1

        base_counter = save_dir_for(int(tensors[0].shape[2]), int(tensors[0].shape[1]))[2]

        orig_names = [str(x) for x in parse_json_list(文件名列表)]

        disable_metadata = False
        try:
            from comfy.cli_args import args
            disable_metadata = bool(getattr(args, "disable_metadata", False))
        except Exception:
            pass

        saved, ui_images = [], []
        for i, tensor in enumerate(tensors):
            arr = np.clip(tensor[0].cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

            w, h = int(tensor.shape[2]), int(tensor.shape[1])
            full_folder, filename, _counter = save_dir_for(w, h)
            os.makedirs(full_folder, exist_ok=True)

            if naming == "original" and i < len(orig_names):
                out_name = _sanitize(orig_names[i])
                if not out_name.lower().endswith(".png"):
                    out_name = os.path.splitext(out_name)[0] + ".png"
            elif naming == "prefix_original" and i < len(orig_names):
                stem = os.path.splitext(_sanitize(orig_names[i]))[0]
                out_name = f"{filename}_{stem}.png"
            else:
                out_name = f"{filename}_{base_counter + i:05d}.png"

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

            rel = os.path.relpath(path, output_dir).replace("\\", "/")
            ui_images.append({
                "filename": os.path.basename(rel),
                "subfolder": os.path.dirname(rel),
                "type": "output",
            })

        rel_all = [os.path.relpath(p, output_dir) for p in saved]
        print(f"[ZXIN 批量保存器] 已保存 {len(saved)} 张到 {output_dir}")

        result = (tensors, safe_json(rel_all))
        # ComfyUI 0.42 支持 {"ui":..., "result":...} 同时回传预览与输出；
        # 老版本只认 tuple，这里做能力探测，不支持时安全退回。
        if supports_result_ui():
            return {"ui": {"images": ui_images}, "result": result}
        return result


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
    # 显式声明接收/输出列表，不再依赖引擎的隐式展开行为
    INPUT_IS_LIST = True
    OUTPUT_IS_LIST = (True,)
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

    @staticmethod
    def _pick(value, default=""):
        if isinstance(value, list):
            return value[0] if value else default
        return value

    def annotate(self, 图片列表, 标签内容, 标签位置, 字号, 文字颜色, 背景颜色, 文件名列表=""):
        # 列表输入：摊平后逐张标注，保持各自原始尺寸
        tensors = _flatten_images(图片列表)

        if not tensors:
            raise ValueError("没有可标注的图片")

        标签内容 = self._pick(标签内容, "序号和文件名")
        标签位置 = self._pick(标签位置, "左下角")
        字号 = self._pick(字号, 20)
        文字颜色 = self._pick(文字颜色, "#FFFFFF")
        背景颜色 = self._pick(背景颜色, "#000000")
        文件名列表 = self._pick(文件名列表, "")

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
