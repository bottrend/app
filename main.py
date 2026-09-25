import os, time, threading, json, base64, hmac, hashlib, math, uuid
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask, jsonify, render_template_string
import requests

app=Flask(__name__)
lock=threading.Lock()
OKX_BASE="https://www.okx.com"
INST_ID=os.getenv("INST_ID","OP-USDT").strip().upper()
_pair=INST_ID.split("-")
if len(_pair)!=2 or not _pair[0] or not _pair[1]: raise RuntimeError("INST_ID must be a SPOT pair like OP-USDT")
BASE_CCY,QUOTE_CCY=_pair
STEP=float(os.getenv("GRID_STEP","0.01"))
TRADE_USD=float(os.getenv("TRADE_USD","2"))
POLL_SECONDS=int(os.getenv("POLL_SECONDS","5"))
MIN_ROUNDTRIP_MARGIN=float(os.getenv("MIN_ROUNDTRIP_MARGIN","0.0025"))
LIVE_ENABLED=os.getenv("LIVE_TRADING_ENABLED","false").lower()=="true"
API_KEY=os.getenv("OKX_API_KEY","")
SECRET_KEY=os.getenv("OKX_SECRET_KEY","")
PASSPHRASE=os.getenv("OKX_PASSPHRASE","")

state={"price":None,"anchor":None,"tick_sz":None,"account_op":0.0,"account_usdt":0.0,"initial_total":None,"trades":0,"buys":0,"sells":0,
"last_trade":None,"started":None,"error":None,"guard":None,"api_ok":False,"mode":"OKX LIVE SPOT","armed":LIVE_ENABLED}

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
    state["tick_sz"]=x.get("tickSz")
    return float(x["minSz"]),float(x["lotSz"])

def market_price():
    return float(okx_request("GET","/api/v5/market/ticker",{"instId":INST_ID})["data"][0]["last"])

def balances():
    d=okx_request("GET","/api/v5/account/balance",{"ccy":f"{BASE_CCY},{QUOTE_CCY}"},auth=True)
    op=usdt=0.0
    for x in d["data"][0].get("details",[]):
        if x["ccy"]==BASE_CCY: op=float(x.get("availBal") or x.get("cashBal") or 0)
        if x["ccy"]==QUOTE_CCY: usdt=float(x.get("availBal") or x.get("cashBal") or 0)
    return op,usdt

def refresh_balances():
    op,usdt=balances(); state["account_op"]=op; state["account_usdt"]=usdt; return op,usdt

def order_fill_price(ord_id,fallback):
    try:
        x=okx_request("GET","/api/v5/trade/order",{"instId":INST_ID,"ordId":ord_id},auth=True)["data"][0]
        return float(x.get("avgPx") or x.get("fillPx") or fallback)
    except Exception:
        return fallback

def order_usd(px):
    min_sz,_=instrument_rules()
    min_usd=min_sz*px
    return TRADE_USD if min_usd < TRADE_USD else min_usd*1.01

def get_order_by_client_id(clid):
    try:
        d=okx_request("GET","/api/v5/trade/order",{"instId":INST_ID,"clOrdId":clid},auth=True)
        return d.get("data",[None])[0] if d.get("data") else None
    except Exception:
        return None

def place_market(side,usd,px):
    if not LIVE_ENABLED: raise RuntimeError("LIVE trading safety lock is OFF")
    min_sz,lot_sz=instrument_rules()
    # Unique client ID makes an ambiguous submit reconcilable and prevents blind duplicate retries.
    clid=("grd"+uuid.uuid4().hex)[:32]
    if side=="buy":
        body={"instId":INST_ID,"tdMode":"cash","side":"buy","ordType":"market","sz":f"{usd:.8f}","tgtCcy":"quote_ccy","clOrdId":clid}
    else:
        qty=max(usd/px,min_sz)
        if lot_sz>0: qty=math.ceil(qty/lot_sz)*lot_sz
        body={"instId":INST_ID,"tdMode":"cash","side":"sell","ordType":"market","sz":f"{qty:.8f}","tgtCcy":"base_ccy","clOrdId":clid}
    try:
        item=okx_request("POST","/api/v5/trade/order",body=body,auth=True)["data"][0]
        if item.get("sCode") not in (None,"","0"): raise RuntimeError(f'Order {item.get("sCode")}: {item.get("sMsg")}')
        oid=item.get("ordId")
        if not oid: raise RuntimeError("OKX accepted response without ordId")
        return oid,clid
    except (requests.Timeout, requests.ConnectionError) as e:
        # Never submit again blindly. Query OKX with the same unique clOrdId first.
        for _ in range(5):
            time.sleep(1)
            item=get_order_by_client_id(clid)
            if item and item.get("ordId"):
                return item["ordId"],clid
        raise RuntimeError(f"Ambiguous order submit; no retry sent. clOrdId={clid}") from e

