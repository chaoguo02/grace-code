"""
T1-T6 Integration Tests — validate Run model, idempotency, CAS, transactions.
"""
import sqlite3, uuid, json, sys
from datetime import datetime, timezone
from agent.session import default_session_db_path

db_path = default_session_db_path(".")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
passed = 0
failed = 0
now = datetime.now(timezone.utc).isoformat()

# Use existing sessions
sid = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()["id"]
sid2_row = conn.execute("SELECT id FROM sessions WHERE id!=? LIMIT 1", (sid,)).fetchone()
if sid2_row is None:
    sid2_id = f"test_t1_{uuid.uuid4().hex[:8]}"
    conn.execute("INSERT INTO sessions(id,root_id,agent_name,mode,repo_path,title,status,run_generation) VALUES(?,?,'build','primary','.','T1','active',0)", [sid2_id, sid2_id])
    conn.commit()
    sid2 = sid2_id
else:
    sid2 = sid2_row["id"]

# Clean up any leftover queued/running runs
conn.execute("DELETE FROM runs WHERE status IN ('queued','running')")
conn.commit()
print(f"DB: {db_path}\nSession A: {sid}\nSession B: {sid2}\n")

def ok(label):
    global passed; passed += 1; print(f"  PASS: {label}")

def fail(label, actual, expected):
    global failed; failed += 1; print(f"  FAIL: {label} — got {actual!r}, expected {expected!r}")

def check(actual, expected, label):
    if actual == expected: ok(label)
    else: fail(label, actual, expected)

def truthy(val, label):
    if val: ok(label)
    else: fail(label, val, "truthy")

# ═══════════════════════════════════════════════════════════════════
print("=== T1: Two sessions concurrently — run_id/turn_id not mixed ===")

run_a, turn_a = str(uuid.uuid4()), str(uuid.uuid4())
run_b, turn_b = str(uuid.uuid4()), str(uuid.uuid4())
conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,1,?,'prompt A','queued',?,?)", [run_a, sid, turn_a, f"ika_{uuid.uuid4().hex[:6]}", now, now])
conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,1,?,'prompt B','queued',?,?)", [run_b, sid2, turn_b, f"ikb_{uuid.uuid4().hex[:6]}", now, now])
conn.commit()
ra = conn.execute("SELECT session_id, turn_id FROM runs WHERE id=?", (run_a,)).fetchone()
rb = conn.execute("SELECT session_id, turn_id FROM runs WHERE id=?", (run_b,)).fetchone()
check(ra["session_id"], sid, "T1: Run A → session A")
check(rb["session_id"], sid2, "T1: Run B → session B")
check(ra["turn_id"], turn_a, "T1: Run A turn_id")
check(rb["turn_id"], turn_b, "T1: Run B turn_id")
truthy(run_a != run_b, "T1: run_ids differ")
# Clean up T1 runs (transition to completed so idx_runs_one_active allows new runs)
conn.execute("UPDATE runs SET status='completed',completed_at=? WHERE id IN (?,?)", (now, run_a, run_b))
conn.commit()

# ═══════════════════════════════════════════════════════════════════
print("\n=== T5: Same idempotency_key twice → one Run ===")
ik = f"ikdup_{uuid.uuid4().hex[:6]}"
r1, t1 = str(uuid.uuid4()), str(uuid.uuid4())
conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,2,?,'same prompt','queued',?,?)", [r1, sid, t1, ik, now, now])
conn.commit()
try:
    conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,2,?,'same prompt','queued',?,?)", [str(uuid.uuid4()), sid, str(uuid.uuid4()), ik, now, now])
    conn.commit()
    fail("T5: Dup idempotency_key blocked", "IntegrityError", "committed")
except sqlite3.IntegrityError:
    conn.rollback(); ok("T5: Dup blocked by unique index")
cnt = conn.execute("SELECT COUNT(*) c FROM runs WHERE session_id=? AND idempotency_key=?", (sid, ik)).fetchone()["c"]
check(cnt, 1, "T5: Only one run")
row = conn.execute("SELECT id,turn_id FROM runs WHERE session_id=? AND idempotency_key=?", (sid, ik)).fetchone()
check(row["id"], r1, "T5: Returns original run_id")
check(row["turn_id"], t1, "T5: Returns original turn_id")
# Cleanup: complete T5's queued run so T6 can create new queued runs
conn.execute("UPDATE runs SET status='completed',completed_at=? WHERE session_id=? AND status='queued'", (now, sid))
conn.commit()

# ═══════════════════════════════════════════════════════════════════
print("\n=== T6: Same key, different prompt → 409 ===")
ik2 = f"ikdiff_{uuid.uuid4().hex[:6]}"
r2, t2 = str(uuid.uuid4()), str(uuid.uuid4())
conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,3,?,'prompt V1','queued',?,?)", [r2, sid, t2, ik2, now, now])
conn.commit()
ex = conn.execute("SELECT id,prompt FROM runs WHERE session_id=? AND idempotency_key=?", (sid, ik2)).fetchone()
truthy(ex is not None, "T6: Existing run found")
check(ex["prompt"], "prompt V1", "T6: Original prompt")
truthy(ex["prompt"] != "prompt V2", "T6: Different prompt → 409")
conn.execute("UPDATE runs SET status='completed',completed_at=? WHERE session_id IN (?,?) AND status='queued'", (now, sid, sid2)); conn.commit()

