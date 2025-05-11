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
from fastapi.staticfiles import StaticFiles
from fastapi import APIRouter, HTTPException, Query, FastAPI, Request, Depends, status
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

# ─── Config ────────────────────────────────────────────────────────────────

SERVICE_URL = "https://bop-central-vcbk.onrender.com/"
DATA_DIR        = os.getenv("DATA_DIR", "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

SECRET_KEY      = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
ALGORITHM       = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

USERS_FILE            = os.path.join(DATA_DIR, "users.json")
INVITES_FILE          = os.path.join(DATA_DIR, "invites.json")
POSTS_FILE            = os.path.join(DATA_DIR, "posts.json")
SAVED_URLS_FILE       = os.path.join(DATA_DIR, "saved_urls.json")
SAVED_USER_URLS_FILE  = os.path.join(DATA_DIR, "saved_user_urls.json")

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

# ─── Persistence ───────────────────────────────────────────────────────────

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

# ─── Saved URLs Persistence ─────────────────────────────────────────────────

def load_saved_urls() -> List[dict]:
    return load_json(SAVED_URLS_FILE, [])

def save_saved_urls(urls: List[dict]):
    save_json(SAVED_URLS_FILE, urls)

# ─── Saved User-URLs Persistence ───────────────────────────────────────────

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
    images:   List[str]

# ─── Globals ────────────────────────────────────────────────────────────────

HD_URLS: Dict[str, str] = {}
HEADERS = {
    "User-Agent": "Mozilla/5.0 ...",
    "Accept-Encoding": "identity"
}
DEFAULT_AVATAR = (
    "https://media.discordapp.net/attachments/1343576085098664020/"
    "1366204471633510530/IMG_20250427_190832_902.jpg?..."
)

# ─── Utility ────────────────────────────────────────────────────────────────

def now_iso():
    return datetime.utcnow().isoformat()

# ─── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── Routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = None,
    type: str = Query(None, regex="^(latest|top)?$")
):
    users = load_users()
    posts = load_posts()

    # ─── MULTIPLE USER SUPPORT ─────────────────────────────────────────────
    if q:
        usernames = [u.strip() for u in q.split(",") if u.strip()]
        if len(usernames) > 1:
            saved_user_urls = load_saved_user_urls()
            for uname in usernames:
                if uname not in users:
                    users[uname] = {
                        "password": "",
                        "avatar": DEFAULT_AVATAR,
                        "fetched_at": now_iso()
                    }
                else:
                    users[uname]["fetched_at"] = now_iso()

                all_posts = []
                cursor = 0
                while True:
                    resp = requests.get(
                        "https://www.tikwm.com/api/user/posts",
                        params={"unique_id": uname, "count": 50, "cursor": cursor},
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

                for v in all_posts:
                    vid = v["video_id"]
                    play = f"https://www.tikwm.com/video/media/play/{vid}.mp4"
                    hd   = f"https://www.tikwm.com/video/media/hdplay/{vid}.mp4"
                    imgs = v.get("images", []) or []
                    entry = {
                        "username": uname,
                        "aweme_id": vid,
                        "play_url": play,
                        "hd_url":   hd,
                        "images":   imgs
                    }
                    if not any(u["username"] == uname and u["aweme_id"] == vid for u in saved_user_urls):
                        saved_user_urls.insert(0, entry)

            save_users(users)
            save_saved_user_urls(saved_user_urls)

            return templates.TemplateResponse("index.html", {
                "request":     request,
                "users":       users,
                "user_videos": [],
                "hd_urls":     HD_URLS,
                "active_q":    q,
                "view_type":   type or "",
            })

    # ─── SINGLE USER (existing logic) ───────────────────────────────────────
    if q:
        if q not in users:
            users[q] = {
                "password": "",
                "avatar": DEFAULT_AVATAR,
                "fetched_at": now_iso()
            }
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
                "play_count": 0,
                "images":     v.get("images", [])
            })
        save_posts(posts)
        save_users(users)

    videos = []
    if type == "latest":
        for ups in posts.values():
            if ups:
                videos.append(ups[0])
    elif type == "top":
        top_list = [max(ups, key=lambda p: p.get("play_count", 0)) for ups in posts.values() if ups]
        videos = sorted(top_list, key=lambda p: p.get("play_count", 0), reverse=True)[:50]
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
    filtered = [u for u in saved if not (u["username"] == username and u["aweme_id"] == aweme_id)]
    save_saved_user_urls(filtered)
    return JSONResponse(status_code=204, content={})

