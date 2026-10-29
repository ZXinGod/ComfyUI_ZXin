/**
 * ComfyUI_ZXin 前端扩展
 *
 * 让「并行分发器」的输出端口数量随「分支数」控件动态增减。
 *
 * 实现要点：
 * - 后端固定注册 MAX_BRANCHES 个输出槽，保证执行契约稳定
 * - 前端在节点创建时按「分支数」裁剪输出槽数量，
 *   并监听控件变化时重新裁剪
 * - 裁剪时保留「分流清单」（最后一个 STRING 输出）始终可见
 * - 被裁剪的槽位先断开连线再移除，避免留下悬空链接
 *
 * 节点识别方式（实测 ComfyUI 0.42 前端）：
 * - nodeType.ComfyClass 在该版本【不存在】，不能作为判断依据
 * - node.comfyClass 与 node.constructor.nodeData.name 均可用
 * - 因此以 nodeCreated 全局钩子为主路径（对每个实例触发），
 *   beforeRegisterNodeDef 作为补充（此时 nodeData.name 可用）
 *
 * 参考的前端 API（nodeDefStore）：
 * - node.addOutput(name, type)
 * - node.removeOutput(index)
 * - node.disconnectOutput(index)
 */

import { app } from "../../scripts/app.js";

const TARGET_CLASS = "ZXIN_BatchImageSplitter";
const BRANCH_COUNT_WIDGET = "分支数";
const MANIFEST_NAME = "分流清单";
const MAX_BRANCHES = 12;

/** 读取节点上数值型 widget 的当前值 */
function getWidgetValue(node, widgetName) {
    const widget = node.widgets?.find((w) => w.name === widgetName);
    if (!widget) return null;
    const v = Number(widget.value);
    return Number.isFinite(v) ? v : null;
}

/** 判断节点是否为目标节点 */
function isTargetNode(node) {
    if (!node) return false;
    const cls = node.comfyClass ||
                node.constructor?.ComfyClass ||
                node.constructor?.nodeData?.name ||
                node.type;
    return cls === TARGET_CLASS;
}

/** 遍历 graph.links，找出从本节点某个输出槽出发的所有连线目标 */
function collectLinkTargets(node, slot) {
    const graph = node?.graph;
    if (!graph || !graph.links) return [];
    const targets = [];
    const push = (link) => {
        if (link && link.origin_id === node.id && link.origin_slot === slot) {
            targets.push({ nodeId: link.target_id, slot: link.target_slot });
        }
    };
    try {
        if (typeof graph.links.forEach === "function" && !Array.isArray(graph.links)) {
            // Map 形态：values() 才是连线对象
            for (const link of graph.links.values()) push(link);
        } else if (Array.isArray(graph.links)) {
            for (const link of graph.links) push(link);
        } else {
            for (const key of Object.keys(graph.links)) push(graph.links[key]);
        }
    } catch (e) { /* 忽略遍历失败 */ }
    return targets;
}

/**
 * 把输出槽数量调整到 branchCount（不含「分流清单」）。
 *
 * 关键点：0.42 前端已废弃「直接给 output.links 赋值」的写法
 * （会走 deprecated 的 legacy 通道，连线不一定能恢复），
 * 因此增删槽位后用 node.connect() 重新连线。
 */
