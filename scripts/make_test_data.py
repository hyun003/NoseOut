"""2단계 색인·검색 테스트용 파일을 test_data/ 에 만든다. 모두 합성 데이터이며 실제 사용자 파일은 쓰지 않는다.
여러 번 실행해도 같은 내용이 나온다(파일은 덮어쓴다). test_data/ 밖에는 아무것도 쓰지 않는다.

usage: .venv\\Scripts\\python scripts\\make_test_data.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test_data"
sys.path.insert(0, str(Path(__file__).parent))

import make_fixtures as drawings  # noqa: E402  (1단계 합성 그림 재사용)

LATIN = "C:/Windows/Fonts/arial.ttf"
LATIN_BOLD = "C:/Windows/Fonts/arialbd.ttf"
KOREAN = "C:/Windows/Fonts/malgun.ttf"


def font(path, size):
    return ImageFont.truetype(path, size)


# PDF (텍스트 레이어가 있는 PDF를 직접 쓴다: Helvetica, ASCII)


def write_text_pdf(path: Path, pages: list[list[str]], title: str) -> None:
    def escape(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: list[bytes] = []
    n_pages = len(pages)
    # 1 catalog, 2 pages, 3 font, 4 info, 그 뒤 페이지마다 (page, content)
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(f"<< /Title ({escape(title)}) /Producer (DigUp-Windows test data) >>".encode())
    for i, lines in enumerate(pages):
        stream = "BT /F1 11 Tf 72 740 Td 15 TL\n" + "".join(f"({escape(line)}) '\n" for line in lines) + "ET"
        data = stream.encode("latin-1")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                       f"/Contents {6 + 2 * i} 0 R >>".encode())
        objects.append(f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def wrap(text: str, width: int = 90) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + [line] if line else lines


LEASE = [
    "RESIDENTIAL LEASE AGREEMENT. This agreement is made between Greenfield Property Management (the Landlord) and "
    "Jordan Lee (the Tenant) for the apartment at 48 Maple Street, Unit 5B. The term begins on March 1, 2026 and ends "
    "on February 28, 2027. Monthly rent is 1,450 dollars, due on the first day of each month. A late fee of 50 dollars "
    "applies after the fifth day. The security deposit is one month of rent and is held in a separate account.",
    "SECTION 7. PETS. The Tenant may keep one cat or one small dog under 25 pounds in the apartment with prior written "
    "consent of the Landlord. A non-refundable pet fee of 300 dollars is due before the animal moves in. The Tenant is "
    "responsible for any damage caused by the animal, must keep it on a leash in common areas, and must clean up after "
    "it. Exotic animals, reptiles and birds of prey are not allowed under any circumstances.",
    "SECTION 12. TERMINATION. Either party may end this lease at the end of the term by giving sixty days written "
    "notice. If the Tenant leaves early, the Tenant pays rent until a new tenant is found or the term ends, whichever "
    "comes first. Keys must be returned on the last day. The deposit is returned within thirty days after move-out, "
    "less any amounts for unpaid rent or damage beyond normal wear and tear.",
]

TOKYO = """# Tokyo trip, November 2026

- **Nov 3**: Flight to Tokyo Haneda, check in at the hotel in Shinjuku.
- **Nov 4**: Day trip by train to Kamakura to see the Great Buddha and the bamboo grove at Hokokuji temple.
- **Nov 5**: Tsukiji outer market for breakfast, teamLab Planets in the afternoon, dinner in Shibuya.
- **Nov 6**: Fly home in the evening. Buy souvenirs at the airport.

Budget: about 2,000 dollars including flights. Remember the rail pass and a portable charger.
"""

KIMCHI = """김치찌개 레시피 (2인분)

재료: 잘 익은 김치 한 컵, 돼지고기 앞다리살 200g, 두부 반 모, 대파 한 대, 양파 반 개, 고춧가루 한 큰술, 다진 마늘 한 큰술.

1. 냄비에 돼지고기를 넣고 중불에서 기름이 나올 때까지 볶습니다.
2. 김치와 양파를 넣고 3분 정도 더 볶습니다.
3. 물 두 컵과 고춧가루, 다진 마늘을 넣고 끓입니다.
4. 끓어오르면 두부와 대파를 넣고 10분 더 끓이면 완성입니다.

