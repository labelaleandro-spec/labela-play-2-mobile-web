#!/usr/bin/env python3
"""Labela Play 2.0 Mobile - private test gateway. No TV installation or update functions."""
import base64
import concurrent.futures
import hmac
import http.cookies
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode, urljoin, urlsplit, parse_qs
from urllib.request import build_opener, HTTPRedirectHandler, Request
from urllib.error import HTTPError, URLError

PORT=int(os.environ.get("PORT","10000"))
APP_PIN=os.environ.get("LABELA_ACCESS_CODE","")
PROVIDER=os.environ.get("LABELA_PROVIDER_HOST","http://4k2026.biz").rstrip("/")
USER=os.environ.get("LABELA_XTREAM_USER","")
PASSWORD=os.environ.get("LABELA_XTREAM_PASSWORD","")
if len(APP_PIN)<16:
    raise RuntimeError("LABELA_ACCESS_CODE must have at least 16 characters")
origin=urlsplit(PROVIDER)
if origin.scheme not in ("https","http") or not origin.hostname or origin.username or origin.password or origin.query:
    raise RuntimeError("Invalid provider host")
COOKIE=secrets.token_urlsafe(36)
LOCK=threading.RLock()
CACHE={"items":None,"at":0,"token":{},"events":[],"series":{},"catalog_issues":{}}
ATTEMPTS={}
ALLOWED={origin.netloc.lower()}
ALLOWED.update(x.strip().lower() for x in os.environ.get("LABELA_ALLOWED_CDN","").split(",") if x.strip())

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None
OPENER=build_opener(NoRedirect)

def probe_provider_network():
    """Non-credentialed network check. No login secrets or account lookup."""
    try:
        p=urlsplit(PROVIDER)
        port=p.port or (443 if p.scheme=="https" else 80)
        infos=socket.getaddrinfo(p.hostname,port,type=socket.SOCK_STREAM)
        print("LABELA_NETWORK DNS_OK address_count="+str(len(infos)),flush=True)
        with socket.create_connection((p.hostname,port),timeout=7):
            print("LABELA_NETWORK TCP_OK port="+str(port),flush=True)
        import http.client
        conn=(http.client.HTTPSConnection(p.hostname,port,timeout=8)
              if p.scheme=="https" else http.client.HTTPConnection(p.hostname,port,timeout=8))
        try:
            conn.request("HEAD","/player_api.php",headers={"User-Agent":"LabelaPlayMobile/2.0"})
            resp=conn.getresponse()
            location=resp.getheader("Location","")
            target=urlsplit(urljoin(PROVIDER+"/",location)).hostname if location else ""
            print("LABELA_NETWORK HTTP_HEAD_STATUS="+str(resp.status)+(" REDIRECT_HOST="+str(target) if target else ""),flush=True)
            resp.read(1000)
        finally:
            conn.close()
    except socket.gaierror:
        print("LABELA_NETWORK DNS_FAILURE - host could not be resolved",flush=True)
    except (TimeoutError,socket.timeout):
        print("LABELA_NETWORK CONNECT_TIMEOUT",flush=True)
    except ConnectionRefusedError:
        print("LABELA_NETWORK CONNECTION_REFUSED",flush=True)
    except Exception as e:
        print("LABELA_NETWORK TEST_FAILED kind="+type(e).__name__,flush=True)

def clean_error(e):
    if isinstance(e,HTTPError):
        if e.code in (301,302,303,307,308):
            return "Servidor IPTV redirecionou para outro endereço (HTTP "+str(e.code)+"). Precisamos confirmar o endereço atualizado com o provedor."
        if e.code==403:return "Servidor IPTV recusou o acesso deste servidor (HTTP 403). Possível bloqueio de IP, servidor ou assinatura."
        return "Servidor IPTV retornou HTTP "+str(e.code)
    if isinstance(e,socket.gaierror):return "Não foi possível encontrar o endereço do provedor (DNS)."
    if isinstance(e,(TimeoutError,socket.timeout)):return "Servidor IPTV não respondeu no tempo esperado."
    if isinstance(e,URLError):
        reason=e.reason
        if isinstance(reason,socket.gaierror):return "DNS do provedor não respondeu."
        if isinstance(reason,(TimeoutError,socket.timeout)):return "Conexão com o provedor expirou."
        return "Não foi possível acessar a rede do provedor IPTV."
    if isinstance(e,(OSError,ConnectionError)):return "Não foi possível conectar ao provedor IPTV."
    if isinstance(e,ValueError):return str(e)[:160]
    return "Erro interno na consulta. Tente novamente."

