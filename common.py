"""ComfyUI_ZXin 共用工具函数。

所有节点共享的底层逻辑：目录枚举、图像读写、缩放、字体解析、张量转换。
本模块不依赖 ComfyUI 运行时，可独立用于测试。
"""

import json
import math
import os
import random

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ComfyUI 内置支持的图像扩展名
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif"}

LANCZOS = Image.Resampling.LANCZOS
NEAREST = Image.Resampling.NEAREST


# ---------------------------------------------------------------- 目录 / 文件

def _input_dir():
    try:
        import folder_paths
        return folder_paths.get_input_directory()
    except Exception:
        return ""


def _output_dir():
    try:
        import folder_paths
        return folder_paths.get_output_directory()
    except Exception:
        return ""


def _safe_walk_subdirs(root, max_depth=3):
    """递归收集 root 下所有子目录（相对路径，正斜杠分隔）。

    max_depth 限制往下钻的层数，避免深目录树或网络盘把下拉列表撑爆。
    """
    out = []
    if not root or not os.path.isdir(root):
        return out
    root_depth = root.rstrip(os.sep).count(os.sep)
    for cur, dirs, _ in os.walk(root):
        dirs.sort()
        if cur.rstrip(os.sep).count(os.sep) - root_depth >= max_depth:
            dirs[:] = []
        for d in dirs:
            rel = os.path.relpath(os.path.join(cur, d), root)
            if rel != ".":
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


_ROOT_CHOICES_CACHE = {"key": None, "value": None}


def root_choices(max_depth=3):
    """生成目录下拉选项：input / output 及其子目录。

    形如 "input"、"input/Pose图"、"output/batch_01"。
    结果按 (根目录, 目录条目数) 做短缓存，避免每次 INPUT_TYPES 都重扫磁盘。
    """
    key = None
    try:
        key = []
        for base in (_input_dir(), _output_dir()):
            if base and os.path.isdir(base):
                key.append((base, len(os.listdir(base))))
            else:
                key.append((base, -1))
        key = tuple(key)
    except OSError:
        key = None

    if key is not None and _ROOT_CHOICES_CACHE["key"] == key:
        return list(_ROOT_CHOICES_CACHE["value"])

    choices = []
    for label, base in (("input", _input_dir()), ("output", _output_dir())):
        if not base:
            continue
        choices.append(label)
        for sub in _safe_walk_subdirs(base, max_depth):
            choices.append(f"{label}/{sub}")
    choices = choices or ["input"]

    if key is not None:
        _ROOT_CHOICES_CACHE["key"] = key
        _ROOT_CHOICES_CACHE["value"] = list(choices)
    return choices


def input_subfolder_choices():
    """只列出 input 目录下的**一级子文件夹**，用于快捷选择。

    保留给外部（含测试）使用；加载节点本身用的是 root_choices() 的完整树。
    返回形如 ["(input 根目录)", "Pose图", "其它图"]。
    """
    base = _input_dir()
    if not base or not os.path.isdir(base):
        return ["(input 根目录)"]

    out = ["(input 根目录)"]
    try:
        for name in sorted(os.listdir(base)):
            if os.path.isdir(os.path.join(base, name)):
                out.append(name)
    except OSError:
        pass
    return out


def resolve_input_subfolder(name):
    """把一级子文件夹名解析为绝对路径；传根目录标记则返回 input 根。"""
    base = _input_dir()
    if not base:
        return ""
    name = (name or "").strip()
    if not name or name.startswith("("):
        return os.path.abspath(base)
    return os.path.abspath(os.path.join(base, name.replace("/", os.sep)))


def resolve_root(source, custom_path=""):
    """把下拉选项值解析为真实绝对路径；custom_path 非空时优先生效。"""
    if custom_path:
        return os.path.abspath(os.path.expanduser(custom_path.strip()))

    source = (source or "").strip()
    if source.startswith("output"):
        base = _output_dir()
        rest = source[len("output"):].lstrip("/")
    else:
        base = _input_dir()
        rest = source[len("input"):].lstrip("/")

    if not base:
        return ""
    return os.path.abspath(os.path.join(base, rest.replace("/", os.sep))) if rest else os.path.abspath(base)


def list_image_files(root, pattern="*", recursive=False):
    """列出 root 下匹配 pattern 的图像文件。

    recursive=False（默认）只扫描一层；True 时连子目录一起扫描。
    pattern 支持 glob 语法，例如 "*.png"、"Pose_*"。
    """
    if not root or not os.path.isdir(root):
        return []
    import fnmatch

    pat = (pattern or "*").strip() or "*"

    def match(name):
        return pat == "*" or fnmatch.fnmatch(name, pat)

    out = []
    try:
        if not recursive:
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                if not os.path.isfile(path):
                    continue
                if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                    continue
                if match(name):
                    out.append(path)
            return out

        for cur, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                    continue
                if not match(name):
                    continue
                out.append(os.path.join(cur, name))
    except OSError:
        return []
    return out