김치가 덜 익었다면 설탕을 조금 넣으면 신맛이 부드러워집니다.
"""

MEETING_HTML = """<!doctype html><html><head><title>Kickoff</title><style>body{font-family:sans-serif}</style></head>
<body><h1>Project Aurora kickoff meeting</h1>
<p>Date: September 22, 2026. Attendees: product, design, engineering.</p>
<ul><li>Launch target moved to the first week of December.</li>
<li>The mobile app will support offline mode for field technicians.</li>
<li>Budget approved: 120,000 dollars for the first phase.</li></ul>
<p>Next meeting: Tuesday at 10 a.m. in room 4. Action item: Mina drafts the onboarding screens.</p>
<script>console.log("not text")</script></body></html>
"""

PARKING_CP949 = "주차 요금 정산 안내\n출구 옆 무인 정산기나 앱으로 주차 요금을 낼 수 있습니다. 첫 30분은 무료입니다.\n"


def invoice_docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("INVOICE INV-4821", level=1)
    document.add_paragraph("Bill to: Harbor Coffee Roasters, 12 Pier Road")
    document.add_paragraph("Date: 2026-09-14. Payment due within 30 days by bank transfer.")
    table = document.add_table(rows=4, cols=2)
    for row, (item, price) in enumerate([("Web hosting (12 months)", "$480.00"), ("Domain renewal", "$20.00"),
                                         ("Support plan", "$750.00"), ("TOTAL DUE", "$1,250.00")]):
        table.rows[row].cells[0].text = item
        table.rows[row].cells[1].text = price
    document.core_properties.title = "Invoice INV-4821"
    document.save(path)


def scanned_korean_pdf(path: Path) -> None:
    page = Image.new("RGB", (1240, 1754), "white")  # A4 150 dpi
    d = ImageDraw.Draw(page)
    d.text((120, 140), "엘리베이터 정기 점검 안내", fill="black", font=font(KOREAN, 64))
    body = ["입주민 여러분께 알려드립니다.", "10월 20일(화) 오전 9시부터 오후 5시까지", "101동 엘리베이터 정기 점검이 있습니다.",
            "점검 시간에는 계단을 이용해 주시기 바랍니다.", "관리사무소 드림"]
    for i, line in enumerate(body):
        d.text((120, 320 + i * 90), line, fill="black", font=font(KOREAN, 42))
    rng = random.Random(3)  # 스캔 느낌의 잡티
    for _ in range(1500):
        x, y = rng.randrange(1240), rng.randrange(1754)
        d.point((x, y), fill=(200, 200, 200))
    page.save(path, "PDF", resolution=150)


def screenshot_error(path: Path) -> None:
    im = Image.new("RGB", (1600, 1000), (243, 243, 243))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1600, 60), fill=(32, 32, 32))
    d.text((24, 16), "Checkout - Example Store", fill="white", font=font(LATIN, 24))
    d.rectangle((400, 300, 1200, 640), fill="white", outline=(200, 200, 200), width=2)
    d.ellipse((450, 350, 530, 430), fill=(210, 40, 40))
    d.text((490, 390), "!", fill="white", font=font(LATIN_BOLD, 54), anchor="mm")
    d.text((560, 360), "Payment declined", fill=(30, 30, 30), font=font(LATIN_BOLD, 44))
    d.text((450, 470), "Your card was declined. Please try another card", fill=(80, 80, 80), font=font(LATIN, 28))
    d.text((450, 510), "or contact your bank. Error code: CARD_DECLINED_51", fill=(80, 80, 80), font=font(LATIN, 28))
    d.rounded_rectangle((900, 560, 1160, 620), 10, fill=(0, 103, 192))
    d.text((1030, 590), "Try again", fill="white", font=font(LATIN_BOLD, 26), anchor="mm")
    im.save(path)


def code_screenshot(path: Path) -> None:
    im = Image.new("RGB", (1400, 900), (30, 30, 30))
    d = ImageDraw.Draw(im)
    for i, line in enumerate(["def retry(call, attempts=5):", "    for n in range(attempts):", "        try:",
                              "            return call()", "        except TimeoutError:",
                              "            time.sleep(2 ** n)"]):
        d.text((60, 80 + i * 50), line, fill=(200, 220, 160), font=font("C:/Windows/Fonts/consola.ttf", 34))
    im.save(path)


def sunset() -> Image.Image:
    w, h = 1200, 800
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h // 2):
        t = y / (h / 2)
        d.line((0, y, w, y), fill=(int(250 - 60 * t), int(120 + 60 * t), int(80 + 40 * t)))
    d.ellipse((520, 300, 680, 460), fill=(255, 200, 80))
    for y in range(h // 2, h):
        t = (y - h / 2) / (h / 2)
        d.line((0, y, w, y), fill=(int(30 + 20 * t), int(60 + 30 * t), int(120 + 40 * t)))
    for k in range(12):  # 물결 반사
        y = h // 2 + 20 + k * 25
        d.line((560 - 40 + k * 3, y, 640 + 40 - k * 3, y), fill=(255, 190, 90), width=4)
    return im


def rotated_photo(path: Path) -> None:
    """화살표가 위를 가리키는 세로 그림을 가로로 저장하고 EXIF Orientation=6(90° 회전해서 보기)을 붙인다."""
    upright = Image.new("RGB", (600, 800), (235, 235, 220))
    d = ImageDraw.Draw(upright)
    d.polygon([(300, 100), (480, 350), (360, 350), (360, 700), (240, 700), (240, 350), (120, 350)], fill=(20, 120, 60))
    stored = upright.transpose(Image.Transpose.ROTATE_90)  # 저장은 800×600
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x0132] = "2026:05:17 14:03:00"
    stored.save(path, exif=exif.tobytes(), quality=92)


# 3단계: 긴 문서와 이름에 단서가 없는 파일

EQUIPMENT = ["compressor", "conveyor belt", "boiler", "cooling tower", "forklift", "ventilation fan", "water heater",
             "air handler", "loading dock door", "freight elevator", "emergency lighting", "sprinkler valve"]
ACTIONS = ["inspect the housing for cracks", "check the mounting bolts", "record the operating temperature",
           "listen for unusual vibration", "clean the intake grille", "verify the indicator lights",
           "lubricate the moving joints", "tighten the electrical terminals", "test the shutoff switch",
           "compare the readings with last month"]
WHEN = ["every week", "at the start of each shift", "once a month", "after any power outage", "twice a year",
        "before the winter season", "whenever the alarm panel shows a warning"]


def filler_paragraph(rng: random.Random, n: int) -> str:
    """주제가 섞인 평범한 점검 문장들. 찾을 내용과 겹치는 단어를 쓰지 않는다."""
    out = []
    for _ in range(n):
        out.append(f"For the {rng.choice(EQUIPMENT)}, {rng.choice(ACTIONS)} {rng.choice(WHEN)}, and "
                   f"{rng.choice(ACTIONS)} if the previous log entry was incomplete.")
    return " ".join(out)


MANUAL_TARGETS = {
    12: "WARRANTY. Damage caused by flooding, leaking pipes or any other water intrusion is not covered by the "
        "manufacturer warranty. Claims for such damage must go to the building insurance policy instead.",
    87: "REPLACING THE HEPA FILTER. Switch the purifier off and unplug it. Open the rear panel, slide the old "
        "HEPA filter out, and insert the new one with the arrow pointing toward the motor. Reset the filter timer.",
    115: "ERROR CODE E42. The drain pump is blocked. Empty the condensate tray, remove the lint from the pump inlet "
         "and restart the unit. If E42 appears again within a day, call a service technician.",
}


def equipment_manual(path: Path) -> None:
    rng = random.Random(42)
    pages = []
    for number in range(1, 121):
        text = f"Facilities manual, page {number}. " + filler_paragraph(rng, 4)
        if number in MANUAL_TARGETS:
            text = f"Facilities manual, page {number}. " + MANUAL_TARGETS[number] + " " + filler_paragraph(rng, 1)
        pages.append(wrap(text))
    write_text_pdf(path, pages, "Facilities Equipment Manual")


HANDBOOK_TARGETS = [
    (4, "Parental leave: every employee who becomes a parent receives sixteen weeks of fully paid leave, which can "
        "be taken in up to three separate blocks during the first year."),
    (55, "The backup diesel generator in the basement is started and run under load for thirty minutes on the first "
         "Monday of every month, so expect the lights to flicker briefly around nine in the morning."),
    (88, "Bicycles may be parked only in the locked cage on basement level B2; access cards for the cage are issued "
         "by the reception desk on the ground floor."),
]


def handbook(path: Path) -> None:
    rng = random.Random(7)
    sections = []
    targets = dict(HANDBOOK_TARGETS)
    for number in range(1, 96):  # 1,800자 청크 약 90개
        body = filler_paragraph(rng, 7)
        if number in targets:
            body = filler_paragraph(rng, 3) + " " + targets[number] + " " + filler_paragraph(rng, 3)
        sections.append(f"Section {number}.\n{body}")
    path.write_text("Employee and Facilities Handbook\n\n" + "\n\n".join(sections), encoding="utf-8")


KO_PLACES = ["사무실", "회의실", "창고", "주차장", "휴게실", "출입구", "엘리베이터 홀", "옥상", "서버실", "탕비실"]
KO_RULES = ["정리 정돈을 유지한다", "사용 후 전등을 끈다", "외부인 출입을 기록한다", "비품 수량을 매주 확인한다",
            "소화기 위치를 표시한다", "창문 잠금 상태를 확인한다", "바닥이 젖으면 즉시 닦는다", "개인 물품을 두지 않는다"]


def korean_rules(path: Path) -> None:
    rng = random.Random(9)
    articles = []
    for number in range(1, 421):
        lines = [f"{rng.choice(KO_PLACES)}에서는 {rng.choice(KO_RULES)}." for _ in range(9)]
        if number == 380:
            lines[4] = "야간 근무를 하는 직원에게는 통상 시급의 1.5배를 야간 근무 수당으로 지급하며, 매월 25일 급여와 함께 지급한다."
        articles.append(f"제{number}조\n" + " ".join(lines))
    path.write_text("사내 규정\n\n" + "\n\n".join(articles), encoding="utf-8")


def house_drawing() -> Image.Image:
    im = Image.new("RGB", (1000, 750), (190, 225, 250))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 560, 1000, 750), fill=(110, 180, 90))
    d.rectangle((330, 330, 670, 600), fill=(240, 230, 200))
    d.polygon([(300, 340), (500, 170), (700, 340)], fill=(190, 40, 40))
    d.rectangle((470, 470, 540, 600), fill=(110, 70, 40))
    d.rectangle((370, 380, 440, 440), fill=(150, 200, 240))
    d.rectangle((560, 380, 630, 440), fill=(150, 200, 240))
    d.rectangle((815, 420, 845, 600), fill=(100, 70, 40))
    d.ellipse((740, 280, 920, 450), fill=(40, 130, 50))
    return im


def misc_files(folder: Path) -> None:
    """파일 이름에 내용 단서가 없는 파일들 (뜻으로만 찾을 수 있다)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "note_a.md").write_text(
        "Spring care: move the succulent into a pot one size larger, use gritty cactus soil, and wait a week "
        "before the first watering so the roots can heal.\n", encoding="utf-8")
    import docx

    document = docx.Document()
    document.add_paragraph("After the collision at the intersection, take photos of both cars, exchange licence and "
                           "insurer details with the other driver, and report it to your insurer within 24 hours.")
    document.save(folder / "doc_17.docx")
    (folder / "memo_3.txt").write_text(
        "세탁기 배수 필터는 한 달에 한 번 열어서 보풀과 동전을 빼 주세요. 막히면 물이 빠지지 않고 오류가 납니다.\n",
        encoding="utf-8")
    house_drawing().save(folder / "img_a.png")


