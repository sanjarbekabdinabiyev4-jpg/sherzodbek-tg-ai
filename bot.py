import asyncio
import base64
import json
import logging
import os
import re
import time
from collections import defaultdict
from io import BytesIO
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAudio,
    MessageMediaDocument,
    MessageMediaPhoto,
    User,
)

# Loglarni sozlash
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("SherzodbekAI")

# .env faylini yuklash
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("API_ID", "31464913"))
API_HASH = os.getenv("API_HASH", "d3e89942a4cf0255fab365e2498d91e0").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
FALLBACK_MODEL = "gemini-flash-latest"
OWNER_NAME = os.getenv("OWNER_NAME", "Sherzodbek").strip()
STRING_SESSION = os.getenv("STRING_SESSION", "").strip()

STATE_FILE = BASE_DIR / "bot_state.json"
SESSION_FILE = str(BASE_DIR / "sherzodbek_session")

# So'kinish va haqorat so'zlar filtri (o'zbek va rus tillarida)
BAD_WORDS_PATTERN = re.compile(
    r"\b("
    r"ahmoq|tentak|jinni|iflos|haromi|itdan tarqagan|ko't|kot|sikay|sikey|qotoq|qo'toq|"
    r"jalab|fohisha|dalbayob|dolboyob|blat|blyat|suka|nahuy|naxuy|pizdes|pizdec|gandon|chmo|"
    r"yiban|yeban|mudak|pidar|pidoraz|sharmanda|тварь|сука|блять|блядь|нахуй|пиздец|гандон|долбоеб"
    r")\b",
    re.IGNORECASE,
)

SYSTEM_PROMPT = f"""Sen {OWNER_NAME}ning shaxsiy Telegram AI yordamchisisan.

QAT'IY QOIDALAR:
1. Muomala madaniyati: Har doim "Siz" deb, juda rasmiy, samimiy, muloyim va yuksak hurmat bilan gapir.
2. Taqiq: Hech qachon, hech qanday holatda so'kinma, qo'pol gapirma, bahslashma va odobsiz so'z ishlatma.
3. Suhbat boshlanishi: Agar foydalanuvchi bilan suhbat endi boshlanayotgan bo'lsa (birinchi xabar bo'lsa), salomlashib (agar u salom bergan bo'lsa "Va alaykum assalom", aks holda "Assalomu alaykum" deb, ikki marta salomlashmasdan), o'zingni quyidagi mazmunda tanishtir:
   "{OWNER_NAME} hozir band edilar, men {OWNER_NAME}ning AI yordamchisiman. Nima savolingiz bo'lsa menga aytavering, yoki muhim bo'lsa chaqirib beraman."
   Keyingi xabarlarda esa bu tanishtiruvni qayta-qayta takrorlama, suhbatni tabiiy va samimiy davom ettir.
4. Vazifang:
   - Suhbatdoshning savollariga aniq, foydali va tushunarli javob berish (agar umumiy savol, maslahat, rasm tahlili yoki ovozli xabar bo'lsa, yordam berish).
   - Agar suhbatdosh {OWNER_NAME}ning o'zida shaxsiy yoki muhim ishi borligini aytsa, nima masala ekanligini muloyimlik bilan so'rab bilib ol va {OWNER_NAME}ga darhol yetkazishingni ayt.
   - Agar suhbatdosh "{OWNER_NAME}ni chaqirib bering", "juda zarur ish bor", "tezda qaytib yozsinlar" desa yoki muhim xabar qoldirsa, javobingning eng oxiriga [CHAQIRUV] belgisini qo'sh (bu belgi orqali tizim {OWNER_NAME}ga xabar yuboradi).
5. Ovozli xabar va rasmlar:
   - Agar foydalanuvchi ovozli xabar (voice/audio) yuborgan bo'lsa, uni diqqat bilan eshitib, mazmuniga mos va muloyim javob qaytar.
   - Agar foydalanuvchi rasm yuborgan bo'lsa, rasmni ko'rib tushunib, rasmda nima borligi yoki foydalanuvchi so'ragan narsa bo'yicha aniq va hurmat bilan javob ber.
6. Haqoratga munosabat: Agar suhbatdosh qo'pol gapirsa yoki haqorat qilsa, aslo javoban qo'pollik qilma. Faqat: "Iltimos, hurmat doirasida va odob bilan muloqot qilsak. Savolingiz bo'lsa, marhamat." deb javob ber.
7. Til: Suhbatdosh qaysi tilda (asosan o'zbek tilida) yozsa, o'sha tilda chiroyli va imloviy xatolarsiz javob ber. Javoblaring juda cho'zilib ketmasin, aniq va samimiy bo'lsin.
"""


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"enabled": True, "muted_users": []}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"Holatni saqlashda xatolik: {e}")


