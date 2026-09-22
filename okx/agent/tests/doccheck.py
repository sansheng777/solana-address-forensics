import os, re, subprocess
A="okx/agent"
code=open(A+"/verify.py").read(); sch=open(A+"/SCHEMA.md").read(); rd=open(A+"/README.md").read()
pr=open(A+"/prompt.md").read(); run=open(A+"/run.py").read()
bad=[]
# 2026-09-22: the checks are now numbered as plain "(n)" in verify.py, so the old circled-number
# range is gone. We read the numbers straight out of the source.

# ⚠️ Both forms must be counted: a direct v.append("(7) ...") and one produced through
# _scan(..., "(7)", ...). After (7)/(8) moved into _scan on 2026-09-20, a checker that only knew
# the first form started raising false alarms — and a checker that cries wolf is no checker.
nums_code=sorted({x for x in (re.findall(r'v\.append\("\((\d+)\)', code)
                              + re.findall(r'_scan\([^)]*?"\((\d+)\)"', code))}, key=int)
nums_sch=sorted(set(re.findall(r'^\| (\d+) \|', sch, re.M)))
if sorted(nums_code,key=int)!=sorted(nums_sch,key=int): bad.append("SCHEMA check table %s != verify.py actual %s"%(nums_sch,nums_code))
blocks=re.findall(r'"([^"]+)"', re.search(r'ANALYTIC_BLOCKS = \[(.*?)\]', code, re.S).group(1))
for b in blocks:
    if b not in pr: bad.append("verify requires block '%s' to be covered, the prompt never mentions it"%b)
for f in ("forbidden.txt","vague.txt","follower-banned.txt"):
    if not [l for l in open(A+"/"+f) if l.split('#')[0].strip()]: bad.append("%s is empty"%f)
for t,name in ((A+"/tests/test_agent.py","layer 4"),(A+"/tests/test_robust.py","network layer")):
    out=subprocess.run(["python3",t],capture_output=True,text=True).stdout
    n=re.search(r'\((\d+) assertions\)',out)
    if n and n.group(1) not in rd: bad.append("README assertion count for %s != actual %s"%(name,n.group(1)))
if 'body["reasoning_effort"]' in run: bad.append("run.py still uses the wrong parameter reasoning_effort")
# ★ verify's check count: CHECK_COUNT / the SCHEMA table / the actual appends must all agree
import subprocess as _sp
_cc = int(re.search(r'CHECK_COUNT = (\d+)', code).group(1))
if _cc != len(nums_code): bad.append("verify.CHECK_COUNT=%d != actual %d" % (_cc, len(nums_code)))
_app = open("okx/service/app.py", encoding="utf-8").read()
if 'verify.CHECK_COUNT' not in _app and re.search(r'"checks":\s*\d', _app):
    bad.append("app.py hard-codes the check count; it must reference verify.CHECK_COUNT")
# ★ the service must hand its Dune cache directory to store, or every call really burns credits
if "store.CACHE_DIR" not in _app: bad.append("app.py does not set store.CACHE_DIR — the service would hit Dune on every call")
# okx/.env is local-only (gitignored), so a clone of this repo simply skips the two checks below
if os.path.exists("okx/.env"):
    env=dict(l.split("=",1) for l in open("okx/.env") if "=" in l and not l.startswith("#"))
    mo=env.get("OKX_AGENT_MODEL","").strip(); ef=env.get("OKX_AGENT_EFFORT","").strip()
    if mo not in rd: bad.append("the model %s in .env is not documented in the README"%mo)
    if "OKX_AGENT_EFFORT=%s"%ef not in rd: bad.append("EFFORT=%s in .env is not documented in the README"%ef)
else:
    print("note: okx/.env not present, the model/effort documentation checks were skipped")
print("check ids: code=%s SCHEMA=%s"%(nums_code,nums_sch))
print("blocks to cover: %d  %s"%(len(blocks),blocks))
print()
print(("FAIL  %d inconsistency(ies):\n   "%len(bad))+"\n   ".join(bad) if bad else "OK  docs and code agree")