# ←── NEW: Slideshow API ────────────────────────────────────────────────────
@app.get("/api/slideshow")
async def get_slideshow():
    """
    Returns a flat list of every saved image URL with its username and aweme_id.
    """
    slides = []
    for entry in load_saved_user_urls():
        uname = entry["username"]
        aid   = entry["aweme_id"]
        for img in entry.get("images", []):
            slides.append({
                "username":  uname,
                "aweme_id":  aid,
                "image_url": img
            })
    return JSONResponse(slides)

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

    username = next(u for u, ups in posts.items() if any(v["aweme_id"] == video_id for v in ups))
    if "images" in found and found["images"]:
        for i, image_url in enumerate(found["images"]):
            image_file_path = f"Downloads/{username}/{video_id}_{i+1}.jpg"
            os.makedirs(os.path.dirname(image_file_path), exist_ok=True)
            image_resp = requests.get(image_url, headers=HEADERS, stream=True)
            if image_resp.status_code == 200:
                with open(image_file_path, 'wb') as file:
                    for chunk in image_resp.iter_content(chunk_size=1024):
                        file.write(chunk)
                logging.debug("Downloaded image %d for video %s to %s", i+1, video_id, image_file_path)
            else:
                logging.error("Failed to download image %d for video %s: HTTP %s", i+1, video_id, image_resp.status_code)

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
    if not user or not pwd_context.verify(form_data.password, user.get("password", "")):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"}
        )
    token = jwt.encode(
        {"sub": form_data.username,
         "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
         SECRET_KEY, algorithm=ALGORITHM
    )
    return {"access_token": token, "token_type": "bearer"}

@app.get("/api/users")
async def api_users():
    users = load_users()
    return JSONResponse([
        {"username": u,
         "avatar": users[u].get("avatar", ""),
         "fetched_at": users[u].get("fetched_at", "")}
        for u in users
    ])

@app.delete("/api/users/{username}", status_code=204)
async def delete_user(username: str):
    users = load_users()
    if username in users:
        del users[username]
        save_users(users)
    return JSONResponse(status_code=204, content={})

@app.get("/api/latest")
async def api_latest():
    posts = load_posts()
    result = []
    users = load_users()
    for user, ups in posts.items():
        if ups:
            p = ups[0]
            result.append({
                "aweme_id": p["aweme_id"],
                "text":     p["text"],
                "cover":    p["cover"],
                "play_url": p["play_url"],
                "hd_url":   HD_URLS.get(p["aweme_id"], ""),
                "username": user,
                "avatar":   users.get(user, {}).get("avatar", "")
            })
    return JSONResponse(result)

@app.get("/api/top")
async def api_top(limit: int = 20):
    posts = load_posts()
    users = load_users()
    top = []
    for ups in posts.values():
        if ups:
            top.append(max(ups, key=lambda p: p.get("play_count", 0)))
    top = sorted(top, key=lambda p: p.get("play_count", 0), reverse=True)[:limit]
    return JSONResponse([
        {
            "aweme_id":   pc["aweme_id"],
            "text":       pc["text"],
            "cover":      pc["cover"],
            "play_url":   pc["play_url"],
            "hd_url":     HD_URLS.get(pc["aweme_id"], ""),
            "username":   next(u for u, ups in posts.items() if pc in ups),
            "avatar":     users.get(next(u for u, ups in posts.items() if pc in ups), {}).get("avatar", ""),
            "play_count": pc.get("play_count", 0)
        } for pc in top
    ])

@app.post("/api/view/{video_id}")
async def api_view(video_id: str):
    posts = load_posts()
    for ups in posts.values():
        for p in ups:
            if p["aweme_id"] == video_id:
                p["play_count"] = p.get("play_count", 0) + 1
                save_posts(posts)
                return {"play_count": p["play_count"]}
    raise HTTPException(404, "Video not found")

@app.post("/api/from-url")
async def from_url(payload: URLIn):
    try:
        r = requests.get(payload.url, headers=HEADERS, timeout=10, allow_redirects=True)
        final = r.url
    except:
        raise HTTPException(400, "Failed to resolve URL")
    parts = [p for p in urlparse(final).path.split("/") if p]
    aweme_id = None
    for i, p in enumerate(parts):
        if p == "video" and i + 1 < len(parts):
            aweme_id = parts[i + 1]
            break
    if not aweme_id:
        raise HTTPException(400, "Could not extract video ID from URL")
    try:
        info = requests.get(
            f"https://www.tikwm.com/api/?url={final}&hd=1",
            headers=HEADERS, timeout=10
        ).json()
        images = info.get("data", {}).get("images", [])
    except:
        images = []
    play_url = f"https://www.tikwm.com/video/media/play/{aweme_id}.mp4"
    hd_url   = f"https://www.tikwm.com/video/media/hdplay/{aweme_id}.mp4"
    HD_URLS[aweme_id] = hd_url
    return JSONResponse({
        "aweme_id": aweme_id,
        "play_url": play_url,
        "hd_url":   hd_url,
        "images":   images
    })

@app.get("/api/saved-urls")
async def get_saved_urls():
    return JSONResponse(load_saved_urls())

@app.post("/api/saved-urls", status_code=201)
async def post_saved_url(url_data: SavedURL):
    saved = load_saved_urls()
    if not any(u["aweme_id"] == url_data.aweme_id for u in saved):
        saved.insert(0, url_data.dict())
        save_saved_urls(saved)
    return JSONResponse(url_data.dict())

@app.delete("/api/saved-urls/{aweme_id}", status_code=204)
async def delete_saved_url(aweme_id: str):
    saved = load_saved_urls()
    saved = [u for u in saved if u["aweme_id"] != aweme_id]
    save_saved_urls(saved)
    return JSONResponse(status_code=204, content={})

@app.get("/api/saved-users")
async def get_saved_users():
    users = load_users()
    return JSONResponse([
        {
            "username":   u,
            "avatar":     users[u].get("avatar", ""),
            "fetched_at": users[u].get("fetched_at", "")
        }
        for u in users
    ])

@app.post("/api/saved-users", status_code=201)
async def post_saved_user(u: UserIn):
    users = load_users()
    if u.username not in users:
        users[u.username] = {
            "password": "", "avatar": DEFAULT_AVATAR, "fetched_at": now_iso()
        }
        save_users(users)
    return JSONResponse({"username": u.username})

@app.delete("/api/saved-users/{username}", status_code=204)
async def delete_saved_user(username: str):
    users = load_users()
    if username in users:
        del users[username]
        save_users(users)
    return JSONResponse(status_code=204, content={})

@app.get("/api/images/username/{username}")
async def get_images_by_username(username: str):
    posts = load_posts()
    if username not in posts:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    result = []
    for p in posts[username]:
        vid = p["aweme_id"]
        try:
            info = requests.get(
                f"https://www.tikwm.com/api/?url=https://www.tiktok.com/video/{vid}&hd=1",
                headers=HEADERS, timeout=10
            ).json()
            imgs = info.get("data", {}).get("images", [])
        except:
            imgs = []
        if imgs:
            result.append({"aweme_id": vid, "images": imgs})
    return JSONResponse(result)

@app.get("/api/images/url/{video_id}")
async def get_images_by_video(video_id: str):
    try:
        info = requests.get(
            f"https://www.tikwm.com/api/?url=https://www.tiktok.com/video/{video_id}&hd=1",
            headers=HEADERS, timeout=10
        ).json()
        imgs = info.get("data", {}).get("images", [])
    except:
        imgs = []
    return JSONResponse({"aweme_id": video_id, "images": imgs})

@app.on_event("startup")
async def schedule_ping_task():
    async def ping_loop():
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                try:
                    resp = await client.get(f"{SERVICE_URL}/ping")
                    if resp.status_code != 200:
                        print(f"Health ping returned {resp.status_code}")
                except Exception as e:
                    print(f"External ping failed: {e!r}")
                await asyncio.sleep(120)
    asyncio.create_task(ping_loop())

@app.get("/ping")
async def ping():
    return {"status": "alive"}

app.include_router(router)

def start():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    start()
