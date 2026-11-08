import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from pypdf import PdfReader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная BOT_TOKEN.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_allowed(message: Message) -> bool:
    # Если список ID пуст, бот доступен всем, кто найдёт его.
    return not ALLOWED_USER_IDS or (
        message.from_user is not None and message.from_user.id in ALLOWED_USER_IDS
    )


def tidy(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n:;,")


def find(pattern: str, text: str, flags=re.IGNORECASE):
    match = re.search(pattern, text, flags)
    return tidy(match.group(1)) if match else None


def extract_fields(text: str) -> dict:
    text = text.replace("\u00a0", " ")
    lines = [tidy(line) for line in text.splitlines() if tidy(line)]

    # В типовом счёте продавец расположен после заголовка «Продавец».
    seller = find(
        r"Продавец\s+([А-ЯЁA-Z][А-ЯЁA-Z\s-]{5,}?)\s+Покупатель",
        text,
    )
    regime = find(r"Режим\s+НО\s*:\s*([^\r\n]+)", text)

    # ИНН продавца берём из строки после «Режим НО», чтобы не взять ИНН покупателя.
    inn = None
    for i, line in enumerate(lines):
        if re.match(r"Режим\s+НО\s*:", line, re.IGNORECASE):
            for candidate in lines[i + 1:i + 4]:
                m = re.fullmatch(r"ИНН\s+(\d{10,12})", candidate, re.IGNORECASE)
                if m:
                    inn = m.group(1)
                    break
            break

    phone = find(
        r"(\+7\s*\(?\d{3}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2})",
        text,
    )

    bank = None
    if inn:
        for i, line in enumerate(lines):
            if re.fullmatch(r"ИНН\s+" + re.escape(inn), line, re.IGNORECASE):
                if i + 1 < len(lines) and re.fullmatch(
                    r"[А-ЯЁA-Z][А-ЯЁA-Z -]{1,}", lines[i + 1], re.IGNORECASE
                ):
                    bank = lines[i + 1]
                break

    invoice_line = find(
        r"(Сч[её]т\s+на\s+оплату\s+№\s*[0-9A-Za-zА-Яа-я/-]+\s+от\s+[^\r\n]+)",
        text,
    )
    invoice_number = find(
        r"Сч[её]т\s+на\s+оплату\s+№\s*([0-9A-Za-zА-Яа-я/-]+)",
        text,
    )
    invoice_date = find(
        r"Сч[её]т\s+на\s+оплату\s+№\s*[0-9A-Za-zА-Яа-я/-]+\s+от\s+([^\r\n]+)",
        text,
    )

    amount = find(
        r"Итого\s+к\s+оплате\s*:\s*([0-9][0-9\s]*(?:,[0-9]{2})?)",
        text,
    )
    if not amount:
        amount = find(
            r"Всего\s+наименований.*?на\s+сумму\s+([0-9][0-9\s]*(?:,[0-9]{2})?)",
            text,
        )

    return {
        "seller": seller,
        "regime": regime,
        "inn": inn,
        "bank": bank,
        "phone": phone,
        "invoice_line": invoice_line,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "amount": amount,
    }


def make_message(data: dict) -> str:
    result = []
    fields = [
        ("seller", None),
        ("regime", "Режим НО"),
        ("inn", "ИНН"),
        ("bank", "Банк"),
        ("phone", "Номер телефона"),
    ]
    for key, label in fields:
        value = data.get(key)
        if value:
            result.append(value if label is None else f"{label}: {value}")

    if data.get("invoice_line"):
        result.append(data["invoice_line"])
    else:
        number = data.get("invoice_number") or "не найден"
        date = data.get("invoice_date") or "не найдена"
        result.append(f"Счёт на оплату №{number} от {date}")

    result.append("")
    if data.get("amount"):
        result.append(f"Сумма {data['amount']} руб.")
    else:
        result.append("Сумма не найдена.")

    missing = []
    for key, label in [
        ("seller", "ФИО"),
        ("regime", "режим НО"),
        ("inn", "ИНН"),
        ("bank", "банк"),
        ("phone", "номер телефона"),
        ("invoice_number", "номер счёта"),
        ("invoice_date", "дата счёта"),
        ("amount", "сумма"),
    ]:
        if not data.get(key):
            missing.append(label)
    if missing:
        result.extend(["", "Не удалось найти: " + ", ".join(missing)])

    return "\n".join(result)


@dp.message(CommandStart())
async def start(message: Message):
    if not is_allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    await message.answer(
        "Отправь PDF-счёт. Я извлеку ФИО продавца, режим НО, ИНН, банк, "
        "телефон, номер и дату счёта, а также сумму и пришлю результат текстом."
    )


@dp.message(F.document)
async def process_pdf(message: Message):
    if not is_allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return

    doc = message.document
    filename = (doc.file_name or "").lower()
    if not filename.endswith(".pdf") and doc.mime_type != "application/pdf":
        await message.answer("Пожалуйста, отправь документ в формате PDF.")
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer("Файл больше 20 МБ. Отправь PDF меньшего размера.")
        return

    status = await message.answer("Обрабатываю PDF…")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
            temp_path = Path(temp.name)
        await bot.download(doc, destination=temp_path)

        reader = PdfReader(str(temp_path))
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not extracted.strip():
            await status.edit_text(
                "Не получилось прочитать текст PDF. Возможно, это скан или фото. "
                "Для таких файлов нужно добавить распознавание текста (OCR)."
            )
            return

        data = extract_fields(extracted)
        await status.edit_text(make_message(data))
    except Exception:
        logger.exception("Ошибка обработки PDF")
        await status.edit_text(
            "Не удалось обработать файл. Проверь, что PDF не повреждён, и попробуй ещё раз."
        )
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


@dp.message()
async def other_message(message: Message):
    if not is_allowed(message):
        await message.answer("У вас нет доступа к этому боту.")
        return
    await message.answer("Отправь PDF-счёт, и я извлеку из него нужные данные.")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
