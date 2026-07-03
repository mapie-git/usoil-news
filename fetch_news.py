import os
import re
import json
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import anthropic

JST = timezone(timedelta(hours=9))
DB_PATH = "docs/cache.db"

DISPLAY_WINDOW_DAYS = 5   # この日数以内に公開された記事だけを候補にする
POOL_LIMIT = 60           # 候補プールの上限（直近日数の中から新しい順に採用）
DISPLAY_LIMIT = 25        # 実際にページへ表示する件数（重要度順に採用）
RETENTION_DAYS = 30       # DBに保持しておく期間（これより古い記事は掃除する）

VERIFY_AFTER_SECONDS = 24 * 60 * 60  # 予測を検証するまでの待機時間（記事評価から24時間後）
NEUTRAL_THRESHOLD_PCT = 0.3          # neutral予測を的中とみなす価格変動幅（%）

RSS_FEEDS = [
    ("OilPrice.com",         "https://oilprice.com/rss/main"),
    ("EIA",                  "https://www.eia.gov/rss/todayinenergy.xml"),
    ("Hellenic Shipping",    "https://www.hellenicshippingnews.com/category/oil-energy/feed/"),
    ("Google News",          "https://news.google.com/rss/search?q=WTI+crude+oil+OPEC&hl=en&gl=US&ceid=US:en"),
    ("Rigzone",              "https://www.rigzone.com/news/rss/rigzone_latest.aspx"),
    ("Reddit r/oil",         "https://www.reddit.com/r/oil/.rss"),
    ("Reddit r/Commodities", "https://www.reddit.com/r/Commodities/.rss"),
]

OIL_KEYWORDS = [
    "oil", "crude", "wti", "brent", "opec", "petroleum",
    "barrel", "refinery", "gasoline", "energy", "eia",
    "inventory", "production", "supply", "demand", "sanctions",
    "libya", "iran", "saudi", "russia", "shale",
]

# ── 日付ユーティリティ ─────────────────────────────────────────────
# RSS(RFC2822)とAtom(ISO8601)の両方の日付フォーマットに対応する

def parse_pub_dt(pub):
    if not pub:
        return None
    from email.utils import parsedate_to_datetime
    dt = None
    try:
        dt = parsedate_to_datetime(pub)
    except Exception:
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST)

def pub_ts(pub):
    dt = parse_pub_dt(pub)
    return int(dt.timestamp()) if dt else 0

def fmt_pub(pub):
    dt = parse_pub_dt(pub)
    return dt.strftime("%m/%d %H:%M") if dt else ""

def is_new(pub):
    dt = parse_pub_dt(pub)
    if not dt:
        return False
    return (datetime.now(JST) - dt).total_seconds() <= 1800

def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

# ── WTI価格取得（Yahoo Financeの非公式チャートAPI）───────────────────
# 予測（bullish/bearish/neutral）の的中検証に使う。フォーマット変更等で
# 取得に失敗しても致命的にはならないよう呼び出し側でNoneを許容する。

def fetch_wti_price():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        for c in reversed(closes):
            if c is not None:
                return float(c)
    except Exception as e:
        print(f"[WARN] WTI価格取得失敗: {e}")
    return None

# ── SQLite（記事プールの永続化）────────────────────────────────────

def init_db():
    os.makedirs("docs", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            url          TEXT PRIMARY KEY,
            source       TEXT,
            title        TEXT,
            desc         TEXT,
            pub          TEXT,
            pub_ts       INTEGER,
            impact       INTEGER,
            direction    TEXT,
            summary      TEXT,
            time_horizon TEXT,
            main_factor  TEXT,
            reliability  TEXT,
            evaluated    INTEGER DEFAULT 0,
            fetched_at   TEXT,
            price_at_pub REAL,
            price_after  REAL,
            verify_at    INTEGER,
            verified     INTEGER DEFAULT 0,
            correct      INTEGER
        )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    migrations = {
        "price_at_pub": "REAL",
        "price_after":  "REAL",
        "verify_at":    "INTEGER",
        "verified":     "INTEGER DEFAULT 0",
        "correct":      "INTEGER",
    }
    for col, coltype in migrations.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {coltype}")
    conn.commit()
    return conn

