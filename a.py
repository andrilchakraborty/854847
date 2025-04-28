# tikify.py
import os
import json
import secrets
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import uvicorn

import requests
import jwt
from passlib.context import CryptContext

from fastapi import FastAPI, Request, Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

# ─── Config ────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tikify.db")
USERS_FILE   = "users.json"
INVITES_FILE = "invites.json"

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

# ─── SQLAlchemy setup ──────────────────────────────────────────────────────
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def now():
    return datetime.utcnow()

class DBUser(Base):
    __tablename__ = "users"
    id         = Column(Integer, primary_key=True, index=True)
    username   = Column(String, unique=True, index=True)
    avatar     = Column(String, default="")
    fetched_at = Column(DateTime, default=now)
    posts      = relationship("Post", back_populates="user", cascade="all, delete-orphan")

class Post(Base):
    __tablename__ = "posts"
    id         = Column(Integer, primary_key=True, index=True)
    aweme_id   = Column(String, unique=True, index=True)
    text       = Column(Text)
    cover      = Column(String)
    play_url   = Column(String)
    play_count = Column(Integer, default=0)
    user_id    = Column(Integer, ForeignKey("users.id"))
    user       = relationship("DBUser", back_populates="posts")

Base.metadata.create_all(bind=engine)
logging.debug("Ensured SQL tables exist.")

# ─── FastAPI app ──────────────────────────────────────────────────────────
app = FastAPI()
templates = Jinja2Templates(directory="templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

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

# ─── Routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = None,
    type: str = Query(None, regex="^(latest|top)?$"),
    db=Depends(get_db)
):
    users = db.query(DBUser).all()

    # fetch posts when searching
    if q:
        user = db.query(DBUser).filter_by(username=q).first()
        if not user:
            user = DBUser(username=q, avatar=DEFAULT_AVATAR, fetched_at=now())
            db.add(user); db.commit(); db.refresh(user)
        else:
            user.fetched_at = now()
            db.query(Post).filter_by(user_id=user.id).delete()
            db.commit()

        all_posts, cursor = [], 0
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
            post = Post(
                aweme_id=vid,
                text=v.get("title", ""),
                cover=v.get("cover", ""),
                play_url=f"https://www.tikwm.com/video/media/play/{vid}.mp4",
                user_id=user.id
            )
            db.add(post)
        db.commit()

    # pick videos to render
    if type == "latest":
        subq = (
            db.query(Post.user_id, func.max(Post.id).label("max_id"))
              .group_by(Post.user_id)
              .subquery()
        )
        videos = db.query(Post).join(subq, Post.id == subq.c.max_id).all()

    elif type == "top":
        top_per_user = (
            db.query(Post.user_id, func.max(Post.play_count).label("max_count"))
              .group_by(Post.user_id)
              .subquery()
        )
        videos = (
            db.query(Post)
              .join(
                top_per_user,
                (Post.user_id == top_per_user.c.user_id) &
                (Post.play_count == top_per_user.c.max_count)
              )
              .order_by(Post.play_count.desc())
              .limit(50)
              .all()
        )

    else:
        videos = (
            db.query(Post)
              .filter_by(user_id=db.query(DBUser).filter_by(username=q).first().id)
              .all()
            if q else []
        )

    return templates.TemplateResponse("index.html", {
        "request":     request,
        "users":       users,
        "user_videos": videos,
        "hd_urls":     HD_URLS,
        "active_q":    q or "",
        "view_type":   type or "",
    })

@app.get("/download")
async def download(video_id: str, hd: int = 0, db=Depends(get_db)):
    post = db.query(Post).filter_by(aweme_id=video_id).first()
    if not post:
        raise HTTPException(404, "Video not found")

    url = HD_URLS.get(video_id) if hd == 1 else post.play_url
    r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
    if r.status_code != 200:
        raise HTTPException(r.status_code, "Failed to fetch video")

    post.play_count += 1
    db.commit()

    fname = f"{video_id}{'_HD' if hd else ''}.mp4"
    return StreamingResponse(
        r.raw,
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )

@app.post("/api/invite-code", status_code=201)
async def generate_invite_code():
    invites = load_json(INVITES_FILE, {})
    code = secrets.token_urlsafe(8)
    invites[code] = False
    save_json(INVITES_FILE, invites)
    return {"invite_code": code}

@app.post("/api/register", status_code=201)
async def register(data: RegisterIn):
    invites = load_json(INVITES_FILE, {})
    if data.invite_code not in invites or invites[data.invite_code]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or used invite code")
    invites[data.invite_code] = True
    save_json(INVITES_FILE, invites)
    users = load_json(USERS_FILE, {})
    if data.username in users:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username already registered")
    users[data.username] = pwd_context.hash(data.password)
    save_json(USERS_FILE, users)
    return {"msg": "Registered"}

@app.post("/api/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    users  = load_json(USERS_FILE, {})
    hashed = users.get(form_data.username)
    if not hashed or not pwd_context.verify(form_data.password, hashed):
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
async def api_users(db=Depends(get_db)):
    return JSONResponse([
        {"username": u.username, "avatar": u.avatar, "fetched_at": u.fetched_at.isoformat()}
        for u in db.query(DBUser).all()
    ])

@app.get("/api/latest")
async def api_latest(db=Depends(get_db)):
    subq = (
        db.query(Post.user_id, func.max(Post.id).label("max_id"))
          .group_by(Post.user_id)
          .subquery()
    )
    posts = db.query(Post).join(subq, Post.id == subq.c.max_id).all()
    return JSONResponse([
        {
          "aweme_id": p.aweme_id, "text": p.text, "cover": p.cover,
          "play_url": p.play_url, "hd_url": HD_URLS.get(p.aweme_id, ""),
          "username": p.user.username, "avatar": p.user.avatar
        } for p in posts
    ])

@app.get("/api/top")
async def api_top(limit: int = 20, db=Depends(get_db)):
    top_per_user = (
        db.query(Post.user_id, func.max(Post.play_count).label("max_count"))
          .group_by(Post.user_id)
          .subquery()
    )
    posts = (
        db.query(Post)
          .join(
            top_per_user,
            (Post.user_id == top_per_user.c.user_id) &
            (Post.play_count == top_per_user.c.max_count)
          )
          .order_by(Post.play_count.desc())
          .limit(limit)
          .all()
    )
    return JSONResponse([
        {
          "aweme_id":  p.aweme_id, "text": p.text, "cover": p.cover,
          "play_url": p.play_url, "hd_url": HD_URLS.get(p.aweme_id, ""),
          "username": p.user.username, "avatar": p.user.avatar,
          "play_count": p.play_count
        } for p in posts
    ])

# ─── NEW: bump view count when video is played ─────────────────────────────
@app.post("/api/view/{video_id}")
async def api_view(video_id: str, db=Depends(get_db)):
    post = db.query(Post).filter_by(aweme_id=video_id).first()
    if not post:
        raise HTTPException(404, "Video not found")
    post.play_count += 1
    db.commit()
    return {"play_count": post.play_count}

# ---- Run ----
def start():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    start()
