#!/usr/bin/env python3
"""cdp_slider_capture — CDP 自动滑块 + VerifyCaptchaV3 协议捕获。

链路：headless Edge（CDP 9333）加载 CN 验证页
  → Page.addScriptToEvaluateOnNewDocument 注入 CryptoJS/加密原语钩子（抓加密前明文）
  → Network 域全量录包（VerifyCaptchaV3 等阿里云验证端点的完整请求体）
  → 截图 → 像素梯度边缘对检测缺口 → Input.dispatchMouseEvent 合成拟人轨迹
  → 成功后 captchaVerifyParam 落盘 + 全部捕获物写 output/cdp/

用法：python cdp_slider_capture.py [--headed] [--keep]
"""
import argparse
import base64
import io
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP_PORT = 9333
URL = ("https://static.chagee.com/cdn-tools-transfer/chagee-cn-app/"
       "user/verify.html?sceneId=9nsud17h&language=zh")
OUT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "output", "cdp"))

# 新文档注入：包住常见加密入口，把明文/密文经 console.log 上报（Runtime.consoleAPICalled 收）
HOOK_JS = r"""
(function () {
  function rpt(tag, obj) {
    try { console.log('[ENC]' + tag + '|' + JSON.stringify(obj).substring(0, 4000)); } catch (e) {}
  }
  function wrapCryptoJS(C) {
    try {
      ['AES', 'DES', 'TripleDES', 'RC4', 'Rabbit'].forEach(function (algo) {
        if (C[algo] && C[algo].encrypt) {
          var orig = C[algo].encrypt;
          C[algo].encrypt = function (msg, key, cfg) {
            rpt(algo, { op: 'encrypt', msg: String(msg), key: String(key).substring(0, 200) });
            var out = orig.apply(this, arguments);
            try { rpt(algo, { op: 'result', out: out.toString ? out.toString() : String(out) }); } catch (e) {}
            return out;
          };
        }
      });
      if (C.MD5) { var m = C.MD5; C.MD5 = function (s) { var r = m.apply(this, arguments); rpt('MD5', { msg: String(s) }); return r; }; }
      if (C.SHA256) { var h = C.SHA256; C.SHA256 = function (s) { var r = h.apply(this, arguments); rpt('SHA256', { msg: String(s) }); return r; }; }
      if (C.HmacSHA256) { var g = C.HmacSHA256; C.HmacSHA256 = function (m1, k) { rpt('HmacSHA256', { msg: String(m1), key: String(k).substring(0, 100) }); return g.apply(this, arguments); }; }
      rpt('cryptojs_wrapped', { ok: true });
    } catch (e) {}
  }
  // CryptoJS 挂 window 时立即包裹
  var _cj;
  Object.defineProperty(window, 'CryptoJS', {
    configurable: true,
    get: function () { return _cj; },
    set: function (v) { _cj = v; wrapCryptoJS(v); }
  });
  // JSEncrypt（RSA）
  var _js;
  try {
    Object.defineProperty(window, 'JSEncrypt', {
      configurable: true,
      get: function () { return _js; },
      set: function (v) {
        _js = v;
        try {
          var proto = v && v.prototype;
          if (proto && proto.encrypt) {
            var oe = proto.encrypt;
            proto.encrypt = function (msg) {
              var out = oe.apply(this, arguments);
              rpt('JSEncrypt', { op: 'encrypt', msg: String(msg).substring(0, 600), pub: (this.getPublicKey && String(this.getPublicKey()).substring(0, 120)) || '' });
              return out;
            };
            rpt('jsencrypt_wrapped', { ok: true });
          }
        } catch (e) {}
      }
    });
  } catch (e) {}
  // 原生 crypto.subtle
  try {
    var enc = window.crypto && window.crypto.subtle;
    if (enc && enc.encrypt) {
      var se = enc.encrypt.bind(enc);
      enc.encrypt = function (alg, key, data) {
        try {
          data.arrayBuffer().then(function (ab) {
            rpt('subtle.encrypt', { alg: JSON.stringify(alg), data: btoa(String.fromCharCode.apply(null, new Uint8Array(ab).slice(0, 300))) });
          });
        } catch (e) {}
        return se(alg, key, data);
      };
    }
  } catch (e) {}
  // token 捕获（页面 success 原生 console 已有，这里再加固一层）
  window.__cdp_token = null;
  var _cap = window.ChageeCall;
  window.ChageeCall = { postMessage: function (m) { window.__cdp_token = m; try { console.log('[CDPTOKEN]' + m); } catch (e) {} } };
})();
"""


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.id = 0
        self.events = []          # 原始事件流
        self.net_posts = {}       # requestId -> {url, postData}
        self.console = []         # console 消息
        self.token = None

    def send(self, method, **params):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.id:
                return msg.get("result", {})
            self._dispatch(msg)

    def recv_until(self, pred, timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self.ws.settimeout(max(0.5, timeout - (time.time() - t0)))
                msg = json.loads(self.ws.recv())
            except Exception:
                break
            if self._dispatch(msg) and pred(self):
                return True
        return pred(self)

    def _dispatch(self, msg):
        m, p = msg.get("method"), msg.get("params", {})
        if not m:
            return False
        self.events.append(msg)
        if m == "Network.requestWillBeSent":
            r = p.get("request", {})
            if r.get("method") == "POST" and r.get("postData") is not None:
                self.net_posts[p.get("requestId")] = {
                    "url": r.get("url"), "postData": r.get("postData", "")[:8000],
                    "ts": p.get("timestamp")}
        elif m == "Runtime.consoleAPICalled":
            text = "".join(str(a.get("value", "")) for a in p.get("args", []))
            self.console.append({"type": p.get("type"), "text": text})
            if text.startswith("[CDPTOKEN]"):
                self.token = text[len("[CDPTOKEN]"):]
            if text.startswith("success captchaVerifyParam"):
                self.token = text.split("success captchaVerifyParam:", 1)[1].strip()
        return True

    def evaluate(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")

    def screenshot(self):
        r = self.send("Page.captureScreenshot", format="png")
        return base64.b64decode(r["data"])

    def mouse(self, typ, x, y, **kw):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                  button="left", pointerType="mouse", **kw)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def find_gap(img):
    """列梯度边缘对检测缺口左缘（PIL）。"""
    from PIL import Image
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    grads = []
    for x in range(62, w - 2):
        s = 0
        for y in range(0, h, 2):
            s += abs(px[x + 1, y] - px[x - 1, y])
        grads.append((x, s))
    cand = sorted(grads, key=lambda t: -t[1])[:20]
    cand.sort()
    best = None
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            d = cand[j][0] - cand[i][0]
            if 42 <= d <= 60:
                score = cand[i][1] + cand[j][1]
                if best is None or score > best[0]:
                    best = (score, cand[i][0], cand[j][0])
    return best


def human_drag(cdp, x0, y0, dist, total_ms=1300):
    """Input.dispatchMouseEvent 合成拟人轨迹（ease-out + 抖动 + 中途微停）。"""
    cdp.mouse("mousePressed", x0, y0, clicks=1)
    steps = 28
    for i in range(1, steps + 1):
        t = i / steps
        ease = 1 - (1 - t) ** 2.2
        jitter = random.choice([0, 0, 0, 1, -1])
        pause = 0.045 if i in (int(steps * 0.55),) else 0.028
        x = x0 + dist * ease
        cdp.mouse("mouseMoved", x, y0 + jitter)
        time.sleep(pause)
    cdp.mouse("mouseMoved", x0 + dist, y0)
    time.sleep(0.12)
    cdp.mouse("mouseReleased", x0 + dist, y0, clicks=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--keep", action="store_true", help="结束后保留浏览器")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    flags = [EDGE, f"--remote-debugging-port={CDP_PORT}", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu",
             "--remote-allow-origins=*",
             "--user-data-dir=" + os.path.join(OUT_DIR, "edge_profile"),
             "--window-size=460,760"]
    if not args.headed:
        flags.append("--headless=new")
    proc = subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 取页面 ws
    for _ in range(30):
        try:
            tabs = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2))
            page = next((t for t in tabs if t.get("type") == "page"), None)
            if page:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        proc.kill()
        sys.exit("edge cdp 不可达")
    print("[cdp] edge up, page:", page["url"][:60], flush=True)

    cdp = CDP(page["webSocketDebuggerUrl"].replace("127.0.0.1", "localhost"))
    cdp.send("Network.enable")
    cdp.send("Runtime.enable")
    cdp.send("Page.enable")
    cdp.send("Page.addScriptToEvaluateOnNewDocument", source=HOOK_JS)

    cdp.send("Page.navigate", url=URL)
    time.sleep(6)

    # 轮询拼图出现（aliyunCaptcha-img 就位）
    geo = None
    for _ in range(20):
        geo = cdp.evaluate("""(function(){
          var bg=document.getElementById('aliyunCaptcha-img');
          var pz=document.getElementById('aliyunCaptcha-puzzle');
          var kn=document.querySelector("[id*='sliding-slider'],[class*='sliding-slider']");
          if(!bg||!bg.src||!kn) return null;
          var r=bg.getBoundingClientRect(), k=kn.getBoundingClientRect();
          return JSON.stringify({bgX:r.x,bgY:r.y,bgW:r.width,natW:bg.naturalWidth,
                 kx:k.x+k.width/2,ky:k.y+k.height/2,pageX:window.scrollX,pageY:window.scrollY});
        })()""")
        if geo:
            break
        time.sleep(1)
    if not geo:
        dump(cdp, proc, args)
        sys.exit("拼图未渲染（截图已存 output/cdp/）")
    g = json.loads(geo)
    print("[cdp] puzzle geo:", g, flush=True)

    # 截图 → CV 缺口
    png = cdp.screenshot()
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    img.save(os.path.join(OUT_DIR, "before.png"))
    bg_crop = img.crop((int(g["bgX"]), int(g["bgY"]),
                        int(g["bgX"] + g["bgW"]), int(g["bgY"]) + g["bgW"] * 2 // 3))
    pair = find_gap(bg_crop.resize((296, 200)))
    if not pair:
        dump(cdp, proc, args)
        sys.exit("缺口检测失败")
    gap_nat = pair[1]
    scale = g["bgW"] / g["natW"]
    target_px = g["bgX"] + gap_nat * scale
    print(f"[cv] gap nat={gap_nat} -> screen x={target_px:.0f}", flush=True)

    # 比例探测：knob→piece ≈0.677；拖动距离 = (target - pz左缘)/ratio，先按 0.677 发，二段校正
    pz_x = cdp.evaluate("(document.getElementById('aliyunCaptcha-puzzle').getBoundingClientRect().x)")
    need = (target_px - pz_x) / 0.677
    print(f"[drag] knob dx={need:.0f}", flush=True)
    human_drag(cdp, g["kx"], g["ky"], need)
    time.sleep(2.5)

    # 校正循环：检查 piece 位置，差 >4px 补拖
    for _ in range(3):
        px_now = cdp.evaluate("(document.getElementById('aliyunCaptcha-puzzle')||{getBoundingClientRect:function(){return{x:-1}}}).getBoundingClientRect().x")
        if px_now is None or px_now < 0:
            break
        diff = target_px - px_now
        if abs(diff) <= 4 or cdp.token:
            break
        kx_now = cdp.evaluate("(document.querySelector(\"[id*='sliding-slider'],[class*='sliding-slider']\")||{getBoundingClientRect:function(){return{x:-999}}}).getBoundingClientRect().x")
        if kx_now < -100:
            break
        print(f"[fix] piece@{px_now:.0f} diff={diff:.0f} 补拖", flush=True)
        human_drag(cdp, kx_now + 20, g["ky"], diff / 0.677)
        time.sleep(2)

    cdp.recv_until(lambda c: c.token is not None, timeout=8)
    dump(cdp, proc, args)


def dump(cdp, proc, args):
    os.makedirs(OUT_DIR, exist_ok=True)
    # token
    if cdp.token:
        raw = cdp.token
        try:
            tok = json.loads(raw).get("param", raw)
        except Exception:
            tok = raw
        with open(os.path.join(OUT_DIR, "token.txt"), "w") as f:
            f.write(tok)
        print("!!! TOKEN:", tok[:80], flush=True)
    # VerifyCaptchaV3 等网络捕获
    with open(os.path.join(OUT_DIR, "net_posts.json"), "w", encoding="utf-8") as f:
        json.dump(list(cdp.net_posts.values()), f, ensure_ascii=False, indent=1)
    # 加密明文日志
    with open(os.path.join(OUT_DIR, "enc_console.log"), "w", encoding="utf-8") as f:
        for c in cdp.console:
            f.write(f"{c['type']} {c['text']}\n")
    try:
        cdp.screenshot_saved = True
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(cdp.screenshot()))
        img.save(os.path.join(OUT_DIR, "after.png"))
    except Exception:
        pass
    print(f"[done] 产物在 {OUT_DIR}（net_posts.json / enc_console.log / token.txt / after.png）", flush=True)
    cdp.close()
    if not args.keep:
        proc.kill()


if __name__ == "__main__":
    main()
