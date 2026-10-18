"""Self-hosted Withings -> openGym bridge; LAN-only routing is enforced by Traefik."""
from __future__ import annotations
import asyncio
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jwt import PyJWKClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from . import store
from .sync import decode_groups, merge_bodyweight

store.prepare()
PUBLIC = os.environ.get("PUBLIC_URL", "https://opengym-withings.home.glockb.de").rstrip("/")
PARSED = urlparse(PUBLIC)
if PARSED.scheme != "https" or not PARSED.hostname or PARSED.username or PARSED.password:
    raise RuntimeError("PUBLIC_URL must be a HTTPS domain")
API = os.environ.get("OPENGYM_INTERNAL_URL", "http://opengym-api-1:3000").rstrip("/")
# Fixed internal Docker DNS names only: do not create an SSRF gadget from the WebUI.
if API != "http://opengym-api-1:3000":
    raise RuntimeError("OPENGYM_INTERNAL_URL must be http://opengym-api-1:3000")
client_timeout = httpx.Timeout(25.0)
lock = asyncio.Lock()


def require_admin(request: Request):
    if not store.get("oidc_issuer") or not request.session.get("subject"):
        raise HTTPException(401, "Pocket-ID-Anmeldung erforderlich")
    if time.time() - request.session.get("authenticated_at", 0) > 8 * 3600:
        request.session.clear()
        raise HTTPException(401, "Sitzung abgelaufen")


def safe_issuer(raw: str) -> str:
    parsed = urlparse(raw.strip().rstrip("/"))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
        or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise HTTPException(400, "OIDC-Issuer muss eine HTTPS-Domain ohne Pfad sein")
    return raw.strip().rstrip("/")


def same_origin(request: Request):
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    origin = request.headers.get("origin")
    if origin != PUBLIC:
        raise HTTPException(403, "Origin-Prüfung fehlgeschlagen")


def status():
    rows = store.measures()
    latest = {}
    for row in reversed(rows):
        for field in ("weight_kg", "fat_pct", "muscle_kg", "fat_kg", "fat_free_kg", "hydration_kg", "bone_kg"):
            if field not in latest and field in row:
                latest[field] = row[field]
    return {
        "oidc": bool(store.get("oidc_client_id") and store.secret_get("oidc_client_secret")),
        "issuer": store.get("oidc_issuer", "https://id.glockb.de"),
        "allowed_group": store.get("oidc_group", "admin"),
        "withings_app": bool(store.get("withings_client_id") and store.secret_get("withings_client_secret")),
        "withings": bool(store.secret_get("withings_token")),
        "withings_client_id": store.get("withings_client_id"),
        "opengym": bool(store.secret_get("opengym_token")),
        "opengym_user": store.get("opengym_user"),
        "interval_minutes": int(store.get("interval_minutes", "60")),
        "history_days": int(store.get("history_days", "3650")),
        "sync_enabled": store.get("sync_enabled") == "true",
        "prefer_withings": store.get("prefer_withings", "true") == "true",
        "last_sync": store.get("last_sync"),
        "last_result": store.get("last_result"),
        "measures_count": len(rows),
        "latest": latest,
        "recent": rows[-14:],
        "logs": store.recent_logs(),
        "tz": os.environ.get("TZ", "Europe/Berlin"),
    }


def checked_int(value, low, high, field):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Ungültiges Feld: {field}") from None
    if not low <= n <= high:
        raise HTTPException(400, f"{field}: nur {low} bis {high}")
    return n


async def oidc_metadata():
    issuer = safe_issuer(store.get("oidc_issuer"))
    async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=False) as client:
        resp = await client.get(issuer + "/.well-known/openid-configuration")
        resp.raise_for_status()
        meta = resp.json()
    if meta.get("issuer") != issuer:
        raise HTTPException(502, "OIDC-Issuer stimmt nicht mit Discovery überein")
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        parsed = urlparse(meta.get(k, ""))
        if parsed.scheme != "https" or parsed.hostname != urlparse(issuer).hostname:
            raise HTTPException(502, f"Ungültiger OIDC-Endpunkt: {k}")
    return meta


async def withings_token(grant_type: str, **kwargs):
    form = {"action": "requesttoken", "client_id": store.get("withings_client_id"),
            "client_secret": store.secret_get("withings_client_secret"), "grant_type": grant_type, **kwargs}
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        response = await client.post("https://wbsapi.withings.net/v2/oauth2", data=form)
        response.raise_for_status()
        body = response.json()
    if body.get("status") != 0 or not body.get("body", {}).get("access_token"):
        raise RuntimeError("Withings-Tokenanforderung fehlgeschlagen (Status %s)" % body.get("status"))
    token = body["body"]
    data = {
        "access_token":token["access_token"], "refresh_token":token["refresh_token"],
        "expires_at":int(time.time())+int(token.get("expires_in", 10800))-120,
        "userid":str(token.get("userid", ""))
    }
    # Persist *both* updated tokens atomically in one encrypted record.
    store.secret_set("withings_token", json.dumps(data))
    return data


