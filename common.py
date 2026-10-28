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


def _safe_walk_subdirs(root):
    """递归收集 root 下所有子目录（相对路径，正斜杠分隔）。"""
    out = []
    if not root or not os.path.isdir(root):
        return out
    for cur, dirs, _ in os.walk(root):
        dirs.sort()
        for d in dirs:
            rel = os.path.relpath(os.path.join(cur, d), root)
            if rel != ".":
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def root_choices():
    """生成目录下拉选项：input / output 及其子目录。

    形如 "input"、"input/Pose图"、"output/batch_01"。
    """
    choices = []
    for label, base in (("input", _input_dir()), ("output", _output_dir())):
        if not base:
            continue
        choices.append(label)
        for sub in _safe_walk_subdirs(base):
            choices.append(f"{label}/{sub}")
    return choices or ["input"]


def input_subfolder_choices():
    """只列出 input 目录下的**一级子文件夹**，用于快捷选择。

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


def list_image_files(root, pattern="*"):
    """列出 root 下匹配 pattern 的图像文件（仅一层，不递归子目录）。

    pattern 支持 glob 语法，例如 "*.png"、"Pose_*"。
    """
    if not root or not os.path.isdir(root):
        return []
    import fnmatch

    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []

    out = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
            continue
        if pattern and pattern.strip() != "*" and not fnmatch.fnmatch(name, pattern.strip()):
            continue
        out.append(path)
    return out


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
    """RGB PIL → ComfyUI IMAGE 张量 [1, H, W, 3]，取值 0~1。"""
    arr = np.array(img.convert("RGB")).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None].to(device=device, dtype=dtype)


def load_images_varlist(paths, device, dtype):
    """按原始尺寸加载多张图片，返回 IMAGE 张量列表。

    每张图独立成张量 [1,H,W,C]，**尺寸互不相同也不需要相同**。
    这是分发场景的正确形态：下游节点按列表逐个接收，
    同一节点的多个 image 输入口也可以各自接不同尺寸的图。
    """
    out, names, skipped = [], [], []
    for p in paths:
        img = load_pil(p)
        if img is None:
            skipped.append(os.path.basename(p))
            continue
        out.append(pil_to_tensor(img.convert("RGB"), device, dtype))
        names.append(os.path.basename(p))
    return out, names, skipped


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


def interpolate_batch(images, w, h, mode="bicubic"):
    """用 torch 插值调整整个批次的尺寸（IMAGE 内部使用，优于逐张 PIL 处理）。"""
    if mode == "nearest-exact":
        imode = "nearest-exact"
    elif mode == "area":
        imode = "area"
    elif mode == "bilinear":
        imode = "bilinear"
    else:
        imode = "bicubic"

    # [B, H, W, C] → [B, C, H, W]
    samples = images.movedim(-1, 1)
    if imode in ("nearest-exact", "area", "bilinear", "bicubic"):
        kwargs = {"align_corners": False} if imode in ("bilinear", "bicubic") else {}
        samples = torch.nn.functional.interpolate(samples, size=(h, w), mode=imode, **kwargs)
    else:
        samples = torch.nn.functional.interpolate(samples, size=(h, w), mode=imode)
    return samples.movedim(1, -1)


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


def find_font(size=16):
    """解析一个可用的 TrueType 字体（优先中文字体），失败回退到内置位图字体。"""
    try:
        import folder_paths
        comfy_fonts = os.path.join(os.path.dirname(os.path.abspath(folder_paths.__file__)), "fonts")
        if os.path.isdir(comfy_fonts):
            for name in sorted(os.listdir(comfy_fonts)):
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    _FONT_CANDIDATES.insert(0, os.path.join(comfy_fonts, name))
    except Exception:
        pass

    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
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
                       label=True, max_items=64):
    """把多张图片拼成一张网格缩略图，用于快速浏览文件夹内容。

    返回 (PIL 图像, 拼贴布局信息列表)。单张图读取失败会被跳过。
    布局信息每项为 {"行", "列", "文件名"}，便于核对顺序。
    """
    cell = max(32, int(cell))
    gap = max(0, int(gap))
    cols = max(1, int(cols))

    tiles = []
    for path in paths:
        if len(tiles) >= max_items:
            break
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
