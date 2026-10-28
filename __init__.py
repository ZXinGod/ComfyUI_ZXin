"""ComfyUI_ZXin —— ComfyUI 自定义节点包入口。

导出 NODE_CLASS_MAPPINGS 后，ComfyUI 启动时会扫描并注册其中的节点。

5 个节点，每个用途单一：
① 批量图像加载器   目录扫描 → 图片列表（保留原始尺寸）+ 文件夹预览
② 批次分发器       每批 N 张 → 批次列表，下游自动逐批执行
③ 并行分发器       图片轮流分到 4 个分支，各接一个不同节点
④ 批量保存器       逐张落盘 + PNG 元数据
⑤ 批量角标标注器   叠加编号/文件名角标
"""

from .nodes import (
    ZXIN_BatchImageAnnotator,
    ZXIN_BatchImageDistributor,
    ZXIN_BatchImageLoader,
    ZXIN_BatchImageSaver,
    ZXIN_BatchImageSplitter,
)

NODE_CLASS_MAPPINGS = {
    "ZXIN_BatchImageLoader": ZXIN_BatchImageLoader,
    "ZXIN_BatchImageDistributor": ZXIN_BatchImageDistributor,
    "ZXIN_BatchImageSplitter": ZXIN_BatchImageSplitter,
    "ZXIN_BatchImageSaver": ZXIN_BatchImageSaver,
    "ZXIN_BatchImageAnnotator": ZXIN_BatchImageAnnotator,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ZXIN_BatchImageLoader": "① 批量图像加载器",
    "ZXIN_BatchImageDistributor": "② 批次分发器（自动逐批执行）",
    "ZXIN_BatchImageSplitter": "③ 并行分发器（可调分支数）",
    "ZXIN_BatchImageSaver": "④ 批量保存器",
    "ZXIN_BatchImageAnnotator": "⑤ 批量角标标注器",
}

# 前端扩展：让并行分发器的输出端口随「分支数」动态增减
WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
