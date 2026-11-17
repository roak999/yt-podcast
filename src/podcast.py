#!/usr/bin/env python3
"""投資系YouTubeの新着動画 → 2人の掛け合いポッドキャスト（MP3 + RSS）を毎日つくる。

流れ:
  1. 各チャンネルのRSSから新着動画を集める
  2. Gemini に動画URLを渡して1本ずつ要約
  3. 要約をもとに掛け合い台本（JSON）を作る
  4. 音声合成 → MP3
  5. ポッドキャスト用 feed.xml を更新

使い方:
  python src/podcast.py            # 本番
  python src/podcast.py --dry-run  # 新着動画の一覧を表示するだけ（APIキー不要）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.sax.saxutils import escape

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
STATE = ROOT / "state" / "state.json"
CACHE = ROOT / "state" / "summaries"  # 要約の途中保存（実行が止まっても次回はここから再開）
DOCS = ROOT / "docs"
OUT = ROOT / "out"

JST = dt.timezone(dt.timedelta(hours=9))
SAMPLE_RATE = 24000  # Gemini TTS の出力（16bit / mono / 24kHz）
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
      "Accept-Language": "ja,en;q=0.8"}


_LOG_LOCK = __import__("threading").Lock()


def log(*a):
    with _LOG_LOCK:
        print(*a, flush=True)


def is_not_found(e: Exception) -> bool:
    return "NOT_FOUND" in str(e) or "404" in str(e)[:20]


def is_busy(e: Exception) -> bool:
    """混雑(503)や回数制限(429)。別のモデルに切り替えると通ることが多い。"""
    t = str(e)[:40]
    return any(k in t for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"))


def with_retry(fn, what: str, tries: int = 4, base: int = 30):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1 or is_not_found(e):
                raise
            wait = base * (2 ** i)
            log(f"  {what} 失敗: {e} → {wait}秒後に再試行")
            time.sleep(wait)


def model_list(v) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


def with_models(models, fn, what: str):
    """候補のモデルを順に試す。使えない・混雑しているときは次の候補へ。
    全候補が混雑していたら、少し待ってからもう一巡する。"""
    names = model_list(models)
    for rnd in range(3):
        for i, m in enumerate(names):
            try:
                return with_retry(lambda: fn(m), f"{what}（{m}）", tries=2, base=15)
            except Exception as e:  # noqa: BLE001
                if not (is_not_found(e) or is_busy(e)):
                    raise
                last = e
                nxt = names[i + 1] if i < len(names) - 1 else None
                if nxt:
                    log(f"  {m} は{'使えません' if is_not_found(e) else '混雑中'} → {nxt} を試します")
        if not is_busy(last) or rnd == 2:
            raise last
        log("  すべてのモデルが混雑中 → 2分待って再挑戦")
        time.sleep(120)


# ---------------------------------------------------------------- state
def load_state() -> dict:
    s = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    s.setdefault("channel_ids", {})
    s.setdefault("processed", {})
    s.setdefault("episodes", [])
    return s


def save_state(s: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", "utf-8")


# ---------------------------------------------------------------- 1. 新着動画
def fetch_feed(channel_id: str) -> str | None:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code == 200 and "<feed" in r.text:
                return r.text
            log(f"  RSS取得 {channel_id}: HTTP {r.status_code}")
        except requests.RequestException as e:
            log(f"  RSS取得 {channel_id}: {e}")
        time.sleep(3 * (i + 1))
    return None


def resolve_channel_id(url: str) -> str | None:
    m = re.search(r"/channel/(UC[\w-]{22})", url)
    if m:
        return m.group(1)
    try:
        html = requests.get(url, headers=UA, cookies={"CONSENT": "YES+1"}, timeout=30).text
    except requests.RequestException:
        return None
    for pat in (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                r'"externalId":"(UC[\w-]{22})"',
                r"channel_id=(UC[\w-]{22})"):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def collect_videos(cfg: dict, state: dict) -> list[dict]:
    c = cfg["collect"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=c["lookback_hours"])
    videos = []
    for ch in cfg["channels"]:
        name = ch["name"]
        cid = state["channel_ids"].get(name) or ch.get("id")
        xml = fetch_feed(cid) if cid else None
        if xml is None and ch.get("url"):
            new = resolve_channel_id(ch["url"])
            if new and new != cid:
                log(f"  {name}: チャンネルIDを {new} に更新")
                cid, xml = new, fetch_feed(new)
        if xml is None:
            log(f"⚠ {name}: RSSを取得できませんでした（スキップ）")
            continue
        state["channel_ids"][name] = cid
        for e in ET.fromstring(xml).findall("atom:entry", NS):
            vid = e.findtext("yt:videoId", namespaces=NS)
            published = dt.datetime.fromisoformat(e.findtext("atom:published", namespaces=NS))
            link_el = e.find("atom:link", NS)
            link = link_el.get("href", "") if link_el is not None else ""
            if not vid or vid in state["processed"] or published < cutoff:
                continue
            if c.get("skip_shorts", True) and "/shorts/" in link:
                continue
            videos.append({
                "id": vid,
                "title": e.findtext("atom:title", namespaces=NS) or "",
                "channel": name,
                "published": published.isoformat(),
                "url": f"https://www.youtube.com/watch?v={vid}",
            })
    videos.sort(key=lambda v: v["published"])
    return videos[-c["max_videos"]:]


# ---------------------------------------------------------------- 2. 要約
SUMMARY_PROMPT = """あなたは投資系YouTube動画の内容を正確に記録するアシスタントです。
この動画（チャンネル: {channel} / タイトル: {title}）の内容を、あとでポッドキャスト台本を作るための詳細メモとして日本語でまとめてください。

