/**
 * ComfyUI_ZXin —— 批量图像加载器的「实时进度」显示
 *
 * 后端加载器在解码过程中通过 WebSocket 推 zxin.loader_progress 事件，
 * 本扩展在节点上挂一个只读面板，实时显示：
 *   加载数量：已加载张数 / 总张数
 *   加载进度：进度文案（惰性分批时含批次信息）
 *
 * 设计要点：
 * - 用 addDOMWidget 挂在节点内，serialize:false → 不写进工作流，
 *   不会污染 widgets_values，也不会让旧工作流错位。
 * - 事件按 unique_id 定位节点；节点被删除后再收到事件直接忽略。
 * - 前端扩展缺失或 API 变化时静默降级，不影响节点本身的执行。
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EVENT = "zxin.loader_progress";
const TARGET_CLASS = "ZXIN_BatchImageLoader";

const STYLE_ID = "zxin-loader-progress-style";
const THEME = {
    bg: "rgba(28, 30, 34, 0.92)",
    border: "rgba(255, 255, 255, 0.14)",
    text: "#e6e6e6",
    muted: "#9aa0a6",
    accent: "#5ec8f8",
    ok: "#7ddc7d",
};

function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
.zxin-progress-panel {
    box-sizing: border-box;
    width: 100%;
    padding: 6px 8px;
    border-radius: 6px;
    background: ${THEME.bg};
    border: 1px solid ${THEME.border};
    color: ${THEME.text};
    font-size: 12px;
    line-height: 1.5;
    font-family: inherit;
    word-break: break-all;
}
.zxin-progress-row {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    align-items: baseline;
}
.zxin-progress-label { color: ${THEME.muted}; flex: 0 0 auto; }
.zxin-progress-value { color: ${THEME.text}; text-align: right; }
.zxin-progress-value.zxin-done { color: ${THEME.ok}; }
.zxin-progress-text {
    margin-top: 4px;
    padding-top: 4px;
    border-top: 1px solid ${THEME.border};
    color: ${THEME.muted};
    font-size: 11px;
    white-space: pre-wrap;
}
.zxin-progress-text.zxin-done { color: ${THEME.ok}; }
.zxin-progress-bar {
    margin-top: 5px;
    height: 3px;
    border-radius: 2px;
    background: rgba(255, 255, 255, 0.12);
    overflow: hidden;
}
.zxin-progress-bar > i {
    display: block;
    height: 100%;
    width: 0;
    border-radius: 2px;
    background: ${THEME.accent};
    transition: width 0.2s ease;
}
.zxin-progress-bar > i.zxin-done { background: ${THEME.ok}; }
`;
    document.head.appendChild(style);
}

function buildPanel() {
    const panel = document.createElement("div");
    panel.className = "zxin-progress-panel";

    const row1 = document.createElement("div");
    row1.className = "zxin-progress-row";
    const label1 = document.createElement("span");
    label1.className = "zxin-progress-label";
    label1.textContent = "加载数量";
    const value1 = document.createElement("span");
    value1.className = "zxin-progress-value";
    value1.textContent = "—";
    row1.append(label1, value1);

    const row2 = document.createElement("div");
    row2.className = "zxin-progress-row";
    const label2 = document.createElement("span");
    label2.className = "zxin-progress-label";
    label2.textContent = "加载进度";
    const value2 = document.createElement("span");
    value2.className = "zxin-progress-value";
    value2.textContent = "等待运行";
    row2.append(label2, value2);

    const bar = document.createElement("div");
    bar.className = "zxin-progress-bar";
    const fill = document.createElement("i");
    bar.appendChild(fill);

    const text = document.createElement("div");
    text.className = "zxin-progress-text";
    text.textContent = "";

    panel.append(row1, row2, bar, text);

    return {
        panel,
        count: value1,
        progress: value2,
        fill,
        text,
        set(count, total, progress) {
            const hasTotal = Number(total) > 0;
            const n = Number(count) || 0;
            this.count.textContent = hasTotal ? `${n} / ${total}` : String(n);
            this.progress.textContent = progress || (hasTotal ? `${n} / ${total}` : "—");
            const done = hasTotal ? n >= total : false;
            this.count.classList.toggle("zxin-done", done);
            this.progress.classList.toggle("zxin-done", done);
            this.fill.classList.toggle("zxin-done", done);
            const pct = hasTotal ? Math.min(100, (n / total) * 100) : 0;
            this.fill.style.width = `${pct}%`;
            this.text.textContent = progress || "";
            this.text.classList.toggle("zxin-done", done);
        },
    };
}

/** 节点 id → 面板。节点删除时清理，避免 Map 无限增长。 */
const panels = new Map();