state = load_state()
# Har bir foydalanuvchi uchun suhbat tarixi (oxirgi 16 ta xabar)
chat_histories: dict[int, list[dict]] = defaultdict(list)
# Qo'pol yozgan foydalanuvchilar ogohlantirilganini eslab qolish
rude_warnings: dict[int, int] = defaultdict(int)
# Akkaunt egasi o'zi yozishayotgan paytda AI xalaqit bermasligi uchun vaqtinchalik pauza
owner_active_until: dict[int, float] = {}
ai_generated_messages: set[int] = set()


async def call_gemini(
    user_id: int,
    text_prompt: str,
    media_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> str:
    """Google Gemini API ga matn, ovoz yoki rasm yuborib javob olish."""
    history = chat_histories[user_id]

    parts = []
    if media_bytes and mime_type:
        b64_data = base64.b64encode(media_bytes).decode("utf-8")
        parts.append({"inlineData": {"mimeType": mime_type, "data": b64_data}})

    if text_prompt:
        parts.append({"text": text_prompt})
    elif mime_type and mime_type.startswith("audio/"):
        parts.append(
            {
                "text": "Foydalanuvchi ovozli xabar yubordi. Uni eshitib, mazmuniga mos, rasmiy va hurmat bilan javob ber."
            }
        )
    elif mime_type and mime_type.startswith("image/"):
        parts.append(
            {
                "text": "Foydalanuvchi rasm yubordi. Rasmni ko'rib tahlil qil va muloyimlik bilan tushuntirib javob ber."
            }
        )

    # Tarix uchun faqat matnli xulosani saqlaymiz (xotira to'lib ketmasligi uchun)
    history_text_for_user = text_prompt or (
        "[Ovozli xabar yuborildi]"
        if (mime_type and mime_type.startswith("audio/"))
        else "[Rasm yuborildi]"
    )

    contents = list(history[-14:]) + [{"role": "user", "parts": parts}]

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 800,
        },
    }

    models_to_try = [PRIMARY_MODEL, FALLBACK_MODEL]
    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model_name in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            try:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    if resp.status == 200 and "candidates" in data:
                        candidate = data["candidates"][0]
                        reply_parts = candidate.get("content", {}).get("parts", [])
                        reply_text = "".join(
                            p.get("text", "") for p in reply_parts
                        ).strip()
                        if reply_text:
                            # Suhbat tarixiga qo'shamiz
                            history.append(
                                {
                                    "role": "user",
                                    "parts": [{"text": history_text_for_user}],
                                }
                            )
                            history.append(
                                {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "text": reply_text.replace(
                                                "[CHAQIRUV]", ""
                                            ).strip()
                                        }
                                    ],
                                }
                            )
                            if len(history) > 20:
                                chat_histories[user_id] = history[-20:]
                            return reply_text
                    else:
                        err_msg = data.get("error", {}).get("message", str(data))
                        logger.warning(
                            f"Model {model_name} xatolik qaytardi ({resp.status}): {err_msg}"
                        )
            except Exception as e:
                logger.error(f"Gemini ({model_name}) ulanish xatosi: {e}")

    return (
        f"Assalomu alaykum! {OWNER_NAME} hozir band edilar, men {OWNER_NAME}ning AI yordamchisiman. "
        "Xabaringizni qabul qildim, bo'shashlari bilan sizga javob beradilar."
    )


def extract_media_type(message) -> str | None:
    """Xabardagi media turini aniqlash: rasm yoki ovozli/audio xabar."""
    if isinstance(message.media, MessageMediaPhoto):
        return "image/jpeg"

    if isinstance(message.media, MessageMediaDocument) and message.media.document:
        doc = message.media.document
        mime = (doc.mime_type or "").lower()

        # Rasm fayli bo'lsa
        if mime.startswith("image/"):
            return mime

        # Ovozli xabar (voice) yoki audio fayl bo'lsa
        if mime.startswith("audio/"):
            return mime

        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeAudio):
                return mime or "audio/ogg"

    return None


session_obj = StringSession(STRING_SESSION) if STRING_SESSION else SESSION_FILE
client = TelegramClient(session_obj, API_ID, API_HASH)