async def fresh_access_token():
    obj = json.loads(store.secret_get("withings_token"))
    if time.time() >= obj["expires_at"]:
        obj = await withings_token("refresh_token", refresh_token=obj["refresh_token"])
    return obj["access_token"]


async def fetch_measures():
    token = await fresh_access_token()
    since = store.get("last_fetch")
    options = {"action":"getmeas", "category":1, "meastypes":"1,5,6,8,76,77,88"}
    if since:
        options["lastupdate"] = max(0, int(since)-3600)  # overlap catches edited values
    else:
        options["startdate"] = int(time.time()) - int(store.get("history_days", "3650")) * 86400
    rows = []
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        for _ in range(250):  # bounded pagination
            response = await client.post("https://wbsapi.withings.net/measure",
                                         data=options, headers={"Authorization":f"Bearer {token}"})
            response.raise_for_status()
            content = response.json()
            if content.get("status") != 0:
                raise RuntimeError("Withings-Messwerte: API-Status %s; Anfrage von Withings abgelehnt. Bitte API-Berechtigungen und Parameter prüfen." % content.get("status"))
            body = content.get("body", {})
            rows.extend(body.get("measuregrps", []))
            if not body.get("more"):
                break
            offset = body.get("offset")
            if offset is None:
                raise RuntimeError("Withings meldet weitere Ergebnisse ohne Offset")
            options["offset"] = offset
        else:
            raise RuntimeError("Withings-Paginierung zu groß; nichts gespeichert")
    return decode_groups(rows, os.environ.get("TZ", "Europe/Berlin"))


async def read_opengym(client, token):
    r = await client.get(API+"/api/data", headers={"Authorization":f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


async def write_opengym(measurements: list[dict], dry_run: bool = False):
    bearer = store.secret_get("opengym_token")
    if not bearer:
        raise RuntimeError("openGym noch nicht verbunden")
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        # openGym refreshes paired bearer sessions after half their lifetime via /api/me.
        identity = await client.get(API + "/api/me", headers={"Authorization": f"Bearer {bearer}"})
        identity.raise_for_status()
        renewed = identity.json().get("token")
        if renewed and isinstance(renewed, str):
            bearer = renewed
            store.secret_set("opengym_token", bearer)
        for attempt in range(4):
            document = await read_opengym(client, bearer)
            current = document.get("state")
            if not isinstance(current, dict):
                raise RuntimeError("openGym-Profil hat noch keinen gespeicherten Zustand; bitte in openGym einmal Einstellungen speichern")
            updated, changes = merge_bodyweight(current, measurements, store.get("prefer_withings", "true") == "true")
            if dry_run or not (changes["added"] or changes["replaced"]):
                return changes
            payload = {"state": updated, "baseRev": document["rev"]}
            if current.get("_wid"):
                payload["baseWid"] = current["_wid"]
            response = await client.put(API+"/api/data", json=payload,
                                        headers={"Authorization":f"Bearer {bearer}"})
            if response.status_code == 409:
                continue
            response.raise_for_status()
            return changes
    raise RuntimeError("openGym-Konflikt auch nach vier Versuchen; Daten wurden nicht überschrieben")


async def perform_sync(dry_run: bool = False):
    if lock.locked():
        raise HTTPException(409, "Synchronisierung läuft bereits")
    async with lock:
        started = int(time.time())
        try:
            fetched = await fetch_measures()
            existing = {r["group_id"]:r for r in store.measures()}
            for row in fetched:
                existing[row["group_id"]] = row
            changes = await write_opengym(list(existing.values()), dry_run=dry_run)
            if not dry_run:
                store.save_measures(fetched)
                store.put("last_fetch", str(started))
                store.put("last_sync", datetime.now(timezone.utc).isoformat())
                store.put("last_result", json.dumps(changes, ensure_ascii=False))
            store.log("info", ("Vorschau" if dry_run else "Sync") + f": {changes['added']} neu, {changes['replaced']} ersetzt, {changes['unchanged']} unverändert")
            return {**changes, "fetched":len(fetched), "dry_run":dry_run}
        except Exception as exc:
            store.log("error", f"Synchronisierung fehlgeschlagen: {str(exc)[:180]}")
            raise


async def scheduler():
    while True:
        await asyncio.sleep(60)
        try:
            if store.get("sync_enabled") != "true" or not store.secret_get("withings_token") or not store.secret_get("opengym_token"):
                continue
            last = store.get("last_sync")
            last_time = datetime.fromisoformat(last).timestamp() if last else 0
            interval = int(store.get("interval_minutes", "60")) * 60
            if time.time()-last_time >= interval and not lock.locked():
                await perform_sync()
        except Exception as exc:
            store.log("error", "Automatischer Sync: " + str(exc)[:170])


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="openGym × Withings", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=(store.DATA/"session.key").read_text(),
                   session_cookie="ogw_session", https_only=True, same_site="lax", max_age=8*3600)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[PARSED.hostname, "localhost", "127.0.0.1"])