# 目录扫描缓存：IS_CHANGED 与真正加载会各扫一遍同一个目录，
# 用「目录 mtime + 条目数」做校验键，命中就复用，省掉一次磁盘遍历。
_DIR_CACHE = {}
_DIR_CACHE_MAX = 64


def cached_list_images(root, pattern="*", recursive=False):
    """带缓存的目录扫描；目录内容变化（mtime / 条目数）时自动失效。"""
    stamp = None
    try:
        stamp = (os.stat(root).st_mtime_ns, len(os.listdir(root)))
    except OSError:
        stamp = None

    key = (root, pattern, bool(recursive))
    if stamp is not None:
        hit = _DIR_CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return list(hit[1])

    paths = list_image_files(root, pattern, recursive)
    if stamp is not None:
        if len(_DIR_CACHE) >= _DIR_CACHE_MAX:
            _DIR_CACHE.clear()
        _DIR_CACHE[key] = (stamp, list(paths))
    return paths


def sort_files(paths, sort_mode="name_asc", seed=0):
    """按指定规则排序文件列表。"""
    mode = sort_mode or "name_asc"

    if mode == "name_desc":
        return sorted(paths, key=lambda p: os.path.basename(p).lower(), reverse=True)
    if mode == "mtime_desc":
        return sorted(paths, key=lambda p: (os.path.getmtime(p), os.path.basename(p)), reverse=True)
    if mode == "mtime_asc":
        return sorted(paths, key=lambda p: (os.path.getmtime(p), os.path.basename(p)))
    if mode == "random":
        rng = random.Random(int(seed))
        out = list(paths)
        rng.shuffle(out)
        return out
    return sorted(paths, key=lambda p: os.path.basename(p).lower())


def files_signature(paths):
    """文件列表的内容指纹（名称 + mtime + size），用于 IS_CHANGED。"""
    import hashlib

    h = hashlib.sha256()
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        h.update(os.path.basename(p).encode("utf-8", "replace"))
        h.update(str(st.st_mtime_ns).encode())
        h.update(str(st.st_size).encode())
    return h.hexdigest()


# ---------------------------------------------------------------- 图像处理

def probe_size(path):
    """只读取图片尺寸，不解码像素。"""
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def load_pil(path):
    """读取图片为 PIL 对象（含 EXIF 方向修正），失败返回 None。"""
    try:
        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        return im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    except Exception:
        return None


def resize_to(img, w, h, mode="stretch"):
    """把 PIL 图片调整到 (w, h)。

    stretch —— 直接拉伸填满（可能有形变）
    contain —— 等比缩放后居中放置，空余处补黑
    cover   —— 等比缩放后居中裁切，铺满不留边
    """
    w, h = max(1, int(w)), max(1, int(h))
    if img.size == (w, h):
        return img

    if mode == "stretch":
        return img.resize((w, h), LANCZOS)

    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        return img.resize((w, h), LANCZOS)

    if mode == "contain":
        scale = min(w / sw, h / sh)
        nw, nh = max(1, int(round(sw * scale))), max(1, int(round(sh * scale)))
        resized = img.resize((nw, nh), LANCZOS)
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2))
        return canvas

    # cover
    scale = max(w / sw, h / sh)
    nw, nh = max(1, math.ceil(sw * scale)), max(1, math.ceil(sh * scale))
    resized = img.resize((nw, nh), LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return resized.crop((left, top, left + w, top + h))


def pil_to_tensor(img, device, dtype):
    """RGB PIL → ComfyUI IMAGE 张量 [1, H, W, 3]，取值 0~1。

    原地做除法，避免 `astype(...)/255.0` 额外产生一份 float32 中间数组
    （大图下每张可省约 1/3 的峰值内存）。
    """
    rgb = img.convert("RGB")
    arr = np.array(rgb, dtype=np.uint8, copy=True)
    out = np.empty(arr.shape, dtype=np.float32)
    np.multiply(arr, np.float32(1.0 / 255.0), out=out, casting="unsafe")
    t = torch.from_numpy(out)[None]
    if t.dtype != dtype or t.device != torch.device(device):
        t = t.to(device=device, dtype=dtype)
    return t


def load_images_varlist(paths, device, dtype, max_side=0,
                        thumb_limit=0, thumb_size=192,
                        start=0, count=0):
    """按原始尺寸加载多张图片，返回 IMAGE 张量列表。

    每张图独立成张量 [1,H,W,C]，**尺寸互不相同也不需要相同**。
    这是分发场景的正确形态：下游节点按列表逐个接收，
    同一节点的多个 image 输入口也可以各自接不同尺寸的图。

    max_side > 0 时把长边等比缩放到不超过该值（防止超大图撑爆内存），
    max_side = 0 表示完全保留原始尺寸。

    thumb_limit > 0 时额外返回前 thumb_limit 张的缩略图（PIL，长边不超过
    thumb_size），供预览拼贴复用，避免同一批图片被解码两次。

    start / count 用于**惰性分批**：只解码 [start, start+count) 这一段
    （count<=0 表示一直取到末尾）。这是控制峰值内存的唯一有效手段 ——
    ComfyUI 的 IMAGE 端口只收张量，返回 N 张就必然同时驻留 N 张。

    返回 (张量列表, 文件名列表, 跳过列表, 缩略图列表[(路径, PIL)])。
    """
    total = len(paths)
    start = max(0, min(int(start), total))
    end = total if not count or count <= 0 else min(total, start + int(count))
    paths = paths[start:end]

    out, names, skipped, thumbs = [], [], [], []
    for p in paths:
        img = load_pil(p)
        if img is None:
            skipped.append(os.path.basename(p))
            continue
        img = img.convert("RGB")

        if max_side and max_side > 0:
            w, h = img.size
            longest = max(w, h)
            if longest > max_side:
                scale = max_side / float(longest)
                img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), LANCZOS)

        if thumb_limit and len(thumbs) < thumb_limit:
            thumb = img.copy()
            thumb.thumbnail((max(1, int(thumb_size)), max(1, int(thumb_size))), LANCZOS)
            thumbs.append((p, thumb))

        out.append(pil_to_tensor(img, device, dtype))
        names.append(os.path.basename(p))
    return out, names, skipped, thumbs


