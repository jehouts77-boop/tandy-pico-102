# m100_home.py
#
# Real home-menu program for the Model 102 + Pico W terminal.
# Confirmed link settings: 19200 baud, 8N1, no flow control.
# On the Model 102: STAT string 98N1D, then into the live terminal.
#
# Run this from Thonny like the earlier test scripts. Once it's solid,
# copy it to main.py on the board so the Pico boots straight into it.
#
# IMPORTANT for SOFTWARE UPDATE (OTA, see that section below): this
# file must actually be saved as main.py on the Pico's own flash (not
# just run ad hoc from Thonny) - the OTA code renames/replaces
# main.py by name, so it has nothing to act on until that's done once.

from machine import UART, Pin, reset
import network
import socket
import time
import secrets
import random
import os

try:
    import ssl
except ImportError:
    import ussl as ssl

try:
    import ujson as json
except ImportError:
    import json

try:
    import ntptime
except ImportError:
    ntptime = None

# DIAGNOSED 2026-09-14 from a hardware screenshot: a HEADLINES page was
# missing its own header line ('BBC NEWS HEADLINES') - everything had
# shifted up by exactly one row. The line right where the shift started
# was exactly 40 characters wide with nothing after it before the \r\n.
# Best explanation: TELCOM auto-wraps its cursor the moment column 40
# is written, so a line that exactly fills the display width, followed
# by our own \r\n, causes TWO line advances instead of one - silently
# eating a line off the top of the 6-line budget. The physical screen
# is still 40 columns wide (that part's real - see club100.org and the
# ROWS note below), but nothing this program prints should actually
# reach column 40, so COLS is set one narrower than the true width as
# a safety margin. Comment left in place in case this needs revisiting
# once it's re-tested on hardware.
COLS = 39

# CONFIRMED 2026-09-13 via a row-fill test: the screen is NOT 8 usable
# content rows. The bottom row is a fixed F-key legend (not ours to use),
# and at 8 lines + a leading blank line (9 lines total) the first two
# lines scrolled off before the user could read them. Dropping to 6
# content lines (no leading blank, no separator row) fit with the cursor
# landing on the row right above the F-key legend, no scrolling and no
# dropped bytes at 500ms/line. Treat 6 as the safe budget until proven
# otherwise - going back up to 7 is worth trying later, but 6 is what's
# confirmed working right now.
ROWS = 6

# If typed characters DON'T show up on the Model 102's own screen as you
# type, leave this True so the Pico echoes each keystroke back.
# If characters show up DOUBLED, TELCOM is already echoing locally -
# set this to False.
ECHO_INPUT = True

uart = UART(0, baudrate=19200, tx=Pin(0), rx=Pin(1), bits=8, parity=None, stop=1)


# CONFIRMED 2026-09-13: 500ms/line, at ROWS=6 (no leading blank line),
# rendered a full clean screen with no dropped/garbled characters. 600ms
# was the last untested guess before this; 500 is what's actually been
# proven on the real hardware, so that's what we're using now. Worth
# trying to dial down further later, but no need to guess anymore -
# lower it and re-test the same way (report back what shows up).
LINE_DELAY_MS = 500


# FIX 2026-09-13: a typed note containing a high-bit character (the
# M102 keyboard/TELCOM can send codes above 127 - e.g. its GRPH-shifted
# graphics characters) crashed send_screen() with UnicodeError. Root
# cause: this MicroPython build's str.encode() doesn't actually
# implement the 'replace' error handler - .encode('ascii', 'replace')
# still raises instead of substituting. Same risk existed on the
# decode side in read_line(). Fixed by doing the ASCII-safing by hand
# (byte-by-byte / char-by-char) instead of trusting the codec's error
# mode, so this can't happen regardless of what a given MicroPython
# build supports.
def _ascii_safe_chr(code):
    return chr(code) if 32 <= code < 127 else '?'


def bytes_to_ascii(buf):
    """Turn raw serial bytes into a str, replacing anything outside
    printable 7-bit ASCII with '?'."""
    return ''.join(_ascii_safe_chr(b) for b in buf)


def ascii_safe(text):
    """Same idea, starting from a str instead of raw bytes."""
    return ''.join(_ascii_safe_chr(ord(ch)) for ch in text)


def send_screen(lines):
    """Print a block of up to ROWS lines, each truncated to COLS. No
    leading blank line - that turned out to just cost us a row for
    nothing. TELCOM has no screen-clear/cursor addressing, so this
    prints a fresh block each time and lets it scroll - same idea as
    the browser mockup's pages, just append-only instead of redrawn."""
    for i in range(ROWS):
        text = lines[i] if i < len(lines) else ''
        uart.write(ascii_safe(text[:COLS]).encode('ascii'))
        uart.write(b'\r\n')
        time.sleep_ms(LINE_DELAY_MS)


def read_line():
    """Block until a full line (terminated by CR) arrives from the
    Model 102, optionally echoing each character back as it arrives."""
    buf = b''
    while True:
        if uart.any():
            b = uart.read(1)
            if not b:
                continue
            if b in (b'\r', b'\n'):
                if ECHO_INPUT:
                    uart.write(b'\r\n')
                return bytes_to_ascii(buf).strip()
            if ECHO_INPUT:
                uart.write(b)
            buf += b
        else:
            time.sleep(0.02)


def _sync_clock():
    """Best-effort NTP time sync, needed only by ON THIS DAY so it knows
    today's real UTC date. Skipped quietly if ntptime isn't available on
    this firmware build or the sync fails for any reason - the caller
    just falls back to whatever the Pico's RTC already reads, which may
    be wrong until this actually succeeds."""
    if ntptime is None:
        return
    try:
        ntptime.settime()
    except Exception:
        pass


def show_pages(header, lines):
    """Shared pager: same pattern proven first in Notes' view flow -
    one header line, up to ROWS-2 content lines per page, and a footer
    that reads 'ENTER=MORE   0=BACK' while more remains or '0=CONTINUE'
    on the last page. Any keypress but '0' advances; '0' exits right
    away. Unlike notes_view_flow() (which rebuilds a numeric-range
    header per page), `header` here is one fixed line shown on every
    page - fine for a fixed title, a fixed feature name, etc."""
    page_size = ROWS - 2
    if not lines:
        lines = ['(NOTHING TO SHOW)']
    total = len(lines)
    start = 0
    while start < total:
        page = lines[start:start + page_size]
        end = start + len(page)
        has_more = end < total

        screen = [header[:COLS]]
        screen.extend(page)
        while len(screen) < ROWS - 1:
            screen.append('')
        screen.append('ENTER=MORE   0=BACK' if has_more else '0=CONTINUE')
        send_screen(screen)

        reply = read_line()
        if reply == '0' or not has_more:
            return
        start = end


# Tracks whichever network the Pico is ACTUALLY using right now, which
# isn't always secrets.WIFI_SSID once wifi_connect_to() below has been
# used to switch to something else (a phone hotspot, a friend's WiFi,
# etc.) for the current session. None means "not connected right now."
# MicroPython's WLAN object doesn't reliably expose "what SSID am I
# associated with" across ports, so this is tracked by hand instead of
# queried from the hardware.
CURRENT_SSID = None


def connect_wifi(timeout_s=15):
    """Connects to the HOME network from secrets.py - but only if
    nothing is connected yet. If wifi_connect_to() below has already
    connected to some OTHER network this session, isconnected() is
    already True, so this deliberately does nothing and leaves that
    connection alone - every module that just wants "some working
    WiFi" calls this, and it transparently keeps using whatever
    network is currently active rather than forcing things back to
    home mid-session."""
    global CURRENT_SSID
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout_s:
                return None
            time.sleep(0.5)
        CURRENT_SSID = secrets.WIFI_SSID
    return wlan


def wifi_connect_to(ssid, password, timeout_s=15):
    """Connects to an arbitrary network for THIS SESSION ONLY - never
    writes to secrets.py, so a power-cycle reverts back to the home
    network. Drops any current connection first. Returns (True, None)
    on success or (False, message) on failure."""
    global CURRENT_SSID
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        wlan.disconnect()
    try:
        wlan.connect(ssid, password)
    except Exception as e:
        return False, 'CONNECT ERROR: {}'.format(e)

    start = time.time()
    while not wlan.isconnected():
        if time.time() - start > timeout_s:
            return False, 'TIMED OUT'
        time.sleep(0.5)

    CURRENT_SSID = ssid
    return True, None


