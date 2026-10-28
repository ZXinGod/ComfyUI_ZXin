/**
 * ComfyUI_ZXin 前端扩展的逻辑单元测试。
 *
 * 用一个最小的 LiteGraph 节点桩来验证：
 * - 分支数变化时输出槽数量正确增减
 * - 「分流清单」始终保留在最后
 * - 移除槽位前先断开连线，避免悬空链接
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const jsPath = path.join(__dirname, "..", "js", "dynamic_branch_outputs.js");
const src = readFileSync(jsPath, "utf-8");

// ---- 构造最小环境 ----
let registered = null;
const app = { registerExtension: (ext) => (registered = ext) };

// 模拟 app.js 的 import：把 import 语句替换掉
const body = src.replace(/import\s*\{[^}]*\}\s*from\s*["'][^"']+["'];?/g, "")
                 .replace(/export\s+/g, "");

// 用 Function 构造作用域并注入 app
const factory = new Function("app", body + "\nreturn app;");
factory(app);

if (!registered) {
    console.error("✗ 扩展未注册");
    process.exit(1);
}
console.log("✓ 扩展注册成功:", registered.name);

// ---- 构造节点桩 ----
function makeNode(outputCount) {
    const node = {
        comfyClass: "ZXIN_BatchImageSplitter",
        widgets: [{ name: "分支数", value: 4, callback: null }],
        outputs: [],
        dirty: 0,
        disconnected: [],
        removed: [],
        added: [],
    };
    for (let i = 0; i < outputCount; i++) {
        node.outputs.push({ name: `分支${i + 1}`, type: "IMAGE", linkIds: [i + 1] });
    }
    node.outputs.push({ name: "分流清单", type: "STRING", linkIds: [999] });

    node.disconnectOutput = function (i) {
        this.disconnected.push(i);
    };
    node.removeOutput = function (i) {
        this.removed.push(i);
        this.outputs.splice(i, 1);
    };
    node.addOutput = function (name, type) {
        this.added.push(name);
        this.outputs.push({ name, type, linkIds: [] });
    };
    node.setDirtyCanvas = function () {
        this.dirty++;
    };
    return node;
}

// ---- 模拟 beforeRegisterNodeDef / onNodeCreated ----
const nodeType = {
    ComfyClass: undefined,
    prototype: { onNodeCreated: function () {} },
};
await registered.beforeRegisterNodeDef(nodeType, { name: "ZXIN_BatchImageSplitter" });

const node = makeNode(12);
// 走 nodeCreated 全局钩子（真实前端主路径）
await registered.nodeCreated(node);

// 触发 widget 回调
const widget = node.widgets.find((w) => w.name === "分支数");
const setCount = (n) => {
    widget.value = n;
    widget.callback?.(n);
};

// ---- 测试 ----
let pass = 0, fail = 0;
function check(name, cond, extra = "") {
    if (cond) { pass++; console.log(`  [PASS] ${name}`); }
    else { fail++; console.log(`  [FAIL] ${name} ${extra}`); }
}

console.log("\n[测试]");

check("创建后立即裁剪到 4 个分支", node.outputs.length === 5, `实际 ${node.outputs.length}`);
check("分流清单保留在最后", node.outputs[node.outputs.length - 1].name === "分流清单");
check("前 4 路名为 分支1..4",
    node.outputs.slice(0, 4).every((o, i) => o.name === `分支${i + 1}`));

// 裁剪时先断开连线
check("裁剪时断开了多余槽位的连线", node.disconnected.length >= 8, `断开 ${node.disconnected.length}`);

// 增加到 6
setCount(6);
check("分支数→6 后输出为 6+1", node.outputs.length === 7, `实际 ${node.outputs.length}`);
check("新增了 分支5/分支6",
    node.outputs[4].name === "分支5" && node.outputs[5].name === "分支6");
check("分流清单仍在最后", node.outputs[6].name === "分流清单");

// 减少到 2
setCount(2);
check("分支数→2 后输出为 2+1", node.outputs.length === 3, `实际 ${node.outputs.length}`);
check("剩余分支名为 分支1/分支2",
    node.outputs[0].name === "分支1" && node.outputs[1].name === "分支2");
check("分流清单仍在最后", node.outputs[2].name === "分流清单");

// 减少到 1
setCount(1);
check("分支数→1 后输出为 1+1", node.outputs.length === 2, `实际 ${node.outputs.length}`);

// 回到 12（上限）
setCount(12);
check("分支数→12 后输出为 12+1", node.outputs.length === 13, `实际 ${node.outputs.length}`);
check("分流清单在第 13 位", node.outputs[12].name === "分流清单");

// 边界：0 与超范围
setCount(0);
check("分支数=0 时钳制为 1", node.outputs.length === 2, `实际 ${node.outputs.length}`);
setCount(99);
check("分支数=99 时钳制为 12", node.outputs.length === 13, `实际 ${node.outputs.length}`);

// 非目标节点不应被处理
let otherTouched = false;
const otherType = {
    ComfyClass: "SomeOtherNode",
    prototype: { onNodeCreated: function () {} },
};
await registered.beforeRegisterNodeDef(otherType, { name: "SomeOtherNode" });
const otherNode = makeNode(4);
otherType.prototype.onNodeCreated.call(otherNode);
check("非目标节点不被裁剪", otherNode.outputs.length === 5);

// 关键回归：nodeData.name 匹配（官方前端实际使用的判断方式）
const nameType = {
    ComfyClass: undefined,   // 模拟前端未提供 ComfyClass 的情况
    prototype: { onNodeCreated: function () {} },
};
await registered.beforeRegisterNodeDef(nameType, { name: "ZXIN_BatchImageSplitter" });
const nameNode = makeNode(12);
nameNode.comfyClass = "ZXIN_BatchImageSplitter";
nameType.prototype.onNodeCreated.call(nameNode);
// nodeCreated 也会触发（幂等）
check("nodeData.name 匹配时可生效（无 ComfyClass）", nameNode.outputs.length === 5,
    `实际 ${nameNode.outputs.length}`);

// 关键回归：仅靠 node.type 也能识别
const typeType = {
    prototype: { onNodeCreated: function () {} },
};
await registered.beforeRegisterNodeDef(typeType, { name: "SomethingElse" });
const typeNode = makeNode(12);
typeNode.comfyClass = undefined;
typeNode.type = "ZXIN_BatchImageSplitter";
await registered.nodeCreated(typeNode);
check("仅靠 node.type 也能识别", typeNode.outputs.length === 5,
    `实际 ${typeNode.outputs.length}`);

// 关键回归：nodeCreated 全局钩子路径（真实前端的主路径）
const createdType = { prototype: {} };
await registered.beforeRegisterNodeDef(createdType, { name: "Whatever" });
const createdNode = makeNode(12);
createdNode.comfyClass = "ZXIN_BatchImageSplitter";
await registered.nodeCreated(createdNode);
check("nodeCreated 钩子路径可生效", createdNode.outputs.length === 5,
    `实际 ${createdNode.outputs.length}`);

// 关键回归：nodeCreated 对非目标节点不处理
const otherCreated = makeNode(4);
otherCreated.comfyClass = "SomeOtherNode";
await registered.nodeCreated(otherCreated);
check("nodeCreated 对非目标节点不处理", otherCreated.outputs.length === 5);

// 关键回归：setupNode 幂等（重复调用不会重复裁剪）
const idemNode = makeNode(12);
idemNode.comfyClass = "ZXIN_BatchImageSplitter";
await registered.nodeCreated(idemNode);
const after1 = idemNode.outputs.length;
await registered.nodeCreated(idemNode);
check("重复安装不会破坏端口", idemNode.outputs.length === after1,
    `${after1} → ${idemNode.outputs.length}`);

console.log(`\n通过 ${pass} 项，失败 ${fail} 项`);
process.exit(fail ? 1 : 0);
