/**
 * 诊断 v2：验证修正后的 beforeRegisterNodeDef 是否生效，
 * 以及 nodeCreated 兜底路径的可用性。
 */

const http = require("node:http");
const { spawn } = require("node:child_process");
const path = require("node:path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 8191;
const BASE = `http://127.0.0.1:${PORT}`;
const PROFILE = path.join(process.env.TEMP || ".", "zxin_edge_profile3");

function httpGet(url) {
    return new Promise((resolve, reject) => {
        http.get(url, (res) => {
            let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(d));
        }).on("error", reject);
    });
}
async function waitFor(fn, ms, label) {
    const s = Date.now();
    while (Date.now() - s < ms) { try { const v = await fn(); if (v) return v; } catch (e) {} await new Promise((r) => setTimeout(r, 1500)); }
    throw new Error("超时: " + label);
}

(async () => {
    await waitFor(async () => (await httpGet(BASE + "/object_info")).includes("ZXIN_"), 20000, "服务");

    const proc = spawn(EDGE, [
        "--headless=new", "--remote-debugging-port=9335",
        `--user-data-dir=${PROFILE}`, "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", BASE,
    ], { stdio: "ignore" });

    const wsUrl = await waitFor(async () => {
        const list = JSON.parse(await httpGet("http://127.0.0.1:9335/json/list"));
        const p = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
        return p ? p.webSocketDebuggerUrl : null;
    }, 30000, "CDP");

    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
        ws.addEventListener("open", res); ws.addEventListener("error", rej);
        setTimeout(() => rej(new Error("WS 超时")), 15000);
    });

    let msgId = 0; const pending = new Map();
    ws.addEventListener("message", (ev) => {
        const m = JSON.parse(ev.data.toString());
        if (m.id && pending.has(m.id)) {
            const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
            m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
        }
    });
    const send = (method, params) => new Promise((resolve, reject) => {
        const id = ++msgId; pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error("超时 " + method)); } }, 30000);
    });
    const evaluate = async (expr) => {
        const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
        if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "异常");
        return r.result.value;
    };

    await waitFor(async () => evaluate("!!(window.app && window.app.graph)"), 60000, "前端");
    await new Promise((r) => setTimeout(r, 8000));

    console.log("=== A. 手动注册扩展，验证 beforeRegisterNodeDef 回调参数 ===");
    const hookProbe = await evaluate(`(() => {
        const app = window.app;
        const probe = { called: 0, nodeDataNames: [], targetSeen: false };
        const ext = {
            name: "__zxin_probe__",
            async beforeRegisterNodeDef(nodeType, nodeData) {
                probe.called++;
                if (nodeData?.name) probe.nodeDataNames.push(nodeData.name);
                if (nodeData?.name === "ZXIN_BatchImageSplitter") probe.targetSeen = true;
            }
        };
        app.registerExtension(ext);
        // 手动触发一次注册流程不可行；改为直接检查 nodeDef 数据来源
        return { registered: true, note: "beforeRegisterNodeDef 只在注册时调用" };
    })()`);
    console.log("  ", JSON.stringify(hookProbe));

    console.log("\n=== B. nodeDef 数据从哪里取（前端 API） ===");
    const defSource = await evaluate(`(() => {
        const app = window.app;
        const out = {};
        // 尝试各种可能的路径
        const candidates = [
            ["app.extensionManager.nodeDefs", app?.extensionManager?.nodeDefs],
            ["app.extensionManager.nodeTypes", app?.extensionManager?.nodeTypes],
            ["app.nodeDefs", app?.nodeDefs],
        ];
        for (const [path, v] of candidates) {
            if (v && typeof v === "object") {
                const keys = Object.keys(v).filter(k => /ZXIN/i.test(k));
                out[path] = { total: Object.keys(v).length, zxin: keys };
                if (keys.length) {
                    const d = v[keys[0]];
                    out[path].sampleName = d?.name;
                    out[path].sampleOutputCount = (d?.output || []).length;
                }
            } else {
                out[path] = String(v);
            }
        }
        return out;
    })()`);
    console.log("  ", JSON.stringify(defSource, null, 2).slice(0, 1200));

    console.log("\n=== C. 手动调用我的 JS 里的逻辑（验证裁剪函数本身） ===");
    const manual = await evaluate(`(async () => {
        const res = await fetch("/extensions/ComfyUI_ZXin/dynamic_branch_outputs.js");
        const src = await res.text();
        const mod = await import("/extensions/ComfyUI_ZXin/dynamic_branch_outputs.js");
        return { imported: true, exports: Object.keys(mod || {}), srcLen: src.length };
    })()`).catch(e => ({ error: String(e).slice(0, 200) }));
    console.log("  ", JSON.stringify(manual));

    console.log("\n=== D. 关键：验证 app.registerExtension 的 nodeCreated 全局钩子 ===");
    const globalHook = await evaluate(`(() => {
        const app = window.app;
        let createdCount = 0;
        let seen = [];
        const ext = {
            name: "__zxin_created_probe__",
            async nodeCreated(node) {
                createdCount++;
                if (node?.comfyClass === "ZXIN_BatchImageSplitter" || node?.type === "ZXIN_BatchImageSplitter") {
                    seen.push({ comfyClass: node.comfyClass, type: node.type });
                }
            }
        };
        app.registerExtension(ext);
        // 手动创建一个节点实例
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageSplitter"];
        const n = new nt();
        return {
            createdCount,
            seen,
            instanceComfyClass: n.comfyClass,
            instanceType: n.type,
            constructorComfyClass: n.constructor?.ComfyClass,
            constructorNodeDataName: n.constructor?.nodeData?.name,
        };
    })()`);
    console.log("  ", JSON.stringify(globalHook, null, 2));

    ws.close(); proc.kill();
})().catch((e) => { console.error("失败:", e); process.exit(1); });
