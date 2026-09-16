#!/usr/bin/env python3
"""
app_gui.py — Graphite/gold dashboard · Grok Auto Signup Engine v4.2

Perf: Treeview (native) for accounts + live results — no 350× CTk row widgets.
Run:  python app_gui.py
Portable: dist\\GrokSignup\\GrokSignup.exe  (NOT build\\)
"""
import sys
import time
import queue
import threading
import re
import csv
from pathlib import Path
from datetime import datetime

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox

import importlib
import importlib.util


def _load_engine():
    roots = []
    if getattr(sys, 'frozen', False):
        roots.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parent)
    for root in roots:
        path = root / 'grok-signup.py'
        if path.exists():
            spec = importlib.util.spec_from_file_location('grok_signup_engine', path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules['grok_signup_engine'] = mod
            spec.loader.exec_module(mod)
            return mod
    return importlib.import_module('grok-signup')


engine = _load_engine()

# ── Graphite / champagne gold ─────────────────────────────────
BG = '#0A0A0B'
CARD = '#141416'
BORDER = '#2A2A2E'
ACCENT = '#C9A227'
SUCCESS = '#3DDC97'
DANGER = '#E85D5D'
WARN = '#E0A458'
MUTED = '#8B8B93'
TEXT = '#F2F2F3'
INPUT_BG = '#101012'
HEADER_BG = '#111113'
GOLD_SOFT = '#1A160C'
TREE_BG = '#0E0E10'
TREE_FG = '#E8E8EA'
TREE_SEL = '#2A2410'


def _style_treeview():
    style = ttk.Style()
    try:
        style.theme_use('clam')
    except Exception:
        pass
    style.configure(
        'Premium.Treeview',
        background=TREE_BG,
        foreground=TREE_FG,
        fieldbackground=TREE_BG,
        borderwidth=0,
        rowheight=28,
        font=('Segoe UI', 10),
    )
    style.configure(
        'Premium.Treeview.Heading',
        background='#161618',
        foreground=MUTED,
        relief='flat',
        font=('Segoe UI', 10, 'bold'),
        borderwidth=0,
    )
    style.map(
        'Premium.Treeview',
        background=[('selected', TREE_SEL)],
        foreground=[('selected', ACCENT)],
    )
    style.map('Premium.Treeview.Heading', background=[('active', '#1C1C1F')])
    style.layout('Premium.Treeview', [
        ('Premium.Treeview.treearea', {'sticky': 'nswe'}),
    ])


class GrokAppGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode('dark')
        ctk.set_default_color_theme('green')

        self.title('Grok Auto Signup · v4.2')
        self.geometry('1220x820')
        self.minsize(1040, 720)
        self.configure(fg_color=BG)

        self.is_running = False
        self.worker_thread = None
        self.event_queue = queue.Queue()
        self.env_path = engine.ROOT / '.env'
        self.env_data = {}
        self._session_start = None
        self._elapsed_job = None
        self._accounts = []
        self._acc_refresh_job = None
        self._activity_buf = []
        self._activity_flush_job = None

        _style_treeview()
        self.load_env_settings()
        self._build_ui()
        self.after(200, self._poll_events)   # lighter UI CPU when idle/running
        self.after(200, self.refresh_accounts)

    # ── env ───────────────────────────────────────────────────
    def load_env_settings(self):
        self.env_data = {}
        if self.env_path.exists():
            for line in self.env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    self.env_data[k.strip()] = v.strip()

    def save_env_settings(self):
        prov = self._mail_provider_key()
        lines = [
            '# Grok signup config — Generated via GUI App',
            f'PASSWORD={self.ent_account_pass.get().strip()}',
            f'ROUTER9_URL={self.ent_router_url.get().strip()}',
            f'ROUTER9_PASS={self.ent_router_pass.get().strip()}',
            f'MAIL_PROVIDER={prov}',
            f'MAILLDEZ_URL={self.ent_mail_url.get().strip()}',
            f'MAILLDEZ_DOMAINS={self.ent_mail_domain.get().strip()}',
            f'IMAP_HOST={self.ent_imap_host.get().strip()}',
            f'IMAP_PORT={self.ent_imap_port.get().strip() or "993"}',
            f'IMAP_USER={self.ent_imap_user.get().strip()}',
            f'IMAP_PASS={self.ent_imap_pass.get().strip()}',
            f'IMAP_SSL={"1" if self.var_imap_ssl.get() else "0"}',
        ]
        self.env_path.write_text('\n'.join(lines), encoding='utf-8')

    # provider UI labels → engine keys
    _PROV_CF = 'Cloudflare (Mailldez API)'
    _PROV_IMAP = 'IMAP / aaPanel (Server)'

    def _mail_provider_key(self):
        label = (self.cmb_mail_provider.get() if hasattr(self, 'cmb_mail_provider') else self._PROV_CF).strip().lower()
        if label.startswith('imap') or 'aapanel' in label or 'server' in label:
            return 'imap'
        return 'mailldez'

    def _mail_provider_label(self, key=None):
        k = (key or self.env_data.get('MAIL_PROVIDER', 'mailldez') or 'mailldez').strip().lower()
        return self._PROV_IMAP if k in ('imap', 'aapanel', 'cpanel', 'mailserver') else self._PROV_CF

    # ── UI helpers ────────────────────────────────────────────
    def _card(self, parent, **kw):
        opts = dict(fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
        opts.update(kw)
        return ctk.CTkFrame(parent, **opts)

    def _muted(self, parent, text, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=MUTED, font=ctk.CTkFont('Segoe UI', 12), **kw)

    def _section(self, parent, text):
        return ctk.CTkLabel(
            parent, text=text, text_color=TEXT,
            font=ctk.CTkFont('Segoe UI', 13, 'bold'), anchor='w',
        )

    def _entry(self, parent, show=None, placeholder=''):
        return ctk.CTkEntry(
            parent, height=34, corner_radius=8,
            fg_color=INPUT_BG, border_color=BORDER, border_width=1,
            text_color=TEXT, placeholder_text=placeholder, show=show or '',
            font=ctk.CTkFont('Segoe UI', 12),
        )

    def _nav_btn(self, parent, text, cmd):
        return ctk.CTkButton(
            parent, text=text, width=110, height=32, corner_radius=8,
            fg_color='transparent', hover_color=GOLD_SOFT,
            text_color=MUTED, border_width=0,
            font=ctk.CTkFont('Segoe UI', 12, 'bold'), command=cmd,
        )

    def _tree(self, parent, columns, headings, widths):
        """Native Treeview — scales to thousands of rows."""
        frame = tk.Frame(parent, bg=CARD, highlightthickness=0)
        tree = ttk.Treeview(
            frame, columns=columns, show='headings',
            style='Premium.Treeview', selectmode='browse',
        )
        vsb = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        for col, head, w in zip(columns, headings, widths):
            tree.heading(col, text=head, anchor='w')
            tree.column(col, width=w, minwidth=40, anchor='w', stretch=(col in ('email', 'account')))
        tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        tree.tag_configure('ok', foreground=SUCCESS)
        tree.tag_configure('fail', foreground=DANGER)
        tree.tag_configure('muted', foreground=MUTED)
        tree.tag_configure('alt', background='#121214')
        return frame, tree

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color=HEADER_BG, height=68, corner_radius=0)
        header.pack(fill='x')
        header.pack_propagate(False)

        left_h = ctk.CTkFrame(header, fg_color='transparent')
        left_h.pack(side='left', padx=22, pady=12)
        ctk.CTkLabel(
            left_h, text='GROK SIGNUP',
            font=ctk.CTkFont('Segoe UI', 18, 'bold'), text_color=ACCENT,
        ).pack(anchor='w')
        ctk.CTkLabel(
            left_h, text='Portable Engine · Camoufox · Multithread',
            font=ctk.CTkFont('Segoe UI', 11), text_color=MUTED,
        ).pack(anchor='w')

        nav = ctk.CTkFrame(header, fg_color='transparent')
        nav.pack(side='left', padx=30, pady=18)
        self.btn_nav_run = self._nav_btn(nav, 'Run', lambda: self._show_page('run'))
        self.btn_nav_run.pack(side='left', padx=4)
        self.btn_nav_acc = self._nav_btn(nav, 'Accounts', lambda: self._show_page('accounts'))
        self.btn_nav_acc.pack(side='left', padx=4)

        right_h = ctk.CTkFrame(header, fg_color='transparent')
        right_h.pack(side='right', padx=20, pady=16)
        ctk.CTkLabel(
            right_h, text='  v4.2  ', corner_radius=6,
            fg_color='#1C1C1F', text_color=MUTED,
            font=ctk.CTkFont('Segoe UI', 11, 'bold'),
        ).pack(side='left', padx=(0, 10))
        self.badge_status = ctk.CTkLabel(
            right_h, text='  READY  ', corner_radius=8,
            fg_color=GOLD_SOFT, text_color=ACCENT,
            font=ctk.CTkFont('Segoe UI', 12, 'bold'),
        )
        self.badge_status.pack(side='left')

        self.pages = ctk.CTkFrame(self, fg_color=BG)
        self.pages.pack(fill='both', expand=True, padx=14, pady=12)

        self.page_run = ctk.CTkFrame(self.pages, fg_color=BG)
        self.page_acc = ctk.CTkFrame(self.pages, fg_color=BG)

        self._build_run_page()
        self._build_accounts_page()
        self._show_page('run')

    def _show_page(self, name):
        self.page_run.pack_forget()
        self.page_acc.pack_forget()
        if name == 'accounts':
            self.page_acc.pack(fill='both', expand=True)
            self.btn_nav_acc.configure(text_color=ACCENT, fg_color=GOLD_SOFT)
            self.btn_nav_run.configure(text_color=MUTED, fg_color='transparent')
            self.refresh_accounts()
        else:
            self.page_run.pack(fill='both', expand=True)
            self.btn_nav_run.configure(text_color=ACCENT, fg_color=GOLD_SOFT)
            self.btn_nav_acc.configure(text_color=MUTED, fg_color='transparent')

    # ── RUN PAGE ──────────────────────────────────────────────
    def _build_run_page(self):
        body = self.page_run
        body.grid_columnconfigure(0, weight=0, minsize=340)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = self._card(body)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        left_scroll = ctk.CTkScrollableFrame(left, fg_color='transparent', corner_radius=0)
        left_scroll.pack(fill='both', expand=True, padx=14, pady=14)

        self._section(left_scroll, 'Task Settings').pack(anchor='w', pady=(0, 8))

        row = ctk.CTkFrame(left_scroll, fg_color='transparent')
        row.pack(fill='x', pady=4)
        self._muted(row, 'Total Accounts').pack(side='left')
        self.spn_total = ctk.CTkEntry(
            row, width=90, height=32, fg_color=INPUT_BG, border_color=BORDER,
            text_color=TEXT, justify='center',
        )
        self.spn_total.insert(0, '10')
        self.spn_total.pack(side='right')

        row2 = ctk.CTkFrame(left_scroll, fg_color='transparent')
        row2.pack(fill='x', pady=4)
        self._muted(row2, 'Parallel Browsers').pack(side='left')
        self.spn_concurrency = ctk.CTkEntry(
            row2, width=90, height=32, fg_color=INPUT_BG, border_color=BORDER,
            text_color=TEXT, justify='center',
        )
        self.spn_concurrency.insert(0, '1')  # 1 = lightest on PC; raise only if needed
        self.spn_concurrency.pack(side='right')

        ctk.CTkFrame(left_scroll, height=1, fg_color=BORDER).pack(fill='x', pady=14)

        self._section(left_scroll, 'Proxy').pack(anchor='w', pady=(0, 6))
        self.var_use_proxy = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            left_scroll, text='Enable Proxy', variable=self.var_use_proxy,
            command=self._toggle_proxy, progress_color=ACCENT, button_color=TEXT,
            font=ctk.CTkFont('Segoe UI', 12), text_color=TEXT,
        ).pack(anchor='w', pady=4)

        self.ent_proxy_server = self._entry(left_scroll, placeholder='http://host:port')
        self.ent_proxy_server.insert(0, 'http://127.0.0.1:8080')
        self.ent_proxy_server.pack(fill='x', pady=3)

        p_auth = ctk.CTkFrame(left_scroll, fg_color='transparent')
        p_auth.pack(fill='x', pady=3)
        self.ent_proxy_user = self._entry(p_auth, placeholder='Username')
        self.ent_proxy_user.pack(side='left', fill='x', expand=True, padx=(0, 4))
        self.ent_proxy_pass = self._entry(p_auth, show='*', placeholder='Password')
        self.ent_proxy_pass.pack(side='left', fill='x', expand=True, padx=(4, 0))

        ctk.CTkFrame(left_scroll, height=1, fg_color=BORDER).pack(fill='x', pady=14)

        self._section(left_scroll, 'Credentials (.env)').pack(anchor='w', pady=(0, 6))
        self._muted(left_scroll, 'Account Password').pack(anchor='w')
        self.ent_account_pass = self._entry(left_scroll, show='*')
        self.ent_account_pass.insert(0, self.env_data.get('PASSWORD', ''))
        self.ent_account_pass.pack(fill='x', pady=(2, 8))

        self._muted(left_scroll, '9Router URL').pack(anchor='w')
        self.ent_router_url = self._entry(left_scroll)
        self.ent_router_url.insert(0, self.env_data.get('ROUTER9_URL', ''))
        self.ent_router_url.pack(fill='x', pady=(2, 8))

        self._muted(left_scroll, '9Router Password').pack(anchor='w')
        self.ent_router_pass = self._entry(left_scroll, show='*')
        self.ent_router_pass.insert(0, self.env_data.get('ROUTER9_PASS', ''))
        self.ent_router_pass.pack(fill='x', pady=(2, 8))

        self._section(left_scroll, 'OTP Mail Provider').pack(anchor='w', pady=(8, 6))
        self._muted(left_scroll, 'Pilih sumber OTP email').pack(anchor='w')
        self.cmb_mail_provider = ctk.CTkOptionMenu(
            left_scroll,
            values=[self._PROV_CF, self._PROV_IMAP],
            command=lambda _=None: self._toggle_mail_fields(),
            fg_color=INPUT_BG, button_color=ACCENT, button_hover_color='#A8871F',
            text_color=TEXT, dropdown_fg_color=CARD, dropdown_hover_color=GOLD_SOFT,
            dropdown_text_color=TEXT, height=34,
            font=ctk.CTkFont('Segoe UI', 12),
        )
        self.cmb_mail_provider.set(self._mail_provider_label())
        self.cmb_mail_provider.pack(fill='x', pady=(2, 6))

        self.lbl_prov_badge = ctk.CTkLabel(
            left_scroll, text='', corner_radius=6,
            fg_color=GOLD_SOFT, text_color=ACCENT,
            font=ctk.CTkFont('Segoe UI', 11, 'bold'), anchor='w',
        )
        self.lbl_prov_badge.pack(fill='x', pady=(0, 8))

        # Shared domain(s) — dipakai kedua provider
        self._muted(left_scroll, 'Mail Domains (comma-separated)').pack(anchor='w')
        self.ent_mail_domain = self._entry(left_scroll, placeholder='itbaarts.net  atau  domain.cf')
        self.ent_mail_domain.insert(0, self.env_data.get('MAILLDEZ_DOMAINS', ''))
        self.ent_mail_domain.pack(fill='x', pady=(2, 8))

        # Cloudflare / Mailldez block
        self.frame_mailldez = ctk.CTkFrame(left_scroll, fg_color='transparent')
        self.frame_mailldez.pack(fill='x')
        self._muted(self.frame_mailldez, 'Cloudflare Worker / Mailldez API URL').pack(anchor='w')
        self.ent_mail_url = self._entry(self.frame_mailldez, placeholder='https://….workers.dev')
        self.ent_mail_url.insert(0, self.env_data.get('MAILLDEZ_URL', ''))
        self.ent_mail_url.pack(fill='x', pady=(2, 4))
        ctk.CTkLabel(
            self.frame_mailldez,
            text='Cloudflare: Workers catch-all API (Mailldez-compatible).\nDomain di atas = domain yang di-handle worker.',
            text_color=MUTED, font=ctk.CTkFont('Segoe UI', 10), justify='left', anchor='w',
        ).pack(anchor='w', pady=(0, 6))

        # IMAP / aaPanel block
        self.frame_imap = ctk.CTkFrame(left_scroll, fg_color='transparent')
        self.frame_imap.pack(fill='x')
        self._muted(self.frame_imap, 'IMAP Host').pack(anchor='w')
        self.ent_imap_host = self._entry(self.frame_imap, placeholder='mail.itbaarts.net')
        self.ent_imap_host.insert(0, self.env_data.get('IMAP_HOST', ''))
        self.ent_imap_host.pack(fill='x', pady=(2, 6))

        row_imap = ctk.CTkFrame(self.frame_imap, fg_color='transparent')
        row_imap.pack(fill='x', pady=2)
        self._muted(row_imap, 'Port').pack(side='left')
        self.ent_imap_port = ctk.CTkEntry(
            row_imap, width=70, height=32, fg_color=INPUT_BG, border_color=BORDER,
            text_color=TEXT, justify='center',
        )
        self.ent_imap_port.insert(0, self.env_data.get('IMAP_PORT', '993'))
        self.ent_imap_port.pack(side='right')

        self._muted(self.frame_imap, 'IMAP User (mailbox catch-all)').pack(anchor='w')
        self.ent_imap_user = self._entry(self.frame_imap, placeholder='catchall@itbaarts.net')
        self.ent_imap_user.insert(0, self.env_data.get('IMAP_USER', ''))
        self.ent_imap_user.pack(fill='x', pady=(2, 6))

        self._muted(self.frame_imap, 'IMAP Password').pack(anchor='w')
        self.ent_imap_pass = self._entry(self.frame_imap, show='*')
        self.ent_imap_pass.insert(0, self.env_data.get('IMAP_PASS', ''))
        self.ent_imap_pass.pack(fill='x', pady=(2, 6))

        self.var_imap_ssl = ctk.BooleanVar(value=self.env_data.get('IMAP_SSL', '1') not in ('0', 'false', 'False'))
        ctk.CTkSwitch(
            self.frame_imap, text='IMAP SSL (port 993)', variable=self.var_imap_ssl,
            progress_color=ACCENT, button_color=TEXT,
            font=ctk.CTkFont('Segoe UI', 12), text_color=TEXT,
        ).pack(anchor='w', pady=(2, 4))

        self.lbl_imap_hint = ctk.CTkLabel(
            self.frame_imap,
            text='aaPanel: Mail → Catch-all → mailbox di atas.\nMX domain harus ke server. Login = catchall@…',
            text_color=MUTED, font=ctk.CTkFont('Segoe UI', 10), justify='left', anchor='w',
        )
        self.lbl_imap_hint.pack(anchor='w', pady=(2, 4))

        self._toggle_mail_fields()

        act = ctk.CTkFrame(left_scroll, fg_color='transparent')
        act.pack(fill='x', pady=(16, 4))
        self.btn_start = ctk.CTkButton(
            act, text='Start', height=40, corner_radius=10,
            fg_color=ACCENT, hover_color='#A8871F', text_color='#1A1405',
            font=ctk.CTkFont('Segoe UI', 13, 'bold'), command=self.start_process,
        )
        self.btn_start.pack(side='left', fill='x', expand=True, padx=(0, 6))
        self.btn_stop = ctk.CTkButton(
            act, text='Stop', height=40, corner_radius=10, state='disabled',
            fg_color='#2A1515', hover_color=DANGER, text_color=DANGER,
            font=ctk.CTkFont('Segoe UI', 13, 'bold'), command=self.stop_process,
        )
        self.btn_stop.pack(side='left', fill='x', expand=True, padx=(6, 0))
        self._toggle_proxy()

        right = ctk.CTkFrame(body, fg_color='transparent')
        right.grid(row=0, column=1, sticky='nsew')
        right.grid_rowconfigure(2, weight=1)
        right.grid_columnconfigure(0, weight=1)

        stats = ctk.CTkFrame(right, fg_color='transparent')
        stats.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        for i in range(4):
            stats.grid_columnconfigure(i, weight=1)

        self.stat_ok = self._stat_card(stats, 'Success', '0', SUCCESS)
        self.stat_ok.grid(row=0, column=0, sticky='ew', padx=(0, 6))
        self.stat_fail = self._stat_card(stats, 'Failed', '0', DANGER)
        self.stat_fail.grid(row=0, column=1, sticky='ew', padx=6)
        self.stat_run = self._stat_card(stats, 'Running', '0', ACCENT)
        self.stat_run.grid(row=0, column=2, sticky='ew', padx=6)
        self.stat_time = self._stat_card(stats, 'Elapsed', '0s', MUTED)
        self.stat_time.grid(row=0, column=3, sticky='ew', padx=(6, 0))

        prog_card = self._card(right)
        prog_card.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        pf = ctk.CTkFrame(prog_card, fg_color='transparent')
        pf.pack(fill='x', padx=16, pady=12)
        self.lbl_progress = ctk.CTkLabel(
            pf, text='Selesai 0/0 · Sukses 0',
            font=ctk.CTkFont('Segoe UI', 12), text_color=MUTED, anchor='w',
        )
        self.lbl_progress.pack(fill='x')
        self.progress = ctk.CTkProgressBar(
            pf, height=10, corner_radius=6,
            fg_color=INPUT_BG, progress_color=ACCENT,
        )
        self.progress.pack(fill='x', pady=(8, 0))
        self.progress.set(0)

        bottom = ctk.CTkFrame(right, fg_color='transparent')
        bottom.grid(row=2, column=0, sticky='nsew')
        bottom.grid_columnconfigure(0, weight=3)
        bottom.grid_columnconfigure(1, weight=2)
        bottom.grid_rowconfigure(0, weight=1)

        table_card = self._card(bottom)
        table_card.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        ctk.CTkLabel(
            table_card, text='Live Results',
            font=ctk.CTkFont('Segoe UI', 13, 'bold'), text_color=TEXT, anchor='w',
        ).pack(fill='x', padx=14, pady=(12, 4))
        live_host = ctk.CTkFrame(table_card, fg_color=CARD)
        live_host.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.live_frame, self.live_tree = self._tree(
            live_host,
            columns=('num', 'account', 'status', 'time'),
            headings=('#', 'Account', 'Status', 'Time'),
            widths=(50, 220, 100, 80),
        )
        self.live_frame.pack(fill='both', expand=True)

        act_card = self._card(bottom)
        act_card.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        ctk.CTkLabel(
            act_card, text='Activity',
            font=ctk.CTkFont('Segoe UI', 13, 'bold'), text_color=TEXT, anchor='w',
        ).pack(fill='x', padx=14, pady=(12, 4))
        # tk.Text faster than CTkTextbox for high-frequency inserts
        act_wrap = tk.Frame(act_card, bg=INPUT_BG, highlightthickness=0)
        act_wrap.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        self.txt_activity = tk.Text(
            act_wrap, bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
            font=('Consolas', 10), relief='flat', borderwidth=0,
            wrap='word', state='disabled', highlightthickness=0,
        )
        act_sb = ttk.Scrollbar(act_wrap, orient='vertical', command=self.txt_activity.yview)
        self.txt_activity.configure(yscrollcommand=act_sb.set)
        self.txt_activity.pack(side='left', fill='both', expand=True)
        act_sb.pack(side='right', fill='y')
        self.txt_activity.tag_configure('ok', foreground=SUCCESS)
        self.txt_activity.tag_configure('fail', foreground=DANGER)
        self.txt_activity.tag_configure('warn', foreground=WARN)
        self.txt_activity.tag_configure('info', foreground=MUTED)
        self.txt_activity.tag_configure('step', foreground=TEXT)
        self._activity('Ready. Configure & press Start.')

    # ── ACCOUNTS PAGE ─────────────────────────────────────────
    def _build_accounts_page(self):
        page = self.page_acc
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)

        bar = self._card(page)
        bar.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        inner = ctk.CTkFrame(bar, fg_color='transparent')
        inner.pack(fill='x', padx=14, pady=12)

        self.lbl_acc_count = ctk.CTkLabel(
            inner, text='0 accounts',
            font=ctk.CTkFont('Segoe UI', 14, 'bold'), text_color=ACCENT,
        )
        self.lbl_acc_count.pack(side='left')
        self.lbl_acc_path = ctk.CTkLabel(
            inner, text=str(engine.OUT),
            font=ctk.CTkFont('Consolas', 10), text_color=MUTED,
        )
        self.lbl_acc_path.pack(side='left', padx=16)

        ctk.CTkButton(
            inner, text='Export CSV', width=100, height=32, corner_radius=8,
            fg_color='#1C1C1F', hover_color=BORDER, text_color=TEXT,
            font=ctk.CTkFont('Segoe UI', 12), command=self.export_accounts_csv,
        ).pack(side='right', padx=(6, 0))
        ctk.CTkButton(
            inner, text='Copy emails', width=110, height=32, corner_radius=8,
            fg_color='#1C1C1F', hover_color=BORDER, text_color=TEXT,
            font=ctk.CTkFont('Segoe UI', 12), command=self.copy_emails,
        ).pack(side='right', padx=6)
        ctk.CTkButton(
            inner, text='Copy row', width=90, height=32, corner_radius=8,
            fg_color='#1C1C1F', hover_color=BORDER, text_color=ACCENT,
            font=ctk.CTkFont('Segoe UI', 12), command=self.copy_selected_account,
        ).pack(side='right', padx=6)
        ctk.CTkButton(
            inner, text='Refresh', width=90, height=32, corner_radius=8,
            fg_color=ACCENT, hover_color='#A8871F', text_color='#1A1405',
            font=ctk.CTkFont('Segoe UI', 12, 'bold'), command=self.refresh_accounts,
        ).pack(side='right', padx=6)

        filt = self._card(page)
        filt.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        fi = ctk.CTkFrame(filt, fg_color='transparent')
        fi.pack(fill='x', padx=14, pady=10)

        self.ent_search = self._entry(fi, placeholder='Search email…')
        self.ent_search.pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.ent_search.bind('<KeyRelease>', lambda e: self._schedule_acc_render())

        self.var_r9_only = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            fi, text='9Router OK only', variable=self.var_r9_only,
            command=self._schedule_acc_render, progress_color=ACCENT, button_color=TEXT,
            font=ctk.CTkFont('Segoe UI', 12), text_color=TEXT, width=160,
        ).pack(side='left', padx=8)

        self.var_show_pass = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            fi, text='Show password', variable=self.var_show_pass,
            command=self._schedule_acc_render, progress_color=ACCENT, button_color=TEXT,
            font=ctk.CTkFont('Segoe UI', 12), text_color=TEXT, width=150,
        ).pack(side='left', padx=8)

        list_card = self._card(page)
        list_card.grid(row=2, column=0, sticky='nsew')
        host = ctk.CTkFrame(list_card, fg_color=CARD)
        host.pack(fill='both', expand=True, padx=10, pady=10)
        self.acc_frame, self.acc_tree = self._tree(
            host,
            columns=('num', 'email', 'password', 'r9', 'date', 'cookies'),
            headings=('#', 'Email', 'Password', '9Router', 'Date', 'Cookies'),
            widths=(50, 280, 150, 80, 140, 70),
        )
        self.acc_frame.pack(fill='both', expand=True)
        self.acc_tree.bind('<Double-1>', lambda e: self.copy_selected_account())

    def refresh_accounts(self):
        self._accounts = engine.load_sso_accounts()
        self.lbl_acc_count.configure(text=f'{len(self._accounts)} accounts')
        self.lbl_acc_path.configure(text=str(engine.OUT))
        self._render_accounts()

    def _schedule_acc_render(self):
        if self._acc_refresh_job:
            try:
                self.after_cancel(self._acc_refresh_job)
            except Exception:
                pass
        self._acc_refresh_job = self.after(120, self._render_accounts)

    def _render_accounts(self):
        self._acc_refresh_job = None
        tree = self.acc_tree
        tree.delete(*tree.get_children())

        q = (self.ent_search.get() if hasattr(self, 'ent_search') else '').strip().lower()
        r9_only = self.var_r9_only.get() if hasattr(self, 'var_r9_only') else False
        show_pw = self.var_show_pass.get() if hasattr(self, 'var_show_pass') else False

        # newest first; keep original 1-based index
        items = list(enumerate(self._accounts, start=1))
        items.reverse()

        rows = []
        for idx, acc in items:
            email = str(acc.get('email', ''))
            if q and q not in email.lower():
                continue
            r9 = bool(acc.get('r9_success'))
            if r9_only and not r9:
                continue
            pw = str(acc.get('password', ''))
            pw_disp = pw if show_pw else ('•' * min(12, max(6, len(pw) or 6)))
            ts = acc.get('timestamp')
            try:
                date_s = datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M') if ts else '—'
            except Exception:
                date_s = '—'
            cookies = acc.get('sso_cookies') or []
            n_ck = len(cookies) if isinstance(cookies, list) else 0
            tag = 'ok' if r9 else 'muted'
            rows.append((
                str(idx),
                (f'{idx:03d}', email, pw_disp, 'OK' if r9 else '—', date_s, str(n_ck)),
                (tag, 'alt') if idx % 2 == 0 else (tag,),
            ))

        # bulk insert
        for iid, values, tags in rows:
            tree.insert('', 'end', iid=iid, values=values, tags=tags)

        self.lbl_acc_count.configure(text=f'{len(rows)} / {len(self._accounts)} accounts')

    def copy_selected_account(self):
        sel = self.acc_tree.selection()
        if not sel:
            messagebox.showinfo('Accounts', 'Pilih baris dulu.')
            return
        try:
            idx = int(sel[0]) - 1
            acc = self._accounts[idx]
        except Exception:
            messagebox.showinfo('Accounts', 'Baris tidak valid.')
            return
        email = acc.get('email', '')
        pw = acc.get('password', '')
        self.clipboard_clear()
        self.clipboard_append(f'{email}:{pw}')
        self._activity(f'Copied {email}', 'ok')

    def copy_emails(self):
        emails = [a.get('email', '') for a in self._accounts if a.get('email')]
        if not emails:
            messagebox.showinfo('Accounts', 'Tidak ada email.')
            return
        self.clipboard_clear()
        self.clipboard_append('\n'.join(emails))
        messagebox.showinfo('Accounts', f'{len(emails)} emails disalin.')

    def export_accounts_csv(self):
        if not self._accounts:
            messagebox.showinfo('Export', 'Tidak ada akun.')
            return
        out = engine.ROOT / f"accounts-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        with open(out, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['email', 'password', 'r9_success', 'timestamp', 'final_url', 'cookies_count'])
            for a in self._accounts:
                ck = a.get('sso_cookies') or []
                w.writerow([
                    a.get('email', ''),
                    a.get('password', ''),
                    a.get('r9_success', False),
                    a.get('timestamp', ''),
                    a.get('final_url', ''),
                    len(ck) if isinstance(ck, list) else 0,
                ])
        messagebox.showinfo('Export', f'Disimpan:\n{out}')

    # ── shared ────────────────────────────────────────────────
    def _stat_card(self, parent, title, value, color):
        card = self._card(parent)
        inner = ctk.CTkFrame(card, fg_color='transparent')
        inner.pack(fill='both', expand=True, padx=12, pady=12)
        ctk.CTkLabel(
            inner, text=title, text_color=MUTED,
            font=ctk.CTkFont('Segoe UI', 11), anchor='w',
        ).pack(anchor='w')
        lbl = ctk.CTkLabel(
            inner, text=value, text_color=color,
            font=ctk.CTkFont('Segoe UI', 22, 'bold'), anchor='w',
        )
        lbl.pack(anchor='w', pady=(2, 0))
        card._value_lbl = lbl
        return card

    def _set_stat(self, card, value):
        card._value_lbl.configure(text=str(value))

    def _toggle_proxy(self):
        state = 'normal' if self.var_use_proxy.get() else 'disabled'
        for w in (self.ent_proxy_server, self.ent_proxy_user, self.ent_proxy_pass):
            w.configure(state=state)

    def _toggle_mail_fields(self):
        if not hasattr(self, 'frame_mailldez'):
            return
        key = self._mail_provider_key()
        if key == 'imap':
            self.frame_mailldez.pack_forget()
            self.frame_imap.pack(fill='x')
            if hasattr(self, 'lbl_prov_badge'):
                self.lbl_prov_badge.configure(
                    text='  MODE: IMAP / aaPanel  ·  OTP dari inbox catch-all  ',
                    fg_color='#1A2418', text_color=SUCCESS,
                )
        else:
            self.frame_imap.pack_forget()
            self.frame_mailldez.pack(fill='x')
            if hasattr(self, 'lbl_prov_badge'):
                self.lbl_prov_badge.configure(
                    text='  MODE: Cloudflare / Mailldez  ·  OTP via Workers API  ',
                    fg_color=GOLD_SOFT, text_color=ACCENT,
                )

    def _set_status(self, text, kind='ready'):
        colors = {
            'ready': (GOLD_SOFT, ACCENT),
            'run': ('#0F2A1C', SUCCESS),
            'stop': ('#2A1515', DANGER),
            'warn': ('#2A2010', WARN),
        }
        bg, fg = colors.get(kind, colors['ready'])
        self.badge_status.configure(text=f'  {text}  ', fg_color=bg, text_color=fg)

    def _activity(self, msg, level='info'):
        if not msg or engine.is_noise_line(msg):
            return
        clean = re.sub(r'\033\[[0-9;]*m', '', str(msg)).strip()
        if not clean or engine.is_noise_line(clean):
            return
        ts = time.strftime('%H:%M:%S')
        prefix = {'info': '·', 'ok': '✓', 'fail': '✗', 'warn': '!', 'step': '›'}.get(level, '·')
        self._activity_buf.append((f'{ts}  {prefix}  {clean}\n', level if level in ('ok', 'fail', 'warn', 'step', 'info') else 'info'))
        if self._activity_flush_job is None:
            self._activity_flush_job = self.after(100, self._flush_activity)

    def _flush_activity(self):
        self._activity_flush_job = None
        if not self._activity_buf:
            return
        batch = self._activity_buf
        self._activity_buf = []
        self.txt_activity.configure(state='normal')
        for line, tag in batch:
            self.txt_activity.insert('end', line, tag)
        # cap log size
        try:
            lines = int(self.txt_activity.index('end-1c').split('.')[0])
            if lines > 800:
                self.txt_activity.delete('1.0', f'{lines - 600}.0')
        except Exception:
            pass
        self.txt_activity.see('end')
        self.txt_activity.configure(state='disabled')

    def _live_upsert(self, index, email='', status='…', elapsed='', ok=None):
        iid = str(index)
        if ok is True:
            st, tag = 'SUCCESS', 'ok'
        elif ok is False:
            st, tag = 'FAILED', 'fail'
        else:
            st, tag = status, 'muted'
        vals = (f'{index:03d}', (email or '—')[:40], st, elapsed or '—')
        if self.live_tree.exists(iid):
            self.live_tree.item(iid, values=vals, tags=(tag,))
        else:
            self.live_tree.insert('', 'end', iid=iid, values=vals, tags=(tag,))
            self.live_tree.see(iid)

    def _clear_live(self):
        self.live_tree.delete(*self.live_tree.get_children())

    # ── control ───────────────────────────────────────────────
    def start_process(self):
        if self.is_running:
            return
        try:
            total_acc = int(self.spn_total.get().strip())
            concurrency = int(self.spn_concurrency.get().strip())
            if total_acc < 1 or concurrency < 1:
                raise ValueError
            if concurrency > 3:
                if not messagebox.askyesno(
                    'Resource warning',
                    f'Parallel {concurrency} browser sangat membebani PC.\n\n'
                    'Saran: 1 (ringan) atau max 2–3.\n\nLanjut?',
                ):
                    return
        except ValueError:
            messagebox.showerror('Input Error', 'Total accounts & parallel browsers harus angka positif.')
            return

        self.save_env_settings()
        engine.PASSWORD = self.ent_account_pass.get().strip()
        engine.ROUTER9 = self.ent_router_url.get().strip()
        engine.ROUTER9_PASS = self.ent_router_pass.get().strip()
        engine.MAIL_PROVIDER = self._mail_provider_key()
        engine.MAILLDEZ = self.ent_mail_url.get().strip()
        engine.DOMAINS = [d.strip() for d in self.ent_mail_domain.get().strip().split(',') if d.strip()]
        engine.IMAP_HOST = self.ent_imap_host.get().strip()
        try:
            engine.IMAP_PORT = int(self.ent_imap_port.get().strip() or '993')
        except ValueError:
            engine.IMAP_PORT = 993
        engine.IMAP_USER = self.ent_imap_user.get().strip()
        engine.IMAP_PASS = self.ent_imap_pass.get().strip()
        engine.IMAP_SSL = bool(self.var_imap_ssl.get())

        if engine.MAIL_PROVIDER == 'imap':
            if not engine.IMAP_HOST or not engine.IMAP_USER or not engine.IMAP_PASS:
                messagebox.showerror('IMAP', 'IMAP Host / User / Password wajib diisi (aaPanel catch-all).')
                return
            if not engine.DOMAINS:
                messagebox.showerror('IMAP', 'Mail Domains wajib diisi (domain di aaPanel).')
                return
        else:
            if not engine.MAILLDEZ:
                messagebox.showerror('Cloudflare', 'Mailldez / Cloudflare Worker URL wajib diisi.')
                return
            if not engine.DOMAINS:
                messagebox.showerror('Cloudflare', 'Mail Domains wajib diisi.')
                return

        self._activity(
            f"OTP provider: {'IMAP/aaPanel' if engine.MAIL_PROVIDER == 'imap' else 'Cloudflare/Mailldez'}"
            f" · domains={','.join(engine.DOMAINS)}",
            'info',
        )

        proxy_config = None
        if self.var_use_proxy.get():
            server_str = self.ent_proxy_server.get().strip()
            if server_str:
                proxy_config = {'server': server_str}
                u, p = self.ent_proxy_user.get().strip(), self.ent_proxy_pass.get().strip()
                if u and p:
                    proxy_config['username'] = u
                    proxy_config['password'] = p

        while not self.event_queue.empty():
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break
        self._clear_live()
        self.txt_activity.configure(state='normal')
        self.txt_activity.delete('1.0', 'end')
        self.txt_activity.configure(state='disabled')
        self._set_stat(self.stat_ok, 0)
        self._set_stat(self.stat_fail, 0)
        self._set_stat(self.stat_run, min(concurrency, total_acc))
        self._set_stat(self.stat_time, '0s')
        self.progress.set(0)
        self.lbl_progress.configure(text=f'Selesai 0/{total_acc} · Sukses 0')

        self.is_running = True
        self.btn_start.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self._set_status('RUNNING', 'run')
        self._session_start = time.time()
        self._tick_elapsed()

        self.worker_thread = threading.Thread(
            target=self._run_engine,
            args=(total_acc, concurrency, proxy_config),
            daemon=True,
        )
        self.worker_thread.start()

    def _run_engine(self, total, concurrency, proxy_config):
        try:
            engine.run_batch(total, concurrency, proxy_config=proxy_config, event_queue=self.event_queue)
        except Exception as e:
            engine.LOGGER.error(f'GUI batch fatal: {e}')
            self.event_queue.put({
                'type': 'log', 'level': 'error',
                'message': f'Fatal: {engine._friendly_error(str(e))}',
            })
            self.event_queue.put({'type': 'session', 'status': 'error', 'error': str(e)})
        finally:
            self.after(0, self._on_finished)

    def stop_process(self):
        if not self.is_running:
            return
        engine.STOP.request_stop()
        self._set_status('STOPPING', 'stop')
        self._activity('Stopping… closing browsers', 'warn')
        self.btn_stop.configure(state='disabled')

    def _on_finished(self):
        self.is_running = False
        self.btn_start.configure(state='normal')
        self.btn_stop.configure(state='disabled')
        self._set_status('READY', 'ready')
        self._set_stat(self.stat_run, 0)
        if self._elapsed_job:
            try:
                self.after_cancel(self._elapsed_job)
            except Exception:
                pass
            self._elapsed_job = None
        self.refresh_accounts()

    def _tick_elapsed(self):
        if self._session_start and self.is_running:
            sec = int(time.time() - self._session_start)
            self._set_stat(self.stat_time, f'{sec}s')
            self._elapsed_job = self.after(1000, self._tick_elapsed)

    # ── event consumer ────────────────────────────────────────
    def _poll_events(self):
        try:
            n = 0
            while n < 40:
                ev = self.event_queue.get_nowait()
                self._handle_event(ev)
                n += 1
        except queue.Empty:
            pass
        self.after(200, self._poll_events)

    def _handle_event(self, ev):
        if not isinstance(ev, dict):
            return
        et = ev.get('type')

        if et == 'log':
            level = ev.get('level', 'info')
            map_lvl = {'error': 'fail', 'warn': 'warn', 'info': 'info'}.get(level, 'info')
            self._activity(ev.get('message', ''), map_lvl)

        elif et == 'step':
            b = ev.get('browser', 0)
            name = ev.get('name', '')
            status = ev.get('status', '')
            detail = ev.get('detail', '')
            if status == 'WAIT' and 'Waiting (' in (detail or ''):
                return
            lvl = 'ok' if status in ('OK', 'DONE') else ('fail' if status == 'FAIL' else 'step')
            bit = f'[B{b:02d}] {name}'
            if detail:
                bit += f' — {detail}'
            self._activity(bit, lvl)

        elif et == 'progress':
            c = int(ev.get('completed', 0))
            o = int(ev.get('success', 0))
            f = int(ev.get('failed', c - o))
            total = max(1, int(ev.get('total', 1)))
            self._set_stat(self.stat_ok, o)
            self._set_stat(self.stat_fail, f)
            try:
                conc = int(self.spn_concurrency.get() or 1)
            except ValueError:
                conc = 1
            running = max(0, min(conc, total - c)) if self.is_running else 0
            self._set_stat(self.stat_run, running)
            self.progress.set(c / total)
            self.lbl_progress.configure(text=f'Selesai {c}/{total} · Sukses {o}')

        elif et == 'account_ok':
            idx = int(ev.get('index', 0))
            email = ev.get('email', '')
            self._live_upsert(idx, email=email, ok=True, elapsed=ev.get('elapsed', ''))
            self._activity(f'#{idx:03d} success {email}', 'ok')

        elif et == 'account_fail':
            idx = int(ev.get('index', 0))
            err = ev.get('error', 'failed')
            if engine.is_noise_line(err):
                err = 'UI interaction timeout'
            self._live_upsert(idx, email=ev.get('email') or '—', ok=False, elapsed=ev.get('elapsed', ''))
            self._activity(f'#{idx:03d} failed — {err}', 'fail')

        elif et == 'session':
            st = ev.get('status')
            if st == 'start':
                self._activity(
                    f"Session · {ev.get('total')} accounts · {ev.get('concurrency')} browsers",
                    'info',
                )
            elif st == 'done':
                self._activity(f"Done · {ev.get('ok', 0)}/{ev.get('total', 0)} success", 'ok')
            elif st == 'error':
                err = engine._friendly_error(str(ev.get('error', 'error')))
                self._activity(f'Session error — {err}', 'fail')


def main():
    import os

    class _NullIO:
        def write(self, *a, **k): return 0
        def flush(self, *a, **k): pass
        def isatty(self): return False
        def reconfigure(self, *a, **k): pass

    # Windowed PyInstaller exe: stdout/stderr may be None
    if sys.stdout is None:
        sys.stdout = _NullIO()
    if sys.stderr is None:
        sys.stderr = _NullIO()
    if sys.platform == 'win32':
        try:
            if hasattr(sys.stdout, 'reconfigure'):
                sys.stdout.reconfigure(encoding='utf-8')
            if hasattr(sys.stderr, 'reconfigure'):
                sys.stderr.reconfigure(encoding='utf-8')
        except Exception:
            pass
    try:
        os.chdir(engine.ROOT)
    except Exception:
        pass
    # engine also needs safe stdio (imported before main)
    try:
        engine._ensure_stdio()
    except Exception:
        pass
    app = GrokAppGUI()
    app.mainloop()


if __name__ == '__main__':
    main()