# 4단계: 음성·영상 (Windows 내장 TTS + 그림 + ffmpeg, 실제 녹음·영상 없음)

PODCAST = [  # (주제, 문장들). 각 주제 사이에 2초 쉼. 시작 시각은 media_files가 돌려준다.
    ("intro", ["Welcome back to the weekly home show. Today we have three topics for you.",
               "First a quick word about the new community garden, then the main topic, and travel at the end."]),
    ("garden", ["The community garden opens next month on the corner of Elm Street.",
                "Volunteers can sign up for a plot, and tools will be shared in a small shed by the gate.",
                "Bring your own gloves, and remember that the water tap is only turned on in the mornings."]),
    ("sleep", ["Now to our main topic, which is sleep.",
               "Most adults need between seven and nine hours of sleep every night.",
               "Keep the bedroom cool and dark, and put the phone away an hour before bed.",
               "If you cannot fall asleep after twenty minutes, get up and read until you feel tired."]),
    ("travel", ["Finally, travel. Trains are often faster than flying for trips under five hundred kilometers.",
                "Book early for the cheapest seats, and bring snacks for the journey."]),
]


def tts_wav(text: str, culture: str, out: Path) -> None:
    import os
    import subprocess

    env = {**os.environ, "TTS_CULTURE": culture, "TTS_OUT": str(out), "TTS_TEXT": text}
    done = subprocess.run(["powershell", "-NoProfile", "-Command", drawings.TTS], env=env, capture_output=True, text=True)
    if done.returncode != 0 or not out.exists():
        raise RuntimeError(f"TTS failed ({culture}): {done.stderr.strip()[:200]}")


