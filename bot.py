import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import MessageEntityType, ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =========================================================
# الإعدادات الأساسية
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN غير موجود في ملف .env")

if not GROUP_CHAT_ID:
    raise ValueError("GROUP_CHAT_ID غير موجود في ملف .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

WELCOME_MESSAGE = config.get(
    "welcome_message",
    "أهلاً وسهلاً بك 👋\n\nيرجى اختيار كليتك من القائمة التالية:"
)
QUESTION_RECEIVED_TEMPLATE = config.get(
    "question_received_template",
    "✅ تم استلام سؤالك بنجاح.\nرقم المرجع: #{reference}\n\nيرجى انتظار الرد."
)
ALREADY_PENDING_MESSAGE = config.get(
    "already_pending_message",
    "⚠️ لديك استفسار قيد المعالجة حالياً.\nإذا أردت البدء من جديد اضغط /start"
)
TEXT_ONLY_MESSAGE = config.get(
    "text_only_message",
    "⚠️ يرجى إرسال رسالة نصية فقط."
)
START_FIRST_MESSAGE = config.get(
    "start_first_message",
    "يرجى الضغط على /start أولاً ثم اختيار الكلية."
)
RESTART_BUTTON_TEXT = config.get(
    "restart_button_text",
    "🔄 إعادة البدء"
)

COLLEGES = config.get("colleges", {})

if not COLLEGES:
    raise ValueError("لا توجد كليات داخل config.json")

# =========================================================
# البوت
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

BOT_ID = None

# =========================================================
# الحالة
# =========================================================

class StudentStates(StatesGroup):
    waiting_question = State()

# =========================================================
# الذاكرة المؤقتة
# =========================================================

# قفل السؤال الحالي للمستخدم: user_id -> reference
current_open_question_ref = {}

# لمنع حالتي الرد المتزامن على نفس السؤال
processing_group_questions = set()

# =========================================================
# ثوابت مخفية
# =========================================================

HIDDEN_ID_PREFIX = "https://student.local/"

# =========================================================
# دوال مساعدة
# =========================================================

def build_hidden_id_link(student_id: int) -> str:
    # رابط مخفي تماماً داخل الرسالة
    return f'<a href="{HIDDEN_ID_PREFIX}{student_id}">&#8203;</a>'


def strip_invisible_chars(text: str) -> str:
    if not text:
        return ""
    invisible_chars = ["\u200b", "\u200c", "\u200d", "\ufeff"]
    for ch in invisible_chars:
        text = text.replace(ch, "")
    return text


def get_full_name(user) -> str:
    parts = []
    if getattr(user, "first_name", None):
        parts.append(user.first_name)
    if getattr(user, "last_name", None):
        parts.append(user.last_name)
    name = " ".join(parts).strip()
    return name or "مستخدم"


def get_username_display(user) -> str:
    username = getattr(user, "username", None)
    return f"@{username}" if username else "غير متوفر"


def build_colleges_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, data in COLLEGES.items():
        row.append(
            InlineKeyboardButton(
                text=data["name"],
                callback_data=f"college:{key}"
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_restart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=RESTART_BUTTON_TEXT, callback_data="restart_flow")]
        ]
    )


def build_college_message(college_data: dict) -> str:
    college_name = html.escape(college_data.get("name", "غير محدد"))
    contact_name = html.escape(college_data.get("contact_name", "غير متوفر"))
    contact_info = html.escape(college_data.get("contact_info", "غير متوفر"))

    return (
        f"أهلاً بك في <b>{college_name}</b>.\n\n"
        f"يمكنك إرسال سؤالك هنا مباشرة، أو التواصل مع الشخص المعني "
        f"بالتثقيف القانوني في كليتك:\n\n"
        f"الاسم: <b>{contact_name}</b>\n"
        f"التواصل: <b>{contact_info}</b>\n\n"
        f"يرجى إرسال سؤالك في رسالة نصية مستقلة.\n"
        f"ولإعادة البدء اضغط /start"
    )