def execute(side,level):
    # Sequence rule: compare a new order only with the immediately previous bot order.
    prev=state.get("last_trade")
    if prev:
        prev_side=prev.get("side")
        prev_px=float(prev.get("fill_price") or prev.get("trigger_price") or 0)
        if prev_px>0:
            if side=="BUY":
                if prev_side=="BUY" and level>=prev_px:
                    state["guard"]=f"BUY blocked: {level:.8f} >= previous BUY {prev_px:.8f}"
                    return False
                if prev_side=="SELL" and level>=prev_px*(1-MIN_ROUNDTRIP_MARGIN):
                    state["guard"]=f"BUY blocked: not safely below previous SELL {prev_px:.8f}"
                    return False
            else:
                if prev_side=="SELL" and level<=prev_px:
                    state["guard"]=f"SELL blocked: {level:.8f} <= previous SELL {prev_px:.8f}"
                    return False
                if prev_side=="BUY" and level<=prev_px*(1+MIN_ROUNDTRIP_MARGIN):
                    state["guard"]=f"SELL blocked: not safely above previous BUY {prev_px:.8f}"
                    return False
    state["guard"]=None
    op,usdt=refresh_balances(); usd=order_usd(level)
    if side=="BUY" and usdt<usd: raise RuntimeError(f"Insufficient LIVE {QUOTE_CCY}")
    if side=="SELL" and op*level<usd: raise RuntimeError(f"Insufficient LIVE {BASE_CCY}")
    oid,clid=place_market(side.lower(),usd,level)
    time.sleep(1); fill_px=order_fill_price(oid,level); refresh_balances()
    state["trades"]+=1; state["buys"]+=side=="BUY"; state["sells"]+=side=="SELL"; state["anchor"]=fill_px
    state["last_trade"]={"side":side,"trigger_price":level,"fill_price":fill_px,"usd":usd,"ordId":oid,"clOrdId":clid,"time":datetime.now(timezone.utc).isoformat()}
    return True
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
                    state["anchor"]=px; state["started"]=datetime.now(timezone.utc).isoformat(); state["initial_op"]=state["account_op"]; state["initial_usdt"]=state["account_usdt"]
                if LIVE_ENABLED and state["api_ok"]:
                    while px>=state["anchor"]*(1+STEP):
                        if not execute("SELL",state["anchor"]*(1+STEP)): break
                    while px<=state["anchor"]*(1-STEP):
                        if not execute("BUY",state["anchor"]*(1-STEP)): break
        except Exception as e:
            with lock: state["error"]=str(e)[:220]
            print("WORKER ERROR:",state["error"],flush=True)
        time.sleep(POLL_SECONDS)

def snapshot():
    with lock:
        s=dict(state)
        if s["anchor"]:
            s["upper"]=s["anchor"]*(1+STEP); s["lower"]=s["anchor"]*(1-STEP)
        else:
            s["upper"]=None; s["lower"]=None
        if s["price"] is not None:
            s["op_value"]=s["account_op"]*s["price"]
            s["total"]=s["account_usdt"]+s["op_value"]
        else:
            s["op_value"]=None; s["total"]=None
        s["pnl"]=None; s["pnl_pct"]=None
        s["op_change"]=None if s.get("initial_op") is None else s["account_op"]-s["initial_op"]
        s["usdt_change"]=None if s.get("initial_usdt") is None else s["account_usdt"]-s["initial_usdt"]
        s["anchor_vs_price_pct"]=None if not s["price"] or not s["anchor"] else (s["price"]/s["anchor"]-1)*100
        s["grid_pct"]=STEP*100
        s["trade_target_usd"]=TRADE_USD
        s["inst_id"]=INST_ID; s["base_ccy"]=BASE_CCY; s["quote_ccy"]=QUOTE_CCY; s["tick_sz"]=state.get("tick_sz")
        return s