def alpha_to_mask(img, w, h, device, dtype):
    """取 alpha 通道生成 MASK 张量 [1, H, W]（ComfyUI 约定：1 为有效区域）。

    无 alpha 时返回全 0（与内置 LoadImage 行为一致）。
    """
    if img.mode not in ("RGBA", "LA"):
        return torch.zeros((1, h, w), dtype=dtype, device=device)

    alpha = img.convert("RGBA").getchannel("A")
    if alpha.size != (w, h):
        alpha = alpha.resize((w, h), NEAREST)
    arr = np.array(alpha).astype(np.float32) / 255.0
    mask = torch.from_numpy(arr)[None]
    return mask.to(device=device, dtype=dtype)


def tensor_to_pil(tensor):
    """ComfyUI IMAGE 张量 [H, W, 3] → PIL RGB。"""
    arr = np.clip(tensor.cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


# ---------------------------------------------------------------- 字体

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


# 解析成功的字体路径缓存在这里（只是记录，不修改 _FONT_CANDIDATES，
# 否则每次调用都会往全局候选表头部插入，列表会无限膨胀）
_RESOLVED_FONT = {"path": None}


def find_font(size=16):
    """解析一个可用的 TrueType 字体（优先中文字体），失败回退到内置位图字体。"""
    candidates = list(_FONT_CANDIDATES)

    # ComfyUI 自带 fonts 目录优先（若存在）
    try:
        import folder_paths
        comfy_fonts = os.path.join(os.path.dirname(os.path.abspath(folder_paths.__file__)), "fonts")
        if os.path.isdir(comfy_fonts):
            extra = [os.path.join(comfy_fonts, n) for n in sorted(os.listdir(comfy_fonts))
                     if n.lower().endswith((".ttf", ".otf", ".ttc"))]
            candidates = extra + candidates
    except Exception:
        pass

    cached = _RESOLVED_FONT.get("path")
    if cached:
        candidates.insert(0, cached)

    for path in candidates:
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, size)
                _RESOLVED_FONT["path"] = path
                return font
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def draw_label(canvas, text, box_xy, font, fill=(255, 255, 255), bg=(0, 0, 0)):
    """在画布上绘制带底色的标签文字。"""
    draw = ImageDraw.Draw(canvas)
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = font.getsize(text)

    x, y = box_xy
    pad = max(2, int(th * 0.18))
    draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2], fill=bg)
    draw.text((x + pad, y + pad), text, fill=fill, font=font)
    return (tw + pad * 2, th + pad * 2)


