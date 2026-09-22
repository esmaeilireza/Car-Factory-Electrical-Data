
"""Surgical patch for hmi_gui.py - repairs corruption + adds real-panel features."""
import shutil, sys, datetime

FILE = "hmi/hmi_gui.py"
src = open(FILE, encoding="utf-8").read()

edits = []

# R1: blink state init
edits.append(("R1 blink init",
"""        self.connected = False
        self.running = True
""",
"""        self.connected = False
        self.running = True
        self._blink = False
"""))

# R2: combobox pack + binding + equipment lamp strip
edits.append(("R2 lamp strip",
"""        self.eq_combo.current(0)


    def _create_main_display(self):""",
"""        self.eq_combo.current(0)
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

    def _create_main_display(self):"""))

# R3: load label styled as LCD setpoint + pack + keypad binding
edits.append(("R3 load setpoint display",
"""        self.load_label = tk.Label(
            status_row,
            text="0%",
            font=("Arial", 11, "bold"),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_white"],
        )
""",
"""        self.load_label = tk.Label(
            status_row,
            text="0%",
            font=("Arial", 12, "bold"),
            bg="black",
            fg="#00ff00",
            bd=2, relief=tk.SUNKEN, padx=12, pady=4,
        )
        self.load_label.pack(side=tk.LEFT, padx=15)
        self.load_label.bind("<Double-Button-1>", lambda e: self.open_keypad())
"""))

# R4: fix corrupted estop_all + restore reset_protection with confirmation
edits.append(("R4 estop/reset repair",
"""        except Exception as e:
            print(f"[HMI] E-STOP error: {e}")


            self.client.write_coil(
                address=COIL_RESET,
                value=True,
                slave=1,
            )

            print("[HMI] \U0001f513 RESET sent")

        except Exception as e:
            print(f"[HMI] RESET error: {e}")
""",
"""        except Exception as e:
            print(f"[HMI] E-STOP error: {e}")

    def reset_protection(self):
        if not self.connected:
            return

        if not messagebox.askyesno(
            "Confirm RESET",
            "Clear protection lockouts and the E-STOP latch?\\n"
            "All equipment will be allowed to restart.",
        ):
            return

        try:
            self.client.write_coil(
                address=COIL_RESET,
                value=True,
                slave=1,
            )

            print("[HMI] \U0001f513 RESET sent")

        except Exception as e:
            print(f"[HMI] RESET error: {e}")
"""))

# R5: restore _update_estop_banner (with blinking)
edits.append(("R5 estop banner restore",
"""        self.sys_status_label.config(text=text, fg=color)
""",
"""        self.sys_status_label.config(text=text, fg=color)

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
"""))

# R6: restore update_display def + blink tick
edits.append(("R6 update_display restore",
"""    # =========================================================================
    # Lifecycle
    # =========================================================================


            if not self.connected:
                self.connect_plc()

            if self.connected:
                self.read_plc_data()

            self.root.after(POLL_INTERVAL_MS, self.update_display)
""",
"""    # =========================================================================
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
"""))

# R7: new methods - lamp navigation + DOP-B numeric keypad (insert before connect_plc)
edits.append(("R7 navigation + keypad",
"""    def connect_plc(self):""",
"""    def select_equipment(self, idx: int):
        \"\"\"Lamp-strip navigation: select equipment, highlight the active lamp.\"\"\"
        self.eq_combo.current(idx)
        for i, btn in self.lamp_buttons.items():
            if i == idx:
                btn.config(relief=tk.SUNKEN, bg=COLORS["cyan"], fg="black")
            else:
                btn.config(relief=tk.RAISED, bg=COLORS["bg_header"], fg=COLORS["text_white"])

    def open_keypad(self):
        \"\"\"DOP-B style numeric keypad for the Load setpoint.\"\"\"
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

    def connect_plc(self):"""))

backup = FILE + ".pre-patch-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(FILE, backup)

applied = 0
failed = []
for name, old, new in edits:
    count = src.count(old)
    if count != 1:
        failed.append((name, count))
        print(f"[WARN] {name}: matched {count} times (expected 1) - SKIPPED")
        continue
    src = src.replace(old, new, 1)
    applied += 1
    print(f"[OK]   {name}")

if applied == 0:
    print(f"\n[ABORT] Only {applied}/{len(edits)} matched. File NOT modified.")
    for name, count in failed:
        print(f"   -> {name}: count={count}")
    sys.exit(1)

open(FILE, "w", encoding="utf-8", newline="\n").write(src)
print(f"\n[DONE] {applied}/{len(edits)} edits applied. Backup: {backup}")