function applyBranchCount(node, branchCount) {
    if (!node || !Array.isArray(node.outputs)) return;

    const want = Math.max(1, Math.min(Math.floor(branchCount), MAX_BRANCHES));

    // 定位「分流清单」：它必须始终保持在最后一个位置
    let manifestIndex = node.outputs.findIndex((o) => o.name === MANIFEST_NAME);
    if (manifestIndex < 0) {
        // 找不到清单（理论上不会发生），退化为按最后一个槽处理
        manifestIndex = node.outputs.length - 1;
    }

    // 1) 多余的槽位：先显式断开连线再移除
    //    removeOutput 在多数 LiteGraph 版本里会自动断线，并把后续槽位
    //    （含清单）连线的 origin_slot 前移；这里仍显式断一次，
    //    对不自动断线的实现同样安全（断开是幂等的）。
    for (let i = manifestIndex - 1; i >= want; i--) {
        try {
            node.disconnectOutput?.(i);
        } catch (e) {
            /* 忽略断开失败 */
        }
        try {
            node.removeOutput(i);
        } catch (e) {
            /* 忽略移除失败 */
        }
        manifestIndex = node.outputs.findIndex((o) => o.name === MANIFEST_NAME);
        if (manifestIndex < 0) manifestIndex = node.outputs.length - 1;
    }

    // 2) 不足的槽位：补齐到 want 个
    //    addOutput 只能追加到末尾，所以先把「分流清单」临时摘下，
    //    补完分支后再挂回去，并用 connect 恢复它原有的连线
    const manifestSlot = node.outputs.find((o) => o.name === MANIFEST_NAME);
    const currentBranchCount = node.outputs.length - (manifestSlot ? 1 : 0);

    if (currentBranchCount < want) {
        const savedTargets = manifestSlot
            ? collectLinkTargets(node, node.outputs.indexOf(manifestSlot))
            : [];

        if (manifestSlot) {
            try {
                node.removeOutput(node.outputs.indexOf(manifestSlot));
            } catch (e) {
                /* 忽略移除失败 */
            }
        }
        for (let i = currentBranchCount; i < want; i++) {
            try {
                node.addOutput(`分支${i + 1}`, "IMAGE");
            } catch (e) {
                /* 忽略添加失败 */
            }
        }
        if (manifestSlot) {
            try {
                node.addOutput(MANIFEST_NAME, "STRING");
            } catch (e) {
                /* 忽略添加失败 */
            }
            const newIndex = node.outputs.findIndex((o) => o.name === MANIFEST_NAME);
            if (newIndex >= 0 && savedTargets.length) {
                for (const t of savedTargets) {
                    try {
                        const target = node.graph?.getNodeById?.(t.nodeId);
                        if (target) node.connect(newIndex, target, t.slot);
                    } catch (e) {
                        /* 忽略重连失败 */
                    }
                }
            }
        }
    }

    // 3) 修正名称与顺序：分支按序排列，「分流清单」永远在最后
    node.outputs.forEach((out, idx) => {
        if (out.name !== MANIFEST_NAME) {
            out.name = `分支${idx + 1}`;
            out.label = undefined;
        }
    });

    node.setDirtyCanvas?.(true, true);
}

/** 给一个目标节点安装动态端口逻辑（幂等） */
function setupNode(node) {
    if (!node || node.__zxinDynamicPorts) return;
    node.__zxinDynamicPorts = true;

    const apply = () => {
        const count = getWidgetValue(node, BRANCH_COUNT_WIDGET);
        if (count !== null) {
            applyBranchCount(node, count);
        }
    };

    // 节点创建后立即裁剪
    apply();

    // 监听「分支数」控件变化
    const widget = node.widgets?.find((w) => w.name === BRANCH_COUNT_WIDGET);
    if (widget) {
        const originalCallback = widget.callback;
        widget.callback = function (value) {
            const r = originalCallback?.apply(this, arguments);
            const n = Number(value);
            if (Number.isFinite(n)) {
                applyBranchCount(node, n);
            }
            return r;
        };
    }

    // 工作流载入 / 图重配置后再次同步
    const prevAfterConfigure = node.onAfterConfigureGraph;
    node.onAfterConfigureGraph = function () {
        const r = prevAfterConfigure?.apply(this, arguments);
        apply();
        return r;
    };
}

app.registerExtension({
    name: "ComfyUI_ZXin.DynamicBranchOutputs",

    // 主路径：每个节点实例创建时触发，node.comfyClass 在该前端版本可用
    async nodeCreated(node) {
        if (!isTargetNode(node)) return;
        setupNode(node);
    },

    // 补充路径：节点定义注册时按 nodeData.name 判断并预装原型钩子
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== TARGET_CLASS && nodeType?.ComfyClass !== TARGET_CLASS) {
            return;
        }

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            if (isTargetNode(this)) {
                setupNode(this);
            }
            return result;
        };
    },
});
