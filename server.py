#!/usr/bin/env python3
"""Labela Play 2.0 Mobile - private test gateway. No TV installation or update functions."""
import base64
import gzip
import concurrent.futures
import hmac
import http.cookies
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode, urljoin, urlsplit, parse_qs
from urllib.request import build_opener, HTTPRedirectHandler, Request
from urllib.error import HTTPError, URLError

PORT=int(os.environ.get("PORT","10000"))
APP_PIN=os.environ.get("LABELA_ACCESS_CODE","")
PROVIDER=os.environ.get("LABELA_PROVIDER_HOST","http://4k2026.biz").rstrip("/")
DEFAULT_USER=os.environ.get("LABELA_XTREAM_USER","")
DEFAULT_PASSWORD=os.environ.get("LABELA_XTREAM_PASSWORD","")
if len(APP_PIN)<16:
    raise RuntimeError("LABELA_ACCESS_CODE must have at least 16 characters")
origin=urlsplit(PROVIDER)
if (origin.scheme not in ("https","http") or not origin.hostname or origin.username or origin.password
        or origin.query or origin.fragment):
    raise RuntimeError("Invalid provider host")
LOCK=threading.RLock()
SESSIONS={}
NETWORK_STATUS={}
ATTEMPTS=[]
SESSION_TTL=7200
MAX_SESSIONS=8
AI_KEY=os.environ.get("LABELA_OPENAI_API_KEY","")
AI_MODEL=os.environ.get("LABELA_OPENAI_MODEL","gpt-4.1-mini")
AI_SLOT=threading.BoundedSemaphore(1)
CATALOG_SLOT=threading.BoundedSemaphore(1)
ALLOWED={origin.netloc.lower()}
ALLOWED.update(x.strip().lower() for x in os.environ.get("LABELA_ALLOWED_CDN","").split(",") if x.strip())

def load_brand():
    try:
        encoded=os.environ.get("LABELA_OFFICIAL_BRAND_B64","").encode("ascii")
        if len(encoded)>200000:return b""
        data=base64.b64decode(encoded,validate=True)
    except ValueError:
        return b""
    if len(data)>150000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return b""
    return data

BRAND_ICON=load_brand()

class ProviderError(ValueError):
    def __init__(self, code, message, status=502):
        super().__init__(message)
        self.code=code
        self.status=status

class Account:
    def __init__(self,user="",password=""):
        self.user=user
        self.password=password
        self.verified=False
        self.items=None
        self.at=0
        self.tokens={}
        self.series={}
        self.issues={}
        self.catalog_lock=threading.Lock()