def render_group_question_message(
    reference,
    college_name: str,
    student_name: str,
    username_display: str,
    question_text: str,
    status_text: str,
    student_id: int
) -> str:
    visible = (
        f"📬 <b>سؤال جديد</b>\n\n"
        f"المرجع: <b>#{html.escape(str(reference))}</b>\n"
        f"الكلية: {html.escape(college_name)}\n"
        f"الطالب: {html.escape(student_name)}\n"
        f"اليوزر: {html.escape(username_display)}\n\n"
        f"<b>السؤال:</b>\n{html.escape(question_text)}\n\n"
        f"الحالة: {html.escape(status_text)}"
    )
    return visible + build_hidden_id_link(student_id)


def extract_student_id_from_message(message: Message):
    entities = message.entities or []
    for entity in entities:
        if entity.type == MessageEntityType.TEXT_LINK and entity.url:
            if entity.url.startswith(HIDDEN_ID_PREFIX):
                raw_id = entity.url.replace(HIDDEN_ID_PREFIX, "").strip()
                if raw_id.isdigit():
                    return int(raw_id)
    return None


def extract_visible_text(message: Message) -> str:
    return strip_invisible_chars(message.text or "")


def parse_group_question_text(text: str):
    text = strip_invisible_chars(text or "")

    if "المرجع:" not in text or "السؤال:" not in text or "الحالة:" not in text:
        return None

    ref_match = re.search(r"المرجع:\s*#(\d+)", text)
    college_match = re.search(r"الكلية:\s*(.+)", text)
    student_match = re.search(r"الطالب:\s*(.+)", text)
    username_match = re.search(r"اليوزر:\s*(.+)", text)
    question_match = re.search(r"السؤال:\n([\s\S]*?)\n\nالحالة:", text)
    status_match = re.search(r"الحالة:\s*(.+)", text)

    if not all([ref_match, college_match, student_match, username_match, question_match, status_match]):
        return None

    return {
        "reference": int(ref_match.group(1)),
        "college_name": college_match.group(1).strip(),
        "student_name": student_match.group(1).strip(),
        "username_display": username_match.group(1).strip(),
        "question_text": question_match.group(1).strip(),
        "status_text": status_match.group(1).strip(),
    }


def is_answered_status(status_text: str) -> bool:
    return status_text.startswith("✅ تمت الإجابة")


def is_private(message: Message) -> bool:
    return message.chat.type == "private"


# =========================================================
# أوامر الطالب
# =========================================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if not is_private(message):
        return

    user_id = message.from_user.id
    current_open_question_ref.pop(user_id, None)
    await state.clear()

    await message.answer(
        WELCOME_MESSAGE,
        reply_markup=build_colleges_keyboard()
    )