HTML="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Grid · OKX LIVE</title>
<style>body{margin:0;background:#080b12;color:#eaf0ff;font-family:system-ui,Arial}.wrap{max-width:900px;margin:auto;padding:20px}h1{font-size:22px}.muted{color:#8d98ad}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.card{background:#111724;border:1px solid #222d42;border-radius:12px;padding:14px}.v{font-size:22px;font-weight:700;margin-top:6px}.pos,.buy{color:#4ade80}.neg,.sell{color:#fb7185}small{color:#8d98ad}</style></head><body><div class="wrap">
<h1><span id="pairTitle">GRID</span> · OKX LIVE</h1><div class="muted">LIVE SPOT <span id="pairHead">...</span> · Grid <span id="gridHead">...</span> · target $2/order · update 5s</div><div id="conn" class="card" style="margin-top:14px">OKX LIVE CONNECTION<div class="v">CHECKING...</div></div>
<div id="x" style="margin-top:14px">Loading...</div></div><script>
const n=(x,d=2)=>x==null?'N/A':Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const tickDigits=t=>{if(t==null)return null;let v=String(t).toLowerCase();if(v.includes('e-'))return Number(v.split('e-')[1]);let q=v.split('.')[1];return q?q.length:0};
const px=(x,t)=>{let d=tickDigits(t);return n(x,d==null?6:d)};
async function go(){try{let s=await(await fetch('/api',{cache:'no-store'})).json();let p=s.pnl||0,cl=p>=0?'pos':'neg';
document.getElementById('gridHead').textContent=n(s.grid_pct,2)+'%'; document.getElementById('pairTitle').textContent=s.base_ccy+' GRID'; document.getElementById('pairHead').textContent=s.inst_id; document.title=s.base_ccy+' Grid · OKX LIVE';\ndocument.getElementById('conn').innerHTML=`OKX LIVE CONNECTION<div class="v ${s.api_ok&&!s.error?'pos':'neg'}">${s.api_ok&&!s.error?'CONNECTED':'ERROR'}</div><small>${s.api_ok?'Account '+s.base_ccy+' '+n(s.account_op,8)+' · '+s.quote_ccy+' '+n(s.account_usdt,4):(s.error||'Waiting for API')}</small><br><small>Trading: <b class="${s.armed?'pos':'neg'}">${s.armed?'LIVE ENABLED':'SAFETY LOCKED'}</b></small>`;
document.getElementById('x').innerHTML=`<div class="grid">
<div class="card">${s.base_ccy} PRICE<div class="v">$${px(s.price,s.tick_sz)}</div></div>
<div class="card">ANCHOR<div class="v">$${px(s.anchor,s.tick_sz)}</div><small>Now vs anchor ${s.anchor_vs_price_pct==null?"N/A":(s.anchor_vs_price_pct>=0?"+":"")+n(s.anchor_vs_price_pct,3)+"%"}<br>Buy ≤ ${px(s.lower,s.tick_sz)} · Sell ≥ ${px(s.upper,s.tick_sz)}</small></div>
<div class="card">GRID<div class="v">${n(s.grid_pct,2)}%</div><small>Target $${n(s.trade_target_usd,2)}/order</small></div>
<div class="card">ACCOUNT VALUE<div class="v ${cl}">$${n(s.total,4)}</div><small class="${cl}">${s.pnl==null?'P&L starts after API connects':(p>=0?'+':'')+'$'+n(p,4)+' ('+n(s.pnl_pct,3)+'%) since bot start'}</small></div>
<div class="card">${s.base_ccy} AVAILABLE<div class="v">${n(s.account_op,8)}</div><small>Start ${n(s.initial_op,8)} · Change ${s.op_change==null?"N/A":(s.op_change>=0?"+":"")+n(s.op_change,8)+" "+s.base_ccy} · ≈ ${n(s.op_value,2)}</small></div>
<div class="card">${s.quote_ccy} AVAILABLE<div class="v">${n(s.account_usdt,4)}</div><small>Start ${n(s.initial_usdt,4)} · Change ${s.usdt_change==null?"N/A":(s.usdt_change>=0?"+":"")+n(s.usdt_change,4)+" "+s.quote_ccy}</small></div>
<div class="card">ORDERS<div class="v">${s.trades}</div><small><span class="buy">BUY ${s.buys}</span> · <span class="sell">SELL ${s.sells}</span></small></div>
<div class="card">LAST ORDER<div class="v ${s.last_trade?.side==='BUY'?'buy':'sell'}">${s.last_trade?s.last_trade.side:'N/A'}</div><small>${s.last_trade?'~$'+n(s.last_trade.usd,2)+' · OKX '+s.last_trade.ordId:'Waiting for grid trigger'}</small></div>
<div class="card">STARTED<div class="v" style="font-size:14px">${s.started||'N/A'}</div><small>Mode: ${s.mode}</small></div>
</div><p class="muted">Status: ${s.error?'ERROR · '+s.error:(s.guard?'PROTECTED · '+s.guard:(s.api_ok?'CONNECTED · OKX LIVE':'Waiting for API'))}</p>`; }catch(e){}}
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