STATIC=Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_store(request: Request, call_next):
    try:
        same_origin(request)
        response = await call_next(request)
    except HTTPException as exc:
        response = JSONResponse({"error":exc.detail}, status_code=exc.status_code)
    response.headers["Cache-Control"]="no-store"
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["Referrer-Policy"]="no-referrer"
    response.headers["Content-Security-Policy"]="default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; form-action 'self'; frame-ancestors 'none'"
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC/"index.html")

@app.get("/health")
async def health():
    return {"ok":True}

@app.get("/api/initial")
async def initial(request: Request):
    configured = bool(store.get("oidc_client_id"))
    return {"configured":configured,"signed_in":bool(request.session.get("subject")),
            "user":request.session.get("name"),"issuer":store.get("oidc_issuer", "https://id.glockb.de"),
            "callback":PUBLIC+"/auth/oidc/callback", "withings_callback":PUBLIC+"/auth/withings/callback"}

@app.post("/api/setup")
async def setup(request: Request):
    if store.get("oidc_client_id"):
        raise HTTPException(403, "Einrichtung bereits abgeschlossen")
    data=await request.json()
    token=str(data.get("setup_code", ""))
    if not hmac.compare_digest(token, (store.DATA/"setup.key").read_text().strip()):
        raise HTTPException(403, "Falscher Setup-Code")
    issuer=safe_issuer(str(data.get("issuer", "")))
    clientid=str(data.get("client_id", "")).strip()
    secret=str(data.get("client_secret", ""))
    group=str(data.get("allowed_group", "admin")).strip()
    if not clientid or not secret or not group:
        raise HTTPException(400, "Client-ID, Secret und erlaubte Gruppe erforderlich")
    store.put("oidc_issuer", issuer)
    store.put("oidc_client_id", clientid)
    store.put("oidc_group", group)
    store.secret_set("oidc_client_secret", secret)
    store.log("info", "OIDC-Konfiguration eingerichtet")
    return {"ok":True}

@app.get("/auth/oidc/start")
async def login(request: Request):
    if not store.get("oidc_client_id"):
        return RedirectResponse("/")
    meta=await oidc_metadata()
    state=secrets.token_urlsafe(30)
    nonce=secrets.token_urlsafe(30)
    request.session["oidc_state"]=state
    request.session["oidc_nonce"]=nonce
    query=urlencode({"client_id":store.get("oidc_client_id"),"response_type":"code",
                     "redirect_uri":PUBLIC+"/auth/oidc/callback", "scope":"openid profile email groups",
                     "state":state,"nonce":nonce})
    return RedirectResponse(meta["authorization_endpoint"]+"?"+query, status_code=302)

@app.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = "", state: str = ""):
    if not state or not hmac.compare_digest(state, request.session.pop("oidc_state", "")) or not code:
        raise HTTPException(400, "Ungültiger OIDC-State")
    nonce=request.session.pop("oidc_nonce", "")
    meta=await oidc_metadata()
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        resp=await client.post(meta["token_endpoint"], data={"grant_type":"authorization_code", "code":code,
            "redirect_uri":PUBLIC+"/auth/oidc/callback", "client_id":store.get("oidc_client_id"),
            "client_secret":store.secret_get("oidc_client_secret")})
        resp.raise_for_status()
        token=resp.json()
    if not token.get("id_token"):
        raise HTTPException(403, "Kein OIDC-ID-Token")
    jwks=PyJWKClient(meta["jwks_uri"])
    signing_key=await asyncio.to_thread(jwks.get_signing_key_from_jwt, token["id_token"])
    claims=jwt.decode(token["id_token"], signing_key.key, algorithms=["RS256","ES256","PS256"],
                      audience=store.get("oidc_client_id"), issuer=store.get("oidc_issuer"),
                      options={"require":["iss","sub","aud","exp","iat"]})
    if not nonce or claims.get("nonce") != nonce:
        raise HTTPException(403, "OIDC-Nonce ungültig")
    groups=claims.get("groups") or []
    if not isinstance(groups, list) or store.get("oidc_group", "admin") not in groups:
        store.log("warning", "OIDC-Login verweigert: erforderliche Gruppe fehlt")
        raise HTTPException(403, "Deinem Pocket-ID-Account fehlt die freigegebene Gruppe")
    request.session.clear()
    request.session.update({"subject":claims["sub"],"name":claims.get("name") or claims.get("preferred_username") or "Administrator",
                            "authenticated_at":time.time()})
    return RedirectResponse("/", status_code=302)