def wifi_scan():
    """Scans for nearby networks. Returns (True, [(ssid, rssi,
    security), ...]) - deduplicated by SSID (keeping the strongest
    signal seen for each), sorted strongest-first, capped to the 4
    strongest so they fit a single screen with single-digit picks (no
    multi-page selection needed) - or (False, message) on failure.

    FIX 2026-09-15: hardware testing hit 'NO NETWORKS FOUND' every
    time, even right after a fresh DISCONNECT. Two known cyw43
    (Pico W/2 W radio) quirks both produce exactly that symptom:
    scan() can come back empty while STA is still associated to an
    AP, and it can also come back empty on a just-activated/just-
    disconnected radio that hasn't had a moment to settle yet. This
    now explicitly disconnects before scanning (safe - connect_wifi()
    will silently reconnect to the home network the next time
    anything needs WiFi if this gets cancelled) and retries once
    after a short delay if the first pass comes back empty."""
    global CURRENT_SSID
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        wlan.disconnect()
        CURRENT_SSID = None
        time.sleep_ms(300)

    try:
        results = wlan.scan()
    except Exception as e:
        return False, 'SCAN ERROR: {}'.format(e)

    if not results:
        time.sleep_ms(500)
        try:
            results = wlan.scan()
        except Exception as e:
            return False, 'SCAN ERROR: {}'.format(e)

    # DIAGNOSED 2026-09-15 from hardware: the added counters below
    # (see the FIX comment above) came back 'RAW 11 HIDDEN 11 USABLE 0'
    # right next to a phone hotspot and with a working home-network
    # connection - i.e. scan() DID see 11 real networks, but every
    # single one of them got misread as "hidden", which is essentially
    # impossible in a real environment. This firmware/MicroPython
    # build's 'hidden' field isn't trustworthy, so it's no longer used
    # to filter anything - a genuinely hidden network already shows up
    # as an empty SSID, which the `if not ssid` check below still
    # catches correctly on its own.
    raw_count = len(results)
    seen = {}
    for entry in results:
        if len(entry) >= 6:
            ssid_raw, bssid, channel, rssi, security = entry[:5]
        elif len(entry) == 5:
            ssid_raw, bssid, channel, rssi, security = entry
        else:
            continue
        ssid = bytes_to_ascii(ssid_raw).strip()
        if not ssid:
            continue
        if ssid not in seen or rssi > seen[ssid][1]:
            seen[ssid] = (ssid, rssi, security)

    networks = sorted(seen.values(), key=lambda t: -t[1])[:4]
    if not networks:
        return False, 'RAW {} ALL BLANK SSID'.format(raw_count)
    return True, networks


def wifi_status_screen():
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active() or not wlan.isconnected():
        send_screen(['WIFI STATUS', 'NOT CONNECTED.', 'CONNECTING...',
                      '', '', ''])
        wlan = connect_wifi()
        if wlan is None:
            send_screen(['WIFI STATUS', 'COULD NOT CONNECT.',
                          'CHECK secrets.py AND TRY AGAIN.', '', '',
                          '0=BACK'])
            return

    ip = wlan.ifconfig()[0]
    try:
        rssi_line = '{} DBM'.format(wlan.status('rssi'))
    except Exception:
        rssi_line = 'N/A'

    send_screen([
        'WIFI STATUS',
        'SSID     ' + (CURRENT_SSID or '?'),
        'SIGNAL   ' + rssi_line,
        'IP ADDR  ' + ip,
        'STATUS   CONNECTED',
        '0=BACK   R=REFRESH',
    ])


def wifi_status_flow():
    wifi_status_screen()
    while True:
        choice = read_line()
        if choice.lower() == 'r':
            wifi_status_screen()
        else:
            return


def wifi_scan_connect_flow():
    send_screen(['SCAN & CONNECT', 'SCANNING...', '', '', '', ''])
    ok, result = wifi_scan()
    if not ok:
        send_screen(['SCAN & CONNECT', 'COULD NOT SCAN:', result[:COLS],
                      '', '', '0=CONTINUE'])
        read_line()
        return

    picks = {}
    screen = ['PICK A NETWORK (*=SECURE):']
    for i, (ssid, rssi, security) in enumerate(result, 1):
        picks[str(i)] = (ssid, security)
        marker = ' *' if security else ''
        screen.append('{}. {}{}'.format(i, ssid, marker)[:COLS])
    while len(screen) < ROWS - 1:
        screen.append('')
    screen.append('0=BACK')
    send_screen(screen)

    choice = read_line()
    if choice == '0' or choice == '':
        return
    if choice not in picks:
        send_screen(['SCAN & CONNECT', 'INVALID CHOICE.', '', '', '',
                      '0=CONTINUE'])
        read_line()
        return

    ssid, security = picks[choice]
    password = ''
    if security:
        send_screen(['SCAN & CONNECT', ssid[:COLS], 'ENTER PASSWORD:',
                      '0=BACK TO CANCEL', '', ''])
        password = read_line()
        if password == '0':
            return

    send_screen(['SCAN & CONNECT', 'CONNECTING TO:', ssid[:COLS], '', '', ''])
    ok, msg = wifi_connect_to(ssid, password)
    if ok:
        send_screen(['SCAN & CONNECT', 'CONNECTED.', ssid[:COLS],
                      'LASTS UNTIL NEXT REBOOT.', '', '0=CONTINUE'])
    else:
        send_screen(['SCAN & CONNECT', 'FAILED:', msg[:COLS], '', '',
                      '0=CONTINUE'])
    read_line()


def wifi_disconnect_flow():
    global CURRENT_SSID
    wlan = network.WLAN(network.STA_IF)
    was_connected = wlan.isconnected()
    ssid = CURRENT_SSID
    if was_connected:
        wlan.disconnect()
    CURRENT_SSID = None

    if was_connected:
        send_screen(['DISCONNECT', 'DISCONNECTED FROM:', (ssid or '?')[:COLS],
                      'NEXT WIFI USE RECONNECTS', 'TO YOUR HOME NETWORK.',
                      '0=CONTINUE'])
    else:
        send_screen(['DISCONNECT', 'NOT CONNECTED.', '', '', '',
                      '0=CONTINUE'])
    read_line()


# ---------------------------------------------------------------------
# SOFTWARE UPDATE (OTA) module
#
# Added 2026-09-16 so the Pico can update its own code over WiFi once
# it's sealed inside the Model 102's case and no longer easy to plug
# into a laptop. First built on top of the Notes Apps Script relay
# (DriveApp storage), then briefly redesigned around a Google Doc, but
# BOTH were abandoned 2026-09-16 after discovering DriveApp access is
# blocked for anonymous/external callers on an unverified Apps Script
# project - it works fine when run by hand in the script editor, but
# fails every time when hit via the deployed /exec URL, no matter how
# many times permissions are granted or the deployment is redeployed
# (a Google policy thing, not a bug in that code). Landed on a **public
# GitHub repo** instead: github.com/jehouts77-boop/tandy-pico-102,
# holding just this one file. No secret/token needed at all since it's
# public - simpler than any of the Google-based designs, and immune to
# the Google Doc plan's rich-text corruption risk, since git stores
# exact bytes rather than something a word processor could autocorrect.
#
# Versioning has no separate number to maintain by hand: the "remote
# version" is the file's git blob SHA, read from GitHub's Contents API
# (a small JSON response, cheap to poll) - it changes automatically
# every time the file's content changes, no matter how the update gets
# pushed (web UI upload, git push, editing directly on github.com). The
# Pico remembers whichever SHA it last installed in a small local file
# (OTA_LOCAL_VERSION_FILE) and compares the two - no match required
# beyond "did this change." The actual program bytes come from a
# separate plain-text fetch to raw.githubusercontent.com - the Contents
# API response also embeds the content, but base64-encoded and capped
# for large files, so a plain raw fetch is simpler with no size games.
#
# To publish a future update: replace m100_home.py in that repo (the
# GitHub web UI's "Upload files", overwriting the existing one, is
# easiest - no git command line needed) and the Pico picks up the new
# SHA automatically next time SOFTWARE UPDATE > CHECK FOR UPDATES runs.
#
# Safety: a download is written to a temp file first and only swapped
# into place after it completely finishes and passes a basic sanity
# check, so a dropped connection mid-download leaves the currently
# running program untouched. The previous file is kept as a one-level
# backup (OTA_BACKUP_FILENAME) specifically so ROLLBACK works without
# any laptop access, since that's the whole point once this is inside
# a closed case.
# ---------------------------------------------------------------------

OTA_GH_OWNER = 'jehouts77-boop'
OTA_GH_REPO = 'tandy-pico-102'
OTA_GH_BRANCH = 'main'
OTA_GH_PATH = 'm100_home.py'

OTA_MAIN_FILENAME = 'main.py'        # the file MicroPython boots from
OTA_BACKUP_FILENAME = 'main_prev.py'
OTA_NEW_FILENAME = 'main_new.py'
OTA_SWAP_FILENAME = '_ota_swap.py'
OTA_LOCAL_VERSION_FILE = 'ota_version.txt'
OTA_MAX_BYTES = 200000  # generous headroom over the ~60KB program file
OTA_MIN_BYTES = 5000    # anything smaller than this is obviously not it


def _ota_read_local_version():
    try:
        with open(OTA_LOCAL_VERSION_FILE, 'r') as f:
            return f.read().strip()
    except OSError:
        return 'UNKNOWN'


def _ota_write_local_version(version):
    with open(OTA_LOCAL_VERSION_FILE, 'w') as f:
        f.write(version)


def _ota_check_version():
    """Fetches the program file's current git blob SHA from GitHub's
    Contents API - a small JSON response (a couple KB, not the whole
    ~60KB program) that changes automatically whenever the file's
    content changes. No auth needed - the repo is public. Reuses
    _json_str_after() (see the Text Browser / ON THIS DAY section
    below) to pull the "sha" field out by hand rather than a full JSON
    parse, same reasoning as onthisday_fetch(). Returns (True, sha) or
    (False, message)."""
    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'
    url = 'https://api.github.com/repos/{}/{}/contents/{}?ref={}'.format(
        OTA_GH_OWNER, OTA_GH_REPO, OTA_GH_PATH, OTA_GH_BRANCH)
    try:
        status, body = http_get(url, user_agent=WIKI_USER_AGENT, max_bytes=4096)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)
    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)
    sha = _json_str_after(body, '"sha":"')
    if not sha:
        return False, 'BAD RESPONSE FROM GITHUB'
    return True, sha