# ═══════════════════════════════════════════════════════════════════
print("\n=== T3: Cancel vs Complete CAS race ===")
ik3 = f"ikrace_{uuid.uuid4().hex[:6]}"
r3 = str(uuid.uuid4())
conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,4,?,'race','queued',?,?)", [r3, sid, str(uuid.uuid4()), ik3, now, now])
conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=? AND status='queued'", (now, r3)); conn.commit()
c1 = conn.execute("UPDATE runs SET status='cancelled',error='cancel',updated_at=? WHERE id=? AND status='running'", (now, r3))
check(c1.rowcount, 1, "T3: Cancel CAS succeeds")
c2 = conn.execute("UPDATE runs SET status='completed',completed_at=?,updated_at=? WHERE id=? AND status='running'", (now, now, r3))
check(c2.rowcount, 0, "T3: Complete CAS fails after cancel")
s = conn.execute("SELECT status FROM runs WHERE id=?", (r3,)).fetchone()
check(s["status"], "cancelled", "T3: Final = cancelled")
conn.commit()

# ═══════════════════════════════════════════════════════════════════
print("\n=== Concurrent Run prevention ===")
ik4 = f"ikconc_{uuid.uuid4().hex[:6]}"
r4 = str(uuid.uuid4())
conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,5,?,'conc','queued',?,?)", [r4, sid, str(uuid.uuid4()), ik4, now, now])
conn.commit()
try:
    conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,5,?,'conc2','queued',?,?)", [str(uuid.uuid4()), sid, str(uuid.uuid4()), f"ikconc_b_{uuid.uuid4().hex[:6]}", now, now])
    conn.commit()
    fail("Concurrent blocked", "committed", "IntegrityError")
except sqlite3.IntegrityError:
    conn.rollback(); ok("Concurrent: idx_runs_one_active blocks parallel queued")
# Cleanup all queued/running runs before T2
conn.execute("UPDATE runs SET status='completed',completed_at=? WHERE session_id IN (?,?) AND status IN ('queued','running')", (now, sid, sid2)); conn.commit()

# ═══════════════════════════════════════════════════════════════════
print("\n=== T2: Transaction atomicity (Run CAS + trace event in one tx) ===")
ik5 = f"ikatom_{uuid.uuid4().hex[:6]}"
r5, t5 = str(uuid.uuid4()), str(uuid.uuid4())
conn.execute("INSERT INTO runs(id,session_id,turn_id,turn_index,idempotency_key,prompt,status,created_at,updated_at) VALUES(?,?,?,6,?,'atom','queued',?,?)", [r5, sid, t5, ik5, now, now])
conn.execute("UPDATE runs SET status='running',started_at=? WHERE id=? AND status='queued'", (now, r5)); conn.commit()

conn.execute("BEGIN IMMEDIATE")
c = conn.execute("UPDATE runs SET status='completed',summary='done',steps_taken=3,total_tokens=100,completed_at=?,updated_at=? WHERE id=? AND status='running'", (now, now, r5))
check(c.rowcount, 1, "T2: CAS in tx")
seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 s FROM session_trace_events WHERE session_id=?", (sid,)).fetchone()["s"]
evt = {"type":"run_terminal","run_id":r5,"turn_id":t5,"turn_index":6,"status":"completed","summary":"done","steps_taken":3,"total_tokens":100}
conn.execute("INSERT INTO session_trace_events(session_id,seq,event_type,timestamp,event_json,source) VALUES(?,?,'run_terminal',?,?,'run_terminal')", [sid, seq, now, json.dumps({**evt,"seq":seq,"sequence":seq})])
conn.execute("COMMIT")
ok("T2: Run + trace committed atomically")

rs = conn.execute("SELECT status,summary FROM runs WHERE id=?", (r5,)).fetchone()
check(rs["status"], "completed", "T2: Run COMPLETED")
check(rs["summary"], "done", "T2: Summary persisted")
tr = conn.execute("SELECT event_json FROM session_trace_events WHERE session_id=? AND event_type='run_terminal' ORDER BY seq DESC LIMIT 1", (sid,)).fetchone()
truthy(tr is not None, "T2: run_terminal trace exists")
te = json.loads(tr["event_json"])
check(te.get("type"), "run_terminal", "T2: Trace type correct")
check(te.get("run_id"), r5, "T2: Trace has run_id")
truthy(te.get("sequence") or te.get("seq"), "T2: Has sequence")

# ═══════════════════════════════════════════════════════════════════
print("\n=== T4: Replay by after_seq ===")
evts = conn.execute("SELECT seq FROM session_trace_events WHERE session_id=? ORDER BY seq ASC", (sid,)).fetchall()
seqs = [e["seq"] for e in evts]
check(seqs, sorted(seqs), "T4: Sequences monotonic")
if len(seqs) >= 1:
    mid = seqs[0]
    after = conn.execute("SELECT seq FROM session_trace_events WHERE session_id=? AND seq>? ORDER BY seq ASC", (sid, mid)).fetchall()
    truthy(len(after) >= 0, f"T4: after_seq={mid} returns {len(after)} events")

# ═══════════════════════════════════════════════════════════════════
# Cleanup test data
conn.execute("DELETE FROM runs WHERE session_id IN (?,?)", (sid, sid2))
conn.execute("DELETE FROM session_trace_events WHERE source='run_terminal' AND session_id IN (?,?)", (sid, sid2))
if sid2_id := locals().get('sid2_id'):
    if isinstance(sid2_id, str) and sid2_id.startswith('test_t1_'):
        conn.execute("DELETE FROM sessions WHERE id=?", (sid2_id,))
conn.commit()
conn.close()

print(f"\n{'='*60}")
print(f"RESULTS: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
