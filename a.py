import os 
import json
import secrets
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from urllib.parse import urlparse
import asyncio
import httpx
import requests

from jose import jwt
from passlib.context import CryptContext

from fastapi import APIRouter, HTTPException, Query, FastAPI, Request, Depends, status
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates

from pydantic import HttpUrl, BaseModel

# ─── Config ────────────────────────────────────────────────────────────────

SERVICE_URL = "https://bop-central.onrender.com"

DATA_DIR        = os.getenv("DATA_DIR", "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

SECRET_KEY      = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
ALGORITHM       = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

USERS_FILE      = os.path.join(DATA_DIR, "users.json")
INVITES_FILE    = os.path.join(DATA_DIR, "invites.json")
POSTS_FILE      = os.path.join(DATA_DIR, "posts.json")
SAVED_URLS_FILE = os.path.join(DATA_DIR, "saved_urls.json")
SAVED_USER_URLS_FILE = os.path.join(DATA_DIR, "saved_user_urls.json")

router = APIRouter()

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [DEBUG] %(message)s')

# ─── Helpers ────────────────────────────────────────────────────────────────

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ─── Persistence ────────────────────────────────────────────────────────────

def load_users() -> Dict[str, dict]:
    return load_json(USERS_FILE, {})

def save_users(users: Dict[str, dict]):
    save_json(USERS_FILE, users)

def load_invites() -> dict:
    return load_json(INVITES_FILE, {})

def save_invites(invites: dict):
    save_json(INVITES_FILE, invites)

def load_posts() -> Dict[str, list]:
    return load_json(POSTS_FILE, {})

def save_posts(posts: Dict[str, list]):
    save_json(POSTS_FILE, posts)

def load_saved_urls() -> List[dict]:
    return load_json(SAVED_URLS_FILE, [])

def save_saved_urls(urls: List[dict]):
    save_json(SAVED_URLS_FILE, urls)

def load_saved_user_urls() -> List[dict]:
    return load_json(SAVED_USER_URLS_FILE, [])

def save_saved_user_urls(urls: List[dict]):
    save_json(SAVED_USER_URLS_FILE, urls)

# ─── Password hashing ───────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ─── Pydantic Models ────────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    username:    str
    password:    str
    invite_code: str

class Token(BaseModel):
    access_token: str
    token_type:   str

class URLIn(BaseModel):
    url: str

class SavedURL(BaseModel):
    aweme_id: str
    play_url: str
    hd_url:    str

class UserIn(BaseModel):
    username: str

class SavedUserURL(BaseModel):
    username: str
    aweme_id: str
    play_url: str
    hd_url:    str

# ─── Globals ───────────────────────────────────────────────────────────────

HD_URLS: Dict[str, str] = {}
HEADERS = {
    "User-Agent": "Mozilla/5.0 ...",
    "Accept-Encoding": "identity"
}
DEFAULT_AVATAR = (
    "https://media.discordapp.net/attachments/1343576085098664020/"
    "1366204471633510530/...jpg"
)

# ─── Utility ───────────────────────────────────────────────────────────────

def now_iso():
    return datetime.utcnow().isoformat()

# ─── FastAPI app ──────────────────────────────────────────────────────────

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ─── Routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = None,
    type: str = Query(None, regex="^(latest|top)?$")
):
    users = load_users()
    posts = load_posts()

    if q:
        if q not in users:
            users[q] = {"password": "", "avatar": DEFAULT_AVATAR, "fetched_at": now_iso()}
        else:
            users[q]["fetched_at"] = now_iso()
        posts[q] = []

        all_posts = []
        cursor = 0
        while len(all_posts) < 100:
            resp = requests.get(
                "https://www.tikwm.com/api/user/posts",
                params={"unique_id": q, "count": 50, "cursor": cursor},
                headers=HEADERS, timeout=10
            ).json()
            if resp.get("msg") != "success":
                break
            vids = resp.get("data", {}).get("videos", [])
            if not vids:
                break
            all_posts.extend(vids)
            if not resp["data"].get("has_more", False):
                break
            cursor = resp["data"].get("cursor", cursor)

        for v in all_posts[:100]:
            vid = v["video_id"]
            HD_URLS[vid] = f"https://www.tikwm.com/video/media/hdplay/{vid}.mp4"
            posts[q].append({
                "aweme_id":   vid,
                "text":       v.get("title", ""),
                "cover":      v.get("cover", ""),
                "play_url":   f"https://www.tikwm.com/video/media/play/{vid}.mp4",
                "play_count": 0
            })
        save_posts(posts)
        save_users(users)

    videos = []
    if type == "latest":
        for ups in posts.values():
            if ups:
                videos.append(ups[0])
    elif type == "top":
        top_list = [max(ups, key=lambda p: p.get("play_count",0)) for ups in posts.values() if ups]
        videos = sorted(top_list, key=lambda p: p.get("play_count",0), reverse=True)[:50]
    elif q:
        videos = posts.get(q, [])

    return templates.TemplateResponse("index.html", {
        "request":     request,
        "users":       users,
        "user_videos": videos,
        "hd_urls":     HD_URLS,
        "active_q":    q or "",
        "view_type":   type or "",
    })

# ─── API Endpoints ─────────────────────────────────────────────────────────