/**
 * 当前正在执行的加载器节点 id。
 *
 * 后端推的 zxin.loader_progress 带的是 unique_id（prompt 里的字符串 key），
 * 而画布节点的 id 是数字，两边对不上。所以这里监听 ComfyUI 原生的
 * `executing` 事件 —— 它派发的是 display_node（正是画布节点 id），
 * 配合后端同时推送的 prompt_id 做二次校验，避免张冠李戴。
 */
let executingNodeId = null;
let executingPromptId = null;

function findNode(id) {
    if (id === null || id === undefined || id === "") return null;
    const numeric = Number(id);
    if (Number.isFinite(numeric)) {
        const byId = app.graph?._nodes_by_id?.[numeric];
        if (byId) return byId;
        const byGetter = app.graph?.getNodeById?.(numeric);
        if (byGetter) return byGetter;
    }
    // 兜底：遍历节点按 id / pk 对比（不同版本字段名有差异）
    const nodes = app.graph?._nodes || [];
    for (const node of nodes) {
        if (node && (String(node.id) === String(id) || String(node.pk) === String(id))) {
            return node;
        }
    }
    return null;
}

/**
 * 定位这次进度应该写到哪个节点。
 * 优先用后端给的 id；取不到时退回到「当前正在执行的加载器节点」。
 */
function resolveTarget(d) {
    const byId = findNode(d.node);
    if (byId) return byId;
    const byExec = findNode(executingNodeId);
    if (byExec) return byExec;
    return null;
}

function attach(node) {
    if (!node || node.__zxinProgressSetup) return;
    if (typeof node.addDOMWidget !== "function") return;
    node.__zxinProgressSetup = true;

    ensureStyle();

    let built;
    try {
        built = buildPanel();
    } catch (e) {
        console.warn("[ZXin] 进度面板构建失败：", e);
        node.__zxinProgressSetup = false;
        return;
    }

    try {
        const widget = node.addDOMWidget("zxin_loader_progress", "zxin_loader_progress", built.panel, {
            serialize: false,
            getMinHeight: () => 74,
        });
        widget.serialize = false;
        node.__zxinProgressPanel = built;
        node.__zxinProgressWidget = widget;
        panels.set(String(node.id), built);
        built.set(0, 0, "等待运行");
    } catch (e) {
        console.warn("[ZXin] 进度面板挂载失败：", e);
        node.__zxinProgressSetup = false;
    }
}

api.addEventListener(EVENT, (event) => {
    const d = (event && event.detail) || {};
    const node = resolveTarget(d);
    if (!node) return;
    const built = node.__zxinProgressPanel || panels.get(String(node.id));
    if (!built) return;
    try {
        built.set(d.count, d.total, d.progress);
    } catch (e) {
        /* 显示失败不影响主流程 */
    }
});

/**
 * 跟踪「当前正在执行的节点」。
 * ComfyUI 的 executing 事件派发的是 display_node（画布节点 id），
 * 正好用来把后端推送的进度落到正确的节点上。
 */
api.addEventListener("executing", (event) => {
    const id = (event && event.detail) !== undefined ? event.detail : event;
    executingNodeId = (id === null || id === undefined) ? null : id;
    // 节点执行结束（id 为 null）时清空，避免下次误用
    if (executingNodeId === null) {
        executingNodeId = null;
    }
});

app.registerExtension({
    name: "ComfyUI_ZXin.LoaderProgress",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== TARGET_CLASS) return;
        if (nodeType.prototype.__zxinProgressInstalled) return;
        nodeType.prototype.__zxinProgressInstalled = true;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            try {
                attach(this);
            } catch (e) {
                console.warn("[ZXin] 进度面板初始化失败：", e);
            }
            return r;
        };
    },

    async nodeCreated(node) {
        if (node?.comfyClass !== TARGET_CLASS && node?.constructor?.nodeData?.name !== TARGET_CLASS) {
            return;
        }
        attach(node);
    },
});