def _ota_fetch_program():
    """Downloads the actual program file straight from GitHub's raw
    content host - plain source text exactly as committed, no JSON
    wrapper and no base64 to decode."""
    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'
    url = 'https://raw.githubusercontent.com/{}/{}/{}/{}'.format(
        OTA_GH_OWNER, OTA_GH_REPO, OTA_GH_BRANCH, OTA_GH_PATH)
    try:
        status, body = http_get(url, user_agent=WIKI_USER_AGENT, max_bytes=OTA_MAX_BYTES)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)
    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)
    return True, body


def ota_check_flow():
    send_screen(['SOFTWARE UPDATE', 'CHECKING...', '', '', '', ''])
    ok, remote_version = _ota_check_version()
    if not ok:
        send_screen(['SOFTWARE UPDATE', 'COULD NOT CHECK:',
                      remote_version[:COLS], '', '', '0=CONTINUE'])
        read_line()
        return

    local_version = _ota_read_local_version()
    if remote_version == local_version:
        send_screen(['SOFTWARE UPDATE', 'ALREADY UP TO DATE.', '', '', '',
                      '0=CONTINUE'])
        read_line()
        return

    send_screen(['SOFTWARE UPDATE', 'AN UPDATE IS AVAILABLE.',
                  'TYPE Y TO INSTALL IT', '', '', '0=CANCEL'])
    choice = read_line().upper()
    if choice != 'Y':
        return

    send_screen(['SOFTWARE UPDATE', 'DOWNLOADING...', '', '', '', ''])
    ok, body = _ota_fetch_program()
    if not ok:
        send_screen(['SOFTWARE UPDATE', 'DOWNLOAD FAILED:', body[:COLS],
                      '', 'NOTHING CHANGED.', '0=CONTINUE'])
        read_line()
        return

    if isinstance(body, str):
        body = body.encode('utf-8')
    if len(body) < OTA_MIN_BYTES or b'def main()' not in body:
        send_screen(['SOFTWARE UPDATE', 'DOWNLOADED FILE LOOKS',
                      'WRONG - NOT INSTALLED.', '', 'NOTHING CHANGED.',
                      '0=CONTINUE'])
        read_line()
        return

    try:
        with open(OTA_NEW_FILENAME, 'wb') as f:
            f.write(body)
        try:
            os.remove(OTA_BACKUP_FILENAME)
        except OSError:
            pass
        os.rename(OTA_MAIN_FILENAME, OTA_BACKUP_FILENAME)
        os.rename(OTA_NEW_FILENAME, OTA_MAIN_FILENAME)
        _ota_write_local_version(remote_version)
    except Exception as e:
        send_screen(['SOFTWARE UPDATE', 'INSTALL ERROR:', str(e)[:COLS],
                      '', 'NOTHING CHANGED.', '0=CONTINUE'])
        read_line()
        return

    send_screen(['SOFTWARE UPDATE', 'INSTALLED.', 'REBOOTING IN 3 SEC...',
                  'IF SOMETHING LOOKS WRONG,', 'USE ROLLBACK AFTER.', ''])
    time.sleep(3)
    reset()


def ota_rollback_flow():
    try:
        os.stat(OTA_BACKUP_FILENAME)
    except OSError:
        send_screen(['SOFTWARE UPDATE', 'NO PREVIOUS VERSION', 'IS SAVED.',
                      '', '', '0=CONTINUE'])
        read_line()
        return

    send_screen(['SOFTWARE UPDATE', 'ROLL BACK TO THE',
                  'PREVIOUS VERSION?', 'TYPE Y TO CONFIRM', '', '0=CANCEL'])
    choice = read_line().upper()
    if choice != 'Y':
        return

    try:
        os.rename(OTA_MAIN_FILENAME, OTA_SWAP_FILENAME)
        os.rename(OTA_BACKUP_FILENAME, OTA_MAIN_FILENAME)
        os.rename(OTA_SWAP_FILENAME, OTA_BACKUP_FILENAME)
    except Exception as e:
        send_screen(['SOFTWARE UPDATE', 'ROLLBACK FAILED:', str(e)[:COLS],
                      '', '', '0=CONTINUE'])
        read_line()
        return

    send_screen(['SOFTWARE UPDATE', 'ROLLED BACK.', 'REBOOTING IN 3 SEC...',
                  '', '', ''])
    time.sleep(3)
    reset()


def ota_menu():
    send_screen([
        'SOFTWARE UPDATE',
        'RUNNING: {}'.format(_ota_read_local_version())[:COLS],
        ' 1  CHECK FOR UPDATES',
        ' 2  ROLLBACK LAST UPDATE',
        '',
        '0=BACK',
    ])


def ota_menu_loop():
    ota_menu()
    while True:
        choice = read_line()
        if choice == '0' or choice == '':
            return
        if choice == '1':
            ota_check_flow()
        elif choice == '2':
            ota_rollback_flow()
        ota_menu()


def wifi_menu():
    send_screen([
        'WIFI - ENTER A NUMBER:',
        ' 1  STATUS',
        ' 2  SCAN & CONNECT',
        ' 3  DISCONNECT',
        ' 4  SOFTWARE UPDATE',
        '0=BACK',
    ])


def wifi_menu_loop():
    wifi_menu()
    while True:
        choice = read_line()
        if choice == '0' or choice == '':
            return
        if choice == '1':
            wifi_status_flow()
        elif choice == '2':
            wifi_scan_connect_flow()
        elif choice == '3':
            wifi_disconnect_flow()
        elif choice == '4':
            ota_menu_loop()
        wifi_menu()


# ---------------------------------------------------------------------
# NOTES module
#
# Append-only note capture. Each note the user types on the Model 102
# gets sent as one HTTPS GET to a Google Apps Script "web app" URL
# (see apps_script_notes.gs), which appends it as a timestamped line to
# a Google Doc. No OAuth on the Pico - the Apps Script deployment itself
# is the auth boundary, gated by a shared-secret query param.
#
# Written dependency-free (raw socket + ssl) rather than pulling in
# urequests, to match the rest of this file and avoid a mip/package
# install step. NOT YET BENCH-TESTED on real hardware - this is the
# next thing to prove out the same way WiFi Status was (see build log).
# Apps Script web app URLs redirect (302) from script.google.com to
# script.googleusercontent.com to actually serve the response, so
# http_get() follows a bounded number of redirects.
# ---------------------------------------------------------------------

def url_quote(s):
    """Minimal percent-encoding for a URL query value."""
    safe = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~'
    out = []
    for ch in s:
        if ch in safe:
            out.append(ch)
        else:
            out.append('%{:02X}'.format(ord(ch) & 0xFF))
    return ''.join(out)


def http_get(url, timeout_s=10, max_redirects=3, user_agent=None, max_bytes=None):
    """Minimal dependency-free HTTP/HTTPS GET using HTTP/1.0 + Connection:
    close, so responses arrive as one plain body with no chunked-transfer
    decoding needed. Returns (status_code, body_text). Follows up to
    max_redirects 30x redirects. Raises on network/socket errors - callers
    should wrap in try/except. Optional user_agent lets a caller identify
    itself to APIs (like Wikipedia's) that ask for a descriptive
    User-Agent - no personal info goes in it, just what the project is.

    Optional max_bytes caps how much of the response this will buffer
    before giving up and closing the connection early, returning
    whatever prefix it got instead of the whole thing. FIX 2026-09-15:
    ON THIS DAY crashed with a MemoryError ('memory allocation failed')
    - the real payload for that endpoint turned out to be far bigger
    than expected (each event bundles full Wikipedia page metadata, not
    just its own text - see the Text Browser module notes). HTTP/1.0 +
    Connection: close means there's no Content-Length to check ahead of
    time, so the only way to bound memory use is to stop reading partway
    through and accept a truncated body. Since HTTP/1.0 already signals
    the end of a response by closing the connection, stopping early on
    the client side isn't a protocol violation - the server just sees a
    client that hung up. Callers that can tolerate a partial/truncated
    result (like _onthisday_events(), which already stops cleanly at
    the first incomplete event) should pass this; callers that need a
    complete response (Notes, Home Control - both small and known-safe)
    should leave it unset."""
    for _ in range(max_redirects + 1):
        scheme, rest = url.split('://', 1)
        host_part, _, path = rest.partition('/')
        path = '/' + path
        if ':' in host_part:
            host, port_s = host_part.split(':', 1)
            port = int(port_s)
        else:
            host = host_part
            port = 443 if scheme == 'https' else 80

        addr = socket.getaddrinfo(host, port)[0][-1]
        s = socket.socket()
        s.settimeout(timeout_s)
        s.connect(addr)
        if scheme == 'https':
            s = ssl.wrap_socket(s, server_hostname=host)

        req = 'GET {} HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n'.format(path, host)
        if user_agent:
            req += 'User-Agent: {}\r\n'.format(user_agent)
        req += '\r\n'
        s.write(req.encode())

        # Accumulate chunks in a list and join once at the end, rather
        # than repeated `resp += chunk` - bytes are immutable, so `+=`
        # reallocates and copies the WHOLE thing again on every chunk
        # (roughly doubling the memory churn for no reason). One join
        # at the end does a single allocation instead.
        chunks = []
        total = 0
        while True:
            chunk = s.read(512)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if max_bytes is not None and total >= max_bytes:
                break
        s.close()
        resp = b''.join(chunks)

        header_bytes, _, body = resp.partition(b'\r\n\r\n')
        header_lines = header_bytes.decode('ascii', 'replace').split('\r\n')
        status_code = int(header_lines[0].split()[1])
        headers = {}
        for line in header_lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        if status_code in (301, 302, 303, 307, 308) and 'location' in headers:
            url = headers['location']
            continue
        return status_code, body.decode('utf-8', 'replace')
    return None, 'TOO MANY REDIRECTS'