def read_wav(path: Path):
    import wave

    import numpy as np

    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), "<i2")


def write_wav16(samples, path: Path) -> None:
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.astype("<i2").tobytes())


def ffmpeg(*args: str) -> None:
    import subprocess

    sys.path.insert(0, str(ROOT / "src"))
    from noseout import media

    tool = media.find("ffmpeg")
    if not tool:
        raise RuntimeError("ffmpeg not found: vendor/ffmpeg (docs/progress.md 4단계)")
    subprocess.run([tool, "-v", "error", "-y", *args], check=True)


def zebra_stripes() -> Image.Image:
    im = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(im)
    for x in range(-720, 1280, 80):
        d.polygon([(x, 720), (x + 40, 720), (x + 760, 0), (x + 720, 0)], fill="black")
    return im


def sailboat() -> Image.Image:
    im = Image.new("RGB", (1280, 720), (150, 200, 240))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 470, 1280, 720), fill=(30, 90, 160))
    d.polygon([(480, 450), (800, 450), (760, 520), (520, 520)], fill=(120, 70, 30))
    d.line((640, 450, 640, 140), fill=(80, 60, 40), width=10)
    d.polygon([(645, 150), (645, 440), (820, 440)], fill="white")
    d.polygon([(635, 180), (635, 440), (500, 440)], fill=(245, 245, 230))
    return im


