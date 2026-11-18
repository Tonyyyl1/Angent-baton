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
            "/api/cloud/transcripts",
            "need_path",
            "target_dirty",
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
                    "send",
                    "inbox",
                    "receive",
                    "transcripts",
                    "--path",
                    "--into",
                    "handoff",
                    "pickup",
                    "已交棒，可以关机",
                ):
                    self.assertIn(token, text, "%s SKILL.md missing %r" % (tool, token))

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