# 出力する項目
1. 動画のテーマ（1〜2文）
2. 主な主張・結論（発言者の意見として、箇条書き）
3. 登場した銘柄・セクター・指数・経済指標と、それぞれについて言われていたこと
4. 主張の根拠として示されたデータや数字（動画内で言われた値のみ）
5. リスクや注意点として話されていたこと
6. 視聴者への具体的な提案（あれば）

# ルール
- 動画で実際に話されている内容だけを書き、推測や一般論で補わない
- 数字・銘柄名・日付は聞き取れた通り正確に。不確かなものは「（聞き取り不確か）」と付ける
- 宣伝や告知（メンバーシップ、セミナー、書籍など）は省く
"""


def summarize(client, models, v: dict, fps: float | None = 0.2) -> str:
    """fps: 映像を何秒に1コマ見るか（0.2 = 5秒に1コマ）。投資動画は話が中心なので少なくて十分で、速く安くなる。"""
    from google.genai import types
    opt = {"fps": fps}

    def call(model):
        meta = types.VideoMetadata(fps=opt["fps"]) if opt["fps"] else None
        try:
            r = client.models.generate_content(
                model=model,
                contents=types.Content(parts=[
                    types.Part(file_data=types.FileData(file_uri=v["url"]), video_metadata=meta),
                    types.Part(text=SUMMARY_PROMPT.format(**v)),
                ]),
                config=types.GenerateContentConfig(media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW),
            )
        except Exception as e:  # noqa: BLE001
            if opt["fps"] and "INVALID_ARGUMENT" in str(e):
                log("  コマ数の指定が使えないため、通常設定でやり直します")
                opt["fps"] = None
                return call(model)
            raise
        if not r.text:
            raise RuntimeError("空の応答")
        return r.text

    return with_models(models, call, f"要約 {v['id']}")


def summarize_all(client, cfg: dict, videos: list[dict]) -> tuple[list[dict], list[str]]:
    """複数の動画を同時並行で要約。終わったものから state/summaries/ に保存する。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    ids = {v["id"] for v in videos}
    for f in CACHE.glob("*.md"):  # 対象外になった古い途中保存は削除
        if f.stem not in ids:
            f.unlink()
    c = cfg["collect"]

    def work(v):
        f = CACHE / f"{v['id']}.md"
        if f.exists():
            log(f"要約（保存済みを使用）: {v['title'][:40]}")
            return f.read_text("utf-8")
        log(f"要約開始: {v['title'][:40]}")
        t0 = time.time()
        s = summarize(client, cfg["models"]["summary"], v, c.get("video_fps", 0.2))
        f.write_text(s, "utf-8")
        log(f"  ✓ 要約完了（{(time.time() - t0) / 60:.1f}分）: {v['title'][:40]}")
        return s

    results = {}
    with ThreadPoolExecutor(max_workers=c.get("parallel", 3)) as ex:
        futs = {ex.submit(work, v): v for v in videos}
        for fu in as_completed(futs):
            v = futs[fu]
            try:
                results[v["id"]] = fu.result()
            except Exception as e:  # noqa: BLE001
                log(f"  ⚠ 要約できませんでした（次回再試行）: {v['title'][:40]}: {e}")
    done = [v for v in videos if v["id"] in results]
    notes = [f"## {v['channel']}「{v['title']}」\n{results[v['id']]}" for v in done]
    return done, notes


