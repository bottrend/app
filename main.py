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
BALANCE_PARTS=float(os.getenv("BALANCE_PARTS","100"))
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

def order_qty(base_balance):
    min_sz,lot_sz=instrument_rules()
    qty=base_balance/BALANCE_PARTS
    if lot_sz>0:
        qty=math.floor(qty/lot_sz)*lot_sz
    return max(qty,min_sz)

def order_usd(px,base_balance):
    return order_qty(base_balance)*px

def get_order_by_client_id(clid):
    try:
        d=okx_request("GET","/api/v5/trade/order",{"instId":INST_ID,"clOrdId":clid},auth=True)
        return d.get("data",[None])[0] if d.get("data") else None
    except Exception:
        return None

def place_market(side,qty,px):
    if not LIVE_ENABLED: raise RuntimeError("LIVE trading safety lock is OFF")
    min_sz,lot_sz=instrument_rules()
    # Unique client ID makes an ambiguous submit reconcilable and prevents blind duplicate retries.
    clid=("grd"+uuid.uuid4().hex)[:32]
    if qty<min_sz: raise RuntimeError(f"Dynamic order below OKX minimum {min_sz} {BASE_CCY}")
    if side=="buy":
        body={"instId":INST_ID,"tdMode":"cash","side":"buy","ordType":"market","sz":f"{qty:.8f}","tgtCcy":"base_ccy","clOrdId":clid}
    else:
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
    op,usdt=refresh_balances(); qty=order_qty(op); usd=qty*level
    if side=="BUY" and usdt<usd: raise RuntimeError(f"Insufficient LIVE {QUOTE_CCY}")
    if side=="SELL" and op<qty: raise RuntimeError(f"Insufficient LIVE {BASE_CCY}")
    oid,clid=place_market(side.lower(),qty,level)
    time.sleep(1); fill_px=order_fill_price(oid,level); refresh_balances()
    state["trades"]+=1; state["buys"]+=side=="BUY"; state["sells"]+=side=="SELL"; state["anchor"]=fill_px
    state["last_trade"]={"side":side,"trigger_price":level,"fill_price":fill_px,"usd":usd,"qty":qty,"ordId":oid,"clOrdId":clid,"time":datetime.now(timezone.utc).isoformat()}
    return True
def worker():
    while True:
        try:
            px=market_price()
            if state.get("tick_sz") is None:
                instrument_rules()
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
        s["balance_parts"]=BALANCE_PARTS
        s["next_order_qty"]=order_qty(s["account_op"]) if s["account_op"]>0 else None
        s["inst_id"]=INST_ID; s["base_ccy"]=BASE_CCY; s["quote_ccy"]=QUOTE_CCY; s["tick_sz"]=state.get("tick_sz")
        return s