def notes_send(text):
    """POSTs (as a GET, to sidestep Apps Script's POST-redirect quirks)
    one note to the Apps Script relay. Returns (ok, message)."""
    if not getattr(secrets, 'NOTES_URL', ''):
        return False, 'NOTES_URL NOT SET IN secrets.py'

    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'

    url = '{}?secret={}&text={}'.format(
        secrets.NOTES_URL,
        url_quote(secrets.NOTES_SECRET),
        url_quote(text),
    )
    try:
        status, body = http_get(url)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)

    if status == 200 and body.strip().upper().startswith('OK'):
        return True, 'SAVED'
    return False, 'SERVER SAID: {}'.format(body.strip()[:COLS])


def notes_list(n):
    """Fetches the last n notes (newest first) from the Apps Script
    relay's 'list' action. Returns (ok, lines) where lines is a list of
    strings on success, or (False, message) on failure."""
    if not getattr(secrets, 'NOTES_URL', ''):
        return False, 'NOTES_URL NOT SET IN secrets.py'

    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'

    url = '{}?secret={}&action=list&n={}'.format(
        secrets.NOTES_URL,
        url_quote(secrets.NOTES_SECRET),
        n,
    )
    try:
        status, body = http_get(url)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)

    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)

    lines = [ln for ln in body.split('\n') if ln.strip()]
    if not lines:
        lines = ['(NO NOTES YET)']
    return True, lines


def _notes_intro_screen():
    send_screen([
        'NOTES',
        'TYPE A NOTE AND PRESS ENTER.',
        'IT SAVES TO YOUR NOTES DOC.',
        '',
        'V=VIEW LAST NOTES',
        '0=BACK',
    ])


def notes_view_flow():
    """Asks how many recent notes to pull (1-9), fetches them, and
    pages through them via the shared show_pages() pager.

    FIX 2026-09-15: this used to show each note as one raw, unwrapped
    line - anything past COLS characters just got silently cut off by
    send_screen()'s own truncation, since Notes (unlike Text Browser's
    sources) was never word-wrapped. Now each note is numbered (newest
    first, matching the order notes_list() already returns) and run
    through word_wrap() same as everything else, so a long note spans
    as many lines as it needs instead of losing its tail. The one
    trade-off: the old header showed a note-index range per page
    ('NOTES 1-4 OF 9:'), which doesn't translate cleanly once one note
    can span multiple lines - replaced with a simple note count."""
    send_screen([
        'VIEW NOTES',
        'HOW MANY? (1-9)',
        'PAGES OF 4 - ENTER FOR MORE.',
        '',
        '',
        '0=BACK',
    ])
    choice = read_line()
    if choice == '0' or choice == '':
        return

    if len(choice) != 1 or choice not in '123456789':
        send_screen(['VIEW NOTES', 'ENTER A SINGLE DIGIT, 1-9.',
                      '', '', '', '0=CONTINUE'])
        read_line()
        return

    n = int(choice)
    ok, result = notes_list(n)
    if not ok:
        send_screen(['VIEW NOTES', 'COULD NOT LOAD NOTES:',
                      result[:COLS], '', '', '0=CONTINUE'])
        read_line()
        return

    lines = []
    for i, note_text in enumerate(result, 1):
        lines.extend(word_wrap('{}. {}'.format(i, note_text), COLS))
        lines.append('')
    while lines and lines[-1] == '':
        lines.pop()

    show_pages('NOTES ({})'.format(len(result)), lines)


def notes_menu_loop():
    _notes_intro_screen()
    while True:
        line = read_line()
        if line == '0' or line == '':
            return
        if line.upper() == 'V':
            notes_view_flow()
            _notes_intro_screen()
            continue
        ok, msg = notes_send(line)
        send_screen([
            'NOTES',
            'SAVED.' if ok else 'NOT SAVED:',
            line[:COLS],
            '' if ok else msg,
            'V=VIEW   ENTER=ANOTHER NOTE',
            '0=BACK',
        ])


# ---------------------------------------------------------------------
# HOME CONTROL module
#
# Outbound only: Tandy -> Alexa, never the reverse. Each entry below is
# a Voice Monkey "Routine Trigger" device that an Alexa Routine is
# wired to (Voice Monkey only fires the trigger - the Routine itself,
# set up in the Alexa app, does the actual work of turning the smart
# plug/switch on or off). No OAuth on the Pico: VOICEMONKEY_TOKEN in
# secrets.py is the only credential needed.
#
# API confirmed 2026-09-14 straight from voicemonkey.io/docs/api:
#   GET https://api-v3.voicemonkey.io/trigger?token=...&device=...
# Plain JSON response, no redirects to chase (unlike the Apps Script
# relay) - {"success": true, "data": "OK"} on success.
#
# Reworked 2026-09-14: the separate On/Off lights triggers were
# replaced with a single toggle, and the freed-up device slot went to
# a Pecron (portable power station) charge timer instead of Garage
# Door - garage is skipped for now. Voice Monkey's free plan caps at 3
# Routine Trigger devices total. Add more entries here (and wire up
# the matching Alexa Routine + Voice Monkey trigger) whenever a slot
# opens up - nothing else in this module needs to change.
# ---------------------------------------------------------------------

HOME_CONTROLS = [
    ('1', 'TOGGLE LIVING ROOM LIGHTS', 'toggle-living-room-lights-5qgbr'),
    ('2', 'CHARGE PECRON 60 MIN', 'charge-pecron-for-60-minutes-yyxnk'),
]


def voicemonkey_trigger(device_id):
    """Fires one Voice Monkey Routine Trigger device. Returns (ok, message)."""
    if not getattr(secrets, 'VOICEMONKEY_TOKEN', ''):
        return False, 'VOICEMONKEY_TOKEN NOT SET IN secrets.py'

    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'

    url = 'https://api-v3.voicemonkey.io/trigger?token={}&device={}'.format(
        url_quote(secrets.VOICEMONKEY_TOKEN),
        url_quote(device_id),
    )
    try:
        status, body = http_get(url)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)

    if status == 200 and '"success":true' in body.replace(' ', ''):
        return True, 'OK'
    return False, 'SERVER SAID: {}'.format(body.strip()[:COLS])


def home_control_menu():
    lines = ['HOME CONTROL - ENTER A NUMBER:']
    for key, label, _ in HOME_CONTROLS:
        lines.append(' {}  {}'.format(key, label))
    while len(lines) < ROWS - 1:
        lines.append('')
    lines.append('0=BACK')
    send_screen(lines)


def home_control_menu_loop():
    home_control_menu()
    while True:
        choice = read_line()
        if choice == '0' or choice == '':
            return

        match = None
        for key, label, device_id in HOME_CONTROLS:
            if choice == key:
                match = (label, device_id)
                break

        if match is None:
            home_control_menu()
            continue

        label, device_id = match
        ok, msg = voicemonkey_trigger(device_id)
        send_screen([
            'HOME CONTROL',
            label,
            'SENT.' if ok else 'FAILED:',
            '' if ok else msg,
            'ENTER A NUMBER FOR ANOTHER',
            '0=BACK',
        ])


# ---------------------------------------------------------------------
# TEXT BROWSER module
#
# Live content over WiFi, not canned text - four independent sources,
# all free and keyless, all reusing the same http_get()/ascii_safe()/
# show_pages() building blocks proven first for Notes and the original
# Wikipedia lookup:
#   1. WIKIPEDIA LOOKUP - type a topic, get its summary (the original
#      Text Browser feature, unchanged in behavior - see wiki_lookup()).
#   2. HEADLINES - latest BBC News RSS titles. RSS is XML, not JSON, so
#      this hand-rolls just enough tag-scanning to pull out <title>
#      text - no XML library needed for something this regular.
#   3. ON THIS DAY - Wikipedia's "on this day" feed for today's real
#      date (needs an NTP time sync - see _sync_clock() - since the
#      Pico's RTC has no idea what day it actually is otherwise). The
#      raw feed bundles a full Wikipedia page object (thumbnail, links,
#      extract, etc.) for every related article on top of each event -
#      far more data than a 40-col screen can use, and more than's
#      worth risking the Pico's RAM on parsing in full. So this scans
#      the JSON by hand for just each event's (year, text) pair instead
#      of a full json.loads() - see _onthisday_events() below.
#   4. QUOTE OF THE DAY - a single short quote, smallest of the four.
# ---------------------------------------------------------------------

WIKI_USER_AGENT = 'Model102PicoTerminal/1.0 (personal hobby project)'
BBC_RSS_URL = 'https://feeds.bbci.co.uk/news/rss.xml'
ZENQUOTES_URL = 'https://zenquotes.io/api/today'

# FIX 2026-09-15: ON THIS DAY crashed with MemoryError on real hardware
# - its feed bundles full per-page metadata onto every event, making
# the true response far bigger than the few short lines it's actually
# used for. Capping the read (see http_get()'s max_bytes) keeps this
# module's worst-case memory bounded regardless of how big Wikipedia's
# response actually is on any given day. Picked conservatively; if
# events still come up short after this, it's safe to raise a bit.
ONTHISDAY_MAX_BYTES = 16384

# Precautionary only - see headlines_fetch(). Generous enough that it
# shouldn't ever actually trim a real BBC fetch under normal conditions.
HEADLINES_MAX_BYTES = 32768