# ---------------------------------------------------------------- 3. 台本
SCRIPT_PROMPT = """あなたは投資情報ポッドキャストの構成作家です。以下は、今日フォローしているYouTubeチャンネルに投稿された動画の要約メモです。
これをもとに、{a}と{b}の2人による日本語の掛け合い台本を書いてください。

# 番組
- 番組名: {title}
- 日付: {date}
- 聞き手はジムで運動しながら聞く個人投資家。耳だけで理解できるよう短めの文で話し、数字は聞き取りやすく言う（例:「3.5%」→「3.5パーセント」）

# 登場人物
- {a}: {a_role}
- {b}: {b_role}

# 構成
1. オープニング（今日扱う動画の本数と、全体の雰囲気をひと言で）
2. 動画ごとのコーナー。冒頭で必ずチャンネル名と動画の主題を紹介し、主張・根拠・リスクを掛け合いで深掘りする
3. 複数の動画に共通していた見方や、意見が分かれていた点の整理
4. エンディング（今日のポイントを3つ程度で振り返り、「最終的な判断はご自身で」と添える）

# ルール
- 全体で約{chars}文字（読み上げ約{minutes}分）を目安にする。動画の内容が少ない日は無理に引き延ばさず短くてよい
- 要約メモにない事実・数字を足さない。主張は「〇〇さんは〜と見ている」のように誰の意見か分かる形で伝える
- 番組として特定銘柄の売買を勧めない
- セリフにト書き・記号・URLを入れない。英語の略語は読み方が分かるように（例: FRB→エフアールビー）
- 1つのセリフは長くても200文字程度まで

# 出力形式（JSONのみ）
{{"title": "エピソードのタイトル（30文字程度）", "description": "エピソードの概要（100〜200文字）", "lines": [{{"speaker": "{a}", "text": "..."}}, {{"speaker": "{b}", "text": "..."}}]}}

# 要約メモ
{notes}
"""


def make_script(client, cfg: dict, notes: str, today: dt.date) -> dict:
    from google.genai import types
    s = cfg["script"]
    a, b = s["hosts"][0], s["hosts"][1]
    prompt = SCRIPT_PROMPT.format(
        a=a["name"], b=b["name"], a_role=a["role"], b_role=b["role"],
        title=cfg["podcast"]["title"], date=f"{today.year}年{today.month}月{today.day}日",
        chars=s["target_minutes"] * 320, minutes=s["target_minutes"], notes=notes)

    def call(model):
        r = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=60000))
        data = json.loads(r.text)
        names = {a["name"], b["name"]}
        lines = [{"speaker": l["speaker"], "text": l["text"].strip()}
                 for l in data["lines"] if l.get("speaker") in names and l.get("text", "").strip()]
        if len(lines) < 10:
            raise RuntimeError(f"台本が短すぎます（{len(lines)}行）")
        data["lines"] = lines
        return data

    return with_models(cfg["models"]["script"], call, "台本作成")


# ---------------------------------------------------------------- 4. 音声
def silence(sec: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * sec)