class Session:
    def __init__(self,now):
        self.expires=now+SESSION_TTL
        self.account=Account(DEFAULT_USER,DEFAULT_PASSWORD)
        self.events=[]
        self.connect_lock=threading.Lock()
        self.last_ai=float("-inf")

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
        NETWORK_STATUS["dns"]="ok"
        print("LABELA_NETWORK DNS_OK address_count="+str(len(infos)),flush=True)
        with socket.create_connection((p.hostname,port),timeout=7):
            NETWORK_STATUS["tcp"]="ok"
            print("LABELA_NETWORK TCP_OK port="+str(port),flush=True)
        import http.client
        conn=(http.client.HTTPSConnection(p.hostname,port,timeout=8)
              if p.scheme=="https" else http.client.HTTPConnection(p.hostname,port,timeout=8))
        try:
            conn.request("HEAD",p.path+"/player_api.php",headers={"User-Agent":"LabelaPlayMobile/2.0"})
            resp=conn.getresponse()
            print("LABELA_NETWORK HTTP_HEAD_STATUS="+str(resp.status),flush=True)
            resp.read(1000)
        finally:
            conn.close()
        for probe_path in ("/",p.path+"/player_api.php"):
            conn=(http.client.HTTPSConnection(p.hostname,port,timeout=8)
                  if p.scheme=="https" else http.client.HTTPConnection(p.hostname,port,timeout=8))
            try:
                conn.request("GET",probe_path,headers={"User-Agent":"LabelaPlayMobile/2.0"})
                reply=conn.getresponse()
                if probe_path.endswith("/player_api.php"):NETWORK_STATUS["xtreamGet"]=reply.status
                print("LABELA_NETWORK HTTP_GET_STATUS="+str(reply.status),flush=True)
                reply.read(512)
            finally:
                conn.close()
        try:
            with socket.create_connection((p.hostname,443),timeout=7) as sock:
                with ssl.create_default_context().wrap_socket(sock,server_hostname=p.hostname) as tls:
                    NETWORK_STATUS["tls443"]="ok"
                    print("LABELA_NETWORK HTTPS_TLS_OK VERSION="+str(tls.version()),flush=True)
            conn=http.client.HTTPSConnection(p.hostname,443,timeout=8)
            try:
                conn.request("GET","/",headers={"User-Agent":"LabelaPlayMobile/2.0"})
                reply=conn.getresponse()
                print("LABELA_NETWORK HTTPS_GET_ROOT="+str(reply.status),flush=True)
                reply.read(512)
            finally:
                conn.close()
        except (TimeoutError,socket.timeout):
            NETWORK_STATUS["tls443"]="timeout"
            print("LABELA_NETWORK HTTPS_443_TIMEOUT",flush=True)
        except Exception as e:
            NETWORK_STATUS["tls443"]="failed"
            print("LABELA_NETWORK HTTPS_TEST_FAILED kind="+type(e).__name__,flush=True)
    except socket.gaierror:
        print("LABELA_NETWORK DNS_FAILURE - host could not be resolved",flush=True)
    except (TimeoutError,socket.timeout):
        print("LABELA_NETWORK CONNECT_TIMEOUT",flush=True)
    except ConnectionRefusedError:
        print("LABELA_NETWORK CONNECTION_REFUSED",flush=True)
    except Exception as e:
        print("LABELA_NETWORK TEST_FAILED kind="+type(e).__name__,flush=True)

def provider_error(e):
    if isinstance(e,ProviderError):return e
    if isinstance(e,HTTPError):
        e.close()
        if e.code in (301,302,303,307,308):
            return ProviderError("upstream_redirect","O provedor redirecionou a solicitação. Confirme o endpoint oficial com o suporte.")
        if e.code==403:
            return ProviderError("upstream_forbidden","O provedor recusou a solicitação (HTTP 403). O suporte precisa confirmar o motivo.")
        return ProviderError("upstream_http","O provedor retornou HTTP "+str(e.code)+".")
    reason=e.reason if isinstance(e,URLError) else e
    if isinstance(reason,ssl.SSLError):return ProviderError("upstream_tls","A conexão HTTPS com o provedor falhou. Confirme o endereço e o certificado.")
    if isinstance(reason,socket.gaierror):return ProviderError("upstream_dns","Não foi possível resolver o endereço do provedor (DNS).")
    if isinstance(reason,TimeoutError):return ProviderError("upstream_timeout","O provedor não respondeu no tempo esperado.")
    if isinstance(e,(URLError,OSError)):return ProviderError("upstream_network","Não foi possível acessar a rede do provedor.")
    return ProviderError("unexpected_response","A resposta não pôde ser processada.")

def require_https():
    if origin.scheme!="https":
        raise ProviderError("https_required","O provedor está configurado em HTTP. Solicite um endereço Xtream oficial com HTTPS válido para enviar as credenciais.",503)

def media_url(url):
    p=urlsplit(url)
    if p.scheme!="https" or p.netloc.lower() not in ALLOWED or p.username or p.password or not p.hostname:
        raise ProviderError("media_origin","A origem HTTPS da mídia não foi autorizada no teste.")
    return p

def upstream(url,range_header=None):
    media_url(url)
    hdr={"User-Agent":"LabelaPlayMobile/2.0","Accept-Encoding":"identity"}
    if range_header and re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)",range_header):
        hdr["Range"]=range_header
    return OPENER.open(Request(url,headers=hdr),timeout=24)