@client.on(events.NewMessage(outgoing=True))
async def handle_owner_commands(event):
    """Akkaunt egasi (Sherzodbek aka) tomonidan yozilgan boshqaruv buyruqlari."""
    text = (event.raw_text or "").strip()
    if not text.startswith("/"):
        return

    cmd = text.lower().split()[0]

    # Saved Messages yoki istalgan joyda global komandalar
    if cmd == "/on":
        state["enabled"] = True
        save_state(state)
        await event.edit(
            f"✅ **{OWNER_NAME} AI yordamchisi YOQILDI!**\nEndi shaxsiy yozishmalarga avtomatik javob beraman."
        )
        logger.info("AI yordamchi yoqildi (/on)")

    elif cmd == "/off":
        state["enabled"] = False
        save_state(state)
        await event.edit(
            f"⏸ **{OWNER_NAME} AI yordamchisi O'CHIRILDI!**\nQayta yoqish uchun `/on` deb yozing."
        )
        logger.info("AI yordamchi o'chirildi (/off)")

    elif cmd == "/status":
        holat = "✅ Yoqilgan (ON)" if state.get("enabled", True) else "⏸ O'chirilgan (OFF)"
        muted_count = len(state.get("muted_users", []))
        await event.edit(
            f"🤖 **{OWNER_NAME} AI Yordamchi holati:**\n"
            f"• Holati: **{holat}**\n"
            f"• Istisno qilingan chatlar: **{muted_count} ta**\n\n"
            f"📌 **Buyruqlar:**\n"
            f"• `/on` — AI ni yoqish\n"
            f"• `/off` — AI ni o'chirish\n"
            f"• `/stop` — Shu yozishmada AI ni to'xtatish\n"
            f"• `/start` — Shu yozishmada AI ni qayta yoqish\n"
            f"• `/clear` — Suhbat xotirasini tozalash"
        )

    elif cmd == "/stop" and event.is_private:
        chat_id = event.chat_id
        me = await client.get_me()
        if chat_id != me.id:
            if chat_id not in state["muted_users"]:
                state["muted_users"].append(chat_id)
                save_state(state)
            await event.delete()
            await client.send_message(
                "me",
                f"🔇 Chat (`{chat_id}`) uchun AI javob berishi to'xtatildi. Qayta yoqish uchun o'sha chatda `/start` deb yozing.",
            )

    elif cmd == "/start" and event.is_private:
        chat_id = event.chat_id
        me = await client.get_me()
        if chat_id != me.id:
            if chat_id in state["muted_users"]:
                state["muted_users"].remove(chat_id)
                save_state(state)
            await event.delete()
            await client.send_message(
                "me", f"🔔 Chat (`{chat_id}`) uchun AI javob berishi qayta yoqildi!"
            )

    elif cmd == "/clear":
        chat_histories.clear()
        rude_warnings.clear()
        await event.edit("🧹 Barcha suhbat xotiralari tozalandi!")