def log(message):
    """Messages must be sanitized before calling: never log user, password or URLs."""
    safe=message[:160]
    print("LABELA_DIAG "+safe,flush=True)
    with LOCK:
        CACHE["events"].append({"at":time.strftime("%H:%M:%S"),"message":safe})
        CACHE["events"]=CACHE["events"][-30:]

def upstream(url,range_header=None):
    p=urlsplit(url)
    if p.scheme not in ("https","http") or p.netloc.lower() not in ALLOWED or p.username or p.password or not p.hostname:
        raise ValueError("Domínio de mídia não autorizado no teste.")
    hdr={"User-Agent":"LabelaPlayMobile/2.0","Accept-Encoding":"identity"}
    if range_header and re.fullmatch(r"bytes=\d*-\d*",range_header):
        hdr["Range"]=range_header
    return OPENER.open(Request(url,headers=hdr),timeout=24)

def xtream(action="",extra=None):
    if not USER or not PASSWORD:raise ValueError("Conta Xtream não configurada.")
    args={"username":USER,"password":PASSWORD}
    if action:args["action"]=action
    if extra:args.update(extra)
    uri=PROVIDER+"/player_api.php?"+urlencode(args)
    with upstream(uri) as r:
        data=r.read(24*1024*1024+1)
        if len(data)>24*1024*1024:raise ValueError("Catálogo excede limite de segurança.")
        try:
            return json.loads(data)
        except (json.JSONDecodeError,UnicodeDecodeError):
            raise ValueError("O provedor não retornou JSON Xtream válido.")

