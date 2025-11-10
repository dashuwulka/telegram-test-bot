
"""
Телеграм-бот для тестирования студентов.
- Показывает список тестов из папки tests/ (как кнопки).
- Поддерживает разные форматы ввода (TTFT, a-2,b=1, dcbae и т.д.).
- Автоматически оценивает формализованные вопросы.
- Сохраняет все ответы в Google Sheets:
    - all_answers_json (вся структура ответов)
    - q12_raw, q13_raw (открытые вопросы отдельно)
    - telegram_id (id студента)
    - manual_score_total (преподаватель вписывает туда итог)
    - notified (бот пометит TRUE после уведомления студента)
- Команда /check_updates (только для ADMIN_CHAT_ID) отправляет студенту в Telegram сообщение после того, как преподаватель проставил manual_score_total.
"""
import os
import json
import time
import re
from datetime import datetime
from dotenv import load_dotenv
import threading
from flask import Flask

import telebot
from telebot import types

import gspread
from google.oauth2.service_account import Credentials


# Загрузка конфигурации

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "TestResults")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # строка или пусто

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в .env")

bot = telebot.TeleBot(TOKEN)

# Flask сервер (для Render)
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!", 200

# Загрузка тестов из tests/

TESTS_DIR = "tests"

def load_tests():
    tests = {}
    if not os.path.exists(TESTS_DIR):
        os.makedirs(TESTS_DIR)
    for fname in os.listdir(TESTS_DIR):
        if fname.lower().endswith(".json"):
            path = os.path.join(TESTS_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tests[data["id"]] = data
            except Exception as e:
                print(f"Не удалось загрузить тест {fname}: {e}")
    return tests

TESTS = load_tests()


# Google Sheets 

gc = None
sheet = None
if SERVICE_ACCOUNT_FILE and os.path.exists(SERVICE_ACCOUNT_FILE):
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    try:
        sh = gc.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(SHEET_NAME)
    sheet = sh.sheet1
else:
    print("⚠️ Google service account file не найден. Запись результатов отключена.")

# Базовый заголовок таблицы
BASE_HEADER = [
    "timestamp","student_name","group","test_id","test_title",
    "score","max_score","auto_score","manual_needed",
    "manual_score_total","teacher_comment","all_answers_json",
    "q12_raw","q13_raw","manual_score_q12","manual_score_q13",
    "telegram_id","notified"
]

def ensure_sheet_header():
    if not sheet:
        return
    current = sheet.row_values(1)
    if current[:len(BASE_HEADER)] != BASE_HEADER:
        extra = current[len(BASE_HEADER):] if len(current) > len(BASE_HEADER) else []
        new_header = BASE_HEADER + extra
        sheet.update("A1", [new_header])

if sheet:
    ensure_sheet_header()


# Временное состояние пользователей (в памяти)

user_states = {}

def start_test_for_user(chat_id, test_id, tg_user):
    """Инициализация состояния теста для пользователя."""
    test = TESTS[test_id]
    user_states[chat_id] = {
        "test_id": test_id,
        "test": test,
        "stage": "get_name",
        "index": 0,
        "answers": {},
        "start_time": time.time(),
        "attempt": 1,
        "telegram_id": tg_user.id,  # сохраняем id студента
        "student_username": tg_user.username or ""
    }


# Парсеры ответов (толерантные)

def normalize_choice(s):
    return (s or "").strip().lower()

def parse_matching_input(s):
    """
    Принимает варианты:
      a-2 b-1 c-4
      a=2,b=1;c=4
      a2 b1 c4
    Возвращает dict { 'a': 2, 'b': 1, ... }
    """
    res = {}
    if not s:
        return res
    # заменим разные разделители пробелом
    s2 = s.replace(";", " ").replace(",", " ").replace(":", " ").strip()
    # регулярка на пары: буква (a-i) + optional non-digit + число
    parts = s2.split()
    for p in parts:
        m = re.match(r"^([A-Za-z])\s*[-=:]?\s*(\d+)$", p)
        if m:
            left = m.group(1).lower()
            right = int(m.group(2))
            res[left] = right
        else:
            # try contiguous like a2b1 (not very common) — fallback: search all letter-number pairs
            for mm in re.finditer(r"([A-Za-z])\s*[-=:]?\s*(\d+)", p):
                left = mm.group(1).lower()
                right = int(mm.group(2))
                res[left] = right
    return res

def parse_tf_list_input(s):
    """
    Поддерживает:
      'T T F T'
      'TTFTTFT'
      't,t,f,t'
    Возвращает список ['T','T','F',...]
    """
    if not s:
        return []
    # оставим только буквы T или F (регистр игнорируем)
    s2 = s.upper()
    # заменим запятые и точки на пробелы
    s2 = re.sub(r"[,\.;]", " ", s2)
    # если есть пробелы, разбиваем по пробелу и фильтруем
    if re.search(r"\s", s2):
        parts = [p for p in s2.split() if p in ("T","F","TRUE","FALSE")]
        out = []
        for p in parts:
            if p in ("T","TRUE"):
                out.append("T")
            else:
                out.append("F")
        return out
    # если нет пробелов, вероятно запись слитно: TTFT...
    compact = re.findall(r"[TF]", s2)
    return compact

def parse_ordering_input(s):
    """
    Поддерживает:
     'd c b a e', 'dcbae', 'd,c,b,a,e'
    Возвращает список ['d','c','b','a','e']
    """
    if not s:
        return []
    s2 = s.strip()
    # если есть запятые или пробелы, разделяем
    if "," in s2:
        parts = [p.strip().lower() for p in s2.split(",") if p.strip()]
    elif re.search(r"\s", s2):
        parts = [p.strip().lower() for p in s2.split() if p.strip()]
    else:
        parts = list(s2.lower())
    parts = [p for p in parts if re.match(r"^[a-z]$", p)]
    return parts


# Форматирование вопроса/ответа для отправки в чат

def format_question_text(q):
    text = f"❓ {q['text']}\n"
    t = q["type"]
    if t == "single":
        for i,opt in enumerate(q["options"]):
            text += f"{chr(ord('a')+i)}. {opt}\n"
    elif t == "matching":
        text += "\nLeft:\n"
        for idx,l in enumerate(q["left"]):
            text += f"{chr(ord('a')+idx)}. {l}\n"
        text += "\nRight:\n"
        for idx,r in enumerate(q["right"], start=1):
            text += f"{idx}. {r}\n"
        text += "\n💬 Формат: a-8 b-3 c-4 (или a=2,b=1 и т.д.)"
    elif t == "tf_list":
        for idx,item in enumerate(q["items"], start=1):
            text += f"{idx}. {item}\n"
        text += "\n💬 Формат: T F T ... или TTFT..."
    elif t == "ordering":
        for i,opt in enumerate(q["options"]):
            text += f"{chr(ord('a')+i)}. {opt}\n"
        text += "\n💬 Формат: a b c d e "
    else:
        text += "\n💬 Введите ваш ответ (текст)."
    return text

def make_inline_keyboard_for_testlist():
    kb = types.InlineKeyboardMarkup()
    for tid, t in TESTS.items():
        btn = types.InlineKeyboardButton(text=t.get("title", tid), callback_data=f"take::{tid}")
        kb.add(btn)
    return kb

def make_inline_keyboard_for_options(options):
    kb = types.InlineKeyboardMarkup()
    for i,opt in enumerate(options):
        kb.add(types.InlineKeyboardButton(text=f"{chr(ord('a')+i)}. {opt}", callback_data=chr(ord('a')+i)))
    return kb

# Оценивание

def question_max_points(q):
    if "points" in q:
        return q["points"]
    t = q["type"]
    if t == "single":
        return 1
    if t == "matching":
        return len(q.get("answer", {}))
    if t == "tf_list":
        return len(q.get("items", []))
    if t == "ordering":
        return len(q.get("options", []))
    if t.startswith("free_text"):
        return q.get("points", 2)
    return 1

def grade_answers(test, answers):
    """
    Возвращает словарь:
      { score, max_score, auto_score, manual_needed, per_q_scores, details }
    details содержит диагностическую информацию для формирования отчёта.
    """
    total = 0.0
    max_total = 0.0
    auto_score = 0.0
    manual_needed = False
    per_q_scores = {}
    details = {}

    for q in test["questions"]:
        qid = q["id"]
        pts = question_max_points(q)
        max_total += pts
        score = 0.0
        student_ans = answers.get(qid, "")
        qtype = q["type"]

        # single
        if qtype == "single":
            correct = normalize_choice(q.get("answer", ""))
            got = normalize_choice(student_ans)
            if got == correct:
                score = pts
            details[qid] = {"type":"single", "student": got, "correct": correct, "score": score}

        # matching
        elif qtype == "matching":
            correct_map = {k.lower(): int(v) for k,v in q.get("answer", {}).items()}
            s_map = parse_matching_input(student_ans)
            matched = 0
            for left_key, corr in correct_map.items():
                if s_map.get(left_key) == corr:
                    matched += 1
            # частичная оценка: количество совпавших пар
            score = matched * (pts / max(1, len(correct_map)))
            details[qid] = {"type":"matching", "student_map": s_map, "correct_map": correct_map,
                            "matched_pairs": matched, "total_pairs": len(correct_map), "score": round(score,2)}

        # tf_list
        elif qtype == "tf_list":
            correct = [c.upper() for c in q.get("answer", [])]
            parts = parse_tf_list_input(student_ans)
            matched = 0
            for i, exp in enumerate(correct):
                if i < len(parts) and parts[i] == exp:
                    matched += 1
            # баллы пропорционально правильным
            score = matched * (pts / max(1, len(correct)))
            details[qid] = {"type":"tf_list", "student": parts, "correct": correct,
                            "matched": matched, "total": len(correct), "score": round(score,2)}

        # ordering
        elif qtype == "ordering":
            correct = [c.lower() for c in q.get("answer", [])]
            parts = parse_ordering_input(student_ans)
            matched = 0
            for i, exp in enumerate(correct):
                if i < len(parts) and parts[i] == exp:
                    matched += 1
            score = matched * (pts / max(1, len(correct)))
            details[qid] = {"type":"ordering", "student": parts, "correct": correct,
                            "matched": matched, "total": len(correct), "score": round(score,2)}

        # free_text and free_text_explain
        elif qtype.startswith("free_text"):
            manual_needed = True
            keywords = [k.lower() for k in q.get("keywords", [])] if q.get("keywords") else []
            found = 0
            if isinstance(student_ans, str) and student_ans.strip() and keywords:
                low = student_ans.lower()
                for kw in keywords:
                    if kw in low:
                        found += 1
                ratio = found / len(keywords) if keywords else 0
                score = round(min(pts, pts * ratio), 2)
            else:
                score = 0.0
            details[qid] = {"type": qtype, "student": student_ans, "keywords_found": found,
                            "keywords_total": len(keywords), "auto_score": score}

        else:
            # неизвестный тип — сохраняем как текст
            details[qid] = {"type": qtype, "student": student_ans, "score": 0}

        # накопление
        # округляем score до 2 знаков
        score = round(score, 2)
        per_q_scores[qid] = score
        auto_score += score
        total += score

    return {
        "score": round(total,2),
        "max_score": round(max_total,2),
        "auto_score": round(auto_score,2),
        "manual_needed": manual_needed,
        "per_q_scores": per_q_scores,
        "details": details
    }


# Сохранение результата в Google Sheet

def ensure_header_and_get_indices():
    """
    Возвращает header список и словарь column->index (1-based).
    """
    header = sheet.row_values(1)
    # если заголовок неполный, обновим
    if header[:len(BASE_HEADER)] != BASE_HEADER:
        extra = header[len(BASE_HEADER):] if len(header) > len(BASE_HEADER) else []
        new_header = BASE_HEADER + extra
        sheet.update("A1", [new_header])
        header = new_header
    indices = {h: i+1 for i,h in enumerate(header)}
    return header, indices

def save_result_to_sheet(test, state, result):
    """
    Сохраняет одну строку в таблицу. В колонке 'telegram_id' сохраняем id студента.
    """
    if not sheet:
        return
    header, indices = ensure_header_and_get_indices()
    # подготовка row_map
    row_map = {h: "" for h in header}
    row_map["timestamp"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    row_map["student_name"] = state.get("student_name", "")
    row_map["group"] = state.get("group", "")
    row_map["test_id"] = state.get("test_id", "")
    row_map["test_title"] = test.get("title", "")
    row_map["score"] = result["score"]
    row_map["max_score"] = result["max_score"]
    row_map["auto_score"] = result["auto_score"]
    row_map["manual_needed"] = "YES" if result["manual_needed"] else "NO"
    row_map["manual_score_total"] = ""  # преподаватель вписывает вручную
    row_map["teacher_comment"] = ""
    row_map["all_answers_json"] = json.dumps(state["answers"], ensure_ascii=False)
    # открытые вопросы отдельно (если есть)
    row_map["q12_raw"] = state["answers"].get("q12", "")
    row_map["q13_raw"] = state["answers"].get("q13", "")
    row_map["manual_score_q12"] = ""
    row_map["manual_score_q13"] = ""
    row_map["telegram_id"] = str(state.get("telegram_id", ""))
    row_map["notified"] = ""  # пометка о рассылке после проверки

    # формируем строку в порядке header
    row = [row_map.get(h, "") for h in header]
    sheet.append_row(row)

# Telegram handlers

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    """
    Показываем список тестов как inline-кнопки.
    Сохраняем telegram_id, но не просим email — уведомления будут в Telegram.
    """
    chat_id = message.chat.id
    text = "👋 Привет! Я бот для тестирования.\n\nВыберите тест:"
    if not TESTS:
        bot.send_message(chat_id, text + "\n(Тесты не найдены в папке tests/)")
        return
    kb = make_inline_keyboard_for_testlist()
    bot.send_message(chat_id, text, reply_markup=kb)

# Обработчик нажатий кнопок (выбор теста)
@bot.callback_query_handler(func=lambda call: True)
def callback_query_handler(call):
    data = call.data
    chat_id = call.message.chat.id
    # команда на взятие теста
    if data.startswith("take::"):
        test_id = data.split("::", 1)[1]
        if test_id not in TESTS:
            bot.answer_callback_query(call.id, "Тест не найден.")
            return
        # инициализация состояния: используем message.from_user
        start_test_for_user(chat_id, test_id, call.from_user)
        bot.answer_callback_query(call.id, f"Вы выбрали тест: {TESTS[test_id].get('title')}")
        bot.send_message(chat_id, "Пожалуйста, введите ваше ФИО (полностью):")
        return

    # single-choice ответ (a/b/c) — обрабатываем как выбор для текущего вопроса если это single
    state = user_states.get(chat_id)
    if state:
        # защитимся от неактивных состояний
        try:
            q = state["test"]["questions"][state["index"]]
        except Exception:
            q = None
        if q and q["type"] == "single" and data in ("a","b","c","d","e","f","g","h"):
            qid = q["id"]
            state["answers"][qid] = data
            bot.answer_callback_query(call.id, f"Вы выбрали: {data}")
            # удаляем сообщение с кнопками для чистоты чата
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            state["index"] += 1
            # следующая стадия
            if state["index"] < len(state["test"]["questions"]):
                send_question_to_user(chat_id, state["test"]["questions"][state["index"]])
            else:
                finish_test(chat_id)
            return
    bot.answer_callback_query(call.id, "Нажата неизвестная кнопка.")

def send_question_to_user(chat_id, q):
    """Отправляет вопрос пользователю (inline для single)"""
    if q["type"] == "single":
        kb = make_inline_keyboard_for_options(q["options"])
        bot.send_message(chat_id, format_question_text(q), reply_markup=kb)
    else:
        bot.send_message(chat_id, format_question_text(q))

@bot.message_handler(func=lambda m: True)
def handle_text_message(message):
    chat_id = message.chat.id
    text = message.text.strip() if message.text else ""
    state = user_states.get(chat_id)
    if not state:
        bot.send_message(chat_id, "Отправьте /start чтобы начать тест.")
        return

    stage = state["stage"]
    if stage == "get_name":
        state["student_name"] = text
        state["stage"] = "get_group"
        bot.send_message(chat_id, "Введите вашу группу:")
        return

    if stage == "get_group":
        state["group"] = text
        state["stage"] = "asking"
        state["index"] = 0
        bot.send_message(chat_id, f"🎬 Начинаем тест: {state['test']['title']}")
        # send first question
        send_question_to_user(chat_id, state["test"]["questions"][0])
        return

    if stage == "asking":
        # сохраняем ответ на текущий вопрос как есть
        q = state["test"]["questions"][state["index"]]
        qid = q["id"]
        # Сохраняем raw-ответ (строка)
        state["answers"][qid] = text
        state["index"] += 1
        if state["index"] < len(state["test"]["questions"]):
            send_question_to_user(chat_id, state["test"]["questions"][state["index"]])
        else:
            finish_test(chat_id)
        return


# Завершение теста: оценка, отчёт, запись

def finish_test(chat_id):
    state = user_states.get(chat_id)
    if not state:
        return
    test = state["test"]
    result = grade_answers(test, state["answers"])

    # формируем подробный разбор для студента
    lines = []
    details = result["details"]
    for q in test["questions"]:
        qid = q["id"]
        qtext = q["text"]
        qtype = q["type"]
        pts = question_max_points(q)
        score = result["per_q_scores"].get(qid, 0)
        # для каждого типа формируем сообщение
        if qtype == "single":
            student = details[qid]["student"]
            correct = details[qid]["correct"]
            ok = student == correct
            lines.append(f"Q: {qtext}")
            lines.append(f"   Ваш ответ: {student} | Правильный: {correct} — {'✅' if ok else '❌'} (+{int(score)}/{pts})")
        elif qtype == "matching":
            info = details[qid]
            lines.append(f"Q: {qtext}")
            lines.append(f"   Совпало пар: {info['matched_pairs']}/{info['total_pairs']} — +{round(info['score'],2)}/{pts}")
            lines.append(f"   Ваши пары: {info['student_map']}")
        elif qtype == "tf_list":
            info = details[qid]
            lines.append(f"Q: {qtext}")
            lines.append(f"   Правильных: {info['matched']}/{info['total']} — +{round(info['score'],2)}/{pts}")
            lines.append(f"   Ваш ответ: {' '.join(info['student']) if info['student'] else '(пустой)'}")
        elif qtype == "ordering":
            info = details[qid]
            lines.append(f"Q: {qtext}")
            lines.append(f"   Совпало по позициям: {info['matched']}/{info['total']} — +{round(info['score'],2)}/{pts}")
            lines.append(f"   Правильный порядок: {' '.join(info['correct'])}")
            lines.append(f"   Ваш порядок: {' '.join(info['student']) if info['student'] else '(пустой)'}")
        elif qtype.startswith("free_text"):
            info = details[qid]
            # показываем автооценку по ключевым словам (если есть) и помечаем как требующее проверки
            if info.get("keywords_total", 0) > 0:
                lines.append(f"Q: {qtext}")
                lines.append(f"   Найдено ключевых слов: {info['keywords_found']}/{info['keywords_total']} — авто +{info['auto_score']}/{pts}")
            else:
                lines.append(f"Q: {qtext}")
                lines.append(f"   Ответ отправлен на ручную проверку преподавателю. (+0/{pts} авто)")
            lines.append(f"   Ваш ответ: {info.get('student','(пустой)')}")
        else:
            lines.append(f"Q: {qtext}")
            lines.append(f"   Ответ: {state['answers'].get(qid,'')}")
    # итог
    report = "\n\n".join(lines)
    summary = (f"✅ Тест завершён!\n\n📊 Автоматический разбор:\n\n{report}\n\n"
               f"Автоматический балл: {result['auto_score']} / {result['max_score']}\n")
    if result["manual_needed"]:
        summary += "⚠️ Некоторые ответы требуют ручной проверки преподавателем.\n"

    # сохраняем в Google Sheets
    try:
        save_result_to_sheet(test, state, result)
        sheet_msg = "Результат сохранён в Google Sheets."
    except Exception as e:
        sheet_msg = f"⚠️ Не удалось сохранить в Google Sheets: {e}"

    # отправляем студенту
    bot.send_message(chat_id, summary + "\n" + sheet_msg)

    # уведомление администратору о новом результате убрано по просьбе (не отправляем '📄 Новый результат')
    # если нужно — можно отправить короткое уведомление, но вы просили убрать.

    # удаляем состояние
    del user_states[chat_id]



# Запуск бота

def run_bot():
    print("🤖 Bot starting...")
    while True:
        try:
            print("🔄 Starting bot polling...")
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            print(f"❌ Bot error: {e}")
            print("🔄 Restarting in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Запускаем Flask сервер
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port)
