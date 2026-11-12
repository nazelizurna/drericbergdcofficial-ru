import json
import os
import re
import tempfile
import textwrap
import time

import pytesseract
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont
from pytesseract import Output

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SOURCE = os.environ.get("SOURCE", "drericbergdcofficial").replace("https://t.me/", "").strip("@/ ")
TARGET = os.environ.get("TARGET", "@drericbergdcofficialru")
REPOST_LAST = os.environ.get("REPOST_LAST", "").lower() == "true"   # test mode
FONT_PATH = os.environ.get(
    "FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)
TEXT_COLOR = (255, 255, 255)
LAST_ID_FILE = "last_id.txt"
MAX_POSTS_PER_RUN = 20
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

translator = GoogleTranslator(source="auto", target="ru")


# ---------------------------------------------------------------- translation
def tr(text: str) -> str:
    """Translate text, keeping line breaks and bullets."""
    if not text or not text.strip():
        return ""
    out = []
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        out.append(translator.translate(line[:4900]) or line)
    return "\n".join(out)


def fit_font(draw, text, box_w, box_h, max_size=120):
    for size in range(max_size, 10, -2):
        font = ImageFont.truetype(FONT_PATH, size)
        avg = draw.textlength("абвгдежзик", font=font) / 10
        wrap_at = max(8, int(box_w / avg))
        lines = textwrap.wrap(text, width=wrap_at)
        line_h = size * 1.25
        widest = max((draw.textlength(l, font=font) for l in lines), default=0)
        if len(lines) * line_h <= box_h and widest <= box_w:
            return font, lines, line_h
    font = ImageFont.truetype(FONT_PATH, 12)
    return font, textwrap.wrap(text, 40), 15


def translate_image(path: str) -> str:
    """OCR the image, erase the text block, draw the Russian translation."""
    img = Image.open(path).convert("RGB")
    W, H = img.size
    data = pytesseract.image_to_data(img, lang="eng", output_type=Output.DICT)

    boxes = []
    for i, word in enumerate(data["text"]):
        try:
            conf = float(data["conf"][i])
        except ValueError:
            continue
        # skip junk and the logo area at the bottom
        if word.strip() and conf > 50 and data["top"][i] < H * 0.82:
            boxes.append((data["left"][i], data["top"][i],
                          data["left"][i] + data["width"][i],
                          data["top"][i] + data["height"][i], word))
    if len(boxes) < 2:
        return path  # no real text found -> send image as is

    pad = 12
    x0 = max(min(b[0] for b in boxes) - pad, 0)
    y0 = max(min(b[1] for b in boxes) - pad, 0)
    x1 = min(max(b[2] for b in boxes) + pad, W)
    y1 = min(max(b[3] for b in boxes) + pad, H)

    russian = translator.translate(" ".join(b[4] for b in boxes))

    bg = img.getpixel((max(x0 - 6, 0), max(y0 - 6, 0)))
    draw = ImageDraw.Draw(img)
    draw.rectangle([x0, y0, x1, y1], fill=bg)

    font, lines, line_h = fit_font(draw, russian, x1 - x0, y1 - y0)
    y = y0 + ((y1 - y0) - len(lines) * line_h) / 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text((x0 + ((x1 - x0) - w) / 2, y), line, font=font, fill=TEXT_COLOR)
        y += line_h

    out = path.rsplit(".", 1)[0] + "_ru.png"
    img.save(out)
    return out


# ------------------------------------------------------------ read the channel
def parse_page(html: str):
    """Return a list of posts: {id, text, photos[], video}."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for el in soup.select("div.tgme_widget_message[data-post]"):
        try:
            pid = int(el["data-post"].split("/")[-1])
        except ValueError:
            continue

        text = ""
        t = el.select_one("div.tgme_widget_message_text")
        if t:
            for br in t.find_all("br"):
                br.replace_with("\n")
            text = t.get_text().strip()

        photos = []
        for a in el.select("a.tgme_widget_message_photo_wrap"):
            m = re.search(r"url\('?([^')]+)'?\)", a.get("style", ""))
            if m:
                photos.append(m.group(1))

        video = None
        v = el.find("video")
        if v and v.get("src"):
            video = v["src"]

        posts.append({"id": pid, "text": text, "photos": photos, "video": video})
    posts.sort(key=lambda p: p["id"])
    return posts


def fetch_posts():
    r = requests.get(f"https://t.me/s/{SOURCE}", headers=UA, timeout=30)
    r.raise_for_status()
    posts = parse_page(r.text)
    if not posts:
        raise SystemExit(
            "No posts found. The channel must be public and have the web preview "
            f"enabled: open https://t.me/s/{SOURCE} in a browser to check."
        )
    return posts


# -------------------------------------------------------------- post via bot
def bot(method, data=None, files=None):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=data, files=files, timeout=180,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"{method} failed: {j}")
    return j


def send_text(text: str):
    text = text.strip()
    while text:
        chunk = text[:4000]
        if len(text) > 4000 and "\n" in chunk:
            chunk = chunk[: chunk.rfind("\n")]
        bot("sendMessage", {"chat_id": TARGET, "text": chunk,
                            "disable_web_page_preview": True})
        text = text[len(chunk):].strip()


def download(url: str, folder: str, name: str) -> str:
    path = os.path.join(folder, name)
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def publish(post):
    caption = tr(post["text"])
    short = len(caption) <= 1024

    if post["photos"]:
        folder = tempfile.mkdtemp()
        paths = []
        for i, url in enumerate(post["photos"][:10]):
            p = download(url, folder, f"img{i}.jpg")
            try:
                p = translate_image(p)
            except Exception as e:   # never lose a post over an OCR problem
                print("Image translation failed, sending original:", e)
            paths.append(p)

        cap = caption if short else ""
        if len(paths) == 1:
            with open(paths[0], "rb") as f:
                bot("sendPhoto", {"chat_id": TARGET, "caption": cap},
                    files={"photo": f})
        else:
            media, files, handles = [], {}, []
            for i, p in enumerate(paths):
                h = open(p, "rb")
                handles.append(h)
                files[f"f{i}"] = h
                item = {"type": "photo", "media": f"attach://f{i}"}
                if i == 0 and cap:
                    item["caption"] = cap
                media.append(item)
            try:
                bot("sendMediaGroup",
                    {"chat_id": TARGET, "media": json.dumps(media)}, files=files)
            finally:
                for h in handles:
                    h.close()
        if caption and not short:
            send_text(caption)

    elif post["video"]:
        try:
            bot("sendVideo", {"chat_id": TARGET, "video": post["video"],
                              "caption": caption if short else ""})
            if caption and not short:
                send_text(caption)
        except RuntimeError as e:   # e.g. video too big for the Bot API
            print("Video failed:", e)
            send_text((caption + "\n\n" if caption else "") + "🎬 Видео смотрите в оригинале.")
    elif caption:
        send_text(caption)


# ---------------------------------------------------------------------- main
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set.")

    posts = fetch_posts()
    newest = posts[-1]["id"]

    if REPOST_LAST:                          # manual test: re-send the latest post
        print("Test mode: reposting post", newest)
        publish(posts[-1])
        return

    if not os.path.exists(LAST_ID_FILE):     # first run: don't repost history
        open(LAST_ID_FILE, "w").write(str(newest))
        print("Initialized last_id.txt =", newest, "- new posts will be copied from now on.")
        return

    last_id = int(open(LAST_ID_FILE).read().strip() or 0)
    new = [p for p in posts if p["id"] > last_id][:MAX_POSTS_PER_RUN]
    if not new:
        print("No new posts.")
        return

    for p in new:
        try:
            publish(p)
            print("Posted", p["id"])
        except Exception as e:
            print("Failed on post", p["id"], e)
            break                              # retry this one next run
        open(LAST_ID_FILE, "w").write(str(p["id"]))
        time.sleep(3)                          # be gentle with Telegram limits


if __name__ == "__main__":
    main()
