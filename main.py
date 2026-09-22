import os, time, threading, json, base64, hmac, hashlib, math
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask, jsonify, render_template_string
import requests

app=Flask(__name__)
lock=threading.Lock()
OKX_BASE="https://www.okx.com"
INST_ID=os.getenv("INST_ID","OP-USDT")
STEP=float(os.getenv("GRID_STEP","0.01"))
TRADE_USD=float(os.getenv("TRADE_USD","2"))
POLL_SECONDS=int(os.getenv("POLL_SECONDS","5"))
LIVE_ENABLED=os.getenv("LIVE_TRADING_ENABLED","false").lower()=="true"
API_KEY=os.getenv("OKX_API_KEY","")
SECRET_KEY=os.getenv("OKX_SECRET_KEY","")
PASSPHRASE=os.getenv("OKX_PASSPHRASE","")

state={"price":None,"anchor":None,"account_op":0.0,"account_usdt":0.0,"trades":0,"buys":0,"sells":0,
"last_trade":None,"started":None,"error":None,"api_ok":False,"mode":"OKX LIVE SPOT","armed":LIVE_ENABLED}

def iso_ts():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")

def okx_request(method,path,params=None,body=None,auth=False):
    params=params or {}; body=body or {}
    query=urlencode(params); request_path=path+(("?"+query) if query else "")
    payload=json.dumps(body,separators=(",",":")) if method!="GET" and body else ""
    headers={"Content-Type":"application/json"}
    if auth:
        if not (API_KEY and SECRET_KEY and PASSPHRASE): raise RuntimeError("Missing OKX LIVE API variables")
        ts=iso_ts(); prehash=ts+method+request_path+payload
        sign=base64.b64encode(hmac.new(SECRET_KEY.encode(),prehash.encode(),hashlib.sha256).digest()).decode()
        headers.update({"OK-ACCESS-KEY":API_KEY,"OK-ACCESS-SIGN":sign,"OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":PASSPHRASE})
    r=requests.request(method,OKX_BASE+request_path,headers=headers,data=payload or None,timeout=10)
    r.raise_for_status(); data=r.json()
    if str(data.get("code","0"))!="0": raise RuntimeError(f'OKX {data.get("code")}: {data.get("msg")}')
    return data

def instrument_rules():
    x=okx_request("GET","/api/v5/public/instruments",{"instType":"SPOT","instId":INST_ID})["data"][0]
    return float(x["minSz"]),float(x["lotSz"])

def market_price():
    return float(okx_request("GET","/api/v5/market/ticker",{"instId":INST_ID})["data"][0]["last"])

def balances():
    d=okx_request("GET","/api/v5/account/balance",{"ccy":"OP,USDT"},auth=True)
    op=usdt=0.0
    for x in d["data"][0].get("details",[]):
        if x["ccy"]=="OP": op=float(x.get("availBal") or x.get("cashBal") or 0)
        if x["ccy"]=="USDT": usdt=float(x.get("availBal") or x.get("cashBal") or 0)
    return op,usdt

def refresh_balances():
    op,usdt=balances(); state["account_op"]=op; state["account_usdt"]=usdt; return op,usdt

def order_usd(px):
    min_sz,_=instrument_rules()
    min_usd=min_sz*px
    return TRADE_USD if min_usd < TRADE_USD else min_usd*1.01

def place_market(side,usd,px):
    if not LIVE_ENABLED: raise RuntimeError("LIVE trading safety lock is OFF")
    min_sz,lot_sz=instrument_rules()
    if side=="buy":
        body={"instId":INST_ID,"tdMode":"cash","side":"buy","ordType":"market","sz":f"{usd:.8f}","tgtCcy":"quote_ccy"}
    else:
        qty=max(usd/px,min_sz)
        if lot_sz>0: qty=math.ceil(qty/lot_sz)*lot_sz
        body={"instId":INST_ID,"tdMode":"cash","side":"sell","ordType":"market","sz":f"{qty:.8f}","tgtCcy":"base_ccy"}
    item=okx_request("POST","/api/v5/trade/order",body=body,auth=True)["data"][0]
    if item.get("sCode") not in (None,"","0"): raise RuntimeError(f'Order {item.get("sCode")}: {item.get("sMsg")}')
    return item.get("ordId")

def execute(side,level):
    op,usdt=refresh_balances(); usd=order_usd(level)
    if side=="BUY" and usdt<usd: raise RuntimeError("Insufficient LIVE USDT")
    if side=="SELL" and op*level<usd: raise RuntimeError("Insufficient LIVE OP")
    oid=place_market(side.lower(),usd,level)
    time.sleep(1); refresh_balances()
    state["trades"]+=1; state["buys"]+=side=="BUY"; state["sells"]+=side=="SELL"; state["anchor"]=level
    state["last_trade"]={"side":side,"trigger_price":level,"usd":usd,"ordId":oid,"time":datetime.now(timezone.utc).isoformat()}

