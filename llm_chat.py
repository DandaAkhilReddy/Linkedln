"""Minimal Azure OpenAI chat-completions client (REST, no SDK)."""
import requests


def chat(endpoint, key, deployment, messages, temperature=0.4, max_tokens=700):
    url = (f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"
           "/chat/completions?api-version=2024-06-01")
    r = requests.post(url,
                      headers={"api-key": key, "Content-Type": "application/json"},
                      json={"messages": messages, "temperature": temperature,
                            "max_tokens": max_tokens},
                      timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