@app.get("/api/saved-user-urls")
async def get_saved_user_urls():
    return JSONResponse(load_saved_user_urls())

@app.post("/api/saved-user-urls", status_code=201)
async def post_saved_user_url(data: SavedUserURL):
    saved = load_saved_user_urls()
    if not any(u["username"] == data.username and u["aweme_id"] == data.aweme_id for u in saved):
        saved.insert(0, data.dict())
        save_saved_user_urls(saved)
    return JSONResponse(data.dict())

@app.delete("/api/saved-user-urls/{username}/{aweme_id}", status_code=204)
async def delete_saved_user_url(username: str, aweme_id: str):
    saved = load_saved_user_urls()
    filtered = [
        u for u in saved
        if not (u["username"] == username and u["aweme_id"] == aweme_id)
    ]
    save_saved_user_urls(filtered)
    return JSONResponse(status_code=204, content={})

@app.get("/download")
async def download(video_id: str, hd: int = 0):
    posts = load_posts()
    found = None
    for ups in posts.values():
        for p in ups:
            if p["aweme_id"] == video_id:
                found = p
                break
        if found:
            break
    if not found:
        raise HTTPException(404, "Video not found")

    url = HD_URLS.get(video_id) if hd else found["play_url"]
    r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
    if r.status_code != 200:
        raise HTTPException(r.status_code, "Failed to fetch video")

    found["play_count"] = found.get("play_count", 0) + 1
    save_posts(posts)

    fname = f"{video_id}{'_HD' if hd else ''}.mp4"
    return StreamingResponse(r.raw, media_type="video/mp4",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})

@app.post("/api/invite-code", status_code=201)
async def generate_invite_code():
    invites = load_invites()
    code = secrets.token_urlsafe(8)
    invites[code] = False
    save_invites(invites)
    return {"invite_code": code}

@app.post("/api/register", status_code=201)
async def register(data: RegisterIn):
    invites = load_invites()
    if data.invite_code not in invites or invites[data.invite_code]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or used invite code")
    invites[data.invite_code] = True
    save_invites(invites)

    users = load_users()
    if data.username in users and users[data.username].get("password"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username already registered")
    users[data.username] = {
        "password":   pwd_context.hash(data.password),
        "avatar":     DEFAULT_AVATAR,
        "fetched_at": now_iso()
    }
    save_users(users)
    return {"msg": "Registered"}

@app.post("/api/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    users = load_users()
    user = users.get(form_data.username)
    if not user or not pwd_context.verify(form_data.password, user.get("password","")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Incorrect username or password",
                            headers={"WWW-Authenticate":"Bearer"})
    token = jwt.encode(
        {"sub": form_data.username,
         "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
         SECRET_KEY, algorithm=ALGORITHM
    )
    return {"access_token": token, "token_type": "bearer"}

@app.get("/ping")
async def ping():
    return {"status": "alive"}

# ←── MODIFIED from_url endpoint ────────────────────────────────────────────
@app.post("/api/from-url")
async def from_url(payload: URLIn):
    try:
        # first resolve the URL
        r = requests.get(payload.url, headers=HEADERS, timeout=10, allow_redirects=True)
        data = r.json()  # TikWM returns a JSON including "images" list
    except Exception as e:
        raise HTTPException(400, f"Failed to fetch metadata: {e}")

    # extract video ID as before
    parts = [p for p in urlparse(r.url).path.split("/") if p]
    aweme_id = None
    for i,p in enumerate(parts):
        if p == "video" and i+1 < len(parts):
            aweme_id = parts[i+1]
            break
    if not aweme_id:
        raise HTTPException(400, "Could not extract video ID from URL")

    play_url = f"https://www.tikwm.com/video/media/play/{aweme_id}.mp4"
    hd_url   = f"https://www.tikwm.com/video/media/hdplay/{aweme_id}.mp4"
    HD_URLS[aweme_id] = hd_url

    # **NEW**: download any 'images' in the JSON
    image_paths = []
    if "images" in data and data["images"]:
        # ensure download dir
        dl_dir = f"Downloads/{payload.url.split('/')[-1]}"
        os.makedirs(dl_dir, exist_ok=True)
        for i, image_url in enumerate(data["images"]):
            image_file_path = os.path.join(dl_dir, f"{aweme_id}_{i+1}.jpg")
            img_r = requests.get(image_url, headers=HEADERS, stream=True)
            if img_r.status_code == 200:
                with open(image_file_path, 'wb') as f:
                    for chunk in img_r.iter_content(chunk_size=1024):
                        f.write(chunk)
                logging.debug("Downloaded image %d for video %s to %s", i+1, aweme_id, image_file_path)
                image_paths.append(image_file_path)
            else:
                logging.error("Failed to download image %d for video %s: HTTP %s", i+1, aweme_id, img_r.status_code)

    # return both video URLs and downloaded image paths
    result = {"aweme_id": aweme_id, "play_url": play_url, "hd_url": hd_url}
    if image_paths:
        result["images"] = image_paths if len(image_paths) > 1 else image_paths[0]
    return JSONResponse(result)

# ─── include router, startup, etc. ──────────────────────────────────────────

app.include_router(router)

def start():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT",8000)))

if __name__ == "__main__":
    start()
