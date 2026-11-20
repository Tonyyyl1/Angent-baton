import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _find_node():
    found = shutil.which("node")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _parse_frontmatter(text):
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta


PHASE_D_HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const fx = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {};
function makeEl(id, tag="DIV") {
  const el = {
    id, tagName: tag, hidden: false, open: false, value: "", textContent: "", _html: "",
    className: "", dataset: {}, disabled: false, offsetTop: 100, isConnected: true, elements: {},
    set innerHTML(v) { this._html = String(v); }, get innerHTML() { return this._html; },
    querySelector() { return null; }, addEventListener(ev, fn) { el["_on_" + ev] = fn; },
    showModal() { this.open = true; }, close() { this.open = false; }, setAttribute() {}, focus() {},
    scrollIntoView() {}, contains() { return false; }
  };
  els[id] = el;
  return el;
}
["hereSel","syncBadge","notice","track","tabs","filters","f-device","f-count","f-q","f-tool","f-status","f-dirty","view","dlg","txDlg","toast","io","cloud-remote","cloud-root","tx-q"].forEach(id => makeEl(id));
const listeners = {};
global.window = { claude: null, scrollTo() {} };
global.location = { protocol: "http:", hostname: "127.0.0.1" };
global.navigator = { userAgent: "Macintosh", clipboard: { writeText: () => Promise.resolve() } };
global.localStorage = { _m: {}, getItem(k){ return this._m[k] || null; }, setItem(k,v){ this._m[k]=String(v); } };
global.AbortController = class { constructor(){ this.signal = {}; } abort(){} };
const origSetTimeout = global.setTimeout;
global.setInterval = () => 1; global.clearInterval = () => {};
global.setTimeout = () => 1; global.clearTimeout = () => {};
global.document = {
  hidden: false, activeElement: null,
  getElementById(id) { return els[id] || null; },
  querySelector(s) { return s.startsWith("#") ? (els[s.slice(1)] || makeEl(s.slice(1))) : null; },
  addEventListener(ev, fn) { listeners[ev] = fn; }
};
const calls = [];
let sends = 0;
global.fetch = async (url, opt) => {
  const body = opt.body ? JSON.parse(opt.body) : null;
  calls.push({ method: opt.method, url, body });
  const reply = (status, d) => ({ ok: status < 300, status, json: async () => d });
  if (url.startsWith("/api/ledger")) return reply(200, fx.ledger);
  if (url === "/api/cloud/status") return reply(200, fx.status);
  if (url === "/api/cloud/inbox") return reply(200, { incoming: [], syncedReceipts: [] });
  if (url === "/api/cloud/doctor") return reply(200, fx.doctor);
  if (url === "/api/cloud/cache/prune") return reply(200, fx.prune);
  if (url === "/api/cloud/send") { sends++; return body.reissue ? reply(200, fx.sendOk) : reply(409, fx.sendError); }
  throw new Error("Unexpected url: " + url);
};
eval(script);
(async () => {
  const tick = () => new Promise(r => origSetTimeout(r, 30));
  const out = {};
  await tick();
  const click = (act, dataset={}) => listeners.click({ target: { closest: () => ({ dataset: Object.assign({ act }, dataset), disabled: false }) } });
  click("tab", { v: "inbox" });
  await tick(); await tick();
  out.inbox = els.view.innerHTML;
  click("cloudCachePreview");
  await tick(); await tick();
  out.preview = els.view.innerHTML;
  click("relay", { id: fx.project });
  await tick(); await tick();
  click("cloudSend", { id: fx.project, to: fx.to });
  await tick(); await tick();
  out.sendError = els.view.innerHTML;
  click("cloudReissue", { id: fx.project, to: fx.to });
  await tick(); await tick();
  out.calls = calls;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def render_phase_d(node_bin, html_path, fixture):
    """Drive relay-desk.html in node with canned API replies; returns the rendered views and the calls made."""
    with tempfile.TemporaryDirectory() as td:
        runner = os.path.join(td, "run_ui_phase_d.js")
        fx = os.path.join(td, "fixture.json")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(PHASE_D_HARNESS)
        with open(fx, "w", encoding="utf-8") as f:
            json.dump(fixture, f, ensure_ascii=False)
        proc = subprocess.run([node_bin, runner, str(html_path), fx], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise AssertionError("Phase D UI harness failed:\n%s\n%s" % (proc.stdout, proc.stderr))
        return json.loads(proc.stdout)


def esc_html(text):
    """The page's esc(): & < > " ' as entities."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def phase_d_fixture(doctor, prune=None, project="demo", to="b"):
    return {
        "project": project, "to": to,
        "ledger": {"rev": "1", "config": {"devices": [
            {"id": "a", "name": "Win 台式", "os": "windows", "drop": "", "deviceId": "dev_a111"},
            {"id": "b", "name": "MacBook Pro", "os": "mac", "drop": "", "deviceId": "dev_b222"}], "reg": {}},
            "projects": [{"id": project, "name": "Demo", "device": "a", "paths": {"a": "/code/demo"}, "sessions": []}]},
        "status": {"configured": True, "cloud": {"provider": "local", "root": "/cloud"}, "deviceId": "dev_a111",
                   "alias": "a", "paired": True, "hasCrypto": True,
                   "peers": [{"deviceId": "dev_b222", "alias": "b", "status": "paired", "paired": True}],
                   "syncWatch": {"running": False, "status": "", "runId": "", "lastSeq": 0, "cycles": 0}},
        "doctor": doctor,
        "prune": prune or {"mode": "dry-run", "planDigest": "ab" * 32, "estimatedBytesFreed": 2048,
                           "counts": {"deletablePayloads": 2, "deletableCheckpoints": 3, "leftovers": 0,
                                      "keptPayloads": 1, "keptCheckpoints": 0, "unclear": 1}},
        "sendError": {"error": "目标设备「b」已更换密钥，在途交接 r_1 是发给它旧公钥的。", "code": "handoff_recipient_key_changed",
                      "action": "relay send %s --to %s --reissue" % (project, to)},
        "sendOk": {"ok": True, "relayId": "r_2", "reissued": True, "supersedesRelayId": "r_1", "to": to,
                   "packageSize": 10, "packageSha256": "cd" * 32, "message": "已为目标设备当前公钥重新签发交接（取代 r_1，原交接包保持不变）。已交棒，可以关机"},
    }


class TestWebWorkbenchHtml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html_path = REPO_ROOT / "relay-desk.html"
        cls.html = cls.html_path.read_text(encoding="utf-8")

    def test_artifact_compatible_no_document_wrapper_tags(self):
        for pat in (
            r"<\s*!doctype\b",
            r"<\s*/?\s*html\b",
            r"<\s*/?\s*head\b",
            r"<\s*/?\s*body\b",
        ):
            m = re.search(pat, self.html, re.I)
            self.assertIsNone(
                m,
                "relay-desk.html must not contain outer document tag %r"
                % (m.group(0) if m else pat),
            )

    def test_javascript_syntax_and_handlers(self):
        scripts = re.findall(r"<script>(.*?)</script>", self.html, re.S)
        self.assertEqual(len(scripts), 1, "Expected exactly one inline <script> block")
        script = scripts[0]
        self.assertIn("'use strict';", script)

        # Structural check: every data-act and data-chg used in templates is wired in ACT / CHG
        act_match = re.search(r"const\s+ACT\s*=\s*\{(.*?)\n\};", script, re.S)
        chg_match = re.search(r"const\s+CHG\s*=\s*\{(.*?)\n\};", script, re.S)
        self.assertIsNotNone(act_match)
        self.assertIsNotNone(chg_match)
        defined_acts = set(re.findall(r"(?:^|\n)\s*([A-Za-z0-9_]+)\s*:", act_match.group(1)))
        defined_chgs = set(re.findall(r"(?:^|\n)\s*([A-Za-z0-9_]+)\s*:", chg_match.group(1)))
        used_acts = set(re.findall(r'data-act="([A-Za-z0-9_]+)"', self.html))
        used_chgs = set(re.findall(r'data-chg="([A-Za-z0-9_]+)"', self.html))
        self.assertTrue(
            used_acts.issubset(defined_acts),
            "Missing ACT handlers: %r" % sorted(used_acts - defined_acts),
        )
        self.assertTrue(
            used_chgs.issubset(defined_chgs),
            "Missing CHG handlers: %r" % sorted(used_chgs - defined_chgs),
        )

        node_bin = _find_node()
        if node_bin:
            with tempfile.TemporaryDirectory() as td:
                js_file = os.path.join(td, "workbench.js")
                with open(js_file, "w", encoding="utf-8") as f:
                    f.write(script)
                proc = subprocess.run(
                    [node_bin, "--check", js_file],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    "node --check failed:\n%s" % proc.stderr,
                )

    def test_store_modes_and_cloud_relay_features_present(self):
        # Preserve all three Store modes + manual transports
        for mode_token in ("S.mode = 'local'", "S.mode = 'file'", "S.mode = 'db'"):
            self.assertIn(mode_token, self.html)
        for tr in ("remote:", "bundle:", "patch:", "folder:"):
            self.assertIn(tr, self.html)

        # Required cloud UI copy & endpoints
        for required in (
            "已交棒，可以关机",
            "待接棒",
            "云端异步接力（加密自动上传）",
            "加密上传并交棒",
            "接收交接包",
            "历史对话全文",
            "云接力与设备配对",
            "保存并发布本机公钥",
            "核对指纹无误，确认配对",
            "/api/cloud/status",
            "/api/cloud/pair",
            "/api/cloud/inbox",
            "/api/cloud/send",
            "/api/cloud/receive",
            "/api/cloud/publish",
            "/api/cloud/sync",
            "/api/cloud/recover",
            "/api/cloud/transcripts",
            "/api/cloud/sync/watch",
            "/api/cloud/sync/watch/stop",
            "持续同步",
            "部分失败（自动重试中）",
            "仅显示最近 200 条事件，累计数以计数为准",
            "need_path",
            "target_dirty",
            "ambiguous_peer",
            "recovery_conflict",
            "X-Relay-Desk",
        ):
            self.assertIn(required, self.html)

    def test_runtime_ui_flows_in_node(self):
        node_bin = _find_node()
        if not node_bin:
            self.skipTest("node is not installed")
        harness = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const els = {};
function makeEl(id, tag="DIV") {
  const el = {
    id, tagName: tag, hidden: false, open: false, value: "", textContent: "", _html: "",
    className: "", dataset: {}, disabled: false, offsetTop: 100, isConnected: true,
    elements: {},
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    querySelector(sel) {
      if (sel === "#dlgForm" || sel === "#rcvForm") {
        const f = makeEl(sel.slice(1), "FORM");
        f.elements = { path: makeEl("rcv-path", "INPUT"), into: makeEl("rcv-into", "INPUT") };
        el._form = f;
        return f;
      }
      return null;
    },
    addEventListener(ev, fn) { el["_on_" + ev] = fn; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    setAttribute() {},
    focus() {},
    scrollIntoView() {},
    contains() { return false; }
  };
  els[id] = el;
  return el;
}
["hereSel","syncBadge","notice","track","tabs","filters","f-device","f-count","f-q","f-tool","f-status","f-dirty","view","dlg","txDlg","toast","io","cloud-remote","cloud-root","tx-q"].forEach(id => makeEl(id));

const listeners = {};
global.window = { claude: null, scrollTo() {} };
global.location = { protocol: "http:", hostname: "127.0.0.1" };
global.navigator = { userAgent: "Macintosh", clipboard: { writeText: () => Promise.resolve() } };
global.localStorage = { _m: {}, getItem(k){ return this._m[k] || null; }, setItem(k,v){ this._m[k]=String(v); } };
global.AbortController = class { constructor(){ this.signal = {}; } abort(){} };
const origSetTimeout = global.setTimeout;
global.setInterval = () => 1;
global.clearInterval = () => {};
global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.document = {
  hidden: false,
  activeElement: null,
  getElementById(id) { return els[id] || null; },
  querySelector(s) { return s.startsWith("#") ? (els[s.slice(1)] || makeEl(s.slice(1))) : null; },
  addEventListener(ev, fn) { listeners[ev] = fn; }
};

let receiveStep = 0;
const fetchCalls = [];
global.fetch = async (url, opt) => {
  fetchCalls.push({ method: opt.method, url, headers: opt.headers, body: opt.body ? JSON.parse(opt.body) : null });
  if (url.startsWith("/api/ledger")) {
    return { ok: true, status: 200, json: async () => ({
      rev: "1",
      config: { devices: [{ id: "a", name: "Win 台式", os: "windows", drop: "" }, { id: "b", name: "MacBook", os: "mac", drop: "" }], reg: {} },
      projects: [{ id: "demo", name: "Demo App", device: "b", branch: "main", remote: "origin", dirty: 2, tool: "all", paths: { b: "/Users/me/demo" }, sessions: [{ id: "s1", tool: "anti", device: "b", sessionId: "conv-1", title: "测试会话" }] }]
    }) };
  }
  if (url === "/api/cloud/status") {
    return { ok: true, status: 200, json: async () => ({
      configured: true, cloud: { provider: "rclone", remote: "my_s3", root: "relay-desk" },
      deviceId: "dev_b123", alias: "b", keyFingerprint: "AAAA-BBBB-CCCC-DDDD",
      paired: true, pairFingerprint: "1111-2222-3333-4444-5555-6666",
      peer: { deviceId: "dev_a999", alias: "a", name: "Win 台式", os: "windows", keyFingerprint: "EEEE-FFFF-0000-1111" },
      remotes: ["my_s3", "onedrive"], hasCrypto: true, hasRclone: true
    }) };
  }
  if (url === "/api/cloud/inbox") {
    return { ok: true, status: 200, json: async () => ({
      incoming: [{ relayId: "r_20261118_abcd1234", parentRelayId: "", fromAlias: "a", toAlias: "b", createdAt: "2026-11-18T01:00:00Z", packageSize: 4096, projectId: "newproj", projectName: "New Proj", lastTask: "完成加密", nextAction: "写文档", branch: "feat/cloud", dirty: 3, sessionCount: 2, pendingAck: false }],
      syncedReceipts: []
    }) };
  }
  if (url === "/api/cloud/pair") {
    return { ok: true, status: 200, json: async () => ({
      configured: true, cloud: { provider: "rclone", remote: "my_s3", root: "relay-desk" },
      deviceId: "dev_b123", alias: "b", keyFingerprint: "AAAA-BBBB-CCCC-DDDD",
      paired: true, pairFingerprint: "1111-2222-3333-4444-5555-6666",
      peer: { deviceId: "dev_a999", alias: "a", name: "Win 台式", os: "windows", keyFingerprint: "EEEE-FFFF-0000-1111" },
      remotes: ["my_s3", "onedrive"], hasCrypto: true, hasRclone: true
    }) };
  }
  if (url === "/api/cloud/send") {
    return { ok: true, status: 200, json: async () => ({
      ok: true, relayId: "r_20261118_sent9999", packageSize: 8192, packageSha256: "deadbeef1234", message: "已交棒，可以关机"
    }) };
  }
  if (url === "/api/cloud/receive") {
    receiveStep++;
    if (receiveStep === 1) return { ok: false, status: 409, json: async () => ({ error: "首次接收需指定目录", code: "need_path", projectName: "New Proj" }) };
    if (receiveStep === 2) return { ok: false, status: 409, json: async () => ({ error: "目标目录存在未同步改动", code: "target_dirty", projectName: "New Proj", targetPath: "/Users/me/newproj", reasons: ["M README.md"] }) };
    return { ok: true, status: 200, json: async () => ({ ok: true, relayId: "r_20261118_abcd1234", projectId: "demo", targetPath: "/Users/me/newproj-clean", lastTask: "完成加密", nextAction: "写文档", conversations: { total: 3, added: 2, updated: 0 }, message: "已接收" }) };
  }
  if (url.startsWith("/api/cloud/transcripts")) {
    return { ok: true, status: 200, json: async () => ({
      projectId: "demo", archiveDir: "/tmp/archives/demo",
      sessions: [{ tool: "anti", sessionId: "conv-1", contentVersion: "v1_abc", originDevice: "a", title: "测试会话", lastUsed: "2026-11-18", turnCount: 4 }],
      matches: [{ tool: "anti", sessionId: "conv-1", title: "测试会话", role: "assistant", snippet: "命中片段" }],
      markdown: "# 测试会话\n\n1. [user] 你好\n2. [assistant] 已完成"
    }) };
  }
  throw new Error("Unexpected url: " + url);
};

eval(script);
(async () => {
  await new Promise(r => origSetTimeout(r, 30));
  const click = (act, dataset={}) => listeners.click({ target: { closest: () => ({ dataset: Object.assign({ act }, dataset), disabled: false }) } });

  if (!els.tabs.innerHTML.includes("待接棒") || !els.tabs.innerHTML.includes(">1<")) throw new Error("Inbox badge missing");
  click("tab", { v: "inbox" });
  await new Promise(r => origSetTimeout(r, 15));
  if (!els.view.innerHTML.includes("New Proj") || !els.view.innerHTML.includes("r_20261118_abcd1234")) throw new Error("Inbox item missing");

  click("receiveRelay", { r: "r_20261118_abcd1234" });
  if (!els.dlg.innerHTML.includes("rcv-path")) throw new Error("need_path dialog missing");
  els.dlg._form.elements.path.value = "/Users/me/newproj";
  els.dlg._form._on_submit({ preventDefault(){} });
  await new Promise(r => origSetTimeout(r, 15));
  els.dlg._form.elements.path.value = "/Users/me/newproj";
  els.dlg._form._on_submit({ preventDefault(){} });
  await new Promise(r => origSetTimeout(r, 15));
  if (!els.dlg.innerHTML.includes("原目录已完整保留") || !els.dlg.innerHTML.includes("rcv-into")) throw new Error("target_dirty dialog missing");
  els.dlg._form.elements.into.value = "/Users/me/newproj-clean";
  els.dlg._form._on_submit({ preventDefault(){} });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("/Users/me/newproj-clean") || !els.view.innerHTML.includes("查看历史对话全文")) throw new Error("Receive confirmation missing");

  click("openTranscripts", { id: "demo" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.txDlg.innerHTML.includes("# 测试会话")) throw new Error("Transcript markdown missing");

  click("relay", { id: "demo" });
  await new Promise(r => origSetTimeout(r, 15));
  click("cloudSend", { id: "demo", to: "a" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("已交棒，可以关机") || !els.view.innerHTML.includes("r_20261118_sent9999")) throw new Error("Send banner missing");

  click("tab", { v: "settings" });
  await new Promise(r => origSetTimeout(r, 15));
  if (!els.view.innerHTML.includes("1111-2222-3333-4444-5555-6666")) throw new Error("Pair fingerprint missing");
  els["cloud-remote"].value = "my_s3";
  els["cloud-root"].value = "relay-desk";
  click("cloudPairPublish");
  await new Promise(r => origSetTimeout(r, 15));
  click("cloudPairAccept");
  await new Promise(r => origSetTimeout(r, 15));

  const posts = fetchCalls.filter(c => c.method === "POST");
  if (posts.length < 4 || posts.some(p => p.headers["X-Relay-Desk"] !== "1")) throw new Error("Missing X-Relay-Desk header");
})().catch(e => { console.error(e); process.exit(1); });
"""
        with tempfile.TemporaryDirectory() as td:
            runner = os.path.join(td, "run_ui.js")
            with open(runner, "w", encoding="utf-8") as f:
                f.write(harness)
            proc = subprocess.run(
                [node_bin, runner, str(self.html_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(
                proc.returncode,
                0,
                "Runtime UI flow check failed:\n%s\n%s" % (proc.stdout, proc.stderr),
            )

    def test_sync_watch_ui_in_node(self):
        node_bin = _find_node()
        if not node_bin:
            self.skipTest("node is not installed")
        harness = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {};
function makeEl(id, tag="DIV") {
  const el = {
    id, tagName: tag, hidden: false, open: false, value: "", textContent: "", _html: "",
    className: "", dataset: {}, disabled: false, offsetTop: 100, isConnected: true, elements: {},
    set innerHTML(v) { this._html = String(v); }, get innerHTML() { return this._html; },
    querySelector() { return null; }, addEventListener(ev, fn) { el["_on_" + ev] = fn; },
    showModal() { this.open = true; }, close() { this.open = false; }, setAttribute() {}, focus() {},
    scrollIntoView() {}, contains() { return false; }
  };
  els[id] = el;
  return el;
}
["hereSel","syncBadge","notice","track","tabs","filters","f-device","f-count","f-q","f-tool","f-status","f-dirty","view","dlg","txDlg","toast","io","cloud-remote","cloud-root","tx-q"].forEach(id => makeEl(id));
const listeners = {};
global.window = { claude: null, scrollTo() {} };
global.location = { protocol: "http:", hostname: "127.0.0.1" };
global.navigator = { userAgent: "Macintosh", clipboard: { writeText: () => Promise.resolve() } };
global.localStorage = { _m: {}, getItem(k){ return this._m[k] || null; }, setItem(k,v){ this._m[k]=String(v); } };
global.AbortController = class { constructor(){ this.signal = {}; } abort(){} };
const origSetTimeout = global.setTimeout;
global.setInterval = () => 1; global.clearInterval = () => {};
global.setTimeout = () => 1; global.clearTimeout = () => {};
global.document = {
  hidden: false, activeElement: null,
  getElementById(id) { return els[id] || null; },
  querySelector(s) { return s.startsWith("#") ? (els[s.slice(1)] || makeEl(s.slice(1))) : null; },
  addEventListener(ev, fn) { listeners[ev] = fn; }
};
const calls = [];
const peers = { dev_b222: { alias: "b", state: "backoff", nextAttemptInSec: 12.4, lastErrorClass: "transient_transport" } };
global.fetch = async (url, opt) => {
  const body = opt.body ? JSON.parse(opt.body) : null;
  calls.push({ method: opt.method, url, body });
  const ok = (d) => ({ ok: true, status: 200, json: async () => d });
  if (url.startsWith("/api/ledger")) return ok({ rev: "1", config: { devices: [
      { id: "a", name: "Win 台式", os: "windows", drop: "", deviceId: "dev_a111" },
      { id: "b", name: "MacBook Pro", os: "mac", drop: "", deviceId: "dev_b222" }], reg: {} },
    projects: [{ id: "demo", name: "Demo", device: "a", paths: { a: "/code/demo" }, sessions: [] }] });
  if (url === "/api/cloud/status") return ok({ configured: true, cloud: { provider: "local", root: "/cloud" }, deviceId: "dev_a111",
    alias: "a", paired: true, peers: [{ deviceId: "dev_b222", alias: "b", status: "paired", paired: true }],
    hasCrypto: true, syncWatch: { running: false, status: "", runId: "", lastSeq: 0, cycles: 0 } });
  if (url === "/api/cloud/inbox") return ok({ incoming: [], syncedReceipts: [] });
  if (url === "/api/cloud/sync/watch" && opt.method === "POST") return ok({ running: true, runId: "sr_20261118_aa", status: "success",
    cycles: 0, counters: null, peers: {}, unresolved: [], events: [], gap: 0, lastSeq: 1 });
  if (url.startsWith("/api/cloud/sync/watch?since=")) return ok({ running: true, runId: "sr_20261118_aa", status: "partial", cycles: 7,
    counters: { snapshotsCached: 3, forwarded: 2, receiptsSynced: 1, retriesScheduled: 4 }, peers,
    unresolved: [{ key: "inbox_list||", errorClass: "transient_transport", code: "storage_transport", message: "云端暂时不可用" },
                 { key: "peer|dev_b222|trust", errorClass: "needs_user_action", code: "peer_key_changed", message: "对端密钥已变更", action: "relay pair --with dev_b222 --accept" }],
    recentEventsTruncated: true, gap: 37, lastSeq: 240,
    events: [{ seq: 239, kind: "snapshot_cached", at: "2026-11-18T10:00:00.000Z", snapshotId: "snap_x" },
             { seq: 240, kind: "problem", at: "2026-11-18T10:00:01.000Z", errorClass: "transient_transport", message: "云端暂时不可用" }] });
  if (url === "/api/cloud/sync/watch/stop") return ok({ running: false, status: "cancelled", runId: "sr_20261118_aa", cycles: 8,
    counters: { snapshotsCached: 3, forwarded: 2, receiptsSynced: 1, retriesScheduled: 4 }, peers, unresolved: [] });
  throw new Error("Unexpected url: " + url);
};
eval(script);
(async () => {
  const tick = () => new Promise(r => origSetTimeout(r, 30));
  await tick();
  const click = (act, dataset={}) => listeners.click({ target: { closest: () => ({ dataset: Object.assign({ act }, dataset), disabled: false }) } });
  click("tab", { v: "inbox" });
  await tick();
  let v = els.view.innerHTML;
  if (!v.includes('id="btn-inbox-watch"') || !v.includes(">持续同步<")) throw new Error("watch toggle missing");
  if (calls.some(c => c.url.startsWith("/api/cloud/sync/watch"))) throw new Error("watch polled before it was started");
  click("cloudWatchStart");
  await tick(); await tick();
  v = els.view.innerHTML;
  if (!v.includes("运行中 · 部分失败（自动重试中）")) throw new Error("running status chip missing: " + v);
  if (!v.includes("第 7 轮 · 快照 3 · 转发 2 · 回执 1 · 重试 4")) throw new Error("counters missing");
  if (!v.includes("MacBook Pro · 重试中（12s 后）")) throw new Error("peer chip missing");
  if (!v.includes("relay pair --with dev_b222 --accept")) throw new Error("action command missing");
  if (!v.includes("仅显示最近 200 条事件，累计数以计数为准")) throw new Error("truncation note missing");
  if (!v.includes("缓存快照 snap_x")) throw new Error("event list missing");
  if (!v.includes('停止持续同步')) throw new Error("stop button missing");
  if (!/id="btn-inbox-sync" disabled/.test(v)) throw new Error("one-shot sync must be disabled while watching");
  const polls = calls.filter(c => c.url.startsWith("/api/cloud/sync/watch?since="));
  if (polls.length !== 1 || polls[0].url !== "/api/cloud/sync/watch?since=1") throw new Error("unexpected polls " + JSON.stringify(polls));
  click("cloudWatchPoll");
  await tick();
  const polls2 = calls.filter(c => c.url.startsWith("/api/cloud/sync/watch?since="));
  if (polls2[1].url !== "/api/cloud/sync/watch?since=240") throw new Error("poll did not use lastSeq: " + polls2[1].url);
  click("cloudWatchStop");
  await tick(); await tick();
  v = els.view.innerHTML;
  if (!v.includes('id="sync-watch-status">已取消')) throw new Error("cancelled status missing");
  if (!v.includes(">持续同步<")) throw new Error("toggle not reset after stop");
  const posts = calls.filter(c => c.method === "POST").map(c => c.url);
  if (JSON.stringify(posts) !== JSON.stringify(["/api/cloud/sync/watch", "/api/cloud/sync/watch/stop"])) throw new Error("posts " + posts);
})().catch(e => { console.error(e); process.exit(1); });
"""
        with tempfile.TemporaryDirectory() as td:
            runner = os.path.join(td, "run_ui_watch.js")
            with open(runner, "w", encoding="utf-8") as f:
                f.write(harness)
            proc = subprocess.run([node_bin, runner, str(self.html_path)], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, "Sync watch UI check failed:\n%s\n%s" % (proc.stdout, proc.stderr))

    def test_three_device_runtime_ui_flows_in_node(self):
        node_bin = _find_node()
        if not node_bin:
            self.skipTest("node is not installed")
        harness = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const els = {};
function makeEl(id, tag="DIV") {
  const el = {
    id, tagName: tag, hidden: false, open: false, value: "", textContent: "", _html: "",
    className: "", dataset: {}, disabled: false, offsetTop: 100, isConnected: true,
    elements: {},
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    querySelector(sel) {
      if (sel === "#dlgForm" || sel === "#rcvForm") {
        const f = makeEl(sel.slice(1), "FORM");
        f.elements = { path: makeEl("rcv-path", "INPUT"), into: makeEl("rcv-into", "INPUT") };
        el._form = f;
        return f;
      }
      return null;
    },
    addEventListener(ev, fn) { el["_on_" + ev] = fn; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    setAttribute() {},
    focus() {},
    scrollIntoView() {},
    contains() { return false; }
  };
  els[id] = el;
  return el;
}
["hereSel","syncBadge","notice","track","tabs","filters","f-device","f-count","f-q","f-tool","f-status","f-dirty","view","dlg","txDlg","toast","io","cloud-remote","cloud-root","tx-q"].forEach(id => makeEl(id));

const listeners = {};
global.window = { claude: null, scrollTo() {} };
global.location = { protocol: "http:", hostname: "127.0.0.1" };
global.navigator = { userAgent: "Macintosh", clipboard: { writeText: () => Promise.resolve() } };
global.localStorage = { _m: {}, getItem(k){ return this._m[k] || null; }, setItem(k,v){ this._m[k]=String(v); } };
global.AbortController = class { constructor(){ this.signal = {}; } abort(){} };
const origSetTimeout = global.setTimeout;
global.setInterval = () => 1;
global.clearInterval = () => {};
global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.document = {
  hidden: false,
  activeElement: null,
  getElementById(id) { return els[id] || null; },
  querySelector(s) { return s.startsWith("#") ? (els[s.slice(1)] || makeEl(s.slice(1))) : null; },
  addEventListener(ev, fn) { listeners[ev] = fn; }
};

const fetchCalls = [];
let sendCount = 0;
global.fetch = async (url, opt) => {
  const body = opt.body ? JSON.parse(opt.body) : null;
  fetchCalls.push({ method: opt.method, url, headers: opt.headers, body });
  if (url.startsWith("/api/ledger")) {
    return { ok: true, status: 200, json: async () => ({
      rev: "1",
      config: {
        devices: [
          { id: "a", name: "Win 台式", os: "windows", drop: "", deviceId: "dev_a111" },
          { id: "b", name: "MacBook Pro", os: "mac", drop: "", deviceId: "dev_b222" },
          { id: "c", name: "Linux 工作站", os: "linux", drop: "", deviceId: "dev_c333" }
        ],
        reg: {}
      },
      projects: [
        { id: "demo", name: "Multi Demo", device: "a", branch: "main", remote: "origin", dirty: 1, tool: "all", paths: { a: "/code/demo", b: "/Users/me/demo", c: "/srv/demo" }, sessions: [] }
      ]
    }) };
  }
  if (url === "/api/cloud/status") {
    return { ok: true, status: 200, json: async () => ({
      configured: true,
      cloud: { provider: "rclone", remote: "my_s3", root: "relay-desk" },
      deviceId: "dev_a111",
      alias: "a",
      keyFingerprint: "AAAA-1111-2222-3333",
      paired: true,
      pairFingerprint: "BBBB-2222-3333-4444-5555-6666",
      peers: [
        { deviceId: "dev_b222", alias: "b", name: "MacBook Pro", os: "mac", status: "paired", paired: true, keyFingerprint: "BBBB-0000-1111-2222", pairFingerprint: "BBBB-2222-3333-4444-5555-6666" },
        { deviceId: "dev_c333", alias: "c", name: "Linux 工作站", os: "linux", status: "paired", paired: true, keyFingerprint: "CCCC-0000-1111-2222", pairFingerprint: "CCCC-3333-4444-5555-6666-7777" }
      ],
      remotes: ["my_s3"],
      hasCrypto: true,
      hasRclone: true
    }) };
  }
  if (url === "/api/cloud/inbox") {
    return { ok: true, status: 200, json: async () => ({ incoming: [], syncedReceipts: [] }) };
  }
  if (url === "/api/cloud/publish") {
    return { ok: true, status: 200, json: async () => ({
      ok: true, snapshotId: "snap_20261118_1234", projectId: "demo", deliveredTo: [{ alias: "b", deviceId: "dev_b222" }, { alias: "c", deviceId: "dev_c333" }]
    }) };
  }
  if (url === "/api/cloud/sync") {
    return { ok: true, status: 200, json: async () => ({
      ok: true, cycles: 1, syncedSnapshots: [{ snapshotId: "snap_remote_1", projectId: "demo" }], forwarded: [], syncedReceipts: [], conflictsDetected: []
    }) };
  }
  if (url === "/api/cloud/send") {
    sendCount++;
    if (!body.to) {
      return { ok: false, status: 409, json: async () => ({
        error: "已配对多台设备，请指定目标设备",
        code: "ambiguous_peer",
        candidates: [
          { alias: "b", deviceId: "dev_b222", name: "MacBook Pro" },
          { alias: "c", deviceId: "dev_c333", name: "Linux 工作站" }
        ]
      }) };
    }
    return { ok: true, status: 200, json: async () => ({
      ok: true, relayId: "r_20261118_to_c", to: body.to, packageSize: 4096, packageSha256: "cafebabe", message: "已交棒，可以关机"
    }) };
  }
  if (url === "/api/cloud/pair") {
    return { ok: true, status: 200, json: async () => ({
      configured: true,
      cloud: { provider: "rclone", remote: "my_s3", root: "relay-desk" },
      deviceId: "dev_a111",
      alias: "a",
      keyFingerprint: "AAAA-1111-2222-3333",
      paired: true,
      pairFingerprint: "CCCC-3333-4444-5555-6666-7777",
      peers: [
        { deviceId: "dev_b222", alias: "b", name: "MacBook Pro", os: "mac", status: "paired", paired: true, keyFingerprint: "BBBB-0000-1111-2222", pairFingerprint: "BBBB-2222-3333-4444-5555-6666" },
        { deviceId: "dev_c333", alias: "c", name: "Linux 工作站", os: "linux", status: "paired", paired: true, keyFingerprint: "CCCC-0000-1111-2222", pairFingerprint: "CCCC-3333-4444-5555-6666-7777" }
      ],
      remotes: ["my_s3"],
      hasCrypto: true,
      hasRclone: true
    }) };
  }
  throw new Error("Unexpected url: " + url);
};

eval(script);
(async () => {
  await new Promise(r => origSetTimeout(r, 30));
  const click = (act, dataset={}) => listeners.click({ target: { closest: () => ({ dataset: Object.assign({ act }, dataset), disabled: false }) } });
  const change = (chg, dataset={}, value="") => listeners.change({ target: { closest: () => ({ dataset: Object.assign({ chg }, dataset), value }) } });

  // Verify dynamic 3-device rendering in track, hereSel, f-device
  if (els.track.className !== "track multi") throw new Error("Expected track.className === 'track multi', got " + els.track.className);
  for (const name of ["Win 台式", "MacBook Pro", "Linux 工作站"]) {
    if (!els.track.innerHTML.includes(name)) throw new Error("Track missing device lane: " + name);
    if (!els.hereSel.innerHTML.includes(name)) throw new Error("hereSel missing device: " + name);
    if (!els["f-device"].innerHTML.includes(name)) throw new Error("f-device missing device: " + name);
  }

  // Open viewRelay for "demo"
  click("relay", { id: "demo" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("cloud-target-demo")) throw new Error("Multi-peer cloud target selector missing in viewRelay");
  if (!els.view.innerHTML.includes("MacBook Pro") || !els.view.innerHTML.includes("Linux 工作站")) {
    throw new Error("Multi-peer options missing in viewRelay");
  }
  if (!els.view.innerHTML.includes('data-chg="handoffTo"')) throw new Error("Manual handoff target selector missing in viewRelay");

  // cloudPublish
  click("cloudPublish", { id: "demo" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("已发布快照缓存（本机继续保留编辑权）") || !els.view.innerHTML.includes("snap_20261118_1234")) {
    throw new Error("Publish result box missing after cloudPublish");
  }

  // cloudSync
  click("cloudSync", { id: "demo" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("网络快照同步结果") || !els.view.innerHTML.includes("同步快照 1 条")) {
    throw new Error("Sync result box missing after cloudSync");
  }

  // Sending without target triggers 409 ambiguous_peer and renders candidate buttons
  click("cloudSend", { id: "demo" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("发往 MacBook Pro") || !els.view.innerHTML.includes("发往 Linux 工作站")) {
    throw new Error("Candidate buttons missing after 409 ambiguous_peer");
  }

  // Pick target "c" via cloudTarget selector, then send
  change("cloudTarget", { id: "demo" }, "c");
  click("cloudSend", { id: "demo" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("已交棒，可以关机") || !els.view.innerHTML.includes("r_20261118_to_c")) {
    throw new Error("Send confirmation missing after selecting target c");
  }

  // Switch to viewSettings and verify 3-device cards + multi-peer pairing selector
  click("tab", { v: "settings" });
  await new Promise(r => origSetTimeout(r, 20));
  if (!els.view.innerHTML.includes("已登记设备（3 台）")) throw new Error("3-device header missing in viewSettings");
  if (!els.view.innerHTML.includes("cloud-peer-sel") || !els.view.innerHTML.includes("已发现的云端设备（2 台）")) {
    throw new Error("Multi-peer pairing selector missing in viewSettings");
  }
  click("cloudPickPeer", { did: "dev_c333" });
  if (!els.view.innerHTML.includes("CCCC-3333-4444-5555-6666-7777")) {
    throw new Error("Selected peer c fingerprint not shown in viewSettings");
  }
  click("cloudPairAccept");
  await new Promise(r => origSetTimeout(r, 20));

  const pairPosts = fetchCalls.filter(c => c.url === "/api/cloud/pair" && c.method === "POST");
  if (!pairPosts.length || pairPosts[0].body.with !== "dev_c333" || pairPosts[0].body.fingerprint !== "CCCC-3333-4444-5555-6666-7777") {
    throw new Error("Expected multi-peer pair POST with dev_c333 and fingerprint, got: " + JSON.stringify(pairPosts));
  }
  const sendPosts = fetchCalls.filter(c => c.url === "/api/cloud/send" && c.method === "POST");
  if (sendPosts.length !== 2 || sendPosts[1].body.to !== "c") {
    throw new Error("Expected second send POST to target c, got: " + JSON.stringify(sendPosts));
  }
  const pubPosts = fetchCalls.filter(c => c.url === "/api/cloud/publish" && c.method === "POST");
  const syncPosts = fetchCalls.filter(c => c.url === "/api/cloud/sync" && c.method === "POST");
  if (pubPosts.length !== 1 || pubPosts[0].body.project !== "demo") throw new Error("Unexpected publish calls");
  if (syncPosts.length !== 1 || syncPosts[0].body.project !== "demo") throw new Error("Unexpected sync calls");
  if (fetchCalls.filter(c => c.method === "POST").some(p => p.headers["X-Relay-Desk"] !== "1")) {
    throw new Error("Missing X-Relay-Desk header on POST");
  }
})().catch(e => { console.error(e); process.exit(1); });
"""
        with tempfile.TemporaryDirectory() as td:
            runner = os.path.join(td, "run_ui_3dev.js")
            with open(runner, "w", encoding="utf-8") as f:
                f.write(harness)
            proc = subprocess.run(
                [node_bin, runner, str(self.html_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(
                proc.returncode,
                0,
                "3-device runtime UI flow check failed:\n%s\n%s" % (proc.stdout, proc.stderr),
            )


class TestPhaseDNetworkPanel(unittest.TestCase):
    html_path = REPO_ROOT / "relay-desk.html"

    def test_network_panel_reissue_button_and_prune_preview(self):
        node_bin = _find_node()
        if not node_bin:
            self.skipTest("node is not installed")
        doctor = {
            "schema": "relaydesk.doctor/1", "status": "needs_action",
            "findings": [
                {"code": "handoff_recipient_key_changed", "severity": "error", "projectId": "demo", "relayId": "r_1",
                 "reason": "交接 r_1 是发给 b 换钥前公钥的 <old> & \"key\"", "command": "relay send demo --to b --reissue"},
                {"code": "sync_never_run", "severity": "info", "reason": "本机还没有同步记录。", "command": "relay sync"}],
            "projects": [{"projectId": "demo", "projectName": "Demo", "editor": {"alias": "a", "name": "Win 台式", "isHere": True},
                          "handoff": {"relayId": "r_1", "from": "a", "to": "b", "toDeviceId": "dev_b222", "transport": "cloud"},
                          "versions": ["snap_1"],
                          "devices": [{"deviceId": "dev_b222", "alias": "b", "name": "MacBook Pro", "status": "unknown",
                                       "label": "未知", "detail": "尚无该设备最近的缓存清单记录"}],
                          "confirmedCache": [], "awaiting": [], "pendingResend": [], "unknown": ["b"], "needsAction": []}],
        }
        out = render_phase_d(node_bin, self.html_path, phase_d_fixture(doctor))
        v = out["inbox"]
        self.assertIn('id="net-panel"', v)
        self.assertIn("当前编辑设备：Win 台式（本机）", v)
        self.assertIn("在途交接 <code>r_1</code>：a → b", v)
        self.assertIn("MacBook Pro · 未知", v)
        for word in ("离线", "缺少快照"):
            self.assertNotIn(word, v.replace("不代表离线或缺少快照", ""))
        for f in doctor["findings"]:  # reason and command exactly as the API gave them (escaped once)
            self.assertIn(esc_html(f["reason"]), v)
            self.assertIn('<code class="dx-command">%s</code>' % esc_html(f["command"]), v)
        self.assertIn("预计释放 2.0 KB", out["preview"])
        self.assertIn("relay cache prune --apply --expect-plan " + "ab" * 32, out["preview"])
        self.assertIn('data-act="cloudReissue"', out["sendError"])
        self.assertIn('<code class="sync-action">relay send demo --to b --reissue</code>', out["sendError"])
        sends = [c for c in out["calls"] if c["url"] == "/api/cloud/send"]
        self.assertEqual([c["body"].get("reissue") for c in sends], [None, True])  # only the explicit click re-issues
        prunes = [c for c in out["calls"] if c["url"] == "/api/cloud/cache/prune"]
        self.assertEqual([c["body"] for c in prunes], [{"mode": "dry-run"}])  # the page never applies a prune

    def test_phase_d_strings_and_endpoints_present(self):
        html = self.html_path.read_text(encoding="utf-8")
        for required in ("/api/cloud/doctor", "/api/cloud/cache/prune", "接力网络状态", "重新诊断", "预览缓存清理",
                         "为当前公钥重新签发交接", "不代表离线或缺少快照", "cloudReissue"):
            self.assertIn(required, html)


class TestAgentSkillsAndDocs(unittest.TestCase):
    SKILL_PATHS = (
        ("claude", REPO_ROOT / "skills" / "claude" / "relay-desk" / "SKILL.md"),
        ("codex", REPO_ROOT / "skills" / "codex" / "relay-desk" / "SKILL.md"),
        ("anti", REPO_ROOT / "skills" / "anti" / "relay-desk" / "SKILL.md"),
    )

    def test_all_three_skills_valid_and_complete(self):
        for tool, path in self.SKILL_PATHS:
            with self.subTest(tool=tool):
                self.assertTrue(path.is_file(), "Missing skill file: %s" % path)
                text = path.read_text(encoding="utf-8")
                meta = _parse_frontmatter(text)
                self.assertIsNotNone(meta, "%s SKILL.md missing valid YAML frontmatter" % tool)
                self.assertEqual(meta.get("name"), "relay-desk")
                self.assertTrue(meta.get("description"), "%s SKILL.md missing description" % tool)

                for token in (
                    "{{RELAY}}",
                    "pair",
                    "cloud remotes",
                    "publish",
                    "sync",
                    "send",
                    "inbox",
                    "receive",
                    "recover",
                    "transcripts",
                    "--with",
                    "--fingerprint",
                    "--to",
                    "--path",
                    "--into",
                    "handoff",
                    "pickup",
                    "device --json",
                    "ambiguous_peer",
                    "recovery_conflict",
                    "已交棒，可以关机",
                ):
                    self.assertIn(token, text, "%s SKILL.md missing %r" % (tool, token))

                # Multi-peer user selection rule: must ask user rather than guessing target
                self.assertIn("猜测", text, "%s SKILL.md missing multi-peer no-guessing rule" % tool)
                self.assertIn("用户", text, "%s SKILL.md missing user-confirmation rule" % tool)

                # Three hard safety rules
                self.assertIn("git push", text)
                self.assertIn(".env", text)
                self.assertIn("auth.json", text)
                self.assertIn(".claude.json", text)
                self.assertIn("oauth_creds.json", text)
                self.assertIn("私钥", text)
                self.assertIn("HANDOFF.md", text)
                self.assertIn("恢复了原 Session", text)

        claude_text = (REPO_ROOT / "skills" / "claude" / "relay-desk" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("handoff 插件", claude_text)

    def test_readmes_document_cloud_and_manual_workflows(self):
        for name in ("README.zh-CN.md", "README.md"):
            with self.subTest(readme=name):
                path = REPO_ROOT / name
                self.assertTrue(path.is_file())
                text = path.read_text(encoding="utf-8")
                for token in (
                    "cloud remotes",
                    "pair",
                    "send",
                    "inbox",
                    "receive",
                    "transcripts",
                    "pynacl",
                    "rclone",
                    "StorageProvider",
                    "已交棒，可以关机",
                    "handoff",
                    "pickup",
                ):
                    self.assertIn(token, text, "%s missing %r" % (name, token))


if __name__ == "__main__":
    unittest.main()
