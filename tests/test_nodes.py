"""ComfyUI_ZXin 真机验证脚本。

在 ComfyUI 的 Python 环境中运行，模拟节点注册流程，
并对每个节点执行功能冒烟测试。

用法：
    # Windows 便携版
    python_embeded/python.exe tests/test_nodes.py
    # 或指定 ComfyUI 位置
    set COMFY_ROOT=D:/path/to/ComfyUI && python tests/test_nodes.py
"""

import asyncio
import inspect
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(HERE)          # ComfyUI_ZXin 包目录
PKG_PARENT = os.path.dirname(PKG_DIR)    # 包的父目录

# 自动探测 ComfyUI 位置（不硬编码任何人的路径）
sys.path.insert(0, HERE)
from comfy_paths import require_comfy_root  # noqa: E402

COMFY_ROOT = require_comfy_root()

# 把 ComfyUI 主目录放进 sys.path，才能 import folder_paths / comfy.model_management
if COMFY_ROOT not in sys.path:
    sys.path.insert(0, COMFY_ROOT)
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, e))
        print(f"  [FAIL] {name}: {e}")
        traceback.print_exc()


def make_test_images(root, count=20, size=(256, 256)):
    """造一批测试图片（含不同尺寸与 alpha，验证各种分支）。"""
    from PIL import Image
    import numpy as np

    os.makedirs(root, exist_ok=True)
    for p in os.listdir(root):
        if p.lower().endswith((".png", ".jpg", ".jpeg")):
            os.remove(os.path.join(root, p))

    for i in range(count):
        w = size[0] if i % 3 else size[0] + 64
        h = size[1] if i % 2 else size[1] + 32
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        arr[..., 0] = (i * 37) % 256
        arr[..., 1] = (i * 71) % 256
        arr[..., 2] = (i * 113) % 256
        img = Image.fromarray(arr, "RGB")
        if i == 0:
            img = img.convert("RGBA")
            img.putalpha(Image.new("L", (w, h), 128))
        img.save(os.path.join(root, f"test_{i:02d}.png"))
    return root


def clean_output_artifacts():
    """清理上一次测试留下的输出文件，保证测试可重复执行。

    保存器带有防覆盖逻辑（同名文件自动加 _001 后缀），
    若不清理，第二次运行的文件名会变化，断言将不再成立。
    """
    out_dir = os.path.join(COMFY_ROOT, "output")
    if not os.path.isdir(out_dir):
        return
    for name in os.listdir(out_dir):
        if name.startswith(("ZXinTest", "ZXinOrig", "ZXinDup", "alpha", "beta")) and name.lower().endswith(".png"):
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass


def main():
    print("=" * 68)
    print("ComfyUI_ZXin 真机验证")
    print("=" * 68)
    print(f"ComfyUI 根目录: {COMFY_ROOT}")
    print(f"节点包目录    : {PKG_DIR}")

    # ---------------------------------------------------------- 1. 导入
    print("\n[1] 按 ComfyUI 生产加载机制导入节点包")
    import importlib.util

    try:
        # 复刻 ComfyUI nodes.load_custom_node 的加载方式：
        # spec_from_file_location + sys.modules 注册 + exec_module
        pkg_name = "ComfyUI_ZXin"
        spec = importlib.util.spec_from_file_location(
            pkg_name, os.path.join(PKG_DIR, "__init__.py")
        )
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = pkg
        spec.loader.exec_module(pkg)
        print(f"  模块: {pkg.__file__}")
        print(f"  相对导入正常，节点数: {len(pkg.NODE_CLASS_MAPPINGS)}")
    except Exception as e:
        print(f"  致命错误：无法导入节点包：{e}")
        traceback.print_exc()
        return 1

    # ---------------------------------------------------------- 2. 注册表
    print("\n[2] 检查 NODE_CLASS_MAPPINGS 注册表")

    def check_registry():
        assert hasattr(pkg, "NODE_CLASS_MAPPINGS"), "缺少 NODE_CLASS_MAPPINGS"
        assert hasattr(pkg, "NODE_DISPLAY_NAME_MAPPINGS"), "缺少 NODE_DISPLAY_NAME_MAPPINGS"
        assert "NODE_CLASS_MAPPINGS" in pkg.__all__, "__all__ 未包含 NODE_CLASS_MAPPINGS"

        mapping = pkg.NODE_CLASS_MAPPINGS
        display = pkg.NODE_DISPLAY_NAME_MAPPINGS
        assert len(mapping) == 5, f"节点数量应为 5，实际 {len(mapping)}"

        for key, cls in mapping.items():
            assert key in display, f"{key} 缺少显示名"
            for attr in ("INPUT_TYPES", "RETURN_TYPES", "FUNCTION", "CATEGORY"):
                assert hasattr(cls, attr), f"{key} 缺少属性 {attr}"
            assert isinstance(cls.RETURN_TYPES, tuple), f"{key} RETURN_TYPES 必须是 tuple"
            assert hasattr(cls, cls.FUNCTION), f"{key} 的 FUNCTION 指向的方法不存在"
            assert callable(cls.INPUT_TYPES), f"{key} INPUT_TYPES 不可调用"

            spec = cls.INPUT_TYPES()
            assert "required" in spec, f"{key} INPUT_TYPES 缺少 required"
            ins = spec.get("required", {})
            outs = spec.get("optional", {})
            hidden = spec.get("hidden", {})
            assert isinstance(ins, dict) and isinstance(outs, dict), f"{key} 输入定义必须是 dict"

            # 执行函数的参数必须都在输入里（执行引擎按 kwargs 传参）
            sig = inspect.signature(cls.__dict__[cls.FUNCTION])
            params = [p for p in sig.parameters if p != "self"]
            has_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if not has_kwargs:
                for p in params:
                    assert p in ins or p in outs or p in hidden, \
                        f"{key} 的执行函数参数 {p} 未在 INPUT_TYPES 中声明"

            print(f"    {key}: 输入 {len(ins)} 必填 / {len(outs)} 可选, 输出 {len(cls.RETURN_TYPES)}")

    check("注册表完整性", check_registry)

    # ---------------------------------------------------------- 3. 契约细节
    print("\n[3] 检查 V1 契约细节")

    def check_contract():
        mapping = pkg.NODE_CLASS_MAPPINGS

        for key, cls in mapping.items():
            assert len(cls.RETURN_TYPES) >= 1, f"{key} RETURN_TYPES 为空"
            if len(cls.RETURN_TYPES) == 1:
                assert isinstance(cls.RETURN_TYPES, tuple), f"{key} 单输出需为 tuple"
            if hasattr(cls, "OUTPUT_IS_LIST"):
                assert len(cls.OUTPUT_IS_LIST) == len(cls.RETURN_TYPES), \
                    f"{key} OUTPUT_IS_LIST 长度与 RETURN_TYPES 不符"
            assert cls.CATEGORY.startswith("ZXin"), f"{key} CATEGORY 异常"

        loader = mapping["ZXIN_BatchImageLoader"]
        assert hasattr(loader, "IS_CHANGED"), "Loader 缺少 IS_CHANGED"

    check("V1 契约细节", check_contract)

    # ---------------------------------------------------------- 3.5 中文显示
    print("\n[3.5] 检查参数中文化")

    def check_chinese():
        mapping = pkg.NODE_CLASS_MAPPINGS
        for key, cls in mapping.items():
            spec = cls.INPUT_TYPES()
            for section in ("required", "optional"):
                for pname in spec.get(section, {}):
                    assert any("\u4e00" <= ch <= "\u9fff" for ch in pname), \
                        f"{key} 的参数「{pname}」未中文化"
            for rname in getattr(cls, "RETURN_NAMES", ()):
                assert any("\u4e00" <= ch <= "\u9fff" for ch in rname), \
                    f"{key} 的输出名「{rname}」未中文化"
            # 显示名与描述也应为中文
            disp = pkg.NODE_DISPLAY_NAME_MAPPINGS[key]
            assert any("\u4e00" <= ch <= "\u9fff" for ch in disp), \
                f"{key} 的显示名「{disp}」未中文化"

        # 中文参数必须能被 JSON 序列化（前端 /object_info 依赖）
        info = {
            k: {"input": cls.INPUT_TYPES(), "output": list(cls.RETURN_TYPES)}
            for k, cls in mapping.items()
        }
        text = json.dumps(info, ensure_ascii=False)
        restored = json.loads(text)
        assert restored["ZXIN_BatchImageLoader"]["input"]["required"].get("加载来源") is not None, \
            "中文参数名 JSON 往返失败"
        print(f"    全部参数/输出名均为中文，JSON 序列化正常（{len(text)} 字符）")

    check("参数与输出名中文化", check_chinese)

    # ---------------------------------------------------------- 4. 功能冒烟
    print("\n[4] 功能冒烟测试")

    tmp_input = os.path.join(COMFY_ROOT, "input", "_zxin_test")
    make_test_images(tmp_input, count=20)
    clean_output_artifacts()

    import torch
    mapping = pkg.NODE_CLASS_MAPPINGS

    def test_loader():
        """加载器保留原始尺寸，以列表输出。"""
        node = mapping["ZXIN_BatchImageLoader"]()
        out = node.load_batch(
            加载来源="input/_zxin_test",
            文件名匹配="*.png",
            排序方式="按名称升序",
            随机种子=0,
            最多加载张数=0,
            载入设备="仅CPU",
            生成预览拼贴=False, 预览列数=4, 预览单格大小=128, 自定义路径="",
        )
        images, preview, names, count = out
        assert isinstance(images, list), f"图片列表必须是 list: {type(images)}"
        assert len(images) == count == 20, f"数量错误: {len(images)} vs {count}"
        assert preview.ndim == 4, f"预览图维度错误: {preview.shape}"
        assert "test_00.png" in names, f"文件名列表异常: {names}"
        # 每张图独立，尺寸可以不同
        for t in images:
            assert t.ndim == 4 and t.shape[0] == 1, f"单图形状错误: {t.shape}"
            assert t.shape[3] == 3, f"通道数错误: {t.shape}"
        # 关键：测试图片本身就故意做成不同尺寸
        shapes = {tuple(t.shape[1:3]) for t in images}
        assert len(shapes) > 1, f"测试图片应包含不同尺寸，实际都是 {shapes}"
        print(f"    加载 {len(images)} 张，尺寸种类 {len(shapes)} 种（保留原始尺寸）")

    check("批量加载器 (保留原始尺寸)", test_loader)

    def test_loader_no_size_error():
        """关键回归：尺寸不一致时不再报错。"""
        node = mapping["ZXIN_BatchImageLoader"]()
        try:
            out = node.load_batch(
                加载来源="input/_zxin_test", 文件名匹配="*.png", 排序方式="按名称升序",
                随机种子=0, 最多加载张数=0, 载入设备="仅CPU",
                生成预览拼贴=False, 预览列数=4, 预览单格大小=128, 自定义路径="",
            )
        except ValueError as e:
            raise AssertionError(f"尺寸不一致不应报错，但报错了：{e}")
        assert len(out[0]) == 20, "应正常加载全部图片"
        print("    尺寸不一致不再抛异常")

    check("批量加载器 (尺寸不一致不再报错)", test_loader_no_size_error)

    def test_loader_custom_path():
        node = mapping["ZXIN_BatchImageLoader"]()
        out = node.load_batch(
            加载来源="input", 文件名匹配="*.png", 排序方式="按名称升序", 随机种子=0,
            最多加载张数=0, 载入设备="仅CPU",
            生成预览拼贴=False, 预览列数=4, 预览单格大小=128, 自定义路径=tmp_input,
        )
        assert len(out[0]) == 20, f"自定义路径加载失败: {len(out[0])}"
        print(f"    自定义路径加载 {len(out[0])} 张")

    check("批量加载器 (自定义路径)", test_loader_custom_path)

    def test_loader_max_images():
        node = mapping["ZXIN_BatchImageLoader"]()
        out = node.load_batch(
            加载来源="input/_zxin_test", 文件名匹配="*.png", 排序方式="按名称升序",
            随机种子=0, 最多加载张数=3, 载入设备="仅CPU",
            生成预览拼贴=False, 预览列数=4, 预览单格大小=128, 自定义路径="",
        )
        assert len(out[0]) == 3, f"最多加载张数未生效: {len(out[0])}"
        print(f"    最多加载张数=3 实际取到 {len(out[0])} 张")

    check("批量加载器 (最多加载张数截断)", test_loader_max_images)

    def test_loader_subfolder_choice():
        """加载来源下拉应能列出 input 的一级子文件夹。"""
        from ComfyUI_ZXin.common import input_subfolder_choices, resolve_input_subfolder

        choices = input_subfolder_choices()
        assert choices and choices[0].startswith("("), f"子文件夹列表异常: {choices}"
        print(f"    input 子文件夹下拉: {choices}")

    check("加载器支持 input 子文件夹选择", test_loader_subfolder_choice)

    def test_distributor_list_mode():
        """列表模式：20 张 / 每批 4 张 → 5 批。"""
        node = mapping["ZXIN_BatchImageDistributor"]()
        # 模拟加载器输出：20 张独立张量（尺寸可不同）
        imgs = [torch.rand(1, 16 + i, 16 + i, 3) for i in range(20)]
        out = node.distribute(imgs, 4, False)
        batches = out[0]
        assert isinstance(batches, list), "第1批输出必须是 list"
        assert len(batches) == 5, f"批次数错误: {len(batches)}"
        assert out[2] == 5, f"批次数输出错误: {out[2]}"
        manifest = json.loads(out[1])
        assert manifest["批次数"] == 5 and manifest["总张数"] == 20, f"清单异常: {manifest}"
        # 顺序必须保持：第1批应是前4张
        first = batches[0]
        assert isinstance(first, list), "每批必须是 list"
        # 变尺寸时每批保持 4 个独立元素；同尺寸时合并为 1 个 [4,H,W,C] 张量
        assert len(first) in (1, 4), f"第1批结构异常: {len(first)}"
        print(f"    20 张 → 每批 4 张 → {len(batches)} 批，顺序正确")

    check("批次分发器 (列表模式 20→5批)", test_distributor_list_mode)

    def test_distributor_varlist():
        """关键：不同尺寸的图可以正常分组，不报错。"""
        node = mapping["ZXIN_BatchImageDistributor"]()
        imgs = [torch.rand(1, 10 + i * 7, 20 + i * 5, 3) for i in range(12)]
        out = node.distribute(imgs, 4, False)
        batches = out[0]
        assert len(batches) == 3, f"批次数错误: {len(batches)}"
        # 每批内尺寸不同 → 保持为独立的 list 元素，不强行 cat
        for bi, batch in enumerate(batches):
            assert isinstance(batch, list), f"第{bi + 1}批应为 list"
            assert len(batch) == 4, f"第{bi + 1}批张数错误: {len(batch)}"
        print("    变尺寸图片分组正常，批内尺寸不强制统一")

    check("批次分发器 (变尺寸图片分组)", test_distributor_varlist)

    def test_distributor_same_size_merged():
        """同尺寸的批会合并成张量，下游处理更高效。"""
        node = mapping["ZXIN_BatchImageDistributor"]()
        imgs = [torch.rand(1, 32, 32, 3) for _ in range(8)]
        out = node.distribute(imgs, 4, False)
        batches = out[0]
        assert len(batches) == 2, f"批次数错误: {len(batches)}"
        # 同尺寸时每批为 [tensor] 形式（长度1，内含 [4,H,W,C]）
        assert len(batches[0]) == 1, f"同尺寸批应合并为单张量: {len(batches[0])}"
        assert batches[0][0].shape[0] == 4, f"合并后张量张数错误: {batches[0][0].shape}"
        print("    同尺寸批次自动合并为张量")

    check("批次分发器 (同尺寸自动合并)", test_distributor_same_size_merged)

    def test_distributor_drop_last():
        node = mapping["ZXIN_BatchImageDistributor"]()
        imgs = [torch.rand(1, 16, 16, 3) for _ in range(20)]
        out = node.distribute(imgs, 6, True)
        batches = out[0]
        assert len(batches) == 3, f"丢弃余数后批次数错误: {len(batches)}"
        print(f"    20 张 / 每批 6 张 / 丢弃余数 → {len(batches)} 批")

    check("批次分发器 (丢弃余数)", test_distributor_drop_last)

    def test_splitter_four_branch():
        """并行分发器：轮流分到 4 个分支，各接一个不同节点。"""
        node = mapping["ZXIN_BatchImageSplitter"]()
        imgs = [torch.rand(1, 16 + i, 16 + i, 3) for i in range(8)]
        out = node.split_branches(imgs, 4)
        counts = [len(out[i]) for i in range(4)]
        assert counts == [2, 2, 2, 2], f"四分支分配不均: {counts}"
        assert sum(counts) == 8, f"总数不守恒: {counts}"
        assert all(isinstance(out[i], list) for i in range(4)), "分支输出必须是 list"
        # 分支1 = 第1、5张；分支2 = 第2、6张；依此类推
        assert torch.equal(out[0][0], imgs[0]), "分支1 首图错误"
        assert torch.equal(out[0][1], imgs[4]), "分支1 第二张应为第5张"
        assert torch.equal(out[3][0], imgs[3]), "分支4 首图应为第4张"
        manifest = json.loads(out[-1])
        assert manifest["分支数"] == 4, f"清单异常: {manifest}"
        assert manifest["各分支图片序号"]["分支1"] == [0, 4], f"索引映射异常: {manifest}"
        print(f"    四路并行: " + " / ".join(str(c) for c in counts))

    check("并行分发器 (四路分支)", test_splitter_four_branch)

    def test_splitter_dynamic_branch_count():
        """关键：分支数可调（1~12），输出端口随之增减。"""
        node = mapping["ZXIN_BatchImageSplitter"]()
        imgs = [torch.rand(1, 16, 16, 3) for _ in range(12)]

        for n in (1, 2, 3, 5, 7, 8, 12):
            out = node.split_branches(imgs, n)
            counts = [len(out[i]) for i in range(n)]
            assert sum(counts) == 12, f"{n} 分支总数不守恒: {counts}"
            assert all(c > 0 for c in counts), f"{n} 分支有空的: {counts}"
            # 未使用的分支必须为空
            for g in range(n, 12):
                assert len(out[g]) == 0, f"{n} 分支时第{g + 1}路应为空: {len(out[g])}"
            manifest = json.loads(out[-1])
            assert manifest["分支数"] == n, f"清单分支数错误: {manifest}"
        print(f"    分支数 1/2/3/5/7/8/12 全部正确")

    check("并行分发器 (分支数动态可调 1~12)", test_splitter_dynamic_branch_count)

    def test_splitter_output_contract():
        """后端必须固定 12 路输出 + 1 个清单，保证执行契约稳定。"""
        split = mapping["ZXIN_BatchImageSplitter"]
        assert split.MAX_BRANCHES == 12, f"MAX_BRANCHES 应为 12: {split.MAX_BRANCHES}"
        assert len(split.RETURN_TYPES) == 13, \
            f"输出数应为 12 IMAGE + 1 STRING: {len(split.RETURN_TYPES)}"
        assert len(split.RETURN_NAMES) == 13, \
            f"输出名数不匹配: {len(split.RETURN_NAMES)}"
        assert len(split.OUTPUT_IS_LIST) == 13, \
            f"OUTPUT_IS_LIST 长度不匹配: {len(split.OUTPUT_IS_LIST)}"
        assert split.RETURN_TYPES[-1] == "STRING", "最后一个输出应为 STRING 清单"
        assert split.RETURN_NAMES[-1] == "分流清单", "最后一个输出名应为 分流清单"
        assert all(t == "IMAGE" for t in split.RETURN_TYPES[:-1]), "前 12 路应为 IMAGE"
        # 分支数控件上限必须与 MAX_BRANCHES 一致
        spec = split.INPUT_TYPES()
        assert spec["required"]["分支数"][1]["max"] == 12, \
            "分支数控件上限应与 MAX_BRANCHES 一致"
        print("    输出契约稳定：12 路 IMAGE + 1 清单")

    check("并行分发器 (输出契约与控件上限一致)", test_splitter_output_contract)

    def test_splitter_varlist():
        """并行分发器：各分支的图保持各自原始尺寸。"""
        node = mapping["ZXIN_BatchImageSplitter"]()
        imgs = [torch.rand(1, 10 + i * 13, 20 + i * 7, 3) for i in range(6)]
        out = node.split_branches(imgs, 3)
        counts = [len(out[i]) for i in range(3)]
        assert counts == [2, 2, 2], f"三分支分配错误: {counts}"
        assert len(out[3]) == 0, "未使用的分支应为空"
        shapes = [tuple(t.shape[1:3]) for t in out[0]]
        assert len(set(shapes)) == 2, f"变尺寸信息丢失: {shapes}"
        print(f"    三分支变尺寸正常: {shapes}")

    check("并行分发器 (变尺寸图片)", test_splitter_varlist)

    def test_multi_round_execution():
        """核心验证：分发器输出 list 后，下游节点被自动执行与批次数相同的轮次。

        复刻 ComfyUI execution.py 的 merge_result_data + map_node_over_list。
        """
        upstream = mapping["ZXIN_BatchImageDistributor"]()
        imgs = [torch.rand(1, 16, 16, 3) for _ in range(20)]
        up_return = upstream.distribute(imgs, 4, False)

        # merge_result_data: OUTPUT_IS_LIST=True → 展平为元素列表
        output_is_list = list(upstream.OUTPUT_IS_LIST)
        merged = []
        for i, is_list in enumerate(output_is_list):
            if is_list:
                value = []
                for o in [up_return]:
                    value.extend(o[i])
                merged.append(value)
            else:
                merged.append([o[i] for o in [up_return]])
        assert len(merged[0]) == 5, f"展平后列表长度错误: {len(merged[0])}"

        # 非列表输出必须保持单值，不能被拆成列表
        assert merged[1] == [up_return[1]], "分批清单被错误拆分为列表"
        assert merged[2] == [up_return[2]], "批次数被错误拆分为列表"
        assert up_return[2] == 5 and isinstance(up_return[2], int), \
            f"批次数必须是单个整数: {up_return[2]}"

        # map_node_over_list: 下游按列表长度被调用多次
        calls = []

        class Downstream:
            def run(self, 图片列表):
                calls.append(len(图片列表))
                return (图片列表,)

        input_data_all = {"图片列表": merged[0]}
        max_len = max(len(v) for v in input_data_all.values())
        assert max_len == 5, f"下游输入列表长度错误: {max_len}"

        down = Downstream()
        for i in range(max_len):
            idx = i if len(input_data_all["图片列表"]) > i else -1
            down.run(图片列表=input_data_all["图片列表"][idx])

        assert len(calls) == 5, f"下游执行轮数错误: {len(calls)}"
        print(f"    一次运行 → 下游自动执行 {len(calls)} 轮，每轮 {calls[0]} 张")

    check("多轮自动执行 (按一次运行跑完5轮)", test_multi_round_execution)

    def test_varlist_end_to_end():
        """端到端：不同尺寸的图从加载器走到分发器，全程不报错。"""
        loader = mapping["ZXIN_BatchImageLoader"]()
        dist = mapping["ZXIN_BatchImageDistributor"]()
        out = loader.load_batch(
            加载来源="input/_zxin_test", 文件名匹配="*.png", 排序方式="按名称升序",
            随机种子=0, 最多加载张数=0, 载入设备="仅CPU",
            生成预览拼贴=False, 预览列数=4, 预览单格大小=128, 自定义路径="",
        )
        images = out[0]
        assert len(images) == 20, f"加载数量错误: {len(images)}"

        dist_out = dist.distribute(images, 4, False)
        batches = dist_out[0]
        assert len(batches) == 5, f"批次数错误: {len(batches)}"

        # 每批内的图尺寸仍然各自保留
        shapes = set()
        for batch in batches:
            for t in (batch if isinstance(batch, list) else [batch]):
                if t.ndim == 4 and t.shape[0] == 1:
                    shapes.add(tuple(t.shape[1:3]))
        assert len(shapes) > 1, f"变尺寸信息丢失: {shapes}"
        print(f"    加载 {len(images)} 张 → 分发 {len(batches)} 批，保留 {len(shapes)} 种原始尺寸")

    check("变尺寸端到端链路 (加载→分发)", test_varlist_end_to_end)

    def test_output_is_list_scope():
        """OUTPUT_IS_LIST 必须只作用于图像输出，清单/计数保持单值。"""
        dist = mapping["ZXIN_BatchImageDistributor"]
        assert dist.OUTPUT_IS_LIST == (True, False, False), \
            f"批次分发器 OUTPUT_IS_LIST 配置错误: {dist.OUTPUT_IS_LIST}"
        split = mapping["ZXIN_BatchImageSplitter"]
        assert split.OUTPUT_IS_LIST == tuple([True] * 12 + [False]), \
            f"并行分发器 OUTPUT_IS_LIST 配置错误: {split.OUTPUT_IS_LIST}"
        loader = mapping["ZXIN_BatchImageLoader"]
        assert loader.OUTPUT_IS_LIST == (True, False, False, False), \
            f"加载器 OUTPUT_IS_LIST 配置错误: {loader.OUTPUT_IS_LIST}"
        print("    列表标记仅作用于图像输出")

    check("列表标记作用域正确", test_output_is_list_scope)

    def test_preview_sheet():
        """加载器应输出文件夹缩略图拼贴，且尺寸符合网格排布。"""
        node = mapping["ZXIN_BatchImageLoader"]()
        out = node.load_batch(
            加载来源="input/_zxin_test", 文件名匹配="*.png", 排序方式="按名称升序",
            随机种子=0, 最多加载张数=0, 载入设备="仅CPU",
            生成预览拼贴=True, 预览列数=4, 预览单格大小=128, 自定义路径="",
        )
        images, preview = out[0], out[1]
        assert preview.ndim == 4, f"预览图维度错误: {preview.shape}"

        cols, cell, gap = 4, 128, 6
        rows = (20 + cols - 1) // cols
        exp_w = cols * cell + (cols + 1) * gap
        exp_h = rows * cell + (rows + 1) * gap
        assert preview.shape[1] == exp_h and preview.shape[2] == exp_w, \
            f"预览拼贴尺寸错误: {tuple(preview.shape)}，期望 ({exp_h}, {exp_w})"
        assert preview.shape[0] == 1, "预览图应为单张"
        # 拼贴内容不能是全黑（说明确实拼了图）
        assert float(preview.max()) > 0.0, "预览拼贴全黑，疑似未生成"
        print(f"    预览拼贴 {tuple(preview.shape)}（{cols}列 x {rows}行）")

    check("加载器输出文件夹预览拼贴", test_preview_sheet)

    def test_preview_disabled():
        """关闭预览时应返回占位图，不报错。"""
        node = mapping["ZXIN_BatchImageLoader"]()
        out = node.load_batch(
            加载来源="input/_zxin_test", 文件名匹配="*.png", 排序方式="按名称升序",
            随机种子=0, 最多加载张数=2, 载入设备="仅CPU",
            生成预览拼贴=False, 预览列数=4, 预览单格大小=128, 自定义路径="",
        )
        assert out[1].shape[0] == 1, "关闭预览时应返回占位图"
        print("    关闭预览时返回占位图")

    check("关闭预览拼贴不报错", test_preview_disabled)

    def test_saver():
        node = mapping["ZXIN_BatchImageSaver"]()
        imgs = [torch.rand(1, 48, 48, 3) for _ in range(3)]
        out = node.save_batch(
            图片列表=imgs, 文件名前缀="ZXinTest", 命名方式="按序号",
            文件名列表='["a.png", "b.jpg", "c.png"]', 写入元数据=True,
            prompt={"1": {"class_type": "Test"}}, extra_pnginfo={"workflow": {"nodes": []}},
        )
        files = json.loads(out[1])
        assert len(files) == 3, f"保存数量错误: {len(files)}"
        for rel in files:
            full = os.path.join(COMFY_ROOT, "output", rel)
            assert os.path.exists(full), f"文件未落盘: {full}"
        print(f"    已落盘 {len(files)} 个文件: {files[0]}")

    check("批量保存器 (按序号)", test_saver)

    def test_saver_varlist():
        """关键：不同尺寸的图也能逐张保存。"""
        node = mapping["ZXIN_BatchImageSaver"]()
        imgs = [torch.rand(1, 20 + i * 10, 30 + i * 10, 3) for i in range(4)]
        out = node.save_batch(
            图片列表=imgs, 文件名前缀="ZXinVar", 命名方式="按序号",
            文件名列表="", 写入元数据=False,
        )
        files = json.loads(out[1])
        assert len(files) == 4, f"变尺寸保存数量错误: {len(files)}"
        for rel in files:
            assert os.path.exists(os.path.join(COMFY_ROOT, "output", rel)), f"未落盘: {rel}"
        print(f"    变尺寸 {len(files)} 张全部落盘")

    check("批量保存器 (变尺寸图片)", test_saver_varlist)

    def test_saver_original_naming():
        node = mapping["ZXIN_BatchImageSaver"]()
        imgs = [torch.rand(1, 32, 32, 3) for _ in range(2)]
        out = node.save_batch(
            图片列表=imgs, 文件名前缀="ZXinOrig", 命名方式="沿用源文件名",
            文件名列表='["alpha.png", "beta.png"]', 写入元数据=False,
        )
        files = json.loads(out[1])
        assert "alpha.png" in files[0], f"未沿用源文件名: {files[0]}"
        assert "beta.png" in files[1], f"未沿用源文件名: {files[1]}"
        print(f"    沿用源文件名: {files}")

    check("批量保存器 (沿用源文件名)", test_saver_original_naming)

    def test_saver_no_overwrite():
        """同名文件必须自动改名，不能覆盖已有产物。"""
        node = mapping["ZXIN_BatchImageSaver"]()
        imgs = [torch.rand(1, 16, 16, 3)]
        out1 = node.save_batch(
            图片列表=imgs, 文件名前缀="ZXinDup", 命名方式="按序号",
            文件名列表="", 写入元数据=False,
        )
        out2 = node.save_batch(
            图片列表=imgs, 文件名前缀="ZXinDup", 命名方式="按序号",
            文件名列表="", 写入元数据=False,
        )
        f1 = json.loads(out1[1])[0]
        f2 = json.loads(out2[1])[0]
        assert f1 != f2, f"第二次保存覆盖了原文件: {f1} == {f2}"
        assert os.path.exists(os.path.join(COMFY_ROOT, "output", f1)), "原文件丢失"
        assert os.path.exists(os.path.join(COMFY_ROOT, "output", f2)), "新文件未生成"
        print(f"    防覆盖生效: {f1} → {f2}")

    check("批量保存器 (防覆盖)", test_saver_no_overwrite)

    def test_annotator():
        node = mapping["ZXIN_BatchImageAnnotator"]()
        imgs = torch.zeros(2, 64, 64, 3)
        out = node.annotate(
            图片列表=imgs, 标签内容="序号和文件名", 标签位置="左下角",
            字号=14, 文字颜色="#FFFFFF", 背景颜色="#000000",
            文件名列表='["测试A.png", "测试B.png"]',
        )
        res = out[0]
        assert isinstance(res, list), "标注输出必须是 list"
        assert len(res) == 2, f"标注数量错误: {len(res)}"
        for t in res:
            assert t.ndim == 4 and t.shape[0] == 1, f"标注后形状错误: {t.shape}"
        corner = res[0][0, 40:60, 0:50]
        assert float(corner.max()) > 0.0, "角标区域全黑，疑似未绘制"
        print(f"    输出 {len(res)} 张，角标像素最大值 {float(corner.max()):.1f}")

    check("批量角标标注器 (中文文件名)", test_annotator)

    def test_annotator_varlist():
        """变尺寸图片也能标注，且各自保持原尺寸。"""
        node = mapping["ZXIN_BatchImageAnnotator"]()
        imgs = [torch.zeros(1, 40 + i * 20, 60 + i * 20, 3) for i in range(3)]
        out = node.annotate(
            imgs, "序号和文件名", "左下角", 14, "#FFFFFF", "#000000",
            '["甲.png", "乙.png", "丙.png"]',
        )
        res = out[0]
        assert len(res) == 3, f"标注数量错误: {len(res)}"
        shapes = [tuple(t.shape[1:3]) for t in res]
        assert len(set(shapes)) == 3, f"标注后应保持各自原尺寸: {shapes}"
        print(f"    变尺寸标注完成，各图尺寸保持: {shapes}")

    check("批量角标标注器 (变尺寸图片)", test_annotator_varlist)

    def test_annotator_none():
        node = mapping["ZXIN_BatchImageAnnotator"]()
        imgs = [torch.rand(1, 32, 32, 3) for _ in range(2)]
        out = node.annotate(imgs, "不添加", "左上角", 14, "#FFF", "#000", "")
        assert len(out[0]) == 2, "不添加模式应原样返回"
        assert torch.equal(out[0][0], imgs[0]), "不添加模式不应修改图像"
        print("    不添加模式原样透传")

    check("批量角标标注器 (不添加透传)", test_annotator_none)

    # ---------------------------------------------------------- 5. 边界
    print("\n[5] 边界与健壮性")

    def test_empty_batch():
        node = mapping["ZXIN_BatchImageDistributor"]()
        out = node.distribute([], 4, False)
        assert len(out[0]) == 0, f"空列表应产出 0 批: {len(out[0])}"
        assert out[2] == 0, f"批次数应为 0: {out[2]}"
        print("    空列表安全（0 批，不报错）")

    check("空批次不崩溃", test_empty_batch)

    def test_bad_dimension():
        node = mapping["ZXIN_BatchImageDistributor"]()
        try:
            node.distribute(torch.rand(4, 4), 4, False)
        except ValueError:
            print("    非法维度被正确拦截")
            return
        raise AssertionError("非法维度应该抛 ValueError")

    check("非法输入维度被拦截", test_bad_dimension)

    def test_missing_dir():
        node = mapping["ZXIN_BatchImageLoader"]()
        try:
            node.load_batch(
                加载来源="input/_zxin_test", 文件名匹配="*.png", 排序方式="按名称升序",
                随机种子=0, 最多加载张数=0, 载入设备="仅CPU",
                生成预览拼贴=False, 预览列数=4, 预览单格大小=128, 自定义路径=os.path.join(COMFY_ROOT, "input", "__not_exist__"),
            )
        except ValueError:
            print("    不存在的目录被正确拦截")
            return
        raise AssertionError("不存在的目录应该抛 ValueError")

    check("不存在的目录被拦截", test_missing_dir)

    def test_single_batch_edge():
        """只有 1 张图时，列表模式应只产出 1 批。"""
        node = mapping["ZXIN_BatchImageDistributor"]()
        imgs = [torch.rand(1, 16, 16, 3)]
        out = node.distribute(imgs, 4, False)
        assert len(out[0]) == 1, f"单图应产出 1 批: {len(out[0])}"
        first = out[0][0]
        assert len(first) == 1, "单图批次应有 1 个元素"
        assert first[0].shape[0] == 1, "单图批次张数错误"
        print("    单张图 → 1 批")

    check("单张图边界", test_single_batch_edge)

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 68)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败明细：")
        for name, err in FAIL:
            print(f"  - {name}: {err}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
