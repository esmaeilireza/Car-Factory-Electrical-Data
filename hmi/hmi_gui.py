"""
Optimized HMI Simulator - Delta DOP-B style for 6 equipment.

This version:
- Corrects Modbus register mapping to match data_generator.py.
- Removes the old bit-15 lockout hack.
- Uses dedicated lockout register HR[offset+14].
- Uses correct motor_state interpretation.
- Adds NEXUS AI Advisor panel.
- Adds operator presence control.
- Uses background threads for HTTP requests to avoid UI freezing.
- Shows heartbeat and trip count in diagnostics.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Dict, Optional

from pymodbus.client import ModbusTcpClient

try:
    import requests
except Exception:
    requests = None


# =============================================================================
# Equipment and ANSI definitions
# =============================================================================

EQUIPMENT_LIST = [
    ("STP-01", "Stamping Press", 0),
    ("WLD-01", "Welding Robots", 18),
    ("PNT-01", "Paint Booth", 36),
    ("ASM-01", "Assembly", 54),
    ("UTI-01", "Compressor", 72),
    ("UTI-02", "Chiller", 90),
]

EQUIPMENT_ORDER = [eq[0] for eq in EQUIPMENT_LIST]

ANSI_NAMES = [
    "49 Thermal",
    "50 Inst OC",
    "51 Time OC",
    "27 Undervolt",
    "59 Overvolt",
    "38 Overtemp",
    "46 Phase Imb",
    "37 Loss Load",
]


# =============================================================================
# Modbus register map per equipment
# =============================================================================
#
# Matches data_generator.py:
# [0]  Voltage        V * 10
# [1]  Current        A * 10
# [2]  Active Power   kW * 10
# [3]  Reactive Power kVAR * 10
# [4]  Apparent Power kVA * 10
# [5]  Power Factor   pf * 100
# [6]  Frequency      Hz * 10
# [7]  Energy         kWh * 100
# [8]  Motor State    0=STOP, 1=RUN, 2=PENDING, 3=LOCKOUT
# [9]  Alarm Flag     1=any alarm
# [10] Trip Word      ANSI fault bitmask
# [11] Running Time   minutes
# [12] Load           percent
# [13] Alarm Word     ANSI warning bitmask
# [14] Lockout Status 1=latched, 0=clear
# [15] Theta          thermal capacity * 1000
# [16] Trip Count     cumulative
# [17] Heartbeat      watchdog counter
#

REG_VOLTAGE = 0
REG_CURRENT = 1
REG_ACTIVE_POWER = 2
REG_REACTIVE_POWER = 3
REG_APPARENT_POWER = 4
REG_POWER_FACTOR = 5
REG_FREQUENCY = 6
REG_ENERGY = 7
REG_MOTOR_STATE = 8
REG_ALARM_FLAG = 9
REG_TRIP_WORD = 10
REG_RUNNING_TIME = 11
REG_LOAD = 12
REG_ALARM_WORD = 13
REG_LOCKOUT_STATUS = 14
REG_THETA_PM = 15
REG_TRIP_COUNT = 16
REG_HEARTBEAT = 17

REGS_PER_EQUIPMENT = 18


# =============================================================================
# System status and coils
# =============================================================================

SYS_STATUS_ADDR = 120
SYS_STATUS_ESTOP_BIT = 0
SYS_STATUS_ANY_TRIP_BIT = 1

COIL_ESTOP = 6
COIL_RESET = 7

POLL_INTERVAL_MS = 500
START_PENDING_TIMEOUT_SEC = 8.0


# =============================================================================
# API configuration
# =============================================================================

API_URL = os.environ.get("NEXUS_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("NEXUS_API_KEY", "")


# =============================================================================
# Color palette
# =============================================================================

COLORS = {
    "bg_dark": "#1a1a2e",
    "bg_panel": "#16213e",
    "bg_header": "#0f3460",
    "text_light": "#a0a0a0",
    "text_white": "white",
    "cyan": "#00E5FF",
    "green": "#00E676",
    "red": "#FF1744",
    "amber": "#FFB300",
    "orange": "orange",
    "gray": "#a0a0a0",
    "purple": "#bb86fc",
    "estop_bg": "#3d0000",
    "normal_bg": "#16213e",
}


# =============================================================================
# Main HMI application
# =============================================================================

class DeltaHMISimulator:
    """
    Delta DOP-B style HMI simulator with NEXUS AI advisor panel.
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Delta DOP-B HMI - Car Factory + NEXUS AI")
        self.root.state("zoomed")
        self.root.configure(bg=COLORS["bg_dark"])

        self.client = ModbusTcpClient(
            host="127.0.0.1",
            port=5020,
            timeout=5,
        )

        self.connected = False
        self.running = True
        self._blink = False

        # System-level flags
        self.estop_active = False
        self.any_trip_active = False
        self.is_latched = False

        # Current equipment state cache
        self.motor_state = 0
        self.trip_word = 0
        self.alarm_word = 0
        self.lockout_status = 0
        self.heartbeat = 0
        self.trip_count = 0

        # START PENDING tracking
        self._start_pending_times: Dict[str, float] = {}

        # AI advisor state
        self.operator_present = True
        self.last_ai_status: Optional[Dict[str, Any]] = None
        self._ai_fetch_in_progress = False
        self._updating_presence = False
        self.ai_queue = queue.Queue()


        self.create_ui()
        self.connect_plc()
        self.update_display()
        self.start_ai_polling()

    # =========================================================================
    # UI construction
    # =========================================================================

    def create_ui(self):
        self._create_title_bar()
        self._create_estop_banner()
        self._create_connection_status()
        self._create_equipment_selector()
        self._create_main_display()
        self._create_protection_panel()
        self._create_controls()
        self._create_alarm_label()
        self._create_ai_panel()
        self._create_debug_panel()
        self._create_footer()

    def _create_title_bar(self):
        title_frame = tk.Frame(
            self.root,
            bg=COLORS["bg_header"],
            height=70,
            bd=2,
            relief=tk.RAISED,
        )
        title_frame.pack(fill=tk.X, padx=5, pady=(5, 10))
        title_frame.pack_propagate(False)

        tk.Label(
            title_frame,
            text="⚡ DELTA DOP-B HMI - Car Factory + NEXUS AI SUPERVISOR",
            font=("Arial", 16, "bold"),
            bg=COLORS["bg_header"],
            fg=COLORS["cyan"],
        ).pack(pady=20)

    def _create_estop_banner(self):
        self.estop_banner = tk.Label(
            self.root,
            text="🚨 EMERGENCY STOP ACTIVE — ALL EQUIPMENT LOCKED OUT — PRESS RESET TO CLEAR",
            font=("Arial", 12, "bold"),
            bg=COLORS["red"],
            fg=COLORS["text_white"],
            anchor=tk.CENTER,
            height=2,
        )

    def _create_connection_status(self):
        status_frame = tk.Frame(self.root, bg=COLORS["bg_dark"])
        status_frame.pack(fill=tk.X, padx=15, pady=8)

        self.conn_label = tk.Label(
            status_frame,
            text="🔴 DISCONNECTED",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_dark"],
            fg="red",
        )
        self.conn_label.pack(side=tk.LEFT)

        self.sys_status_label = tk.Label(
            status_frame,
            text="SYS: --",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_dark"],
            fg=COLORS["gray"],
        )
        self.sys_status_label.pack(side=tk.RIGHT)

    def _create_equipment_selector(self):
        selector_frame = tk.Frame(self.root, bg=COLORS["bg_dark"])
        selector_frame.pack(fill=tk.X, padx=15, pady=10)

        tk.Label(
            selector_frame,
            text="Select Equipment:",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_dark"],
            fg=COLORS["text_white"],
        ).pack(side=tk.LEFT, padx=5)

        self.equipment_names = [f"{eq[0]} - {eq[1]}" for eq in EQUIPMENT_LIST]

        self.eq_combo = ttk.Combobox(
            selector_frame,
            values=self.equipment_names,
            state="readonly",
            width=40,
            font=("Arial", 10),
        )
        self.eq_combo.current(0)
        self.eq_combo.pack(side=tk.LEFT, padx=10)
        self.eq_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self.select_equipment(self.eq_combo.current()),
        )

        # Real-panel lamp strip: click a lamp to select that machine
        self.lamp_buttons: Dict[int, tk.Button] = {}
        lamp_row = tk.Frame(selector_frame, bg=COLORS["bg_dark"])
        lamp_row.pack(side=tk.LEFT, padx=25)

        for i, (eq_id, _name, _off) in enumerate(EQUIPMENT_LIST):
            btn = tk.Button(
                lamp_row, text=eq_id,
                font=("Arial", 10, "bold"), width=9,
                relief=tk.RAISED, bd=3,
                bg=COLORS["bg_header"], fg=COLORS["text_white"],
                command=lambda idx=i: self.select_equipment(idx),
            )
            btn.pack(side=tk.LEFT, padx=3)
            self.lamp_buttons[i] = btn

    def _create_main_display(self):
        display_frame = tk.Frame(
            self.root,
            bg=COLORS["bg_panel"],
            bd=3,
            relief=tk.RAISED,
        )
        display_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        self.display_frame = display_frame
        self.labels = {}

        data_items = [
            ("voltage", "Voltage", "V", 0, 0),
            ("current", "Current", "A", 0, 1),
            ("active_power", "Active Power", "kW", 0, 2),
            ("reactive_power", "Reactive Power", "kVAR", 1, 0),
            ("apparent_power", "Apparent Power", "kVA", 1, 1),
            ("power_factor", "Power Factor", "", 1, 2),
            ("frequency", "Frequency", "Hz", 2, 0),
            ("energy", "Energy", "kWh", 2, 1),
            ("running_time", "Running Time", "min", 2, 2),
        ]

        for key, title, unit, row, col in data_items:
            frame = tk.Frame(
                display_frame,
                bg=COLORS["bg_panel"],
                bd=2,
                relief=tk.SUNKEN,
            )
            frame.grid(row=row, column=col, sticky="nsew", padx=8, pady=8)

            display_frame.columnconfigure(col, weight=1)
            display_frame.rowconfigure(row, weight=1)

            tk.Label(
                frame,
                text=title,
                font=("Arial", 10, "bold"),
                bg=COLORS["bg_panel"],
                fg=COLORS["text_light"],
            ).pack(pady=(8, 0))

            value_label = tk.Label(
                frame,
                text="---",
                font=("Courier New", 22, "bold"),
                bg=COLORS["bg_panel"],
                fg="#00ff00",
            )
            value_label.pack(pady=8)

            if unit:
                tk.Label(
                    frame,
                    text=unit,
                    font=("Arial", 9),
                    bg=COLORS["bg_panel"],
                    fg=COLORS["text_light"],
                ).pack(pady=(0, 8))

            self.labels[key] = value_label

        self._create_motor_status_row(display_frame)

    def _create_motor_status_row(self, parent):
        status_row = tk.Frame(
            parent,
            bg=COLORS["bg_panel"],
            bd=2,
            relief=tk.SUNKEN,
        )
        status_row.grid(
            row=3,
            column=0,
            columnspan=3,
            sticky="nsew",
            padx=8,
            pady=10,
        )

        tk.Label(
            status_row,
            text="Motor Status:",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=15, pady=10)

        self.motor_label = tk.Label(
            status_row,
            text="⏹️ STOPPED",
            font=("Arial", 16, "bold"),
            bg=COLORS["bg_panel"],
            fg="red",
        )
        self.motor_label.pack(side=tk.LEFT, padx=15)

        tk.Label(
            status_row,
            text="Load:",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).pack(side=tk.LEFT, padx=20)

        self.load_bar = ttk.Progressbar(
            status_row,
            length=250,
            mode="determinate",
        )
        self.load_bar.pack(side=tk.LEFT, padx=15)

        self.load_label = tk.Label(
            status_row,
            text="0%",
            font=("Arial", 12, "bold"),
            bg="black",
            fg="#00ff00",
            bd=2, relief=tk.SUNKEN, padx=12, pady=4,
        )
        self.load_label.pack(side=tk.LEFT, padx=15)
        self.load_label.bind("<Double-Button-1>", lambda e: self.open_keypad())


    def _create_protection_panel(self):
        prot_frame = tk.LabelFrame(
            self.display_frame,
            text="ANSI Protection Status",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["cyan"],
            bd=3,
            relief=tk.GROOVE,
        )
        prot_frame.grid(
            row=4,
            column=0,
            columnspan=3,
            sticky="nsew",
            padx=8,
            pady=10,
        )

        self.prot_labels = {}

        for i, name in enumerate(ANSI_NAMES):
            row_i = i // 4
            col_i = i % 4

            lbl = tk.Label(
                prot_frame,
                text=f"{name}: OK",
                font=("Arial", 10, "bold"),
                bg=COLORS["bg_panel"],
                fg=COLORS["gray"],
                width=20,
                anchor=tk.W,
            )
            lbl.grid(row=row_i, column=col_i, padx=8, pady=5)
            self.prot_labels[i] = lbl

        tk.Label(
            prot_frame,
            text="Thermal (49):",
            font=("Arial", 10, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        ).grid(row=2, column=0, padx=8, pady=8, sticky=tk.W)

        self.theta_bar = ttk.Progressbar(
            prot_frame,
            length=350,
            mode="determinate",
            maximum=150,
        )
        self.theta_bar.grid(row=2, column=1, columnspan=3, padx=8, pady=8)

        self.theta_label = tk.Label(
            prot_frame,
            text="0%",
            font=("Arial", 10, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["cyan"],
        )
        self.theta_label.grid(row=2, column=4, padx=8, pady=8)

    def _create_controls(self):
        control_frame = tk.Frame(self.root, bg=COLORS["bg_dark"])
        control_frame.pack(fill=tk.X, padx=15, pady=12)

        self.start_btn = tk.Button(
            control_frame,
            text="▶️ START",
            font=("Arial", 12, "bold"),
            bg="#2ecc71",
            fg="white",
            width=14,
            height=2,
            command=self.start_motor,
            relief=tk.RAISED,
            bd=4,
        )
        self.start_btn.pack(side=tk.LEFT, padx=12)

        self.stop_btn = tk.Button(
            control_frame,
            text="⏹️ STOP",
            font=("Arial", 12, "bold"),
            bg="#e74c3c",
            fg="white",
            width=14,
            height=2,
            command=self.stop_motor,
            relief=tk.RAISED,
            bd=4,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=12)

        self.estop_btn = tk.Button(
            control_frame,
            text="🚨 E-STOP ALL",
            font=("Arial", 12, "bold"),
            bg=COLORS["red"],
            fg="white",
            width=16,
            height=2,
            command=self.estop_all,
            relief=tk.RAISED,
            bd=5,
        )
        self.estop_btn.pack(side=tk.LEFT, padx=12)

        self.reset_btn = tk.Button(
            control_frame,
            text="🔓 RESET",
            font=("Arial", 12, "bold"),
            bg=COLORS["amber"],
            fg="black",
            width=14,
            height=2,
            command=self.reset_protection,
            relief=tk.RAISED,
            bd=4,
        )
        self.reset_btn.pack(side=tk.LEFT, padx=12)

        load_frame = tk.Frame(control_frame, bg=COLORS["bg_dark"])
        load_frame.pack(side=tk.LEFT, padx=25)

        tk.Label(
            load_frame,
            text="Simulated Load (%):",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_dark"],
            fg=COLORS["text_white"],
        ).pack()

        self.load_var = tk.IntVar(value=0)

        self.load_slider = tk.Scale(
            load_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.load_var,
            bg=COLORS["bg_dark"],
            fg=COLORS["text_white"],
            troughcolor=COLORS["bg_panel"],
            length=220,
            command=self.set_load,
        )
        self.load_slider.pack()

    def _create_alarm_label(self):
        self.alarm_label = tk.Label(
            self.root,
            text="",
            font=("Arial", 13, "bold"),
            bg=COLORS["bg_dark"],
            fg="yellow",
        )
        self.alarm_label.pack(pady=8)

    def _create_ai_panel(self):
        ai_frame = tk.LabelFrame(
            self.root,
            text="NEXUS AI Advisor",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["purple"],
            bd=3,
            relief=tk.GROOVE,
        )
        ai_frame.pack(fill=tk.X, padx=15, pady=8)

        self.ai_state_label = tk.Label(
            ai_frame,
            text="AI: waiting for backend...",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["gray"],
        )
        self.ai_state_label.pack(anchor=tk.W, padx=10, pady=(8, 2))

        self.ai_msg_label = tk.Label(
            ai_frame,
            text="",
            font=("Arial", 10),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            wraplength=1080,
            justify=tk.LEFT,
        )
        self.ai_msg_label.pack(anchor=tk.W, padx=10, pady=2)

        ctrl = tk.Frame(ai_frame, bg=COLORS["bg_panel"])
        ctrl.pack(anchor=tk.W, padx=10, pady=8)

        self.presence_var = tk.BooleanVar(value=True)

        self.presence_cb = tk.Checkbutton(
            ctrl,
            text="Operator Present",
            variable=self.presence_var,
            command=self.toggle_operator_presence,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_white"],
            selectcolor=COLORS["bg_dark"],
            activebackground=COLORS["bg_panel"],
            activeforeground=COLORS["text_white"],
            font=("Arial", 10, "bold"),
        )
        self.presence_cb.pack(side=tk.LEFT)

        tk.Button(
            ctrl,
            text="Refresh AI",
            font=("Arial", 10, "bold"),
            bg=COLORS["bg_header"],
            fg=COLORS["cyan"],
            width=12,
            command=self.fetch_ai_status_async,
        ).pack(side=tk.LEFT, padx=12)

        tk.Label(
            ctrl,
            text=f"API: {API_URL}",
            font=("Arial", 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["gray"],
        ).pack(side=tk.LEFT, padx=8)

    def _create_debug_panel(self):
        debug_frame = tk.Frame(
            self.root,
            bg=COLORS["bg_panel"],
            bd=2,
            relief=tk.SUNKEN,
        )
        debug_frame.pack(fill=tk.X, padx=15, pady=8)

        tk.Label(
            debug_frame,
            text="🛠️ DIAGNOSTIC PANEL:",
            font=("Arial", 10, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["cyan"],
        ).pack(side=tk.LEFT, padx=10, pady=8)

        self.debug_label = tk.Label(
            debug_frame,
            text="Waiting for data...",
            font=("Courier New", 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
        )
        self.debug_label.pack(side=tk.LEFT, padx=10, pady=8)

    def _create_footer(self):
        tk.Label(
            self.root,
            text=(
                "Delta DOP-B Simulator | Modbus TCP: 127.0.0.1:5020 "
                "| ANSI Protection | HR[120] Synced | NEXUS AI Advisor"
            ),
            font=("Arial", 9),
            bg=COLORS["bg_header"],
            fg=COLORS["text_white"],
            anchor=tk.W,
        ).pack(fill=tk.X, side=tk.BOTTOM, pady=(10, 5))



    def select_equipment(self, idx: int):
        """Lamp-strip navigation: select equipment, highlight the active lamp."""
        self.eq_combo.current(idx)
        for i, btn in self.lamp_buttons.items():
            if i == idx:
                btn.config(relief=tk.SUNKEN, bg=COLORS["cyan"], fg="black")
            else:
                btn.config(relief=tk.RAISED, bg=COLORS["bg_header"], fg=COLORS["text_white"])

    def open_keypad(self):
        """DOP-B style numeric keypad for the Load setpoint."""
        if self.estop_active:
            return

        pad = tk.Toplevel(self.root)
        pad.title("Setpoint Keypad - Load %")
        pad.geometry("280x400")
        pad.configure(bg=COLORS["bg_panel"])
        pad.transient(self.root)
        pad.grab_set()

        display_var = tk.StringVar(value=str(self.load_var.get()))

        tk.Label(
            pad, textvariable=display_var,
            font=("Courier New", 26, "bold"),
            bg="black", fg="#00ff00",
            bd=3, relief=tk.SUNKEN, anchor=tk.E,
        ).pack(fill=tk.X, padx=12, pady=12)

        def press(ch: str):
            cur = display_var.get()
            if ch == "CLR":
                display_var.set("0")
            elif ch == "ENT":
                try:
                    val = max(0, min(100, int(cur)))
                except ValueError:
                    val = 0
                self.load_slider.set(val)
                self.set_load(str(val))
                pad.destroy()
            elif len(cur) < 3:
                display_var.set((cur + ch).lstrip("0") or "0")

        grid = tk.Frame(pad, bg=COLORS["bg_panel"])
        grid.pack(padx=12, pady=8)

        keys = ["7", "8", "9", "4", "5", "6", "1", "2", "3", "0", "CLR", "ENT"]
        for i, k in enumerate(keys):
            bg = COLORS["bg_header"]
            if k == "ENT":
                bg = COLORS["green"]
            elif k == "CLR":
                bg = COLORS["red"]
            tk.Button(
                grid, text=k, width=6, height=2,
                font=("Arial", 12, "bold"),
                bg=bg, fg="white",
                command=lambda c=k: press(c),
            ).grid(row=i // 3, column=i % 3, padx=4, pady=4)

    def connect_plc(self):
        try:
            if self.client.connect():
                self.connected = True
                self.conn_label.config(text="🟢 LINK UP", fg="green")
                print("[HMI] Connected to PLC")
            else:
                self.connected = False
                self.conn_label.config(text="🔴 LINK DOWN", fg="red")
        except Exception as e:
            self.connected = False
            self.conn_label.config(text="🔴 LINK ERROR", fg="red")
            print(f"[HMI] Connection error: {e}")

    # =========================================================================
    # Address helpers
    # =========================================================================

    def get_current_register_offset(self) -> int:
        idx = self.eq_combo.current()
        return EQUIPMENT_LIST[idx][2]

    def get_current_coil_index(self) -> int:
        idx = self.eq_combo.current()
        eq_id = EQUIPMENT_LIST[idx][0]
        return EQUIPMENT_ORDER.index(eq_id)

    def get_current_equipment_id(self) -> str:
        idx = self.eq_combo.current()
        return EQUIPMENT_LIST[idx][0]

    # =========================================================================
    # Modbus read helpers
    # =========================================================================

    def _read_system_status(self) -> tuple[bool, bool]:
        try:
            result = self.client.read_holding_registers(
                address=SYS_STATUS_ADDR,
                count=1,
                slave=1,
            )

            if not result.isError() and result.registers:
                word = int(result.registers[0])
                estop = bool(word & (1 << SYS_STATUS_ESTOP_BIT))
                any_trip = bool(word & (1 << SYS_STATUS_ANY_TRIP_BIT))
                return estop, any_trip

        except Exception as e:
            print(f"[HMI] SYS status read error: {e}")

        return False, False

    def _read_coil_command(self) -> bool:
        try:
            coil_res = self.client.read_coils(
                address=0,
                count=8,
                slave=1,
            )

            if not coil_res.isError():
                return bool(coil_res.bits[self.get_current_coil_index()])

        except Exception:
            pass

        return False

    # =========================================================================
    # Main PLC polling
    # =========================================================================

    def read_plc_data(self):
        if not self.connected:
            return

        try:
            offset = self.get_current_register_offset()

            result = self.client.read_holding_registers(
                address=offset,
                count=REGS_PER_EQUIPMENT,
                slave=1,
            )

            if result.isError():
                return

            r = result.registers

            if len(r) < REGS_PER_EQUIPMENT:
                return

            self.estop_active, self.any_trip_active = self._read_system_status()
            coil_cmd = self._read_coil_command()
            eq_id = self.get_current_equipment_id()

            # -------------------------------------------------------------------------
            # Correct register extraction
            # -------------------------------------------------------------------------
            voltage = float(r[REG_VOLTAGE]) / 10.0
            current = float(r[REG_CURRENT]) / 10.0
            active_power = float(r[REG_ACTIVE_POWER]) / 10.0
            reactive_power = float(r[REG_REACTIVE_POWER]) / 10.0
            apparent_power = float(r[REG_APPARENT_POWER]) / 10.0
            power_factor = float(r[REG_POWER_FACTOR]) / 100.0
            frequency = float(r[REG_FREQUENCY]) / 10.0
            energy = float(r[REG_ENERGY]) / 100.0

            self.motor_state = int(r[REG_MOTOR_STATE])
            alarm_flag = int(r[REG_ALARM_FLAG])
            self.trip_word = int(r[REG_TRIP_WORD])
            running_time = int(r[REG_RUNNING_TIME])
            load = int(r[REG_LOAD])
            self.alarm_word = int(r[REG_ALARM_WORD])
            self.lockout_status = int(r[REG_LOCKOUT_STATUS])
            theta_pm = int(r[REG_THETA_PM])
            self.trip_count = int(r[REG_TRIP_COUNT])
            self.heartbeat = int(r[REG_HEARTBEAT])

            motor_on = self.motor_state == 1
            motor_pending = self.motor_state == 2
            self.is_latched = bool(self.lockout_status) or self.motor_state == 3

            # -------------------------------------------------------------------------
            # Numeric display
            # -------------------------------------------------------------------------
            if self.estop_active:
                display_current = 0.0
                display_active = 0.0
                display_reactive = 0.0
                display_apparent = 0.0
                display_pf = 0.0
                display_load = 0
            else:
                display_current = current
                display_active = active_power
                display_reactive = reactive_power
                display_apparent = apparent_power
                display_pf = power_factor
                display_load = load

            self.labels["voltage"].config(text=f"{voltage:.1f}")
            self.labels["current"].config(text=f"{display_current:.1f}")
            self.labels["active_power"].config(text=f"{display_active:.2f}")
            self.labels["reactive_power"].config(text=f"{display_reactive:.2f}")
            self.labels["apparent_power"].config(text=f"{display_apparent:.2f}")
            self.labels["power_factor"].config(text=f"{display_pf:.3f}")
            self.labels["frequency"].config(text=f"{frequency:.2f}")
            self.labels["energy"].config(text=f"{energy:.2f}")
            self.labels["running_time"].config(text=f"{running_time}")

            # -------------------------------------------------------------------------
            # Motor status logic
            # Priority:
            #   E-STOP > LOCKED > TRIPPED > START PENDING/TIMEOUT > RUNNING > STOPPED
            # -------------------------------------------------------------------------
            if self.estop_active:
                self._start_pending_times.pop(eq_id, None)
                self.motor_label.config(text="🚨 E-STOP", fg=COLORS["red"])

            elif self.is_latched:
                self._start_pending_times.pop(eq_id, None)
                self.motor_label.config(text="🔒 LOCKED", fg=COLORS["amber"])

            elif self.trip_word and not motor_on:
                self._start_pending_times.pop(eq_id, None)
                self.motor_label.config(text="⚡ TRIPPED", fg=COLORS["amber"])

            elif motor_pending or (coil_cmd and not motor_on and not self.is_latched):
                current_time = time.time()

                if eq_id not in self._start_pending_times:
                    self._start_pending_times[eq_id] = current_time

                elapsed = current_time - self._start_pending_times[eq_id]

                if elapsed < START_PENDING_TIMEOUT_SEC:
                    self.motor_label.config(
                        text="⏳ START PENDING",
                        fg=COLORS["orange"],
                    )
                else:
                    self.motor_label.config(
                        text="⚠️ START TIMEOUT",
                        fg=COLORS["red"],
                    )

            elif motor_on:
                self._start_pending_times.pop(eq_id, None)
                self.motor_label.config(text="✅ RUNNING", fg=COLORS["green"])

            else:
                self._start_pending_times.pop(eq_id, None)
                self.motor_label.config(text="⏹️ STOPPED", fg="red")

            # -------------------------------------------------------------------------
            # Load bar
            # -------------------------------------------------------------------------
            self.load_bar["value"] = display_load
            self.load_label.config(text=f"{display_load}%")

            # -------------------------------------------------------------------------
            # ANSI protection indicators
            # -------------------------------------------------------------------------
            for i in range(8):
                tripped = bool(self.trip_word & (1 << i))
                alarmed = bool(self.alarm_word & (1 << i))

                if tripped:
                    self.prot_labels[i].config(
                        text=f"{ANSI_NAMES[i]}: TRIP",
                        fg=COLORS["red"],
                    )
                elif alarmed:
                    self.prot_labels[i].config(
                        text=f"{ANSI_NAMES[i]}: ALARM",
                        fg=COLORS["amber"],
                    )
                else:
                    self.prot_labels[i].config(
                        text=f"{ANSI_NAMES[i]}: OK",
                        fg=COLORS["gray"],
                    )

            # -------------------------------------------------------------------------
            # Thermal capacity
            # -------------------------------------------------------------------------
            theta_pct = theta_pm / 10.0
            self.theta_bar["value"] = theta_pct

            if theta_pct >= 100:
                theta_color = COLORS["red"]
            elif theta_pct >= 85:
                theta_color = COLORS["amber"]
            else:
                theta_color = COLORS["cyan"]

            self.theta_label.config(
                text=f"{theta_pct:.1f}%",
                fg=theta_color,
            )

            # -------------------------------------------------------------------------
            # Alarm banner
            # -------------------------------------------------------------------------
            if self.estop_active:
                self.alarm_label.config(
                    text="🚨 E-STOP LATCHED — ALL EQUIPMENT STOPPED",
                    fg=COLORS["red"],
                )

            elif self.is_latched:
                active_trips = [
                    ANSI_NAMES[i]
                    for i in range(8)
                    if self.trip_word & (1 << i)
                ]
                trip_text = ", ".join(active_trips) if active_trips else "PROTECTION"
                self.alarm_label.config(
                    text=f"🔒 LOCKOUT: {trip_text} — PRESS RESET",
                    fg=COLORS["amber"],
                )

            elif self.trip_word:
                active_trips = [
                    ANSI_NAMES[i]
                    for i in range(8)
                    if self.trip_word & (1 << i)
                ]
                self.alarm_label.config(
                    text=f"⚡ TRIPS: {', '.join(active_trips)}",
                    fg=COLORS["red"],
                )

            elif self.alarm_word:
                active_alarms = [
                    ANSI_NAMES[i]
                    for i in range(8)
                    if self.alarm_word & (1 << i)
                ]
                self.alarm_label.config(
                    text=f"⚠️ ALARMS: {', '.join(active_alarms)}",
                    fg=COLORS["amber"],
                )

            else:
                self.alarm_label.config(text="")

            # -------------------------------------------------------------------------
            # System status label
            # -------------------------------------------------------------------------
            self._update_system_status_label()
            self._update_estop_banner()
            self._update_button_states()

            # -------------------------------------------------------------------------
            # Diagnostic panel
            # -------------------------------------------------------------------------
            pending_info = ""
            if eq_id in self._start_pending_times:
                elapsed = time.time() - self._start_pending_times[eq_id]
                pending_info = f" | pending: {elapsed:.1f}s"

            ai_state = "offline"
            if self.last_ai_status:
                ai_state = self.last_ai_status.get("system_state", "unknown")

            debug_text = (
                f"motor_state={self.motor_state} | "
                f"coil_cmd={coil_cmd} | "
                f"trip_word={self.trip_word} | "
                f"alarm_word={self.alarm_word} | "
                f"lockout={self.lockout_status} | "
                f"is_latched={self.is_latched} | "
                f"estop={self.estop_active} | "
                f"hb={self.heartbeat} | "
                f"trips={self.trip_count} | "
                f"ai={ai_state}"
                f"{pending_info}"
            )

            self.debug_label.config(text=debug_text)

        except Exception as e:
            print(f"[HMI] Read error: {e}. Triggering auto-reconnect...")
            self.connected = False
            self.conn_label.config(text="🔴 LINK LOST", fg="red")

            try:
                self.client.close()
            except Exception:
                pass

    # =========================================================================
    # UI update helpers
    # =========================================================================

    def _update_system_status_label(self):
        parts = []

        if self.estop_active:
            parts.append("E-STOP")

        if self.any_trip_active:
            parts.append("TRIP")

        if parts:
            text = f"SYS: {' | '.join(parts)}"
            color = COLORS["red"] if self.estop_active else COLORS["amber"]
        else:
            text = "SYS: NORMAL"
            color = COLORS["green"]

        self.sys_status_label.config(text=text, fg=color)

    def _update_estop_banner(self):
        if self.estop_active:
            if not self.estop_banner.winfo_manager():
                self.estop_banner.pack(
                    fill=tk.X,
                    padx=15,
                    pady=(5, 0),
                    before=self.conn_label.master,
                )
            self.estop_banner.config(
                bg=COLORS["red"] if self._blink else "#7a0000",
            )
        else:
            if self.estop_banner.winfo_manager():
                self.estop_banner.pack_forget()



    def _update_button_states(self):
        if not self.connected:
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.DISABLED)
            self.estop_btn.config(state=tk.DISABLED)
            self.reset_btn.config(state=tk.DISABLED)
            self.load_slider.config(state=tk.DISABLED)
            return

        can_start = (
            not self.estop_active
            and not self.is_latched
            and self.motor_state not in (1, 2)
        )

        can_stop = (
            not self.estop_active
            and self.motor_state in (1, 2)
        )

        can_estop = not self.estop_active

        can_reset = (
            self.estop_active
            or self.is_latched
            or self.trip_word != 0
        )

        can_load = not self.estop_active

        self.start_btn.config(state=tk.NORMAL if can_start else tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL if can_stop else tk.DISABLED)
        self.estop_btn.config(state=tk.NORMAL if can_estop else tk.DISABLED)
        self.reset_btn.config(state=tk.NORMAL if can_reset else tk.DISABLED)
        self.load_slider.config(state=tk.NORMAL if can_load else tk.DISABLED)

    # =========================================================================
    # Motor / system commands
    # =========================================================================

    def start_motor(self):
        if not self.connected:
            return

        eq_id = self.get_current_equipment_id()

        if self.estop_active:
            print("[HMI] ⛔ START blocked — E-STOP is active")
            return

        if self.is_latched:
            print("[HMI] ⛔ START blocked — protection lockout active")
            return

        if self.motor_state in (1, 2):
            print(f"[HMI] ⛔ START ignored — {eq_id} already running or pending")
            return

        try:
            coil_idx = self.get_current_coil_index()
            self.client.write_coil(
                address=coil_idx,
                value=True,
                slave=1,
            )

            self._start_pending_times[eq_id] = time.time()
            print(f"[HMI] ▶️ START sent for {eq_id}")

        except Exception as e:
            print(f"[HMI] Start error: {e}")

    def stop_motor(self):
        if not self.connected:
            return

        try:
            coil_idx = self.get_current_coil_index()
            self.client.write_coil(
                address=coil_idx,
                value=False,
                slave=1,
            )

            eq_id = self.get_current_equipment_id()
            self._start_pending_times.pop(eq_id, None)

            print(f"[HMI] ⏹️ STOP sent for {eq_id}")

        except Exception as e:
            print(f"[HMI] Stop error: {e}")

    def estop_all(self):
        if not self.connected:
            return

        try:
            self.client.write_coil(
                address=COIL_ESTOP,
                value=True,
                slave=1,
            )

            self._start_pending_times.clear()
            print("[HMI] 🚨 E-STOP sent")

        except Exception as e:
            print(f"[HMI] E-STOP error: {e}")

    def reset_protection(self):
        if not self.connected:
            return

        if not messagebox.askyesno(
            "Confirm RESET",
            "Clear protection lockouts and the E-STOP latch?\n"
            "All equipment will be allowed to restart.",
        ):
            return

        try:
            self.client.write_coil(
                address=COIL_RESET,
                value=True,
                slave=1,
            )

            print("[HMI] 🔓 RESET sent")

        except Exception as e:
            print(f"[HMI] RESET error: {e}")

    def set_load(self, value):
        if not self.connected:
            return

        if self.estop_active:
            return

        try:
            # Write the operator SETPOINT (HR[160+eq]); the PLC ramps the
            # actual load register toward it. 0 = release to autonomous.
            coil_idx = self.get_current_coil_index()
            self.client.write_register(
                address=160 + coil_idx,
                value=max(0, min(100, int(float(value)))),
                slave=1,
            )

        except Exception as e:
            print(f"[HMI] Load error: {e}")

    # =========================================================================
    # AI advisor polling
    # =========================================================================

    def start_ai_polling(self):
        self.root.after(1000, self.fetch_ai_status_async)
        self.root.after(200, self.process_ai_queue)

    def fetch_ai_status_async(self):
        if self._ai_fetch_in_progress:
            return

        self._ai_fetch_in_progress = True
        threading.Thread(target=self._fetch_ai_worker, daemon=True).start()

    def _fetch_ai_worker(self):
        try:
            if requests is None:
                self.ai_queue.put(("err", "requests library not available"))
                return

            resp = requests.get(
                f"{API_URL}/api/agent/status",
                timeout=1.5,
            )

            if resp.ok:
                self.ai_queue.put(("ok", resp.json()))
            else:
                self.ai_queue.put(("err", f"HTTP {resp.status_code}: {resp.text[:120]}"))

        except Exception as e:
            self.ai_queue.put(("err", str(e)))

        finally:
            self._ai_fetch_in_progress = False

    def process_ai_queue(self):
        try:
            while True:
                kind, payload = self.ai_queue.get_nowait()

                if kind == "ok":
                    self._update_ai_panel(payload)

                elif kind == "err":
                    self.ai_state_label.config(
                        text="AI: backend unavailable",
                        fg=COLORS["gray"],
                    )
                    self.ai_msg_label.config(
                        text=f"Error: {payload}",
                        fg=COLORS["red"],
                    )

                elif kind == "presence":
                    self._handle_presence_result(payload)

        except queue.Empty:
            pass

        self.root.after(200, self.process_ai_queue)
        self.root.after(2000, self.fetch_ai_status_async)

    def _update_ai_panel(self, status: Dict[str, Any]):
        self.last_ai_status = status

        state = str(status.get("system_state", "UNKNOWN"))
        mode = str(status.get("operating_mode", "ADVISORY"))
        llm_available = bool(status.get("llm_available", False))
        operator_present = bool(status.get("operator_present", self.operator_present))

        color_map = {
            "NORMAL": COLORS["green"],
            "DEGRADED": COLORS["gray"],
            "WARNING": COLORS["amber"],
            "CRITICAL": COLORS["red"],
            "LOCKED_OUT": COLORS["amber"],
            "ESTOP": COLORS["red"],
        }

        color = color_map.get(state, COLORS["cyan"])

        self.ai_state_label.config(
            text=(
                f"AI State: {state} | Mode: {mode} | "
                f"LLM: {'online' if llm_available else 'offline'} | "
                f"Operator: {'present' if operator_present else 'absent'}"
            ),
            fg=color,
        )

        recommendations = status.get("recommendations", [])

        if recommendations:
            top = recommendations[0]
            msg = top.get("message", "")
            action = top.get("action", "manual_review")
            severity = str(top.get("severity", "LOW")).upper()

            sev_color = {
                "CRITICAL": COLORS["red"],
                "HIGH": COLORS["red"],
                "MEDIUM": COLORS["amber"],
                "LOW": COLORS["cyan"],
                "INFO": COLORS["gray"],
            }.get(severity, COLORS["text_light"])

            self.ai_msg_label.config(
                text=f"Top recommendation: {msg} | Action: {action}",
                fg=sev_color,
            )
        else:
            self.ai_msg_label.config(
                text="No active recommendation. System nominal.",
                fg=COLORS["green"],
            )

        # Sync checkbox without triggering command recursion.
        self._updating_presence = True
        self.presence_var.set(operator_present)
        self.operator_present = operator_present
        self._updating_presence = False

    def toggle_operator_presence(self):
        if self._updating_presence:
            return

        present = bool(self.presence_var.get())
        self.operator_present = present

        threading.Thread(
            target=self._post_operator_presence_worker,
            args=(present,),
            daemon=True,
        ).start()

    def _post_operator_presence_worker(self, present: bool):
        try:
            if requests is None:
                self.ai_queue.put(("err", "requests library not available"))
                return

            resp = requests.post(
                f"{API_URL}/api/agent/operator-presence",
                params={"present": "true" if present else "false"},
                headers=({"X-API-Key": API_KEY} if API_KEY else {}),
                timeout=2.0,
            )

            self.ai_queue.put(
                (
                    "presence",
                    {
                        "present": present,
                        "status_code": resp.status_code,
                        "body": resp.text[:200],
                    },
                )
            )

        except Exception as e:
            self.ai_queue.put(("err", f"Presence update failed: {e}"))

    def _handle_presence_result(self, payload: Dict[str, Any]):
        present = bool(payload.get("present", False))
        status_code = int(payload.get("status_code", 0))

        if 200 <= status_code < 300:
            self.ai_msg_label.config(
                text=(
                    f"Operator presence updated: {'present' if present else 'absent'}. "
                    f"Agent mode will follow backend policy."
                ),
                fg=COLORS["purple"],
            )
        else:
            self.ai_msg_label.config(
                text=f"Operator presence update failed: HTTP {status_code}",
                fg=COLORS["red"],
            )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def update_display(self):
        if self.running:
            self._blink = not self._blink

            if not self.connected:
                self.connect_plc()

            if self.connected:
                self.read_plc_data()

            self.root.after(POLL_INTERVAL_MS, self.update_display)

    def on_closing(self):
        self.running = False

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        self.root.destroy()


# =============================================================================
# Entry point
# =============================================================================

def main():
    root = tk.Tk()
    root.geometry("1280x860")
    root.minsize(1100, 700)
    app = DeltaHMISimulator(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()