"""테스트 공용 헬퍼(픽스처·테스트가 공유). store.* 로 import 가능."""
import uuid
from sqlalchemy import text

def sqlstate(exc):
    for o in (getattr(exc, "orig", None), exc):
        try:
            return o.args[0].get("C")
        except Exception:
            pass
    return type(exc).__name__

def set_tenant(conn, hospital_id):
    conn.execute(text("select set_config('app.hospital_id', :h, true)"), {"h": str(hospital_id)})

def new_version(conn, hospital_id, source="migration"):
    sc, v = uuid.uuid4(), uuid.uuid4()
    conn.execute(text("insert into scripts(id,hospital_id,topic) values(:s,:h,'t')"), {"s": sc, "h": hospital_id})
    conn.execute(text("insert into script_versions(id,hospital_id,script_id,version_no,source) values(:v,:h,:s,1,:src)"),
                 {"v": v, "h": hospital_id, "s": sc, "src": source})
    conn.execute(text("insert into version_approval_states(id,hospital_id,version_id,status) values(:i,:h,:v,'none')"),
                 {"i": uuid.uuid4(), "h": hospital_id, "v": v})
    return sc, v

def new_block(conn, hospital_id, version_id, order_index=0, key=None):
    b = uuid.uuid4()
    conn.execute(text("insert into script_blocks(id,hospital_id,version_id,stable_block_key,order_index,block_type,text) "
                      "values(:b,:h,:v,:k,:o,'explanation','x')"),
                 {"b": b, "h": hospital_id, "v": version_id, "k": key or f"blk_{order_index}", "o": order_index})
    return b

def new_sentence(conn, hospital_id, version_id, block_id, idx=0):
    s = uuid.uuid4()
    conn.execute(text("insert into script_sentences(id,hospital_id,version_id,block_id,sentence_index,text,start_offset,end_offset,offset_unit,segmenter_version) "
                      "values(:s,:h,:v,:b,:i,'x',0,1,'codepoint','v1')"),
                 {"s": s, "h": hospital_id, "v": version_id, "b": block_id, "i": idx})
    return s

def new_claim(conn, hospital_id, version_id, sentence_id, idx=0):
    c = uuid.uuid4()
    conn.execute(text("insert into claims(id,hospital_id,version_id,sentence_id,claim_index,claim_text,claim_type,detection_method) "
                      "values(:c,:h,:v,:s,:i,'x','statistic','migration')"),
                 {"c": c, "h": hospital_id, "v": version_id, "s": sentence_id, "i": idx})
    return c