def birthday_cake() -> Image.Image:
    im = Image.new("RGB", (1280, 720), (60, 40, 70))
    d = ImageDraw.Draw(im)
    d.rectangle((420, 380, 860, 600), fill=(240, 200, 220))
    d.rectangle((420, 380, 860, 420), fill=(250, 250, 250))
    for k in range(5):
        x = 470 + k * 85
        d.rectangle((x, 300, x + 14, 380), fill=(90, 160, 230))
        d.ellipse((x - 6, 270, x + 20, 305), fill=(255, 190, 40))
    d.ellipse((380, 580, 900, 640), fill=(220, 220, 220))
    return im


def media_files(folder: Path) -> dict[str, float]:
    """test_data/media/ 를 만든다. 팟캐스트 주제별 시작 초를 돌려준다(평가 질의의 기대 위치)."""
    import numpy as np

    folder.mkdir(parents=True, exist_ok=True)
    work = ROOT / ".tmp" / "make_media"
    work.mkdir(parents=True, exist_ok=True)
    gap = np.zeros(2 * 16000, np.int16)
    parts, starts, position = [], {}, 0
    for n, (topic, sentences) in enumerate(PODCAST):
        tts_wav(" ".join(sentences), "en-US", work / f"p{n}.wav")
        voice = read_wav(work / f"p{n}.wav")
        starts[topic] = position / 16000
        parts += [voice, gap]
        position += len(voice) + len(gap)
    write_wav16(np.concatenate(parts), work / "podcast.wav")
    ffmpeg("-i", str(work / "podcast.wav"), "-c:a", "aac", "-b:a", "64k", str(folder / "home_show_ep12.m4a"))

    tts_wav("내일 오전 열 시에 치과 예약이 있어요. 잊지 말고 건강보험증을 챙겨 가세요.", "ko-KR", work / "memo.wav")
    ffmpeg("-i", str(work / "memo.wav"), "-c:a", "aac", "-b:a", "48k", str(folder / "음성메모_0917.m4a"))

    rng = np.random.default_rng(5)  # 거의 무음: 아주 작은 잡음 6초 → 색인하지 않아야 한다
    write_wav16(rng.normal(0, 8, 6 * 16000), folder / "room_tone.wav")

    for name, draw in [("s1", zebra_stripes), ("s2", sailboat), ("s3", birthday_cake)]:
        draw().save(work / f"{name}.png")
    tts_wav("Good evening. Tomorrow will be cloudy in the morning with light rain in the afternoon, "
            "so take a jacket if you go out.", "en-US", work / "narration.wav")
    ffmpeg("-loop", "1", "-t", "5", "-i", str(work / "s1.png"), "-loop", "1", "-t", "5", "-i", str(work / "s2.png"),
           "-loop", "1", "-t", "5", "-i", str(work / "s3.png"), "-i", str(work / "narration.wav"),
           "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p,fps=10[v]",
           "-map", "[v]", "-map", "3:a", "-c:v", "mpeg4", "-q:v", "4", "-c:a", "aac", "-t", "15",
           str(folder / "clip_2026-08-30.mp4"))
    return starts


