"""YouTube Data API v3 메타데이터 — 조회수/좋아요/댓글/게시일/길이/캡션여부/구독자.

 - env YOUTUBE_API_KEY 없으면 비활성(enabled()=False) → 메타 없이 URL만으로도 프로젝트 진행 가능(무영향).
 - 공식 Data API(특정 비공식 업체 종속 아님). google-api-python-client lazy import.
 - fetch(video_ids)는 순수 조회 → service가 fetcher를 주입해 테스트에서 mock 가능.
"""
import os


def enabled():
    return bool(os.environ.get("YOUTUBE_API_KEY"))


def _client():
    from googleapiclient.discovery import build
    return build("youtube", "v3", developerKey=os.environ["YOUTUBE_API_KEY"], cache_discovery=False)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch(video_ids):
    """[video_id,...] → {video_id: {title,description,channel_id,channel_name,published_at,
    thumbnail_url,view_count,like_count,comment_count,duration,caption_status,subscriber_count}}.
    비활성/빈입력이면 {}. 개별 실패는 건너뜀(전체 실패 아님)."""
    ids = [v for v in (video_ids or []) if v]
    if not enabled() or not ids:
        return {}
    yt = _client()
    out = {}
    chan_ids = set()
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        resp = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(batch)).execute()
        for it in resp.get("items", []):
            sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
            out[it["id"]] = {
                "title": sn.get("title"), "description": sn.get("description"),
                "channel_id": sn.get("channelId"), "channel_name": sn.get("channelTitle"),
                "published_at": sn.get("publishedAt"), "thumbnail_url": thumb,
                "view_count": _int(st.get("viewCount")), "like_count": _int(st.get("likeCount")),
                "comment_count": _int(st.get("commentCount")),
                "duration": cd.get("duration"),
                "caption_status": "available" if str(cd.get("caption")).lower() == "true" else "none",
                "subscriber_count": None,
            }
            if sn.get("channelId"):
                chan_ids.add(sn["channelId"])
    # 채널 구독자수(조회수/구독자 비율 = 흥행 신호) — videos.list엔 없어 channels.list로 보강
    subs = {}
    cids = list(chan_ids)
    for i in range(0, len(cids), 50):
        try:
            resp = yt.channels().list(part="statistics", id=",".join(cids[i:i + 50])).execute()
            for it in resp.get("items", []):
                subs[it["id"]] = _int(it.get("statistics", {}).get("subscriberCount"))
        except Exception:
            pass
    for m in out.values():
        m["subscriber_count"] = subs.get(m.get("channel_id"))
    return out