def upsert_articles(conn, articles):
    seen_titles = {row[0] for row in conn.execute("SELECT title FROM articles").fetchall()}
    now_iso = datetime.now(JST).isoformat()
    for a in articles:
        if a["title"] in seen_titles:
            continue
        seen_titles.add(a["title"])
        conn.execute("""
            INSERT OR IGNORE INTO articles (url, source, title, desc, pub, pub_ts, evaluated, fetched_at)
            VALUES (?,?,?,?,?,?,0,?)
        """, (a["link"], a["source"], a["title"], a["desc"], a["pub"], pub_ts(a["pub"]), now_iso))
    conn.commit()

def cleanup_old(conn):
    cutoff = int((datetime.now(JST) - timedelta(days=RETENTION_DAYS)).timestamp())
    conn.execute("DELETE FROM articles WHERE pub_ts > 0 AND pub_ts < ?", (cutoff,))
    conn.commit()

def get_recent_articles(conn, days=DISPLAY_WINDOW_DAYS, limit=POOL_LIMIT):
    cutoff = int((datetime.now(JST) - timedelta(days=days)).timestamp())
    rows = conn.execute("""
        SELECT url, source, title, desc, pub, impact, direction, summary, time_horizon, main_factor, reliability,
               verified, correct
        FROM articles
        WHERE pub_ts >= ? AND evaluated = 1
        ORDER BY pub_ts DESC
        LIMIT ?
    """, (cutoff, limit)).fetchall()
    return [
        {
            "link": r[0], "source": r[1], "title": r[2], "desc": r[3], "pub": r[4],
            "impact": r[5], "direction": r[6], "summary": r[7],
            "time_horizon": r[8], "main_factor": r[9], "reliability": r[10],
            "verified": r[11], "correct": r[12],
        }
        for r in rows
    ]

def get_accuracy_stats(conn):
    row = conn.execute("SELECT COUNT(*), SUM(correct) FROM articles WHERE verified = 1").fetchone()
    total = row[0] or 0
    if total == 0:
        return None
    correct = row[1] or 0
    return {"total": total, "correct": correct, "rate": round(correct / total * 100)}

# ── RSS 取得 ────────────────────────────────────────────────────────

def fetch_rss(name, url):
    articles = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "usoil-news-monitor/1.0 (RSS fetcher; +https://github.com/)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items[:20]:
            title = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "").strip()
            desc  = (item.findtext("description") or item.findtext("atom:summary", namespaces=ns)
                     or item.findtext("atom:content", namespaces=ns) or "").strip()
            desc  = strip_html(desc)
            link  = (item.findtext("link") or "").strip()
            if not link:
                link_el = item.find("atom:link", ns)
                if link_el is not None:
                    link = (link_el.get("href") or "").strip()
            pub   = (item.findtext("pubDate") or item.findtext("atom:updated", namespaces=ns) or "").strip()
            text  = (title + " " + desc).lower()
            if any(k in text for k in OIL_KEYWORDS):
                articles.append({"source": name, "title": title, "desc": desc[:300], "link": link, "pub": pub})
    except Exception as e:
        print(f"[WARN] {name}: {e}")
    return articles

# ── Claude 評価 ─────────────────────────────────────────────────────

EVAL_BATCH_SIZE = 15  # 1回のAPI呼び出しで評価する件数（出力トークン超過によるJSON破損を防ぐ）