def word_wrap(text, width):
    """Greedy word-wrap of a block of prose to `width` columns. Any
    single word longer than width gets hard-broken so it can't blow
    past the screen width."""
    lines = []
    line = ''
    for word in text.split(' '):
        while len(word) > width:
            if line:
                lines.append(line)
                line = ''
            lines.append(word[:width])
            word = word[width:]
        if not word:
            continue
        candidate = (line + ' ' + word) if line else word
        if len(candidate) <= width:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines if lines else ['']


def wiki_lookup(topic):
    """Fetches the Wikipedia REST summary for `topic`. Returns
    (True, title, lines) on success - lines already word-wrapped to
    COLS - or (False, None, message) on failure."""
    wlan = connect_wifi()
    if wlan is None:
        return False, None, 'NO WIFI'

    slug = url_quote(topic.strip().replace(' ', '_'))
    url = 'https://en.wikipedia.org/api/rest_v1/page/summary/' + slug
    try:
        status, body = http_get(url, user_agent=WIKI_USER_AGENT)
    except Exception as e:
        return False, None, 'NETWORK ERROR: {}'.format(e)

    if status == 404:
        return False, None, 'NOT FOUND. TRY ANOTHER TOPIC.'
    if status != 200:
        return False, None, 'SERVER STATUS {}'.format(status)

    try:
        data = json.loads(body)
    except Exception:
        return False, None, 'BAD RESPONSE FROM SERVER'

    title = ascii_safe(data.get('title', topic))
    extract = data.get('extract', '')
    if not extract:
        return False, None, 'NO SUMMARY AVAILABLE.'

    extract = ascii_safe(extract).replace('\n', ' ')
    lines = word_wrap(extract, COLS)
    return True, title, lines


def wiki_lookup_flow():
    """Prompts for a topic, looks it up, and pages through the summary
    via show_pages(), with the article title as the fixed header."""
    send_screen([
        'WIKIPEDIA LOOKUP',
        'TYPE A TOPIC AND PRESS ENTER.',
        '',
        '',
        '',
        '0=BACK',
    ])
    topic = read_line()
    if topic == '0' or topic == '':
        return

    send_screen(['WIKIPEDIA LOOKUP', 'SEARCHING...', topic[:COLS],
                  '', '', ''])
    ok, title, result = wiki_lookup(topic)
    if not ok:
        send_screen(['WIKIPEDIA LOOKUP', 'COULD NOT LOAD:',
                      result[:COLS], '', '', '0=CONTINUE'])
        read_line()
        return

    show_pages(title, result)


# --- HEADLINES (BBC News RSS) ---

def _xml_tag_text(s, tag):
    """Pulls the text content of the first <tag>...</tag> in `s`,
    unwrapping a <![CDATA[ ... ]]> block if the feed uses one (BBC's
    does). Not a general XML parser - just enough for one well-formed
    RSS 2.0 feed's flat <title>/<description> tags."""
    open_at = s.find('<' + tag)
    if open_at == -1:
        return ''
    open_at = s.find('>', open_at)
    if open_at == -1:
        return ''
    start = open_at + 1
    end = s.find('</' + tag + '>', start)
    if end == -1:
        return ''
    text = s[start:end].strip()
    if text.startswith('<![CDATA['):
        text = text[len('<![CDATA['):]
        cdata_end = text.find(']]>')
        if cdata_end != -1:
            text = text[:cdata_end]
        text = text.strip()
    return text


def _html_unescape(s):
    """Just the handful of entities that actually turn up in RSS titles
    - not a general HTML decoder."""
    for a, b in (('&amp;', '&'), ('&quot;', '"'), ('&apos;', "'"),
                 ('&#39;', "'"), ('&lt;', '<'), ('&gt;', '>')):
        s = s.replace(a, b)
    return s


def rss_titles(body, max_items=8):
    """Pulls just the <title> text out of the first max_items <item>
    blocks of an RSS 2.0 feed, by hand-scanning for the tags rather
    than pulling in a real XML parser for something this regular."""
    items = []
    pos = body.find('<item')
    while pos != -1 and len(items) < max_items:
        item_end = body.find('</item>', pos)
        if item_end == -1:
            break
        title = _xml_tag_text(body[pos:item_end], 'title')
        if title:
            items.append(_html_unescape(title))
        pos = body.find('<item', item_end)
    return items


def headlines_fetch():
    """Fetches and parses the BBC News RSS feed. Returns (True, lines)
    - one word-wrapped headline per block, blank line between - or
    (False, message) on failure."""
    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'

    try:
        # Same defensive cap as ON THIS DAY (see ONTHISDAY_MAX_BYTES) -
        # rss_titles() only keeps the first 8 headlines anyway, and
        # those are always the earliest ones in the feed, so capping
        # the read can't drop a headline this would've shown. Sized
        # generously since BBC's feed hasn't shown the same bloat
        # onthisday's did - this is precautionary, not a fix for an
        # observed problem here.
        status, body = http_get(BBC_RSS_URL, timeout_s=15, max_bytes=HEADLINES_MAX_BYTES)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)

    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)

    titles = rss_titles(body)
    if not titles:
        return False, 'NO HEADLINES FOUND'

    lines = []
    for title in titles:
        lines.extend(word_wrap(ascii_safe(title), COLS))
        lines.append('')
    while lines and lines[-1] == '':
        lines.pop()
    return True, lines


def headlines_flow():
    send_screen(['HEADLINES', 'FETCHING BBC NEWS...', '', '', '', ''])
    ok, result = headlines_fetch()
    if not ok:
        send_screen(['HEADLINES', 'COULD NOT LOAD:', result[:COLS],
                      '', '', '0=CONTINUE'])
        read_line()
        return
    show_pages('BBC NEWS HEADLINES', result)


# --- ON THIS DAY (Wikipedia) ---

def _find_matching_bracket(s, open_idx):
    """Given s[open_idx] is '{' or '[', returns the index of its
    matching close, correctly skipping over anything inside quoted
    strings (respecting \\" escapes) so a brace/bracket character
    inside some event's text can't throw off the count. Returns -1 if
    the input is malformed enough that no match is found."""
    depth = 0
    in_str = False
    esc = False
    i = open_idx
    n = len(s)
    while i < n:
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in '{[':
                depth += 1
            elif c in '}]':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _json_str_after(s, marker):
    """Finds `marker` (e.g. '"text":"') in s and reads the JSON string
    literal starting right after it, unescaping as it goes. Returns ''
    if the marker isn't found."""
    idx = s.find(marker)
    if idx == -1:
        return ''
    i = idx + len(marker)
    n = len(s)
    out = []
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            nxt = s[i + 1]
            out.append(' ' if nxt in ('n', 't', 'r') else nxt)
            i += 2
            continue
        if c == '"':
            break
        out.append(c)
        i += 1
    return ''.join(out)


def _json_int_after(s, marker):
    """Finds `marker` (e.g. '"year":') in s and reads the (possibly
    negative) integer right after it. Returns None if not found."""
    idx = s.find(marker)
    if idx == -1:
        return None
    i = idx + len(marker)
    n = len(s)
    neg = i < n and s[i] == '-'
    if neg:
        i += 1
    digits_start = i
    while i < n and s[i].isdigit():
        i += 1
    if i == digits_start:
        return None
    val = int(s[digits_start:i])
    return -val if neg else val


def _onthisday_events(body):
    """Manually scans the raw JSON body of Wikipedia's onthisday
    'selected' feed for each top-level event's (year, text) pair,
    skipping the bulky per-page metadata entirely - see the module
    header comment for why this avoids a full json.loads()."""
    marker = '"selected":['
    idx = body.find(marker)
    if idx == -1:
        return []
    arr_open = idx + len(marker) - 1
    arr_close = _find_matching_bracket(body, arr_open)
    if arr_close == -1:
        arr_close = len(body)

    events = []
    j = arr_open + 1
    while j < arr_close:
        while j < arr_close and body[j] in ' \t\r\n,':
            j += 1
        if j >= arr_close or body[j] != '{':
            break
        end = _find_matching_bracket(body, j)
        if end == -1:
            break
        ev = body[j:end + 1]
        text = _json_str_after(ev, '"text":"')
        year = _json_int_after(ev, '"year":')
        if text:
            events.append((year, text))
        j = end + 1
    return events


def onthisday_fetch():
    """Returns (True, lines) - word-wrapped 'YEAR: EVENT' entries,
    blank line between - or (False, message) on failure."""
    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'

    _sync_clock()
    now = time.localtime()
    month, day = now[1], now[2]
    url = 'https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{:02d}/{:02d}'.format(month, day)
    try:
        # ONTHISDAY_MAX_BYTES: this feed bundles full Wikipedia page
        # metadata (thumbnails, links, full extracts) for every related
        # article on top of each event's own one-line text - the real
        # payload turned out to be big enough to crash with a
        # MemoryError before this cap was added (2026-09-15). Capping
        # the read means only a truncated prefix of the feed ever gets
        # buffered; _onthisday_events() already stops cleanly at the
        # first incomplete event rather than choking on the cut-off
        # tail, so this trades "every event Wikipedia curated for
        # today" for "as many as fit in a safe amount of RAM" - plenty
        # for a screen that only shows a few lines at a time anyway.
        status, body = http_get(url, timeout_s=20, user_agent=WIKI_USER_AGENT,
                                 max_bytes=ONTHISDAY_MAX_BYTES)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)

    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)

    events = _onthisday_events(body)
    if not events:
        return False, 'NO EVENTS FOUND'

    lines = []
    for year, text in events:
        entry = ascii_safe(text)
        entry = '{}: {}'.format(year, entry) if year else entry
        lines.extend(word_wrap(entry, COLS))
        lines.append('')
    while lines and lines[-1] == '':
        lines.pop()
    return True, lines


