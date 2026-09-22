#!/usr/bin/env python3
"""A minimal Dune API client (standard library only).

The endpoints follow the official API v1 docs; **verify each response shape against a real call
before relying on it** (a project rule: documented behaviour must be measured — the lesson from
another vendor's docs).

Usage (always through ./bin/dune-py):

  ./bin/dune-py /work/dune/lib/dune_client.py run <query_id> [param=value ...]
  ./bin/dune-py /work/dune/lib/dune_client.py csv <query_id> > out.csv   # fetch a cached result
  ./bin/dune-py /work/dune/lib/dune_client.py upload <table_name> <csv path>

Credit discipline: both execution and export burn credits; aggregate inside Dune and export only the
conclusion table; get a new query working on a small LIMIT before opening it up.
"""
import json, os, sys, time, urllib.request, urllib.error

BASE = "https://api.dune.com/api/v1"
KEY  = os.environ["DUNE_API_KEY"]

def _req(path, body=None, raw=False, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"X-Dune-API-Key": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            payload = resp.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code}: {e.read().decode()[:300]}\n")
        raise

def execute(query_id, params=None, performance="medium"):
    body = {"performance": performance}
    if params: body["query_parameters"] = params
    return _req(f"/query/{query_id}/execute", body)["execution_id"]

def wait(execution_id, poll=5, log=True, deadline=None):
    """Poll until a terminal state. With log=True the cost is written to dune/results/cost_log.jsonl.

    ⚠️ **Every call is billed twice** (stated on the official billing page, checked 2026-08-19):
        1. Query Execution — `execution_cost_credits` in the status response, which is what this
           function records
        2. API Result Read — charged separately when the results are fetched, and **that number is
           not in the status response**
    The official wording: "If you execute a query but never pull the results, you are only charged
    for the execution." — so reading the results is a separate charge.

    The read-cost formula (**derived by measurement; it disagrees with the official docs**):
        read_credits = result_set_bytes / 100_000
    Three checkpoints, all matching exactly:
        50 rows, 4,750 bytes → 0.0475 (billed 0.0475)
         6 rows,   147 bytes → 0.0015 (billed 0.0015)
         1 row,     24 bytes → 0.0002 (billed 0.0002)
    ⚠️ The official FAQ says `datapoints = max(rows x cols, ceil(bytes/100))` with 1 Analyst credit =
       1,000 datapoints. By that, 50 rows x 9 columns = 450 dp → 0.45, **ten times the actual bill**.
       Real billing depends only on the byte count, not on rows or columns — the measured formula
       wins.

    So the `credits` this function records is **the execution cost, not the total**. The log also
    carries `read_credits_est` as an estimate of the read cost, and `total_est` as their sum. The
    authoritative total is Dune's own Activity page.
    """
    # `deadline` (seconds, optional) exists because a caller may hold a lock while waiting: Dune has
    # no upper bound on how long an execution stays PENDING, and an unbounded wait would block every
    # other caller behind it. Leaving it None keeps the original behaviour (wait for ever).
    t_end = None if deadline is None else time.time() + deadline
    while True:
        st = _req(f"/execution/{execution_id}/status")
        state = st.get("state")
        if state in ("QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            if log:
                rm = st.get("result_metadata") or {}
                exec_c = st.get("execution_cost_credits") or 0
                # read cost = bytes / 100,000 (derived by measurement, three matching checkpoints;
                # the official formula overstates it tenfold)
                rows_n = rm.get("row_count") or 0
                cols_n = len(rm.get("column_names") or []) or 0
                by_n   = rm.get("result_set_bytes") or 0
                read_c = by_n / 100000.0
                rec = {"eid": execution_id, "query_id": st.get("query_id"), "state": state,
                       "credits": exec_c,                    # execution cost (an official field, exact)
                       "read_credits": round(read_c, 6),     # read cost (measured formula, verified at three points)
                       "total": round(exec_c + read_c, 6),
                       "started": st.get("execution_started_at"), "ended": st.get("execution_ended_at"),
                       "rows": rows_n, "cols": cols_n, "bytes": by_n}
                try:
                    # ★ The path can be overridden with DUNE_COST_LOG — inside the service image the
                    #   root is /app rather than /work, and a failed write would be swallowed by the
                    #   except below, losing the cost record entirely.
                    _cl = os.environ.get("DUNE_COST_LOG", "/work/dune/results/cost_log.jsonl")
                    os.makedirs(os.path.dirname(_cl), exist_ok=True)
                    with open(_cl, "a") as f:
                        f.write(json.dumps(rec) + "\n")
                except Exception:
                    pass
                sys.stderr.write(f"[cost] {state} exec={exec_c:.4f} + read={read_c:.4f} "
                                 f"= {exec_c + read_c:.4f} credits  ({rows_n} rows, {by_n} bytes)\n")
            return st
        if t_end is not None and time.time() > t_end:
            raise TimeoutError("execution %s is still %s after %ds — giving up rather than blocking "
                               "the caller indefinitely" % (execution_id, state, deadline))
        time.sleep(poll)

def results(execution_id, limit=None):
    q = f"?limit={limit}" if limit else ""
    return _req(f"/execution/{execution_id}/results{q}")

def latest_csv(query_id):
    return _req(f"/query/{query_id}/results/csv", raw=True)

def create_query(name, sql, private=True):
    return _req("/query", {"name": name, "query_sql": sql, "is_private": private})

def update_query(query_id, sql, name=None):
    """Edit an existing query's SQL instead of creating new ones.
    This plan has a private-query quota of 0 (402 "Max number of private queries reached"), and while
    public queries are unlimited they are visible to everyone — reusing one id is cleaner than piling
    up public queries."""
    body = {"query_sql": sql}
    if name: body["name"] = name
    return _req(f"/query/{query_id}", body, method="PATCH")   # measured: POST returns 405, PATCH is required


def upload_csv(table_name, csv_path, description="", private=True):
    # Note: the old /table/upload/csv endpoint was retired (removed 2026-03-01); use /uploads/csv
    # Upload billing: 3 credits/GB (minimum 1); storage cap is 1GB on the Analyst plan
    # ⚠️ Measured 2026-08-15: is_private=True is rejected on this plan — 402 "Private data uploads are
    #    not enabled for this account." (it fails loudly rather than silently becoming public).
    #    private=True is the default as a fuse: pass private=False only for data that has been
    #    deliberately cleared for publication.
    data = open(csv_path).read()
    return _req("/uploads/csv", {
        "table_name": table_name, "data": data,
        "description": description, "is_private": private})

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "run":
        qid = int(sys.argv[2])
        params = dict(a.split("=", 1) for a in sys.argv[3:])
        eid = execute(qid, params or None)
        sys.stderr.write(f"execution_id={eid}\n")
        st = wait(eid)
        sys.stderr.write(f"state={st.get('state')}\n")
        if st.get("state") == "QUERY_STATE_COMPLETED":
            print(json.dumps(results(eid), ensure_ascii=False))
    elif cmd == "csv":
        sys.stdout.buffer.write(latest_csv(int(sys.argv[2])))
    elif cmd == "upload":
        print(json.dumps(upload_csv(sys.argv[2], sys.argv[3])))
    else:
        print(__doc__)
