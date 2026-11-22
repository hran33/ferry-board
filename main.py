import time
import network
import urequests
import uasyncio as asyncio
import pngdec
import WIFI_CONFIG
from presto import Presto

SERVER_URL   = "https://ferry-board.onrender.com/departures"
REFRESH_SECS = 60
ROWS         = 3

presto  = Presto(full_res=True, layers=2, ambient_light=False)
for i in range(7): presto.set_led_rgb(i, 0, 0, 0)
display = presto.display
touch   = presto.touch
W, H    = 480, 480

# ── palette ───────────────────────────────────────────────────────────────────
BG         = display.create_pen(11,  15,  25)
HDR_BG     = display.create_pen(0,   131, 156)
WHITE      = display.create_pen(226, 232, 240)
GREEN      = display.create_pen(52,  211, 153)
GREEN_BG   = display.create_pen(6,   78,  59)
DIM        = display.create_pen(75,  85,  99)
DIVIDER    = display.create_pen(20,  25,  41)
BADGE_TEAL = display.create_pen(0,   131, 156)
BADGE_DARK = display.create_pen(20,  25,  41)
BADGE_DTXT = display.create_pen(75,  145, 161)
ALERT_BG   = display.create_pen(28,  16,  8)
ALERT_TXT  = display.create_pen(251, 191, 36)
FOOTER_BG  = display.create_pen(8,   12,  20)

# ── layout (480×480) ──────────────────────────────────────────────────────────
HDR_H    = 102
ROW_H    = 110   # 3 × 110 = 330
ALERT_H  = 48
ROW_Y    = HDR_H                  # 102
ALERT_Y  = ROW_Y + ROWS * ROW_H  # 432
# total: 102 + 330 + 48 = 480 ✓

# ── shared state ─────────────────────────────────────────────────────────────
_data           = None
_last_fetch     = 0
_page           = 0
_needs_render   = True
_alert_popup    = False

# ── WiFi ─────────────────────────────────────────────────────────────────────
async def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_CONFIG.SSID, WIFI_CONFIG.PSK)
        for _ in range(80):
            if wlan.isconnected():
                break
            await asyncio.sleep_ms(250)   # yield to keep CYW43 chip polled
    if wlan.isconnected():
        try:
            import ntptime
            ntptime.settime()
        except Exception:
            pass
    return wlan.isconnected()

# ── data ─────────────────────────────────────────────────────────────────────
def fetch():
    global _data, _last_fetch
    try:
        r = urequests.get(SERVER_URL, timeout=10)
        _data = r.json()
        r.close()
        _last_fetch = time.time()
    except Exception as e:
        print("fetch:", e)