def chunk_lines(lines: list[dict], max_chars: int) -> list[list[dict]]:
    chunks, cur, n = [], [], 0
    for ln in lines:
        if cur and n + len(ln["text"]) > max_chars:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(ln)
        n += len(ln["text"])
    if cur:
        chunks.append(cur)
    return chunks


TTS_PREFIX = ("以下は日本語の投資ニュース番組での{a}と{b}の会話です。"
              "落ち着いた聞き取りやすいテンポで、自然な掛け合いとして読み上げてください。\n\n")


def tts_gemini(client, cfg: dict, lines: list[dict]) -> bytes:
    from google.genai import types
    hosts = cfg["script"]["hosts"]
    t = cfg["tts"]
    speech = types.SpeechConfig(multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
        speaker_voice_configs=[
            types.SpeakerVoiceConfig(speaker=h["name"], voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=h["voice"])))
            for h in hosts]))
    prefix = TTS_PREFIX.format(a=hosts[0]["name"], b=hosts[1]["name"])
    chunks = chunk_lines(lines, t["chunk_chars"])
    tts_models = model_list(cfg["models"]["tts"])
    pcm = bytearray()
    for i, ch in enumerate(chunks, 1):
        text = prefix + "\n".join(f"{l['speaker']}: {l['text']}" for l in ch)
        n_chars = sum(len(l["text"]) for l in ch)

        def call(model):
            r = client.models.generate_content(
                model=model, contents=text,
                config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=speech))
            part = r.candidates[0].content.parts[0].inline_data
            data = part.data
            m = re.search(r"rate=(\d+)", part.mime_type or "")
            if m and int(m.group(1)) != SAMPLE_RATE:
                raise RuntimeError(f"想定外のサンプルレート {part.mime_type}")
            secs = len(data) / (2 * SAMPLE_RATE)
            if secs < n_chars / 15:  # 途中で切れた音声を検出（通常は1秒あたり5〜7文字）
                raise RuntimeError(f"音声が短すぎます（{secs:.0f}秒 / {n_chars}文字）")
            return data

        log(f"  音声合成 {i}/{len(chunks)}（{n_chars}文字）")
        pcm += with_models(tts_models, call, f"音声合成 {i}") + silence(0.4)
        if i < len(chunks):
            time.sleep(t["wait_seconds"])
    return bytes(pcm)


def mp3_to_pcm(path: Path) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
                           "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
                          capture_output=True, check=True).stdout


def tts_edge(cfg: dict, lines: list[dict]) -> bytes:
    import edge_tts
    voices = {h["name"]: h["edge_voice"] for h in cfg["script"]["hosts"]}

    async def run() -> bytes:
        out = bytearray()
        with tempfile.TemporaryDirectory() as d:
            for i, l in enumerate(lines):
                p = Path(d) / f"{i}.mp3"
                for k in range(3):
                    try:
                        await edge_tts.Communicate(l["text"], voices[l["speaker"]], rate="+5%").save(str(p))
                        break
                    except Exception:  # noqa: BLE001
                        if k == 2:
                            raise
                        await asyncio.sleep(5)
                out += mp3_to_pcm(p) + silence(0.25)
        return bytes(out)

    log(f"  音声合成（edge）: {len(lines)}行")
    return asyncio.run(run())


def pcm_to_mp3(pcm: bytes, path: Path, title: str):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                    "-i", "-", "-codec:a", "libmp3lame", "-b:a", "64k",
                    "-metadata", f"title={title}", str(path)], input=pcm, check=True)


# ---------------------------------------------------------------- 5. フィード
def attr(s: str) -> str:
    return escape(str(s), {'"': "&quot;"})