def on_this_day_flow():
    send_screen(['ON THIS DAY', 'FETCHING...', '', '', '', ''])
    ok, result = onthisday_fetch()
    if not ok:
        send_screen(['ON THIS DAY', 'COULD NOT LOAD:', result[:COLS],
                      '', '', '0=CONTINUE'])
        read_line()
        return
    show_pages('ON THIS DAY', result)


# --- QUOTE OF THE DAY (ZenQuotes) ---

def quote_of_day_fetch():
    """Returns (True, lines) - the quote word-wrapped, blank line,
    then '- AUTHOR' - or (False, message) on failure."""
    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'

    try:
        status, body = http_get(ZENQUOTES_URL, timeout_s=10)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)

    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)

    try:
        data = json.loads(body)
    except Exception:
        return False, 'BAD RESPONSE FROM SERVER'

    if not data:
        return False, 'NO QUOTE AVAILABLE'

    q = data[0]
    quote = ascii_safe(q.get('q', ''))
    author = ascii_safe(q.get('a', 'UNKNOWN'))
    if not quote:
        return False, 'NO QUOTE AVAILABLE'

    lines = word_wrap(quote, COLS)
    lines.append('')
    lines.append('- ' + author)
    return True, lines


def quote_of_day_flow():
    send_screen(['QUOTE OF THE DAY', 'FETCHING...', '', '', '', ''])
    ok, result = quote_of_day_fetch()
    if not ok:
        send_screen(['QUOTE OF THE DAY', 'COULD NOT LOAD:',
                      result[:COLS], '', '', '0=CONTINUE'])
        read_line()
        return
    show_pages('QUOTE OF THE DAY', result)


# --- Text Browser submenu ---

def text_browser_menu():
    send_screen([
        'TEXT BROWSER - ENTER A NUMBER:',
        ' 1  WIKIPEDIA LOOKUP',
        ' 2  HEADLINES (BBC NEWS)',
        ' 3  ON THIS DAY',
        ' 4  QUOTE OF THE DAY',
        '0=BACK',
    ])


def text_browser_menu_loop():
    text_browser_menu()
    while True:
        choice = read_line()
        if choice == '0' or choice == '':
            return
        if choice == '1':
            wiki_lookup_flow()
        elif choice == '2':
            headlines_flow()
        elif choice == '3':
            on_this_day_flow()
        elif choice == '4':
            quote_of_day_flow()
        text_browser_menu()


# ---------------------------------------------------------------------
# GAMES module
#
# All three offline (no WiFi needed) - unlike Text Browser/Home Control,
# these just need `random`, which MicroPython provides but with a
# smaller surface than desktop Python: only getrandbits/seed/randrange/
# randint/choice/random/uniform are reliably present. Notably NO
# random.shuffle() - designs below use random.randint()/random.choice()
# with plain rejection-sampling loops instead, since the state spaces
# here are tiny (a handful of picks out of 20 rooms, or one word out of
# a few dozen) so a few retries are effectively free.
# ---------------------------------------------------------------------

# --- HUNT THE WUMPUS ---------------------------------------------------
#
# Classic 20-room dodecahedron cave: every room connects to exactly 3
# others, and the graph is symmetric (if A lists B, B lists A) - verified
# by hand before this was wired in. One simplification from the classic
# game, called out here and to the user: shooting only targets a single
# ADJACENT room (no multi-room crooked-arrow paths) - much easier to
# reason about on a numeric keypad-style prompt with no path-drawing UI.
CAVE_MAP = {
    1: (2, 5, 8), 2: (1, 3, 10), 3: (2, 4, 12), 4: (3, 5, 14), 5: (1, 4, 6),
    6: (5, 7, 15), 7: (6, 8, 17), 8: (1, 7, 9), 9: (8, 10, 18), 10: (2, 9, 11),
    11: (10, 12, 19), 12: (3, 11, 13), 13: (12, 14, 20), 14: (4, 13, 15),
    15: (6, 14, 16), 16: (15, 17, 20), 17: (7, 16, 18), 18: (9, 17, 19),
    19: (11, 18, 20), 20: (13, 16, 19),
}

WUMPUS_START_ARROWS = 5
WUMPUS_BAT_CHAIN_CAP = 5  # safety valve against a pathological bat-snatch chain


def _wumpus_pick_rooms(n, exclude):
    """Pick `n` distinct rooms (1-20), none of them in `exclude` and no
    two the same as each other. Plain rejection sampling instead of
    random.shuffle() - fine at this scale (5 picks out of 20 rooms)."""
    picked = []
    while len(picked) < n:
        r = random.randint(1, 20)
        if r in exclude or r in picked:
            continue
        picked.append(r)
    return picked


def wumpus_new_state():
    start = random.randint(1, 20)
    wumpus, pit_a, pit_b, bat_a, bat_b = _wumpus_pick_rooms(5, {start})
    return {
        'room': start,
        'arrows': WUMPUS_START_ARROWS,
        'wumpus': wumpus,
        'pits': (pit_a, pit_b),
        'bats': (bat_a, bat_b),
    }


def wumpus_resolve_room(state, room):
    """Move the player into `room`, resolving pits/wumpus/bats - including
    a chained bat-snatch if the bats drop them in another hazard room.
    Returns ('lose', message) or ('continue', None)."""
    for _ in range(WUMPUS_BAT_CHAIN_CAP):
        state['room'] = room
        if room == state['wumpus']:
            return 'lose', 'THE WUMPUS GOT YOU!'
        if room in state['pits']:
            return 'lose', 'YOU FELL INTO A PIT!'
        if room in state['bats']:
            room = random.randint(1, 20)
            continue
        return 'continue', None
    return 'continue', None


def wumpus_room_screen(state):
    room = state['room']
    neighbors = CAVE_MAP[room]
    sensed = []
    if state['wumpus'] in neighbors:
        sensed.append('WUMPUS')
    if state['pits'][0] in neighbors or state['pits'][1] in neighbors:
        sensed.append('PIT')
    if state['bats'][0] in neighbors or state['bats'][1] in neighbors:
        sensed.append('BATS')
    sense_line = 'YOU SENSE: ' + ', '.join(sensed) if sensed else ''
    send_screen([
        'ROOM {} ARROWS {}'.format(room, state['arrows']),
        sense_line,
        'TUNNELS: {}, {}, {}'.format(*neighbors),
        '',
        '',
        'M=MOVE S=SHOOT 0=QUIT',
    ])


def _wumpus_ask_room(prompt, neighbors):
    send_screen([
        prompt,
        'TUNNELS: {}, {}, {}'.format(*neighbors),
        '', '', '',
        'ENTER ROOM #   0=CANCEL',
    ])
    choice = read_line()
    if choice == '0' or choice == '':
        return None
    try:
        room = int(choice)
    except ValueError:
        return None
    if room not in neighbors:
        return None
    return room


def wumpus_move_flow(state):
    room = _wumpus_ask_room('MOVE TO WHICH ROOM?', CAVE_MAP[state['room']])
    if room is None:
        return None
    return wumpus_resolve_room(state, room)


def wumpus_shoot_flow(state):
    room = _wumpus_ask_room('SHOOT INTO WHICH ROOM?', CAVE_MAP[state['room']])
    if room is None:
        return None
    state['arrows'] -= 1
    if room == state['wumpus']:
        return 'win', 'YOU KILLED THE WUMPUS!'
    if state['arrows'] <= 0:
        return 'lose', 'OUT OF ARROWS. YOU STARVE.'
    # Missed - a disturbed wumpus has a 50% chance to wander to an
    # adjacent room, which might be straight into the player.
    if random.randint(0, 1) == 0:
        state['wumpus'] = random.choice(CAVE_MAP[state['wumpus']])
        if state['wumpus'] == state['room']:
            return 'lose', 'THE WUMPUS FOUND YOU!'
    return 'continue', 'MISSED.'


def _wumpus_pause(msg):
    send_screen(['HUNT THE WUMPUS', msg, '', '', '', '0=CONTINUE'])
    read_line()


def wumpus_game_loop():
    state = wumpus_new_state()
    send_screen([
        'HUNT THE WUMPUS',
        '20 ROOMS, 1 WUMPUS,',
        '2 PITS, 2 BATS.',
        '5 ARROWS. GOOD LUCK.',
        '',
        '0=CONTINUE',
    ])
    read_line()
    while True:
        wumpus_room_screen(state)
        choice = read_line().upper()
        if choice == '0' or choice == '':
            return
        if choice == 'M':
            result = wumpus_move_flow(state)
        elif choice == 'S':
            result = wumpus_shoot_flow(state)
        else:
            continue
        if result is None:
            continue
        status, msg = result
        if status in ('win', 'lose'):
            _wumpus_pause(msg)
            return
        if msg:
            _wumpus_pause(msg)


# --- HANGMAN ------------------------------------------------------------
#
# No network needed - a small built-in word list. The physical screen is
# too small for gallows ASCII art, so misses are just tracked as a
# count/limit instead - a deliberate simplification for a 6-row display.
HANGMAN_WORDS = [
    'PYTHON', 'RADIO', 'SIGNAL', 'CIRCUIT', 'BATTERY', 'ANTENNA', 'MORSE',
    'STATIC', 'VOLTAGE', 'CURRENT', 'RESISTOR', 'CAPACITOR', 'TRANSISTOR',
    'MODEM', 'KEYBOARD', 'MONITOR', 'PRINTER', 'DISKETTE', 'TERMINAL',
    'CURSOR', 'MEMORY', 'PROCESSOR', 'NETWORK', 'WIRELESS', 'FREQUENCY',
    'AMPLIFIER', 'OSCILLATOR', 'SOLDER', 'BREADBOARD', 'MICROPHONE',
]

