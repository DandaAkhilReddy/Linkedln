"""
Azure Functions adapter (Python v2 model). Thin on purpose: every timer just
calls `jobs.run(<name>)` and every HTTP route calls a `jobs.*` function.
The schedules come from jobs.SCHEDULE so Azure and worker.py never drift.

HTTP (function-key protected):
  linkedin_run?action=generate&group=a..e|company=<name>&hours=N
  linkedin_run?action=drain
  linkedin_run?action=testcard&company=<name>
  growth_run?action=ask|poll
  run_now?company=<name>&hours=N        manual email batch (optional feature)
"""

import logging
import traceback

import azure.functions as func

import jobs
from companies import COMPANIES, GROUPS, GROUP_A, GROUP_B, GROUP_C, GROUP_D, GROUP_E  # noqa: F401 (re-exported for tests/tools)

app = func.FunctionApp()


def _ncron(cron5):
    """5-field cron (UTC) -> NCRONTAB 6-field with seconds."""
    return "0 " + cron5


def _text(body, status=200):
    return func.HttpResponse(body, status_code=status, mimetype="text/plain; charset=utf-8")


# ---- timers, generated from the shared schedule ----

@app.timer_trigger(schedule=_ncron("0 12 * * *"), arg_name="timer", run_on_startup=False)
def linkedin_generate_a(timer: func.TimerRequest) -> None:
    jobs.run("generate_a")


@app.timer_trigger(schedule=_ncron("20 12 * * *"), arg_name="timer", run_on_startup=False)
def linkedin_generate_b(timer: func.TimerRequest) -> None:
    jobs.run("generate_b")


@app.timer_trigger(schedule=_ncron("30 12 * * *"), arg_name="timer", run_on_startup=False)
def linkedin_generate_c(timer: func.TimerRequest) -> None:
    jobs.run("generate_c")


@app.timer_trigger(schedule=_ncron("40 12 * * *"), arg_name="timer", run_on_startup=False)
def linkedin_generate_d(timer: func.TimerRequest) -> None:
    jobs.run("generate_d")


@app.timer_trigger(schedule=_ncron("50 12 * * *"), arg_name="timer", run_on_startup=False)
def linkedin_generate_e(timer: func.TimerRequest) -> None:
    jobs.run("generate_e")


@app.timer_trigger(schedule=_ncron("5-55/10 * * * *"), arg_name="timer", run_on_startup=False)
def linkedin_drain(timer: func.TimerRequest) -> None:
    jobs.run("drain")


@app.timer_trigger(schedule=_ncron("0 13 * * *"), arg_name="timer", run_on_startup=False)
def growth_ask(timer: func.TimerRequest) -> None:
    jobs.run("growth_ask")


@app.timer_trigger(schedule=_ncron("*/20 * * * *"), arg_name="timer", run_on_startup=False)
def growth_poll(timer: func.TimerRequest) -> None:
    jobs.run("growth_poll")


# ---- manual HTTP routes ----

@app.route(route="linkedin_run", auth_level=func.AuthLevel.FUNCTION)
def linkedin_run(req: func.HttpRequest) -> func.HttpResponse:
    try:
        action = req.params.get("action", "drain")
        if action == "generate":
            target = req.params.get("company") or req.params.get("group") or None
            hours = int(req.params.get("hours", "24"))
            out = jobs.generate(target, hours)
        elif action == "testcard":
            out = [f"test card posted: {jobs.test_card(req.params.get('company', 'microsoft'))}"]
        else:
            out = jobs.drain()
        return _text("NOTES: " + "; ".join(out))
    except Exception:
        return _text("CRASH:\n" + traceback.format_exc(), 500)


@app.route(route="growth_run", auth_level=func.AuthLevel.FUNCTION)
def growth_run(req: func.HttpRequest) -> func.HttpResponse:
    try:
        out = jobs.growth_ask() if req.params.get("action") == "ask" else jobs.growth_poll()
        return _text("NOTES: " + "; ".join(out))
    except Exception:
        return _text("CRASH:\n" + traceback.format_exc(), 500)


@app.route(route="run_now", auth_level=func.AuthLevel.FUNCTION)
def run_now(req: func.HttpRequest) -> func.HttpResponse:
    try:
        hours = int(req.params.get("hours", "0")) or None
        company = req.params.get("company") or None
        out = jobs.email_batch([company] if company in COMPANIES else None, hours)
        return _text("NOTES: " + "; ".join(out))
    except Exception:
        return _text("CRASH:\n" + traceback.format_exc(), 500)


@app.route(route="test_email", auth_level=func.AuthLevel.FUNCTION)
def test_email(req: func.HttpRequest) -> func.HttpResponse:
    try:
        import emailer
        return _text("RESULT: " + emailer.send_email("Test email — delivery works! ✅",
                                                     "\U0001F9EA Jobs bot", "(delivery test)"))
    except Exception:
        return _text("EMAIL ERROR:\n" + traceback.format_exc(), 500)
