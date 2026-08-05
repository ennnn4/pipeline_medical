"""자막 수집 — provider adapter(교체·mock 가능). 개발지침 6절.

원칙:
 - 외부 provider 실패가 전체 대본 프로젝트를 망치지 않게 '수동 fallback'(붙여넣기·파일 업로드) 보장.
 - 타 채널 영상 무단 다운로드·음원추출을 '기본 경로'로 만들지 않음(권리 보유 파일만 STT — 별도 provider).
 - 특정 외부 업체 종속 금지 → provider 교체 가능한 인터페이스.
 - 자막 원본 출처·provider·timestamp 기록.
 - 순수 로직(DB·네트워크 분리) → 테스트에서 mock 가능.
"""
import re
from dataclasses import dataclass, field

VALID_STATUS = ("pending", "fetching", "available", "provider_failed", "manual_required", "completed")


@dataclass
class TranscriptResult:
    status: str                              # VALID_STATUS
    text: str = ""
    has_timestamps: bool = False
    lang: str = None
    provider: str = None
    source_note: str = ""
    segments: list = field(default_factory=list)   # [{"start","text"}] (timestamp 있을 때)


def youtube_video_id(url):
    """유튜브 URL → video_id(11자). shorts/embed/youtu.be 지원. 실패 시 None."""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


class TranscriptProvider:
    name = "base"
    def fetch(self, video_url, **kw) -> TranscriptResult:
        raise NotImplementedError


class ExternalTranscriptProvider(TranscriptProvider):
    """youtube-transcript-api(비공식) — 서버 IP 차단 잦음. 실패하면 provider_failed → 수동 fallback."""
    name = "external"
    def fetch(self, video_url, lang_pref=("ko", "en"), **kw):
        vid = youtube_video_id(video_url)
        if not vid:
            return TranscriptResult(status="provider_failed", provider=self.name, source_note="URL에서 video_id 추출 실패")
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except Exception:
            return TranscriptResult(status="provider_failed", provider=self.name, source_note="youtube-transcript-api 미설치")
        try:
            data = None
            try:                              # 신·구 API 모두 방어적 시도
                data = YouTubeTranscriptApi.get_transcript(vid, languages=list(lang_pref))
            except Exception:
                tl = YouTubeTranscriptApi.list_transcripts(vid)
                tr = None
                for lg in lang_pref:
                    try:
                        tr = tl.find_transcript([lg]); break
                    except Exception:
                        pass
                tr = tr or next(iter(tl))
                data = tr.fetch()
            segs = [{"start": round(float(d.get("start", 0)), 2), "text": d.get("text", "")} for d in data]
            txt = "\n".join(d.get("text", "") for d in data).strip()
            if not txt:
                return TranscriptResult(status="provider_failed", provider=self.name, source_note="빈 자막")
            return TranscriptResult(status="available", text=txt, has_timestamps=True,
                                    provider=self.name, segments=segs, source_note=f"youtube-transcript-api:{vid}")
        except Exception as e:
            return TranscriptResult(status="provider_failed", provider=self.name,
                                    source_note=f"수집 실패: {type(e).__name__}")


class ManualTranscriptProvider(TranscriptProvider):
    """사용자가 붙여넣은 자막."""
    name = "manual"
    def fetch(self, video_url, pasted_text="", **kw):
        t = (pasted_text or "").strip()
        if not t:
            return TranscriptResult(status="manual_required", provider=self.name, source_note="붙여넣은 자막 없음")
        return TranscriptResult(status="available", text=t, provider=self.name, source_note="수동 붙여넣기")


class UploadedTranscriptProvider(TranscriptProvider):
    """SRT/VTT/TXT 파일 업로드(다글로 등에서 추출한 파일 운영 fallback)."""
    name = "upload"
    def fetch(self, video_url, file_bytes=b"", filename="", **kw):
        if not file_bytes:
            return TranscriptResult(status="manual_required", provider=self.name, source_note="업로드 파일 없음")
        raw = file_bytes.decode("utf-8", "ignore") if isinstance(file_bytes, (bytes, bytearray)) else str(file_bytes)
        low = (filename or "").lower()
        if low.endswith((".srt", ".vtt")) or "-->" in raw:
            segs, txt = parse_srt_vtt(raw)
            return TranscriptResult(status="available", text=txt, has_timestamps=bool(segs),
                                    provider=self.name, segments=segs, source_note=f"업로드 {filename or 'srt/vtt'}")
        return TranscriptResult(status="available", text=raw.strip(), provider=self.name,
                                source_note=f"업로드 {filename or 'txt'}")


_TS = re.compile(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->")

def parse_srt_vtt(raw):
    """SRT/VTT → (segments[{start,text}], 평문). 태그·인덱스·헤더 제거."""
    segs, texts = [], []
    for block in re.split(r"\n\s*\n", (raw or "").strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        start, txt_lines = None, []
        for l in lines:
            m = _TS.search(l)
            if m:
                start = m.group(1)
            elif re.match(r"^\d+$", l.strip()):
                continue                        # SRT 인덱스줄
            elif l.strip().upper().startswith("WEBVTT"):
                continue
            else:
                txt_lines.append(re.sub(r"<[^>]+>", "", l).strip())
        t = " ".join(x for x in txt_lines if x)
        if t:
            if start:
                segs.append({"start": start, "text": t})
            texts.append(t)
    return segs, "\n".join(texts)


def collect_transcript(video_url, *, pasted_text=None, file_bytes=None, filename=None, try_external=True):
    """우선순위: 사용자 제공(업로드>붙여넣기) → 외부 API → 실패 시 manual_required.
    외부 실패해도 전체를 실패시키지 않고 '수동 필요' 상태를 반환한다."""
    if file_bytes:
        r = UploadedTranscriptProvider().fetch(video_url, file_bytes=file_bytes, filename=filename or "")
        if r.status == "available":
            return r
    if pasted_text:
        r = ManualTranscriptProvider().fetch(video_url, pasted_text=pasted_text)
        if r.status == "available":
            return r
    if try_external:
        r = ExternalTranscriptProvider().fetch(video_url)
        if r.status == "available":
            return r
        return TranscriptResult(status="manual_required", provider="external",
                                source_note=(r.source_note or "") + " → 수동 자막(붙여넣기/파일) 필요")
    return TranscriptResult(status="manual_required", source_note="자막 소스 없음 — 붙여넣기/업로드 필요")