def build_catalog():
    actions=("get_live_categories","get_live_streams","get_vod_categories","get_vod_streams","get_series_categories","get_series")
    data={}
    problems={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        fut={a:ex.submit(xtream,a) for a in actions}
        for action,f in fut.items():
            try:
                value=f.result()
                if not isinstance(value,list):
                    raise ValueError("Resposta inesperada do provedor.")
                data[action]=value
            except Exception as e:
                problems[action]=clean_error(e)
                log("Falha ao consultar "+action+": "+clean_error(e))
    categories={}
    for kind,act in (("live","get_live_categories"),("movie","get_vod_categories"),("series","get_series_categories")):
        categories[kind]={str(x.get("category_id")):str(x.get("category_name") or "Outros") for x in data.get(act,[]) if isinstance(x,dict)}
    output=[]
    for kind,action in (("live","get_live_streams"),("movie","get_vod_streams"),("series","get_series")):
        for item in data.get(action,[])[:65000]:
            if not isinstance(item,dict):continue
            sid=str(item.get("series_id") if kind=="series" else item.get("stream_id"))
            if not sid.isdecimal():continue
            cat=str(item.get("category_id") or "")
            ext=re.sub("[^a-z0-9]","",str(item.get("container_extension") or "mp4").lower())[:7]
            output.append({"id":sid,"key":kind+":"+sid,"kind":kind,"name":str(item.get("name") or item.get("title") or "Sem nome")[:170],
                "group":categories[kind].get(cat,"Outros"),
                "poster":str(item.get("stream_icon") or item.get("cover") or "")[:700],
                "plot":str(item.get("plot") or "")[:800],"ext":ext,
                "added":str(item.get("added") or "")[:25]})
    stream_actions=("get_live_streams","get_vod_streams","get_series")
    if all(act in problems for act in stream_actions):
        raise ValueError("A autenticação foi aceita, mas nenhuma consulta de catálogo respondeu. Consulte Diagnóstico.")
    with LOCK:
        CACHE["items"]=output
        CACHE["at"]=time.monotonic()
        CACHE["catalog_issues"]=problems
    log("Catálogo: "+str(len(output))+" itens; "+str(len(problems))+" consultas com problema.")
    return output

def get_catalog():
    with LOCK:
        old=CACHE["items"]
        if old is not None and time.monotonic()-CACHE["at"]<1800:return old
    return build_catalog()

def token_for(url):
    p=urlsplit(url)
    if p.scheme not in ("https","http") or p.netloc.lower() not in ALLOWED or p.username or p.password:
        raise ValueError("CDN não autorizado. Consulte o diagnóstico.")
    token=secrets.token_urlsafe(24)
    with LOCK:
        CACHE["token"][token]=(url,time.monotonic()+1800)
        if len(CACHE["token"])>5000:
            now=time.monotonic()
            CACHE["token"]={k:v for k,v in CACHE["token"].items() if v[1]>now}
    return "/segment/"+token

def rewrite_hls(data,base):
    s=data.decode("utf-8-sig","replace")
    if not s.lstrip().startswith("#EXTM3U"):raise ValueError("A origem não retornou um manifesto HLS.")
    out=[]
    for line in s.splitlines():
        v=line.strip()
        if v and not v.startswith("#"):
            out.append(token_for(urljoin(base,v)))
        elif 'URI="' in line:
            out.append(re.sub(r'URI="([^"]+)"',lambda m:'URI="'+token_for(urljoin(base,m.group(1)))+'"',line))
        else:
            out.append(line)
    return ("\n".join(out)+"\n").encode("utf-8")

def json_bytes(data):
    return json.dumps(data,ensure_ascii=False,separators=(",",":")).encode("utf-8")

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):return
    def common(self,code=200,ctype="application/json; charset=utf-8",extra=None,length=None):
        self.send_response(code)
        self.send_header("Content-Type",ctype)
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Content-Security-Policy","default-src 'self'; img-src 'self' https: data:; media-src 'self' blob:; connect-src 'self' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if extra:
            for k,v in extra.items():self.send_header(k,v)
        if length is not None:self.send_header("Content-Length",str(length))
        self.end_headers()
    def send_body(self,data,ctype="text/html; charset=utf-8",code=200,extra=None):
        self.common(code,ctype,extra,len(data))
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass
    def sendj(self,obj,status=200):
        self.send_body(json_bytes(obj),"application/json; charset=utf-8",status)
    def authenticated(self):
        jar=http.cookies.SimpleCookie()
        try:jar.load(self.headers.get("Cookie",""))
        except Exception:return False
        c=jar.get("labela_session")
        return bool(c and hmac.compare_digest(c.value,COOKIE))
    def same_origin(self):
        origin=self.headers.get("Origin")
        if not origin:return True
        p=urlsplit(origin)
        return p.scheme=="https" and p.netloc==self.headers.get("Host")
    def gated(self):
        if not self.authenticated():
            self.sendj({"error":"Sessão de testes não autorizada."},401)
            return False
        return True
    def body(self,max_len=2048):
        n=int(self.headers.get("Content-Length","0"))
        if n<1 or n>max_len:raise ValueError("Dados inválidos.")
        return json.loads(self.rfile.read(n))
    def do_POST(self):
        path=urlsplit(self.path).path
        if not self.same_origin():return self.sendj({"error":"Origem bloqueada."},403)
        if path=="/api/unlock":
            ip=self.client_address[0]
            now=time.monotonic()
            with LOCK:
                ATTEMPTS[ip]=[v for v in ATTEMPTS.get(ip,[]) if now-v<180]
                if len(ATTEMPTS[ip])>=9:return self.sendj({"error":"Limite de tentativas."},429)
                ATTEMPTS[ip].append(now)
            try:
                attempt=str(self.body(512).get("code",""))
                if not hmac.compare_digest(attempt,APP_PIN):return self.sendj({"error":"Código de acesso incorreto."},401)
                with LOCK:ATTEMPTS.pop(ip,None)
                return self.send_body(json_bytes({"ok":True}),"application/json",200,{"Set-Cookie":"labela_session="+COOKIE+"; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=7200"})
            except Exception:return self.sendj({"error":"Código inválido."},400)
        if not self.gated():return
        if path=="/api/connect":
            stage="preenchimento"
            authenticated_provider=False
            try:
                inp=self.body(1500)
                user=str(inp.get("user","")).strip()
                pw=str(inp.get("password",""))
                if len(user)>150 or len(pw)>150 or not user or not pw:raise ValueError("Preencha usuário e senha.")
                global USER,PASSWORD
                with LOCK:
                    USER,PASSWORD=user,pw
                    CACHE.update({"items":None,"at":0,"token":{},"series":{}})
                stage="autenticação"
                log("Consultando autenticação Xtream no servidor configurado.")
                test=xtream()
                if not isinstance(test,dict) or not isinstance(test.get("user_info"),dict):
                    raise ValueError("Servidor não confirmou uma conta Xtream.")
                status=str(test["user_info"].get("status",""))
                if status.lower() not in ("active","ativo","enabled","trial"):
                    raise ValueError("Conta não está ativa: "+status[:40])
                authenticated_provider=True
                stage="catálogo"
                log("Conta validada pelo provedor. Consultando categorias e títulos.")
                try:
                    data=get_catalog()
                    return self.sendj({"ok":True,"count":len(data),"partial":bool(CACHE.get("catalog_issues")),"issues":list(CACHE.get("catalog_issues",{}))})
                except Exception as e:
                    log("Conta validada, catálogo indisponível: "+clean_error(e))
                    return self.sendj({"error":"Conta autenticada, mas o catálogo não carregou. "+clean_error(e),"authenticated":True},502)
            except Exception as e:
                if not authenticated_provider:
                    with LOCK:
                        USER,PASSWORD="",""
                        CACHE["items"]=None
                log("Falha na etapa de "+stage+": "+clean_error(e))
                return self.sendj({"error":"Falha em "+stage+": "+clean_error(e),"authenticated":authenticated_provider},502)
        return self.sendj({"error":"Rota inexistente."},404)
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=="/health":return self.sendj({"ok":True,"service":"labela-play-mobile","providerConnected":False})
        if path=="/login":
            html=b'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Labela Play | Acesso</title><style>body{background:#071326;color:#f6f7ff;font:16px system-ui;min-height:93vh;display:grid;place-items:center}main{padding:28px;background:#102543;border:1px solid #375d83;border-radius:19px;width:min(84vw,380px)}h1{letter-spacing:-1px}em{color:#ffc338;font-style:normal}input,button{width:100%;padding:15px;border-radius:11px;margin-top:12px;box-sizing:border-box;font:inherit}input{background:#07192e;border:1px solid #53769d;color:white}button{background:#ffc338;color:#101c31;border:0;font-weight:800}p{color:#bacce1;font-size:13px}</style><main><h1>LABELA <em>PLAY 2.0</em></h1><p>Acesso ao ambiente de teste privado. Este codigo e diferente da senha IPTV.</p><form id="f"><input id="pin" type="password" autocomplete="off" placeholder="Codigo de acesso ao teste" required><button>Entrar</button></form><p id="m"></p></main><script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();try{const r=await fetch("/api/unlock",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:document.getElementById("pin").value})});const d=await r.json();if(!r.ok)throw Error(d.error||"Erro de acesso");location.href="/";}catch(e){document.getElementById("m").textContent=e.message}}</script></html>'''
            return self.send_body(html)
        if path in ("/","/index.html") and not self.authenticated():
            self.send_response(303)
            self.send_header("Location","/login")
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            return
        if not self.gated():return
        if path=="/api/catalog":
            try:
                items=get_catalog()
                return self.sendj({"connected":True,"items":items,"count":len(items)})
            except Exception as e:
                log("Não foi possível consultar catálogo.")
                return self.sendj({"connected":False,"error":clean_error(e)},502)
        if path=="/api/account":
            return self.sendj({"configured":bool(USER and PASSWORD),"provider":"4KPlay - origem HTTP","warning":"Trecho da hospedagem ate o provedor nao criptografado.","count":len(CACHE["items"] or []),"catalogIssues":list(CACHE.get("catalog_issues",{}))})
        if path=="/api/events":
            return self.sendj({"events":CACHE["events"]})
        if path=="/api/epg":
            sid=parse_qs(urlsplit(self.path).query).get("id",[""])[0]
            if not sid.isdecimal():return self.sendj({"error":"Canal inválido."},400)
            try:
                j=xtream("get_short_epg",{"stream_id":sid,"limit":"7"})
                return self.sendj({"epg":j.get("epg_listings",[]) if isinstance(j,dict) else []})
            except Exception as e:return self.sendj({"error":clean_error(e)},502)
        if path=="/api/series":
            sid=parse_qs(urlsplit(self.path).query).get("id",[""])[0]
            if not sid.isdecimal():return self.sendj({"error":"Série inválida."},400)
            try:
                items=get_catalog()
                if not any(i["kind"]=="series" and i["id"]==sid for i in items):return self.sendj({"error":"Série fora do catálogo."},404)
                j=xtream("get_series_info",{"series_id":sid})
                output=[]
                for s,episodes in (j.get("episodes") or {}).items():
                    for ep in episodes:
                        if not isinstance(ep,dict):continue
                        eid=str(ep.get("id") or "")
                        if not eid.isdecimal():continue
                        ext=re.sub("[^a-z0-9]","",str(ep.get("container_extension") or "mp4").lower())[:7]
                        output.append({"id":eid,"season":str(s)[:10],"number":str(ep.get("episode_num") or "")[:10],
                                       "name":str(ep.get("title") or "Episódio")[:150],"ext":ext})
                with LOCK:CACHE["series"][sid]={e["id"] for e in output}
                return self.sendj({"episodes":output,"plot":str((j.get("info") or {}).get("plot") or "")[:1000]})
            except Exception as e:return self.sendj({"error":clean_error(e)},502)
        if path.startswith("/media/"):
            match=re.fullmatch(r"/media/(live|movie|series)/(\d+)\.([a-zA-Z0-9]{2,8})",path)
            if not match:return self.sendj({"error":"Mídia inválida."},400)
            kind,sid,ext=match.groups()
            if ext not in ("m3u8","ts","mp4","mkv","avi","mp3","aac","mov"):return self.sendj({"error":"Formato não autorizado."},400)
            try:
                data=get_catalog()
                if kind!="series" and not any(i["kind"]==kind and i["id"]==sid for i in data):
                    return self.sendj({"error":"Conteúdo fora do catálogo."},404)
                if kind=="series" and not any(sid in ids for ids in CACHE["series"].values()):
                    return self.sendj({"error":"Selecione o episódio primeiro."},404)
                uri=f"{PROVIDER}/{kind}/{quote(USER,safe='')}/{quote(PASSWORD,safe='')}/{sid}.{ext}"
                return self.stream(uri,ext=="m3u8")
            except Exception as e:return self.sendj({"error":clean_error(e)},502)
        if path.startswith("/segment/"):
            token=path[len("/segment/"):]
            with LOCK:entry=CACHE["token"].get(token)
            if not entry or time.monotonic()>entry[1]:return self.sendj({"error":"Segmento expirado."},404)
            return self.stream(entry[0],False)
        if path=="/":
            path="/index.html"
        if path!="/index.html":return self.sendj({"error":"Não encontrado."},404)
        try:
            with open("index.html","rb") as f:return self.send_body(f.read())
        except OSError:
            return self.sendj({"error":"Interface não encontrada."},500)
    def stream(self,url,force=False):
        try:
            rng=self.headers.get("Range")
            with upstream(url,rng) as resp:
                typ=resp.headers.get("Content-Type","application/octet-stream")
                is_hls=force or "mpegurl" in typ.lower() or urlsplit(resp.url).path.lower().endswith(".m3u8")
                if is_hls:
                    data=resp.read(4*1024*1024+1)
                    if len(data)>4*1024*1024:raise ValueError("Playlist grande demais.")
                    return self.send_body(rewrite_hls(data,resp.url),"application/vnd.apple.mpegurl")
                extra={}
                for h in ("Content-Length","Content-Range","Accept-Ranges"):
                    v=resp.headers.get(h)
                    if v:extra[h]=v
                self.common(resp.status,typ,extra)
                while True:
                    block=resp.read(64*1024)
                    if not block:break
                    self.wfile.write(block)
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception as e:
            log("Erro de streaming (sem credenciais).")
            try:self.sendj({"error":clean_error(e)},502)
            except Exception:pass

if __name__=="__main__":
    print("Labela Play 2.0 Mobile - HTTPS via Render, HTTP upstream - test only",flush=True)
    threading.Thread(target=probe_provider_network,daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0",PORT),Handler).serve_forever()
