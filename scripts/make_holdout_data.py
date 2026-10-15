"""독립 평가용 합성 데이터를 test_data_holdout/ 에 만든다 (4단계 시작 전, setsScale 켬/끔 재비교용).

3단계 평가 세트(test_data, evals/test_data.json)와 주제·문장·그림·질의를 겹치지 않게 새로 썼다. 질의(evals/holdout.json)는
결과를 보기 전에 확정했다. 긴 문서는 같은 문장을 되풀이하지 않고 장마다 주제가 다른 "책"처럼 만든다.
test_data_holdout/ 밖에는 아무것도 쓰지 않는다. 실제 사용자 파일은 쓰지 않는다.

usage: .venv\\Scripts\\python scripts\\make_holdout_data.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test_data_holdout"
sys.path.insert(0, str(Path(__file__).parent))
from make_test_data import wrap, write_text_pdf  # noqa: E402  (PDF 작성기만 재사용)

LATIN = "C:/Windows/Fonts/arial.ttf"
BOLD = "C:/Windows/Fonts/arialbd.ttf"
KOREAN = "C:/Windows/Fonts/malgun.ttf"


def font(path, size):
    return ImageFont.truetype(path, size)


# 긴 영어 책: 장마다 다른 주제의 문장 묶음 (같은 장 안에서만 섞는다)

GARDEN_CHAPTERS = {
    "Soil": ["Healthy soil crumbles in the hand and smells faintly sweet after rain.",
             "Clay soil holds water for days, while sandy soil drains within minutes.",
             "Mixing in compost each autumn slowly darkens and loosens heavy ground.",
             "Earthworms are a sign that the soil has enough air and organic matter.",
             "A simple jar test shows the share of sand, silt and clay in a sample.",
             "Mulch keeps the surface from crusting and shelters soil life from the sun."],
    "Seeds": ["Small seeds such as lettuce need light to germinate and should barely be covered.",
              "Beans and peas can be sown straight into the bed once the frost has passed.",
              "A heat mat under the tray speeds up peppers that sulk in cold compost.",
              "Label every row on the day it is sown, because seedlings look alike for weeks.",
              "Thin crowded seedlings early so the strongest have room to spread their roots.",
              "Old seed packets lose vigour, so test a few on damp paper before sowing a whole row."],
    "Watering": ["Water deeply and less often so roots grow down instead of staying near the surface.",
                 "Morning watering lets leaves dry before evening and keeps mildew away.",
                 "A finger pushed into the soil is a better guide than the calendar.",
                 "Drip lines deliver water to the roots and waste very little to evaporation.",
                 "Pots in full sun can dry out twice a day in the middle of summer.",
                 "Collected rainwater is softer than tap water and loved by acid-loving plants."],
    "Pests": ["Aphids cluster on new shoots and can be washed off with a firm spray of water.",
              "Slugs feed at night, so a torch walk after dark shows where the damage comes from.",
              "Ladybirds and lacewings eat far more aphids than any spray can reach.",
              "Netting over brassicas stops cabbage white butterflies from laying eggs.",
              "Yellow sticky cards reveal whiteflies in a greenhouse before numbers explode.",
              "Rotating crops each year breaks the life cycle of pests that overwinter in the soil."],
    "Pruning": ["Cut just above an outward-facing bud so the new shoot grows away from the centre.",
                "Remove dead, damaged and crossing branches before shaping anything else.",
                "Spring-flowering shrubs are pruned right after they bloom, not in winter.",
                "Sharp, clean blades make smooth cuts that heal quickly.",
                "Never take more than a third of a shrub in one season.",
                "Pinching out the tips of herbs keeps them bushy instead of leggy."],
    "Harvest": ["Pick courgettes small and often, or the plant stops making new fruit.",
                "Tomatoes ripen best on the vine, but a sunny windowsill finishes green ones.",
                "Dig potatoes on a dry day and let the skins harden before storing them.",
                "Herbs taste strongest when cut in the morning after the dew has dried.",
                "Onions are ready when the leaves fall over and start to turn yellow.",
                "Store apples in a cool dark shed, not touching one another."],
    "Compost": ["A good compost heap mixes green kitchen scraps with brown dry leaves.",
                "Turning the heap every few weeks adds air and speeds up decay.",
                "Avoid meat and dairy, which attract rats and smell as they rot.",
                "Finished compost is dark, crumbly and smells like a forest floor.",
                "A heap that smells sour is too wet and needs more cardboard or straw.",
                "Worm bins turn kitchen waste into rich castings even on a balcony."],
    "Greenhouse": ["Open the vents on sunny mornings, even in early spring, to stop overheating.",
                   "Shade paint on the glass keeps summer temperatures bearable for seedlings.",
                   "Bubble wrap on the inside walls holds heat on frosty nights.",
                   "Water butts inside the greenhouse store warmth and release it after dark.",
                   "Clean the glass in autumn to let in every bit of winter light.",
                   "A max-min thermometer shows how cold it really got overnight."],
}
GARDEN_TARGETS = {
    # 장, 그 장의 몇 번째 문단에 넣을지, 문장
    ("Seeds", 3): "Our record: the giant pumpkin from the north bed weighed 212 kilograms and won the county show.",
    ("Pests", 20): "Copper tape around the raised beds kept snails out completely for the whole wet summer.",
    ("Greenhouse", 25): "The old paraffin heater was replaced with an electric fan heater wired to a frost thermostat.",
}


def garden_book(path: Path) -> list[int]:
    rng = random.Random(2041)
    parts = ["The Patient Gardener\n"]
    for chapter, sentences in GARDEN_CHAPTERS.items():
        parts.append(f"\nChapter: {chapter}\n")
        for paragraph in range(30):
            chosen = rng.sample(sentences, 4)
            target = GARDEN_TARGETS.get((chapter, paragraph))
            if target:
                chosen.insert(2, target)
            parts.append(" ".join(chosen) + "\n")
    path.write_text("\n".join(parts), encoding="utf-8")


# 긴 PDF: 도시 안내서, 장마다 다른 주제

CITY_CHAPTERS = {
    "Old Town": ["The old town grew around a river crossing that merchants used for centuries.",
                 "Narrow lanes still follow the medieval property lines.",
                 "Timber-framed houses lean over the streets, their upper floors wider than the ground floors.",
                 "The market square hosts a flower market every Saturday morning.",
                 "A stone bridge with seven arches links the two halves of the old town."],
    "Transport": ["Trams run every eight minutes on the main lines during the day.",
                  "A single ticket is valid for ninety minutes, including transfers.",
                  "Night buses replace the trams after midnight on weekends.",
                  "Bicycles can be rented from docking stations near every tram stop.",
                  "The central station has direct trains to the airport every twenty minutes."],
    "Food": ["The local speciality is a dumpling filled with smoked cheese and herbs.",
             "Bakeries open at six and sell out of rye bread by noon.",
             "Street stalls near the river sell grilled fish wrapped in paper.",
             "Many restaurants close on Mondays, so plan dinner ahead.",
             "The covered food hall gathers twenty small kitchens under one roof."],
    "Museums": ["The art museum is free on the first Sunday of every month.",
                "A combined pass covers six museums for three days.",
                "The science centre has a planetarium with shows every hour.",
                "Audio guides are available in eight languages at the entrance.",
                "Photography without flash is allowed in most galleries."],
    "Parks": ["The botanical garden keeps a tropical glasshouse warm all year.",
              "A lake in the city park freezes in winter and becomes a skating rink.",
              "Joggers use a five-kilometre loop that circles the hill park.",
              "Dogs must be kept on a lead near the playgrounds.",
              "Open-air concerts take place in the park bandstand in July."],
    "History": ["The city walls were torn down in the nineteenth century to make room for boulevards.",
                "A great fire destroyed much of the harbour district two centuries ago.",
                "The university, founded in the fourteenth century, is one of the oldest in the region.",
                "The town hall clock has struck the hour since it was installed by a guild of smiths.",
                "Many street names recall the crafts that were practised there."],
}
CITY_TARGETS = {
    9: "The cathedral tower has 386 steps, and the climb is closed whenever wind speed exceeds 60 km/h.",
    44: "Lost property from trams and buses is kept for three months at the office behind platform 2.",
    73: "In 1911 a hot-air balloon landed on the roof of the opera house, and the dent in the copper dome is still visible.",
}


def city_guide(path: Path) -> None:
    rng = random.Random(77)
    chapters = list(CITY_CHAPTERS.items())
    pages = []
    for number in range(1, 91):
        name, sentences = chapters[(number - 1) // 15]
        text = f"{name}. " + " ".join(rng.sample(sentences, 4))
        if number in CITY_TARGETS:
            text = f"{name}. " + CITY_TARGETS[number] + " " + " ".join(rng.sample(sentences, 2))
        pages.append(wrap(text + " " + " ".join(rng.sample(sentences, 2))))
    write_text_pdf(path, pages, "A Walker's Guide to the River City")


# 긴 한국어 문서: 사내 IT 안내서, 장마다 다른 주제

KO_CHAPTERS = {
    "계정": ["처음 입사하면 인사팀이 발급한 임시 비밀번호로 로그인한 뒤 바로 바꿔야 합니다.",
            "비밀번호는 90일마다 바꾸며 이전 세 개와 같은 것은 쓸 수 없습니다.",
            "다섯 번 연속으로 틀리면 계정이 30분 동안 잠깁니다.",
            "퇴사자의 계정은 마지막 근무일 저녁에 자동으로 비활성화됩니다.",
            "공용 계정은 쓰지 않고 한 사람에게 한 계정을 발급합니다."],
    "메일": ["회사 메일함의 용량은 50기가바이트이며 넘으면 오래된 메일부터 보관함으로 옮겨집니다.",
            "외부로 보내는 첨부 파일은 25메가바이트까지 가능합니다.",
            "의심스러운 메일은 열지 말고 보안팀 신고 버튼을 누르세요.",
            "부재 중 자동 회신은 웹 메일의 설정 메뉴에서 켤 수 있습니다.",
            "메일 서명에는 이름, 부서, 내선 번호를 적습니다."],
    "회의실": ["회의실은 사내 포털에서 최대 2주 앞까지 예약할 수 있습니다.",
             "예약하고 10분 안에 아무도 오지 않으면 예약이 자동으로 취소됩니다.",
             "화상 회의 장비는 큰 회의실 세 곳에만 설치되어 있습니다.",
             "회의가 끝나면 화이트보드를 지우고 의자를 정리해 주세요.",
             "외부 손님과의 회의는 1층 접견실을 우선 사용합니다."],
    "보안": ["자리를 비울 때는 반드시 화면을 잠급니다.",
            "개인 USB 저장장치는 회사 컴퓨터에 연결할 수 없습니다.",
            "출입증은 항상 보이는 곳에 착용합니다.",
            "중요 문서는 퇴근 전에 잠금 서랍에 넣습니다.",
            "보안 교육은 매년 상반기에 온라인으로 이수해야 합니다."],
    "장비": ["노트북은 입사 첫날 IT 지원 창구에서 받습니다.",
            "모니터가 고장 나면 사내 포털에 수리 요청을 등록합니다.",
            "장비 교체 주기는 노트북 4년, 모니터 6년입니다.",
            "프린터 용지가 떨어지면 각 층 탕비실 옆 비품함을 확인하세요.",
            "분실한 장비는 즉시 보안팀과 IT 지원팀에 알려야 합니다."],
    "원격근무": ["재택근무 때는 반드시 회사 VPN에 접속한 뒤 업무 시스템을 씁니다.",
              "VPN 접속이 끊기면 앱을 다시 실행하고 그래도 안 되면 IT 지원팀에 연락합니다.",
              "집에서 쓰는 공유기는 기본 비밀번호를 바꿔 두는 것이 좋습니다.",
              "원격근무 신청은 하루 전까지 팀장 승인을 받아야 합니다.",
              "화상 회의 때는 배경 흐림 기능을 켜도 됩니다."],
}
KO_TARGETS = {
    ("장비", 2): "모니터 받침대와 키보드 같은 개인 사무용품은 1년에 15만 원까지 회사가 비용을 지원합니다.",
    ("원격근무", 9): "해외에서 원격근무를 하려면 최소 3주 전에 보안팀의 사전 검토를 받아야 하며, 최대 30일까지만 허용됩니다.",
}


def korean_it_guide(path: Path) -> None:
    rng = random.Random(510)
    parts = ["사내 IT 이용 안내서\n"]
    for edition in range(7):  # 같은 장 구성이 판을 거듭한 것처럼 일곱 번 (내용은 매번 섞인다)
        for chapter, sentences in KO_CHAPTERS.items():
            parts.append(f"\n[{chapter}]\n")
            for paragraph in range(12):
                chosen = rng.sample(sentences, 4)
                target = KO_TARGETS.get((chapter, paragraph))
                if target and edition == 6:  # 마지막 판에만: 문서의 뒷부분
                    chosen.insert(1, target)
                parts.append(" ".join(chosen) + "\n")
    path.write_text("\n".join(parts), encoding="utf-8")


# 짧은 문서들

SHORT_DOCS = {
    "documents/email_landlord.txt":
        "Hi Marta, the kitchen tap has been dripping since Sunday and the cold water pressure is very low. "
        "Could you send a plumber this week? I am home every afternoon after three. Thanks, Sam",
    "documents/gym_membership.md":
        "# Membership\n\nMonthly fee 39 euros, cancel with one month notice. The pool is closed for cleaning on "
        "the first Tuesday of each month. Towels are not included; bring your own or rent one for 2 euros.",
    "documents/birthday_party.txt":
        "Plan for Leo's 7th birthday: dinosaur theme, 12 kids, Saturday 2-5 pm at the community hall. "
        "Order a chocolate cake shaped like a volcano. Games: fossil dig in the sandpit, pin the tail on the T-rex.",
    "documents/car_service.txt":
        "Service record: oil and filter changed at 45,210 km, front brake pads replaced, wiper blades new. "
        "Next inspection due in March. The mechanic noted a small leak from the rear shock absorber.",
    "documents/독서모임.md":
        "# 10월 독서 모임\n\n이번 달 책은 장편 소설이고, 각자 가장 인상 깊은 장면을 하나씩 골라 오기로 했다. "
        "장소는 역 앞 카페 2층, 셋째 주 목요일 저녁 7시. 다음 달 책은 투표로 정한다.",
    "documents/이사_체크리스트.txt":
        "이사 전에 할 일: 전입 신고, 인터넷 이전 신청, 가스 점검 예약, 관리비 정산. "
        "냉장고는 하루 전에 비워 두고, 깨지기 쉬운 그릇은 신문지로 따로 싼다.",
    "documents/allergy_note.txt":
        "Note for the babysitter: Mia is allergic to peanuts and tree nuts. Her EpiPen is in the blue pouch "
        "on the fridge door. Call 112 first, then us, if she has trouble breathing.",
    "documents/recipe_bread.md":
        "No-knead loaf: 500 g flour, 400 ml water, 8 g salt, a pinch of yeast. Mix, cover, leave 18 hours. "
        "Bake in a hot cast-iron pot, 30 minutes with the lid on and 15 without.",
}


def screenshot(path: Path, title: str, lines: list[tuple[str, int, tuple]], accent=(0, 103, 192)) -> None:
    im = Image.new("RGB", (1500, 940), (245, 245, 245))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1500, 56), fill=(40, 40, 40))
    d.text((24, 14), title, fill="white", font=font(LATIN, 24))
    y = 160
    for text, size, color in lines:
        d.text((120, y), text, fill=color, font=font(BOLD if size >= 36 else LATIN, size))
        y += size + 30
    d.rounded_rectangle((120, y + 20, 380, y + 80), 10, fill=accent)
    im.save(path)


def drawings() -> dict[str, Image.Image]:
    out = {}
    # 우산
    im = Image.new("RGB", (900, 900), (210, 220, 230))
    d = ImageDraw.Draw(im)
    for i in range(60):
        x = (i * 97) % 900
        d.line((x, (i * 53) % 900, x - 15, (i * 53) % 900 + 40), fill=(120, 140, 170), width=3)
    d.pieslice((180, 180, 720, 720), 180, 360, fill=(30, 90, 200))
    d.line((450, 450, 450, 760), fill=(60, 60, 60), width=14)
    d.arc((390, 720, 450, 800), 0, 180, fill=(60, 60, 60), width=14)
    out["IMG_3301.jpg"] = im
    # 자전거
    im = Image.new("RGB", (1100, 700), "white")
    d = ImageDraw.Draw(im)
    for cx in (300, 800):
        d.ellipse((cx - 170, 330, cx + 170, 670), outline=(30, 30, 30), width=12)
        for k in range(12):
            a = math.radians(k * 30)
            d.line((cx, 500, cx + 160 * math.cos(a), 500 + 160 * math.sin(a)), fill=(120, 120, 120), width=3)
    d.line((300, 500, 520, 320, 800, 500, 560, 500, 300, 500), fill=(200, 30, 30), width=14)
    d.line((520, 320, 500, 250), fill=(30, 30, 30), width=10)
    d.line((450, 245, 560, 245), fill=(30, 30, 30), width=14)
    d.line((800, 500, 760, 260, 720, 240), fill=(30, 30, 30), width=10)
    out["IMG_3302.jpg"] = im
    # 커피 잔
    im = Image.new("RGB", (900, 800), (230, 210, 180))
    d = ImageDraw.Draw(im)
    d.ellipse((200, 600, 700, 700), fill=(250, 250, 250))
    d.rectangle((270, 320, 600, 640), fill=(255, 255, 255))
    d.ellipse((270, 290, 600, 360), fill=(90, 50, 20))
    d.arc((560, 380, 700, 540), 270, 90, fill=(255, 255, 255), width=26)
    for k in range(3):
        d.arc((320 + k * 80, 150, 380 + k * 80, 280), 90, 270, fill=(150, 150, 150), width=6)
    out["IMG_3303.jpg"] = im
    # 원그래프
    im = Image.new("RGB", (900, 900), "white")
    d = ImageDraw.Draw(im)
    start = 0
    for share, color in [(40, (230, 120, 40)), (25, (60, 150, 220)), (20, (90, 180, 90)), (15, (200, 60, 120))]:
        d.pieslice((150, 150, 750, 750), start, start + share * 3.6, fill=color)
        start += share * 3.6
    out["chart_final.png"] = im
    # 눈 덮인 산
    im = Image.new("RGB", (1200, 800), (150, 200, 245))
    d = ImageDraw.Draw(im)
    d.polygon([(0, 800), (420, 220), (800, 800)], fill=(110, 110, 120))
    d.polygon([(420, 220), (340, 330), (390, 310), (430, 345), (480, 300), (500, 330)], fill="white")
    d.polygon([(500, 800), (850, 300), (1200, 800)], fill=(90, 95, 105))
    d.polygon([(850, 300), (790, 385), (840, 365), (880, 395), (910, 385)], fill="white")
    out["IMG_3304.jpg"] = im
    # 시계
    im = Image.new("RGB", (800, 800), (240, 240, 240))
    d = ImageDraw.Draw(im)
    d.ellipse((100, 100, 700, 700), fill="white", outline=(30, 30, 30), width=16)
    for k in range(12):
        a = math.radians(k * 30)
        d.line((400 + 250 * math.cos(a), 400 + 250 * math.sin(a), 400 + 280 * math.cos(a), 400 + 280 * math.sin(a)),
               fill=(30, 30, 30), width=10)
    d.line((400, 400, 400, 200), fill=(30, 30, 30), width=14)
    d.line((400, 400, 540, 470), fill=(30, 30, 30), width=18)
    out["IMG_3305.jpg"] = im
    return out


def main() -> None:
    for sub in ["documents/long", "pictures", "pictures/Screenshots"]:
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    garden_book(OUT / "documents/long/the_patient_gardener.txt")
    city_guide(OUT / "documents/long/river_city_guide.pdf")
    korean_it_guide(OUT / "documents/long/IT_안내서.txt")
    for rel, text in SHORT_DOCS.items():
        (OUT / rel).write_text(text + "\n", encoding="utf-8")
    for name, im in drawings().items():
        im.save(OUT / "pictures" / name, quality=90) if name.endswith(".jpg") else im.save(OUT / "pictures" / name)
    shots = OUT / "pictures/Screenshots"
    screenshot(shots / "Screenshot 2026-09-02 101500.png", "Sign in",
               [("Incorrect password", 44, (190, 30, 30)), ("You have 2 attempts left before your account", 28,
                                                            (70, 70, 70)), ("is locked for 30 minutes.", 28, (70, 70, 70))])
    screenshot(shots / "Screenshot 2026-09-05 160200.png", "Downloads",
               [("Download complete", 44, (20, 120, 40)), ("holiday_photos.zip  -  2.4 GB", 28, (70, 70, 70)),
                ("Saved to the Downloads folder", 28, (70, 70, 70))], accent=(20, 120, 40))
    screenshot(shots / "Screenshot 2026-09-11 083000.png", "Calendar",
               [("Dentist", 44, (30, 30, 30)), ("Thursday 16 October, 4:30 pm", 30, (70, 70, 70)),
                ("Reminder 1 hour before", 28, (70, 70, 70))])
    screenshot(shots / "Screenshot 2026-09-20 221000.png", "Battery",
               [("Battery low: 5%", 44, (200, 120, 0)), ("Connect your charger now.", 28, (70, 70, 70)),
                ("Battery saver is on.", 28, (70, 70, 70))], accent=(200, 120, 0))
    print(f"holdout data -> {OUT}")


if __name__ == "__main__":
    main()
