"""R2(Cloudflare, S3 호환) 오브젝트 스토리지 추상화 — 큰 파일(원본 PDF·이미지·산출물)을
PostgreSQL bytea 대신 R2에 저장해 DB 1GB 압박·비용을 해소한다(GPT 권고).

활성 조건: 환경변수 R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET 4개 모두 존재.
없으면 enabled()=False → 호출부는 기존 PG bytea 경로 유지(무영향). boto3는 lazy import라 미설치·미설정이어도
이 모듈 import 자체는 안전(스켈레톤 배포 무해).

키 구조(테넌트 격리):
  hospitals/{hospital_id}/materials/{material_id}/{version_id}/{filename}
  hospitals/{hospital_id}/artifacts/{topic}/{kind}
  hospitals/{hospital_id}/images/{block_key}.jpg
  hospitals/{hospital_id}/exports/{export_id}.docx
서빙은 public 금지 — presigned_get(짧은 만료) 또는 서버 프록시(권한검사 후 get).
"""
import os
import hashlib

_ENV = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")

def _cfg():
    return tuple(os.environ.get(k) for k in _ENV)

def enabled():
    """R2 설정 4개가 모두 있으면 True. 하나라도 없으면 False(→ PG bytea 폴백)."""
    return all(_cfg())

_client = None
def _s3():
    global _client
    if _client is None:
        import boto3   # lazy — 미설정 시 import 안 함
        ep, ak, sk, _bk = _cfg()
        _client = boto3.client("s3", endpoint_url=ep, aws_access_key_id=ak,
                               aws_secret_access_key=sk, region_name="auto")
    return _client

def bucket():
    return _cfg()[3]

def put(key, data, content_type="application/octet-stream"):
    """바이트를 R2에 저장. 반환 {key,size,sha256}. R2 미설정이면 RuntimeError."""
    if not enabled():
        raise RuntimeError("R2 미설정(env 필요)")
    data = bytes(data)
    _s3().put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)
    return {"key": key, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

def get(key):
    r = _s3().get_object(Bucket=bucket(), Key=key)
    return r["Body"].read()

def delete(key):
    _s3().delete_object(Bucket=bucket(), Key=key)

def exists(key):
    try:
        _s3().head_object(Bucket=bucket(), Key=key)
        return True
    except Exception:
        return False

def presigned_get(key, expiry=900):
    """짧은 만료의 GET presigned URL(공개 노출 금지 · 권한검사 통과 후 발급)."""
    return _s3().generate_presigned_url("get_object",
                                        Params={"Bucket": bucket(), "Key": key}, ExpiresIn=expiry)

def health():
    """설정·연결 점검(관리용). 반환 dict."""
    if not enabled():
        return {"enabled": False, "reason": "env 4개 중 일부 없음", "have": [k for k in _ENV if os.environ.get(k)]}
    try:
        _s3().head_bucket(Bucket=bucket())
        return {"enabled": True, "bucket": bucket(), "ok": True}
    except Exception as e:
        return {"enabled": True, "bucket": bucket(), "ok": False, "error": str(e)[:200]}
