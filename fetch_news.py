import os
import json
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import anthropic

JST = timezone(timedelta(hours=9))
DB_PATH = "docs/cache.db"

RSS_FEEDS = [
    ("Reuters Energy",    "https://feeds.reuters.com/reuters/businessNews"),
    ("OilPrice.com",      "https://oilprice.com/rss/main"),
    ("EIA",               "https://www.eia.gov/rss/press_releases.xml"),
    ("OPEC",              "https://www.opec.org/opec_web/en/press_room/rss.htm"),
]

OIL_KEYWORDS = [
    "oil", "crude", "wti", "brent", "opec", "petroleum",
    "barrel", "refinery", "gasoline", "energy", "eia",
    "inventory", "production", "supply", "demand", "sanctions",
    "libya", "iran", "saudi", "russia", "shale",
]

# ── SQLite キャッシュ ──────────────────────────────────────────────

def init_db():
    os.makedirs("docs", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_cache (
            url        TEXT PRIMARY KEY,
            impact     INTEGER,
            direction  TEXT,
            summary    TEXT,
            time_horizon TEXT,
            main_factor  TEXT,
            reliability  TEXT,
            cached_at  TEXT
        )
    """)
    conn.commit()
    return conn

def load_cache(conn, url):
    row = conn.execute(
        "SELECT impact,direction,summary,time_horizon,main_factor,reliability FROM eval_cache WHERE url=?",
        (url,)
    ).fetchone()
    if row:
        return {
            "impact": row[0], "direction": row[1], "summary": row[2],
            "time_horizon": row[3], "main_factor": row[4], "reliability": row[5],
        }
    return None

def save_cache(conn, url, ev):
    conn.execute(
        "INSERT OR REPLACE INTO eval_cache VALUES (?,?,?,?,?,?,?,?)",
        (url, ev["impact"], ev["direction"], ev["summary"],
         ev["time_horizon"], ev["main_factor"], ev["reliability"],
         datetime.now(JST).isoformat())
    )
    conn.commit()

# ── RSS 取得 ────────────────────────────────────────────────────────

def fetch_rss(name, url):
    articles = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        for item in items[:10]:
            title = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "").strip()
            desc  = (item.findtext("description") or item.findtext("atom:summary", namespaces=ns) or "").strip()
            link  = (item.findtext("link") or item.findtext("atom:link", namespaces=ns) or "").strip()
            pub   = (item.findtext("pubDate") or item.findtext("atom:updated", namespaces=ns) or "").strip()
            text  = (title + " " + desc).lower()
            if any(k in text for k in OIL_KEYWORDS):
                articles.append({"source": name, "title": title, "desc": desc[:300], "link": link, "pub": pub})
    except Exception as e:
        print(f"[WARN] {name}: {e}")
    return articles

# ── Claude 評価 ─────────────────────────────────────────────────────

def evaluate_articles(articles, conn):
    uncached = []
    for a in articles:
        cached = load_cache(conn, a["link"])
        if cached:
            a.update(cached)
            print(f"  [CACHE] {a['title'][:40]}")
        else:
            uncached.append(a)

    if not uncached:
        return articles

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    items_text = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']} / {a['desc']}"
        for i, a in enumerate(uncached)
    )
    prompt = f"""以下はWTI原油（USOIL）に関するニュース記事のリストです。
各記事についてJSON配列で以下を返してください。

- impact: 1〜5の整数（WTI価格への影響度。5=極めて高い、1=ほぼなし）
- direction: "bullish" / "bearish" / "neutral"
- summary: 日本語で60字以内の要約
- time_horizon: "ultra_short"（数分〜数時間）/ "short"（1〜3日）/ "medium"（1〜4週間）/ "long"（1か月以上）
- main_factor: 主因を日本語で10字以内（例：地政学リスク、供給過剰、需要減少、在庫増加）
- reliability: 以下のスコアリングで算出し "A"/"B"/"C" で返す
  【ソース品質】Reuters/Bloomberg=+40, EIA=+50, OPEC公式=+50, その他主要メディア=+30, 専門サイト=+20, SNS/匿名=+10
  【事実性】発生済み事実/正式発表=+40, 検討中/交渉中=+20, 観測/予測/匿名情報=+10
  【市場感応度推定】明確な需給変化が確定=+20, 示唆あり=+10, 曖昧=0
  合計80以上=A, 60〜79=B, 59以下=C
  ※"このニュースから予想した価格方向が当たる可能性"として判定する

必ずJSON配列のみを返し、説明文や```は不要です。

記事リスト:
{items_text}
"""
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    evaluations = json.loads(raw)

    for i, a in enumerate(uncached):
        ev = evaluations[i] if i < len(evaluations) else {}
        result = {
            "impact":      int(ev.get("impact", 1)),
            "direction":   ev.get("direction", "neutral"),
            "summary":     ev.get("summary", ""),
            "time_horizon":ev.get("time_horizon", "short"),
            "main_factor": ev.get("main_factor", ""),
            "reliability": ev.get("reliability", "C"),
        }
        a.update(result)
        save_cache(conn, a["link"], result)
        print(f"  [API]   {a['title'][:40]}")

    return articles

# ── HTML 生成 ────────────────────────────────────────────────────────

def impact_group(n):
    if n >= 4: return "high"
    if n >= 2: return "mid"
    return "low"

def generate_html(articles):
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    articles.sort(key=lambda x: -x.get("impact", 1))

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
    RELIABILITY_TITLE = {"A": "高信頼：客観的事実", "B": "中信頼：事実と予測混在", "C": "低信頼：思惑・観測"}

    def impact_badge(n):
        group = impact_group(n)
        label = IMPACT_LABELS.get(n, "LOW")
        return f'<span class="impact-badge ig-{group}">{label}</span>'

    def dir_tag(direction):
        arr, label, cls = DIRECTION_LABELS.get(direction, ("→", "ニュートラル", "neutral"))
        return f'<span class="direction-tag {cls}">{arr} {label}</span>'

    def horizon_tag(h):
        label, cls = HORIZON_LABELS.get(h, (h, "hz-short"))
        return f'<span class="meta-tag {cls}">{label}</span>'

    def reliability_badge(r):
        title = RELIABILITY_TITLE.get(r, "")
        return f'<span class="reliability-badge rel-{r.lower()}" title="{title}">{r}</span>'

    def fmt_pub(pub):
        if not pub:
            return ""
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pub).astimezone(JST)
            return dt.strftime("%m/%d %H:%M")
        except Exception:
            return pub[:16]

    cards = ""
    for a in articles:
        n         = a.get("impact", 1)
        group     = impact_group(n)
        direction = a.get("direction", "neutral")
        summary   = a.get("summary", "")
        title     = a.get("title", "")
        link      = a.get("link", "#")
        source    = a.get("source", "")
        pub       = fmt_pub(a.get("pub", ""))
        horizon   = a.get("time_horizon", "short")
        factor    = a.get("main_factor", "")
        rel       = a.get("reliability", "C")

        cards += f"""
    <div class="news-card ig-{group}" data-group="{group}">
      <div class="card-top">
        <div class="headline"><a href="{link}" target="_blank" rel="noopener">{title}</a></div>
        {impact_badge(n)}
      </div>
      <div class="summary">{summary}</div>
      <div class="card-foot">
        <span class="meta-tag source-tag">{source}</span>
        {dir_tag(direction)}
        {horizon_tag(horizon)}
        {"<span class='meta-tag factor-tag'>" + factor + "</span>" if factor else ""}
        {reliability_badge(rel)}
        {"<span class='time-tag'>" + pub + "</span>" if pub else ""}
      </div>
    </div>"""

    source_guide = """
<section class="source-guide">
  <div class="source-guide-title">ニュースソース ガイド</div>
  <div class="source-grid">
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">Reuters</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★</span><span class="star-off">★</span></span></div>
      </div>
      <div class="source-desc">世界最大級の通信社。200拠点・2,600人の記者が24時間配信。OPEC決定・地政学リスク・大手産油国の動向など、相場を動かすニュースをいち早く報じる。金融機関が一次ソースとして使用するレベルの信頼性。</div>
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
      <div class="source-card-header"><span class="source-name">OilPrice.com</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★</span><span class="star-off">★★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★</span><span class="star-off">★★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
      </div>
      <div class="source-desc">原油・エネルギー市場に特化した専門メディア。分析・意見記事が多く補完情報として活用。一次情報ではなく他社記事の転載・解説も多い。</div>
    </div>
    <div class="source-card">
      <div class="source-card-header"><span class="source-name">OPEC 公式</span></div>
      <div class="source-metrics">
        <div class="source-metric"><span class="metric-label">即時性</span><span class="metric-stars"><span class="star-on">★★★</span><span class="star-off">★★</span></span></div>
        <div class="source-metric"><span class="metric-label">信頼性</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
        <div class="source-metric"><span class="metric-label">原油専門度</span><span class="metric-stars"><span class="star-on">★★★★★</span></span></div>
      </div>
      <div class="source-desc">OPEC本部からの公式声明・プレスリリース。減産合意・増産決定など価格に直撃する情報の一次ソース。更新頻度は低いが出たときのインパクトは最大級。</div>
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
  .meta{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted)}}
  main{{max-width:860px;margin:0 auto;padding:24px 16px}}
  .status-bar{{max-width:860px;margin:12px auto 0;padding:0 16px 20px}}
  .status-bar-inner{{display:flex;align-items:center;gap:8px;padding:8px 14px;background:rgba(74,144,96,.1);border:1px solid rgba(74,144,96,.3);border-radius:6px;font-size:12px;color:var(--muted)}}
  .status-dot{{width:7px;height:7px;border-radius:50%;background:#4a9060;flex-shrink:0}}
  .filter-bar{{display:flex;gap:8px;margin-top:16px;margin-bottom:20px;align-items:center;flex-wrap:nowrap}}
  .filter-label{{font-size:11px;color:var(--muted);margin-right:4px}}
  .filter-btn{{font-size:11px;padding:4px 12px;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s}}
  .filter-btn:hover,.filter-btn.active{{background:var(--tag-bg);color:var(--text);border-color:var(--muted)}}
  .filter-btn.f-high.active{{border-color:var(--high);color:var(--high)}}
  .filter-btn.f-mid.active{{border-color:var(--mid);color:var(--mid)}}
  .filter-btn.f-low.active{{border-color:var(--low);color:var(--low)}}
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
  .time-tag{{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);margin-left:auto}}
  .direction-tag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:2px;white-space:nowrap}}
  .direction-tag.bearish{{background:rgba(232,64,64,.12);color:var(--high)}}
  .direction-tag.bullish{{background:rgba(74,144,96,.12);color:var(--low)}}
  .direction-tag.neutral{{background:rgba(90,96,112,.12);color:var(--muted)}}
  .reliability-badge{{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;padding:2px 7px;border-radius:2px;border:1px solid;cursor:default}}
  .reliability-badge.rel-a{{color:#7ec8e3;border-color:rgba(126,200,227,.35);background:rgba(126,200,227,.08)}}
  .reliability-badge.rel-b{{color:var(--muted);border-color:var(--border);background:var(--tag-bg)}}
  .reliability-badge.rel-c{{color:#887755;border-color:rgba(136,119,85,.3);background:rgba(136,119,85,.07)}}
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
  <div class="meta">最終更新: {now} ｜ {len(articles)}件</div>
</header>
<main>
  <div class="status-bar">
    <div class="status-bar-inner">
      <div class="status-dot"></div>
      <span>最終確認: {now} — ニュースは最新の状態です</span>
    </div>
  </div>
  <div class="filter-bar">
    <span class="filter-label">フィルター：</span>
    <button class="filter-btn active" onclick="filterAll(this)">ALL</button>
    <button class="filter-btn f-high" onclick="filterGroup('high',this)">🔴 高</button>
    <button class="filter-btn f-mid"  onclick="filterGroup('mid',this)">🟡 中</button>
    <button class="filter-btn f-low"  onclick="filterGroup('low',this)">🟢 低</button>
  </div>
  <div class="news-list" id="newsList">
    {"<div class='no-articles'>原油関連ニュースが見つかりませんでした。</div>" if not articles else cards}
  </div>
</main>
{source_guide}
<footer>USOIL News Monitor ｜ 重要度はAIによる自動評価です。投資判断の参考情報であり、売買を推奨するものではありません。</footer>
<script>
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
</script>
</body>
</html>"""

# ── メイン ──────────────────────────────────────────────────────────

def main():
    conn = init_db()

    print("RSSフィードを取得中...")
    articles = []
    for name, url in RSS_FEEDS:
        found = fetch_rss(name, url)
        print(f"  {name}: {len(found)}件")
        articles.extend(found)

    seen = set()
    unique = []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)
    articles = unique[:20]

    print(f"合計: {len(articles)}件 → 評価中（キャッシュ利用）...")
    if articles:
        articles = evaluate_articles(articles, conn)

    conn.close()

    html = generate_html(articles)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("docs/index.html を生成しました")

if __name__ == "__main__":
    main()
