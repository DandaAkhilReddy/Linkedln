"""
LinkedIn poster — official API, Default tier (legacy /v2/ugcPosts + /v2/assets).
Confirmed working with a w_member_social + openid + profile member token.

Env / config:
  LINKEDIN_ACCESS_TOKEN   member access token
  LINKEDIN_PERSON_URN      urn:li:person:XXXX (auto-derived from /userinfo if unset)
  LINKEDIN_VISIBILITY      PUBLIC (default) or CONNECTIONS
"""

import os
import time
import logging
import requests

log = logging.getLogger("linkedin")

API = "https://api.linkedin.com"


def _blob_secrets():
    """Read li_secrets.json from the linkedin-posts container (MFA-free path)."""
    try:
        from azure.storage.blob import BlobServiceClient
        conn = os.environ["AzureWebJobsStorage"]
        c = BlobServiceClient.from_connection_string(conn).get_container_client("linkedin-posts")
        import json as _j
        return _j.loads(c.download_blob("li_secrets.json").readall())
    except Exception:
        return {}


def _token():
    t = os.environ.get("LINKEDIN_ACCESS_TOKEN") or _blob_secrets().get("access_token")
    if not t:
        raise RuntimeError("LINKEDIN_ACCESS_TOKEN not set (env or blob)")
    return t


def person_urn(token=None):
    token = token or _token()
    cached = os.environ.get("LINKEDIN_PERSON_URN") or _blob_secrets().get("person_urn")
    if cached:
        return cached
    r = requests.get(f"{API}/v2/userinfo",
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return f"urn:li:person:{r.json()['sub']}"


def _register_image(token, urn):
    body = {"registerUploadRequest": {
        "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
        "owner": urn,
        "serviceRelationships": [{"relationshipType": "OWNER",
                                  "identifier": "urn:li:userGeneratedContent"}]}}
    r = requests.post(f"{API}/v2/assets?action=registerUpload",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      json=body, timeout=30)
    r.raise_for_status()
    v = r.json()["value"]
    upload_url = v["uploadMechanism"][
        "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
    return upload_url, v["asset"]


def _upload_image(token, upload_url, png_bytes):
    r = requests.post(upload_url,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "image/png"},
                      data=png_bytes, timeout=60)
    r.raise_for_status()


def _utf16_len(t):
    return len(t.encode("utf-16-le")) // 2


def _commentary(text, mention=None):
    """mention = (display_name, organization_urn) -> blue @tag on first occurrence."""
    body = {"text": text}
    if mention:
        name, org = mention
        idx = text.find(name)
        if idx >= 0 and org:
            body["attributes"] = [{
                "start": _utf16_len(text[:idx]),
                "length": _utf16_len(name),
                "value": {"com.linkedin.common.CompanyAttributedEntity":
                          {"company": org}},
            }]
    return body


def post_with_image(text, png_bytes, title="Hiring", token=None, urn=None, mention=None):
    """Publish a member post with one image. Returns the share URN."""
    token = token or _token()
    urn = urn or person_urn(token)
    upload_url, asset = _register_image(token, urn)
    _upload_image(token, upload_url, png_bytes)
    body = {
        "author": urn, "lifecycleState": "PUBLISHED",
        "specificContent": {"com.linkedin.ugc.ShareContent": {
            "shareCommentary": _commentary(text, mention),
            "shareMediaCategory": "IMAGE",
            "media": [{"status": "READY",
                       "description": {"text": title},
                       "media": asset,
                       "title": {"text": title}}]}},
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility":
                       os.environ.get("LINKEDIN_VISIBILITY", "PUBLIC")}}
    r = requests.post(f"{API}/v2/ugcPosts",
                      headers={"Authorization": f"Bearer {token}",
                               "X-Restli-Protocol-Version": "2.0.0",
                               "Content-Type": "application/json"},
                      json=body, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"ugcPost failed {r.status_code}: {r.text[:200]}")
    return r.headers.get("x-restli-id", r.json().get("id", "ok"))


def post_text(text, token=None, urn=None):
    token = token or _token()
    urn = urn or person_urn(token)
    body = {"author": urn, "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE"}},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility":
                           os.environ.get("LINKEDIN_VISIBILITY", "PUBLIC")}}
    r = requests.post(f"{API}/v2/ugcPosts",
                      headers={"Authorization": f"Bearer {token}",
                               "X-Restli-Protocol-Version": "2.0.0",
                               "Content-Type": "application/json"},
                      json=body, timeout=30)
    r.raise_for_status()
    return r.headers.get("x-restli-id", r.json().get("id", "ok"))


def token_valid(token=None):
    try:
        person_urn(token or _token())
        return True
    except Exception:
        return False
