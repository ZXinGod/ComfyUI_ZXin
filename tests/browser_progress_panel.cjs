/**
 * 真实浏览器端到端验证：批量图像加载器的「实时进度」面板
 *
 * 1. 扩展加载后节点上是否挂上进度面板（addDOMWidget）
 * 2. 后端真实推送 zxin.loader_progress 后面板是否更新数量与进度
 * 3. 面板 serialize:false → 不写进工作流，不污染 widgets_values
 *
 * 第 4 项走**真实链路**（不是 mock）：
 *   浏览器里建节点 → 拿到画布节点 id → 用该 id 作为 prompt 的 key 提交
 *   → 后端执行加载器并广播 zxin.loader_progress（node 字段 = 该 id）
 *   → 浏览器收到后更新面板。
 * 这样 node id 与 prompt key 一致，后端推送能被面板的 findNode 直接命中，
 * 走的是和生产环境完全相同的代码路径。
 */

const http = require("node:http");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 8188;
const BASE = `http://127.0.0.1:${PORT}`;
const PROFILE = path.join(process.env.TEMP || ".", "zxin_progress_" + Date.now());
const IMAGES = "E:\\AI\\ComfyUI_windows_portable-G313\\ComfyUI\\input\\角色卡";
// 注意：加载张数必须是「与历史运行不同」的值。
// IS_CHANGED 的指纹 = 文件列表签名，张数变了指纹才变，
// 否则节点会被 ComfyUI 缓存命中、load_batch 不重跑、后端不推送，
// 面板就会停在「等待运行」。
const EXPECT = 101;

