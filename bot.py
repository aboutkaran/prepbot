#!/usr/bin/env python3
"""
SDE2 Prep Telegram Bot — Powered by Gemini (Free) + Hosted on Render (Free)
"""

import json
import logging
import os
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import google.generativeai as genai
from aiohttp import web

# ─────────────────────────────────────────────────────────────────
# CONFIG — set via environment variables on Render
# ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
TIMEZONE        = "Asia/Kolkata"
DATA_FILE       = Path("sde2_data.json")
PORT            = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
TZ = ZoneInfo(TIMEZONE)

genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")  # free tier model

# ─────────────────────────────────────────────────────────────────
# DATA LAYER
# ─────────────────────────────────────────────────────────────────
def load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except:
            return {}
    return {}

def save(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def get_user(data: dict, uid: str) -> dict:
    if uid not in data:
        data[uid] = {
            "streak": 0,
            "last_log_date": None,
            "logs": [],
            "dsa_log": [],
            "goals": [],
            "target_date": None,
            "target_company": None,
            "state": None,
            "total_dsa_solved": 0,
            "last_nudge_date": None,
        }
    return data[uid]

def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

def update_streak(user: dict) -> int:
    today = today_str()
    if user["last_log_date"] == today:
        return user["streak"]
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    user["streak"] = (user["streak"] + 1) if user["last_log_date"] == yesterday else 1
    user["last_log_date"] = today
    return user["streak"]

def days_to_target(user: dict) -> str:
    if not user.get("target_date"):
        return ""
    try:
        td = datetime.strptime(user["target_date"], "%Y-%m-%d").date()
        diff = (td - datetime.now(TZ).date()).days
        company = user.get("target_company", "your target company")
        if diff < 0:
            return f"⚠️ Target date passed! Reset with /settarget"
        elif diff == 0:
            return f"🚨 TODAY is your {company} interview day!"
        else:
            return f"🎯 {diff} days left to {company}"
    except:
        return ""

def was_active_today(user: dict) -> bool:
    return user.get("last_log_date") == today_str()

def days_since_log(user: dict) -> int:
    if not user.get("last_log_date"):
        return 999
    try:
        last = datetime.strptime(user["last_log_date"], "%Y-%m-%d").date()
        return (datetime.now(TZ).date() - last).days
    except:
        return 999

def get_week_logs(user: dict) -> list:
    week_ago = (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
    return [l for l in user.get("logs", []) if l.get("date", "") >= week_ago]

def dsa_today_count(user: dict) -> int:
    return len([d for d in user.get("dsa_log", []) if d.get("date") == today_str()])

# ─────────────────────────────────────────────────────────────────
# AI LAYER — Gemini Free
# ─────────────────────────────────────────────────────────────────
def ai(prompt: str) -> str:
    try:
        response = gemini.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return None  # fallback to static messages

def ai_morning_nudge(streak: int, goals: list, countdown: str) -> str:
    goal_txt = ", ".join(goals) if goals else "not set yet — set them with /goal"
    result = ai(
        f"You are a brutally motivating SDE2 prep coach. This person works full-time and preps 10PM-2AM. "
        f"Streak: {streak} days. Tonight's goals: {goal_txt}. {countdown}\n"
        f"Send a sharp morning message to keep them fired up through their workday. "
        f"Under 100 words. 1-2 emojis. End with one punchy line."
    )
    return result or random_morning_msg(streak)

def ai_pre_session_hype(streak: int, goals: list, countdown: str) -> str:
    goal_txt = ", ".join(goals) if goals else "not set"
    result = ai(
        f"You are a hype coach. SDE2 prep session starts in 4 hours (10PM). "
        f"Streak: {streak} days. Goals: {goal_txt}. {countdown}\n"
        f"Send an electrifying pre-session message. Competitive fire. Under 100 words."
    )
    return result or random_hype_msg()

def ai_session_start(goals: list, countdown: str) -> str:
    goal_txt = ", ".join(goals) if goals else "No goals set — use /goal NOW"
    result = ai(
        f"It's 10PM, SDE2 prep session starts NOW. Goals: {goal_txt}. {countdown}\n"
        f"Fire them up to put the phone down and start immediately. Under 80 words. No fluff."
    )
    return result or "🚀 Session starts NOW. Phone down. Goals open. Let's go."

def ai_midnight_checkin(goals: list, dsa_count: int) -> str:
    result = ai(
        f"It's midnight, deep in the SDE2 grind. Goals: {', '.join(goals) if goals else 'not set'}. "
        f"DSA solved today: {dsa_count}.\n"
        f"Send a calm but intense check-in. Acknowledge the late night hustle. Push for 2 more hours. Under 80 words."
    )
    return result or "⚡ Midnight. Still going? Good. 2 more hours. Don't stop now."

def ai_analyze_log(log_text: str, streak: int, recent_logs: list, dsa_count: int) -> str:
    recent = "\n".join(f"- {l['date']}: {l['text'][:100]}" for l in recent_logs[-5:])
    result = ai(
        f"You are an SDE2 coach. End-of-session feedback.\n"
        f"Streak: {streak} days. DSA today: {dsa_count}.\n"
        f"Tonight's log: {log_text}\nRecent history:\n{recent}\n\n"
        f"Reply in this format:\n"
        f"🎯 Pattern: (what you notice)\n"
        f"✅ Tonight's win: (acknowledge)\n"
        f"⚡ Tomorrow's focus: (one specific SDE2 action)\n"
        f"🔥 Closer: (motivational punch)\n"
        f"Under 200 words."
    )
    return result or f"✅ Logged! Streak: {streak} days. Keep going — consistency beats intensity."

def ai_roast(days_missed: int, last_log: str) -> str:
    result = ai(
        f"Roast this SDE2 prep student who missed {days_missed} day(s). Last active: {last_log or 'never'}.\n"
        f"Be savage but caring. Reference the competitive SDE2 market. Funny and brutal. Under 120 words."
    )
    return result or f"😤 {days_missed} day(s) missed. Your future self is NOT happy. Get back NOW."

def ai_weekly_report(logs: list, dsa_week: int, streak: int, countdown: str) -> str:
    log_summary = "\n".join(f"- {l['date']}: {l['text'][:100]}" for l in logs) or "No logs this week."
    result = ai(
        f"SDE2 weekly report card.\n"
        f"Streak: {streak}. DSA this week: {dsa_week}. {countdown}\n"
        f"Logs:\n{log_summary}\n\n"
        f"Format:\n"
        f"📊 WEEK REPORT CARD\n"
        f"Grade: (A/B/C/D/F + reason)\n"
        f"💪 Strength:\n"
        f"⚠️ Gap:\n"
        f"🎯 Next week priority:\n"
        f"🔥 Close\nUnder 200 words."
    )
    return result or f"📊 Week done. Streak: {streak}. DSA: {dsa_week}. Keep pushing!"

def ai_daily_tip() -> str:
    result = ai(
        f"Give ONE sharp, actionable SDE2 interview tip. "
        f"Rotate between: DSA patterns, system design, behavioral, coding best practices. "
        f"Immediately useful. Under 100 words. Include a tiny example."
    )
    return result or "💡 Tip: Always clarify constraints before coding. Ask: input size? edge cases? expected complexity?"

# ─────────────────────────────────────────────────────────────────
# STATIC FALLBACK MESSAGES (when AI is rate limited)
# ─────────────────────────────────────────────────────────────────
import random

MORNING_MSGS = [
    "☀️ Another day, another chance to get closer to that SDE2 offer. While others sleep on their prep, you're already thinking about tonight's session. See you at 10PM. 💪",
    "🌅 Good morning. Your future SDE2 self is built in those late night 10PM-2AM sessions. Stay sharp at work today — the real work starts tonight.",
    "⚡ Day job by day, SDE2 grind by night. That's the play. Don't lose focus. Tonight at 10PM — no excuses.",
]

HYPE_MSGS = [
    "🔥 4 hours until the grind. Your competition is already warming up. Are you? Get your goals ready with /goal.",
    "⚡ The session is close. Every problem you solve tonight is an investment in your future salary. Let's go.",
    "🎯 Pre-session mode: ON. Review your goals, clear your desk, get water. 10PM is non-negotiable.",
]

def random_morning_msg(streak): return random.choice(MORNING_MSGS)
def random_hype_msg(): return random.choice(HYPE_MSGS)

# ─────────────────────────────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────────────────────────────
async def broadcast(app: Application, message_fn, check_roast=False):
    data = load()
    for uid, user in data.items():
        try:
            if check_roast:
                missed = days_since_log(user)
                if missed >= 2:
                    msg = ai_roast(missed, user.get("last_log_date"))
                    await app.bot.send_message(chat_id=int(uid), text=f"😤 *Roast Mode Activated*\n\n{msg}", parse_mode="Markdown")
                    continue
            text = message_fn(user)
            if text:
                await app.bot.send_message(chat_id=int(uid), text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Broadcast error for {uid}: {e}")

async def job_morning(app):
    """10:00 AM weekdays"""
    async def build(user):
        msg = ai_morning_nudge(user.get("streak", 0), user.get("goals", []), days_to_target(user))
        return f"🌅 *Morning Fire-up*\n\n{msg}"
    await broadcast(app, build, check_roast=True)

async def job_midday(app):
    """1:00 PM weekdays"""
    tips = [
        "☀️ *Midday Check-in* — Workday half done. Keep your energy up. Tonight's session is 9 hours away. Stay sharp. 💡 Use /weeklytip for today's SDE2 concept.",
        "☀️ *1PM Reminder* — You're building two careers at once. That's hard. That's also exactly why you'll get the SDE2 offer. Keep going. 🎯",
        "☀️ *Afternoon nudge* — Review your goals for tonight with /goal. Preparation before the session = better session. ⚡",
    ]
    async def build(user):
        return random.choice(tips)
    await broadcast(app, build)

async def job_pre_session(app):
    """6:00 PM weekdays"""
    async def build(user):
        msg = ai_pre_session_hype(user.get("streak", 0), user.get("goals", []), days_to_target(user))
        return f"🌆 *Pre-Session Hype — 4 Hours to Go!*\n\n{msg}"
    await broadcast(app, build)

async def job_session_start(app):
    """10:00 PM daily"""
    async def build(user):
        msg = ai_session_start(user.get("goals", []), days_to_target(user))
        countdown = days_to_target(user)
        extra = f"\n\n{countdown}" if countdown else ""
        return f"🚀 *SESSION STARTS NOW*\n\n{msg}{extra}\n\n📝 Log problems with /dsa\n🎯 Set goals with /goal"
    await broadcast(app, build)

async def job_midnight(app):
    """12:00 AM daily"""
    async def build(user):
        msg = ai_midnight_checkin(user.get("goals", []), dsa_today_count(user))
        dsa = dsa_today_count(user)
        return f"⚡ *Midnight Check-in*\n\n{msg}\n\n✅ DSA solved tonight: {dsa}"
    await broadcast(app, build)

async def job_end_session(app):
    """2:00 AM daily — reflection time"""
    async def build(user):
        if not was_active_today(user):
            return None  # will be handled by roast in morning
        return (
            f"🌙 *End of Session — Time to Reflect*\n\n"
            f"Great work staying up. Now log your session so I can analyze your progress.\n\n"
            f"📝 Use /log to record what you did tonight.\n"
            f"🔥 Streak: {user.get('streak', 0)} days"
        )
    await broadcast(app, build)

async def job_weekly_report(app):
    """Sunday 9:00 AM"""
    async def build(user):
        week_logs = get_week_logs(user)
        dsa_week = len([d for d in user.get("dsa_log", [])
                        if d.get("date", "") >= (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")])
        report = ai_weekly_report(week_logs, dsa_week, user.get("streak", 0), days_to_target(user))
        return f"{report}"
    await broadcast(app, build)

async def job_weekend_morning(app):
    """9:00 AM weekends"""
    msgs = [
        "🌅 *Weekend Grind Day!*\n\nNo office today — full day for SDE2 prep. Set your goals with /goal and let's make this count. 💪",
        "🌅 *Weekend is HERE!*\n\nThis is your edge. While others rest, you prep. Full day available — make it count. Start with /goal 🎯",
    ]
    async def build(user):
        return random.choice(msgs)
    await broadcast(app, build)

async def job_weekend_afternoon(app):
    """2:00 PM weekends"""
    async def build(user):
        dsa = dsa_today_count(user)
        return (
            f"☀️ *Weekend Afternoon Check-in*\n\n"
            f"DSA solved today: {dsa} ✅\n"
            f"Still going? Log problems with /dsa\n"
            f"Haven't started? There's still time. Open LeetCode NOW. 🔥"
        )
    await broadcast(app, build)

# ─────────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    get_user(data, uid)
    save(data)
    name = update.effective_user.first_name or "champ"
    await update.message.reply_text(
        f"🚀 *SDE2 Prep Bot — Activated, {name}!*\n\n"
        "I'll push you, track you, roast you, and help you get that SDE2 offer.\n\n"
        "*📅 Your Daily Schedule:*\n"
        "🌅 10:00 AM — Morning fire-up (weekdays)\n"
        "☀️ 1:00 PM — Midday reminder (weekdays)\n"
        "🌆 6:00 PM — Pre-session hype (weekdays)\n"
        "🚀 10:00 PM — Session START 🔥\n"
        "⚡ 12:00 AM — Midnight check-in\n"
        "🌙 2:00 AM — End of session reflection\n"
        "🌅 9:00 AM — Weekend full-day nudges\n"
        "📊 Sunday 9AM — Weekly report card\n\n"
        "*⚡ Commands:*\n"
        "/goal — Set tonight's goals\n"
        "/dsa — Log a DSA problem\n"
        "/log — Log your session\n"
        "/streak — Check your streak 🔥\n"
        "/settarget — Set target company + date\n"
        "/summary — See recent logs\n"
        "/weeklytip — Get today's SDE2 tip\n"
        "/report — Get your weekly report\n\n"
        "👉 Start by setting your target: /settarget",
        parse_mode="Markdown"
    )

async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    user = get_user(data, uid)
    user["state"] = "waiting_goal"
    save(data)
    await update.message.reply_text(
        "🎯 *What are your goals for tonight's session?*\n\n"
        "Type them separated by commas:\n"
        "_Example: Solve 3 DP problems, revise LLD basics, system design mock_",
        parse_mode="Markdown"
    )

async def cmd_dsa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    user = get_user(data, uid)
    user["state"] = "waiting_dsa"
    save(data)
    await update.message.reply_text(
        "✅ *Log a DSA problem:*\n\n"
        "Format: `Problem - Difficulty - Status`\n"
        "_Examples:_\n"
        "• `Two Sum - Easy - Solved`\n"
        "• `LRU Cache - Medium - Need revision`\n"
        "• `Word Ladder - Hard - Attempted`",
        parse_mode="Markdown"
    )

async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    user = get_user(data, uid)
    user["state"] = "waiting_log"
    save(data)
    await update.message.reply_text(
        "📝 *What did you work on tonight?*\n\n"
        "Be specific — problems solved, topics covered, time spent, blockers, how you felt.\n"
        "_The more detail, the better my analysis._",
        parse_mode="Markdown"
    )

async def cmd_streak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    user = get_user(data, uid)
    streak = user.get("streak", 0)
    total_dsa = user.get("total_dsa_solved", 0)
    countdown = days_to_target(user)
    fire = "🔥" * min(streak, 7) if streak > 0 else "😴 No streak yet"
    goals = user.get("goals", [])
    msg = (
        f"{fire}\n\n"
        f"*Streak:* {streak} day{'s' if streak != 1 else ''}\n"
        f"*Total DSA solved:* {total_dsa} 🧠\n"
        f"*Last log:* {user.get('last_log_date', 'never')}\n"
        f"*Tonight\\'s goals:* {', '.join(goals) if goals else 'not set — /goal'}"
    )
    if countdown:
        msg += f"\n\n{countdown}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_settarget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    user = get_user(data, uid)
    user["state"] = "waiting_countdown"
    save(data)
    await update.message.reply_text(
        "🎯 *Set your interview target:*\n\n"
        "Format: `Company YYYY-MM-DD`\n"
        "_Example: `Google 2025-08-15`_",
        parse_mode="Markdown"
    )

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    user = get_user(data, uid)
    logs = user.get("logs", [])
    dsa  = user.get("dsa_log", [])
    if not logs and not dsa:
        await update.message.reply_text("No logs yet! Use /log or /dsa to start tracking. 📝")
        return
    msg = f"📋 *Your Progress Summary*\n\n"
    msg += f"🔥 Streak: {user.get('streak', 0)} days\n"
    msg += f"🧠 Total DSA: {user.get('total_dsa_solved', 0)} problems\n\n"
    if logs:
        msg += "*Last 5 Session Logs:*\n"
        for l in reversed(logs[-5:]):
            msg += f"• *{l['date']}* — {l['text'][:80]}{'...' if len(l['text']) > 80 else ''}\n"
    if dsa:
        msg += f"\n*Last 5 DSA Problems:*\n"
        for d in reversed(dsa[-5:]):
            msg += f"• {d['problem']} _{d['date']}_\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_weeklytip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💡 Fetching today's SDE2 tip...")
    tip = ai_daily_tip()
    await update.message.reply_text(f"💡 *SDE2 Tip of the Day:*\n\n{tip}", parse_mode="Markdown")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    user = get_user(data, uid)
    await update.message.reply_text("📊 Generating your weekly report card...")
    week_logs = get_week_logs(user)
    dsa_week = len([d for d in user.get("dsa_log", [])
                    if d.get("date", "") >= (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")])
    report = ai_weekly_report(week_logs, dsa_week, user.get("streak", 0), days_to_target(user))
    await update.message.reply_text(report, parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────
# MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    text = update.message.text.strip()
    data = load()
    user = get_user(data, uid)
    state = user.get("state")

    if state == "waiting_goal":
        user["goals"] = [g.strip() for g in text.split(",")]
        user["state"] = None
        save(data)
        goals_list = "\n".join(f"  • {g}" for g in user["goals"])
        await update.message.reply_text(
            f"✅ *Goals locked for tonight:*\n{goals_list}\n\n"
            f"Now get through your day. Session starts at 10PM. 🚀",
            parse_mode="Markdown"
        )

    elif state == "waiting_dsa":
        entry = {"problem": text, "date": today_str()}
        user.setdefault("dsa_log", []).append(entry)
        user["total_dsa_solved"] = user.get("total_dsa_solved", 0) + 1
        user["state"] = None
        total = user["total_dsa_solved"]
        save(data)
        milestone = ""
        if total in [10, 25, 50, 100, 200]:
            milestone = f"\n\n🎉 *MILESTONE: {total} problems solved!* You're building real momentum!"
        await update.message.reply_text(
            f"✅ *Logged:* {text}\n"
            f"📊 Total solved: *{total}* problems{milestone}",
            parse_mode="Markdown"
        )

    elif state == "waiting_log":
        streak = update_streak(user)
        user.setdefault("logs", []).append({"date": today_str(), "text": text})
        user["state"] = None
        save(data)
        await update.message.reply_text("⏳ Analyzing your session...")
        analysis = ai_analyze_log(text, streak, user["logs"][:-1], dsa_today_count(user))
        streak_line = f"\n\n{'🔥' * min(streak, 7)} *{streak}-day streak!*" if streak >= 2 else ""
        await update.message.reply_text(
            f"{analysis}{streak_line}",
            parse_mode="Markdown"
        )

    elif state == "waiting_countdown":
        parts = text.strip().split()
        if len(parts) >= 2:
            user["target_company"] = " ".join(parts[:-1])
            user["target_date"] = parts[-1]
            user["state"] = None
            save(data)
            countdown = days_to_target(user)
            await update.message.reply_text(
                f"🎯 *Target set!*\n\n{countdown}\n\nEvery session from now is a step toward this. Let's get it. 💪",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "Format: `Company YYYY-MM-DD`\nExample: `Google 2025-08-15`",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "Use a command to interact with me:\n"
            "/goal /dsa /log /streak /settarget /summary /weeklytip /report",
        )

# ─────────────────────────────────────────────────────────────────
# SCHEDULER SETUP
# ─────────────────────────────────────────────────────────────────
def setup_scheduler(app: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Weekdays (Mon-Fri)
    scheduler.add_job(lambda: asyncio.create_task(job_morning(app)),       CronTrigger(hour=10, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))
    scheduler.add_job(lambda: asyncio.create_task(job_midday(app)),        CronTrigger(hour=13, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))
    scheduler.add_job(lambda: asyncio.create_task(job_pre_session(app)),   CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))

    # Daily (every day)
    scheduler.add_job(lambda: asyncio.create_task(job_session_start(app)), CronTrigger(hour=22, minute=0, timezone=TIMEZONE))
    scheduler.add_job(lambda: asyncio.create_task(job_midnight(app)),      CronTrigger(hour=0,  minute=0, timezone=TIMEZONE))
    scheduler.add_job(lambda: asyncio.create_task(job_end_session(app)),   CronTrigger(hour=2,  minute=0, timezone=TIMEZONE))

    # Weekends (Sat-Sun)
    scheduler.add_job(lambda: asyncio.create_task(job_weekend_morning(app)),   CronTrigger(hour=9,  minute=0, day_of_week="sat,sun", timezone=TIMEZONE))
    scheduler.add_job(lambda: asyncio.create_task(job_weekend_afternoon(app)), CronTrigger(hour=14, minute=0, day_of_week="sat,sun", timezone=TIMEZONE))

    # Weekly report — Sunday 9AM
    scheduler.add_job(lambda: asyncio.create_task(job_weekly_report(app)), CronTrigger(hour=9, minute=0, day_of_week="sun", timezone=TIMEZONE))

    return scheduler

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
async def health(request):
    return web.Response(text="SDE2 Bot running ✅")

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("goal",      cmd_goal))
    app.add_handler(CommandHandler("dsa",       cmd_dsa))
    app.add_handler(CommandHandler("log",       cmd_log))
    app.add_handler(CommandHandler("streak",    cmd_streak))
    app.add_handler(CommandHandler("settarget", cmd_settarget))
    app.add_handler(CommandHandler("summary",   cmd_summary))
    app.add_handler(CommandHandler("weeklytip", cmd_weeklytip))
    app.add_handler(CommandHandler("report",    cmd_report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = setup_scheduler(app)
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep-alive web server for Render
    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info(f"✅ SDE2 Bot running on port {PORT}")

    await asyncio.Event().wait()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
