"""
HMI Simulator - Delta DOP-B style for 6 equipment

FIXED VERSION:
- Reads HR[120] system status word for global E-STOP detection
- Checks bit 15 of alarm_word for protection lockout (is_latched)
- Motor status display logic now matches Streamlit exactly:
      E-STOP > LOCKED > TRIPPED > START PENDING > RUNNING > STOPPED
- Masks bit 15 from alarm_word before displaying ANSI alarms
- Buttons enabled/disabled based on current system state
- Global E-STOP indicator banner added
- NEW: Diagnostic Panel (Debug Mode) added to show raw state variables
- NEW: START PENDING timeout (5 seconds) for optimistic Streamlit-like behavior
- [FIX A] Timeout increased to 5s to prevent CPU starvation timeouts
- [FIX B] Auto-reconnect logic added to main loop for self-healing
"""

import tkinter as tk
from tkinter import ttk
from pymodbus.client import ModbusTcpClient
import time


# ==========================================
# Equipment and ANSI Definitions
# ==========================================

# New offsets: 18 registers per equipment
EQUIPMENT_LIST = [
    ("STP-01", "Stamping Press", 0),
    ("WLD-01", "Welding Robots", 18),
    ("PNT-01", "Paint Booth", 36),
    ("ASM-01", "Assembly", 54),
    ("UTI-01", "Compressor", 72),
    ("UTI-02", "Chiller", 90),
]

# Unified index source
EQUIPMENT_ORDER = [eq[0] for eq in EQUIPMENT_LIST]

# ANSI function definitions
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

# ==========================================
# Modbus Address Constants
# ==========================================

SYS_STATUS_ADDR = 120          # HR[120] = system status word
SYS_STATUS_ESTOP_BIT = 0       # bit 0 of HR[120] = E-STOP latched
SYS_STATUS_ANY_TRIP_BIT = 1    # bit 1 of HR[120] = any equipment tripped

COIL_ESTOP = 6                 # CO[6] = E-STOP command
COIL_RESET = 7                 # CO[7] = RESET command

LOCKOUT_BIT_IN_ALARM_WORD = 15 # bit 15 of per-equipment alarm_word

POLL_INTERVAL_MS = 500
START_PENDING_TIMEOUT_SEC = 5  # Optimistic timeout to match Streamlit


# ==========================================
# Color Palette (matches Streamlit theme)
# ==========================================

COLORS = {
    "bg_dark":      "#1a1a2e",
    "bg_panel":     "#16213e",
    "bg_header":    "#0f3460",
    "text_light":   "#a0a0a0",
    "text_white":   "white",
    "cyan":         "#00E5FF",
    "green":        "#00E676",
    "red":          "#FF1744",
    "amber":        "#FFB300",
    "orange":       "orange",
    "gray":         "#a0a0a0",
    "estop_bg":     "#3d0000",
    "normal_bg":    "#16213e",
}


# ==========================================
# Main HMI Application
# ==========================================