function httpGet(url) {
    return new Promise((resolve, reject) => {
        http.get(url, (res) => { let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(d)); })
            .on("error", reject);
    });
}
function httpPost(url, body) {
    return new Promise((resolve, reject) => {
        const data = Buffer.from(JSON.stringify(body), "utf8");
        const req = http.request(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Content-Length": data.length },
        }, (res) => {
            let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve({ status: res.statusCode, body: d }));
        });
        req.on("error", reject);
        req.write(data); req.end();
    });
}
async function waitFor(fn, ms, label) {
    const s = Date.now();
    while (Date.now() - s < ms) {
        try { const v = await fn(); if (v) return v; } catch (e) { }
        await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error("超时: " + label);
}

(async () => {
    await waitFor(async () => (await httpGet(BASE + "/object_info")).includes("ZXIN_"), 20000, "服务");

    const proc = spawn(EDGE, [
        "--headless=new", "--remote-debugging-port=9345",
        `--user-data-dir=${PROFILE}`, "--no-first-run", "--no-default-browser-check",
        // 禁用 Edge 首次启动的各种向导/同步提示，否则会停在「同步浏览数据」页面
        "--disable-features=msEdgeSync,msEdgeFirstRunExperience,msEdgeAutoSignIn,msEdgeWelcomePage,msEdgeFirstRun",
        "--disable-sync", "--disable-background-networking", "--disable-popup-blocking",
        "--disable-gpu", BASE,
    ], { stdio: "ignore" });

    const wsUrl = await waitFor(async () => {
        const list = JSON.parse(await httpGet("http://127.0.0.1:9345/json/list"));
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
    const send = (method, params, timeoutMs = 30000) => new Promise((resolve, reject) => {
        const id = ++msgId; pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error("超时 " + method)); } }, timeoutMs);
    });
    const evaluate = async (expr, timeoutMs = 30000) => {
        const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }, timeoutMs);
        if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "异常");
        return r.result.value;
    };

    await waitFor(async () => evaluate("!!(window.app && window.app.graph)"), 60000, "前端")
        .catch(async (e) => {
            const diag = await evaluate(`(() => ({
                hasApp: !!window.app,
                hasGraph: !!(window.app && window.app.graph),
                title: document.title,
                bodyLen: document.body ? document.body.innerHTML.length : -1,
            }))()`).catch((x) => ({ evalFailed: String(x) }));
            console.error("前端未就绪，诊断:", JSON.stringify(diag));
            throw e;
        });
    await new Promise((r) => setTimeout(r, 6000));

    let pass = 0, fail = 0;
    const check = (name, cond, extra = "") => {
        if (cond) { pass++; console.log(`  [PASS] ${name}`); }
        else { fail++; console.log(`  [FAIL] ${name} ${extra}`); }
    };

    console.log("=== 浏览器端到端验证：实时进度面板 ===\n");

    // 1. 扩展是否已加载
    const ext = await evaluate(`(() => {
        const names = (window.app?.extensions || []).map(e => e.name || "");
        return { total: names.length, has: names.some(n => n.includes("LoaderProgress")) };
    })()`);
    check("进度扩展已注册", ext.has, `扩展总数 ${ext.total}`);

    // 2. 加载器节点是否挂上 DOM 面板
    const panel = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageLoader"];
        if (!nt) return { error: "节点类型未注册" };
        const n = new nt();
        return {
            hasPanel: !!n.__zxinProgressPanel,
            widgetCount: n.widgets ? n.widgets.length : 0,
            widgetNames: n.widgets ? n.widgets.map(w => w.name) : [],
            count: n.__zxinProgressPanel ? n.__zxinProgressPanel.count.textContent : null,
            progress: n.__zxinProgressPanel ? n.__zxinProgressPanel.progress.textContent : null,
        };
    })()`);
    check("加载器节点已挂进度面板", panel.hasPanel === true, JSON.stringify(panel));
    check("初始显示为「等待运行」", panel.progress === "等待运行", `实际: ${panel.progress}`);
    check("面板不占用原生控件位", panel.widgetCount > 0, JSON.stringify(panel.widgetNames));

    // 3. 面板 widget 是否标记为不序列化
    const ser = await evaluate(`(() => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageLoader"];
        const n = new nt();
        const w = (n.widgets || []).find(x => x.name === "zxin_loader_progress");
        if (!w) return { found: false, widgetNames: (n.widgets||[]).map(x=>x.name) };
        return { found: true, serialize: w.serialize, optionsSerialize: w.options && w.options.serialize };
    })()`);
    check("面板 widget 存在", ser.found === true, JSON.stringify(ser));
    check("面板不写入工作流（serialize:false）", ser.serialize === false, JSON.stringify(ser));

    // 4. 真实链路：浏览器建节点 → HTTP 提交 prompt → 后端广播 → 面板更新
    //    prompt 的 key 直接用画布节点 id，后端推送的 node 字段就能被 findNode 命中。
    console.log("\n  [链路] 提交真实 prompt，等待后端广播...\n");

    const nodeId = await evaluate(`(async () => {
        const nt = window.LiteGraph.registered_node_types["ZXIN_BatchImageLoader"];
        // 清场，只留我们建的节点，保证节点 id 可控
        for (const other of [...(window.app.graph._nodes || [])]) {
            window.app.graph.remove(other);
        }
        const n = new nt();
        n.title = "批量图像加载器";
        n.pos = [80, 80];
        n.size = [360, 720];
        window.app.graph.add(n);
        window.app.graph.setDirtyCanvas(true, true);
        await new Promise(r => setTimeout(r, 1000));
        return String(n.id);
    })()`);
    console.log(`  画布节点 id = ${nodeId}`);

    const prompt = {
        [nodeId]: {
            class_type: "ZXIN_BatchImageLoader",
            inputs: {
                加载来源: "input",
                文件名匹配: "*.png",
                排序方式: "按名称升序",
                随机种子: 0,
                最多加载张数: EXPECT,
                载入设备: "仅CPU",
                生成预览拼贴: false,
                预览列数: 4,
                预览单格大小: 192,
                含子文件夹: false,
                限制最大边长: 0,
                加载模式: "全部载入（不惰性）",
                单批内存上限: 0,
                预览最多显示张数: 0,
                自定义路径: IMAGES,
            },
        },
        "out": {
            class_type: "SaveText",
            inputs: { text: [nodeId, 4], filename_prefix: "zxin_e2e", format: "txt" },
        },
    };

    const submitted = await httpPost(BASE + "/prompt", { prompt, client_id: "zxin-e2e-" + Date.now() });
    if (submitted.status !== 200) {
        console.error("  prompt 提交失败:", submitted.status, submitted.body.slice(0, 500));
        check("prompt 提交成功", false, submitted.body.slice(0, 200));
    } else {
        console.log("  已提交:", JSON.parse(submitted.body).prompt_id);
        check("prompt 提交成功", true);
    }

    // 等待后端广播到达并驱动面板
    const upd = await evaluate(`(async () => {
        const n = window.app.graph._nodes.find(x => String(x.id) === "${nodeId}");
        if (!n) return { error: "画布上找不到节点" };
        const deadline = Date.now() + 180000;
        while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 500));
            const c = n.__zxinProgressPanel ? n.__zxinProgressPanel.count.textContent : "";
            if (c && c !== "—" && c !== "0") break;
        }
        await new Promise(r => setTimeout(r, 500));
        window.app.graph.setDirtyCanvas(true, true);
        await new Promise(r => setTimeout(r, 800));
        const p = n.__zxinProgressPanel;
        return {
            count: p ? p.count.textContent : null,
            progress: p ? p.progress.textContent : null,
            barWidth: p ? p.fill.style.width : null,
            done: p ? p.fill.classList.contains("zxin-done") : null,
            text: p ? p.text.textContent : null,
        };
    })()`, 200000);

    check("真实推送后面板更新数量", upd.count === `${EXPECT} / ${EXPECT}`, `实际: ${upd.count} ${upd.error || ""}`);
    check("真实推送后面板更新进度文案", new RegExp(`${EXPECT} 张`).test(upd.progress || ""), `实际: ${upd.progress}`);
    check("进度条填满 (100%)", upd.barWidth === "100%", `实际: ${upd.barWidth}`);
    check("完成态标记正确", upd.done === true, `实际: ${upd.done}`);
    check("详情文案含加载完成信息", /载入|完成|占用/.test(upd.text || ""), `实际: ${upd.text}`);

    console.log("\n  面板实际内容:", JSON.stringify(upd, null, 0));

    console.log(`\n通过 ${pass} 项，失败 ${fail} 项`);
    proc.kill();
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (e) { }
    process.exit(fail > 0 ? 1 : 0);
})().catch((e) => { console.error("验证失败:", e.message); process.exit(1); });
