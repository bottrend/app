import os, time, threading, json, base64, hmac, hashlib
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask, jsonify, render_template_string
import requests

app=Flask(__name__)
lock=threading.Lock()

OKX_BASE="https://www.okx.com"
INST_ID="BTC-USDT"
STEP=0.005
TRADE_PCT=0.10
MIN_TRADE_USD=2.0
POLL_SECONDS=5

API_KEY=os.getenv("OKX_API_KEY","")
SECRET_KEY=os.getenv("OKX_SECRET_KEY","")
PASSPHRASE=os.getenv("OKX_PASSPHRASE","")

state={"price":None,"anchor":None,"btc":0.0,"usdt":0.0,"initial_total":None,
       "trades":0,"buys":0,"sells":0,"last_trade":None,"started":None,
       "error":None,"api_ok":False,"mode":"OKX DEMO SPOT"}

def iso_ts():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")

def okx_request(method,path,params=None,body=None,auth=False):
    params=params or {}
    body=body or {}
    query=urlencode(params)
    request_path=path + (("?"+query) if query else "")
    payload=json.dumps(body,separators=(",",":")) if method!="GET" and body else ""
    headers={"Content-Type":"application/json","x-simulated-trading":"1"}
    if auth:
        if not (API_KEY and SECRET_KEY and PASSPHRASE):
            raise RuntimeError("Missing OKX Demo API variables")
        ts=iso_ts()
        prehash=ts+method+request_path+payload
        sign=base64.b64encode(hmac.new(SECRET_KEY.encode(),prehash.encode(),hashlib.sha256).digest()).decode()
        headers.update({"OK-ACCESS-KEY":API_KEY,"OK-ACCESS-SIGN":sign,
                        "OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":PASSPHRASE})
    r=requests.request(method,OKX_BASE+request_path,headers=headers,data=payload or None,timeout=10)
    r.raise_for_status()
    data=r.json()
    if str(data.get("code","0"))!="0":
        raise RuntimeError(f'OKX {data.get("code")}: {data.get("msg")}')
    return data

def btc_price():
    d=okx_request("GET","/api/v5/market/ticker",{"instId":INST_ID})
    return float(d["data"][0]["last"])

def balances():
    d=okx_request("GET","/api/v5/account/balance",{"ccy":"BTC,USDT"},auth=True)
    btc=usdt=0.0
    for x in d["data"][0].get("details",[]):
        if x["ccy"]=="BTC": btc=float(x.get("availBal") or x.get("cashBal") or 0)
        if x["ccy"]=="USDT": usdt=float(x.get("availBal") or x.get("cashBal") or 0)
    return btc,usdt

def place_market(side,usd,px):
    if usd < MIN_TRADE_USD: return None
    if side=="buy":
        body={"instId":INST_ID,"tdMode":"cash","side":"buy","ordType":"market",
              "sz":f"{usd:.8f}","tgtCcy":"quote_ccy"}
    else:
        qty=usd/px
        body={"instId":INST_ID,"tdMode":"cash","side":"sell","ordType":"market",
              "sz":f"{qty:.8f}","tgtCcy":"base_ccy"}
    d=okx_request("POST","/api/v5/trade/order",body=body,auth=True)
    item=d["data"][0]
    if item.get("sCode") not in (None,"","0"):
        raise RuntimeError(f'Order {item.get("sCode")}: {item.get("sMsg")}')
    return item.get("ordId")

def refresh_balances():
    btc,usdt=balances()
    state["btc"]=btc; state["usdt"]=usdt
    return btc,usdt

def execute(side,level):
    btc,usdt=refresh_balances()
    trade_usd=(usdt*TRADE_PCT) if side=="BUY" else (btc*level*TRADE_PCT)
    if trade_usd < MIN_TRADE_USD:
        raise RuntimeError(f"{side} size below $2 minimum")
    ord_id=place_market(side.lower(),trade_usd,level)
    time.sleep(1)
    refresh_balances()
    state["trades"]+=1
    state["buys"]+= side=="BUY"
    state["sells"]+= side=="SELL"
    state["anchor"]=level
    state["last_trade"]={"side":side,"trigger_price":level,"usd":trade_usd,
                         "ordId":ord_id,"time":datetime.now(timezone.utc).isoformat()}

def init(px):
    btc,usdt=refresh_balances()
    state["price"]=px; state["anchor"]=px
    state["btc"]=btc; state["usdt"]=usdt
    state["initial_total"]=usdt+btc*px
    state["started"]=datetime.now(timezone.utc).isoformat()
    state["api_ok"]=True