def build_feed(cfg: dict, episodes: list[dict], site_url: str) -> str:
    p = cfg["podcast"]
    items = "\n".join(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(ep['description'])}</description>
      <pubDate>{ep['pub_date']}</pubDate>
      <guid isPermaLink="false">{escape(ep['guid'])}</guid>
      <enclosure url="{attr(ep['url'])}" length="{ep['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{ep['duration']}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""" for ep in episodes)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(p['title'])}</title>
    <link>{escape(site_url)}</link>
    <atom:link href="{attr(site_url)}feed.xml" rel="self" type="application/rss+xml"/>
    <description>{escape(p['description'])}</description>
    <language>ja</language>
    <itunes:author>{escape(p['author'])}</itunes:author>
    <itunes:block>Yes</itunes:block>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Business"><itunes:category text="Investing"/></itunes:category>
{items}
  </channel>
</rss>
"""


def gh_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------- main
def main():
    cfg = yaml.safe_load(CONFIG.read_text("utf-8"))
    state = load_state()
    repo = os.environ.get("GITHUB_REPOSITORY", "owner/repo")
    owner, name = repo.split("/")
    site_url = f"https://{owner.lower()}.github.io/{name}/"
    OUT.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)

    videos = collect_videos(cfg, state)
    log(f"新着動画: {len(videos)}本")
    for v in videos:
        log(f"  - [{v['channel']}] {v['title']}")
    if "--dry-run" in sys.argv:
        return

    def finish_without_episode(reason: str):
        log(reason)
        (DOCS / "feed.xml").write_text(build_feed(cfg, state["episodes"], site_url), "utf-8")
        save_state(state)
        gh_output(has_episode="false")

    if not videos:
        return finish_without_episode("新着がないため今日はお休みです。")

    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                          http_options=types.HttpOptions(api_version="v1beta"))

    done, notes = summarize_all(client, cfg, videos)
    if not done:
        return finish_without_episode("要約できた動画がないため今日はお休みです。")

    now = dt.datetime.now(JST)
    log("台本作成中…")
    script = make_script(client, cfg, "\n\n".join(notes), now.date())
    log(f"  台本: {len(script['lines'])}行 / {sum(len(l['text']) for l in script['lines'])}文字")

    log("音声合成中…")
    if cfg["tts"]["backend"] == "edge":
        pcm = tts_edge(cfg, script["lines"])
    else:
        pcm = tts_gemini(client, cfg, script["lines"])

    tag = f"ep-{now:%Y%m%d-%H%M}"
    title = f"{now.month}/{now.day} {script['title']}"
    mp3 = OUT / f"{tag}.mp3"
    pcm_to_mp3(pcm, mp3, title)
    secs = len(pcm) // (2 * SAMPLE_RATE)
    log(f"  完成: {mp3.name}（{secs // 60}分{secs % 60}秒）")

    sources = "\n".join(f"・{v['channel']}「{v['title']}」 {v['url']}" for v in done)
    ep = {
        "tag": tag, "guid": tag, "title": title,
        "description": f"{script['description']}\n\n取り上げた動画:\n{sources}",
        "pub_date": email.utils.format_datetime(now),
        "url": f"https://github.com/{repo}/releases/download/{tag}/{mp3.name}",
        "bytes": mp3.stat().st_size,
        "duration": f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}",
    }
    keep = cfg["podcast"].get("keep_episodes", 14)
    state["episodes"].insert(0, ep)
    old = state["episodes"][keep:]
    state["episodes"] = state["episodes"][:keep]

    for v in done:
        state["processed"][v["id"]] = now.date().isoformat()
        (CACHE / f"{v['id']}.md").unlink(missing_ok=True)
    limit = (now.date() - dt.timedelta(days=30)).isoformat()
    state["processed"] = {k: d for k, d in state["processed"].items() if d >= limit}

    transcript = "\n".join(f"**{l['speaker']}**: {l['text']}" for l in script["lines"])
    (OUT / "release_notes.md").write_text(f"### 取り上げた動画\n{sources}\n\n### 台本\n{transcript}\n", "utf-8")
    (OUT / "delete_tags.txt").write_text("".join(e["tag"] + "\n" for e in old), "utf-8")
    (DOCS / "feed.xml").write_text(build_feed(cfg, state["episodes"], site_url), "utf-8")
    save_state(state)
    gh_output(has_episode="true", tag=tag, mp3=str(mp3), title=title)


if __name__ == "__main__":
    main()
