#!/usr/bin/env python3
"""grok-signup.py — Auto-register akun Grok (x.ai). Playwright + Camoufox + Multithreading Engine v4.0.

CLI:  python grok-signup.py [total] [concurrency]
GUI:  import + run_batch() + EventBus queue (no stdout hijack).
"""
import sys, time, re, json, os, tempfile, threading, queue, traceback
import imaplib  # OTP IMAP — must be top-level so PyInstaller bundles it
import ssl as ssl_mod
import email as email_lib
from email.header import decode_header
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from playwright.sync_api import sync_playwright
from camoufox.sync_api import Camoufox
import requests

IS_WIN = sys.platform == 'win32'

def app_root() -> Path:
    """Folder app (next to .exe when frozen; project root when source)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

ROOT = app_root()

def _setup_portable_camoufox():
    """Prefer bundled browser under ./camoufox-browser (portable pack)."""
    portable = ROOT / 'camoufox-browser'
    if not portable.exists():
        return
    try:
        import camoufox.pkgman as pm
        pm.INSTALL_DIR = portable
    except Exception:
        pass

_setup_portable_camoufox()

# ── Thread Locks ──────────────────────────────────────────────
log_lock = threading.Lock()
file_lock = threading.Lock()
router_lock = threading.Lock()

class _NullIO:
    """stdout/stderr sink when frozen GUI has no console (sys.stdout is None)."""
    def write(self, *a, **k): return 0
    def flush(self, *a, **k): pass
    def isatty(self): return False
    def reconfigure(self, *a, **k): pass

def _ensure_stdio():
    """Windowed .exe sets stdout/stderr to None — restore safe sinks."""
    if sys.stdout is None:
        sys.stdout = _NullIO()
    if sys.stderr is None:
        sys.stderr = _NullIO()

_ensure_stdio()

def safe_print(*args, **kwargs):
    with log_lock:
        try:
            out = sys.stdout
            if out is None:
                return
            print(*args, **kwargs)
            flush = getattr(out, 'flush', None)
            if callable(flush):
                flush()
        except Exception:
            pass

# ── DualLogger (file full; CLI/user via step_log + events) ──
class DualLogger:
    def __init__(self):
        self.log_dir = ROOT / 'logs'
        self.log_dir.mkdir(exist_ok=True)
        self._path = self.log_dir / f"engine-{time.strftime('%Y%m%d')}.log"
        self._lock = threading.Lock()

    def _write(self, level, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}\n"
        try:
            with self._lock:
                with open(self._path, 'a', encoding='utf-8', errors='replace') as f:
                    f.write(line)
        except Exception:
            pass

    def info(self, msg): self._write('INFO', msg)
    def debug(self, msg): self._write('DEBUG', msg)
    def error(self, msg): self._write('ERROR', msg)

LOGGER = DualLogger()

# ── EventBus (GUI subscribe; no-op if no queue) ───────────────
class EventBus:
    def __init__(self):
        self._q = None
        self._lock = threading.Lock()

    def set_queue(self, q):
        with self._lock:
            self._q = q

    def clear_queue(self):
        with self._lock:
            self._q = None

    def emit(self, event):
        if not isinstance(event, dict):
            return
        with self._lock:
            q = self._q
        if q is not None:
            try:
                q.put(event)
            except Exception:
                pass
        et = event.get('type', '?')
        if et != 'progress':
            LOGGER.debug(f"event {et}: {event}")

EVENTS = EventBus()

# ── StopController ────────────────────────────────────────────
class StopController:
    def __init__(self):
        self._ev = threading.Event()

    def request_stop(self):
        self._ev.set()
        LOGGER.info('stop requested')

    def reset(self):
        self._ev.clear()

    def is_stopped(self):
        return self._ev.is_set()

    def check(self):
        if self._ev.is_set():
            raise RuntimeError('Stopped by user')

STOP = StopController()

# Frontend noise patterns (defense-in-depth for any stray print path)
NOISE_RE = re.compile(
    r'(Call log:|waiting for get_by_|attempting click|forcing action|'
    r'locator resolved to|performing click action|scrolling into view|'
    r'Traceback \(most recent call last\)|File \".*\", line \d+|'
    r'playwright\._impl|TimeoutError:|Error: Locator\.click)',
    re.I,
)

def is_noise_line(text):
    if not text:
        return True
    return bool(NOISE_RE.search(text))

# ── Config (from .env next to app) ────────────────────────────
_env = {}
_envfile = ROOT / '.env'
if _envfile.exists():
    for line in _envfile.read_text(encoding='utf-8', errors='ignore').splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            _env[k.strip()] = v.strip()

def _env_or(key, default): return _env.get(key, default)

PASSWORD = _env_or('PASSWORD', 'change-me')
TS_DIR   = (ROOT / 'turnstilePatch').resolve()
OUT      = ROOT / 'sso.txt'
SIGNUP   = 'https://accounts.x.ai/sign-up?redirect=grok-com'
# mail provider: mailldez | imap  (imap = aaPanel / cPanel / any IMAP catch-all)
MAIL_PROVIDER = _env_or('MAIL_PROVIDER', 'mailldez').strip().lower()
MAILLDEZ = _env_or('MAILLDEZ_URL', 'https://your-mail-api.example')
DOMAINS  = [d.strip() for d in _env_or('MAILLDEZ_DOMAINS', 'example.com').split(',') if d.strip()]
IMAP_HOST = _env_or('IMAP_HOST', '')
IMAP_PORT = int(_env_or('IMAP_PORT', '993') or '993')
IMAP_USER = _env_or('IMAP_USER', '')
IMAP_PASS = _env_or('IMAP_PASS', '')
IMAP_SSL  = _env_or('IMAP_SSL', '1').strip() not in ('0', 'false', 'False', 'no')
ROUTER9  = _env_or('ROUTER9_URL', 'https://your-9router.example')
ROUTER9_PASS = _env_or('ROUTER9_PASS', 'change-me')
_domain_idx = 0
_domain_lock = threading.Lock()
def next_domain():
    global _domain_idx
    domains = DOMAINS or ['example.com']
    with _domain_lock:
        d = domains[_domain_idx % len(domains)].strip()
        _domain_idx += 1
        return d

def unlock_turnstile():
    """Return path to turnstilePatch directory (plaintext script.js + manifest.json)."""
    if not (TS_DIR / 'script.js').exists() or not (TS_DIR / 'manifest.json').exists():
        raise RuntimeError(f"missing turnstilePatch/script.js or manifest.json di {TS_DIR}")
    return str(TS_DIR)

def load_sso_accounts(path=None):
    """Parse sso.txt JSONL → list of account dicts (newest last)."""
    p = Path(path) if path else OUT
    accounts = []
    if not p.exists():
        return accounts
    for line in p.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = line.strip()
        if not line or '"email"' not in line:
            continue
        try:
            accounts.append(json.loads(line))
        except Exception:
            continue
    return accounts

# ── Premium ANSI Palette ──────────────────────────────────────
CYN   = '\033[38;5;39m'
GRN   = '\033[38;5;42m'
RED   = '\033[38;5;203m'
YEL   = '\033[38;5;214m'
PURP  = '\033[38;5;141m'
WHITE = '\033[1;37m'
DIM   = '\033[38;5;243m'
BOLD  = '\033[1m'
RST   = '\033[0m'
BAR_FILLED, BAR_EMPTY = '█', '░'

CURRENT_CONCURRENCY = 1

def clear_line():
    try:
        out = sys.stdout
        if out is None:
            return
        out.write('\r\033[K')
        flush = getattr(out, 'flush', None)
        if callable(flush):
            flush()
    except Exception:
        pass

def prog_bar(cur, total, width=28):
    if total == 0: return ""
    filled = int(cur / total * width)
    return f"[{GRN}{BAR_FILLED*filled}{RST}{DIM}{BAR_EMPTY*(width-filled)}{RST}]"

def step_log(b_id, step_num, step_name, details="", status="WAIT", tag_color=CYN, inplace=False):
    timestamp = time.strftime("%H:%M:%S")
    b_tag = f"{PURP}[B{b_id:02d}]{RST}"
    step_tag = f"{CYN}[{step_num:02d}/10]{RST}"

    if status == "OK":
        badge = f"{GRN}✓ OK  {RST}"
    elif status == "FAIL":
        badge = f"{RED}✗ FAIL{RST}"
    elif status == "WAIT":
        badge = f"{YEL}⏳ WAIT{RST}"
    elif status == "DONE":
        badge = f"{GRN}🎉 DONE{RST}"
    else:
        badge = f"{CYN}ℹ INFO{RST}"

    detail_str = f" {DIM}→ {details}{RST}" if details else ""
    line_content = f"  {DIM}{timestamp}{RST} │ {b_tag} {step_tag} {tag_color}{step_name:<22}{RST} │ {badge}{detail_str}"

    LOGGER.info(f"B{b_id:02d} [{step_num:02d}] {step_name} | {status} | {details}")
    if not inplace:
        EVENTS.emit({
            'type': 'step',
            'browser': b_id,
            'step': step_num,
            'name': step_name,
            'status': status,
            'detail': details or '',
            'ts': timestamp,
        })

    if inplace and CURRENT_CONCURRENCY == 1:
        try:
            out = sys.stdout
            if out is not None:
                out.write(f"\r\033[K{line_content}")
                flush = getattr(out, 'flush', None)
                if callable(flush):
                    flush()
        except Exception:
            pass
    else:
        safe_print(f"\r\033[K{line_content}")

def print_banner(total_acc, concurrency, mode_name):
    safe_print(f"\n{PURP}╭─────────────────────────────────────────────────────────────────────────────╮{RST}")
    safe_print(f"{PURP}│{RST} {WHITE}{BOLD}⚡ GROK AUTO SIGNUP ENGINE{RST}  {DIM}v4.0 Premium Automation{RST}{' '*18}{PURP}{RST}")
    safe_print(f"{PURP}╰─────────────────────────────────────────────────────────────────────────────╯{RST}")
    safe_print(f"  {CYN}🎯 Target Accounts{RST} : {WHITE}{BOLD}{total_acc}{RST} accounts")
    safe_print(f"  {CYN}⚙️  Execution Mode{RST} : {WHITE}{BOLD}{mode_name}{RST} {DIM}(Concurrency: {concurrency}){RST}")
    safe_print(f"  {CYN}📁 Storage Output{RST} : {WHITE}{OUT}{RST}")
    safe_print(f"{DIM}{'─'*77}{RST}\n")

def print_summary_table(results_list):
    safe_print(f"\n{CYN}╭── [ REGISTRATION SUMMARY RESULT ] ──────────────────────────────────────────╮{RST}")
    safe_print(f"{CYN}│{RST} {DIM}#    EMAIL / ACCOUNT ADDRESS              STATUS      DURATION   9ROUTER{RST} {CYN}{RST}")
    safe_print(f"{CYN}├───{'─'*73}┤{RST}")
    for idx, r in enumerate(results_list):
        if r.get('error'):
            status_str = f"{RED}FAILED {RST}"
            email_str = f"{DIM}— registration failed —{RST}"
            duration = f"{r.get('elapsed', '0.0s')}"
            r9_str = f"{DIM}—{RST}"
        else:
            status_str = f"{GRN}SUCCESS{RST}"
            email_str = r.get('email', '')[:34]
            duration = f"{r.get('elapsed', '0.0s')}"
            r9_str = f"{GRN}OK ✓{RST}" if r.get('r9_success') else f"{YEL}SKIP{RST}"

        safe_print(f"{CYN}│{RST} {DIM}{idx+1:03d}{RST} {email_str:<36} {status_str:<17} {duration:<10} {r9_str:<8} {CYN}{RST}")
    safe_print(f"{CYN}╰───{'─'*73}╯{RST}")

def print_footer(ok_n, total, t_start):
    elapsed = time.time() - t_start
    rate = (ok_n / max(1, elapsed)) * 60
    health = (ok_n / max(1, total)) * 100
    bar = prog_bar(ok_n, total, width=32)

    safe_print(f"\n{WHITE}{BOLD}📊 PERFORMANCE DIAGNOSTICS:{RST}")
    safe_print(f"  {CYN}Total Time{RST}   : {WHITE}{elapsed:.1f}s{RST}  │  {CYN}Average Speed{RST} : {WHITE}{rate:.1f} accounts/min{RST}")
    safe_print(f"  {CYN}Success Rate{RST} : {GRN}{health:.1f}%{RST}  │  {CYN}Health Check{RST}  : {GRN}{ok_n} OK{RST} / {RED}{total - ok_n} Error{RST}")
    safe_print(f"  {CYN}Overall Progress{RST}: {bar} {WHITE}{BOLD}{ok_n}/{total}{RST} ({int(ok_n/max(1,total)*100)}%)\n")

# ── Temp Mail providers ───────────────────────────────────────
def _strip_html(txt):
    if not txt:
        return ''
    t = re.sub(r'(?is)<script[^>]*>.*?</script>', ' ', txt)
    t = re.sub(r'(?is)<style[^>]*>.*?</style>', ' ', t)
    t = re.sub(r'(?s)<[^>]+>', ' ', t)
    t = re.sub(r'&nbsp;|&#160;', ' ', t, flags=re.I)
    t = re.sub(r'&amp;', '&', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()

def extract_otp_code(*texts):
    """Parse Grok/x.ai / SpaceXAI OTP from subject/body (plain or HTML)."""
    patterns = [
        # "confirmation code: UNF-G2X" / "code: ABC-DEF"
        r'(?:confirmation\s+)?code\s*[:\-–]?\s*([A-Z0-9]{3}-[A-Z0-9]{3})',
        r'(?:confirmation\s+)?code\s*[:\-–]?\s*([A-Z0-9]{6})',
        # "validate your email" nearby 6-char / xxx-xxx
        r'(?:validate|verification|verify|otp|one[-\s]?time)[^\n\r]{0,80}?([A-Z0-9]{3}-[A-Z0-9]{3})',
        r'(?:validate|verification|verify|otp|one[-\s]?time)[^\n\r]{0,80}?\b([A-Z0-9]{6})\b',
        # standalone token (subject often "… code: UNF-G2X")
        r'\b([A-Z0-9]{3}-[A-Z0-9]{3})\b',
        r'(?m)^\s*([A-Z0-9]{3}-[A-Z0-9]{3})\s*$',
        r'(?m)^\s*([A-Z0-9]{6})\s*$',
    ]
    for raw in texts:
        if not raw:
            continue
        for txt in (raw, _strip_html(raw)):
            if not txt:
                continue
            for pat in patterns:
                g = re.search(pat, txt, re.I)
                if g:
                    return g.group(1).replace('-', '').upper()
    return None

class MailMailldez:
    """HTTP catch-all API (existing workers.dev style)."""
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({'Accept': 'application/json', 'Content-Type': 'application/json'})
        self.addr = None

    def create(self):
        sid = self.s.get(f'{MAILLDEZ}/api/session', timeout=10).json()['sessionId']
        self.s.headers['x-session-id'] = sid
        self.domain = next_domain()
        r = self.s.post(f'{MAILLDEZ}/api/inboxes', json={'domain': self.domain}, timeout=10)
        self.addr = r.json()['address']
        return self.addr

    def peek_code(self):
        try:
            res = self.s.get(f'{MAILLDEZ}/api/inboxes/{self.addr}/messages', timeout=10)
            if res.status_code != 200:
                return None
            for m in res.json() or []:
                code = extract_otp_code(m.get('subject', ''), m.get('body', ''))
                if code:
                    return code
        except Exception:
            pass
        return None

class MailImap:
    """
    IMAP catch-all — aaPanel / cPanel / pure-ftpd mail.
    Setup aaPanel: Mail Server → domain → Catch-all → one mailbox (IMAP_USER).
    DNS MX must point to that server. Engine only needs IMAP login.
    """
    def __init__(self):
        self.host = IMAP_HOST
        self.port = IMAP_PORT
        self.user = IMAP_USER
        self.password = IMAP_PASS
        self.ssl = IMAP_SSL
        self.addr = None
        self.domain = None
        self._seen_uids = set()
        self._M = None  # reused IMAP session during OTP poll

    def create(self):
        if not self.host or not self.user:
            raise RuntimeError('IMAP_HOST / IMAP_USER kosong — isi config aaPanel IMAP')
        import secrets
        self.domain = next_domain()
        local = 'grok_' + secrets.token_hex(4)
        self.addr = f'{local}@{self.domain}'
        return self.addr

    def _connect(self):
        if self.ssl:
            # aaPanel often self-signed — verify would fail on Windows client
            ctx = ssl_mod.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_mod.CERT_NONE
            M = imaplib.IMAP4_SSL(self.host, self.port, timeout=20, ssl_context=ctx)
        else:
            M = imaplib.IMAP4(self.host, self.port, timeout=20)
        M.login(self.user, self.password)
        return M

    def open_session(self):
        """Keep one IMAP connection for whole OTP wait (big speed win vs reconnect each peek)."""
        self.close_session()
        try:
            self._M = self._connect()
            typ, _ = self._M.select('INBOX')
            if typ != 'OK':
                self.close_session()
        except Exception as e:
            LOGGER.debug(f'imap open_session: {e}')
            self.close_session()

    def close_session(self):
        if self._M is None:
            return
        try:
            self._M.logout()
        except Exception:
            try:
                self._M.shutdown()
            except Exception:
                pass
        self._M = None

    def _decode_hdr(self, val):
        if not val:
            return ''
        parts = []
        for chunk, enc in decode_header(val):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(enc or 'utf-8', errors='replace'))
            else:
                parts.append(str(chunk))
        return ' '.join(parts)

    def _msg_targets(self, msg):
        bits = []
        for h in (
            'To', 'Delivered-To', 'X-Original-To', 'Envelope-To', 'Cc',
            'X-Forwarded-To', 'Return-Path', 'Received', 'X-Spamd-Result',
        ):
            # Received may be multi-value
            if h == 'Received':
                for v in msg.get_all('Received') or []:
                    bits.append(self._decode_hdr(v))
            else:
                bits.append(self._decode_hdr(msg.get(h, '')))
        return ' '.join(bits).lower()

    def _msg_body(self, msg):
        texts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ('text/plain', 'text/html'):
                    try:
                        raw = part.get_payload(decode=True) or b''
                        charset = part.get_content_charset() or 'utf-8'
                        texts.append(raw.decode(charset, errors='replace'))
                    except Exception:
                        pass
        else:
            try:
                raw = msg.get_payload(decode=True) or b''
                charset = msg.get_content_charset() or 'utf-8'
                texts.append(raw.decode(charset, errors='replace'))
            except Exception:
                try:
                    texts.append(str(msg.get_payload() or ''))
                except Exception:
                    pass
        return '\n'.join(texts)

    def _msg_matches_target(self, msg, body, target):
        """
        STRICT: only this account's address/local-part.
        Catch-all inbox shares many OTPs — never take another account's code.
        Match via To / Return-Path (bounces+…-local=domain@…) / Received for <…> / body.
        """
        target = (target or '').lower().strip()
        if not target or '@' not in target:
            return False
        local, domain = target.split('@', 1)
        if len(local) < 6:
            return False  # too short = collision risk

        # recipient-oriented headers only (not Subject/From alone)
        recip_blob = ' '.join([
            self._msg_targets(msg),
            (body or '')[:4000].lower(),  # body may quote address
        ]).lower()

        # full address
        if target in recip_blob:
            return True
        # sendgrid/x.ai bounce tag: local=domain@
        if f'{local}={domain}' in recip_blob:
            return True
        # for <local@domain>
        if f'<{target}>' in recip_blob:
            return True
        # local part as whole token (avoid substring false positive)
        if re.search(rf'(?<![a-z0-9_]){re.escape(local)}(?![a-z0-9_])', recip_blob):
            return True
        return False

    def peek_code(self):
        if not self.addr:
            return None
        target = self.addr.lower()
        local = target.split('@')[0]
        own = self._M is not None
        M = self._M
        try:
            if M is None:
                M = self._connect()
                typ, _ = M.select('INBOX')
                if typ != 'OK':
                    LOGGER.debug(f'imap select fail: {typ}')
                    return None
            else:
                # refresh mailbox view without full reconnect
                try:
                    M.select('INBOX')
                except Exception:
                    self.close_session()
                    M = self._connect()
                    self._M = M
                    M.select('INBOX')

            # ONLY search tied to this address — never subject-only / ALL first
            ids = []
            seen = set()
            for crit in (
                f'(TO "{target}")',
                f'(HEADER To "{target}")',
                f'(TEXT "{target}")',
                f'(TEXT "{local}=")',          # bounce path local=domain
                f'(TEXT "{local}")',
            ):
                try:
                    typ, data = M.search(None, crit)
                    if typ == 'OK' and data and data[0]:
                        for mid in data[0].split():
                            if mid not in seen:
                                seen.add(mid)
                                ids.append(mid)
                except Exception as e:
                    LOGGER.debug(f'imap search {crit!r}: {e}')

            if not ids:
                return None

            # newest first
            for num in reversed(ids[-60:]):
                try:
                    uid_key = f'{self.user}:{num.decode() if isinstance(num, bytes) else num}'
                    if uid_key in self._seen_uids:
                        continue
                    typ, msg_data = M.fetch(num, '(RFC822.HEADER BODY.PEEK[TEXT])')
                    if typ != 'OK' or not msg_data:
                        typ, msg_data = M.fetch(num, '(RFC822)')
                    if typ != 'OK' or not msg_data:
                        continue
                    raw = b''
                    for part in msg_data:
                        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                            raw += part[1] + b'\r\n'
                    if not raw:
                        continue
                    msg = email_lib.message_from_bytes(raw)
                    body = self._msg_body(msg)
                    # if only headers fetched, body may be empty — fetch full once for candidates
                    if not body and re.search(r'code', self._decode_hdr(msg.get('Subject', '')), re.I):
                        typ2, full = M.fetch(num, '(RFC822)')
                        if typ2 == 'OK' and full:
                            raw2 = b''
                            for part in full:
                                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                                    raw2 = part[1]
                                    break
                            if raw2:
                                msg = email_lib.message_from_bytes(raw2)
                                body = self._msg_body(msg)
                    subj = self._decode_hdr(msg.get('Subject', ''))

                    # STRICT recipient match — no "any x.ai code" fallback
                    if not self._msg_matches_target(msg, body, target):
                        continue

                    code = extract_otp_code(subj, body, self._msg_targets(msg))
                    if code:
                        self._seen_uids.add(uid_key)
                        LOGGER.info(f'imap OTP for {target}: {code} (subj={subj[:80]!r})')
                        return code
                except Exception as e:
                    LOGGER.debug(f'imap fetch {num}: {e}')
                    continue
            return None
        except Exception as e:
            LOGGER.debug(f'imap peek: {e}\n{traceback.format_exc()}')
            if own:
                self.close_session()
            return None
        finally:
            if not own and M is not None and M is not self._M:
                try:
                    M.logout()
                except Exception:
                    try:
                        M.shutdown()
                    except Exception:
                        pass

def Mail():
    """Factory: mailldez (default) or imap (aaPanel)."""
    prov = (MAIL_PROVIDER or 'mailldez').strip().lower()
    if prov in ('imap', 'aapanel', 'aaPanel', 'cpanel', 'mailserver'):
        return MailImap()
    return MailMailldez()

# ── 9Router ──────────────────────────────────────────────────
class Router9:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({'Accept':'application/json','Content-Type':'application/json'})
    def login(self):
        try:
            r = self.s.post(f'{ROUTER9}/api/auth/login', json={'password':ROUTER9_PASS}, timeout=10)
            return r.json().get('success', False)
        except Exception:
            return False
    def device_code(self):
        r = self.s.get(f'{ROUTER9}/api/oauth/grok-cli/device-code', timeout=10)
        return r.json()
    def poll(self, device_code, code_verifier):
        try:
            r = self.s.post(f'{ROUTER9}/api/oauth/grok-cli/poll',
                            json={'deviceCode': device_code, 'codeVerifier': code_verifier}, timeout=10)
            return r.json()
        except Exception as e:
            return {'error': str(e)}
    def list_providers(self):
        try:
            r = self.s.get(f'{ROUTER9}/api/providers', timeout=10)
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get('providers', data.get('data', [])) or []
        except Exception:
            return []

# Mini browser tile — width >= Firefox practical min (~500) so no empty side chrome gap
WIN_W = 520
WIN_H = 700
WIN_GAP_X = 24
WIN_GAP_Y = 56
WIN_MARGIN = 16

# Track placed Camoufox HWNDs so we never move the Tk GUI or re-steal a tiled browser
_tile_lock = threading.Lock()
_placed_hwnds = set()          # hwnd already assigned a tile
_slot_hwnd = {}                # slot_idx -> hwnd

def get_tile_rect(slot_idx, max_slots=3):
    """Return (x, y, w, h) — unique position per slot, never stacked."""
    cols = max(1, min(int(max_slots), 4))
    col = (int(slot_idx) - 1) % cols
    row = (int(slot_idx) - 1) // cols
    x = WIN_MARGIN + col * (WIN_W + WIN_GAP_X)
    y = WIN_MARGIN + row * (WIN_H + WIN_GAP_Y)
    return x, y, WIN_W, WIN_H

def get_window_layout_args(slot_idx, max_slots=3):
    """Firefox CLI size only. Position via place_browser_window (Win32)."""
    _, _, w, h = get_tile_rect(slot_idx, max_slots)
    return ['-width', str(w), '-height', str(h)]

def clear_tile_registry():
    """Call at session start so new browsers can be tiled fresh."""
    with _tile_lock:
        _placed_hwnds.clear()
        _slot_hwnd.clear()

def place_browser_window(slot_idx, max_slots=3, timeout=8.0):
    """
    Move ONLY Camoufox/Firefox (MozillaWindowClass) to tile slot.
    NEVER touch Tk GUI ("Grok Auto Signup") / Explorer / other apps.
    """
    if not IS_WIN:
        return False
    x, y, w, h = get_tile_rect(slot_idx, max_slots)
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    IsWindowVisible = user32.IsWindowVisible
    GetWindowTextW = user32.GetWindowTextW
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    GetClassNameW = user32.GetClassNameW
    GetWindowRect = user32.GetWindowRect
    SetWindowPos = user32.SetWindowPos
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    IsIconic = user32.IsIconic

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    SWP_NOZORDER = 0x0004
    SWP_SHOWWINDOW = 0x0040

    # GUI / non-browser — NEVER move these (even if title has "Grok")
    TITLE_BLOCK = (
        'auto signup', 'grok auto', 'groksignup', 'file explorer',
        'visual studio', 'cursor', 'notepad',
    )
    CLASS_BLOCK = (
        'tktoplevel', 'cabinetwclass', 'chrome_widgetwin', 'hwndwrapper',
    )
    # Early browser titles BEFORE page load (must still match)
    TITLE_EARLY = (
        'new tab', 'newtab', 'camoufox', 'mozilla firefox', 'firefox',
        'about:blank', 'untitled',
    )
    TITLE_LATER = (
        'accounts.x.ai', 'sign up', 'signup', 'grok account', 'your grok',
        'create your grok', 'x.ai',
    )

    def _title(hwnd):
        n = GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ''
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ''

    def _classname(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, buf, 256)
        return buf.value or ''

    def _list_camoufox():
        found = []

        def _enum(hwnd, _lp):
            if not IsWindowVisible(hwnd):
                return True
            if IsIconic(hwnd):
                return True
            cls = _classname(hwnd)
            title = _title(hwnd)
            cl = cls.lower()
            tl = title.lower()

            # STRICT: only Firefox/Camoufox main window class
            if 'mozilla' not in cl:
                return True

            # block GUI app (TkTopLevel "Grok Auto Signup …")
            if any(b in tl for b in TITLE_BLOCK):
                return True
            if any(b in cl for b in CLASS_BLOCK):
                return True

            rect = wintypes.RECT()
            if not GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            ww = rect.right - rect.left
            hh = rect.bottom - rect.top
            # allow early empty windows (still smallish)
            if ww < 200 or hh < 200:
                return True

            pid = wintypes.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append({
                'hwnd': hwnd,
                'pid': pid.value,
                'title': title,
                'cls': cls,
                'w': ww, 'h': hh,
                'left': rect.left, 'top': rect.top,
            })
            return True

        EnumWindows(WNDENUMPROC(_enum), 0)
        return found

    def _move(hwnd):
        # SWP_NOSENDCHANGING sometimes helps force size past min constraints less aggressively;
        # still set exact w/h — Firefox may clamp width to ~450-500 which is fine if we request 520.
        return bool(SetWindowPos(
            hwnd, 0, int(x), int(y), int(w), int(h),
            SWP_NOZORDER | SWP_SHOWWINDOW,
        ))

    t0 = time.time()
    while time.time() - t0 < timeout:
        with _tile_lock:
            prev = _slot_hwnd.get(slot_idx)
            if prev:
                try:
                    if IsWindowVisible(prev):
                        ok = _move(prev)
                        LOGGER.debug(f'place slot={slot_idx} reapply hwnd={prev} ok={ok}')
                        return ok
                except Exception:
                    _placed_hwnds.discard(prev)
                    _slot_hwnd.pop(slot_idx, None)

            candidates = _list_camoufox()
            free = [c for c in candidates if c['hwnd'] not in _placed_hwnds]

            if free:
                def score(c):
                    t = c['title'].lower().strip()
                    s = 0
                    # early launch titles (BEFORE x.ai loads) — highest priority for new slot
                    if not t or t in ('new tab', 'newtab', 'camoufox', 'mozilla firefox', 'firefox'):
                        s += 30
                    if any(k in t for k in TITLE_EARLY):
                        s += 25
                    if any(k in t for k in TITLE_LATER):
                        s += 15
                    if 'camoufox' in t:
                        s += 10
                    # not already sitting on a tile
                    on_tile = any(
                        abs(c['left'] - get_tile_rect(s2, max_slots)[0]) < 30
                        and abs(c['top'] - get_tile_rect(s2, max_slots)[1]) < 30
                        for s2 in range(1, int(max_slots) + 3)
                    )
                    if not on_tile:
                        s += 20
                    if abs(c['left'] - x) > 40 or abs(c['top'] - y) > 40:
                        s += 5
                    return s

                free.sort(key=score, reverse=True)
                pick = free[0]
                ok = _move(pick['hwnd'])
                if ok:
                    # second force-resize after short delay (Firefox min-width clamp)
                    time.sleep(0.15)
                    _move(pick['hwnd'])
                    _placed_hwnds.add(pick['hwnd'])
                    _slot_hwnd[slot_idx] = pick['hwnd']
                    LOGGER.info(
                        f'tile slot={slot_idx} -> ({x},{y},{w}x{h}) '
                        f'title={pick["title"][:60]!r}'
                    )
                    return True
        time.sleep(0.25)

    LOGGER.debug(f'place_browser_window: no free Camoufox hwnd for slot {slot_idx}')
    return False

def fix_verify_url(url):
    if not url:
        return url
    parsed_router = ROUTER9.rstrip('/')
    return re.sub(r'^https?://(?:0\.0\.1\.204|0\.0\.0\.0|localhost|127\.0\.0\.1)(?::\d+)?', parsed_router, url)

def _fill_oauth_user_code(page, user_code):
    """Device code kadang tidak auto-isi di input — isi manual jika kosong."""
    if not user_code:
        return False
    code = str(user_code).strip().replace(' ', '').upper()
    # strip common separators shown in UI (ABCD-EFGH)
    code_plain = re.sub(r'[^A-Za-z0-9]', '', code)
    selectors = [
        'input[name="user_code"]',
        'input[name="userCode"]',
        'input[id*="user" i][id*="code" i]',
        'input[placeholder*="code" i]',
        'input[aria-label*="code" i]',
        'input[type="text"]',
        'input:not([type])',
    ]
    filled = False
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(min(n, 4)):
                el = loc.nth(i)
                try:
                    if not el.is_visible(timeout=800):
                        continue
                except Exception:
                    continue
                try:
                    cur = (el.input_value(timeout=500) or '').strip()
                except Exception:
                    cur = ''
                cur_plain = re.sub(r'[^A-Za-z0-9]', '', cur).upper()
                if cur_plain and (cur_plain == code_plain or code_plain in cur_plain or cur_plain in code_plain):
                    filled = True
                    continue
                # empty or wrong → fill
                try:
                    el.click(timeout=1500)
                    el.fill('')
                    # prefer formatted if input looks short segments; else plain
                    el.fill(code if '-' in str(user_code) else code_plain)
                    filled = True
                    LOGGER.debug(f'oauth filled user_code via {sel}[{i}]')
                    time.sleep(0.3)
                    return True
                except Exception as e:
                    LOGGER.debug(f'oauth fill fail {sel}[{i}]: {e}')
        except Exception:
            continue
    # JS fallback: first visible text-like input
    if not filled:
        try:
            ok = page.evaluate(
                """(code) => {
                    const inputs = [...document.querySelectorAll('input')].filter(i => {
                        const t = (i.type || 'text').toLowerCase();
                        return !i.disabled && !i.readOnly &&
                            ['text','tel','search',''].includes(t) &&
                            i.offsetParent !== null;
                    });
                    if (!inputs.length) return false;
                    const el = inputs[0];
                    el.focus();
                    el.value = code;
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                }""",
                code if '-' in str(user_code) else code_plain,
            )
            filled = bool(ok)
            if filled:
                LOGGER.debug('oauth filled user_code via JS fallback')
        except Exception as e:
            LOGGER.debug(f'oauth JS fill: {e}')
    return filled

def _click_oauth_buttons(page, names, timeout_each=3000):
    for btn_name in names:
        try:
            btn = page.get_by_role('button', name=btn_name, exact=False)
            if btn.count() == 0:
                btn = page.get_by_text(re.compile(rf'^{re.escape(btn_name)}$', re.I))
            if btn.count() > 0 and btn.first.is_visible(timeout=timeout_each):
                try:
                    btn.first.click(timeout=5000)
                except Exception:
                    btn.first.click(timeout=5000, force=True)
                time.sleep(0.45)
                return btn_name
        except Exception:
            continue
    return None

def do_9router_oauth(page, b_id=1, max_attempts=3):
    """
    Signup sudah sukses. Link 9Router device OAuth.
    Retry penuh bila code tidak terisi / poll timeout.
    Returns True jika provider added.
    """
    r9 = Router9()
    if not r9.login():
        step_log(b_id, 9, "9Router OAuth", "9Router Login Failed", "FAIL")
        return False

    for attempt in range(1, max_attempts + 1):
        STOP.check()
        try:
            d = r9.device_code()
        except Exception as e:
            LOGGER.debug(f'device_code fail: {e}')
            step_log(b_id, 9, "9Router OAuth", f"Device code error (try {attempt}/{max_attempts})", "WAIT")
            time.sleep(1.5)
            continue

        user_code = d.get('user_code') or d.get('userCode') or ''
        raw_url = d.get('verification_uri_complete') or d.get('verification_uri')
        verify_url = fix_verify_url(raw_url)
        if not verify_url:
            step_log(b_id, 9, "9Router OAuth", "No Device Verification URL", "FAIL")
            return False

        step_log(
            b_id, 9, "9Router OAuth",
            f"User Code: {user_code} (try {attempt}/{max_attempts})",
            "WAIT",
        )

        try:
            page.goto(verify_url, wait_until='domcontentloaded', timeout=25000)
        except Exception as e:
            LOGGER.debug(f'oauth goto: {e}')
        # wait input/button instead of fixed 1s
        try:
            page.wait_for_selector('input, button', timeout=4000)
        except Exception:
            time.sleep(0.35)

        # Pastikan kode terisi di input (masalah utama user)
        filled = _fill_oauth_user_code(page, user_code)
        if not filled:
            base = fix_verify_url(d.get('verification_uri') or '')
            if base and base.rstrip('/') != verify_url.split('?')[0].rstrip('/'):
                try:
                    page.goto(base, wait_until='domcontentloaded', timeout=20000)
                    try:
                        page.wait_for_selector('input, button', timeout=3000)
                    except Exception:
                        time.sleep(0.3)
                    filled = _fill_oauth_user_code(page, user_code)
                except Exception as e:
                    LOGGER.debug(f'oauth base goto: {e}')
        if filled:
            step_log(b_id, 9, "9Router OAuth", "Device code filled ✓", "WAIT")
            try:
                page.keyboard.press('Enter')
                time.sleep(0.35)
            except Exception:
                pass
        else:
            LOGGER.debug('oauth: no code input found — maybe URL already authorized form')

        clicked = _click_oauth_buttons(page, ['Continue', 'Authorize', 'Sign in', 'Next', 'Submit', 'Confirm'], timeout_each=2000)
        if clicked:
            LOGGER.debug(f'oauth clicked {clicked}')
            if not filled:
                if _fill_oauth_user_code(page, user_code):
                    filled = True
                    try:
                        page.keyboard.press('Enter')
                        time.sleep(0.3)
                    except Exception:
                        pass
                    _click_oauth_buttons(page, ['Continue', 'Authorize', 'Next', 'Submit'], timeout_each=2000)

        _click_oauth_buttons(page, ['Allow', 'Allow All', 'Accept', 'Approve', 'Confirm'], timeout_each=2000)
        time.sleep(0.35)
        _click_oauth_buttons(page, ['Allow', 'Allow All', 'Accept', 'Approve'], timeout_each=1500)

        # Poll: early snappy (0.7s), then 1.2s — success often within first few polls
        poll_rounds = 12 if attempt < max_attempts else 16
        for i in range(poll_rounds):
            STOP.check()
            res = r9.poll(d.get('device_code') or d.get('deviceCode'), d.get('codeVerifier') or d.get('code_verifier'))
            if res.get('success'):
                step_log(b_id, 9, "9Router OAuth", "Added to 9Router ✓", "OK")
                return True
            if res.get('error') and not res.get('pending', True):
                LOGGER.debug(f'oauth poll error: {res}')
            if i == 2:
                _fill_oauth_user_code(page, user_code)
                _click_oauth_buttons(page, ['Continue', 'Authorize', 'Allow', 'Allow All', 'Accept'], timeout_each=1000)
            time.sleep(0.7 if i < 5 else 1.2)

        step_log(
            b_id, 9, "9Router OAuth",
            f"Timeout — retry OAuth ({attempt}/{max_attempts})",
            "WAIT" if attempt < max_attempts else "FAIL",
        )
        time.sleep(0.5)

    step_log(b_id, 9, "9Router OAuth", "OAuth Timeout / Not Approved", "FAIL")
    return False

def wake_page(page, slot=None, max_slots=None):
    """
    Nudge browser so Windows does not keep it frozen in background.
    - Playwright bring_to_front
    - Win32: restore + topmost pulse (does NOT steal permanent focus)
    """
    try:
        page.bring_to_front()
    except Exception:
        pass
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        SWP_NOACTIVATE = 0x0010
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2

        # reuse slot hwnd if known
        hwnd = None
        with _tile_lock:
            if slot is not None:
                hwnd = _slot_hwnd.get(slot)
        if not hwnd:
            return
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # brief topmost pulse keeps compositor painting without long focus steal
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        time.sleep(0.05)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception as e:
        LOGGER.debug(f'wake_page: {e}')

# ── Smart click (force only as last resort; detail → file log) ─
def smart_click(page, locator, *, timeout=10000, success_selector=None, no_wait_after=False, wait_visible=20000, success_timeout=25000, slot=None):
    wake_page(page, slot=slot)
    target = locator.first
    target.wait_for(state='visible', timeout=wait_visible)
    for _ in range(2):
        try:
            box = target.bounding_box()
            if box and box.get('width', 0) > 0:
                break
        except Exception:
            pass
        time.sleep(0.12)

    click_kw = {'timeout': timeout}
    if no_wait_after:
        click_kw['no_wait_after'] = True

    try:
        target.click(**click_kw)
    except Exception as e:
        LOGGER.debug(f"smart_click normal fail: {type(e).__name__}: {e}")
        if success_selector:
            try:
                loc = page.locator(success_selector)
                if loc.count() > 0 and loc.first.is_visible():
                    return
            except Exception:
                pass
        try:
            force_kw = {'timeout': timeout, 'force': True}
            if no_wait_after:
                force_kw['no_wait_after'] = True
            target.click(**force_kw)
            LOGGER.debug('smart_click used force=True')
        except Exception as e2:
            LOGGER.debug(f"smart_click force fail: {type(e2).__name__}: {e2}")
            if not success_selector:
                raise

    if success_selector:
        try:
            page.wait_for_selector(success_selector, timeout=success_timeout)
        except Exception:
            # recovery under background throttle: wake + force re-click + longer wait
            wake_page(page, slot=slot)
            try:
                page.evaluate("window.scrollTo(0,0)")
            except Exception:
                pass
            try:
                force_kw = {'timeout': timeout, 'force': True}
                if no_wait_after:
                    force_kw['no_wait_after'] = True
                target.click(**force_kw)
            except Exception:
                pass
            page.wait_for_selector(success_selector, timeout=min(30000, max(success_timeout, 20000)))

def _is_retryable(err_msg):
    """Soft-retry flaky browser/UI — not OTP/Turnstile hard env fails."""
    if not err_msg:
        return False
    low = err_msg.lower()
    if 'otp timeout' in low or 'turnstile timeout' in low:
        return False
    if 'stopped by user' in low:
        return False
    keys = (
        '_getchildframes', 'target closed', 'frame detached',
        'browser navigation frame detached', 'browser has been closed',
        'context closed', 'page closed', 'connection closed',
        'wait_for_selector', 'timeout', 'email form', 'throttl',
        'ui interaction timeout', 'locator.click', 'not visible',
    )
    return any(k in low for k in keys)

def _friendly_error(err_msg):
    if not err_msg:
        return 'Unknown error'
    low = err_msg.lower()
    if '_getChildFrames' in err_msg or 'Target closed' in err_msg or 'frame detached' in low:
        return 'Browser navigation frame detached'
    if 'Stopped by user' in err_msg:
        return 'Stopped by user'
    if 'wait_for_selector' in low or ('timeout' in low and 'selector' in low):
        return 'UI timeout (browser background/throttle — auto-recovering)'
    if 'timeout' in low and 'exceeded' in low:
        return 'UI timeout (auto-recovering)'
    first = err_msg.strip().splitlines()[0] if err_msg.strip() else 'Unknown error'
    if is_noise_line(first) or first.startswith('Call log'):
        return 'UI interaction timeout'
    return first[:160]

def _camoufox_kwargs(ext_path=None, proxy_config=None, win_args=None, slot_idx=1, max_slots=1):
    """
    Camoufox = Firefox.
    - window=(w,h) native size (reliable)
    - position via place_browser_window() after launch (Windows)
    - NO Chromium --flags (become fake URLs in Firefox)
    """
    _, _, w, h = get_tile_rect(slot_idx, max_slots)
    args = list(win_args or get_window_layout_args(slot_idx, max_slots))
    if '-foreground' not in args:
        args.append('-foreground')
    # strip Chromium junk / URLs
    clean = []
    for a in args:
        s = str(a)
        if s.startswith('--'):
            continue
        if 'translateui' in s.lower() or 'blinkgen' in s.lower():
            continue
        if s.startswith('http://') or s.startswith('https://'):
            continue
        # drop -x/-y — ignored/unreliable on Win; use Win32 place instead
        if s in ('-x', '-y'):
            continue
        clean.append(a)
    # remove orphaned coords after dropping -x/-y
    args2 = []
    i = 0
    while i < len(clean):
        if clean[i] in ('-x', '-y') and i + 1 < len(clean):
            i += 2
            continue
        args2.append(clean[i])
        i += 1
    args = args2
    if '-width' not in args:
        args.extend(['-width', str(w), '-height', str(h)])

    # Firefox prefs: keep timers/network alive when window not focused (Windows throttle)
    ff_prefs = {
        'dom.min_background_timeout_value': 0,
        'dom.min_background_timeout_value_without_budget_throttling': 0,
        'dom.timeout.enable_budget_timer_throttling': False,
        'dom.timeout.throttling_delay': 0,
        'dom.timeout.background_throttling_max_budget': -1,
        'widget.windows.window_occlusion_tracking.enabled': False,
        'layers.acceleration.disabled': False,
        'network.http.speculative-parallel-limit': 0,
        'browser.tabs.remote.autostart': True,
        'dom.ipc.processPriorityManager.backgroundUseGracePeriod': False,
        'dom.ipc.processPriorityManager.backgroundGracePeriodMS': 0,
        'dom.ipc.processPriorityManager.backgroundPerceivableGracePeriodMS': 0,
    }
    kw = {
        'headless': False,
        'addons': [ext_path] if ext_path else [],
        'args': args,
        'window': (int(w), int(h)),  # Camoufox native fixed window size
        'block_webrtc': True,
        'firefox_user_prefs': ff_prefs,
        'os': 'windows' if IS_WIN else None,
    }
    if kw['os'] is None:
        kw.pop('os', None)
    if proxy_config:
        kw['proxy'] = proxy_config
    return kw

def _new_light_context(browser):
    """Viewport fills content area of 520x700 window (no empty side gap)."""
    try:
        return browser.new_context(
            viewport={'width': 500, 'height': 640},
            ignore_https_errors=True,
        )
    except TypeError:
        return browser.new_context(viewport={'width': 500, 'height': 640})

ANTI_BG_INIT = """
(() => {
  try {
    const vis = { get: () => false, configurable: true };
    const state = { get: () => 'visible', configurable: true };
    Object.defineProperty(document, 'hidden', vis);
    Object.defineProperty(document, 'webkitHidden', vis);
    Object.defineProperty(document, 'visibilityState', state);
    Object.defineProperty(document, 'webkitVisibilityState', state);
    Document.prototype.hasFocus = function() { return true; };
    window.hasFocus = () => true;
  } catch (e) {}
  try {
    for (const ev of ['visibilitychange','webkitvisibilitychange','blur','pagehide','freeze']) {
      window.addEventListener(ev, e => { e.stopImmediatePropagation(); }, true);
      document.addEventListener(ev, e => { e.stopImmediatePropagation(); }, true);
    }
    window.addEventListener('focus', () => {}, true);
  } catch (e) {}
  try {
    // keep rAF alive when backgrounded (best-effort)
    const raf = window.requestAnimationFrame.bind(window);
    let last = performance.now();
    const pump = (t) => { last = t; raf(pump); };
    raf(pump);
    setInterval(() => {
      if (performance.now() - last > 500) {
        try { window.dispatchEvent(new Event('resize')); } catch (e) {}
      }
    }, 1000);
  } catch (e) {}
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
  try { window.chrome = window.chrome || { runtime: {} }; } catch (e) {}
})();
"""

def _run_account_on_browser(browser, acc_num, b_slot, t_acc, concurrency=1):
    """One account on existing Camoufox process (new context = clean cookies)."""
    last_err = None
    conc = max(1, int(concurrency or 1))
    for attempt in range(2):
        if STOP.is_stopped():
            return {'error': 'Stopped by user', 'elapsed': f'{time.time()-t_acc:.1f}s', 'index': acc_num}
        step_log(
            b_slot, 0, f"Account #{acc_num:03d}",
            "Starting..." if attempt == 0 else f"Retry #{attempt} (fresh context)...",
            "WAIT",
        )
        ctx = None
        try:
            ctx = _new_light_context(browser)
            res = run_one(ctx, b_id=b_slot, tile_slot=b_slot, tile_max=conc)
            res['elapsed'] = f"{time.time()-t_acc:.1f}s"
            res['index'] = acc_num
            return res
        except TypeError as e:
            # older camoufox without some context kwargs
            if 'unexpected' in str(e).lower() or 'keyword' in str(e).lower():
                try:
                    if ctx:
                        ctx.close()
                except Exception:
                    pass
                ctx = browser.new_context(viewport={'width': 500, 'height': 640})
                res = run_one(ctx, b_id=b_slot, tile_slot=b_slot, tile_max=conc)
                res['elapsed'] = f"{time.time()-t_acc:.1f}s"
                res['index'] = acc_num
                return res
            raise
        except Exception as e:
            last_err = e
            err_msg = str(e)
            LOGGER.error(f"B{b_slot:02d} account#{acc_num} attempt={attempt}: {err_msg}")
            LOGGER.debug(traceback.format_exc())
            if attempt == 0 and _is_retryable(err_msg) and not STOP.is_stopped():
                step_log(b_slot, 0, f"Account #{acc_num:03d}", "Flaky UI — retry once", "WAIT")
                time.sleep(1.0)
                continue
            friendly = _friendly_error(err_msg)
            step_log(b_slot, 0, f"Account #{acc_num:03d}", friendly, "FAIL")
            return {
                'error': friendly,
                'elapsed': f"{time.time()-t_acc:.1f}s",
                'index': acc_num,
                'retryable': _is_retryable(err_msg),
            }
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
    friendly = _friendly_error(str(last_err) if last_err else 'Unknown error')
    return {
        'error': friendly,
        'elapsed': f"{time.time()-t_acc:.1f}s",
        'index': acc_num,
        'retryable': _is_retryable(str(last_err or '')),
    }

def worker(task_idx, concurrency=1, proxy_config=None, ext_path=None, browser=None):
    """If browser= given (serial reuse), skip Camoufox launch. Else launch per task."""
    acc_num = task_idx + 1
    b_slot = (task_idx % max(1, concurrency)) + 1
    t_acc = time.time()

    if STOP.is_stopped():
        return {'error': 'Stopped by user', 'elapsed': '0.0s', 'index': acc_num}

    # Stagger only for true multi-browser launch (not reused process)
    if browser is None:
        if task_idx < concurrency:
            time.sleep(task_idx * 1.2)
        elif concurrency == 1 and task_idx > 0:
            time.sleep(0.4)

    if STOP.is_stopped():
        return {'error': 'Stopped by user', 'elapsed': f'{time.time()-t_acc:.1f}s', 'index': acc_num}

    if browser is not None:
        return _run_account_on_browser(browser, acc_num, b_slot, t_acc, concurrency=concurrency)

    if not ext_path:
        try:
            ext_path = unlock_turnstile()
        except Exception:
            ext_path = None

    win_args = get_window_layout_args(b_slot, concurrency)
    last_err = None

    for attempt in range(2):
        if STOP.is_stopped():
            return {'error': 'Stopped by user', 'elapsed': f'{time.time()-t_acc:.1f}s', 'index': acc_num}

        step_log(b_slot, 0, f"Account #{acc_num:03d}",
                 "Browser mini launching..." if attempt == 0 else f"Retry #{attempt} launching...",
                 "WAIT")
        try:
            camoufox_kwargs = _camoufox_kwargs(
                ext_path, proxy_config, win_args,
                slot_idx=b_slot, max_slots=concurrency,
            )
            # strip unsupported Camoufox kwargs
            while True:
                try:
                    with Camoufox(**camoufox_kwargs) as browser:
                        if STOP.is_stopped():
                            return {'error': 'Stopped by user', 'elapsed': f'{time.time()-t_acc:.1f}s', 'index': acc_num}
                        # tile ASAP (title still "New Tab" / "Camoufox") then again after load
                        try:
                            place_browser_window(b_slot, concurrency, timeout=10.0)
                        except Exception as pe:
                            LOGGER.debug(f'place window: {pe}')
                        return _run_account_on_browser(
                            browser, acc_num, b_slot, t_acc, concurrency=concurrency,
                        )
                except TypeError as te:
                    m = re.search(r"unexpected keyword argument ['\"]([^'\"]+)['\"]", str(te))
                    if m and m.group(1) in camoufox_kwargs:
                        camoufox_kwargs.pop(m.group(1), None)
                        continue
                    raise
        except Exception as e:
            last_err = e
            err_msg = str(e)
            LOGGER.error(f"B{b_slot:02d} account#{acc_num} launch attempt={attempt}: {err_msg}")
            LOGGER.debug(traceback.format_exc())
            if attempt == 0 and _is_retryable(err_msg) and not STOP.is_stopped():
                step_log(b_slot, 0, f"Account #{acc_num:03d}", "Flaky error — retry once", "WAIT")
                time.sleep(1.0)
                continue
            friendly = _friendly_error(err_msg)
            step_log(b_slot, 0, f"Account #{acc_num:03d}", friendly, "FAIL")
            return {'error': friendly, 'elapsed': f"{time.time()-t_acc:.1f}s", 'index': acc_num}

    friendly = _friendly_error(str(last_err) if last_err else 'Unknown error')
    return {'error': friendly, 'elapsed': f"{time.time()-t_acc:.1f}s", 'index': acc_num}

def run_batch(total_accounts, concurrency, proxy_config=None, event_queue=None):
    """Run full batch. GUI passes event_queue; CLI can call without."""
    global CURRENT_CONCURRENCY
    STOP.reset()
    clear_tile_registry()
    if event_queue is not None:
        EVENTS.set_queue(event_queue)
    concurrency = max(1, min(int(concurrency), int(total_accounts)))
    CURRENT_CONCURRENCY = concurrency
    t_start = time.time()
    results = []
    ok_n = 0
    completed = 0
    counter_lock = threading.Lock()

    EVENTS.emit({
        'type': 'session', 'status': 'start',
        'total': total_accounts, 'concurrency': concurrency,
    })
    EVENTS.emit({
        'type': 'log', 'level': 'info',
        'message': f'Session start · {total_accounts} accounts · {concurrency} browsers',
    })
    if proxy_config:
        EVENTS.emit({
            'type': 'log', 'level': 'info',
            'message': f"Proxy active: {proxy_config.get('server', '?')}",
        })

    try:
        try:
            ext_path = unlock_turnstile()
            step_log(1, 0, "Turnstile Extension", f"Loaded: {TS_DIR.name}", "OK")
        except Exception as e:
            step_log(1, 0, "Turnstile Extension", str(e), "FAIL")
            EVENTS.emit({'type': 'session', 'status': 'error', 'error': str(e)})
            return [], 0

        def finish_one(res, i):
            nonlocal ok_n, completed
            with counter_lock:
                completed += 1
                if not res.get('error'):
                    ok_n += 1
                c, o = completed, ok_n
            EVENTS.emit({
                'type': 'progress',
                'completed': c,
                'success': o,
                'failed': c - o,
                'total': total_accounts,
            })
            b = (i % concurrency) + 1
            if res.get('error'):
                EVENTS.emit({
                    'type': 'account_fail',
                    'browser': b,
                    'index': res.get('index', i + 1),
                    'error': res['error'],
                    'elapsed': res.get('elapsed', ''),
                    'email': res.get('email', ''),
                })
            else:
                EVENTS.emit({
                    'type': 'account_ok',
                    'browser': b,
                    'index': res.get('index', i + 1),
                    'email': res.get('email', ''),
                    'elapsed': res.get('elapsed', ''),
                    'r9_success': bool(res.get('r9_success')),
                })
            return res

        # concurrency==1: reuse browser, but RECOVER after fail streak (background throttle)
        if concurrency == 1:
            EVENTS.emit({
                'type': 'log', 'level': 'info',
                'message': 'Serial mode · browser reuse + auto-recover after fails (OK to switch tabs)',
            })
            win_args = get_window_layout_args(1, 1)
            fail_streak = 0
            browser_cm = None
            browser = None

            def _open_browser():
                kw = _camoufox_kwargs(
                    ext_path, proxy_config, win_args,
                    slot_idx=1, max_slots=1,
                )
                while True:
                    try:
                        cm = Camoufox(**kw)
                        br = cm.__enter__()
                        try:
                            place_browser_window(1, 1, timeout=5.0)
                        except Exception as pe:
                            LOGGER.debug(f'place window serial: {pe}')
                        return cm, br
                    except TypeError as e:
                        msg = str(e)
                        m = re.search(r"unexpected keyword argument '([^']+)'", msg)
                        if not m:
                            m = re.search(r'got an unexpected keyword argument "([^"]+)"', msg)
                        if m and m.group(1) in kw:
                            kw.pop(m.group(1), None)
                            continue
                        raise

            def _close_browser(cm):
                if cm is None:
                    return
                try:
                    cm.__exit__(None, None, None)
                except Exception as e:
                    LOGGER.debug(f'browser close: {e}')

            try:
                browser_cm, browser = _open_browser()
                for i in range(total_accounts):
                    if STOP.is_stopped():
                        EVENTS.emit({'type': 'log', 'level': 'warn', 'message': 'Session stopped by user'})
                        break

                    # 1 UI fail while backgrounded often poisons session → relaunch sooner
                    if fail_streak >= 1:
                        EVENTS.emit({
                            'type': 'log', 'level': 'warn',
                            'message': f'Auto-recover: relaunch browser after {fail_streak} fail(s)',
                        })
                        _close_browser(browser_cm)
                        clear_tile_registry()
                        time.sleep(1.2)
                        browser_cm, browser = _open_browser()
                        fail_streak = 0

                    # long fail wave → cool down
                    if fail_streak >= 3:
                        EVENTS.emit({
                            'type': 'log', 'level': 'warn',
                            'message': 'Cool-down 10s after repeated UI timeouts…',
                        })
                        time.sleep(10)
                        _close_browser(browser_cm)
                        clear_tile_registry()
                        browser_cm, browser = _open_browser()
                        fail_streak = 0

                    res = worker(
                        i, concurrency=1, proxy_config=proxy_config,
                        ext_path=ext_path, browser=browser,
                    )
                    if res.get('error'):
                        fail_streak += 1
                        # hard UI timeout → force relaunch next account
                        if res.get('retryable') or 'timeout' in str(res.get('error', '')).lower():
                            fail_streak = max(fail_streak, 2)
                    else:
                        fail_streak = 0
                    results.append(finish_one(res, i))
                    # tiny gap so next context is clean
                    time.sleep(0.25)
            except Exception as e:
                LOGGER.error(f'serial browser fatal: {e}\n{traceback.format_exc()}')
                if not results:
                    raise
            finally:
                _close_browser(browser_cm)
        else:
            def task(i):
                if STOP.is_stopped():
                    return {'error': 'Stopped by user', 'elapsed': '0.0s', 'index': i + 1}
                res = worker(i, concurrency=concurrency, proxy_config=proxy_config, ext_path=ext_path)
                return finish_one(res, i)

            executor = ThreadPoolExecutor(max_workers=concurrency)
            futures = []
            try:
                futures = [executor.submit(task, i) for i in range(total_accounts)]
                for future in as_completed(futures):
                    if STOP.is_stopped():
                        for f in futures:
                            f.cancel()
                        EVENTS.emit({'type': 'log', 'level': 'warn', 'message': 'Session stopped by user'})
                        break
                    try:
                        results.append(future.result())
                    except CancelledError:
                        results.append({'error': 'Stopped by user', 'elapsed': '0.0s'})
                    except Exception as e:
                        LOGGER.error(f'future error: {e}')
                        results.append({'error': _friendly_error(str(e)), 'elapsed': '0.0s'})
            finally:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    executor.shutdown(wait=False)

        with counter_lock:
            final_ok = ok_n
        print_summary_table(results)
        print_footer(final_ok, total_accounts, t_start)
        EVENTS.emit({
            'type': 'session', 'status': 'done',
            'ok': final_ok, 'total': total_accounts,
            'elapsed': time.time() - t_start,
            'stopped': STOP.is_stopped(),
        })
        return results, final_ok
    except Exception as e:
        LOGGER.error(f'run_batch fatal: {e}\n{traceback.format_exc()}')
        EVENTS.emit({'type': 'session', 'status': 'error', 'error': _friendly_error(str(e))})
        EVENTS.emit({'type': 'log', 'level': 'error', 'message': f'Fatal: {_friendly_error(str(e))}'})
        raise
    finally:
        EVENTS.clear_queue()

def main():
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except Exception:
            pass
    args = sys.argv[1:]
    if args and args[0] == '--router':
        accounts = []
        with open(OUT) as f:
            for l in f:
                if l.strip() and '"email"' in l:
                    try: accounts.append(json.loads(l))
                    except Exception: pass
        if len(args) > 1:
            n = int(args[1])
            accounts = accounts[-n:]
        if not accounts:
            safe_print(f"    {RED}✗{RST} gak ada akun di sso.txt"); return
        safe_print(f"    {YEL}→{RST} {len(accounts)} akun di sso.txt")
        add_to_router(accounts)
        return

    total_accounts = None
    concurrency = None

    if args:
        if args[0].isdigit():
            total_accounts = int(args[0])
            if len(args) > 1 and args[1].isdigit():
                concurrency = int(args[1])

    if total_accounts is None:
        try:
            safe_print(f"\n{PURP}╭──────────────────────────────────────────────────────────╮{RST}")
            safe_print(f"{PURP}│{RST} {WHITE}{BOLD}🎯 SETTING JUMLAH TARGET AKUN GROK{RST}{' '*22}{PURP}{RST}")
            safe_print(f"{PURP}╰──────────────────────────────────────────────────────────╯{RST}")
            val1 = input(f"  {CYN}👉 Berapa total akun yang ingin dibuat? [default: 50]: {RST}").strip()
            total_accounts = int(val1) if val1.isdigit() and int(val1) > 0 else 50
        except (KeyboardInterrupt, EOFError):
            print()
            return

    if concurrency is None:
        try:
            safe_print(f"\n{PURP}╭──────────────────────────────────────────────────────────╮{RST}")
            safe_print(f"{PURP}│{RST} {WHITE}{BOLD}⚙️  PILIH MODE EKSEKUSI PENDAFTARAN{RST}{' '*21}{PURP}{RST}")
            safe_print(f"{PURP}╰──────────────────────────────────────────────────────────╯{RST}")
            safe_print(f"  {GRN}[1]{RST} {WHITE}{BOLD}Mode Paralel{RST}      {DIM}→ Buka beberapa browser mini sekaligus{RST}")
            safe_print(f"  {YEL}[2]{RST} {WHITE}{BOLD}Mode Non-Paralel{RST}  {DIM}→ Jalankan 1 per 1 (Satu browser per sesi){RST}")

            mode_choice = input(f"\n  {CYN}👉 Pilihan Anda [1/2, default 1]: {RST}").strip()

            if mode_choice == '2':
                concurrency = 1
                safe_print(f"  {GRN}✓ Mode Non-Paralel (Satu per Satu) diaktifkan.{RST}\n")
            else:
                val2 = input(f"  {CYN}👉 Berapa browser paralel yang terbuka sekaligus? [default 3]: {RST}").strip()
                concurrency = int(val2) if val2.isdigit() and int(val2) > 0 else 3
                safe_print(f"  {GRN}✓ Mode Paralel ({concurrency} browser sekaligus) diaktifkan.{RST}\n")
        except (KeyboardInterrupt, EOFError):
            print()
            return

    concurrency = min(concurrency, total_accounts)
    mode_name = f"Paralel ({concurrency} Browser)" if concurrency > 1 else "Non-Paralel (1 per 1)"
    print_banner(total_accounts, concurrency, mode_name)
    run_batch(total_accounts, concurrency, proxy_config=None, event_queue=None)

def add_to_router(accounts):
    with router_lock:
        safe_print(f"\n{CYN}╭── [ 9ROUTER BATCH ADD ] ────────────────────────────────────────────────────╮{RST}")
        r9 = Router9()
        if not r9.login():
            safe_print(f"│  {RED}✗ 9router login failed{RST}"); return
        safe_print(f"│  {GRN}✓ 9router login success{RST}")
        existing = {c.get('email') for c in r9.list_providers()}
        safe_print(f"│  {GRN}✓ existing grok-cli accounts: {len(existing)}{RST}")

        with Camoufox(headless=False, args=['-width', '480', '-height', '640', '-x', '100', '-y', '100']) as browser:
            ctx = browser.new_context(viewport={'width': 460, 'height': 580})
            added = 0; skipped = 0; failed = 0
            for i, acc in enumerate(accounts):
                email = acc['email']
                safe_print(f"  {DIM}[{i+1}/{len(accounts)}]{RST} {email}")
                if email in existing:
                    safe_print(f"    {YEL}→{RST} sudah ada, skip")
                    skipped += 1; continue
                try:
                    pw_cookies = []
                    for c in acc.get('sso_cookies', []):
                        cc = dict(c)
                        if not cc.get('domain'): continue
                        ss = cc.get('sameSite','Lax')
                        if ss not in ('Strict','Lax','None'): ss = 'Lax'
                        cc['sameSite'] = ss
                        pw_cookies.append(cc)
                    ctx.clear_cookies()
                    ctx.add_cookies(pw_cookies)
                    d = r9.device_code()
                    user_code = d['user_code']
                    verify_url = fix_verify_url(d.get('verification_uri_complete'))
                    safe_print(f"    {YEL}→{RST} user_code: {user_code}")
                    page = ctx.new_page()
                    page.goto(verify_url, wait_until='domcontentloaded', timeout=30000)
                    time.sleep(2)
                    has_login_input = page.evaluate("!!document.querySelector('input[type=email], input[type=password]')")
                    if has_login_input:
                        safe_print(f"    {RED}✗{RST} SSO expired, need login")
                        page.close(); failed += 1; continue
                    try:
                        page.get_by_role('button', name='Continue', exact=False).click(timeout=5000)
                        clicked = 'continue'
                        time.sleep(2)
                    except Exception:
                        clicked = None
                    if clicked:
                        safe_print(f"    {GRN}✓{RST} continue")
                        try:
                            page.get_by_role('button', name='Allow', exact=True).click(timeout=6000)
                            safe_print(f"    {GRN}✓{RST} allow")
                            time.sleep(2)
                        except Exception:
                            try:
                                page.get_by_role('button', name='Allow All', exact=True).click(timeout=3000)
                                safe_print(f"    {GRN}✓{RST} allow all")
                                time.sleep(2)
                            except Exception:
                                safe_print(f"    {RED}✗{RST} tombol Allow gak ketemu")
                                page.close(); failed += 1; continue
                    time.sleep(2)
                    page.close()
                    for _ in range(12):
                        res = r9.poll(d['device_code'], d['codeVerifier'])
                        if res.get('success'):
                            safe_print(f"    {GRN}✓{RST} added to 9router ✓")
                            added += 1; break
                        elif not res.get('pending'):
                            safe_print(f"    {RED}✗{RST} poll error: {res.get('error')}")
                            failed += 1; break
                        time.sleep(3)
                    else:
                        safe_print(f"    {RED}✗{RST} poll timeout"); failed += 1
                except Exception as e:
                    safe_print(f"    {RED}✗{RST} err: {e}"); failed += 1
            browser.close()
        safe_print(f"{CYN}╰───{'─'*73}╯{RST}\n")

# ── Detect Chrome version for sec-ch-ua header ─────────────────
import subprocess
def _chrome_major():
    try:
        out = subprocess.check_output(['google-chrome-stable', '--version'], stderr=subprocess.DEVNULL, text=True)
        return re.search(r'(\d+)\.', out).group(1)
    except Exception:
        return '148'
CHROME_V = _chrome_major()

def run_one(ctx, b_id=1, tile_slot=None, tile_max=None):
    # init before any page — survives background tab throttle better
    try:
        ctx.add_init_script(ANTI_BG_INIT)
    except Exception as e:
        LOGGER.debug(f'init script: {e}')

    page = ctx.new_page()
    page.set_default_timeout(45000)
    page.set_default_navigation_timeout(60000)
    wake_page(page, slot=tile_slot)
    if tile_slot is not None:
        try:
            place_browser_window(tile_slot, tile_max or 1, timeout=4.0)
        except Exception:
            pass

    is_firefox = ctx.browser and ctx.browser.browser_type.name == 'firefox'
    if not is_firefox:
        page.set_extra_http_headers({
            'sec-ch-ua': f'"Chromium";v="{CHROME_V}", "Google Chrome";v="{CHROME_V}", "Not-A.Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"' if IS_WIN else '"Linux"',
            'accept-language': 'en-US,en;q=0.9',
        })
    try:
        return _flow(page, b_id=b_id, tile_slot=tile_slot, tile_max=tile_max)
    finally:
        try:
            page.close()
        except Exception:
            pass

def _flow(page, b_id=1, tile_slot=None, tile_max=None):
    slot = tile_slot if tile_slot is not None else b_id
    tmax = tile_max if tile_max is not None else max(CURRENT_CONCURRENCY, b_id)

    def _wake():
        wake_page(page, slot=slot)
        try:
            place_browser_window(slot, tmax, timeout=1.5)
        except Exception:
            pass

    # 1 — open signup; create temp mail in parallel thread (overlap network)
    STOP.check()
    step_log(b_id, 1, "Open Signup Page", "accounts.x.ai", "WAIT")
    mail_box = {'mail': None, 'addr': None, 'err': None}

    def _prep_mail():
        try:
            m = Mail()
            mail_box['addr'] = m.create()
            mail_box['mail'] = m
        except Exception as e:
            mail_box['err'] = e

    mail_th = threading.Thread(target=_prep_mail, daemon=True)
    mail_th.start()

    _wake()
    page.goto(SIGNUP, wait_until='domcontentloaded', timeout=60000)
    _wake()
    try:
        page.get_by_role('button', name=re.compile(r'Sign up with email', re.I)).first.wait_for(
            state='visible', timeout=25000
        )
    except Exception:
        _wake()
        time.sleep(0.8)
    try:
        page.get_by_role('button', name='Accept All Cookies').click(timeout=3000)
    except Exception:
        try:
            page.get_by_role('button', name=re.compile(r'Accept All', re.I)).click(timeout=2000)
        except Exception:
            pass
    step_log(b_id, 1, "Open Signup Page", "Page Loaded ✓", "OK")

    # 2
    STOP.check()
    step_log(b_id, 2, "Email Form Click", "Sign up with email", "WAIT")
    btn_email = page.get_by_role('button', name=re.compile(r'Sign up with email', re.I))
    try:
        if btn_email.count() == 0:
            btn_email = page.get_by_text(re.compile(r'Sign up with email', re.I))
    except Exception:
        btn_email = page.get_by_text(re.compile(r'Sign up with email', re.I))
    try:
        smart_click(
            page, btn_email, timeout=12000,
            success_selector='input[type=email]',
            wait_visible=25000,
            success_timeout=35000,
            slot=slot,
        )
    except Exception as e:
        # full page reload recovery (common when OS throttles background browser)
        LOGGER.debug(f'email form first try fail: {e}')
        step_log(b_id, 2, "Email Form Click", "Retry after reload…", "WAIT")
        _wake()
        try:
            page.reload(wait_until='domcontentloaded', timeout=45000)
        except Exception:
            page.goto(SIGNUP, wait_until='domcontentloaded', timeout=60000)
        time.sleep(0.8)
        _wake()
        try:
            page.get_by_role('button', name='Accept All Cookies').click(timeout=3000)
        except Exception:
            pass
        btn_email = page.get_by_role('button', name=re.compile(r'Sign up with email', re.I))
        try:
            if btn_email.count() == 0:
                btn_email = page.get_by_text(re.compile(r'Sign up with email', re.I))
        except Exception:
            btn_email = page.get_by_text(re.compile(r'Sign up with email', re.I))
        smart_click(
            page, btn_email, timeout=12000,
            success_selector='input[type=email]',
            wait_visible=25000,
            success_timeout=35000,
            slot=slot,
        )
    step_log(b_id, 2, "Email Form Click", "Form Ready ✓", "OK")

    # 3 — join mail thread (usually already done)
    STOP.check()
    step_log(b_id, 3, "Create Temp Email", "Requesting inbox...", "WAIT")
    mail_th.join(timeout=15)
    if mail_box['err'] or not mail_box['addr']:
        # fallback sync create
        try:
            mail = Mail()
            addr = mail.create()
        except Exception as e:
            raise RuntimeError(f'temp mail failed: {e}') from e
    else:
        mail, addr = mail_box['mail'], mail_box['addr']
    step_log(b_id, 3, "Create Temp Email", addr, "WAIT")
    _wake()
    page.locator('input[type=email]').fill(addr)
    page.locator('input[type=email]').press('Enter')
    try:
        page.wait_for_selector('input[name=code]', timeout=25000)
    except Exception:
        _wake()
        try:
            page.get_by_role('button', name='Sign up').click(timeout=3000)
            page.wait_for_selector('input[name=code]', timeout=20000)
        except Exception:
            pass
    step_log(b_id, 3, "Create Temp Email", "Email Submitted ✓", "OK")

    # 4 — OTP poll (reuse IMAP session = much faster than reconnect every loop)
    STOP.check()
    step_log(b_id, 4, "Wait OTP Code", "Polling inbox...", "WAIT")
    if hasattr(mail, 'open_session'):
        try:
            mail.open_session()
        except Exception:
            pass
    t = time.time()
    code = None
    try:
        while time.time() - t < 120:
            STOP.check()
            try:
                code = mail.peek_code()
            except Exception:
                pass
            if code:
                break
            elapsed = int(time.time() - t)
            # keep browser paint alive during long OTP wait in background
            if elapsed > 0 and elapsed % 8 == 0:
                _wake()
            if CURRENT_CONCURRENCY == 1:
                step_log(b_id, 4, "Wait OTP Code", f"Waiting ({elapsed}s)...", "WAIT", inplace=True)
            if elapsed < 15:
                time.sleep(0.45)
            elif elapsed < 40:
                time.sleep(0.8)
            else:
                time.sleep(1.4)
    finally:
        if hasattr(mail, 'close_session'):
            try:
                mail.close_session()
            except Exception:
                pass

    if not code:
        step_log(b_id, 4, "Wait OTP Code", "OTP Timeout 120s", "FAIL")
        raise RuntimeError("OTP timeout 120s")
    step_log(b_id, 4, "Wait OTP Code", f"Code: {code} ✓", "OK")

    # 5
    STOP.check()
    step_log(b_id, 5, "Submit OTP Code", "Filling code...", "WAIT")
    _wake()
    page.locator('input[name=code]').first.fill(code, timeout=15000)
    page.keyboard.press('Enter')
    try:
        page.wait_for_selector('input[name=givenName]', timeout=30000)
    except Exception:
        _wake()
        page.wait_for_selector('input[name=givenName]', timeout=20000)
    step_log(b_id, 5, "Submit OTP Code", "Verified ✓", "OK")

    # 6
    STOP.check()
    step_log(b_id, 6, "Fill Profile Form", "Name & Password", "WAIT")
    _wake()
    local = addr.split('@')[0]
    parts = re.split(r'[._\-]', local)
    given  = parts[0].capitalize()
    family = (parts[1] if len(parts) > 1 else 'Xyz').capitalize()
    page.locator('input[name=givenName]').fill(given)
    page.locator('input[name=familyName]').fill(family)
    page.locator('input[name=password]').fill(PASSWORD)
    step_log(b_id, 6, "Fill Profile Form", f"Name: {given} {family} ✓", "OK")

    # 7 — Turnstile poll (wake every few seconds so CF widget keeps ticking in bg)
    STOP.check()
    step_log(b_id, 7, "Solve Turnstile", "Auto-solving Cloudflare...", "WAIT")
    tok = ''
    t7 = time.time()
    while time.time() - t7 < 50:
        STOP.check()
        try:
            tok = page.evaluate("document.querySelector('input[name=cf-turnstile-response]')?.value || ''")
        except Exception:
            tok = ''
        if tok:
            break
        elapsed = int(time.time() - t7)
        if elapsed > 0 and elapsed % 5 == 0:
            _wake()
        if CURRENT_CONCURRENCY == 1:
            step_log(b_id, 7, "Solve Turnstile", f"Waiting ({elapsed}s)...", "WAIT", inplace=True)
        time.sleep(0.35 if elapsed < 12 else 0.65)

    if not tok:
        step_log(b_id, 7, "Solve Turnstile", "Turnstile Timeout 50s", "FAIL")
        raise RuntimeError("Turnstile timeout 50s")
    step_log(b_id, 7, "Solve Turnstile", "Solved & Submitted ✓", "OK")
    time.sleep(0.15)

    btn_signup = page.get_by_role('button', name=re.compile(r'Complete sign up', re.I))
    try:
        if btn_signup.count() == 0:
            btn_signup = page.get_by_text(re.compile(r'Complete sign up', re.I))
    except Exception:
        btn_signup = page.get_by_text(re.compile(r'Complete sign up', re.I))
    try:
        smart_click(
            page, btn_signup, timeout=12000, no_wait_after=True,
            wait_visible=20000, slot=slot,
        )
    except Exception as e:
        LOGGER.debug(f'complete signup click: {e}')

    # 8
    STOP.check()
    step_log(b_id, 8, "Redirect grok.com", "Waiting redirect...", "WAIT")
    try:
        page.wait_for_url(lambda u: 'grok.com' in u, timeout=30000)
        step_log(b_id, 8, "Redirect grok.com", "Grok Logged In ✓", "OK")
    except Exception as e:
        LOGGER.debug(f'redirect wait: {e}')
        try:
            sso = [c for c in page.context.cookies() if 'sso' in c.get('name', '').lower()]
        except Exception:
            sso = []
        if sso:
            step_log(b_id, 8, "Redirect grok.com", "SSO Cookies Found ✓", "OK")
        else:
            step_log(b_id, 8, "Redirect grok.com", "Checking SSO cookies...", "WAIT")
            time.sleep(2)
            try:
                sso = [c for c in page.context.cookies() if 'sso' in c.get('name', '').lower()]
            except Exception:
                sso = []
            if sso:
                step_log(b_id, 8, "Redirect grok.com", "SSO Cookies Found ✓", "OK")
            else:
                raise RuntimeError(f"no redirect (last: {getattr(page, 'url', 'closed')})")

    # 9 — 9Router OAuth (signup sudah OK; retry bila code kosong / timeout)
    STOP.check()
    step_log(b_id, 9, "9Router OAuth", "Logging in 9Router...", "WAIT")
    r9_success = False
    try:
        r9_success = do_9router_oauth(page, b_id=b_id, max_attempts=3)
    except Exception as e:
        if 'Stopped by user' in str(e):
            raise
        LOGGER.debug(f'9router oauth: {e}\n{traceback.format_exc()}')
        step_log(b_id, 9, "9Router OAuth", "OAuth error", "FAIL")

    # 10
    STOP.check()
    step_log(b_id, 10, "Save Credentials", f"Saved → {OUT.name}", "DONE", GRN)
    data = {
        'email': addr, 'password': PASSWORD, 'code': code,
        'sso_cookies': page.context.cookies(), 'final_url': page.url,
        'r9_success': r9_success,
        'timestamp': int(time.time()),
    }
    with file_lock:
        with open(OUT, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data) + '\n')
    return data

if __name__ == '__main__':
    main()
