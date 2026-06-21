import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import anthropic

JST = timezone(timedelta(hours=9))

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
            text = (title + " " + desc).lower()
            if any(k in text for k in OIL_KEYWORDS):
                articles.append({"source": name, "title": title, "desc": desc[:200], "link": link, "pub": pub})
    except Exception as e:
        print(f"[WARN] {name}: {e}")
    return articles

def evaluate_articles(articles):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    items_text = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']} / {a['desc']}"
        for i, a in enumerate(articles)
    )
    prompt = f"""以下はWTI原油（USOIL）に関するニュース記事のリストです。
各記事について以下をJSON配列で返してください。
- impact: "high" / "mid" / "low"（WTI価格への影響度）
- direction: "bullish" / "bearish" / "neutral"
- reason: 判断理由（日本語・50字以内）

必ずJSON配列のみを返し、説明文や```は不要です。

記事リスト:
{items_text}
"""
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    evaluations = json.loads(raw)
    for i, a in enumerate(articles):
        if i < len(evaluations):
            a.update(evaluations[i])
        else:
            a.update({"impact": "low", "direction": "neutral", "reason": "評価データなし"})
    return articles

def generate_html(articles):
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    impact_order = {"high": 0, "mid": 1, "low": 2}
    articles.sort(key=lambda x: impact_order.get(x.get("impact", "low"), 2))

    def badge(impact):
        labels = {"high": "HIGH", "mid": "MID", "low": "LOW"}
        return f'<span class="impact-badge {impact}">{labels.get(impact, "LOW")}</span>'

    def dir_tag(direction):
        labels = {"bullish": "▲ ブリッシュ", "bearish": "▼ ベアリッシュ", "neutral": "→ ニュートラル"}
        return f'<span class="direction-tag {direction}">{labels.get(direction, "→ ニュートラル")}</span>'

    cards = ""
    for a in articles:
        impact    = a.get("impact", "low")
        direction = a.get("direction", "neutral")
        reason    = a.get("reason", "")
        title     = a.get("title", "")
        link      = a.get("link", "#")
        source    = a.get("source", "")
        pub       = a.get("pub", "")[:16]
        cards += f"""
    <div class="news-card impact-{impact}" data-impact="{impact}">
      <div class="card-top">
        <div class="headline"><a href="{link}" target="_blank">{title}</a></div>
        {badge(impact)}
      </div>
      <div class="reason">{reason}</div>
      <div class="card-foot">
        <span class="source-tag">{source}</span>
        {dir_tag(direction)}
        <span class="time-tag">{pub}</span>
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
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px}}
  header{{border-bottom:1px solid var(--border);padding:16px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
  .logo{{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:15px;color:var(--accent);letter-spacing:.08em}}
  .logo span{{color:var(--muted);font-weight:400}}
  .meta{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted)}}
  main{{max-width:860px;margin:0 auto;padding:24px 16px}}
  .filter-bar{{display:flex;gap:8px;margin-bottom:20px;align-items:center;flex-wrap:wrap}}
  .filter-label{{font-size:11px;color:var(--muted);margin-right:4px}}
  .filter-btn{{font-size:11px;padding:4px 12px;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s}}
  .filter-btn:hover,.filter-btn.active{{background:var(--tag-bg);color:var(--text);border-color:var(--muted)}}
  .filter-btn.f-high.active{{border-color:var(--high);color:var(--high)}}
  .filter-btn.f-mid.active{{border-color:var(--mid);color:var(--mid)}}
  .filter-btn.f-low.active{{border-color:var(--low);color:var(--low)}}
  .news-list{{display:flex;flex-direction:column;gap:10px}}
  .news-card{{background:var(--surface);border:1px solid var(--border);border-left:3px solid transparent;border-radius:4px;padding:14px 16px;transition:border-color .15s,background .15s}}
  .news-card:hover{{background:#181b24}}
  .news-card.impact-high{{border-left-color:var(--high)}}
  .news-card.impact-mid{{border-left-color:var(--mid)}}
  .news-card.impact-low{{border-left-color:var(--low)}}
  .card-top{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:8px}}
  .headline{{font-size:14px;font-weight:500;color:#e0e4f0;line-height:1.45;flex:1}}
  .headline a{{color:inherit;text-decoration:none}}
  .headline a:hover{{color:var(--accent)}}
  .impact-badge{{flex-shrink:0;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:.08em;padding:3px 8px;border-radius:2px}}
  .impact-badge.high{{background:rgba(232,64,64,.15);color:var(--high);border:1px solid rgba(232,64,64,.3)}}
  .impact-badge.mid{{background:rgba(232,160,32,.12);color:var(--mid);border:1px solid rgba(232,160,32,.3)}}
  .impact-badge.low{{background:rgba(74,144,96,.12);color:var(--low);border:1px solid rgba(74,144,96,.3)}}
  .reason{{font-size:12px;color:var(--muted);line-height:1.5;margin-bottom:8px}}
  .card-foot{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
  .source-tag{{font-size:10px;padding:2px 8px;border-radius:2px;background:var(--tag-bg);color:var(--muted);border:1px solid var(--border)}}
  .time-tag{{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted)}}
  .direction-tag{{font-size:10px;font-weight:600;padding:2px 8px;border-radius:2px}}
  .direction-tag.bearish{{background:rgba(232,64,64,.12);color:var(--high)}}
  .direction-tag.bullish{{background:rgba(74,144,96,.12);color:var(--low)}}
  .direction-tag.neutral{{background:rgba(90,96,112,.12);color:var(--muted)}}
  .no-articles{{text-align:center;padding:48px;color:var(--muted);font-size:13px}}
  .source-guide{{max-width:860px;margin:40px auto 0;padding:0 16px 32px}}
  .source-guide-title{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);border-top:1px solid var(--border);padding-top:28px;margin-bottom:16px}}
  .source-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}}
  .source-card{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 16px}}
  .source-card-header{{display:flex;align-items:center;gap:10px;margin-bottom:10px}}
  .source-name{{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;color:var(--accent)}}
  .source-type{{font-size:10px;padding:2px 7px;border-radius:2px;background:var(--tag-bg);color:var(--muted);border:1px solid var(--border)}}
  .source-metrics{{display:flex;gap:16px;margin-bottom:10px}}
  .source-metric{{display:flex;flex-direction:column;gap:3px}}
  .metric-label{{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
  .star-on{{color:var(--accent)}}
  .star-off{{color:var(--border)}}
  .source-desc{{font-size:12px;color:var(--muted);line-height:1.6}}
  .source-timing{{margin-top:8px;font-size:11px;color:var(--text);padding:6px 10px;background:var(--tag-bg);border-radius:3px;border-left:2px solid var(--accent)}}
  footer{{text-align:center;padding:24px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);margin-top:32px}}
</style>
</head>
<body>
<header>
  <div class="logo">USOIL <span>/ News Monitor</span></div>
  <div class="meta">最終更新: {now} ｜ {len(articles)}件</div>
</header>
<main>
  <div class="filter-bar">
    <span class="filter-label">フィルター：</span>
    <button class="filter-btn active" onclick="filterAll(this)">すべて</button>
    <button class="filter-btn f-high" onclick="filterImpact('high',this)">🔴 高</button>
    <button class="filter-btn f-mid"  onclick="filterImpact('mid',this)">🟡 中</button>
    <button class="filter-btn f-low"  onclick="filterImpact('low',this)">🟢 低</button>
  </div>
  <div class="news-list" id="newsList">
    {"<div class='no-articles'>原油関連ニュースが見つかりませんでした。</div>" if not articles else cards}
  </div>
</main>
{source_guide}
<footer>USOIL News Monitor ｜ 重要度はAI（Claude API）による自動評価です。投資判断の参考情報であり、売買を推奨するものではありません。</footer>
<script>
  function filterAll(btn){{
    document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.news-card').forEach(c=>c.style.display='');
  }}
  function filterImpact(level,btn){{
    document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.news-card').forEach(c=>{{
      c.style.display=c.dataset.impact===level?'':'none';
    }});
  }}
</script>
</body>
</html>"""

def main():
    print("RSSフィードを取得中...")
    articles = []
    for name, url in RSS_FEEDS:
        found = fetch_rss(name, url)
        print(f"  {name}: {len(found)}件")
        articles.extend(found)

    # 重複タイトルを除去
    seen = set()
    unique = []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)
    articles = unique[:20]  # 最大20件

    print(f"合計: {len(articles)}件 → Claude APIで評価中...")
    if articles:
        articles = evaluate_articles(articles)

    html = generate_html(articles)
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("docs/index.html を生成しました")

if __name__ == "__main__":
    main()
