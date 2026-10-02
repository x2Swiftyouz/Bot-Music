"""Command list shared by /help and the prefix help."""

CATEGORIES: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "play": ("🎵 เล่นเพลง", [
        ("play <ชื่อ/ลิงก์>", "เล่นเพลง เพลย์ลิสต์ Spotify หรือคำค้น"),
        ("search <คำค้น>", "ค้นแล้วเลือกจากรายการ"),
        ("pause / resume", "หยุดชั่วคราว / เล่นต่อ"),
        ("skip", "ข้ามเพลง (โหวตเมื่อมีหลายคนในห้อง)"),
        ("previous", "เพลงก่อนหน้า"),
        ("stop", "หยุดและออกจากห้อง"),
        ("nowplaying", "เพลงที่เล่นอยู่ พร้อมปุ่ม"),
        ("card", "แชร์การ์ดเพลงที่กำลังฟัง"),
        ("seek <เวลา> / forward / backward", "กรอเพลง"),
        ("replay", "เล่นใหม่ตั้งแต่ต้น"),
        ("volume <0-150>", "ปรับเสียง (ไล่ระดับแบบ smooth)"),
    ]),
    "queue": ("📜 คิว", [
        ("queue", "ดูคิว"),
        ("remove <ลำดับ>", "ลบเพลง"),
        ("move <จาก> <ไป>", "ย้ายเพลง"),
        ("jump <ลำดับ>", "ข้ามไปเพลงนั้น"),
        ("shuffle", "สลับคิว"),
        ("clear", "ล้างคิว"),
        ("loop <off/track/queue>", "วนซ้ำ"),
        ("undo", "ย้อน shuffle / clear / remove / move / jump"),
    ]),
    "library": ("📂 เพลย์ลิสต์", [
        ("playlist save/load/list/show/delete", "จัดการเพลย์ลิสต์"),
        ("playlist rename/import", "เปลี่ยนชื่อ นำเข้าจากลิงก์"),
        ("playlist add/removetrack", "เพิ่ม/ลบเพลงในเพลย์ลิสต์"),
    ]),
    "info": ("ℹ️ ทั่วไป", [
        ("help", "คำสั่งทั้งหมด"),
        ("ping", "ความเร็วบอท"),
        ("about", "ข้อมูลบอท"),
        ("log", "ดูว่าใครทำอะไรกับบอท"),
        ("birthday set/remove", "ตั้งวันเกิด การ์ดจะขึ้นป้ายวันเกิดตอนเปิดเพลงของคุณ"),
        ("settings ...", "ตั้งค่าเซิร์ฟเวอร์ (แอดมิน): card, compact, timeformat, 247, announce, voteskip"),
    ]),
}