# ── draw helpers ──────────────────────────────────────────────────────────────
def text_centre(s, cx, y, scale=1):
    display.text(s, cx - display.measure_text(s, scale) // 2, y, scale=scale)

def text_right(s, rx, y, scale=1):
    display.text(s, rx - display.measure_text(s, scale), y, scale=scale)

def rounded_rect(x, y, w, h, r=4):
    display.rectangle(x + r, y, w - 2 * r, h)
    display.rectangle(x, y + r, w, h - 2 * r)
    display.circle(x + r,         y + r,         r)
    display.circle(x + w - r - 1, y + r,         r)
    display.circle(x + r,         y + h - r - 1, r)
    display.circle(x + w - r - 1, y + h - r - 1, r)

# ── draw sections ─────────────────────────────────────────────────────────────
def draw_header():
    display.set_pen(HDR_BG)
    display.rectangle(0, 0, W, HDR_H)
    # ferry icon
    try:
        png = pngdec.PNG(display)
        png.open_file("ferry_icon.png")
        png.decode(10, (HDR_H - 44) // 2)
    except Exception:
        pass
    display.set_font("bitmap8")
    display.set_pen(WHITE)
    display.text("EAST RIVER ROUTE", 62, 25, scale=1)
    display.text("Greenpoint", 62, 38, scale=3)
    display.text("> East 34th St", 62, 70, scale=1)
    clock = _data.get("now_local", "--:--") if _data else "--:--"
    text_right(clock, W - 10, 43, scale=2)


def draw_row(idx, dep, y):
    mid      = y + ROW_H // 2
    mins     = dep.get("minutes_away", 0)
    imminent = dep.get("is_today", False) and mins <= 3
    is_today = dep.get("is_today", False)

    # index circle
    display.set_pen(GREEN_BG if imminent else DIVIDER)
    display.circle(40, mid, 16)
    display.set_pen(GREEN if imminent else DIM)
    display.set_font("bitmap8")
    text_centre(str(idx + 1), 40, mid - 4, scale=1)

    # departure time — vertically centred (32px tall at scale=4)
    display.set_pen(GREEN if imminent else WHITE)
    display.text(dep.get("time", "--:--"), 68, y + (ROW_H - 32) // 2, scale=4)

    # badge
    bx, bw = W - 112, 92
    by, bh = mid - 18, 36
    ty      = by + (bh - 16) // 2   # centre 16px text in 36px pill

    if not is_today:
        label = dep.get("day_abbr", "---")
        display.set_pen(BADGE_DARK)
        rounded_rect(bx, by, bw, bh)
        display.set_pen(BADGE_DTXT)
        text_centre(label, bx + bw // 2, ty, scale=2)
    elif mins <= 1:
        display.set_pen(GREEN_BG)
        rounded_rect(bx, by, bw, bh)
        display.set_pen(GREEN)
        text_centre("NOW", bx + bw // 2, ty, scale=2)
    else:
        label = "{} MIN".format(mins)
        display.set_pen(BADGE_TEAL if mins <= 20 else BADGE_DARK)
        rounded_rect(bx, by, bw, bh)
        display.set_pen(WHITE if mins <= 20 else BADGE_DTXT)
        text_centre(label, bx + bw // 2, ty, scale=2)

    # divider
    display.set_pen(DIVIDER)
    display.rectangle(0, y + ROW_H - 1, W, 1)


def draw_alert(alert):
    display.set_pen(ALERT_BG)
    display.rectangle(0, ALERT_Y, W, ALERT_H)
    display.set_pen(ALERT_TXT)
    display.set_font("bitmap8")
    title = alert.get("title", "")
    if len(title) > 56: title = title[:53] + "..."
    display.text("! " + title, 12, ALERT_Y + 10, scale=1)
    display.set_pen(DIM)
    body = alert.get("body", "")
    if len(body) > 72: body = body[:69] + "..."
    display.text(body, 12, ALERT_Y + 27, scale=1)


def show_alert_popup(alert):
    display.set_layer(1)
    display.set_pen(display.create_pen(0, 0, 0))
    display.clear()
    # modal box
    PAD = 28
    display.set_pen(ALERT_BG)
    display.rectangle(PAD, PAD, W - PAD*2, H - PAD*2)
    # amber top bar
    display.set_pen(ALERT_TXT)
    display.rectangle(PAD, PAD, W - PAD*2, 48)
    # title
    display.set_pen(display.create_pen(20, 16, 8))
    display.set_font("bitmap8")
    display.text("! Service Alert", PAD + 12, PAD + 16, scale=2)
    # body text
    display.set_pen(ALERT_TXT)
    title = alert.get("title", "")
    display.text(title, PAD + 12, PAD + 64, W - PAD*2 - 12, scale=1)
    display.set_pen(display.create_pen(160, 120, 60))
    body = alert.get("body", "")
    display.text(body, PAD + 12, PAD + 110, W - PAD*2 - 12, scale=2)
    # dismiss hint
    display.set_pen(DIM)
    display.text("Tap anywhere to dismiss", PAD + 12, H - PAD - 20, scale=1)
    presto.update()


def hide_alert_popup():
    display.set_layer(1)
    display.set_pen(display.create_pen(0, 0, 0))
    display.clear()
    presto.update()


def render(page):
    display.set_layer(0)
    display.set_pen(BG)
    display.clear()
    draw_header()

    if _data:
        deps  = _data.get("departures", [])
        start = page * ROWS
        for i, dep in enumerate(deps[start:start + ROWS]):
            draw_row(start + i, dep, ROW_Y + i * ROW_H)
        alerts = _data.get("alerts", [])
        if alerts:
            draw_alert(alerts[0])
        else:
            display.set_pen(BG)
            display.rectangle(0, ALERT_Y, W, ALERT_H)
    else:
        display.set_pen(DIM)
        display.set_font("bitmap8")
        text_centre("No data", W // 2, H // 2, scale=2)

    presto.update()


def render_loading(msg):
    display.set_layer(0)
    display.set_pen(BG)
    display.clear()
    display.set_pen(HDR_BG)
    display.rectangle(0, 0, W, HDR_H)
    display.set_pen(WHITE)
    display.set_font("bitmap8")
    display.text("Greenpoint", 62, 22, scale=3)
    display.set_pen(DIM)
    text_centre(msg, W // 2, H // 2, scale=2)
    presto.update()


# ── async tasks ───────────────────────────────────────────────────────────────
async def touch_handler_loop():
    global _page, _needs_render, _alert_popup
    touch_y0    = None
    touch_y_cur = 0
    touch_x0    = 0
    touch_x_cur = 0

    while True:
        touch.poll()
        a = presto.touch_a

        if a.touched:
            if touch_y0 is None:
                touch_y0    = a.y
                touch_x0    = a.x
            touch_y_cur = a.y
            touch_x_cur = a.x
        else:
            if touch_y0 is not None:
                dy = touch_y0 - touch_y_cur
                dx = abs(touch_x0 - touch_x_cur)
                is_tap = abs(dy) < 20 and dx < 20

                if _alert_popup:
                    # any gesture dismisses popup
                    _alert_popup = False
                    hide_alert_popup()
                elif is_tap:
                    # tap on alert strip → open popup
                    if (ALERT_Y <= touch_y0 <= ALERT_Y + ALERT_H
                            and _data and _data.get("alerts")):
                        _alert_popup = True
                        show_alert_popup(_data["alerts"][0])
                else:
                    max_page = max(0, (len(_data.get("departures", [])) - 1) // ROWS) if _data else 0
                    if dy > 30:
                        _page = min(_page + 1, max_page)
                        _needs_render = True
                    elif dy < -30:
                        _page = max(_page - 1, 0)
                        _needs_render = True
            touch_y0 = None

        await asyncio.sleep_ms(10)


async def display_loop():
    global _data, _last_fetch, _needs_render
    last_render = 0

    while True:
        now = time.time()

        retry_interval = 5 if _data is None else REFRESH_SECS
        if now - _last_fetch > retry_interval:
            fetch()
            _needs_render = True

        if _needs_render or now - last_render > 30:
            render(_page)
            _needs_render = False
            last_render = now

        await asyncio.sleep_ms(200)


# ── boot ─────────────────────────────────────────────────────────────────────
async def main():
    global _needs_render
    render_loading("Connecting...")
    await connect_wifi()
    render_loading("Fetching schedule...")
    _needs_render = True
    asyncio.create_task(touch_handler_loop())
    asyncio.create_task(display_loop())
    while True:
        await asyncio.sleep(3600)

asyncio.run(main())
