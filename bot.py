#!/usr/bin/env python3
"""
SDE2 Prep Telegram Bot v2 — Gemini Free + Render Free
Quotes: AI-generated fresh every time (no hardcoded list)
Features: Vent, Day Rating, Consequences, Competitor Mode, Badges
Removed: /dsa, /settarget, /weeklytip
"""

import json, logging, os, asyncio, random
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
# CONFIG
# ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
TIMEZONE       = "Asia/Kolkata"
DATA_FILE      = Path("sde2_data.json")
PORT           = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
TZ  = ZoneInfo(TIMEZONE)

genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")

# ─────────────────────────────────────────────────────────────────
# DATA LAYER
# ─────────────────────────────────────────────────────────────────
def load() -> dict:
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text())
        except: return {}
    return {}

def save(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def get_user(data: dict, uid: str) -> dict:
    if uid not in data:
        data[uid] = {
            "streak": 0, "last_log_date": None,
            "logs": [], "goals": [], "state": None,
            "total_sessions": 0, "ratings": [],
            "consequences": None, "badges": [],
        }
    return data[uid]

def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

def update_streak(user: dict) -> int:
    today = today_str()
    if user["last_log_date"] == today: return user["streak"]
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    user["streak"] = (user["streak"] + 1) if user["last_log_date"] == yesterday else 1
    user["last_log_date"] = today
    user["total_sessions"] = user.get("total_sessions", 0) + 1
    return user["streak"]

def days_since_log(user: dict) -> int:
    if not user.get("last_log_date"): return 999
    try:
        last = datetime.strptime(user["last_log_date"], "%Y-%m-%d").date()
        return (datetime.now(TZ).date() - last).days
    except: return 999

def was_active_today(user: dict) -> bool:
    return user.get("last_log_date") == today_str()

def get_week_logs(user: dict) -> list:
    week_ago = (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
    return [l for l in user.get("logs", []) if l.get("date", "") >= week_ago]

def check_badges(user: dict) -> str:
    earned   = user.setdefault("badges", [])
    new      = []
    streak   = user.get("streak", 0)
    sessions = user.get("total_sessions", 0)
    ratings  = user.get("ratings", [])
    avg      = sum(r["rating"] for r in ratings[-7:]) / len(ratings[-7:]) if ratings else 0

    checks = [
        ("🔥 Week Warrior",   streak >= 7,    "7-day streak!"),
        ("💪 Two Week Beast", streak >= 14,   "14-day streak!"),
        ("👑 Month Legend",   streak >= 30,   "30-day streak!"),
        ("⚡ First Session",  sessions >= 1,  "First session logged!"),
        ("🧠 10 Sessions",    sessions >= 10, "10 sessions done!"),
        ("🚀 25 Sessions",    sessions >= 25, "25 sessions done!"),
        ("🌟 High Performer", avg >= 8,       "7-day avg rating ≥ 8!"),
    ]
    for badge, condition, reason in checks:
        if condition and badge not in earned:
            earned.append(badge)
            new.append(f"{badge} — {reason}")

    return "🏅 *NEW BADGE UNLOCKED!*\n" + "\n".join(new) if new else ""

# ─────────────────────────────────────────────────────────────────
# AI LAYER — all prompts go through here
# ─────────────────────────────────────────────────────────────────
QUOTE_SOURCES = [
    "Elon Musk", "Jeff Bezos", "Steve Jobs", "Mark Zuckerberg", "Bill Gates",
    "David Goggins", "Marcus Aurelius", "Kobe Bryant", "Jocko Willink",
    "Naval Ravikant", "Charlie Munger", "Reid Hoffman", "Sam Altman",
    "Jensen Huang", "Arnold Schwarzenegger", "Rocky Balboa",
]

def ai(prompt: str, fallback: str = "Keep pushing. Every session counts. 💪") -> str:
    try:
        return gemini.generate_content(prompt).text.strip()
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return fallback

def ai_fresh_quote() -> str:
    """Generate a completely fresh motivational quote — real or inspired."""
    source = random.choice(QUOTE_SOURCES)
    return ai(
        f"Generate ONE powerful motivational quote from {source} (or strongly inspired by their philosophy) "
        f"about hard work, discipline, grinding, or building something great. "
        f"It must feel authentic to their voice. "
        f"Reply in EXACTLY this format:\n"
        f'_"<quote text>"_\n— *{source}*\n\n'
        f"No extra text. Just the formatted quote.",
        f'_"The only way out is through."_\n— *{source}*'
    )

def ai_morning_nudge(streak, goals, consequences) -> str:
    return ai(
        f"You are a brutally motivating SDE2 coach. This person works full-time and preps 10PM–2AM.\n"
        f"Streak: {streak} days. Tonight's goals: {', '.join(goals) if goals else 'not set — use /goal tonight'}.\n"
        f"What they lose if they fail: {consequences or 'not set yet'}.\n\n"
        f"Write a morning fire-up message. Include ONE sharp insight about the tech industry reality — "
        f"competition, what separates SDE1 from SDE2, or job market facts. Under 120 words. Punchy closer.",
        "☀️ Rise. The SDE2 grind doesn't care how tired you are. Tonight at 10PM — be ready."
    )

def ai_pre_session(streak, goals, consequences) -> str:
    return ai(
        f"SDE2 prep session starts in 4 hours. Streak: {streak}. "
        f"Goals: {', '.join(goals) if goals else 'not set'}. Stakes: {consequences or 'high'}.\n"
        f"Write Goggins-level pre-session hype. Competitor angle: someone with their same dream "
        f"is already practicing RIGHT NOW. Under 100 words. Fire.",
        "🔥 4 hours. Someone with your exact goal is already warming up. What's your excuse?"
    )

def ai_session_start(goals, consequences) -> str:
    return ai(
        f"10PM. SDE2 session starts NOW. Goals: {', '.join(goals) if goals else 'none — use /goal'}. "
        f"Stakes: {consequences or ''}.\n"
        f"Get them off their phone immediately. Every wasted minute = competitor gaining. Under 80 words.",
        "🚀 10PM. Phone down. Session open. The gap between you and SDE2 closes only right now."
    )

def ai_midnight_checkin(goals, streak) -> str:
    return ai(
        f"Midnight check-in. Streak: {streak}. Goals: {', '.join(goals) if goals else 'not set'}.\n"
        f"Acknowledge they're grinding at midnight. Goggins push: 2 more hours. Calm intensity. Under 80 words.",
        "⚡ Midnight. Still here. Most people gave up hours ago. 2 more hours. Don't stop now."
    )

def ai_end_session(streak, consequences) -> str:
    return ai(
        f"2AM end of session. Streak: {streak}. Stakes: {consequences or ''}.\n"
        f"Celebrate them staying up. Prompt /log and /rate. Warm but fired up. Under 80 words.",
        "🌙 2AM. You showed up. Now lock it in — /log then /rate. Every session compounds."
    )

def ai_analyze_log(log_text, streak, recent_logs, rating=None) -> str:
    recent = "\n".join(f"- {l['date']}: {l['text'][:100]}" for l in recent_logs[-5:])
    return ai(
        f"SDE2 end-of-session analysis. Streak: {streak}. Session rating: {rating or 'not given yet'}.\n"
        f"Tonight: {log_text}\nPast sessions:\n{recent}\n\n"
        f"Reply with exactly:\n"
        f"🎯 Pattern: (what you notice across sessions)\n"
        f"✅ Tonight's win:\n"
        f"⚡ Tomorrow's focus: (one specific action)\n"
        f"🔥 Closer: (sharp reality check or insight)\n"
        f"Under 200 words.",
        f"Logged. Streak: {streak}. Consistency is the only cheat code."
    )

def ai_vent(vent_text, streak, consequences) -> str:
    return ai(
        f"An SDE2 prep student is venting. Streak: {streak}. Stakes: {consequences or 'not set'}.\n"
        f"What they said: {vent_text}\n\n"
        f"Your response:\n"
        f"1. Genuinely acknowledge their struggle — 2-3 sentences, no toxic positivity\n"
        f"2. Goggins or founder-style reality reframe\n"
        f"3. ONE specific action they can take in the next 10 minutes\n"
        f"Under 180 words. Real talk.",
        "I hear you. It's genuinely hard. But you're still here — that means something. Open one problem. Just one."
    )

def ai_rate_analysis(ratings) -> str:
    if len(ratings) < 3:
        return "Keep rating your sessions — I'll spot patterns once you have more data. 📈"
    recent   = ratings[-7:]
    avg      = sum(r["rating"] for r in recent) / len(recent)
    trend    = ("improving 📈" if recent[-1]["rating"] > recent[0]["rating"]
                else "declining 📉" if recent[-1]["rating"] < recent[0]["rating"]
                else "steady ➡️")
    days_str = "\n".join(f"• {r['date']}: {r['rating']}/10" for r in recent)
    return ai(
        f"Session ratings for SDE2 student:\n{days_str}\nAvg: {avg:.1f}/10. Trend: {trend}\n\n"
        f"Sharp pattern insight — burnout? peaking? inconsistent? One concrete recommendation. Under 100 words.",
        f"7-day avg: {avg:.1f}/10. Trend: {trend}. Keep logging for deeper insights."
    )

def ai_weekly_report(logs, streak, ratings, consequences) -> str:
    log_summary = "\n".join(f"- {l['date']}: {l['text'][:100]}" for l in logs) or "No logs this week."
    avg = f"{sum(r['rating'] for r in ratings[-7:])/len(ratings[-7:]):.1f}/10" if ratings else "no ratings"
    return ai(
        f"Weekly SDE2 prep report card.\n"
        f"Streak: {streak}. Avg session rating: {avg}. Stakes: {consequences or 'none set'}.\n"
        f"Week logs:\n{log_summary}\n\n"
        f"Write:\n📊 WEEK REPORT CARD\n"
        f"Grade: (A–F + sharp one-liner reason)\n"
        f"💪 Biggest strength:\n"
        f"⚠️ Biggest gap:\n"
        f"🎯 #1 priority next week:\n"
        f"🔥 Reality check close\n"
        f"Under 220 words.",
        f"Week done. Streak: {streak}. Avg: {avg}. SDE2 is earned, not given."
    )

def ai_competitor_nudge(consequences) -> str:
    return ai(
        f"Competitor mode reminder. Stakes: {consequences or ''}.\n"
        f"Someone with this exact SDE2 goal is grinding RIGHT NOW. Make it visceral. "
        f"Reference the job market — limited SDE2 spots, hundreds of applicants. Under 80 words. No fluff.",
        "⚔️ RIGHT NOW. Someone with your exact goal just finished their 3rd problem. What are you doing?"
    )

def ai_roast(days_missed, last_log, consequences) -> str:
    return ai(
        f"Roast an SDE2 student who missed {days_missed} days. Last active: {last_log or 'never'}. "
        f"What they lose: {consequences or 'not set'}.\n"
        f"Savage but caring. Funny and real. Competition angle. Under 120 words.",
        f"😤 {days_missed} day(s) gone. Your competition didn't miss one. Get back NOW."
    )

# ─────────────────────────────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────────────────────────────
async def broadcast(app, build_fn, check_roast=False):
    data = load()
    for uid, user in data.items():
        try:
            if check_roast and days_since_log(user) >= 2:
                roast = ai_roast(days_since_log(user), user.get("last_log_date"), user.get("consequences"))
                quote = ai_fresh_quote()
                await app.bot.send_message(
                    chat_id=int(uid),
                    text=f"😤 *Roast Mode*\n\n{roast}\n\n{quote}",
                    parse_mode="Markdown"
                )
                continue
            msg = build_fn(user)
            if msg:
                await app.bot.send_message(chat_id=int(uid), text=msg, parse_mode="Markdown")
        except Exception as e:
            log.error(f"Broadcast error {uid}: {e}")

async def job_morning(app):
    def build(u):
        msg   = ai_morning_nudge(u.get("streak",0), u.get("goals",[]), u.get("consequences"))
        quote = ai_fresh_quote()
        return f"🌅 *Morning Fire-up*\n\n{msg}\n\n{quote}"
    await broadcast(app, build, check_roast=True)

async def job_midday(app):
    def build(u):
        cons  = u.get("consequences")
        extra = f"\n\n💀 *Remember:* _{cons}_" if cons and random.random() > 0.5 else ""
        quote = ai_fresh_quote()
        return f"☀️ *Midday Check-in*\n\nHalf the workday done. Tonight at 10PM the real work begins.{extra}\n\n{quote}"
    await broadcast(app, build)

async def job_pre_session(app):
    def build(u):
        msg = ai_pre_session(u.get("streak",0), u.get("goals",[]), u.get("consequences"))
        return f"🌆 *Pre-Session — 4 Hours to Go*\n\n{msg}"
    await broadcast(app, build)

async def job_competitor(app):
    def build(u):
        return f"⚔️ *Competitor Mode*\n\n{ai_competitor_nudge(u.get('consequences'))}"
    await broadcast(app, build)

async def job_consequences(app):
    def build(u):
        if not u.get("consequences"): return None
        return (f"💀 *Consequences Reminder*\n\n_{u['consequences']}_\n\n"
                f"Every session tonight is a vote against this happening. 🔥")
    await broadcast(app, build)

async def job_session_start(app):
    def build(u):
        msg = ai_session_start(u.get("goals",[]), u.get("consequences"))
        return f"🚀 *SESSION STARTS NOW — 10PM*\n\n{msg}\n\n📝 Log after → /log\n⭐ Rate it → /rate"
    await broadcast(app, build)

async def job_midnight(app):
    def build(u):
        msg   = ai_midnight_checkin(u.get("goals",[]), u.get("streak",0))
        quote = ai_fresh_quote()
        return f"⚡ *Midnight Check-in*\n\n{msg}\n\n{quote}"
    await broadcast(app, build)

async def job_end_session(app):
    def build(u):
        if not was_active_today(u): return None
        return f"🌙 *2AM — Session End*\n\n{ai_end_session(u.get('streak',0), u.get('consequences'))}"
    await broadcast(app, build)

async def job_weekend_morning(app):
    def build(u):
        cons  = u.get("consequences")
        extra = f"\n\n💀 *On the line:* _{cons}_" if cons else ""
        quote = ai_fresh_quote()
        return f"🌅 *Weekend — Full Day Grind Mode*\n\nNo office. No excuse. This is your edge over everyone resting today.{extra}\n\n{quote}"
    await broadcast(app, build, check_roast=True)

async def job_weekend_afternoon(app):
    def build(u):
        quote = ai_fresh_quote()
        return (f"☀️ *Weekend Afternoon*\n\nStreak: {u.get('streak',0)} 🔥\n"
                f"Still grinding? Log with /log. Haven't started? Open LeetCode NOW.\n\n{quote}")
    await broadcast(app, build)

async def job_weekly_report(app):
    def build(u):
        return ai_weekly_report(get_week_logs(u), u.get("streak",0), u.get("ratings",[]), u.get("consequences"))
    await broadcast(app, build)

# ─────────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); get_user(data, uid); save(data)
    name = update.effective_user.first_name or "champ"
    await update.message.reply_text(
        f"🚀 *SDE2 Prep Bot v2 — Activated, {name}!*\n\n"
        "*📅 Daily Schedule:*\n"
        "🌅 10AM — Morning fire-up + AI quote\n"
        "☀️ 1PM — Midday reality check\n"
        "🌆 6PM — Pre-session hype\n"
        "💀 7PM — Consequences reminder\n"
        "⚔️ 8PM — Competitor mode\n"
        "🚀 10PM — SESSION STARTS\n"
        "⚡ 12AM — Midnight check-in + AI quote\n"
        "🌙 2AM — End of session\n"
        "📊 Sunday 9AM — Weekly report card\n\n"
        "*⚡ Commands:*\n"
        "/goal — Set tonight's goals\n"
        "/log — Log session → AI analyzes\n"
        "/rate — Rate session 1-10\n"
        "/vent — Talk it out → AI re-motivates\n"
        "/consequences — Set what you lose if you fail\n"
        "/streak — Streak + badges\n"
        "/summary — Recent logs\n"
        "/report — Weekly report now\n"
        "/quote — Fresh AI-generated quote\n\n"
        "👉 *Start here:* /consequences",
        parse_mode="Markdown"
    )

async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    user["state"] = "waiting_goal"; save(data)
    await update.message.reply_text(
        "🎯 *Tonight's goals?*\n\nSeparate by commas:\n"
        "_E.g. Solve 3 DP problems, revise OS concepts, system design mock_",
        parse_mode="Markdown"
    )

async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    user["state"] = "waiting_log"; save(data)
    await update.message.reply_text(
        "📝 *What did you work on tonight?*\n\nBe specific — topics, problems, time, blockers, wins.",
        parse_mode="Markdown"
    )

async def cmd_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    user["state"] = "waiting_rate"; save(data)
    await update.message.reply_text(
        "⭐ *Rate tonight's session (1-10):*\n\n"
        "1-3 = Barely did anything\n"
        "4-6 = Decent but distracted\n"
        "7-8 = Solid 💪\n"
        "9-10 = Beast mode 🔥\n\n"
        "Just type the number.",
        parse_mode="Markdown"
    )

async def cmd_vent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    user["state"] = "waiting_vent"; save(data)
    await update.message.reply_text(
        "💬 *Vent Mode — I'm listening.*\n\nType it all out. No judgment.",
        parse_mode="Markdown"
    )

async def cmd_consequences(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    user["state"] = "waiting_consequences"; save(data)
    await update.message.reply_text(
        "💀 *Consequences Mode*\n\n"
        "What do you LOSE if you don't get the SDE2 offer? Be brutally honest.\n\n"
        "_E.g. Stay stuck at SDE1 salary 2 more years, disappoint my family, miss my financial goals_",
        parse_mode="Markdown"
    )

async def cmd_streak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    streak   = user.get("streak", 0)
    sessions = user.get("total_sessions", 0)
    badges   = user.get("badges", [])
    ratings  = user.get("ratings", [])
    avg      = f"{sum(r['rating'] for r in ratings[-7:])/len(ratings[-7:]):.1f}" if ratings else "N/A"
    fire     = "🔥" * min(streak, 7) if streak > 0 else "😴 No streak yet"
    badge_txt = "\n".join(badges) if badges else "None yet — keep going!"
    await update.message.reply_text(
        f"{fire}\n\n"
        f"*Streak:* {streak} day{'s' if streak != 1 else ''}\n"
        f"*Total sessions:* {sessions}\n"
        f"*7-day avg rating:* {avg}/10\n"
        f"*Last log:* {user.get('last_log_date', 'never')}\n\n"
        f"*🏅 Badges:*\n{badge_txt}",
        parse_mode="Markdown"
    )

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    logs    = user.get("logs", [])
    ratings = user.get("ratings", [])
    if not logs:
        await update.message.reply_text("No logs yet. Use /log after your session tonight! 📝")
        return
    msg = f"📋 *Progress Summary*\n\n🔥 Streak: {user.get('streak',0)} days\n\n*Last 5 Sessions:*\n"
    for l in reversed(logs[-5:]):
        r = next((r["rating"] for r in ratings if r["date"] == l["date"]), "—")
        msg += f"• *{l['date']}* ⭐{r} — {l['text'][:80]}{'...' if len(l['text'])>80 else ''}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load(); user = get_user(data, uid)
    await update.message.reply_text("📊 Generating your weekly report card...")
    report = ai_weekly_report(
        get_week_logs(user), user.get("streak", 0),
        user.get("ratings", []), user.get("consequences")
    )
    await update.message.reply_text(report, parse_mode="Markdown")

async def cmd_quote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💬 Generating fresh quote...")
    quote = ai_fresh_quote()
    await update.message.reply_text(f"💬 *Quote*\n\n{quote}", parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────
# MESSAGE ROUTER — handles state machine responses
# ─────────────────────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = str(update.effective_user.id)
    text  = update.message.text.strip()
    data  = load(); user = get_user(data, uid)
    state = user.get("state")

    if state == "waiting_goal":
        user["goals"] = [g.strip() for g in text.split(",")]
        user["state"] = None; save(data)
        goals_list = "\n".join(f"  • {g}" for g in user["goals"])
        await update.message.reply_text(
            f"✅ *Goals locked in:*\n{goals_list}\n\nSee you at 10PM. 🚀",
            parse_mode="Markdown"
        )

    elif state == "waiting_log":
        streak = update_streak(user)
        user.setdefault("logs", []).append({"date": today_str(), "text": text})
        user["state"] = None; save(data)
        await update.message.reply_text("⏳ Analyzing your session...")
        last_rating = next(
            (r["rating"] for r in reversed(user.get("ratings", [])) if r["date"] == today_str()),
            None
        )
        analysis  = ai_analyze_log(text, streak, user["logs"][:-1], last_rating)
        badge_msg = check_badges(user); save(data)
        streak_line = f"\n\n{'🔥'*min(streak,7)} *{streak}-day streak!*" if streak >= 2 else ""
        full_msg = f"{analysis}{streak_line}"
        if badge_msg: full_msg += f"\n\n{badge_msg}"
        await update.message.reply_text(full_msg, parse_mode="Markdown")
        await update.message.reply_text("⭐ Now rate your session → /rate")

    elif state == "waiting_rate":
        try:
            rating = int(text.strip())
            if not 1 <= rating <= 10: raise ValueError
            user.setdefault("ratings", []).append({"date": today_str(), "rating": rating})
            user["state"] = None; save(data)
            insight = ai_rate_analysis(user["ratings"])
            emoji = "🔥" if rating >= 8 else "💪" if rating >= 6 else "😤"
            await update.message.reply_text(
                f"{emoji} *Session rated: {rating}/10*\n\n{insight}",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("Just send a number from 1 to 10. 👆")

    elif state == "waiting_vent":
        user["state"] = None; save(data)
        await update.message.reply_text("💬 Hear you. Thinking...")
        response = ai_vent(text, user.get("streak", 0), user.get("consequences"))
        quote    = ai_fresh_quote()
        await update.message.reply_text(
            f"💬 *Real Talk*\n\n{response}\n\n{quote}",
            parse_mode="Markdown"
        )

    elif state == "waiting_consequences":
        user["consequences"] = text
        user["state"] = None; save(data)
        await update.message.reply_text(
            f"💀 *Consequences locked.*\n\n_{text}_\n\n"
            f"I'll remind you of this when you need it most. "
            f"Now make sure it never happens. 🔥\n\n"
            f"Next step: set tonight's goals → /goal",
            parse_mode="Markdown"
        )

    else:
        await update.message.reply_text(
            "Use a command:\n/goal /log /rate /vent /consequences /streak /summary /report /quote"
        )

# ─────────────────────────────────────────────────────────────────
# SCHEDULER SETUP
# ─────────────────────────────────────────────────────────────────
def setup_scheduler(app: Application) -> AsyncIOScheduler:
    s = AsyncIOScheduler(timezone=TIMEZONE)

    # Weekday schedule
    s.add_job(lambda: asyncio.create_task(job_morning(app)),      CronTrigger(hour=10, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))
    s.add_job(lambda: asyncio.create_task(job_midday(app)),       CronTrigger(hour=13, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))
    s.add_job(lambda: asyncio.create_task(job_pre_session(app)),  CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))
    s.add_job(lambda: asyncio.create_task(job_consequences(app)), CronTrigger(hour=19, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))
    s.add_job(lambda: asyncio.create_task(job_competitor(app)),   CronTrigger(hour=20, minute=0, day_of_week="mon-fri", timezone=TIMEZONE))

    # Every day
    s.add_job(lambda: asyncio.create_task(job_session_start(app)), CronTrigger(hour=22, minute=0, timezone=TIMEZONE))
    s.add_job(lambda: asyncio.create_task(job_midnight(app)),      CronTrigger(hour=0,  minute=0, timezone=TIMEZONE))
    s.add_job(lambda: asyncio.create_task(job_end_session(app)),   CronTrigger(hour=2,  minute=0, timezone=TIMEZONE))

    # Weekend
    s.add_job(lambda: asyncio.create_task(job_weekend_morning(app)),    CronTrigger(hour=9,  minute=0, day_of_week="sat,sun", timezone=TIMEZONE))
    s.add_job(lambda: asyncio.create_task(job_weekend_afternoon(app)),  CronTrigger(hour=14, minute=0, day_of_week="sat,sun", timezone=TIMEZONE))

    # Weekly
    s.add_job(lambda: asyncio.create_task(job_weekly_report(app)), CronTrigger(hour=9, minute=0, day_of_week="sun", timezone=TIMEZONE))

    return s

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
async def health(request):
    return web.Response(text="SDE2 Bot v2 ✅")

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    for cmd, fn in [
        ("start",        cmd_start),
        ("goal",         cmd_goal),
        ("log",          cmd_log),
        ("rate",         cmd_rate),
        ("vent",         cmd_vent),
        ("consequences", cmd_consequences),
        ("streak",       cmd_streak),
        ("summary",      cmd_summary),
        ("report",       cmd_report),
        ("quote",        cmd_quote),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = setup_scheduler(app)
    scheduler.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    server = web.Application()
    server.router.add_get("/", health)
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    log.info(f"✅ SDE2 Bot v2 running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
