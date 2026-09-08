"""
The company registry: which job pipelines exist, and how they are grouped
into generate waves (each wave runs in its own invocation so no single run
exceeds a serverless timeout). Favorites (Anthropic, OpenAI, Microsoft,
Apple) are in GROUP_A so their cards lead the day's queue.

Adding a company: write a pipeline (or one `board_pipelines` line), add an
entry here, drop its logo in logos/, add its LinkedIn org URN to
linkedin_autopost.ORG_URNS. Nothing else changes.
"""

import ms_jobs_pipeline
import apple_jobs_pipeline
import google_jobs_pipeline
import amazon_jobs_pipeline
import nvidia_jobs_pipeline
import meta_jobs_pipeline
import openai_jobs_pipeline
import anthropic_jobs_pipeline
import netflix_jobs_pipeline
import xai_jobs_pipeline
import board_pipelines

# "seed_first_run": boards whose timestamps don't reflect first posting
# (Meta has none; Greenhouse `updated_at` churns on edits) — first email run
# marks everything seen instead of flooding.
COMPANIES = {
    "microsoft": {"pipeline": ms_jobs_pipeline, "state": "state.json",
                  "prefix": "post", "subject": "\U0001F680 Microsoft jobs LinkedIn posts"},
    "apple": {"pipeline": apple_jobs_pipeline, "state": "apple_state.json",
              "prefix": "apple_post", "subject": "\U0001F34F Apple jobs LinkedIn posts"},
    "google": {"pipeline": google_jobs_pipeline, "state": "google_state.json",
               "prefix": "google_post", "subject": "\U0001F50D Google jobs LinkedIn posts"},
    "amazon": {"pipeline": amazon_jobs_pipeline, "state": "amazon_state.json",
               "prefix": "amazon_post", "subject": "\U0001F4E6 Amazon jobs LinkedIn posts"},
    "nvidia": {"pipeline": nvidia_jobs_pipeline, "state": "nvidia_state.json",
               "prefix": "nvidia_post", "subject": "\U0001F49A NVIDIA jobs LinkedIn posts"},
    "meta": {"pipeline": meta_jobs_pipeline, "state": "meta_state.json",
             "prefix": "meta_post", "subject": "Ⓜ️ Meta jobs LinkedIn posts",
             "seed_first_run": True},
    "openai": {"pipeline": openai_jobs_pipeline, "state": "openai_state.json",
               "prefix": "openai_post", "subject": "\U0001F916 OpenAI jobs LinkedIn posts"},
    "anthropic": {"pipeline": anthropic_jobs_pipeline, "state": "anthropic_state.json",
                  "prefix": "anthropic_post", "subject": "✳️ Anthropic jobs LinkedIn posts",
                  "seed_first_run": True},
    "netflix": {"pipeline": netflix_jobs_pipeline, "state": "netflix_state.json",
                "prefix": "netflix_post", "subject": "\U0001F3AC Netflix jobs LinkedIn posts"},
    "xai": {"pipeline": xai_jobs_pipeline, "state": "xai_state.json",
            "prefix": "xai_post", "subject": "✖️ xAI jobs LinkedIn posts",
            "seed_first_run": True},
    "databricks": {"pipeline": board_pipelines.databricks, "state": "databricks_state.json",
                   "prefix": "databricks_post", "subject": "\U0001F9F1 Databricks jobs",
                   "seed_first_run": True},
    "stripe": {"pipeline": board_pipelines.stripe, "state": "stripe_state.json",
               "prefix": "stripe_post", "subject": "\U0001F4B3 Stripe jobs", "seed_first_run": True},
    "scaleai": {"pipeline": board_pipelines.scaleai, "state": "scaleai_state.json",
                "prefix": "scaleai_post", "subject": "\U0001F4D0 Scale AI jobs", "seed_first_run": True},
    "ramp": {"pipeline": board_pipelines.ramp, "state": "ramp_state.json",
             "prefix": "ramp_post", "subject": "\U0001F7E8 Ramp jobs"},
    "cursor": {"pipeline": board_pipelines.cursor, "state": "cursor_state.json",
               "prefix": "cursor_post", "subject": "⌨️ Cursor jobs"},
    "amd": {"pipeline": board_pipelines.amd, "state": "amd_state.json",
            "prefix": "amd_post", "subject": "\U0001F534 AMD jobs"},
    "ibm": {"pipeline": board_pipelines.ibm, "state": "ibm_state.json",
            "prefix": "ibm_post", "subject": "\U0001F535 IBM jobs"},
}

# Generate waves (UTC minute offsets keep each under a 10-minute budget).
GROUP_A = ["anthropic", "openai", "microsoft", "apple"]      # favorites first
GROUP_B = ["google", "amazon", "nvidia"]
GROUP_C = ["meta", "netflix", "xai"]
GROUP_D = ["databricks", "stripe", "scaleai", "ramp", "cursor"]
GROUP_E = ["amd", "ibm"]

GROUPS = {"a": GROUP_A, "b": GROUP_B, "c": GROUP_C, "d": GROUP_D, "e": GROUP_E}
