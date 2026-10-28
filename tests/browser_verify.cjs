/**
 * 用真实浏览器验证前端动态端口是否生效。
 *
 * 流程：
 * 1. 启动 ComfyUI（外部已完成）
 * 2. 用 Edge 无头模式 + CDP 打开页面
 * 3. 在页面里检查：
 *    - 扩展是否被加载（app.extensions 中能找到）
 *    - beforeRegisterNodeDef 是否对目标节点生效
 *    - 实际创建的节点输出端口数
 */

const http = require("node:http");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 8191;
const BASE = `http://127.0.0.1:${PORT}`;
const PROFILE = path.join(process.env.TEMP || ".", "zxin_edge_profile");

function httpGet(url) {
    return new Promise((resolve, reject) => {
        http.get(url, (res) => {
            let d = "";
            res.on("data", (c) => (d += c));
            res.on("end", () => resolve(d));
        }).on("error", reject);
    });
}

async function waitFor(fn, timeoutMs, label) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        try {
            const v = await fn();
            if (v) return v;
        } catch (e) { /* retry */ }
        await new Promise((r) => setTimeout(r, 1500));
    }
    throw new Error("超时等待: " + label);
}

(async () => {
    // 1. 确认 ComfyUI 在线
    await waitFor(async () => {
        const r = await httpGet(BASE + "/object_info");
        return r.includes("ZXIN_BatchImageSplitter");
    }, 20000, "ComfyUI 服务");
    console.log("✓ ComfyUI 服务在线");

    // 2. 启动 Edge 无头，远程调试
    const proc = spawn(EDGE, [
        "--headless=new",
        "--remote-debugging-port=9333",
        `--user-data-dir=${PROFILE}`,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--window-size=1600,1000",
        BASE,
    ], { stdio: "ignore", detached: false });

    let targets = null;
    try {
        targets = await waitFor(async () => {
            const raw = await httpGet("http://127.0.0.1:9333/json/list");
            const list = JSON.parse(raw);
            const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
            return page ? page.webSocketDebuggerUrl : null;
        }, 30000, "Edge CDP");
        console.log("✓ Edge 无头浏览器已连接");
    } catch (e) {
        console.error("✗ 无法连接 Edge:", e.message);
        proc.kill();
        process.exit(1);
    }

    // 3. 通过 CDP 执行页面脚本
    let ws;
    try {
        ws = new WebSocket(targets);
    } catch (e) {
        console.error("✗ WebSocket 不可用:", e.message);
        proc.kill();
        process.exit(1);
    }

    await new Promise((res, rej) => {
        ws.addEventListener("open", res);
        ws.addEventListener("error", rej);
        setTimeout(() => rej(new Error("WS 连接超时")), 15000);
    });

    let msgId = 0;
    const pending = new Map();
    ws.addEventListener("message", (event) => {
        const m = JSON.parse(event.data.toString());
        if (m.id && pending.has(m.id)) {
            const { resolve, reject } = pending.get(m.id);
            pending.delete(m.id);
            if (m.error) reject(new Error(JSON.stringify(m.error)));
            else resolve(m.result);
        }
    });

    const send = (method, params) => new Promise((resolve, reject) => {
        const id = ++msgId;
        pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
        setTimeout(() => {
            if (pending.has(id)) {
                pending.delete(id);
                reject(new Error("CDP 超时: " + method));
            }
        }, 30000);
    });

    const evaluate = async (expr) => {
        const r = await send("Runtime.evaluate", {
            expression: expr,
            awaitPromise: true,
            returnByValue: true,
        });
        if (r.exceptionDetails) {
            throw new Error(r.exceptionDetails.exception?.description || "页面脚本异常");
        }
        return r.result.value;
    };

    // 4. 等待前端加载完成
    await waitFor(async () => {
        return await evaluate("!!(window.app && window.app.graph && window.app.canvas)");
    }, 60000, "前端 app 就绪");
    console.log("✓ 前端 app 就绪");
    await new Promise((r) => setTimeout(r, 6000));

    // 5. 检查扩展加载
    console.log("\n=== 扩展加载检查 ===");
    const extInfo = await evaluate(`(() => {
        const names = [];
        try {
            const exts = window.app?.extensionManager?.extensions || [];
            exts.forEach(e => names.push(e.name));
        } catch (e) {}
        return { count: names.length, hasMine: names.includes("ComfyUI_ZXin.DynamicBranchOutputs"), names: names.filter(n => /ZXin/i.test(n)) };
    })()`);
    console.log("  扩展总数:", extInfo.count);
    console.log("  我的扩展已注册:", extInfo.hasMine);
    console.log("  匹配 ZXin 的扩展:", extInfo.names);

    // 6. 检查节点定义里的 ComfyClass / nodeData.name
    console.log("\n=== 节点定义检查 ===");
    const defInfo = await evaluate(`(() => {
        const nm = window.app?.extensionManager?.nodeDefs || window.app?.nodeDefs;
        if (!nm) return { error: "找不到 nodeDefs" };
        const d = nm["ZXIN_BatchImageSplitter"];
        if (!d) return { error: "找不到 ZXIN_BatchImageSplitter 定义", keys: Object.keys(nm).filter(k => /ZXIN/i.test(k)) };
        const nt = window.LiteGraph?.registered_node_types?.["ZXIN_BatchImageSplitter"];
        return {
            nodeDataName: d.name,
            nodeDataOutputCount: (d.output || []).length,
            nodeDataOutputName: (d.output_name || []),
            hasPrototype: !!nt,
            protoComfyClass: nt?.ComfyClass,
        };
    })()`);
    console.log("  ", JSON.stringify(defInfo, null, 2).slice(0, 900));

    // 7. 实际创建节点，看输出端口数
    console.log("\n=== 实际创建节点测试 ===");
    const createResult = await evaluate(`(async () => {
        const app = window.app;
        const node = await app.graph.add? undefined : undefined;
        return { hasGraph: !!app.graph };
    })()`).catch(() => ({}));

    const nodeTest = await evaluate(`(() => {
        try {
            const app = window.app;
            const nt = window.LiteGraph?.registered_node_types?.["ZXIN_BatchImageSplitter"];
            if (!nt) return { error: "节点类型未注册到 LiteGraph" };
            const n = new nt();
            // 设置控件值
            const w = n.widgets?.find(x => x.name === "分支数");
            if (w) w.value = 4;
            if (w?.callback) w.callback(4);
            return {
                outputCount: n.outputs?.length,
                outputNames: n.outputs?.map(o => o.name),
                widgetFound: !!w,
            };
        } catch (e) {
            return { error: String(e) };
        }
    })()`);
    console.log("  ", JSON.stringify(nodeTest, null, 2).slice(0, 900));

    ws.close();
    proc.kill();
})().catch((e) => {
    console.error("失败:", e);
    process.exit(1);
});