class DeltaHMISimulator:
    """
    Delta DOP-B style HMI simulator.

    Reads equipment registers + system status word from the
    Modbus TCP server and renders a synchronized view that
    matches the Streamlit SCADA dashboard logic.
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Delta DOP-B HMI - Car Factory (with Protection)")
        self.root.geometry("1100x900")
        self.root.configure(bg=COLORS["bg_dark"])

        # [FIX A] Increased timeout from 3 to 5 seconds to survive CPU starvation
        self.client = ModbusTcpClient(
            host="127.0.0.1",
            port=5020,
            timeout=5,
        )
        self.connected = False
        self.running = True

        # Cached system-level flags (updated every poll)
        self.estop_active = False
        self.any_trip_active = False
        self.is_latched = False

        # NEW: START PENDING timeout tracking per equipment
        self._start_pending_times = {}

        self.create_ui()
        self.connect_plc()
        self.update_display()

    # ==========================================
    # UI Construction
    # ==========================================

    def create_ui(self):
        self._create_title_bar()
        self._create_estop_banner()
        self._create_connection_status()
        self._create_equipment_selector()
        self._create_main_display()
        self._create_protection_panel()
        self._create_controls()
        self._create_alarm_label()
        self._create_debug_panel()
        self._create_footer()

    # ----- Title Bar -----

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
            text="⚡ DELTA DOP-B HMI - Car Factory (WITH ANSI PROTECTION)",
            font=("Arial", 16, "bold"),
            bg=COLORS["bg_header"],
            fg=COLORS["cyan"],
        ).pack(pady=20)

    # ----- Global E-STOP Banner -----

    def _create_estop_banner(self):
        """
        Banner that appears only when global E-STOP is latched.
        Hidden by default; shown/hidden in _update_estop_banner().
        """
        self.estop_banner = tk.Label(
            self.root,
            text="🚨 EMERGENCY STOP ACTIVE — ALL EQUIPMENT LOCKED OUT — PRESS RESET TO CLEAR",
            font=("Arial", 12, "bold"),
            bg=COLORS["red"],
            fg=COLORS["text_white"],
            anchor=tk.CENTER,
            height=2,
        )
        # Not packed yet; will be shown when E-STOP is active

    # ----- Connection Status -----

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

        # System status label (E-STOP / trips) on the right
        self.sys_status_label = tk.Label(
            status_frame,
            text="SYS: --",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_dark"],
            fg=COLORS["gray"],
        )
        self.sys_status_label.pack(side=tk.RIGHT)

    # ----- Equipment Selector -----

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

        self.equipment_names = [
            f"{eq[0]} - {eq[1]}" for eq in EQUIPMENT_LIST
        ]
        self.eq_combo = ttk.Combobox(
            selector_frame,
            values=self.equipment_names,
            state="readonly",
            width=40,
            font=("Arial", 10),
        )
        self.eq_combo.current(0)
        self.eq_combo.pack(side=tk.LEFT, padx=10)

    # ----- Main Data Display -----

    def _create_main_display(self):
        display_frame = tk.Frame(
            self.root,
            bg=COLORS["bg_panel"],
            bd=3,
            relief=tk.RAISED,
        )
        display_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        self.display_frame = display_frame  # keep reference
        self.labels = {}

        data_items = [
            ("voltage",        "Voltage",        "V",    0, 0),
            ("current",        "Current",        "A",    0, 1),
            ("active_power",   "Active Power",   "kW",   0, 2),
            ("reactive_power", "Reactive Power", "kVAR", 1, 0),
            ("apparent_power", "Apparent Power", "kVA",  1, 1),
            ("power_factor",   "Power Factor",   "",     1, 2),
            ("frequency",      "Frequency",      "Hz",   2, 0),
            ("energy",         "Energy",         "kWh",  2, 1),
            ("running_time",   "Running Time",   "min",  2, 2),
        ]

        for key, title, unit, row, col in data_items:
            frame = tk.Frame(
                display_frame,
                bg=COLORS["bg_panel"],
                bd=2,
                relief=tk.SUNKEN,
            )
            frame.grid(
                row=row, column=col,
                sticky="nsew", padx=8, pady=8,
            )
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

    # ----- Motor + Load Status Row -----

    def _create_motor_status_row(self, parent):
        status_row = tk.Frame(
            parent,
            bg=COLORS["bg_panel"],
            bd=2,
            relief=tk.SUNKEN,
        )
        status_row.grid(
            row=3, column=0, columnspan=3,
            sticky="nsew", padx=8, pady=10,
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
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_white"],
        )
        self.load_label.pack(side=tk.LEFT, padx=15)

    # ----- Protection Panel -----

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
            row=4, column=0, columnspan=3,
            sticky="nsew", padx=8, pady=10,
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

        # Thermal capacity bar
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
        self.theta_bar.grid(
            row=2, column=1, columnspan=3,
            padx=8, pady=8,
        )

        self.theta_label = tk.Label(
            prot_frame,
            text="0%",
            font=("Arial", 10, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["cyan"],
        )
        self.theta_label.grid(row=2, column=4, padx=8, pady=8)

    # ----- Control Buttons -----

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

        # Load slider
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

    # ----- Alarm Label -----

    def _create_alarm_label(self):
        self.alarm_label = tk.Label(
            self.root,
            text="",
            font=("Arial", 13, "bold"),
            bg=COLORS["bg_dark"],
            fg="yellow",
        )
        self.alarm_label.pack(pady=8)

    # ----- Diagnostic Panel (Debug Mode) -----

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

    # ----- Footer -----

    def _create_footer(self):
        tk.Label(
            self.root,
            text=(
                "Delta DOP-B Simulator | Modbus TCP: 127.0.0.1:5020 "
                "| WITH ANSI PROTECTION | HR[120] Synced"
            ),
            font=("Arial", 9),
            bg=COLORS["bg_header"],
            fg=COLORS["text_white"],
            anchor=tk.W,
        ).pack(fill=tk.X, side=tk.BOTTOM, pady=(10, 5))

    # ==========================================
    # Connection
    # ==========================================

    def connect_plc(self):
        try:
            if self.client.connect():
                self.connected = True
                self.conn_label.config(text="🟢 LINK UP", fg="green")
                print("[HMI] Connected to PLC")
            else:
                self.connected = False  # [FIX] Ensure state is False on failure
                self.conn_label.config(text="🔴 LINK DOWN", fg="red")
        except Exception as e:
            self.connected = False  # [FIX] Ensure state is False on exception
            self.conn_label.config(text="🔴 LINK ERROR", fg="red")
            print(f"[HMI] Connection error: {e}")

    # ==========================================
    # Address Helpers
    # ==========================================

    def get_current_register_offset(self) -> int:
        idx = self.eq_combo.current()
        return EQUIPMENT_LIST[idx][2]

    def get_current_coil_index(self) -> int:
        idx = self.eq_combo.current()
        eq_id = EQUIPMENT_LIST[idx][0]
        return EQUIPMENT_ORDER.index(eq_id)

    # ==========================================
    # Modbus Read Helpers
    # ==========================================

    def _read_system_status(self) -> tuple:
        """
        Read HR[120] system status word.

        Returns:
            (estop_active, any_trip_active)
        """
        try:
            result = self.client.read_holding_registers(
                address=SYS_STATUS_ADDR,
                count=1,
                slave=1,
            )
            if not result.isError() and result.registers:
                word = result.registers[0]
                estop = bool(word & (1 << SYS_STATUS_ESTOP_BIT))
                any_trip = bool(word & (1 << SYS_STATUS_ANY_TRIP_BIT))
                return estop, any_trip
        except Exception as e:
            print(f"[HMI] SYS status read error: {e}")

        return False, False

    def _read_coil_command(self) -> bool:
        """
        Read back the motor coil for the currently selected equipment.
        Used for START PENDING display.
        """
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

    # ==========================================
    # Main Poll Loop
    # ==========================================

    def read_plc_data(self):
        if not self.connected:
            return

        try:
            offset = self.get_current_register_offset()

            # --- Read 18 equipment registers ---
            result = self.client.read_holding_registers(
                address=offset,
                count=18,
                slave=1,
            )
            if result.isError():
                return

            r = result.registers

            # --- Read global system status (HR[120]) ---
            self.estop_active, self.any_trip_active = (
                self._read_system_status()
            )

            # --- Read coil command for START PENDING ---
            coil_cmd = self._read_coil_command()
            eq_id = EQUIPMENT_LIST[self.eq_combo.current()][0]
            motor_on = bool(r[8])

            # ==========================================
            # Update numeric displays
            # ==========================================
            # When E-STOP is active, force display values to zero
            # (matches Streamlit behavior)
            if self.estop_active:
                self.labels["voltage"].config(text=f"{r[0]/10:.1f}")
                self.labels["current"].config(text="0.0")
                self.labels["active_power"].config(text="0.00")
                self.labels["reactive_power"].config(text="0.00")
                self.labels["apparent_power"].config(text="0.00")
                self.labels["power_factor"].config(text="0.000")
                self.labels["frequency"].config(text=f"{r[6]/10:.2f}")
                self.labels["energy"].config(text=f"{r[7]/100:.2f}")
                self.labels["running_time"].config(text=f"{r[11]}")
            else:
                self.labels["voltage"].config(text=f"{r[0]/10:.1f}")
                self.labels["current"].config(text=f"{r[1]/10:.1f}")
                self.labels["active_power"].config(text=f"{r[2]/10:.2f}")
                self.labels["reactive_power"].config(text=f"{r[3]/10:.2f}")
                self.labels["apparent_power"].config(text=f"{r[4]/10:.2f}")
                self.labels["power_factor"].config(text=f"{r[5]/100:.3f}")
                self.labels["frequency"].config(text=f"{r[6]/10:.2f}")
                self.labels["energy"].config(text=f"{r[7]/100:.2f}")
                self.labels["running_time"].config(text=f"{r[11]}")

            # ==========================================
            # Extract protection words
            # ==========================================
            trip_word = r[13]
            raw_alarm_word = r[14]
            theta_pm = r[15]

            # Bit 15 of alarm_word = lockout flag (set by data_generator)
            self.is_latched = bool(
                raw_alarm_word & (1 << LOCKOUT_BIT_IN_ALARM_WORD)
            )

            # Mask out bit 15 so ANSI alarm panel is not confused
            alarm_word = raw_alarm_word & ~(1 << LOCKOUT_BIT_IN_ALARM_WORD)

            # ==========================================
            # Motor status label (matches Streamlit logic)
            #
            # Priority:
            #   1. E-STOP    (global latch from HR[120])
            #   2. LOCKED    (per-equipment protection latch)
            #   3. TRIPPED   (active trip_word, motor stopped)
            #   4. START PENDING (coil ON but motor not yet running)
            #      → With optimistic timeout (5s) to match Streamlit
            #   5. RUNNING
            #   6. STOPPED
            # ==========================================
            if self.estop_active:
                self.motor_label.config(
                    text="🚨 E-STOP",
                    fg=COLORS["red"],
                )
                self._start_pending_times.pop(eq_id, None)

            elif self.is_latched:
                self.motor_label.config(
                    text="🔒 LOCKED",
                    fg=COLORS["amber"],
                )
                self._start_pending_times.pop(eq_id, None)

            elif trip_word and not motor_on:
                self.motor_label.config(
                    text="⚡ TRIPPED",
                    fg=COLORS["amber"],
                )
                self._start_pending_times.pop(eq_id, None)

            elif not motor_on and coil_cmd:
                # ==========================================
                # START PENDING with optimistic timeout
                # ==========================================
                current_time = time.time()

                if eq_id not in self._start_pending_times:
                    self._start_pending_times[eq_id] = current_time
                    print(f"[HMI] ⏳ START PENDING started for {eq_id}")

                elapsed = current_time - self._start_pending_times[eq_id]

                if elapsed < START_PENDING_TIMEOUT_SEC:
                    # Still waiting for motor to start
                    self.motor_label.config(
                        text="⏳ START PENDING",
                        fg=COLORS["orange"],
                    )
                else:
                    # Timeout: assume motor started successfully
                    # (matches Streamlit optimistic behavior)
                    self.motor_label.config(
                        text="✅ RUNNING",
                        fg=COLORS["green"],
                    )

            elif motor_on:
                # Motor actually running - clear pending timer
                self._start_pending_times.pop(eq_id, None)
                self.motor_label.config(
                    text="✅ RUNNING",
                    fg=COLORS["green"],
                )

            else:
                # Stopped
                self._start_pending_times.pop(eq_id, None)
                self.motor_label.config(
                    text="⏹️ STOPPED",
                    fg="red",
                )

            # ==========================================
            # Load bar
            # ==========================================
            if self.estop_active:
                self.load_bar["value"] = 0
                self.load_label.config(text="0%")
            else:
                self.load_bar["value"] = r[12]
                self.load_label.config(text=f"{r[12]}%")

            # ==========================================
            # ANSI protection indicators
            # ==========================================
            for i in range(8):
                tripped = bool(trip_word & (1 << i))
                alarmed = bool(alarm_word & (1 << i))

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

            # ==========================================
            # Thermal capacity (ANSI 49)
            # ==========================================
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

            # ==========================================
            # Alarm text banner
            # ==========================================
            if self.estop_active:
                self.alarm_label.config(
                    text="🚨 E-STOP LATCHED — ALL EQUIPMENT STOPPED",
                    fg=COLORS["red"],
                )
            elif self.is_latched:
                active_trips = [
                    ANSI_NAMES[i]
                    for i in range(8)
                    if trip_word & (1 << i)
                ]
                trip_text = ", ".join(active_trips) if active_trips else "PROTECTION"
                self.alarm_label.config(
                    text=f"🔒 LOCKOUT: {trip_text} — PRESS RESET",
                    fg=COLORS["amber"],
                )
            elif trip_word:
                active_trips = [
                    ANSI_NAMES[i]
                    for i in range(8)
                    if trip_word & (1 << i)
                ]
                self.alarm_label.config(
                    text=f"⚡ TRIPS: {', '.join(active_trips)}",
                    fg=COLORS["red"],
                )
            elif alarm_word:
                active_alarms = [
                    ANSI_NAMES[i]
                    for i in range(8)
                    if alarm_word & (1 << i)
                ]
                self.alarm_label.config(
                    text=f"⚠️ ALARMS: {', '.join(active_alarms)}",
                    fg=COLORS["amber"],
                )
            else:
                self.alarm_label.config(text="")

            # ==========================================
            # System status label (top right)
            # ==========================================
            self._update_system_status_label()

            # ==========================================
            # E-STOP banner visibility
            # ==========================================
            self._update_estop_banner()

            # ==========================================
            # Button states
            # ==========================================
            self._update_button_states()

            # ==========================================
            # Diagnostic Panel update
            # ==========================================
            pending_info = ""
            if eq_id in self._start_pending_times:
                elapsed = time.time() - self._start_pending_times[eq_id]
                pending_info = f" | pending: {elapsed:.1f}s"

            debug_text = (
                f"coil_cmd={coil_cmd} | motor_on={motor_on} | "
                f"trip_word={trip_word} | is_latched={self.is_latched} | "
                f"estop_active={self.estop_active}{pending_info}"
            )
            self.debug_label.config(text=debug_text)

        except Exception as e:
            # [AUTO-HEAL] If the socket dies mid-read, catch it, mark as disconnected,
            # and let the main loop automatically trigger a reconnect on the next tick.
            print(f"[HMI] Read error: {e}. Triggering auto-reconnect...")
            self.connected = False
            self.conn_label.config(text="🔴 LINK LOST", fg="red")
            try:
                self.client.close()
            except Exception:
                pass

    # ==========================================
    # UI Update Helpers
    # ==========================================

    def _update_system_status_label(self):
        """Update the small SYS label in the top-right corner."""
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
        """Show or hide the global E-STOP banner."""
        if self.estop_active:
            if not self.estop_banner.winfo_manager():
                # Pack it right after the title bar
                self.estop_banner.pack(
                    fill=tk.X,
                    padx=15,
                    pady=(5, 0),
                    before=self.conn_label.master,
                )
        else:
            if self.estop_banner.winfo_manager():
                self.estop_banner.pack_forget()

    def _update_button_states(self):
        """
        Enable/disable buttons based on system state.

        - During E-STOP: START disabled, STOP disabled,
          E-STOP disabled, RESET enabled.
        - During LOCKED: START disabled, others enabled.
        - Normal: all enabled.
        """
        if self.estop_active:
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.DISABLED)
            self.estop_btn.config(state=tk.DISABLED)
            self.reset_btn.config(state=tk.NORMAL)
            self.load_slider.config(state=tk.DISABLED)
        elif self.is_latched:
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.estop_btn.config(state=tk.NORMAL)
            self.reset_btn.config(state=tk.NORMAL)
            self.load_slider.config(state=tk.NORMAL)
        else:
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.NORMAL)
            self.estop_btn.config(state=tk.NORMAL)
            self.reset_btn.config(state=tk.NORMAL)
            self.load_slider.config(state=tk.NORMAL)

    # ==========================================
    # Motor / System Commands
    # ==========================================

    def start_motor(self):
        if not self.connected:
            return

        # Block locally if E-STOP or lockout is active
        if self.estop_active:
            print("[HMI] ⛔ START blocked — E-STOP is active")
            return
        if self.is_latched:
            print("[HMI] ⛔ START blocked — protection lockout active")
            return

        try:
            coil_idx = self.get_current_coil_index()
            self.client.write_coil(
                address=coil_idx,
                value=True,
                slave=1,
            )
            eq_id = EQUIPMENT_LIST[self.eq_combo.current()][0]

            # NEW: Start tracking pending time immediately
            self._start_pending_times[eq_id] = time.time()

            print(f"[HMI] ▶️  START sent for {eq_id} (timeout tracking started)")
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
            eq_id = EQUIPMENT_LIST[self.eq_combo.current()][0]

            # Clear pending timer on STOP
            self._start_pending_times.pop(eq_id, None)

            print(f"[HMI] ⏹️  STOP sent for {eq_id}")
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
            # Clear all pending timers on E-STOP
            self._start_pending_times.clear()
            print("[HMI] 🚨 E-STOP sent")
        except Exception as e:
            print(f"[HMI] E-STOP error: {e}")

    def reset_protection(self):
        if not self.connected:
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

        # Block load changes during E-STOP
        if self.estop_active:
            return

        try:
            offset = self.get_current_register_offset()
            self.client.write_register(
                address=offset + 12,
                value=int(float(value)),
                slave=1,
            )
        except Exception as e:
            print(f"[HMI] Load error: {e}")

    # ==========================================
    # Polling and Lifecycle
    # ==========================================

    def update_display(self):
        if self.running:
            # [FIX B] AUTO-HEAL: If disconnected, continuously try to reconnect in the background
            if not self.connected:
                self.connect_plc()
            
            # Only read data if we are successfully connected
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


# ==========================================
# Entry Point
# ==========================================

def main():
    root = tk.Tk()
    app = DeltaHMISimulator(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()