HANGMAN_MAX_MISSES = 6


def hangman_new_state():
    return {'word': random.choice(HANGMAN_WORDS), 'guessed': set(), 'wrong': 0}


def hangman_screen(state):
    word = state['word']
    masked = ' '.join(ch if ch in state['guessed'] else '_' for ch in word)
    guessed_list = ','.join(sorted(state['guessed'])) if state['guessed'] else '(NONE)'
    send_screen([
        'HANGMAN   MISSES {}/{}'.format(state['wrong'], HANGMAN_MAX_MISSES),
        masked[:COLS],
        'GUESSED: {}'.format(guessed_list)[:COLS],
        '',
        '',
        'ENTER A LETTER   0=QUIT',
    ])


def hangman_game_loop():
    state = hangman_new_state()
    while True:
        hangman_screen(state)
        choice = read_line().upper()
        if choice == '0' or choice == '':
            return
        letter = choice[:1]
        if len(choice) != 1 or not letter.isalpha():
            continue
        if letter in state['guessed']:
            continue
        state['guessed'].add(letter)
        if letter not in state['word']:
            state['wrong'] += 1
        if all(ch in state['guessed'] for ch in state['word']):
            send_screen(['HANGMAN', 'YOU GOT IT!', state['word'], '', '', '0=CONTINUE'])
            read_line()
            return
        if state['wrong'] >= HANGMAN_MAX_MISSES:
            send_screen(['HANGMAN', 'OUT OF GUESSES.', 'THE WORD WAS:',
                          state['word'], '', '0=CONTINUE'])
            read_line()
            return


# --- WORDLE CLONE ---------------------------------------------------------
#
# Same no-network approach as Hangman - a curated list of 5-letter words,
# used both as the answer pool and as accepted guesses (no separate
# "is this a real word" dictionary check - a deliberate simplification).
# send_screen() has no clear/cursor addressing (see the comment on COLS
# above), so it just appends a fresh block each turn and lets the M102
# scroll - which works out nicely here as a free, no-effort guess history.
WORDLE_WORDS = [
    'CRANE', 'SLATE', 'TRACE', 'STARE', 'ADIEU', 'ROAST', 'LIGHT', 'PLANT',
    'GRAPE', 'STONE', 'FLAME', 'BRAVE', 'CHESS', 'PIANO', 'RIVER', 'OCEAN',
    'TIGER', 'MOUSE', 'HOUSE', 'CLOUD', 'STORM', 'BREAD', 'CANDY', 'LEMON',
    'MANGO', 'TOAST', 'FRUIT', 'SNAKE', 'HORSE', 'EAGLE', 'WATCH', 'CHAIR',
    'TABLE', 'PHONE', 'MUSIC', 'DANCE', 'SMILE', 'HEART', 'BRAIN', 'GHOST',
    'ROBOT', 'PLANE', 'TRAIN', 'TRUCK', 'SHARK', 'WHALE', 'ZEBRA', 'PANDA',
    'KOALA',
]

WORDLE_MAX_GUESSES = 6


def _wordle_score(guess, target):
    """Standard two-pass Wordle scoring (handles repeated letters
    correctly): '^' = right letter, right spot; '~' = right letter,
    wrong spot; '.' = not in the word."""
    result = ['.'] * 5
    pool = list(target)
    for i in range(5):
        if guess[i] == target[i]:
            result[i] = '^'
            pool[i] = None
    for i in range(5):
        if result[i] == '^':
            continue
        ch = guess[i]
        if ch in pool:
            result[i] = '~'
            pool[pool.index(ch)] = None
    return ''.join(result)


def wordle_game_loop():
    target = random.choice(WORDLE_WORDS)
    guesses_made = 0
    send_screen([
        'WORDLE',
        'GUESS THE 5-LETTER WORD.',
        '^=RIGHT SPOT ~=WRONG SPOT',
        '.=NOT IN WORD',
        '{} GUESSES - TYPE ONE'.format(WORDLE_MAX_GUESSES),
        '0=QUIT',
    ])
    while guesses_made < WORDLE_MAX_GUESSES:
        guess = read_line().upper()
        if guess == '0':
            return
        if len(guess) != 5 or not guess.isalpha():
            send_screen([
                'WORDLE', 'ENTER EXACTLY 5 LETTERS.', '', '',
                '{} GUESSES LEFT'.format(WORDLE_MAX_GUESSES - guesses_made),
                '0=QUIT',
            ])
            continue
        guesses_made += 1
        score = _wordle_score(guess, target)
        if guess == target:
            send_screen(['WORDLE', guess, score, 'YOU GOT IT!', '', '0=CONTINUE'])
            read_line()
            return
        remaining = WORDLE_MAX_GUESSES - guesses_made
        if remaining <= 0:
            send_screen(['WORDLE', guess, score, 'OUT OF GUESSES.',
                          'WORD WAS: {}'.format(target), '0=CONTINUE'])
            read_line()
            return
        send_screen(['WORDLE', guess, score, '',
                      '{} GUESSES LEFT'.format(remaining),
                      'TYPE YOUR NEXT GUESS'])


# --- SCREENSAVER ---------------------------------------------------------
#
# Just a fun, no-point demo of the display - continuously streams
# decorative lines instead of sitting on a static menu. Every other
# module is built around read_line()'s blocking wait for a full CR-
# terminated line; this one deliberately isn't, since a screensaver
# needs to run forever until SOME key breaks it, not wait for Enter.
#
# The interrupt problem the user asked about turns out to already be
# solved by how read_line() works: TELCOM sends each keystroke over the
# wire the instant it's typed (read_line() relies on this too - it's
# what lets it assemble bytes into a line itself, byte by byte, rather
# than waiting for the M102 to deliver one pre-packaged line). That
# means a single non-blocking `uart.any()` check between lines is
# enough to notice ANY keypress at all, with no need to wait for Enter.
def _screensaver_key_pressed():
    """Non-blocking. True if the user has typed anything since the
    last check - draining whatever's sitting in the UART buffer so the
    keystroke that broke out of the loop doesn't leak into the next
    screen's read_line() prompt."""
    if not uart.any():
        return False
    while uart.any():
        uart.read(1)
    return True


def _screensaver_bounce_line(state):
    """A single '*' bouncing back and forth across the line width -
    each call advances it one step and returns that frame as a line.
    Since there's no cursor addressing, this can't redraw in place;
    printed as a continuous stream of lines it reads as a scrolling
    diagonal bounce instead, which is close enough to the real thing."""
    pos = state['pos']
    line = [' '] * COLS
    line[pos] = '*'
    new_pos = pos + state['dir']
    if new_pos <= 0 or new_pos >= COLS - 1:
        state['dir'] = -state['dir']
        new_pos = max(0, min(COLS - 1, new_pos))
    state['pos'] = new_pos
    return ''.join(line)


def _screensaver_rain_line(state):
    """A handful of random 0/1 'digital rain' characters scattered
    across an otherwise blank line - no state needed, every line is
    independent."""
    line = [' '] * COLS
    for _ in range(3):
        line[random.randint(0, COLS - 1)] = random.choice('01')
    return ''.join(line)


SCREENSAVER_BANNER = 'TANDY 102 * PICO W TERMINAL DEMO * '


def _screensaver_marquee_line(state):
    """A fixed banner scrolling sideways one character per line, BBS-
    marquee style. `SCREENSAVER_BANNER` is repeated 3x so a COLS-wide
    slice starting anywhere in the first copy never runs off the end."""
    text = SCREENSAVER_BANNER * 3
    offset = state['pos'] % len(SCREENSAVER_BANNER)
    state['pos'] += 1
    return text[offset:offset + COLS]


# (line-generator, initial-state-factory) pairs - a factory instead of
# a shared dict so every trip back around to a scene starts the same
# way instead of picking up wherever the last pass left off.
SCREENSAVER_SCENES = [
    (_screensaver_bounce_line, lambda: {'pos': 0, 'dir': 1}),
    (_screensaver_rain_line, lambda: {}),
    (_screensaver_marquee_line, lambda: {'pos': 0}),
]

SCREENSAVER_LINES_PER_SCENE = 20  # ~10s per scene at 500ms/line


def screensaver_flow():
    send_screen([
        'SCREENSAVER',
        'JUST FOR FUN - WATCHING THE',
        'DISPLAY DO ITS THING.',
        '',
        '',
        'PRESS ANY KEY TO STOP',
    ])

    scene_index = 0
    line_count = 0
    fn, make_state = SCREENSAVER_SCENES[scene_index]
    state = make_state()
    while True:
        line = fn(state)
        uart.write(ascii_safe(line[:COLS]).encode('ascii'))
        uart.write(b'\r\n')
        time.sleep_ms(LINE_DELAY_MS)

        if _screensaver_key_pressed():
            return

        line_count += 1
        if line_count >= SCREENSAVER_LINES_PER_SCENE:
            line_count = 0
            scene_index = (scene_index + 1) % len(SCREENSAVER_SCENES)
            fn, make_state = SCREENSAVER_SCENES[scene_index]
            state = make_state()


