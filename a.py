import os
import json
import secrets
import logging
from datetime import datetime, timedelta
from typing import Dict, List

import requests
import jwt
from passlib.context import CryptContext

from fastapi import FastAPI, Request, Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

# ─── Config ────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

USERS_FILE   = "users.json"
INVITES_FILE = "invites.json"
POSTS_FILE   = "posts.json"

# ─── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [DEBUG] %(message)s')

# ─── Helpers ───────────────────────────────────────────────────────────────
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

# ─── Password hashing ─────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ─── Pydantic Models ───────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    username:    str
    password:    str
    invite_code: str

class Token(BaseModel):
    access_token: str
    token_type:   str

# ─── Globals ───────────────────────────────────────────────────────────────
HD_URLS: Dict[str, str] = {}
HEADERS = {
    "User-Agent": "Mozilla/5.0 ...",
    "Accept-Encoding": "identity"
}
DEFAULT_AVATAR = (
    "https://media.discordapp.net/attachments/1343576085098664020/"
    "1366204471633510530/IMG_20250427_190832_902.jpg?format=webp"
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

    # fetch posts when searching
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
            data = resp.get("data", {})
            vids = data.get("videos", [])
            if not vids:
                break
            all_posts.extend(vids)
            if not data.get("has_more", False):
                break
            cursor = data.get("cursor", cursor)
        all_posts = all_posts[:100]

        for v in all_posts:
            vid = v["video_id"]
            HD_URLS[vid] = f"https://www.tikwm.com/video/media/hdplay/{vid}.mp4"
            posts[q].append({
                "aweme_id": vid,
                "text": v.get("title", ""),
                "cover": v.get("cover", ""),
                "play_url": f"https://www.tikwm.com/video/media/play/{vid}.mp4",
                "play_count": 0
            })
        save_posts(posts)
        save_users(users)

    # pick videos to render
    videos = []
    if type == "latest":
        for user, user_posts in posts.items():
            if user_posts:
                videos.append(user_posts[0])
    elif type == "top":
        top_list = []
        for user, user_posts in posts.items():
            if user_posts:
                top_post = max(user_posts, key=lambda p: p.get("play_count", 0))
                top_list.append(top_post)
        videos = sorted(top_list, key=lambda p: p.get("play_count", 0), reverse=True)[:50]
    else:
        if q:
            videos = posts.get(q, [])

    return templates.TemplateResponse("index.html", {
        "request":     request,
        "users":       users,
        "user_videos": videos,
        "hd_urls":     HD_URLS,
        "active_q":    q or "",
        "view_type":   type or "",
    })

@app.get("/download")
async def download(video_id: str, hd: int = 0):
    posts = load_posts()
    found = None
    for user, user_posts in posts.items():
        for post in user_posts:
            if post["aweme_id"] == video_id:
                found = post
                break
        if found:
            break
    if not found:
        raise HTTPException(404, "Video not found")

    url = HD_URLS.get(video_id) if hd == 1 else found["play_url"]
    r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
    if r.status_code != 200:
        raise HTTPException(r.status_code, "Failed to fetch video")

    found["play_count"] = found.get("play_count", 0) + 1
    save_posts(posts)

    fname = f"{video_id}{'_HD' if hd else ''}.mp4"
    return StreamingResponse(
        r.raw,
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )

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
    if data.username in users and users[data.username].get("password") != "":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username already registered")
    users[data.username] = {
        "password": pwd_context.hash(data.password),
        "avatar": DEFAULT_AVATAR,
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
        {"sub": form_data.username, "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
        SECRET_KEY, algorithm=ALGORITHM
    )
    return {"access_token": token, "token_type": "bearer"}

@app.get("/api/users")
async def api_users():
    users = load_users()
    return JSONResponse([
        {"username": u, "avatar": users[u].get("avatar", ""), "fetched_at": users[u].get("fetched_at")} for u in users
    ])

@app.get("/api/latest")
async def api_latest():
    posts = load_posts()
    result = []
    for user, user_posts in posts.items():
        if user_posts:
            p = user_posts[0]
            result.append({
                "aweme_id": p["aweme_id"],
                "text": p["text"],
                "cover": p["cover"],
                "play_url": p["play_url"],
                "hd_url": HD_URLS.get(p["aweme_id"], ""),
                "username": user,
                "avatar": load_users()[user].get("avatar", "")
            })
    return JSONResponse(result)

@app.get("/api/top")
async def api_top(limit: int = 20):
    posts = load_posts()
    top_list = []
    for user, user_posts in posts.items():
        if user_posts:
            top_list.append(max(user_posts, key=lambda p: p.get("play_count", 0)))
    sorted_list = sorted(top_list, key=lambda p: p.get("play_count", 0), reverse=True)[:limit]
    return JSONResponse([
        {
          "aweme_id":  p["aweme_id"],
          "text": p["text"],
          "cover": p["cover"],
          "play_url": p["play_url"],
          "hd_url": HD_URLS.get(p["aweme_id"], ""),
          "username": next(u for u, ps in load_posts().items() if p in ps),
          "avatar": load_users()[next(u for u, ps in load_posts().items() if p in ps)].get("avatar", ""),
          "play_count": p.get("play_count", 0)
        } for p in sorted_list
    ])

@app.post("/api/view/{video_id}")
async def api_view(video_id: str):
    posts = load_posts()
    for user, user_posts in posts.items():
        for p in user_posts:
            if p["aweme_id"] == video_id:
                p["play_count"] = p.get("play_count", 0) + 1
                save_posts(posts)
                return {"play_count": p["play_count"]}
    raise HTTPException(404, "Video not found")

def start():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    start()