@router.callback_query(F.data == "restart_flow")
async def restart_flow(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer()
        return

    user_id = callback.from_user.id
    current_open_question_ref.pop(user_id, None)
    await state.clear()

    try:
        await callback.message.edit_text(
            WELCOME_MESSAGE,
            reply_markup=build_colleges_keyboard()
        )
    except Exception:
        await callback.message.answer(
            WELCOME_MESSAGE,
            reply_markup=build_colleges_keyboard()
        )

    await callback.answer("تمت إعادة البدء")


@router.callback_query(F.data.startswith("college:"))
async def choose_college(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer()
        return

    college_key = callback.data.split(":", 1)[1]

    if college_key not in COLLEGES:
        await callback.answer("كلية غير موجودة", show_alert=True)
        return

    user_id = callback.from_user.id
    current_open_question_ref.pop(user_id, None)
    await state.clear()

    college_data = COLLEGES[college_key]

    await state.update_data(
        college_key=college_key,
        college_name=college_data["name"]
    )
    await state.set_state(StudentStates.waiting_question)

    text = build_college_message(college_data)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=build_restart_keyboard()
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=build_restart_keyboard()
        )

    await callback.answer()


# =========================================================
# استقبال سؤال الطالب
# =========================================================

@router.message(State(StudentStates.waiting_question), ~F.text)
async def reject_non_text_question(message: Message):
    if not is_private(message):
        return
    await message.answer(TEXT_ONLY_MESSAGE)


@router.message(StudentStates.waiting_question, F.text)
async def receive_question(message: Message, state: FSMContext):
    if not is_private(message):
        return

    user_id = message.from_user.id

    # إذا كان لديه سؤال مفتوح بالفعل، يتم منعه
    if user_id in current_open_question_ref:
        await message.answer(ALREADY_PENDING_MESSAGE)
        return

    data = await state.get_data()
    college_name = data.get("college_name")

    if not college_name:
        await state.clear()
        await message.answer(START_FIRST_MESSAGE)
        return

    question_text = (message.text or "").strip()

    if not question_text:
        await message.answer(TEXT_ONLY_MESSAGE)
        return

    student_name = get_full_name(message.from_user)
    username_display = get_username_display(message.from_user)

    try:
        temp_group_text = render_group_question_message(
            reference="...",
            college_name=college_name,
            student_name=student_name,
            username_display=username_display,
            question_text=question_text,
            status_text="🟡 مفتوح",
            student_id=user_id
        )

        sent_msg = await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=temp_group_text
        )

        reference = sent_msg.message_id

        final_group_text = render_group_question_message(
            reference=reference,
            college_name=college_name,
            student_name=student_name,
            username_display=username_display,
            question_text=question_text,
            status_text="🟡 مفتوح",
            student_id=user_id
        )

        await bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=sent_msg.message_id,
            text=final_group_text
        )

        # نقفل الإرسال ونثبت رقم المرجع
        current_open_question_ref[user_id] = reference
        
        # ⚠️ تغيير جوهري: لم نعد نمسح الحالة (state.clear) لتبقى الكلية مخزنة في الذاكرة
        
        await message.answer(
            QUESTION_RECEIVED_TEMPLATE.format(reference=reference)
        )

        logger.info(
            f"New question | ref={reference} | user_id={user_id} | college={college_name}"
        )

    except Exception as e:
        logger.exception("Error while sending question to group")
        await message.answer("❌ حدث خطأ أثناء إرسال السؤال، يرجى المحاولة لاحقاً.")


# =========================================================
# معالجة الرسائل الأخرى أو الـ Fallback
# =========================================================