def worker():
    while True:
        try:
            px=market_price()
            with lock:
                state["price"]=px; state["error"]=None
                if API_KEY and SECRET_KEY and PASSPHRASE:
                    refresh_balances(); state["api_ok"]=True
                else:
                    state["api_ok"]=False
                if state["anchor"] is None:
                    state["anchor"]=px; state["started"]=datetime.now(timezone.utc).isoformat()
                if LIVE_ENABLED and state["api_ok"]:
                    while px>=state["anchor"]*(1+STEP): execute("SELL",state["anchor"]*(1+STEP))
                    while px<=state["anchor"]*(1-STEP): execute("BUY",state["anchor"]*(1-STEP))
        except Exception as e:
            with lock: state["error"]=str(e)[:220]
            print("WORKER ERROR:",state["error"],flush=True)
        time.sleep(POLL_SECONDS)

def snapshot():
    with lock:
        s=dict(state)
        if s["anchor"]:
            s["upper"]=s["anchor"]*(1+STEP); s["lower"]=s["anchor"]*(1-STEP)
        return s

HTML="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OP Grid · OKX LIVE</title>
<style>body{margin:0;background:#080b12;color:#eaf0ff;font-family:system-ui,Arial}.wrap{max-width:900px;margin:auto;padding:20px}h1{font-size:22px}.muted{color:#8d98ad}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.card{background:#111724;border:1px solid #222d42;border-radius:12px;padding:14px}.v{font-size:22px;font-weight:700;margin-top:6px}.pos,.buy{color:#4ade80}.neg,.sell{color:#fb7185}small{color:#8d98ad}</style></head><body><div class="wrap">
<h1>OP GRID · OKX LIVE</h1><div class="muted">LIVE SPOT OP-USDT · adjustable grid · target $2/order · update 5s</div><div id="conn" class="card" style="margin-top:14px">OKX LIVE CONNECTION<div class="v">CHECKING...</div></div>
<div id="x" style="margin-top:14px">Loading...</div></div><script>
const n=(x,d=2)=>x==null?'N/A':Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
async function go(){try{let s=await(await fetch('/api',{cache:'no-store'})).json();let p=s.pnl||0,cl=p>=0?'pos':'neg';
document.getElementById('conn').innerHTML=`OKX LIVE CONNECTION<div class="v ${s.api_ok&&!s.error?'pos':'neg'}">${s.api_ok&&!s.error?'CONNECTED':'ERROR'}</div><small>${s.api_ok?'Account OP '+n(s.account_op,8)+' · USDT '+n(s.account_usdt,4):(s.error||'Waiting for API')}</small><br><small>Trading: <b class="${s.armed?'pos':'neg'}">${s.armed?'LIVE ENABLED':'SAFETY LOCKED'}</b></small>`;
document.getElementById('x').innerHTML=`<div class="grid">
<div class="card">OP PRICE<div class="v">$${n(s.price,6)}</div></div>
<div class="card">ANCHOR<div class="v">$${n(s.anchor,6)}</div><small>Buy ≤ $${n(s.lower,6)} · Sell ≥ $${n(s.upper,6)}</small></div>
<div class="card">GRID<div class="v">${n(s.grid_pct,2)}%</div><small>Target $${n(s.trade_target_usd,2)}/order</small></div>
<div class="card">ACCOUNT VALUE<div class="v ${cl}">$${n(s.total,4)}</div><small class="${cl}">${s.pnl==null?'P&L starts after API connects':(p>=0?'+':'')+'$'+n(p,4)+' ('+n(s.pnl_pct,3)+'%) since bot start'}</small></div>
<div class="card">OP AVAILABLE<div class="v">${n(s.account_op,8)}</div><small>≈ $${n(s.op_value,2)}</small></div>
<div class="card">USDT AVAILABLE<div class="v">${n(s.account_usdt,4)}</div></div>
<div class="card">ORDERS<div class="v">${s.trades}</div><small><span class="buy">BUY ${s.buys}</span> · <span class="sell">SELL ${s.sells}</span></small></div>
<div class="card">LAST ORDER<div class="v ${s.last_trade?.side==='BUY'?'buy':'sell'}">${s.last_trade?s.last_trade.side:'N/A'}</div><small>${s.last_trade?'~$'+n(s.last_trade.usd,2)+' · OKX '+s.last_trade.ordId:'Waiting for grid trigger'}</small></div>
<div class="card">STARTED<div class="v" style="font-size:14px">${s.started||'N/A'}</div><small>Mode: ${s.mode}</small></div>
</div><p class="muted">Status: ${s.error?'ERROR · '+s.error:(s.api_ok?'CONNECTED · OKX LIVE':'Waiting for API')}</p>`; }catch(e){}}
go();setInterval(go,5000);</script></body></html>"""

@app.get("/")
def home():
    return HTML

@app.get("/api")
def api():
    return jsonify(snapshot())

@app.get("/health")
def health():
    return jsonify({"ok":True,"mode":"OKX_LIVE","apiConfigured":bool(API_KEY and SECRET_KEY and PASSPHRASE),"liveTradingEnabled":LIVE_ENABLED})

if __name__=="__main__":
    threading.Thread(target=worker,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