def evaluate_batch(client, batch):
    items_text = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']} / {a['desc']}"
        for i, a in enumerate(batch)
    )
    prompt = f"""以下はWTI原油（USOIL）に関するニュース記事のリストです。
各記事についてJSON配列で以下を返してください。

- impact: 1〜5の整数（WTI価格への影響度。5=極めて高い、1=ほぼなし）
  ※ 既に市場に織り込まれた後追い報道（例：数時間〜前の値動きを後から伝えるだけの記事、既知の事実の言い換え）はimpactを2以下に抑えること
- direction: "bullish" / "bearish" / "neutral"
- summary: 日本語で60字以内の要約
- time_horizon: "ultra_short"（数分〜数時間）/ "short"（1〜3日）/ "medium"（1〜4週間）/ "long"（1か月以上）
- main_factor: 主因を日本語で10字以内（例：地政学リスク、供給過剰、需要減少、在庫増加）
- reliability: 以下のスコアリングで算出し "A"/"B"/"C" で返す
  【ソース品質】Reuters/Bloomberg=+40, EIA=+50, OPEC公式=+50, その他主要メディア=+30, 専門サイト=+20, SNS/掲示板/匿名=+10
  【事実性】発生済み事実/正式発表=+40, 検討中/交渉中=+20, 観測/予測/匿名情報=+10
  【市場感応度推定】明確な需給変化が確定=+20, 示唆あり=+10, 曖昧=0
  合計80以上=A, 60〜79=B, 59以下=C
  ※"このニュースから予想した価格方向が当たる可能性"として判定する
  ※ Reddit等の掲示板ソースは個人の思惑・観測が中心のため、reliabilityはC（まれにB）を基本とする

必ずJSON配列のみを返し、説明文や```は不要です。

記事リスト:
{items_text}
"""
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def evaluate_unevaluated(conn):
    rows = conn.execute("SELECT url, source, title, desc FROM articles WHERE evaluated = 0").fetchall()
    if not rows:
        return
    uncached = [{"link": r[0], "source": r[1], "title": r[2], "desc": r[3]} for r in rows]

    # 評価時点のWTI価格を記録し、後で予測（bullish/bearish/neutral）の的中検証に使う
    price_now = fetch_wti_price()
    verify_at = int(datetime.now(JST).timestamp()) + VERIFY_AFTER_SECONDS

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    for start in range(0, len(uncached), EVAL_BATCH_SIZE):
        batch = uncached[start:start + EVAL_BATCH_SIZE]
        try:
            evaluations = evaluate_batch(client, batch)
        except Exception as e:
            print(f"[WARN] 評価バッチ失敗（{start}件目〜）: {e}")
            continue

        for i, a in enumerate(batch):
            ev = evaluations[i] if i < len(evaluations) else {}
            conn.execute("""
                UPDATE articles
                SET impact=?, direction=?, summary=?, time_horizon=?, main_factor=?, reliability=?, evaluated=1,
                    price_at_pub=?, verify_at=?
                WHERE url=?
            """, (
                int(ev.get("impact", 1)), ev.get("direction", "neutral"), ev.get("summary", ""),
                ev.get("time_horizon", "short"), ev.get("main_factor", ""), ev.get("reliability", "C"),
                price_now, verify_at,
                a["link"],
            ))
            print(f"  [API]   {a['title'][:40]}")
        conn.commit()

# ── 予測の的中検証 ───────────────────────────────────────────────────

def verify_predictions(conn):
    now_ts = int(datetime.now(JST).timestamp())
    rows = conn.execute("""
        SELECT url, direction, price_at_pub FROM articles
        WHERE verified = 0 AND price_at_pub IS NOT NULL AND verify_at IS NOT NULL AND verify_at <= ?
    """, (now_ts,)).fetchall()
    if not rows:
        return

    price_now = fetch_wti_price()
    if price_now is None:
        return

    for url, direction, price_at_pub in rows:
        pct_change = (price_now - price_at_pub) / price_at_pub * 100
        if direction == "bullish":
            correct = 1 if pct_change > 0 else 0
        elif direction == "bearish":
            correct = 1 if pct_change < 0 else 0
        else:
            correct = 1 if abs(pct_change) < NEUTRAL_THRESHOLD_PCT else 0
        conn.execute("""
            UPDATE articles SET price_after=?, verified=1, correct=? WHERE url=?
        """, (price_now, correct, url))
    conn.commit()
    print(f"  予測検証: {len(rows)}件")