def xtream(account,action="",extra=None):
    if not account.user or not account.password:
        raise ProviderError("credentials_missing","Conta Xtream não configurada.",409)
    require_https()
    args={"username":account.user,"password":account.password}
    if action:args["action"]=action
    if extra:args.update(extra)
    uri=PROVIDER+"/player_api.php?"+urlencode(args)
    with upstream(uri) as r:
        data=r.read(24*1024*1024+1)
        if len(data)>24*1024*1024:
            raise ProviderError("catalog_limit","A resposta excede o limite do catálogo de testes.")
        try:
            return json.loads(data)
        except (json.JSONDecodeError,UnicodeDecodeError):
            raise ProviderError("xtream_invalid_json","O provedor não retornou JSON Xtream válido.")

def authenticate(account):
    data=xtream(account)
    info=data.get("user_info") if isinstance(data,dict) else None
    if not isinstance(info,dict):
        raise ProviderError("xtream_invalid_account","O endpoint não retornou dados de conta Xtream.")
    auth=str(info.get("auth","")).strip().lower()
    status=str(info.get("status","")).strip().lower()
    if status in ("expired","disabled","banned","inactive","inativo","expirado"):
        raise ProviderError("account_inactive","O provedor informou que a conta está inativa.",403)
    if auth in ("0","false"):
        raise ProviderError("xtream_rejected","O provedor não autenticou a conta Xtream.",422)
    if status not in ("active","ativo","enabled","trial") and not (auth in ("1","true") and not status):
        raise ProviderError("xtream_invalid_account","O provedor não confirmou uma conta Xtream ativa.")
    account.verified=True

def public_poster(account,value):
    url=str(value or "")[:700]
    try:p=urlsplit(url)
    except ValueError:return ""
    if p.scheme!="https" or not p.hostname or p.username or p.password or p.query or p.fragment:
        return ""
    if any(credential and ("/"+quote(credential,safe="")+"/" in url or
                          (len(credential)>=6 and credential in url))
           for credential in (account.user,account.password)):
        return ""
    return url

def build_catalog(account):

    actions=("get_live_categories","get_live_streams","get_vod_categories","get_vod_streams","get_series_categories","get_series")
    data={}
    problems={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        fut={a:ex.submit(xtream,account,a) for a in actions}
        for action,f in fut.items():
            try:
                value=f.result()
                if not isinstance(value,list):
                    raise ProviderError("catalog_shape","Formato inesperado em uma seção do catálogo.")
                data[action]=value
            except Exception as e:
                problems[action]=provider_error(e).code
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
                "poster":public_poster(account,item.get("stream_icon") or item.get("cover")),
                "plot":str(item.get("plot") or "")[:800],"ext":ext,
                "added":str(item.get("added") or "")[:25]})
    stream_actions=("get_live_streams","get_vod_streams","get_series")
    if all(act in problems for act in stream_actions):
        if all(problems[act]=="upstream_forbidden" for act in stream_actions):
            raise ProviderError("catalog_forbidden","Conta autenticada, mas o provedor recusou o catálogo (HTTP 403).")
        raise ProviderError("catalog_unavailable","Conta autenticada, mas nenhuma seção do catálogo respondeu.")
    account.items=output
    account.at=time.monotonic()
    account.issues=problems
    return output

def get_catalog(account):
    with account.catalog_lock:
        if account.items is not None and time.monotonic()-account.at<1800:
            return account.items
        if not account.verified:
            authenticate(account)
        with CATALOG_SLOT:
            return build_catalog(account)

def token_for(account,url):
    media_url(url)
    token=secrets.token_urlsafe(24)
    with LOCK:
        now=time.monotonic()
        while account.tokens:
            first=next(iter(account.tokens))
            if len(account.tokens)<8000 and account.tokens[first][1]>now:
                break
            del account.tokens[first]
        account.tokens[token]=(url,now+1800)
    return "/segment/"+token