def make_contact_sheet(paths, cols=4, cell=192, gap=6, bg=(24, 24, 24),
                       label=True, max_items=None, preloaded=None):
    """把多张图片拼成一张网格缩略图，用于快速浏览文件夹内容。

    preloaded 传入 [(路径, PIL 缩略图), ...] 时可复用已解码的图片，
    避免同一批文件被解码两次（加载器刚解过一次的情况）。

    max_items 为 None 时不限制拼贴张数；大于 0 时最多拼这么多张。
    **不要在这里写死上限** —— 上限应由调用方按实际加载量与内存预算决定，
    否则会出现「设了最多加载 100 张、预览却只有 64 张」这类不一致。

    返回 (PIL 图像, 拼贴布局信息列表)。单张图读取失败会被跳过。
    布局信息每项为 {"行", "列", "文件名"}，便于核对顺序。
    """
    cell = max(32, int(cell))
    gap = max(0, int(gap))
    cols = max(1, int(cols))
    limit = None if max_items is None else max(0, int(max_items))

    cached = {p: img for p, img in (preloaded or [])}

    tiles = []
    for path in paths:
        if limit is not None and len(tiles) >= limit:
            break
        img = cached.get(path)
        if img is None:
            img = load_pil(path)
            if img is None:
                continue
        tiles.append((path, img.convert("RGB")))

    if not tiles:
        return None, []

    rows = (len(tiles) + cols - 1) // cols
    sheet_w = cols * cell + (cols + 1) * gap
    sheet_h = rows * cell + (rows + 1) * gap
    sheet = Image.new("RGB", (sheet_w, sheet_h), bg)

    font = None
    if label:
        font = find_font(max(12, int(cell * 0.11)))

    layout = []
    for idx, (path, img) in enumerate(tiles):
        r, c = divmod(idx, cols)
        x0 = gap + c * (cell + gap)
        y0 = gap + r * (cell + gap)

        tile = resize_to(img, cell, cell, "cover")
        sheet.paste(tile, (x0, y0))

        if label and font is not None:
            name = os.path.splitext(os.path.basename(path))[0]
            text = name[:18]
            try:
                bbox = font.getbbox(text)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = font.getsize(text)
            pad = max(2, int(th * 0.2))
            draw = ImageDraw.Draw(sheet)
            draw.rectangle([x0, y0 + cell - th - pad * 2, x0 + tw + pad * 2, y0 + cell],
                           fill=(0, 0, 0))
            draw.text((x0 + pad, y0 + cell - th - pad), text, fill=(255, 255, 255), font=font)

        layout.append({"行": r + 1, "列": c + 1, "文件名": os.path.basename(path)})

    return sheet, layout


# ---------------------------------------------------------------- 运行时辅助

def device_dtype():
    """获取 ComfyUI 的中间设备与中间精度；不可用时回退 CPU / float32。"""
    try:
        import comfy.model_management as mm
        return mm.intermediate_device(), mm.intermediate_dtype()
    except Exception:
        return torch.device("cpu"), torch.float32


def safe_json(obj):
    """序列化为 JSON 字符串，无法编码的对象转字符串。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def estimate_memory_mb(shapes):
    """按 ComfyUI IMAGE 的常驻形态（float32 / 3 通道）估算内存占用（MB）。

    shapes 为 [(宽, 高), ...]。用于加载大目录前给用户一个直观的开销提示。
    """
    pixels = sum(int(w) * int(h) for w, h in shapes if w and h)
    return pixels * 3 * 4 / (1024.0 * 1024.0)


# ---------------------------------------------------------------- 内存探测

def available_memory_mb():
    """探测当前可用物理内存（MB）。零额外依赖，失败返回 0。

    Windows 走 GlobalMemoryStatusEx，Linux 读 /proc/meminfo，
    macOS 用 sysctl；都拿不到就返回 0，调用方需自行兜底。
    """
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return float(st.ullAvailPhys) / (1024.0 * 1024.0)
            return 0.0

        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return float(line.split()[1]) / 1024.0
            return 0.0

        out = os.popen("sysctl -n hw.memsize 2>/dev/null").read().strip()
        if out.isdigit():
            # macOS 没有直接的可用量，用总量的一半做个保守估计
            return float(out) / (1024.0 * 1024.0) * 0.5
    except Exception:
        pass
    return 0.0


def plan_chunk(paths, budget_mb, probe_limit=24):
    """按内存预算规划「一次解码多少张」，返回 (块大小, 预估总MB, 单张平均MB)。

    budget_mb <= 0 时表示不限制，返回 (len(paths), 总MB, 平均MB)。
    抽样式预估：只 probe 前 probe_limit 张的尺寸，避免为算预算而扫全目录。
    """
    sample = [s for s in (probe_size(p) for p in paths[:probe_limit]) if s]
    if not sample:
        return len(paths), 0.0, 0.0

    avg_mb = estimate_memory_mb([(w, h) for w, h in sample]) / float(len(sample))
    total_mb = avg_mb * len(paths)

    if budget_mb and budget_mb > 0 and avg_mb > 0:
        chunk = int(budget_mb / avg_mb)
        # 至少解码 1 张，否则永远推不动
        return max(1, min(len(paths), chunk)), total_mb, avg_mb

    return len(paths), total_mb, avg_mb


def parse_json_list(text):
    """解析 JSON 数组字符串；失败时按行拆分。"""
    if not text:
        return []
    text = text.strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return [line for line in text.splitlines() if line.strip()]
