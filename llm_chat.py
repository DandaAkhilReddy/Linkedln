"""Azure OpenAI chat client — supports both classic deployments endpoints
and AI Foundry's OpenAI-v1 surface (model name in the body)."""
import requests


def chat(endpoint, key, deployment, messages, temperature=0.4, max_tokens=700):
    ep = endpoint.rstrip("/")
    if "services.ai.azure.com" in ep or "/openai/v1" in ep:
        base = ep.split("/openai")[0]
        url = f"{base}/openai/v1/chat/completions"
        payload = {"model": deployment, "messages": messages,
                   "max_completion_tokens": max_tokens}   # gpt-5.x style
    else:
        url = (f"{ep}/openai/deployments/{deployment}"
               "/chat/completions?api-version=2024-06-01")
        payload = {"messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens}
    r = requests.post(url, headers={"api-key": key,
                                    "Content-Type": "application/json"},
                      json=payload, timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