def worker():
    while True:
        try:
            px=btc_price()
            with lock:
                state["error"]=None
                if state["anchor"] is None: init(px)
                state["price"]=px
                while px >= state["anchor"]*(1+STEP):
                    level=state["anchor"]*(1+STEP); execute("SELL",level)
                while px <= state["anchor"]*(1-STEP):
                    level=state["anchor"]*(1-STEP); execute("BUY",level)
                refresh_balances()
        except Exception as e:
            with lock:
                state["error"]=str(e)[:220]
                state["api_ok"]=False if state["anchor"] is None else state["api_ok"]
        time.sleep(POLL_SECONDS)

def snapshot():
    with lock:
        s=dict(state)
        px=s["price"]
        if px and s["anchor"]:
            total=s["usdt"]+s["btc"]*px
            initial=s["initial_total"]
            s.update({"btc_value":s["btc"]*px,"total":total,
                      "pnl":None if initial is None else total-initial,
                      "pnl_pct":None if not initial else (total/initial-1)*100,
                      "upper":s["anchor"]*(1+STEP),"lower":s["anchor"]*(1-STEP)})
        return s

@app.get("/api")
def api(): return jsonify(snapshot())

@app.get("/health")
def health(): return {"ok":True,"mode":"OKX_DEMO","apiConfigured":bool(API_KEY and SECRET_KEY and PASSPHRASE)}

HTML="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC Grid · OKX Demo</title><style>
body{margin:0;background:#080b12;color:#eaf0ff;font-family:system-ui,Arial}.wrap{max-width:900px;margin:auto;padding:20px}
h1{font-size:22px}.muted{color:#8d98ad}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.card{background:#111724;border:1px solid #222d42;border-radius:12px;padding:14px}.v{font-size:22px;font-weight:700;margin-top:6px}
.pos,.buy{color:#4ade80}.neg,.sell{color:#fb7185}small{color:#8d98ad}</style></head><body><div class="wrap">
<h1>₿ BTC 0.5% GRID · OKX DEMO</h1><div class="muted">DEMO SPOT BTC-USDT · 10% dynamic · grid 0.5% · market orders · update 5s</div><div id="conn" class="card" style="margin-top:14px">OKX DEMO CONNECTION<div class="v">CHECKING...</div><small>BTC / USDT balance will appear after authentication</small></div>
<div id="x" style="margin-top:14px">Loading...</div></div><script>
const n=(x,d=2)=>x==null?'N/A':Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
async function go(){try{let s=await(await fetch('/api',{cache:'no-store'})).json();let p=s.pnl||0,cl=p>=0?'pos':'neg';
document.getElementById('conn').innerHTML=`OKX DEMO CONNECTION<div class="v ${s.api_ok&&!s.error?'pos':'neg'}">${s.api_ok&&!s.error?'CONNECTED':'ERROR'}</div><small>${s.api_ok&&!s.error?'Authenticated · BTC '+n(s.btc,8)+' · USDT '+n(s.usdt,4):(s.error||'Connecting...')}</small>`;
document.getElementById('x').innerHTML=`<div class="grid">
<div class="card">BTC PRICE<div class="v">$${n(s.price)}</div></div>
<div class="card">ANCHOR<div class="v">$${n(s.anchor)}</div><small>Buy ≤ $${n(s.lower)} · Sell ≥ $${n(s.upper)}</small></div>
<div class="card">OKX DEMO VALUE<div class="v ${cl}">$${n(s.total,4)}</div><small class="${cl}">${p>=0?'+':''}$${n(p,4)} (${n(s.pnl_pct,3)}%) since bot start</small></div>
<div class="card">BTC AVAILABLE<div class="v">${n(s.btc,8)}</div><small>≈ $${n(s.btc_value,2)}</small></div>
<div class="card">USDT AVAILABLE<div class="v">$${n(s.usdt,4)}</div></div>
<div class="card">BOT ORDERS<div class="v">${s.trades}</div><small><span class="buy">BUY ${s.buys}</span> · <span class="sell">SELL ${s.sells}</span></small></div>
<div class="card">LAST ORDER<div class="v ${s.last_trade?.side==='BUY'?'buy':'sell'}">${s.last_trade?s.last_trade.side:'N/A'}</div><small>${s.last_trade?'~$'+n(s.last_trade.usd,2)+' · OKX '+s.last_trade.ordId:'Waiting for ±0.5%'}</small></div>
</div><p class="muted">Status: ${s.error?'ERROR · '+s.error:(s.api_ok?'CONNECTED · OKX DEMO ONLY':'Connecting...')}</p>`; }catch(e){}}
go();setInterval(go,5000);</script></body></html>"""

@app.get("/")
def home(): return render_template_string(HTML)

if __name__=="__main__":
    threading.Thread(target=worker,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
