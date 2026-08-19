"""Probe 2: Chrome TLS impersonation via curl_cffi against Meta careers GraphQL."""
import re, json, sys
from curl_cffi import requests as creq

S = creq.Session(impersonate="chrome124")
r = S.get("https://www.metacareers.com/jobsearch/", timeout=30)
print("PAGE:", r.status_code, len(r.text))
m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', r.text)
if not m:
    print("NO LSD"); sys.exit(0)
lsd = m.group(1)
print("LSD ok, cookies:", list(S.cookies.keys()))

SI = {"q": None, "divisions": [], "offices": [], "roles": [], "leadership_levels": [],
      "saved_jobs": [], "saved_searches": [], "sub_teams": [], "teams": [],
      "is_leadership": False, "is_remote_only": False, "sort_by_new": True,
      "results_per_page": None}

def gql(doc_id, variables, tag):
    resp = S.post("https://www.metacareers.com/graphql",
                  headers={"x-fb-lsd": lsd, "Origin": "https://www.metacareers.com",
                           "Referer": "https://www.metacareers.com/jobsearch/",
                           "Content-Type": "application/x-www-form-urlencoded"},
                  data={"lsd": lsd, "doc_id": doc_id, "variables": json.dumps(variables)},
                  timeout=30)
    print(f"[{tag}] HTTP {resp.status_code} :: {resp.text[:250]}".replace("\n", " "))
    return resp

gql("26446976041587120", {"search_input": SI}, "count")
r2 = gql("27506805582236862", {"isLoggedIn": False, "viewasUserID": None, "search_input": SI}, "resultsA")
if r2.status_code == 200:
    try:
        d = r2.json()
        blob = json.dumps(d)
        print("RESULTS SIZE:", len(blob))
        ids = re.findall(r'"id":"(\d{10,20})"', blob)[:5]
        titles = re.findall(r'"title":"([^"]{5,60})"', blob)[:5]
        print("IDS:", ids)
        print("TITLES:", titles)
    except Exception as e:
        print("parse err:", e)
gql("27129360303422352", {"isLoggedIn": False, "viewasUserID": None, "search_input": SI}, "resultsV2A")