@client.on(events.NewMessage(incoming=True))
async def handle_incoming_message(event):
    """Kiruvchi shaxsiy xabarlarga javob berish."""
    # 1. Faqat shaxsiy (private) yozishmalarga javob beramiz (guruh va kanallarga emas)
    if not event.is_private:
        return

    # 2. Global o'chirilgan bo'lsa javob bermaymiz
    if not state.get("enabled", True):
        return

    sender = await event.get_sender()
    if not isinstance(sender, User):
        return

    # 3. Botlarga, o'ziga yoki Telegram tizim xabarlariga (777000) javob bermaymiz
    if sender.bot or sender.is_self or sender.id == 777000:
        return

    # 4. Agar foydalanuvchi istisno (/stop) qilingan bo'lsa javob bermaymiz
    if sender.id in state.get("muted_users", []):
        return

    text = (event.raw_text or "").strip()
    mime_type = extract_media_type(event.message)

    # Agar matn ham, rasm/ovoz ham bo'lmasa (masalan stiker yoki lokatsiya bo'lsa)
    if not text and not mime_type:
        return

    sender_name = " ".join(
        filter(None, [sender.first_name, sender.last_name])
    ) or "Foydalanuvchi"
    sender_username = f"@{sender.username}" if sender.username else "username yo'q"

    logger.info(
        f"Yangi xabar [{sender_name} | {sender.id}]: text='{text[:50]}' media='{mime_type}'"
    )

    # 5. So'kinish yoki haqorat tekshiruvi
    if text and BAD_WORDS_PATTERN.search(text):
        rude_warnings[sender.id] += 1
        if rude_warnings[sender.id] == 1:
            await event.reply(
                f"Assalomu alaykum. Men {OWNER_NAME}ning AI yordamchisiman. "
                "Iltimos, hurmat doirasida va odob bilan muloqot qilsak. "
                f"Agar {OWNER_NAME}ga zarur gapingiz bo'lsa, marhamat, odob bilan yozib qoldiring."
            )
        else:
            logger.warning(
                f"Foydalanuvchi {sender_name} ({sender.id}) qayta haqorat yozgani uchun javob berilmadi."
            )
        return

    # 6. Media (ovoz yoki rasm) bo'lsa yuklab olamiz
    media_bytes = None
    if mime_type:
        try:
            # 15 MB dan katta fayllarni yuklamaymiz
            if (
                event.message.file
                and event.message.file.size
                and event.message.file.size > 15 * 1024 * 1024
            ):
                await event.reply(
                    "Assalomu alaykum! Yuborgan faylingiz hajmi juda katta ekan, iltimos qisqaroq ovozli xabar yoki matn yozib yuboring."
                )
                return

            buf = BytesIO()
            await client.download_media(event.message, file=buf)
            media_bytes = buf.getvalue()
        except Exception as e:
            logger.error(f"Mediani yuklab olishda xatolik: {e}")
            mime_type = None

    # 7. "Yozmoqda..." (typing) effektini ko'rsatib turamiz
    async with client.action(event.chat_id, "typing"):
        await asyncio.sleep(1.2)
        first_contact = len(chat_histories[sender.id]) == 0
        prompt_prefix = (
            f"[Suhbatdosh ismi: {sender_name}. Bu birinchi xabar, o'zingni tanishtirib javob ber]\n"
            if first_contact
            else f"[Suhbatdosh ismi: {sender_name}]\n"
        )
        full_prompt = f"{prompt_prefix}{text}" if text else prompt_prefix

        reply = await call_gemini(
            user_id=sender.id,
            text_prompt=full_prompt,
            media_bytes=media_bytes,
            mime_type=mime_type,
        )

    # 8. Agar muhim chaqiruv bo'lsa ([CHAQIRUV]), Sherzodbek akaning Saved Messages ("me") ga xabar yuboramiz
    is_urgent = "[CHAQIRUV]" in reply
    clean_reply = reply.replace("[CHAQIRUV]", "").strip()

    await event.reply(clean_reply)

    if is_urgent:
        try:
            alert_msg = (
                f"🔔 **MUHIM XABAR (AI Yordamchi):**\n\n"
                f"👤 **Kimdan:** {sender_name} ({sender_username})\n"
                f"💬 **Xabari:** {text or '[Ovozli xabar / Rasm]'}\n"
                f"🤖 **AI javobi:** {clean_reply}"
            )
            await client.send_message("me", alert_msg)
            logger.info(f"Saved Messages ga muhim chaqiruv yuborildi ({sender_name}).")
        except Exception as e:
            logger.error(f"Saved Messages ga xabar yuborishda xato: {e}")


async def health_handler(request):
    return web.Response(
        text=f"{OWNER_NAME} AI Yordamchi 24/7 ishlamoqda!", content_type="text/plain"
    )


async def keep_alive_pinger():
    """Cloud server (Render) uxlab qolmasligi uchun har 10 daqiqada o'zini ping qiladi."""
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not external_url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(external_url) as _:
                    pass
        except Exception:
            pass


async def start_web_server():
    """Cloud hosting (Render / Koyeb) uchun HTTP port ochish."""
    port_str = os.getenv("PORT", "")
    if not port_str:
        return
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port_str))
    await site.start()
    logger.info(f"24/7 Web Server {port_str}-portda ishga tushdi.")
    asyncio.create_task(keep_alive_pinger())


async def main():
    print("=" * 60)
    print(f"  {OWNER_NAME} uchun Telegram AI Yordamchi (Google Gemini)")
    print("=" * 60)
    print("Telegram akkauntga ulanmoqda...")

    await start_web_server()
    await client.start()
    me = await client.get_me()
    logger.info(
        f"Muvaffaqiyatli ulandi! Akkaunt: {me.first_name} (@{me.username or 'username_yoq'})"
    )
    print("\n✅ AI Yordamchi to'liq ishga tushdi!")
    print("📌 Boshqarish uchun Telegramda 'Saved Messages' (Избранное) ga kiring:")
    print("   /status — Holatni tekshirish")
    print("   /off    — AI ni vaqtincha o'chirish")
    print("   /on     — AI ni qayta yoqish")
    print("   /stop   — Biror odam bilan chatda yozilsa, o'sha odamga AI ni to'xtatadi\n")

    while True:
        try:
            if not client.is_connected():
                await client.connect()
            await client.run_until_disconnected()
        except Exception as e:
            logger.error(f"Aloqa uzildi, 5 soniyadan so'ng qayta ulanmoqda: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDastur to'xtatildi.")