def rewrite_hls(account,data,base):
    s=data.decode("utf-8-sig","replace")
    if not s.lstrip().startswith("#EXTM3U"):
        raise ProviderError("hls_invalid","A origem não retornou um manifesto HLS.")
    out=[]
    for line in s.splitlines():
        v=line.strip()
        if v and not v.startswith("#"):
            out.append(token_for(account,urljoin(base,v)))
        elif 'URI="' in line:
            out.append(re.sub(r'URI="([^"]+)"',lambda m:'URI="'+token_for(account,urljoin(base,m.group(1)))+'"',line))
        else:
            out.append(line)
    return ("\n".join(out)+"\n").encode("utf-8")

def interpret_remote(command):
    data={"model":AI_MODEL,"max_completion_tokens":100,"store":False,
          "response_format":{"type":"json_object"},
          "messages":[
              {"role":"system","content":"Extraia somente a intenção de buscar mídia. Responda em JSON com query (título buscado, até 100 caracteres) e kind (live, movie, series, radio ou any). Para outros pedidos devolva query vazia. Não invente conteúdo, URLs ou instruções."},
              {"role":"user","content":command}]}
    req=Request("https://api.openai.com/v1/chat/completions",data=json_bytes(data),
                headers={"Authorization":"Bearer "+AI_KEY,"Content-Type":"application/json"})
    with OPENER.open(req,timeout=6) as response:
        result=json.loads(response.read(65536))
    result=json.loads(result["choices"][0]["message"]["content"])
    query=result.get("query")
    kind=result.get("kind")
    if not isinstance(query,str) or len(query)>100 or kind not in ("live","movie","series","radio","any"):
        raise ValueError("Invalid assistant response")
    return {"query":query,"kind":kind}

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
        self.send_header("Content-Security-Policy","default-src 'self'; img-src 'self' https: data:; media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if extra:
            for k,v in extra.items():self.send_header(k,v)
        if length is not None:self.send_header("Content-Length",str(length))
        self.end_headers()
    def send_body(self,data,ctype="text/html; charset=utf-8",code=200,extra=None):
        self.common(code,ctype,extra,len(data))
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass
    def sendj(self,obj,status=200):
        data=json_bytes(obj)
        extra=None
        if len(data)>16384 and re.search(r"(?:^|,)\s*gzip\s*(?:,|$)",self.headers.get("Accept-Encoding",""),re.I):
            data=gzip.compress(data,compresslevel=3)
            extra={"Content-Encoding":"gzip","Vary":"Accept-Encoding"}
        return self.send_body(data,"application/json; charset=utf-8",status,extra)
    def session(self):
        jar=http.cookies.SimpleCookie()
        try:jar.load(self.headers.get("Cookie",""))
        except http.cookies.CookieError:return None
        cookie=jar.get("labela_session")
        if not cookie:return None
        with LOCK:
            session=SESSIONS.get(cookie.value)
            if session and session.expires>time.monotonic():return session
            SESSIONS.pop(cookie.value,None)
        return None
    def same_origin(self):
        source=self.headers.get("Origin")
        if not source:return self.headers.get("Sec-Fetch-Site") not in ("cross-site","same-site")
        return source=="https://"+self.headers.get("Host","")
    def gated(self):
        self.session_data=self.session()
        if not self.session_data:
            self.sendj({"error":"Sessão de testes não autorizada."},401)
            return False
        self.account=self.session_data.account
        return True
    def event(self,message):
        print("LABELA_DIAG "+message,flush=True)
        with LOCK:
            events=self.session_data.events
            events.append({"at":time.strftime("%H:%M:%S"),"message":message})
            del events[:-30]
    def fail(self,e,stage):
        err=provider_error(e)
        self.event(stage+": "+err.code)
        return self.sendj({"error":str(err),"code":err.code,"stage":stage},err.status)
    def body(self,max_len=2048):
        n=int(self.headers.get("Content-Length","0"))
        if n<1 or n>max_len:raise ValueError("Dados inválidos.")
        return json.loads(self.rfile.read(n))
    def do_POST(self):
        path=urlsplit(self.path).path
        if not self.same_origin():return self.sendj({"error":"Origem bloqueada."},403)
        if path=="/api/unlock":
            if self.session():return self.sendj({"ok":True})
            now=time.monotonic()
            try:
                attempt=self.body(512).get("code")
                if not isinstance(attempt,str):raise ValueError
                valid=hmac.compare_digest(attempt.encode(),APP_PIN.encode())
            except (AttributeError,ValueError):return self.sendj({"error":"Código inválido."},400)
            with LOCK:
                ATTEMPTS[:]=[v for v in ATTEMPTS if now-v<180]
                if not valid:
                    if len(ATTEMPTS)>=9:return self.sendj({"error":"Limite de tentativas."},429)
                    ATTEMPTS.append(now)
                    return self.sendj({"error":"Código de acesso incorreto."},401)
                for token,session in list(SESSIONS.items()):
                    if session.expires<=now:del SESSIONS[token]
                if len(SESSIONS)>=MAX_SESSIONS:
                    return self.sendj({"error":"Limite de sessões de teste. Tente novamente mais tarde."},429)
                token=secrets.token_urlsafe(36)
                SESSIONS[token]=Session(now)
            return self.send_body(json_bytes({"ok":True}),"application/json",200,
                                  {"Set-Cookie":f"labela_session={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={SESSION_TTL}"})
        if not self.gated():return
        if path=="/api/connect":
            try:
                require_https()
                inp=self.body(1500)
                user=inp.get("user")
                password=inp.get("password")
                if not isinstance(user,str) or not isinstance(password,str) or not user.strip() or not password or max(len(user),len(password))>150:
                    raise ValueError
            except ProviderError as e:return self.fail(e,"autenticação")
            except (AttributeError,ValueError):return self.sendj({"error":"Preencha usuário e senha válidos."},400)
            if not self.session_data.connect_lock.acquire(blocking=False):
                return self.sendj({"error":"Já há uma autenticação em andamento."},409)
            try:
                candidate=Account(user.strip(),password)
                try:authenticate(candidate)
                except Exception as e:return self.fail(e,"autenticação")
                self.session_data.account=candidate
                self.event("autenticação: conta confirmada")
                return self.sendj({"ok":True})
            finally:self.session_data.connect_lock.release()
        if path=="/api/assistant":
            if not AI_KEY:return self.sendj({"error":"A IA remota não está configurada."},503)
            try:
                command=self.body(1000).get("command")
                if not isinstance(command,str) or not 1<=len(command.strip())<=160:raise ValueError
            except (AttributeError,ValueError):return self.sendj({"error":"Digite um comando de até 160 caracteres."},400)
            if (any(value and value.casefold() in command.casefold()
                    for value in (self.account.user,self.account.password,DEFAULT_USER,DEFAULT_PASSWORD,APP_PIN,AI_KEY))
                    or re.search(r"https?://|senha|password|credencia|usu[aá]rio|token|api[_ -]?key",command,re.I)):
                return self.sendj({"error":"Use apenas o nome do conteúdo e o comando de busca."},400)
            if not AI_SLOT.acquire(blocking=False):
                return self.sendj({"error":"Assistente ocupado. Use a busca local."},429)
            try:
                now=time.monotonic()
                if now-self.session_data.last_ai<4:
                    return self.sendj({"error":"Aguarde alguns segundos antes de tentar novamente."},429)
                self.session_data.last_ai=now
                return self.sendj(interpret_remote(command))
            except HTTPError as e:
                e.close()
                return self.sendj({"error":"A IA remota está indisponível. Use a busca local."},502)
            except (ValueError,OSError,KeyError,IndexError,TypeError):
                return self.sendj({"error":"A IA remota está indisponível. Use a busca local."},502)
            finally:AI_SLOT.release()
        return self.sendj({"error":"Rota inexistente."},404)
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=="/health":return self.sendj({"ok":True,"service":"labela-play-mobile","revision":"isolated-xtream","brandConfigured":bool(BRAND_ICON)})
        if path=="/login":
            html=b'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Labela Play | Acesso</title><style>body{background:#071326;color:#f6f7ff;font:16px system-ui;min-height:93vh;display:grid;place-items:center}main{padding:28px;background:#102543;border:1px solid #375d83;border-radius:19px;width:min(84vw,380px)}h1{letter-spacing:-1px}em{color:#ffc338;font-style:normal}input,button{width:100%;padding:15px;border-radius:11px;margin-top:12px;box-sizing:border-box;font:inherit}input{background:#07192e;border:1px solid #53769d;color:white}button{background:#ffc338;color:#101c31;border:0;font-weight:800}p{color:#bacce1;font-size:13px}</style><main><h1>LABELA <em>PLAY 2.0</em></h1><p>Acesso ao ambiente de teste privado. Este codigo e diferente da senha IPTV.</p><form id="f"><input id="pin" type="password" autocomplete="off" placeholder="Codigo de acesso ao teste" required><button>Entrar</button></form><p id="m"></p></main><script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();try{const r=await fetch("/api/unlock",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:document.getElementById("pin").value})});const d=await r.json();if(!r.ok)throw Error(d.error||"Erro de acesso");location.href="/";}catch(e){document.getElementById("m").textContent=e.message}}</script></html>'''
            return self.send_body(html)
        if path in ("/","/index.html") and not self.session():
            self.send_response(303)
            self.send_header("Location","/login")
            self.send_header("Cache-Control","no-store")
            self.end_headers()
            return
        if not self.gated():return
        if path=="/brand/icon.png":
            if not BRAND_ICON:return self.sendj({"error":"Marca não configurada."},404)
            return self.send_body(BRAND_ICON,"image/png")
        if path=="/api/catalog":
            if not self.account.user or not self.account.password:
                return self.sendj({"connected":False,"items":[],"state":"credentials_missing"})
            try:
                items=get_catalog(self.account)
                return self.sendj({"connected":True,"items":items,"count":len(items),"partial":bool(self.account.issues)})
            except Exception as e:
                return self.fail(e,"catálogo" if self.account.verified else "autenticação")
        if path=="/api/account":
            return self.sendj({"configured":bool(self.account.user and self.account.password),
                               "secureUpstream":origin.scheme=="https","aiConfigured":bool(AI_KEY)})
        if path=="/api/events":
            with LOCK:events=list(self.session_data.events)
            return self.sendj({"events":events,"network":NETWORK_STATUS.copy()})
        if path=="/api/epg":
            if not self.account.verified:return self.sendj({"error":"Autentique a lista primeiro."},409)
            sid=parse_qs(urlsplit(self.path).query).get("id",[""])[0]
            if not sid.isdecimal():return self.sendj({"error":"Canal inválido."},400)
            try:
                j=xtream(self.account,"get_short_epg",{"stream_id":sid,"limit":"7"})
                rows=j.get("epg_listings") if isinstance(j,dict) else None
                epg=[]
                for row in (rows or [])[:7]:
                    if not isinstance(row,dict):continue
                    title=str(row.get("title") or "")[:512]
                    try:title=base64.b64decode(title,validate=True).decode("utf-8")
                    except ValueError:pass
                    epg.append({"start":str(row.get("start") or "")[:32],"end":str(row.get("end") or "")[:32],"title":title})
                return self.sendj({"epg":epg})
            except Exception as e:return self.fail(e,"consulta")
        if path=="/api/series":
            sid=parse_qs(urlsplit(self.path).query).get("id",[""])[0]
            if not sid.isdecimal():return self.sendj({"error":"Série inválida."},400)
            try:
                items=get_catalog(self.account)
                if not any(i["kind"]=="series" and i["id"]==sid for i in items):return self.sendj({"error":"Série fora do catálogo."},404)
                j=xtream(self.account,"get_series_info",{"series_id":sid})
                output=[]
                for s,episodes in (j.get("episodes") or {}).items():
                    for ep in episodes:
                        if not isinstance(ep,dict):continue
                        eid=str(ep.get("id") or "")
                        if not eid.isdecimal():continue
                        ext=re.sub("[^a-z0-9]","",str(ep.get("container_extension") or "mp4").lower())[:7]
                        output.append({"id":eid,"season":str(s)[:10],"number":str(ep.get("episode_num") or "")[:10],
                                       "name":str(ep.get("title") or "Episódio")[:150],"ext":ext})
                with LOCK:self.account.series[sid]={e["id"] for e in output}
                return self.sendj({"episodes":output,"plot":str((j.get("info") or {}).get("plot") or "")[:1000]})
            except Exception as e:return self.fail(e,"consulta")
        if path.startswith("/media/"):
            match=re.fullmatch(r"/media/(live|movie|series)/(\d+)\.([a-zA-Z0-9]{2,8})",path)
            if not match:return self.sendj({"error":"Mídia inválida."},400)
            kind,sid,ext=match.groups()
            if ext not in ("m3u8","ts","mp4","mkv","avi","mp3","aac","mov"):return self.sendj({"error":"Formato não autorizado."},400)
            try:
                data=get_catalog(self.account)
                if kind!="series" and not any(i["kind"]==kind and i["id"]==sid for i in data):
                    return self.sendj({"error":"Conteúdo fora do catálogo."},404)
                with LOCK:known_episode=any(sid in ids for ids in self.account.series.values())
                if kind=="series" and not known_episode:
                    return self.sendj({"error":"Selecione o episódio primeiro."},404)
                uri=f"{PROVIDER}/{kind}/{quote(self.account.user,safe='')}/{quote(self.account.password,safe='')}/{sid}.{ext}"
                return self.stream(uri,ext=="m3u8")
            except Exception as e:return self.fail(e,"consulta")
        if path.startswith("/segment/"):
            token=path[len("/segment/"):]
            with LOCK:entry=self.account.tokens.get(token)
            if not entry or time.monotonic()>entry[1]:return self.sendj({"error":"Segmento expirado."},404)
            return self.stream(entry[0],False)
        if path=="/":
            path="/index.html"
        if path!="/index.html":return self.sendj({"error":"Não encontrado."},404)
        try:
            html=Path(__file__).with_name("index.html").read_bytes()
            if BRAND_ICON:
                html=html.replace(b"<!-- BRAND_FAVICON -->",b'<link rel="icon" href="/brand/icon.png" type="image/png">')
                html=html.replace(b"<!-- BRAND_ICON -->",b'<img class="brand-icon" src="/brand/icon.png" width="40" height="40" alt="">')
            return self.send_body(html)
        except OSError:
            return self.sendj({"error":"Interface não encontrada."},500)
    def stream(self,url,force=False):
        started=False
        try:
            rng=self.headers.get("Range")
            with upstream(url,rng) as resp:
                typ=resp.headers.get("Content-Type","application/octet-stream")
                is_hls=force or "mpegurl" in typ.lower() or urlsplit(resp.url).path.lower().endswith(".m3u8")
                if is_hls:
                    data=resp.read(4*1024*1024+1)
                    if len(data)>4*1024*1024:raise ProviderError("hls_limit","Playlist HLS excede o limite.")
                    return self.send_body(rewrite_hls(self.account,data,resp.url),"application/vnd.apple.mpegurl")
                mime=typ.split(";",1)[0].strip().lower()
                if not (mime.startswith(("video/","audio/")) or mime=="application/octet-stream"):
                    raise ProviderError("media_type","A origem não enviou um tipo de mídia aceito pelo player.")
                extra={}
                for h in ("Content-Length","Content-Range","Accept-Ranges"):
                    v=resp.headers.get(h)
                    if v:extra[h]=v
                self.common(resp.status,mime,extra)
                started=True
                while True:
                    block=resp.read(64*1024)
                    if not block:break
                    self.wfile.write(block)
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception as e:
            if not started:return self.fail(e,"reprodução")
            self.event("reprodução interrompida: "+provider_error(e).code)

if __name__=="__main__":
    print("Labela Play 2.0 Mobile - gateway de testes",flush=True)
    threading.Thread(target=probe_provider_network,daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0",PORT),Handler).serve_forever()