HTML="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OKX Grid Bot</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#090d16;color:#eef3ff;font-family:Inter,system-ui,Arial,sans-serif}.wrap{max-width:980px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.title{font-size:25px;font-weight:800}.sub,.muted,small{color:#8e9ab0}.badge{padding:8px 13px;border-radius:999px;font-weight:800;border:1px solid #27334a}.live{color:#4ade80;background:#10261c}.off{color:#fb7185;background:#2a151b}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(185px,1fr));gap:11px;margin-top:15px}.card{background:#111827;border:1px solid #243149;border-radius:14px;padding:15px;min-height:100px}.label{font-size:12px;color:#8e9ab0;font-weight:700;letter-spacing:.06em}.v{font-size:22px;font-weight:800;margin-top:7px}.buy,.pos{color:#4ade80}.sell,.neg{color:#fb7185}.wide{grid-column:span 2}.status{margin-top:12px;padding:12px 14px;background:#111827;border:1px solid #243149;border-radius:12px;font-size:13px}@media(max-width:520px){.wrap{padding:14px}.title{font-size:21px}.wide{grid-column:span 1}.v{font-size:19px}}
</style></head><body><div class="wrap"><div class="top"><div><div class="title"><span id="pair">GRID</span> · OKX</div><div class="sub">Spot Grid · <span id="gridHead">...</span> · dynamic balance sizing</div></div><div id="badge" class="badge off">CHECKING</div></div><div id="x">Loading...</div></div>
<script>
const n=(x,d=2)=>x==null?'N/A':Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const tickDigits=t=>{if(t==null)return null;let v=String(t).toLowerCase();if(v.includes('e-'))return Number(v.split('e-')[1]);let q=v.split('.')[1];return q?q.length:0};
const px=(x,t)=>{let d=tickDigits(t);return n(x,d==null?6:d)};
async function go(){try{let s=await(await fetch('/api',{cache:'no-store'})).json();let b=document.getElementById('badge');document.getElementById('pair').textContent=s.inst_id;document.getElementById('gridHead').textContent=n(s.grid_pct,2)+'%';document.title=s.inst_id+' · OKX Grid';b.className='badge '+(s.armed?'live':'off');b.textContent=s.armed?'LIVE':'SAFETY LOCKED';
let p=s.pnl||0,cl=p>=0?'pos':'neg';document.getElementById('x').innerHTML=`<div class="grid">
<div class="card"><div class="label">${s.quote_ccy} BALANCE</div><div class="v">${n(s.account_usdt,4)}</div><small>${s.quote_ccy}</small></div>
<div class="card"><div class="label">${s.base_ccy} BALANCE</div><div class="v">${n(s.account_op,8)}</div><small>≈ ${n(s.op_value,2)} ${s.quote_ccy}</small></div>
<div class="card"><div class="label">CURRENT PRICE</div><div class="v">${px(s.price,s.tick_sz)}</div><small>${s.quote_ccy}/${s.base_ccy}</small></div>
<div class="card"><div class="label">ANCHOR</div><div class="v">${px(s.anchor,s.tick_sz)}</div><small>${s.anchor_vs_price_pct==null?'N/A':(s.anchor_vs_price_pct>=0?'+':'')+n(s.anchor_vs_price_pct,3)+'%'} vs current</small></div>
<div class="card"><div class="label buy">BUY ≤</div><div class="v buy">${px(s.lower,s.tick_sz)}</div></div>
<div class="card"><div class="label sell">SELL ≥</div><div class="v sell">${px(s.upper,s.tick_sz)}</div></div>
<div class="card"><div class="label">GRID</div><div class="v">${n(s.grid_pct,2)}%</div></div>
<div class="card"><div class="label">NEXT ORDER SIZE</div><div class="v">${n(s.next_order_qty,8)}</div><small>${s.base_ccy} balance / ${n(s.balance_parts,0)}</small></div>
<div class="card wide"><div class="label">TOTAL VALUE</div><div class="v">${n(s.total,4)} ${s.quote_ccy}</div><small>Mark-to-market at current price</small></div>
<div class="card"><div class="label">BUY COUNT</div><div class="v buy">${s.buys}</div></div>
<div class="card"><div class="label">SELL COUNT</div><div class="v sell">${s.sells}</div></div>
<div class="card"><div class="label">TOTAL ORDERS</div><div class="v">${s.trades}</div></div>
<div class="card wide"><div class="label">LAST TRADE</div><div class="v ${s.last_trade?.side==='BUY'?'buy':'sell'}">${s.last_trade?s.last_trade.side+' '+n(s.last_trade.qty,8)+' '+s.base_ccy+' @ '+px(s.last_trade.fill_price,s.tick_sz):'None'}</div><small>${s.last_trade?'~'+n(s.last_trade.usd,4)+' '+s.quote_ccy+' · '+s.last_trade.ordId:'Waiting for grid trigger'}</small></div>
</div><div class="status">API: <b class="${s.api_ok?'pos':'neg'}">${s.api_ok?'CONNECTED':'NOT CONNECTED'}</b> · Pending: NO · Guard: ${s.guard||'none'} · Last error: ${s.error||'none'}<br><span class="muted">Started: ${s.started||'N/A'}</span></div>`; }catch(e){}}
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