def games_menu():
    send_screen([
        'GAMES & FUN - ENTER A NUMBER:',
        ' 1  HUNT THE WUMPUS',
        ' 2  HANGMAN',
        ' 3  WORDLE',
        ' 4  SCREENSAVER',
        '0=BACK',
    ])


def games_menu_loop():
    games_menu()
    while True:
        choice = read_line()
        if choice == '0' or choice == '':
            return
        if choice == '1':
            wumpus_game_loop()
        elif choice == '2':
            hangman_game_loop()
        elif choice == '3':
            wordle_game_loop()
        elif choice == '4':
            screensaver_flow()
        games_menu()


# ---------------------------------------------------------------------
# LOAD PROGRAM module
#
# Added 2026-09-16. Design settled first (see the build log), but only
# the LIBRARY SYNC half is built so far - getting .bas program listings
# from a `programs/` folder in the same public GitHub repo used for
# SOFTWARE UPDATE (see that section above) onto the Pico's own flash,
# entirely over WiFi. This is deliberately the SAME mechanism as OTA,
# just applied to a folder of files instead of one: GitHub's Contents
# API can list a directory too, not just a single file, so the only
# real difference is that this response is a JSON ARRAY (one object per
# file) instead of one object - handled with the same hand-scanning
# approach as onthisday_fetch()'s event array (_find_matching_bracket(),
# _json_str_after()), pulling just each entry's "name" and "sha".
#
# The actual XMODEM-SEND-TO-THE-M100 half (picking a synced program and
# streaming it to TELCOM) is NOT built yet - see the build log for that
# design. This module only gets programs onto the Pico's own flash and
# lets you confirm what's there; sending them onward is next.
#
# A small local manifest (PROGRAMS_MANIFEST_FILE, name<TAB>sha per line)
# tracks which GitHub sha was last downloaded for each file, so a sync
# only re-downloads something that's actually new or changed - same
# "did this change" idea as OTA's local version file, just per-file.
# ---------------------------------------------------------------------

OTA_GH_PROGRAMS_DIR = 'programs'   # folder in the GitHub repo
PROGRAMS_LOCAL_DIR = 'programs'    # folder on the Pico's own flash
PROGRAMS_MANIFEST_FILE = 'programs_manifest.txt'
PROGRAMS_MAX_BYTES = 50000       # generous for a BASIC listing
PROGRAMS_LIST_MAX_BYTES = 8192   # cap for the folder-listing JSON itself


def _programs_ensure_dir():
    try:
        os.mkdir(PROGRAMS_LOCAL_DIR)
    except OSError:
        pass  # already exists


def _programs_read_manifest():
    manifest = {}
    try:
        with open(PROGRAMS_MANIFEST_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or '\t' not in line:
                    continue
                name, sha = line.split('\t', 1)
                manifest[name] = sha
    except OSError:
        pass
    return manifest


def _programs_write_manifest(manifest):
    with open(PROGRAMS_MANIFEST_FILE, 'w') as f:
        for name, sha in manifest.items():
            f.write('{}\t{}\n'.format(name, sha))


def _programs_list_remote():
    """Lists the programs/ folder on GitHub via the Contents API - a
    JSON ARRAY of file objects, unlike OTA's single-file check. Scanned
    by hand the same way onthisday_fetch() scans its event array,
    pulling just "name" and "sha" out of each entry rather than a full
    json.loads(). Only .bas files are kept (case-insensitive), so a
    stray README or similar in that folder is ignored automatically.
    Returns (True, [(name, sha), ...]) or (False, message)."""
    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'
    url = 'https://api.github.com/repos/{}/{}/contents/{}?ref={}'.format(
        OTA_GH_OWNER, OTA_GH_REPO, OTA_GH_PROGRAMS_DIR, OTA_GH_BRANCH)
    try:
        status, body = http_get(url, user_agent=WIKI_USER_AGENT,
                                 max_bytes=PROGRAMS_LIST_MAX_BYTES)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)
    if status == 404:
        return False, 'NO programs/ FOLDER ON GITHUB YET'
    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)

    arr_open = body.find('[')
    if arr_open == -1:
        return False, 'BAD RESPONSE FROM GITHUB'
    arr_close = _find_matching_bracket(body, arr_open)
    if arr_close == -1:
        arr_close = len(body)

    entries = []
    j = arr_open + 1
    while j < arr_close:
        while j < arr_close and body[j] in ' \t\r\n,':
            j += 1
        if j >= arr_close or body[j] != '{':
            break
        end = _find_matching_bracket(body, j)
        if end == -1:
            break
        obj = body[j:end + 1]
        name = _json_str_after(obj, '"name":"')
        sha = _json_str_after(obj, '"sha":"')
        if name.lower().endswith('.bas') and sha:
            entries.append((name, sha))
        j = end + 1

    return True, entries


def _programs_fetch_file(name):
    """Downloads one program file's plain contents from
    raw.githubusercontent.com - same idea as OTA's _ota_fetch_program(),
    just parameterized by filename."""
    wlan = connect_wifi()
    if wlan is None:
        return False, 'NO WIFI'
    url = 'https://raw.githubusercontent.com/{}/{}/{}/{}/{}'.format(
        OTA_GH_OWNER, OTA_GH_REPO, OTA_GH_BRANCH, OTA_GH_PROGRAMS_DIR, name)
    try:
        status, body = http_get(url, user_agent=WIKI_USER_AGENT,
                                 max_bytes=PROGRAMS_MAX_BYTES)
    except Exception as e:
        return False, 'NETWORK ERROR: {}'.format(e)
    if status != 200:
        return False, 'SERVER STATUS {}'.format(status)
    return True, body


def programs_sync_flow():
    send_screen(['SYNC LIBRARY', 'CHECKING GITHUB...', '', '', '', ''])
    ok, remote = _programs_list_remote()
    if not ok:
        send_screen(['SYNC LIBRARY', 'COULD NOT CHECK:', remote[:COLS],
                      '', '', '0=CONTINUE'])
        read_line()
        return

    if not remote:
        send_screen(['SYNC LIBRARY', 'NO PROGRAMS FOUND',
                      'IN THE GITHUB REPO YET.', '', '', '0=CONTINUE'])
        read_line()
        return

    _programs_ensure_dir()
    manifest = _programs_read_manifest()

    added = 0
    updated = 0
    failed = 0
    for name, sha in remote:
        if manifest.get(name) == sha:
            continue
        is_new = name not in manifest
        send_screen(['SYNC LIBRARY', 'DOWNLOADING:', name[:COLS], '', '', ''])
        ok, body = _programs_fetch_file(name)
        if not ok:
            failed += 1
            continue
        if isinstance(body, str):
            body = body.encode('utf-8')
        try:
            with open('{}/{}'.format(PROGRAMS_LOCAL_DIR, name), 'wb') as f:
                f.write(body)
        except Exception:
            failed += 1
            continue
        manifest[name] = sha
        if is_new:
            added += 1
        else:
            updated += 1

    _programs_write_manifest(manifest)

    send_screen([
        'SYNC LIBRARY',
        'DONE.',
        'NEW: {}   UPDATED: {}'.format(added, updated)[:COLS],
        'FAILED: {}'.format(failed) if failed else '',
        '',
        '0=CONTINUE',
    ])
    read_line()


def programs_view_flow():
    """Lists whatever .bas files are actually present in the local
    programs/ folder right now - lets SYNC LIBRARY be checked without
    needing Thonny/USB at all."""
    _programs_ensure_dir()
    try:
        names = sorted(os.listdir(PROGRAMS_LOCAL_DIR))
    except OSError:
        names = []
    lines = [n for n in names if n.lower().endswith('.bas')]
    show_pages('INSTALLED PROGRAMS ({})'.format(len(lines)),
                lines if lines else ['(NONE YET - TRY SYNC LIBRARY)'])


def load_program_menu():
    send_screen([
        'LOAD PROGRAM - ENTER A NUMBER:',
        ' 1  SYNC LIBRARY (WIFI)',
        ' 2  VIEW INSTALLED PROGRAMS',
        '',
        '',
        '0=BACK',
    ])


def load_program_menu_loop():
    load_program_menu()
    while True:
        choice = read_line()
        if choice == '0' or choice == '':
            return
        if choice == '1':
            programs_sync_flow()
        elif choice == '2':
            programs_view_flow()
        load_program_menu()


# CHANGED 2026-09-16: dropped the standalone title row ('TANDY 102 -
# ENTER A NUMBER:') to make room for a 6th item (LOAD PROGRAM) within
# the confirmed ROWS=6 budget, rather than paginating the root menu.
# Same "every row has to earn its place" reasoning as the COLS 40->39
# and ROWS 8->6 changes above - a returning user doesn't need the
# reminder, and this is the one screen that gets redrawn constantly.
def home_menu():
    send_screen([
        ' 1  WIFI STATUS',
        ' 2  NOTES',
        ' 3  TEXT BROWSER',
        ' 4  HOME CONTROL',
        ' 5  GAMES & FUN STUFF',
        ' 6  LOAD PROGRAM',
    ])


def main():
    home_menu()
    while True:
        choice = read_line()
        if choice == '1':
            wifi_menu_loop()
            home_menu()
        elif choice == '2':
            notes_menu_loop()
            home_menu()
        elif choice == '3':
            text_browser_menu_loop()
            home_menu()
        elif choice == '4':
            home_control_menu_loop()
            home_menu()
        elif choice == '5':
            games_menu_loop()
            home_menu()
        elif choice == '6':
            load_program_menu_loop()
            home_menu()
        else:
            home_menu()


main()
