#!/usr/bin/env python3
"""serve_mitm — 手机侧 static.chagee.com MITM 服务器。

链路：Chrome → static.chagee.com:443（iptables REDIRECT→127.0.0.1:8443）
      → adb reverse → PC:8443（本服务，leaf 证书）
职责：
  - 提供改造版 verify.html（注入缺口分析/遥测/token上报）
  - 接收 /report（分析数据）、/token（验证令牌）落盘 output/mitm/
"""
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ADB = r"C:\platform-tools\adb.exe"
DEV = "39.174.221.6:58445"

_original_html = None
_reports = []
TOKEN_FILE = os.path.join(HERE, "minted_token.txt")
ANALYSIS_FILE = os.path.join(HERE, "last_analysis.json")


def fetch_original() -> str:
    """从 PC 直接拉原始 verify.html（不经手机）。"""
    global _original_html
    if _original_html is None:
        import urllib.request
        url = ("https://static.chagee.com/cdn-tools-transfer/chagee-cn-app/"
               "user/verify.html?sceneId=9nsud17h&language=zh")
        with urllib.request.urlopen(url, timeout=15) as r:
            _original_html = r.read().decode("utf-8")
    return _original_html


INJECT_JS = """
<script>
(function(){
  document.title = 'MITM-OK';
  function report(kind, extra){
    try{
      fetch('/report', {method:'POST', body: JSON.stringify(Object.assign({kind:kind, t:Date.now()}, extra||{}))});
    }catch(e){}
  }
  window.__token = null;
  window.ChageeCall = { postMessage: function(m){ window.__token = m; report('token', {msg: m}); } };
  function analyze(){
    var bg = document.getElementById('aliyunCaptcha-img');
    var pz = document.getElementById('aliyunCaptcha-puzzle');
    if(!bg || !pz || !bg.src || !bg.naturalWidth) return null;
    var c = document.createElement('canvas'); c.width=bg.naturalWidth; c.height=bg.naturalHeight;
    var ctx = c.getContext('2d'); ctx.drawImage(bg,0,0);
    var d = ctx.getImageData(0,0,c.width,c.height).data;
    var w=c.width,h=c.height, grads=[];
    for(var x=62;x<w-2;x++){
      var s=0;
      for(var y=0;y<h;y+=2){
        var gx = Math.abs(d[(y*w+x+1)*4] - d[(y*w+x-1)*4]);
        s += gx;
      }
      grads.push([x,s]);
    }
    var cand = grads.slice().sort(function(a,b){return b[1]-a[1];}).slice(0,20).sort(function(a,b){return a[0]-b[0];});
    var best=null;
    for(var i=0;i<cand.length;i++) for(var j=i+1;j<cand.length;j++){
      var dd=cand[j][0]-cand[i][0];
      if(dd>=42&&dd<=60){ var sc=cand[i][1]+cand[j][1]; if(!best||sc>best[0]) best=[sc,cand[i][0],cand[j][0]]; }
    }
    var r=bg.getBoundingClientRect(), pr=pz.getBoundingClientRect();
    var knob=document.querySelector("[id*='sliding-slider'],[class*='sliding-slider']");
    var kr = knob? knob.getBoundingClientRect() : null;
    return { ok: !!best, gapNat: best?best[1]:null,
             bgX:r.x, bgW:r.width, natW:w, pzX:pr.x, pzW:pr.width,
             knobX: kr?kr.x:null, knobY: kr?(kr.y+kr.height/2):null, knobW: kr?kr.width:null,
             trackW: (document.querySelector("[id*='sliding-body'],[class*='sliding-body']")||{getBoundingClientRect:function(){return{width:0}}}).getBoundingClientRect().width };
  }
  window.__probe = function(){ var a = analyze(); if(a) report('analyze', a); return a; };
  var tries = 0;
  var iv = setInterval(function(){ tries++; if(window.__token){ clearInterval(iv); return; } window.__probe(); if(tries>40) clearInterval(iv); }, 1200);
  // 拖动期间 piece 位置遥测（比例标定）
  var lastPz = null, lastT = 0;
  setInterval(function(){
    var pz = document.getElementById('aliyunCaptcha-puzzle');
    if(!pz || !pz.src) return;
    var x = pz.getBoundingClientRect().x;
    var now = Date.now();
    if(lastPz !== null && Math.abs(x-lastPz) > 0.5 && now-lastT > 90){
      report('pzmove', {x: x});
      lastT = now;
    }
    if(Math.abs(x-lastPz) < 0.5) lastT = now;
    lastPz = x;
  }, 90);
})();
</script>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "verify.html" in self.path:
            html = fetch_original()
            if "</body>" in html:
                html = html.replace("</body>", INJECT_JS + "</body>")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            print(f"[{time.strftime('%H:%M:%S')}] served verify.html ({len(body)}B)", flush=True)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw[:500]}
        kind = data.get("kind")
        print(f"[{time.strftime('%H:%M:%S')}] report {kind}: {json.dumps(data, ensure_ascii=False)[:400]}", flush=True)
        _reports.append(data)
        if kind == "token":
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(raw)
            print("!!! TOKEN MINTED !!!", flush=True)
        elif kind == "analyze":
            with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        self._json({"ok": True})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
    httpd = ThreadingHTTPServer(("0.0.0.0", port), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(HERE, "leaf.crt"), os.path.join(HERE, "leaf.key"))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"mitm tls server on :{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
