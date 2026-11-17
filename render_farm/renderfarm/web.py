"""HTTP front end for the render farm plus the scheduler control panel.

Run::

    python3 -m renderfarm.web --data-dir ./farm_data --port 8765

The page at http://127.0.0.1:8765/ is a real control surface: every button
calls the scheduler API (the same API the workers use).  Thumbnails are
fetched from the scheduler per frame; unfinished frames render generated
placeholder PNGs with an explicit status marker rather than being hidden.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .placeholders import placeholder_png
from .renderer import DEFAULT_SETTINGS
from .storage import FarmStore, InvalidImage, StaleLease

_HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_SCENE_PATH = os.path.join(_HERE, "..", "examples", "scene_bounce.json")


class _Reaper(threading.Thread):
    def __init__(self, store: FarmStore, interval: float = 1.0):
        super().__init__(daemon=True)
        self.store = store
        self.interval = interval

    def run(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                self.store.reap_expired()
            except Exception:
                pass


def make_handler(store: FarmStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RenderFarm/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)

        # ------------------------------------------------------------ helpers
        def _json(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _png(self, status: int, blob: bytes, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(blob)

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        # --------------------------------------------------------------- GET
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                if path == "/":
                    return self._serve_panel()
                if path == "/health":
                    return self._json(200, {"ok": True, "ts": time.time()})
                if path == "/api/sample-scene":
                    with open(SAMPLE_SCENE_PATH, "rb") as fh:
                        return self._json(200, json.loads(fh.read()))
                if path == "/api/submissions":
                    return self._json(200, store.list_submissions())
                if path == "/api/workers":
                    return self._json(200, store.list_workers())
                if path == "/api/events":
                    return self._json(200, store.recent_events(int(qs.get("limit", [30])[0])))
                if path.startswith("/api/submissions/"):
                    return self._get_submission(path)
                if path.startswith("/api/scenes/"):
                    parts = path.split("/")
                    scene_id, version = parts[3], parts[4]
                    doc = store.get_scene_doc(scene_id, version)
                    return self._json(200, doc) if doc else self._error(404, "unknown scene")
                if path.startswith("/api/blobs/"):
                    return self._get_blob(path.split("/")[-1])
                self._error(404, "not found")
            except json.JSONDecodeError:
                self._error(400, "invalid JSON body")
            except Exception as exc:
                self._error(500, f"{type(exc).__name__}: {exc}")

        def _serve_panel(self) -> None:
            html = _PANEL_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def _get_submission(self, path: str) -> None:
            parts = path.split("/")
            sub_id = parts[3]
            sub = store.get_submission(sub_id)
            if not sub:
                return self._error(404, "unknown submission")
            if len(parts) == 4:
                tasks = store.list_tasks(sub_id)
                for t in tasks:
                    t["provenance"] = store.task_provenance(t["task_key"])
                return self._json(200, {
                    "submission": sub,
                    "summary": store.summary(sub_id),
                    "tasks": tasks,
                })
            if len(parts) == 6 and parts[4] == "frames":
                return self._get_frame(sub_id, int(parts[5]))
            self._error(404, "not found")

        def _get_frame(self, sub_id: str, frame: int) -> None:
            for t in store.list_tasks(sub_id):
                if t["frame"] != frame:
                    continue
                path = store.frame_blob_path(t["task_key"])
                if path:
                    with open(path, "rb") as fh:
                        blob = fh.read()
                    prov = store.task_provenance(t["task_key"])
                    return self._png(200, blob, {
                        "X-Frame-Status": "succeeded",
                        "X-Worker-Id": prov["completed_by"] or "",
                        "X-Attempt-Id": prov["completed_attempt"] or "",
                        "X-Blob-Sha256": t["blob_sha"],
                    })
                w = int(t["settings"]["width"])
                h = int(t["settings"]["height"])
                status = t["status"]
                if status == "leased":
                    status = "retrying" if t["attempt_count"] > 1 else "leased"
                return self._png(200, placeholder_png(w, h, status),
                                 {"X-Frame-Status": t["status"]})
            self._error(404, "frame not in submission")

        def _get_blob(self, sha: str) -> None:
            path = store.frame_blob_path  # noqa: F841 (directory lookup below)
            full = os.path.join(store.blob_dir, sha[:2], sha[2:] + ".png")
            if not os.path.exists(full):
                return self._error(404, "unknown blob")
            with open(full, "rb") as fh:
                self._png(200, fh.read())

        # --------------------------------------------------------------- POST
        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            try:
                if path == "/api/submissions":
                    return self._create_submission()
                if path == "/api/claim":
                    return self._claim()
                if path == "/api/heartbeat":
                    return self._heartbeat()
                if path == "/api/workers/register":
                    body = self._read_json()
                    store.register_worker(body["worker_id"], body.get("name", "worker"))
                    return self._json(200, {"ok": True})
                if path.endswith("/success"):
                    return self._succeed(path)
                if path.endswith("/fail"):
                    return self._fail(path)
                self._error(404, "not found")
            except json.JSONDecodeError:
                self._error(400, "invalid JSON body")
            except KeyError as exc:
                self._error(400, f"missing field: {exc}")
            except Exception as exc:
                self._error(500, f"{type(exc).__name__}: {exc}")

        def _create_submission(self) -> None:
            body = self._read_json()
            scene_doc = body["scene_doc"]
            settings = {**DEFAULT_SETTINGS, **(body.get("settings") or {})}
            settings = {k: settings[k] for k in sorted(settings)}
            sub_id = body.get("submission_id") or f"sub-{uuid.uuid4().hex[:10]}"
            try:
                result = store.create_submission(
                    sub_id, scene_doc, settings,
                    int(body["frame_start"]), int(body["frame_end"]),
                )
            except ValueError as exc:
                return self._error(400, str(exc))
            result["settings"] = settings
            self._json(201, result)

        def _claim(self) -> None:
            body = self._read_json()
            task = store.claim_task(body["worker_id"], float(body.get("lease_seconds", 10)))
            if task is None:
                return self._json(200, {"task": None})
            task["scene_doc"] = store.get_scene_doc(task["scene_id"], task["version"])
            self._json(200, {"task": task})

        def _heartbeat(self) -> None:
            body = self._read_json()
            try:
                expires = store.heartbeat(body["attempt_id"], body["worker_id"],
                                          float(body.get("extend_seconds", 10)))
            except StaleLease:
                return self._error(409, "lease lost or superseded")
            self._json(200, {"lease_expires": expires})

        def _succeed(self, path: str) -> None:
            attempt_id = path.split("/")[-2]
            worker_id = self.headers.get("X-Worker-Id", "")
            length = int(self.headers.get("Content-Length", 0))
            blob = self.rfile.read(length) if length else b""
            try:
                outcome = store.succeed_attempt(attempt_id, worker_id, blob)
            except InvalidImage as exc:
                return self._error(422, str(exc))
            if outcome == "stale":
                return self._error(409, "attempt superseded; result rejected")
            if outcome == "invalid":
                return self._error(422, "image failed validation")
            self._json(200, {"ok": True, "outcome": outcome})

        def _fail(self, path: str) -> None:
            attempt_id = path.split("/")[-2]
            body = self._read_json()
            outcome = store.fail_attempt(attempt_id, body["worker_id"],
                                         str(body.get("error", "unknown")))
            if outcome == "stale":
                return self._error(409, "attempt superseded; failure ignored")
            self._json(200, {"ok": True})

        # --------------------------------------------------------------- POST cancel
        def do_DELETE(self) -> None:  # noqa: N802
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            parts = path.split("/")
            if (len(parts) == 5 and parts[1] == "api"
                    and parts[2] == "submissions" and parts[4] == "cancel"):
                sub_id = parts[3]
                try:
                    sub = store.cancel_submission(sub_id)
                except KeyError:
                    return self._error(404, "unknown submission")
                return self._json(200, sub)
            self._error(404, "not found")

    return Handler


_PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>动画帧渲染农场 · 调度面板</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font:14px/1.5 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#12151c; color:#d7dce5; }
  header { padding:14px 20px; background:#1a2030; border-bottom:1px solid #2a3346;
           display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  main { display:grid; grid-template-columns:380px 1fr; gap:16px; padding:16px 20px; }
  .card { background:#181d29; border:1px solid #283044; border-radius:10px; padding:14px; margin-bottom:14px; }
  .card h2 { font-size:14px; margin:0 0 10px; color:#9fc1ff; }
  label { display:block; font-size:12px; color:#9aa4b5; margin:8px 0 3px; }
  input,textarea,select { width:100%; box-sizing:border-box; background:#0f1320;
         color:#e6ebf4; border:1px solid #2c3650; border-radius:6px; padding:7px 9px; }
  textarea { min-height:150px; font-family:ui-monospace,monospace; font-size:12px; }
  button { background:#2f6bff; color:#fff; border:0; border-radius:6px; padding:8px 14px;
           cursor:pointer; margin-top:10px; font-weight:600; }
  button.ghost { background:#2a3347; }
  button.danger { background:#a23a3a; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .row { display:flex; gap:8px; }
  .row > * { flex:1; }
  .feedback { margin-top:8px; font-size:12px; padding:7px 9px; border-radius:6px;
              white-space:pre-wrap; display:none; }
  .feedback.err { display:block; background:#3a1d22; color:#ffb4b4; border:1px solid #6e2f38; }
  .feedback.ok { display:block; background:#16301f; color:#a8e6bd; border:1px solid #2c5e3c; }
  .sub { border:1px solid #283044; border-radius:8px; padding:9px 11px; margin-bottom:8px;
         background:#141925; font-size:12px; }
  .sub .id { font-family:ui-monospace,monospace; color:#8fd0ff; }
  .pill { display:inline-block; padding:1px 8px; border-radius:99px; font-size:11px; margin-left:6px; }
  .pill.open { background:#24406e; } .pill.cancelled { background:#5c4423; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; }
  .tile { background:#0f1320; border:1px solid #262f45; border-radius:8px; overflow:hidden; }
  .tile img { width:100%; display:block; image-rendering:auto; }
  .tile .meta { padding:6px 8px; font-size:11px; color:#9aa4b5; }
  .tile .fnum { font-weight:700; color:#e6ebf4; }
  .badge { float:right; padding:1px 7px; border-radius:99px; font-size:10px; }
  .b-succeeded { background:#1f5e38; color:#bff0d0; }
  .b-pending,.b-leased,.b-retrying { background:#34466a; color:#cfe0ff; }
  .b-failed { background:#6e2f38; color:#ffc3c3; }
  .b-cancelled { background:#5c4423; color:#ffe3b0; }
  .mono { font-family:ui-monospace,monospace; font-size:11px; word-break:break-all; }
  .strip { display:flex; gap:4px; overflow-x:auto; padding-bottom:8px; }
  .strip .cell { flex:0 0 auto; width:128px; }
  .strip img { width:128px; border:1px solid #262f45; border-radius:6px; }
  .events { max-height:220px; overflow:auto; font-family:ui-monospace,monospace; font-size:11px;
            color:#8fa0ba; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  td,th { text-align:left; padding:4px 6px; border-bottom:1px solid #232c40; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; }
  .dot.on { background:#43d17a; } .dot.off { background:#6b7280; }
</style>
</head>
<body>
<header>
  <h1>动画帧渲染农场 · 调度面板</h1>
  <span id="health" class="mono"></span>
  <span style="flex:1"></span>
  <button class="ghost" id="btnPlay">▶ 顺序预览（真实帧轮播）</button>
</header>
<main>
<section>
  <div class="card">
    <h2>1 · 提交出图任务</h2>
    <button class="ghost" id="btnSample" type="button">载入示例运动场景</button>
    <label>场景文档（scene_doc，改动后内容哈希即新版本）</label>
    <textarea id="sceneDoc"></textarea>
    <div class="row">
      <div><label>起始帧</label><input id="fStart" type="number" value="0"></div>
      <div><label>结束帧</label><input id="fEnd" type="number" value="11"></div>
    </div>
    <div class="row">
      <div><label>宽</label><input id="setW" type="number" value="320"></div>
      <div><label>高</label><input id="setH" type="number" value="180"></div>
      <div><label>SSAA</label><input id="setSS" type="number" value="2"></div>
    </div>
    <button id="btnSubmit">提交帧段</button>
    <div class="feedback" id="fb"></div>
  </div>
  <div class="card">
    <h2>2 · 任务（可取消）</h2>
    <div id="subs"></div>
  </div>
  <div class="card">
    <h2>工作进程</h2>
    <table id="workers"><thead><tr><th>节点</th><th>状态</th><th>完成</th><th>失败</th></tr></thead><tbody></tbody></table>
  </div>
  <div class="card">
    <h2>调度事件</h2>
    <div class="events" id="events"></div>
  </div>
</section>
<section>
  <div class="card">
    <h2>3 · 时间顺序预览（未完成帧明确标记，不跳过）</h2>
    <div class="strip" id="strip"></div>
  </div>
  <div class="card">
    <h2>4 · 帧缩略图与结果归属（来源 worker / 尝试 / 版本 / 设置哈希）</h2>
    <div class="grid" id="grid"></div>
  </div>
</section>
</main>
<script>
const $ = id => document.getElementById(id);
let currentSub = null, playTimer = null;

function fb(msg, ok) { const el = $('fb'); el.textContent = msg; el.className = 'feedback ' + (ok ? 'ok' : 'err'); }
function clearFb() { $('fb').className = 'feedback'; }

$('btnSample').onclick = async () => {
  const doc = await fetch('/api/sample-scene').then(r => r.json());
  $('sceneDoc').value = JSON.stringify(doc, null, 2);
  $('fEnd').value = doc.total_frames - 1;
};

$('btnSubmit').onclick = async () => {
  clearFb();
  let scene_doc;
  try { scene_doc = JSON.parse($('sceneDoc').value); }
  catch (e) { return fb('场景文档不是合法 JSON：' + e, false); }
  const body = {
    scene_doc,
    frame_start: parseInt($('fStart').value, 10),
    frame_end: parseInt($('fEnd').value, 10),
    settings: { width: +$('setW').value, height: +$('setH').value,
                ssaa: +$('setSS').value, format: 'png' }
  };
  const r = await fetch('/api/submissions', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await r.json();
  if (!r.ok) return fb('提交被调度内核拒绝：' + (data.error || r.status), false);
  fb('已受理 ' + data.submission_id + '\\n场景版本 ' + data.version +
     '\\n设置哈希 ' + data.settings_hash +
     '\\n新建任务 ' + data.created_frames.length + ' 个，命中既有帧 ' +
     data.reused_frames.length + ' 个', true);
  await refresh();
};

async function cancelSub(id, btn) {
  btn.disabled = true;
  const r = await fetch('/api/submissions/' + id + '/cancel', {method: 'DELETE'});
  if (!r.ok) { fb('取消失败：' + (await r.json()).error, false); btn.disabled = false; }
  await refresh();
}

function statusBadge(s) { return '<span class="badge b-' + s + '">' + s + '</span>'; }

async function refresh() {
  const subs = await fetch('/api/submissions').then(r => r.json());
  const box = $('subs'); box.innerHTML = '';
  if (subs.length && !currentSub) currentSub = subs[0].submission_id;
  for (const s of subs) {
    const div = document.createElement('div');
    div.className = 'sub';
    div.innerHTML = '<span class="id">' + s.submission_id + '</span>' +
      '<span class="pill ' + s.status + '">' + s.status + '</span>' +
      '<div>场景 ' + s.scene_id + ' · 版本 <span class="mono">' + s.version + '</span></div>' +
      '<div>帧 ' + s.frame_start + '–' + s.frame_end + ' · 设置 ' +
      '<span class="mono">' + s.settings_hash + '</span></div>';
    const btns = document.createElement('div');
    const open = document.createElement('button');
    open.textContent = '查看结果'; open.className = 'ghost';
    open.onclick = () => { currentSub = s.submission_id; loadDetail(); };
    div.appendChild(open);
    if (s.status === 'open') {
      const c = document.createElement('button');
      c.textContent = '取消任务'; c.className = 'danger';
      c.onclick = e => cancelSub(s.submission_id, e.target);
      div.appendChild(c);
    }
    box.appendChild(div);
  }
  const ws = await fetch('/api/workers').then(r => r.json());
  $('workers').querySelector('tbody').innerHTML = ws.map(w =>
    '<tr><td>' + w.name + '<div class="mono">' + w.worker_id.slice(0,10) + '</div></td>' +
    '<td><span class="dot ' + (w.online ? 'on' : 'off') + '"></span>' +
    (w.online ? '在线' : '失联') + '</td><td>' + w.completed + '</td><td>' + w.failed + '</td></tr>'
  ).join('') || '<tr><td colspan=4>暂无工作进程</td></tr>';
  const evs = await fetch('/api/events?limit=20').then(r => r.json());
  $('events').innerHTML = evs.map(e =>
    '<div>' + new Date(e.ts * 1000).toLocaleTimeString() + ' ' + e.kind +
    ' ' + (e.detail ? JSON.stringify(e.detail) : '') + '</div>').join('');
  await loadDetail();
}

async function loadDetail() {
  if (!currentSub) { $('grid').innerHTML = '<div>暂无提交</div>'; $('strip').innerHTML = ''; return; }
  const r = await fetch('/api/submissions/' + currentSub);
  if (!r.ok) { currentSub = null; return; }
  const { submission, summary, tasks } = await r.json();
  const sumText = Object.entries(summary).map(([k,v]) => k + ':' + v).join('  ');
  const bust = '?t=' + Date.now();
  $('grid').innerHTML = tasks.map(t => {
    const p = t.provenance || {};
    const src = '/api/submissions/' + currentSub + '/frames/' + t.frame + bust;
    return '<div class="tile"><img loading="lazy" src="' + src + '">' +
      '<div class="meta"><span class="fnum">帧 ' + String(t.frame).padStart(3,'0') +
      '</span>' + statusBadge(t.status) +
      '<div>尝试次数 ' + t.attempt_count + (t.last_error ? ' · 最近错误 ' + t.last_error : '') + '</div>' +
      '<div>来源 worker：' + (p.completed_by ? '<span class="mono">' + p.completed_by + '</span>' : '—') + '</div>' +
      '<div>正式尝试：<span class="mono">' + (p.completed_attempt ? p.completed_attempt.slice(0,12) : '—') + '</span></div>' +
      '<div>版本 <span class="mono">' + t.version + '</span> · 设置 <span class="mono">' +
      t.settings_hash + '</span></div>' +
      (t.blob_sha ? '<div>blob <span class="mono">' + t.blob_sha.slice(0,16) + '…</span></div>' : '') +
      '</div></div>';
  }).join('');
  $('strip').innerHTML = tasks.map(t =>
    '<div class="cell"><img src="/api/submissions/' + currentSub + '/frames/' + t.frame + bust +
    '" title="frame ' + t.frame + '"><div class="meta">帧 ' + t.frame + ' ' + statusBadge(t.status) + '</div></div>'
  ).join('');
  document.title = '渲染农场 · ' + currentSub + '（' + sumText + '）';
}

$('btnPlay').onclick = () => {
  if (playTimer) { clearInterval(playTimer); playTimer = null; $('btnPlay').textContent = '▶ 顺序预览（真实帧轮播）'; return; }
  const imgs = [...$('strip').querySelectorAll('img')];
  if (!imgs.length) return fb('暂无可预览的提交', false);
  let i = 0;
  const show = () => { imgs.forEach((im, k) => im.style.outline = k === i ? '3px solid #2f6bff' : 'none');
                       imgs[i].scrollIntoView({inline:'center', block:'nearest'}); i = (i + 1) % imgs.length; };
  show(); playTimer = setInterval(show, 500);
  $('btnPlay').textContent = '■ 停止预览';
};

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


def serve(host: str, port: int, data_dir: str) -> None:
    store = FarmStore(data_dir)
    reaped = store.reap_expired()
    if reaped:
        print(f"[scheduler] restart recovery: reaped {reaped} stale leases", flush=True)
    _Reaper(store).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"[scheduler] panel:  http://{host}:{port}/", flush=True)
    print(f"[scheduler] data:   {os.path.abspath(data_dir)}", flush=True)
    print("[scheduler] start a worker with:", flush=True)
    print(f"    python3 -m renderfarm.worker --server http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the render farm scheduler and panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default="./farm_data")
    args = parser.parse_args()
    serve(args.host, args.port, args.data_dir)


if __name__ == "__main__":
    _main()
