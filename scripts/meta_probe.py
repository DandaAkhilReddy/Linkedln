"""One-shot probe: can a GitHub Actions runner reach Meta's careers GraphQL?"""
import re, json, requests, sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

r = S.get("https://www.metacareers.com/jobsearch/", timeout=30,
          headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                   "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                   "Sec-Fetch-Site": "none"})
print("PAGE:", r.status_code, len(r.text))
m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', r.text)
if not m:
    print("NO LSD TOKEN"); sys.exit(0)
lsd = m.group(1)
print("LSD ok")

SI = {"q": None, "divisions": [], "offices": [], "roles": [], "leadership_levels": [],
      "saved_jobs": [], "saved_searches": [], "sub_teams": [], "teams": [],
      "is_leadership": False, "is_remote_only": False, "sort_by_new": True,
      "results_per_page": None}

def gql(doc_id, variables, tag):
    resp = S.post("https://www.metacareers.com/graphql",
                  headers={"Content-Type": "application/x-www-form-urlencoded",
                           "x-fb-lsd": lsd, "Origin": "https://www.metacareers.com",
                           "Referer": "https://www.metacareers.com/jobsearch/"},
                  data={"lsd": lsd, "doc_id": doc_id,
                        "variables": json.dumps(variables)}, timeout=30)
    body = resp.text[:400].replace("\n", " ")
    print(f"[{tag}] HTTP {resp.status_code} :: {body[:300]}")
    return resp

# 1) known-good count query
gql("26446976041587120", {"search_input": SI}, "count")
# 2) results query variants
gql("27506805582236862", {"isLoggedIn": False, "viewasUserID": None, "search_input": SI}, "resultsA")
gql("27506805582236862", {"search_input": SI}, "resultsB")
gql("27129360303422352", {"isLoggedIn": False, "viewasUserID": None, "search_input": SI}, "resultsV2A")
gql("26210170368675892", {"search_input": SI}, "hideV2")
gql("27808175508766384", {"isLoggedIn": False, "viewasUserID": None, "search_input": SI}, "CPJobSearch")
