
import os, re, time, datetime
import feedparser
from urllib.parse import urlparse
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, Image
from moviepy.editor import AudioFileClip, ImageClip, ColorClip, CompositeVideoClip
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# === 設定 ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY","")   # 任意（空でも動く）
FEED_URL       = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"
VIDEO_W, VIDEO_H = 1080, 1920
MAX_DURATION   = 45
MIN_DURATION   = 16
BG_COLOR       = (18, 18, 18)
TITLE_MAX_CHAR = 18
FPS            = 25
BITRATE        = "2500k"
YT_SCOPES      = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILE     = "yt_token.json"
CLIENT_FILE    = "client_secret.json"
OUTPUT_DIR     = "outputs"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- フォント検出（Noto JP優先） ---
def find_jp_font():
    cands=[
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
        "C:/Windows/Fonts/meiryo.ttc","C:/Windows/Fonts/YuGothM.ttc",
    ]
    for root,_,files in os.walk("/usr/share/fonts"):
        for f in files:
            if ("Noto" in f or "Meiryo" in f or "Goth" in f) and f.lower().endswith((".ttf",".otf",".ttc")):
                cands.insert(0, os.path.join(root,f))
    for p in cands:
        if os.path.exists(p): return p
    raise RuntimeError("日本語フォントが見つかりません。")
FONT_PATH = find_jp_font()
_dummy = Image.new("RGB",(10,10)); _draw = ImageDraw.Draw(_dummy)
def text_size(txt, font):
    b = _draw.textbbox((0,0), txt, font=font); return (b[2]-b[0], b[3]-b[1])

# --- RSS ---
def fetch_latest(url):
    feed = feedparser.parse(url)
    es = list(getattr(feed,"entries",[]))
    es.sort(key=lambda e: e.get("published_parsed", time.gmtime(0)), reverse=True)
    return es[0] if es else None

# --- 整形＆簡易リライト ---
def clean_text(s):
    import re
    if not s: return ""
    s = re.sub(r"<[^>]+>","",s); s = re.sub(r"&[^;]+;"," ",s)
    return re.sub(r"\s+"," ",s).strip()

def simple_rewrite(title, summary, link):
    host = urlparse(link).netloc.replace("www.","")
    s = clean_text(summary or title)[:280]
    lines = ["ポイントだけ簡単に。"]
    for i in range(0,len(s),16): lines.append(s[i:i+16])
    lines.append("続報は出典を確認してね。"); lines.append(f"出典: {host}")
    return "\n".join(lines)

# --- キャプション画像 ---
def wrap_by_width(text, font, max_w):
    lines=[]
    for para in text.splitlines():
        t=para.strip()
        if not t: lines.append(""); continue
        buf=""
        for ch in t:
            w,_=text_size(buf+ch,font)
            if w<=max_w: buf+=ch
            else:
                if buf: lines.append(buf); buf=ch
        if buf: lines.append(buf)
    return lines

def make_caption_img(text, title=False):
    base = Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0))
    draw = ImageDraw.Draw(base)
    box_margin=80; box_w=VIDEO_W-box_margin*2
    font = ImageFont.truetype(FONT_PATH, 88 if title else 64)
    lines = wrap_by_width(text, font, box_w-80)
    line_h = text_size("あ",font)[1] + (18 if title else 12)
    box_h  = line_h*max(1,len(lines))+80
    y_top  = 150 if title else VIDEO_H-box_h-260
    draw.rectangle([box_margin,y_top,VIDEO_W-box_margin,y_top+box_h],
                   fill=(0,0,0,180), outline=(255,255,255,40), width=2)
    y=y_top+40
    for ln in lines:
        w,_=text_size(ln,font); x=box_margin+(box_w-w)//2
        draw.text((x+2,y+2),ln,font=font,fill=(0,0,0,210))
        draw.text((x,y),ln,font=font,fill=(255,255,255,255))
        y+=line_h
    return base

# --- 動画 ---
def build_video(title, script, audio_path, out_path):
    from moviepy.editor import ColorClip, ImageClip, CompositeVideoClip, AudioFileClip
    import numpy as np
    bg = ColorClip(size=(VIDEO_W,VIDEO_H), color=BG_COLOR)
    audio = AudioFileClip(audio_path)
    final_dur = max(MIN_DURATION, min(MAX_DURATION, audio.duration+2.0))
    bg = bg.set_duration(final_dur)

    title_txt = (title or "ニュース").strip()[:TITLE_MAX_CHAR]
    title_clip = ImageClip(np.array(make_caption_img(title_txt, True))).set_start(0).set_duration(final_dur)

    body_lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    if body_lines and body_lines[0]==title_txt: body_lines=body_lines[1:]
    body_lines = body_lines[:10]

    remain = max(3.0, final_dur-3.0); per = remain/max(1,len(body_lines))
    caps=[]
    for i,ln in enumerate(body_lines):
        img = ImageClip(np.array(make_caption_img(ln, False))).set_start(1.0+i*per).set_duration(min(per, max(1.1, final_dur-(1.0+i*per)-0.3)))
        caps.append(img)

    comp = CompositeVideoClip([bg, title_clip]+caps).set_audio(audio)
    comp.write_videofile(out_path, fps=25, codec="libx264", audio_codec="aac",
                         bitrate="2500k", threads=2, ffmpeg_params=["-preset","ultrafast"])
    return out_path, final_dur

# --- YouTube（保存済みrefresh_tokenでヘッドレス） ---
def get_creds():
    if not os.path.exists(TOKEN_FILE) or not os.path.exists(CLIENT_FILE):
        raise RuntimeError("yt_token.json と client_secret.json を用意してください（GitHub Secrets から書き出します）。")
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, YT_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        open(TOKEN_FILE,"w").write(creds.to_json())
    return creds

def upload_to_youtube(path, title, description):
    youtube = build("youtube","v3",credentials=get_creds())
    body = {
        "snippet": {
            "title": title[:95],
            "description": (description + "\\n\\n#Shorts").strip()[:4900],
            "categoryId": "25",
            "tags": ["ニュース","解説","Shorts"]
        },
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    }
    req = youtube.videos().insert(part="snippet,status", body=body,
                                 media_body=MediaFileUpload(path, chunksize=-1, resumable=True))
    resp=None
    while resp is None:
        status, resp = req.next_chunk()
    print("YouTubeアップロード完了: https://studio.youtube.com/video/"+resp.get("id"))

def main():
    e = fetch_latest(FEED_URL)
    if not e:
        print("RSSが空。終了"); return
    link = e.get("link","")
    title = clean_text(e.get("title","")) or "ニュース"
    summary = clean_text(e.get("summary","")) or clean_text(e.get("description",""))
    script = simple_rewrite(title, summary, link)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^0-9A-Za-z一-龥ぁ-んァ-ヶー]+","_", title)[:24]
    mp3 = os.path.join(OUTPUT_DIR, f"{ts}_{safe}.mp3")
    mp4 = os.path.join(OUTPUT_DIR, f"{ts}_{safe}.mp4")

    gTTS(text=script, lang="ja").save(mp3)
    out_path, dur = build_video(title, script, mp3, mp4)
    upload_to_youtube(out_path, f"【ショート】{title}", f"{script}\\n\\n出典: {link}")

if __name__ == "__main__":
    main()