# ── JSON 生成（iOSアプリ用ネイティブAPI）───────────────────────────────

def generate_json(articles, limit=DISPLAY_LIMIT, stats=None, wti_price=None):
    articles = sorted(articles, key=lambda x: -x.get("impact", 1))[:limit]
    now = datetime.now(JST)
    items = []
    for a in articles:
        pub_raw = a.get("pub", "")
        items.append({
            "id": a.get("link", ""),
            "source": a.get("source", ""),
            "title": a.get("title", ""),
            "summary": a.get("summary", ""),
            "link": a.get("link", "#"),
            "pub": pub_raw,
            "pub_ts": pub_ts(pub_raw),
            "impact": a.get("impact", 1),
            "impact_group": impact_group(a.get("impact", 1)),
            "direction": a.get("direction", "neutral"),
            "time_horizon": a.get("time_horizon", "short"),
            "main_factor": a.get("main_factor", ""),
            "reliability": a.get("reliability", "C"),
            "is_new": is_new(pub_raw),
            "verified": bool(a.get("verified")),
            "correct": (bool(a.get("correct")) if a.get("verified") else None),
        })
    payload = {
        "updated_at": now.isoformat(),
        "accuracy": stats,
        "wti_price": wti_price,
        "articles": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

# ── HTML 生成 ────────────────────────────────────────────────────────

def impact_group(n):
    if n >= 4: return "high"
    if n >= 2: return "mid"
    return "low"

def generate_html(articles, limit=DISPLAY_LIMIT, stats=None):
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    articles = sorted(articles, key=lambda x: -x.get("impact", 1))[:limit]

    DIRECTION_LABELS = {
        "bullish": ("↑", "ブリッシュ", "bullish"),
        "bearish": ("↓", "ベアリッシュ", "bearish"),
        "neutral": ("→", "ニュートラル", "neutral"),
    }
    HORIZON_LABELS = {
        "ultra_short": ("超短期", "hz-ultra"),
        "short":       ("短期",   "hz-short"),
        "medium":      ("中期",   "hz-medium"),
        "long":        ("長期",   "hz-long"),
    }
    IMPACT_LABELS = {5: "VERY HIGH", 4: "HIGH", 3: "MEDIUM", 2: "LOW", 1: "VERY LOW"}
    RELIABILITY_STARS = {"A": "★★★", "B": "★★☆", "C": "★☆☆"}
    RELIABILITY_TITLE = {"A": "高信頼：客観的事実", "B": "中信頼：事実と予測混在", "C": "低信頼：思惑・観測"}

    def impact_badge(n):
        group = impact_group(n)
        label = IMPACT_LABELS.get(n, "LOW")
        return f'<span class="impact-badge ig-{group}">{label}</span>'

    def dir_tag(direction, verified, correct):
        arr, label, cls = DIRECTION_LABELS.get(direction, ("→", "ニュートラル", "neutral"))
        verify_html = ""
        if verified:
            verify_html = (
                ' <span class="verify-ok" title="24時間後の価格で的中">✓的中</span>' if correct
                else ' <span class="verify-ng" title="24時間後の価格で不的中">✗不的中</span>'
            )
        return f'<span class="direction-tag {cls}">{arr} {label}{verify_html}</span>'

    def horizon_tag(h):
        label, cls = HORIZON_LABELS.get(h, (h, "hz-short"))
        return f'<span class="meta-tag {cls}">{label}</span>'

    def reliability_badge(r):
        stars = RELIABILITY_STARS.get(r, "★☆☆")
        title = RELIABILITY_TITLE.get(r, "")
        return f'<span class="reliability-badge rel-{r.lower()}" title="{title}">{stars}</span>'

    cards = ""
    for a in articles:
        n         = a.get("impact", 1)
        group     = impact_group(n)
        direction = a.get("direction", "neutral")
        summary   = a.get("summary", "")
        title     = a.get("title", "")
        link      = a.get("link", "#")
        source    = a.get("source", "")
        pub_raw   = a.get("pub", "")
        pub       = fmt_pub(pub_raw)
        horizon   = a.get("time_horizon", "short")
        factor    = a.get("main_factor", "")
        rel       = a.get("reliability", "C")
        new_flag  = is_new(pub_raw)
        new_attr  = ' data-new="1"' if new_flag else ""
        new_badge = '<span class="new-badge">NEW</span>' if new_flag else ""
        verified  = a.get("verified") or 0
        correct   = a.get("correct")
        rel_num   = {"A": 3, "B": 2, "C": 1}.get(rel, 1)
        dir_num   = {"bullish": 1, "neutral": 0, "bearish": -1}.get(direction, 0)
        hz_num    = {"ultra_short": 1, "short": 2, "medium": 3, "long": 4}.get(horizon, 2)
        ts        = pub_ts(pub_raw)

        cards += f"""
    <div class="news-card ig-{group}"{new_attr} data-group="{group}" data-impact="{n}" data-ts="{ts}" data-rel="{rel_num}" data-dir="{dir_num}" data-hz="{hz_num}">
      <div class="card-top">
        <div class="headline">{new_badge}<a href="{link}" target="_blank" rel="noopener">{title}</a></div>
        {impact_badge(n)}
      </div>
      <div class="summary">{summary}</div>
      <div class="card-foot">
        <span class="meta-tag source-tag">{source}</span>
        {dir_tag(direction, verified, correct)}
        {horizon_tag(horizon)}
        {"<span class='meta-tag factor-tag'>" + factor + "</span>" if factor else ""}
        <div class="card-foot-right">
          {reliability_badge(rel)}
          {"<span class='time-tag'>" + pub + "</span>" if pub else ""}
        </div>
      </div>
    </div>"""

    source_guide = """
<section class="source-guide">
  <div class="source-guide-title">ニュースソース ガイド</div>
  <div class="source-grid">
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">OilPrice.com</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★</span><span class="star-off">★★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★</span><span class="star-off">★★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
      </div>
      <div class="source-desc">原油・エネルギー市場に特化した専門メディア。分析・意見記事が多く補完情報として活用。一次情報ではなく他社記事の転載・解説も多い。</div>
    </div>
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">EIA（米エネルギー情報局）</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
      </div>
      <div class="source-desc">米国政府の公式エネルギー統計機関。週次原油在庫統計（WTI価格に最も影響する指標）を毎週水曜に発表。「誤報」の概念がない一次統計ソース。</div>
    </div>
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">Hellenic Shipping News</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
      </div>
      <div class="source-desc">ギリシャ系の海運・エネルギー専門メディア。原油タンカー・OPEC動向・中東情勢など原油市場に直結するニュースを専門的にカバー。業界内で広く読まれる信頼性の高いソース。</div>
    </div>
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">Google News（WTI/OPEC）</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
      </div>
      <div class="source-desc">Reuters・Bloomberg・FT・WSJなど主要メディアの原油関連記事をGoogleが集約。RSSを廃止したReutersやOPEC関連ニュースもここ経由で取得。ソースの質はGoogleの選定に依存。</div>
    </div>
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">Rigzone</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
      </div>
      <div class="source-desc">石油・ガス業界専門の米国メディア。掘削・生産・OPEC・中東情勢など上流部門の一次情報を豊富に配信。業界関係者にも広く読まれる信頼性の高い専門ソース。</div>
    </div>
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">Reddit（r/oil, r/Commodities）</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★</span><span class="star-off">★★★★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★</span><span class="star-off">★★</span></span></div>
      </div>
      <div class="source-desc">個人投資家・トレーダーの思惑や観測が集まる掲示板。事実確認前の噂話も多く信頼性は低いが、市場心理や早耳情報を拾う目的で補助的に採用。</div>
    </div>
  </div>
</section>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>USOIL News Monitor</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Inter:wght@400;500;600&display=swap');
  :root {{
    --bg:#0d0f14;--surface:#141720;--border:#1e2230;--text:#c8cdd8;--muted:#5a6070;
    --accent:#e8a020;--high:#e84040;--mid:#e8a020;--low:#4a9060;--tag-bg:#1a1d25;
    --c5:#e84040;--c4:#d06828;--c3:#c89020;--c2:#4480b8;--c1:#4a9060;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px}}
  header{{border-bottom:1px solid var(--border);padding:16px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
  .logo{{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:15px;color:var(--accent);letter-spacing:.08em}}
  .logo span{{color:var(--muted);font-weight:400}}
  .meta{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);display:flex;align-items:center}}
  main{{max-width:860px;margin:0 auto;padding:12px 16px 24px}}
  .status-dot{{width:9px;height:9px;border-radius:50%;display:inline-block;cursor:pointer;vertical-align:middle;margin-left:8px;flex-shrink:0}}
  .status-dot.ok{{background:#4a9060;box-shadow:0 0 4px rgba(74,144,96,.6)}}
  .status-dot.warn{{background:#e84040;box-shadow:0 0 4px rgba(232,64,64,.6)}}
  .status-popup{{position:fixed;top:50px;right:16px;background:#1e2230;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-size:12px;color:var(--text);box-shadow:0 4px 20px rgba(0,0,0,.5);z-index:999;display:none;max-width:280px;line-height:1.6}}
  .status-popup.show{{display:block}}
  .filter-bar{{display:flex;gap:8px;margin-top:16px;margin-bottom:20px;align-items:center;flex-wrap:wrap}}
  .filter-label{{font-size:11px;color:var(--muted);width:100%;margin-bottom:-2px}}
  .filter-btn{{font-size:11px;padding:4px 0;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s;flex:1;text-align:center}}
  .filter-btn:hover,.filter-btn.active{{background:var(--tag-bg);color:var(--text);border-color:var(--muted)}}
  .filter-btn.f-high.active{{border-color:var(--high);color:var(--high)}}
  .filter-btn.f-mid.active{{border-color:var(--mid);color:var(--mid)}}
  .filter-btn.f-low.active{{border-color:var(--low);color:var(--low)}}
  .filter-btn.f-new.active{{border-color:#3a9bd5;color:#3a9bd5}}
  .sort-bar{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
  .sort-label{{font-size:11px;color:var(--muted);width:100%;margin-bottom:-2px}}
  .sort-btn{{font-size:11px;padding:4px 0;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s;flex:1;display:flex;align-items:center;justify-content:center;gap:3px}}
  .sort-btn:hover{{background:var(--tag-bg);color:var(--text)}}
  .sort-btn.active{{background:var(--tag-bg);color:var(--accent);border-color:var(--accent)}}
  .sort-arrow{{font-size:10px;opacity:.7}}
  .new-badge{{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.06em;color:#fff;background:#3a9bd5;border-radius:3px;padding:1px 5px;margin-right:6px;vertical-align:2px}}
  .news-list{{display:flex;flex-direction:column;gap:10px}}
  .news-card{{background:var(--surface);border:1px solid var(--border);border-left:3px solid transparent;border-radius:4px;padding:14px 16px;transition:background .15s}}
  .news-card:hover{{background:#181b24}}
  .news-card.ig-high{{border-left-color:var(--high)}}
  .news-card.ig-mid{{border-left-color:var(--mid)}}
  .news-card.ig-low{{border-left-color:var(--low)}}
  .card-top{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:7px}}
  .headline{{font-size:14px;font-weight:500;color:#e0e4f0;line-height:1.45;flex:1}}
  .headline a{{color:inherit;text-decoration:none}}
  .headline a:hover{{color:var(--accent)}}
  .impact-badge{{flex-shrink:0;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;padding:3px 8px;border-radius:3px;white-space:nowrap}}
  .impact-badge.ig-high{{background:rgba(232,64,64,.18);color:var(--high);border:1px solid rgba(232,64,64,.35)}}
  .impact-badge.ig-mid{{background:rgba(232,160,32,.14);color:var(--mid);border:1px solid rgba(232,160,32,.35)}}
  .impact-badge.ig-low{{background:rgba(74,144,96,.14);color:var(--low);border:1px solid rgba(74,144,96,.35)}}
  .summary{{font-size:12px;color:var(--text);line-height:1.55;margin-bottom:9px;opacity:.85}}
  .card-foot{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}}
  .meta-tag{{font-size:10px;padding:2px 8px;border-radius:2px;background:var(--tag-bg);color:var(--muted);border:1px solid var(--border);white-space:nowrap}}
  .source-tag{{color:var(--accent);border-color:rgba(232,160,32,.25)}}
  .factor-tag{{font-style:italic}}
  .hz-ultra{{color:#e0e4f0;border-color:rgba(224,228,240,.3)}}
  .hz-short{{color:var(--low);border-color:rgba(74,144,96,.35)}}
  .hz-medium{{color:var(--mid);border-color:rgba(232,160,32,.35)}}
  .hz-long{{color:var(--high);border-color:rgba(232,64,64,.35)}}
  .card-foot-right{{display:flex;align-items:center;gap:6px;margin-left:auto}}
  .time-tag{{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted)}}
  .direction-tag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:2px;white-space:nowrap}}
  .direction-tag.bearish{{background:rgba(232,64,64,.12);color:var(--high)}}
  .direction-tag.bullish{{background:rgba(74,144,96,.12);color:var(--low)}}
  .direction-tag.neutral{{background:rgba(90,96,112,.12);color:var(--muted)}}
  .verify-ok{{color:var(--low);font-weight:700}}
  .verify-ng{{color:var(--high);font-weight:700}}
  .reliability-badge{{font-size:11px;cursor:default}}
  .rel-a{{color:#c8a830}}
  .rel-b{{color:#ddd090}}
  .rel-c{{color:#ede8c8}}
  .no-articles{{text-align:center;padding:48px;color:var(--muted);font-size:13px}}
  .source-guide{{max-width:860px;margin:40px auto 0;padding:0 16px 32px}}
  .source-guide-title{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);border-top:1px solid var(--border);padding-top:28px;margin-bottom:16px}}
  .source-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}}
  .source-card{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 16px}}
  .source-card-header{{display:flex;align-items:center;gap:10px;margin-bottom:10px}}
  .source-name{{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;color:var(--accent)}}
  .source-metrics{{display:flex;gap:16px;margin-bottom:10px}}
  .source-metric{{display:flex;flex-direction:column;gap:3px}}
  .metric-label{{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
  .star-on{{color:var(--accent)}}
  .star-off{{color:var(--border)}}
  .source-desc{{font-size:12px;color:var(--muted);line-height:1.6}}
  footer{{text-align:center;padding:24px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);margin-top:32px}}
</style>
</head>
<body>
<header>
  <div class="logo">USOIL <span>/ News Monitor</span></div>
  <div class="meta">最終更新: {now} ｜ {len(articles)}件<span class="status-dot ok" id="statusDot" onclick="toggleStatus()" title="ステータス確認"></span></div>
</header>
<div class="status-popup" id="statusPopup">
  ✅ 最終確認: {now}<br>ニュースは最新の状態です
  {"<br>AI予測的中率: " + str(stats["rate"]) + "%（" + str(stats["total"]) + "件中）" if stats else ""}
</div>
<main>
  <div class="filter-bar">
    <span class="filter-label">フィルター：</span>
    <button class="filter-btn active" onclick="filterAll(this)">ALL</button>
    <button class="filter-btn f-new"  onclick="filterNew(this)" style="color:#3a9bd5">NEW</button>
    <button class="filter-btn f-high" onclick="filterGroup('high',this)">🔴 高</button>
    <button class="filter-btn f-mid"  onclick="filterGroup('mid',this)">🟡 中</button>
    <button class="filter-btn f-low"  onclick="filterGroup('low',this)">🟢 低</button>
  </div>
  <div class="sort-bar">
    <span class="sort-label">並び替え：</span>
    <button class="sort-btn active" id="sb-impact" onclick="sortCards('impact',this)">重要度<span class="sort-arrow" id="sa-impact">↓</span></button>
    <button class="sort-btn" id="sb-ts"     onclick="sortCards('ts',this)">更新日時<span class="sort-arrow" id="sa-ts">↓</span></button>
    <button class="sort-btn" id="sb-rel"    onclick="sortCards('rel',this)">信頼度<span class="sort-arrow" id="sa-rel">↓</span></button>
    <button class="sort-btn" id="sb-dir"    onclick="sortCards('dir',this)">方向<span class="sort-arrow" id="sa-dir">↓</span></button>
    <button class="sort-btn" id="sb-hz"     onclick="sortCards('hz',this)">期間<span class="sort-arrow" id="sa-hz">↓</span></button>
  </div>
  <div class="news-list" id="newsList">
    {"<div class='no-articles'>原油関連ニュースが見つかりませんでした。</div>" if not articles else cards}
  </div>
</main>
{source_guide}
<footer>USOIL News Monitor ｜ 重要度はAIによる自動評価です。投資判断の参考情報であり、売買を推奨するものではありません。</footer>
<script>
  function toggleStatus(){{
    var p=document.getElementById('statusPopup');
    p.classList.toggle('show');
  }}
  document.addEventListener('click',function(e){{
    if(!e.target.closest('#statusDot')&&!e.target.closest('#statusPopup')){{
      document.getElementById('statusPopup').classList.remove('show');
    }}
  }});
  function filterAll(btn){{
    document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.news-card').forEach(c=>c.style.display='');
  }}
  function filterGroup(group,btn){{
    document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.news-card').forEach(c=>{{
      c.style.display=c.dataset.group===group?'':'none';
    }});
  }}
  function filterNew(btn){{
    document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.news-card').forEach(c=>{{
      c.style.display=c.dataset.new==='1'?'':'none';
    }});
  }}
  var _sortKey='impact', _sortAsc=false;
  function sortCards(key,btn){{
    if(_sortKey===key){{ _sortAsc=!_sortAsc; }}
    else{{ _sortKey=key; _sortAsc=false; }}
    document.querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    ['impact','ts','rel','dir','hz'].forEach(k=>{{
      var el=document.getElementById('sa-'+k);
      if(el) el.textContent=_sortAsc?'↑':'↓';
    }});
    var list=document.getElementById('newsList');
    var cards=Array.from(list.querySelectorAll('.news-card'));
    cards.sort(function(a,b){{
      var av=parseFloat(a.dataset[_sortKey])||0;
      var bv=parseFloat(b.dataset[_sortKey])||0;
      return _sortAsc?(av-bv):(bv-av);
    }});
    cards.forEach(c=>list.appendChild(c));
  }}
</script>
</body>
</html>"""

# ── メイン ──────────────────────────────────────────────────────────

def main():
    conn = init_db()

    print("RSSフィードを取得中...")
    fetched = []
    for name, url in RSS_FEEDS:
        found = fetch_rss(name, url)
        print(f"  {name}: {len(found)}件")
        fetched.extend(found)
        time.sleep(2)

    upsert_articles(conn, fetched)
    cleanup_old(conn)

    print("未評価の記事を評価中...")
    evaluate_unevaluated(conn)

    print("的中検証中...")
    verify_predictions(conn)

    articles = get_recent_articles(conn)
    stats = get_accuracy_stats(conn)
    print(f"表示候補プール: {len(articles)}件（直近{DISPLAY_WINDOW_DAYS}日）")

    wti_price = fetch_wti_price()
    print(f"WTI価格: {wti_price}")

    conn.close()

    html = generate_html(articles, stats=stats)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("docs/index.html を生成しました")

    news_json = generate_json(articles, stats=stats, wti_price=wti_price)
    with open("docs/news.json", "w", encoding="utf-8") as f:
        f.write(news_json)
    print("docs/news.json を生成しました")

if __name__ == "__main__":
    main()