def main() -> None:
    import pillow_heif

    for sub in ["documents", "pictures", "pictures/Screenshots", "code_project", "node_modules/left-pad", ".hidden",
                "certs"]:
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    docs, pics = OUT / "documents", OUT / "pictures"

    write_text_pdf(docs / "lease_agreement.pdf", [wrap(p) for p in LEASE], "Residential Lease - 48 Maple Street")
    scanned_korean_pdf(docs / "엘리베이터_점검_안내.pdf")
    (docs / "tokyo_trip.md").write_text(TOKYO, encoding="utf-8")
    (docs / "김치찌개_레시피.txt").write_text(KIMCHI, encoding="utf-8")
    (docs / "kickoff_meeting.html").write_text(MEETING_HTML, encoding="utf-8")
    (docs / "주차_안내_cp949.txt").write_bytes(PARKING_CP949.encode("cp949"))
    invoice_docx(docs / "invoice_INV-4821.docx")
    rng = random.Random(11)
    (docs / "sensor_dump.txt").write_text("\n".join(
        " ".join(f"{rng.uniform(0, 100):.3f}" for _ in range(8)) for _ in range(300)) + "\nSERIAL-77Q3X\n",
        encoding="utf-8")

    drawings.stop_sign().convert("RGB").save(pics / "stop_sign.jpg", quality=90)
    drawings.night_sky().save(pics / "night_sky.png")
    drawings.bar_chart().save(pics / "quarterly_chart.png")
    drawings.red_circle().save(pics / "red_circle.webp")
    pillow_heif.from_pillow(sunset()).save(pics / "sunset_beach.heic", quality=90)
    rotated_photo(pics / "IMG_0042.jpg")
    screenshot_error(pics / "Screenshots" / "Screenshot 2026-10-01 093000.png")
    Image.new("RGB", (64, 64), (0, 120, 215)).save(pics / "app_icon.png")  # 너무 작음 → 건너뜀

    # 건너뛰어야 하는 것들
    (OUT / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnot a real key\n", encoding="utf-8")
    (OUT / "certs" / "server.key").write_text("not a real key\n", encoding="utf-8")
    (OUT / ".hidden" / "diary.txt").write_text("hidden folder text: should never be indexed\n", encoding="utf-8")
    (OUT / "node_modules" / "left-pad" / "README.md").write_text("left-pad: pads strings on the left\n",
                                                                  encoding="utf-8")
    (OUT / "measurements.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (OUT / "code_project" / "package.json").write_text('{"name": "demo"}\n', encoding="utf-8")
    (OUT / "code_project" / "index.js").write_text("console.log('hi')\n", encoding="utf-8")
    (OUT / "code_project" / "NOTES.md").write_text("Design notes inside a code project: not indexed.\n",
                                                   encoding="utf-8")
    code_screenshot(OUT / "code_project" / "Screenshot 2026-10-02 retry.png")

    # 3단계
    (docs / "long").mkdir(exist_ok=True)
    equipment_manual(docs / "long" / "facilities_manual.pdf")
    handbook(docs / "long" / "handbook.txt")
    korean_rules(docs / "long" / "사내_규정.txt")
    misc_files(OUT / "misc")

    # 4단계
    starts = media_files(OUT / "media")
    print("podcast topic starts (s):", {k: round(v, 1) for k, v in starts.items()})
    print(f"test data -> {OUT}")


if __name__ == "__main__":
    main()
