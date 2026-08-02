"""이미지 bytea 용량 집계 — R2/S3 이전 의사결정용(GPT). 매 요청이 아니라 관리 명령/정기 쿼리.

  python -m store.storage_stats        # OWNER_URL 사용, 표 출력 + storage_stats 이벤트 방출

전체/병원별 scene_images 용량·개수·평균/최대·최근 증가량·DB 대비 비중. 병원 식별자는 해시로만 노출."""
import os, sys
from sqlalchemy import text
from services.observability import emit, hid


def scene_image_stats(engine):
    """집계 dict 반환. table-level 크기는 RLS 무관, 행 집계는 app_owner(si_def USING true)로."""
    with engine.connect() as cn:
        try:
            cn.execute(text("SET ROLE app_owner"))       # 크로스테넌트 행 집계(멤버면 성공, superuser면 불필요)
        except Exception:
            pass
        total_bytes = cn.execute(text("select pg_total_relation_size('scene_images')")).scalar()
        db_bytes = cn.execute(text("select pg_database_size(current_database())")).scalar()
        agg = cn.execute(text(
            "select count(*) n, coalesce(sum(pg_column_size(data)),0) sum_b, "
            "coalesce(avg(pg_column_size(data)),0)::bigint avg_b, coalesce(max(pg_column_size(data)),0) max_b, "
            "count(*) filter (where updated_at > now() - interval '7 days') n_7d "
            "from scene_images")).first()
        per_h = cn.execute(text(
            "select hospital_id, count(*) n, coalesce(sum(pg_column_size(data)),0) b "
            "from scene_images group by hospital_id order by b desc")).all()
    return {
        "images": agg.n, "data_bytes": int(agg.sum_b), "avg_bytes": int(agg.avg_b), "max_bytes": int(agg.max_b),
        "images_last_7d": agg.n_7d,
        "table_total_bytes": int(total_bytes or 0), "db_bytes": int(db_bytes or 0),
        "pct_of_db": round(100.0 * (total_bytes or 0) / db_bytes, 2) if db_bytes else 0.0,
        "per_hospital": [{"hospital": hid(r.hospital_id), "images": r.n, "data_bytes": int(r.b)} for r in per_h],
    }


def _mb(b):
    return f"{b/1048576:.1f}MB"


def main():
    from store.db import make_engine
    url = os.environ.get("OWNER_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("환경변수 OWNER_URL(권장) 또는 DATABASE_URL 필요")
    s = scene_image_stats(make_engine(url))
    print(f"scene_images: {s['images']}장  데이터 {_mb(s['data_bytes'])}  "
          f"평균 {s['avg_bytes']/1024:.0f}KB  최대 {_mb(s['max_bytes'])}  최근7일 {s['images_last_7d']}장")
    print(f"테이블 총크기 {_mb(s['table_total_bytes'])}  /  DB {_mb(s['db_bytes'])}  = {s['pct_of_db']}%")
    for h in s["per_hospital"]:
        print(f"  · {h['hospital']}  {h['images']}장  {_mb(h['data_bytes'])}")
    emit("storage_stats", images=s["images"], data_bytes=s["data_bytes"], max_bytes=s["max_bytes"],
         images_last_7d=s["images_last_7d"], pct_of_db=s["pct_of_db"], hospitals=len(s["per_hospital"]))


if __name__ == "__main__":
    main()
