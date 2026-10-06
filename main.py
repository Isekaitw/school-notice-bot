"""
校網公告自動化智慧篩選（回覆版）
=================================
這一版不再主動推播到 LINE（推播會按群組人數扣額度），改成：

1. 讀取 last_checked.json（上次已看過的公告 ID 清單）
2. 透過學校網站的 RSS 訂閱抓取公告列表
3. 找出「新」公告，呼叫 Gemini 判斷是否符合 USER_CRITERIA
4. 符合的公告存進 matches.json（交給 GitHub Actions commit 回 repo）
5. 之後由 Cloudflare Worker 讀取 matches.json，在群組有人講話時，
   用「回覆訊息」把還沒貼過的新公告貼出來（回覆訊息不扣額度）

所以這支程式不需要任何 LINE 的金鑰。
"""

import os
import re
import sys
import json
from datetime import datetime, timezone, timedelta

import requests
import feedparser
import google.generativeai as genai

STATE_FILE = "last_checked.json"
MATCHES_FILE = "matches.json"
GEMINI_MODEL = "gemini-3.6-flash"
REQUEST_TIMEOUT = 15
MATCH_KEEP_DAYS = 30  # matches.json 只保留最近 30 天，避免檔案一直變大


# ---------------------------------------------------------------------------
# 狀態讀寫（已看過的公告 ID）
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_ids": [], "updated_at": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 符合條件的公告清單（給 Cloudflare Worker 讀取）
# ---------------------------------------------------------------------------
def load_matches() -> list[dict]:
    if os.path.exists(MATCHES_FILE):
        with open(MATCHES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_matches(matches: list[dict]) -> None:
    with open(MATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=False, indent=2)


def prune_matches(matches: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MATCH_KEEP_DAYS)
    kept = []
    for m in matches:
        try:
            if datetime.fromisoformat(m["matched_at"]) >= cutoff:
                kept.append(m)
        except Exception:
            kept.append(m)  # 時間格式有問題就先留著，不要誤刪
    return kept


# ---------------------------------------------------------------------------
# 抓取公告列表（透過 RSS）
# ---------------------------------------------------------------------------
def fetch_announcements(url: str) -> list[dict]:
    resp = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SchoolNoticeBot/1.0)"},
    )
    resp.raise_for_status()

    feed = feedparser.parse(resp.content)

    announcements = []
    for entry in feed.entries:
        link = getattr(entry, "link", "").strip()
        title = getattr(entry, "title", "").strip()
        uid = getattr(entry, "id", None) or link or title
        date_str = getattr(entry, "published", "") or getattr(entry, "updated", "")

        announcements.append(
            {"id": uid, "title": title, "link": link, "date": date_str}
        )

    return announcements


def filter_new(announcements: list[dict], state: dict) -> list[dict]:
    known_ids = set(state.get("last_ids", []))
    return [a for a in announcements if a["id"] not in known_ids]


# ---------------------------------------------------------------------------
# Gemini 判斷
# ---------------------------------------------------------------------------
def check_with_gemini(announcement: dict, criteria: str, api_key: str) -> dict:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""你是一個校園公告篩選助手。請根據使用者條件，判斷這則公告是否相關。

使用者條件：{criteria}

公告標題：{announcement['title']}
公告日期：{announcement['date']}

只回傳 JSON，不要任何其他文字或說明，格式如下：
{{"is_match": true 或 false, "reason": "一句話說明理由"}}"""

    response = model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    text = (response.text or "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"is_match": False, "reason": "Gemini 回傳格式解析失敗"}


# ---------------------------------------------------------------------------
# 縮短網址
# ---------------------------------------------------------------------------
def shorten_url(url: str) -> str:
    """用 TinyURL 的免費 API 縮短網址，失敗就回傳原網址。"""
    try:
        resp = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": url},
            timeout=10,
        )
        if resp.status_code == 200 and resp.text.strip().startswith("http"):
            return resp.text.strip()
    except Exception as e:
        print(f"[縮網址失敗，改用原網址] {e}", file=sys.stderr)
    return url


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    target_url = os.environ["TARGET_URL"]
    gemini_key = os.environ["GEMINI_API_KEY"]
    criteria = os.environ.get("USER_CRITERIA", "")

    state = load_state()
    matches = load_matches()

    try:
        announcements = fetch_announcements(target_url)
    except Exception as e:
        print(f"[爬取失敗] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"抓到 {len(announcements)} 則公告")

    new_items = filter_new(announcements, state)
    print(f"其中 {len(new_items)} 則為新公告")

    known_match_ids = {m["id"] for m in matches}
    new_matches = 0
    for item in new_items:
        try:
            result = check_with_gemini(item, criteria, gemini_key)
        except Exception as e:
            print(f"[Gemini 判斷失敗] {item['title']}: {e}", file=sys.stderr)
            continue

        print(f"- {item['title']} -> {result}")

        if result.get("is_match") and item["id"] not in known_match_ids:
            matches.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "link": shorten_url(item["link"]),
                    "matched_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            new_matches += 1

    matches = prune_matches(matches)
    save_matches(matches)
    print(f"本次新增 {new_matches} 則符合條件的公告（matches.json 目前共 {len(matches)} 則）")

    # 更新狀態：以「這次抓到的公告清單」作為下次比對基準
    state["last_ids"] = [a["id"] for a in announcements]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