@router.message(F.chat.type == "private")
async def private_fallback(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id in current_open_question_ref:
        await message.answer(ALREADY_PENDING_MESSAGE)
        return

    data = await state.get_data()
    # إذا كان الطالب يمتلك كلية مخزنة مسبقاً، نوجهه فوراً لكتابة سؤال
    if data.get("college_name"):
        await state.set_state(StudentStates.waiting_question)
        await receive_question(message, state)
        return

    await message.answer(START_FIRST_MESSAGE)


# =========================================================
# أوامر المشرفين في المجموعة
# =========================================================

@router.message(
    F.chat.id == GROUP_CHAT_ID,
    F.reply_to_message.is_not(None),
    Command("delete")
)
async def delete_answered_question(message: Message):
    original = message.reply_to_message

    if not original or not original.from_user:
        return

    if original.from_user.id != BOT_ID:
        return

    parsed = parse_group_question_text(extract_visible_text(original))
    if not parsed:
        await message.reply("⚠️ هذه ليست رسالة سؤال صالحة.")
        return

    if not is_answered_status(parsed["status_text"]):
        await message.reply("⚠️ لا يمكن حذف سؤال ما زال مفتوحاً.")
        return

    try:
        await bot.delete_message(chat_id=GROUP_CHAT_ID, message_id=original.message_id)
        await bot.delete_message(chat_id=GROUP_CHAT_ID, message_id=message.message_id)
    except Exception:
        await message.reply(
            "❌ لم أتمكن من حذف الرسالة.\n"
            "تأكد أن البوت أدمن ولديه صلاحية حذف الرسائل."
        )


# =========================================================
# رد المشرف على السؤال
# =========================================================

@router.message(
    F.chat.id == GROUP_CHAT_ID,
    F.reply_to_message.is_not(None)
)
async def handle_admin_reply(message: Message):
    if message.text and message.text.startswith("/"):
        return

    original = message.reply_to_message

    if not original or not original.from_user:
        return

    if original.from_user.id != BOT_ID:
        return

    if not message.text:
        await message.reply(TEXT_ONLY_MESSAGE)
        return

    parsed = parse_group_question_text(extract_visible_text(original))
    if not parsed:
        await message.reply("⚠️ هذه ليست رسالة سؤال صالحة.")
        return

    if is_answered_status(parsed["status_text"]):
        await message.reply("⚠️ تمت الإجابة على هذا السؤال مسبقاً.")
        return

    group_question_message_id = original.message_id

    if group_question_message_id in processing_group_questions:
        await message.reply("⚠️ تتم معالجة هذا السؤال حالياً.")
        return

    processing_group_questions.add(group_question_message_id)

    try:
        student_id = extract_student_id_from_message(original)
        if not student_id:
            await message.reply("❌ لم أتمكن من تحديد صاحب السؤال.")
            return

        reference = parsed["reference"]
        college_name = parsed["college_name"]
        student_name = parsed["student_name"]
        username_display = parsed["username_display"]
        question_text = parsed["question_text"]

        admin_name = get_full_name(message.from_user)
        answer_text = message.text.strip()

        if not answer_text:
            await message.reply(TEXT_ONLY_MESSAGE)
            return

        # الرسالة الجديدة للطالب (تخبره بإمكانية السؤال المباشر مجدداً)
        student_reply = (
            f"📩 <b>تم الرد على سؤالك</b>\n\n"
            f"المرجع: <b>#{reference}</b>\n"
            f"الكلية: {html.escape(college_name)}\n\n"
            f"<b>الرد:</b>\n{html.escape(answer_text)}\n\n"
            f"يمكنك إرسال سؤال جديد لنفس الكلية مباشرة،\n"
            f"أو الضغط على /start لتغيير الكلية."
        )

        try:
            await bot.send_message(chat_id=student_id, text=student_reply)
        except Exception:
            await message.reply(
                "❌ فشل إرسال الرد للطالب.\n"
                "قد يكون الطالب حظر البوت أو أوقف المحادثة."
            )
            return

        updated_group_text = render_group_question_message(
            reference=reference,
            college_name=college_name,
            student_name=student_name,
            username_display=username_display,
            question_text=question_text,
            status_text=f"✅ تمت الإجابة بواسطة {admin_name}",
            student_id=student_id
        )

        try:
            await bot.edit_message_text(
                chat_id=GROUP_CHAT_ID,
                message_id=original.message_id,
                text=updated_group_text
            )
        except Exception:
            logger.exception("Failed to edit original group question message")

        # فك القفل فقط إذا كان هذا هو السؤال الفعال الحالي للطالب
        if current_open_question_ref.get(student_id) == reference:
            current_open_question_ref.pop(student_id, None)

        await message.reply(f"✅ تم إرسال الرد وإغلاق السؤال #{reference}")

        logger.info(
            f"Answered | ref={reference} | student_id={student_id} | admin={admin_name}"
        )

    finally:
        processing_group_questions.discard(group_question_message_id)


# =========================================================
# التشغيل
# =========================================================

async def main():
    global BOT_ID

    me = await bot.get_me()
    BOT_ID = me.id

    logger.info("Bot is starting...")
    logger.info(f"Bot username: @{me.username}")
    logger.info(f"Group chat id: {GROUP_CHAT_ID}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())