@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok":True}

@app.get("/api/status")
async def get_status(request: Request):
    require_admin(request)
    return status()

@app.post("/api/config/oidc")
async def oidc_config(request: Request):
    require_admin(request)
    values=await request.json()
    if "issuer" in values:
        store.put("oidc_issuer", safe_issuer(str(values["issuer"])))
    if "client_id" in values and values["client_id"]:
        store.put("oidc_client_id",str(values["client_id"]).strip())
    if "client_secret" in values and values["client_secret"]:
        store.secret_set("oidc_client_secret",str(values["client_secret"]))
    if "allowed_group" in values and values["allowed_group"]:
        store.put("oidc_group",str(values["allowed_group"]).strip())
    store.log("info", "OIDC-Einstellungen geändert")
    return {"ok":True}

@app.post("/api/config/withings")
async def withings_config(request: Request):
    require_admin(request)
    values=await request.json()
    new_id=str(values.get("client_id", "")).strip()
    new_secret=str(values.get("client_secret", ""))
    if new_id:
        if new_id != store.get("withings_client_id"):
            store.secret_set("withings_token", "")
        store.put("withings_client_id", new_id)
    if new_secret:
        store.secret_set("withings_client_secret", new_secret)
    return {"ok":True}

@app.get("/auth/withings/start")
async def withings_start(request: Request):
    require_admin(request)
    if not store.get("withings_client_id") or not store.secret_get("withings_client_secret"):
        raise HTTPException(400, "Withings-App noch nicht eingerichtet")
    state=secrets.token_urlsafe(30)
    request.session["withings_state"] = state
    query=urlencode({"response_type":"code", "client_id":store.get("withings_client_id"),
                     "scope":"user.metrics", "redirect_uri":PUBLIC+"/auth/withings/callback", "state":state})
    return RedirectResponse("https://account.withings.com/oauth2_user/authorize2?"+query)

@app.get("/auth/withings/callback")
async def withings_callback(request: Request, code: str = "", state: str = ""):
    require_admin(request)
    if not state or not hmac.compare_digest(state,request.session.pop("withings_state", "")) or not code:
        raise HTTPException(400, "Ungültiger Withings-State")
    await withings_token("authorization_code", code=code, redirect_uri=PUBLIC+"/auth/withings/callback")
    store.log("info", "Withings-Konto erfolgreich verbunden")
    return RedirectResponse("/?connected=withings")

@app.post("/api/config/sync")
async def sync_config(request: Request):
    require_admin(request)
    value=await request.json()
    interval=checked_int(value.get("interval_minutes",60),15,1440,"Intervall")
    history=checked_int(value.get("history_days",3650),1,7300,"Historie")
    store.put("interval_minutes",interval)
    store.put("history_days",history)
    store.put("prefer_withings", "true" if value.get("prefer_withings",True) else "false")
    store.put("sync_enabled", "true" if value.get("sync_enabled",False) else "false")
    return {"ok":True}

@app.post("/api/opengym/pair")
async def pair(request: Request):
    require_admin(request)
    code=str((await request.json()).get("code", "")).strip()
    if len(code) != 8 or not code.isalnum():
        raise HTTPException(400, "8-stelligen openGym-Pairing-Code eingeben")
    async with httpx.AsyncClient(timeout=client_timeout) as client:
        resp=await client.post(API+"/api/pair/redeem", json={"code":code})
        resp.raise_for_status()
        data=resp.json()
        bearer=data.get("token", "")
        if not bearer:
            raise HTTPException(502, "openGym gab keinen Bearer-Token zurück")
        identity=await client.get(API+"/api/me", headers={"Authorization":f"Bearer {bearer}"})
        identity.raise_for_status()
        user=identity.json().get("user") or {}
    store.secret_set("opengym_token", bearer)
    store.put("opengym_user",user.get("name") or user.get("id", "Unbekannt"))
    store.log("info", "openGym-Profil verbunden")
    return {"ok":True,"user":store.get("opengym_user")}

@app.post("/api/sync/{mode}")
async def sync_api(request: Request, mode: str):
    require_admin(request)
    if mode not in ("preview", "run"):
        raise HTTPException(404, "Nicht gefunden")
    try:
        return await perform_sync(dry_run=mode=="preview")
    except RuntimeError as exc:
        # Expected integration errors are upstream failures, not internal server bugs.
        raise HTTPException(502, detail=str(exc)) from None
