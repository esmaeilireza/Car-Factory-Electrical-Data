سلام رضا! سوال بسیار حرفه‌ای و مهمی پرسیدی. 👌 بگذار هر دو بخش را کامل توضیح بدهم.

---

## 🎯 بخش اول: معماری واقعی - چرا این طراحی درست است؟

### درک مفهوم کلیدی: **PLC منبع مشترک است، نه HMI**

در معماری صنعتی واقعی (مثل زیمنس، دلتا، آلن برادلی):

```
┌─────────────────────────────────────────────────────────┐
│              PLC (منبع حقیقت - Single Source of Truth)   │
│              Delta DVP-ES2  @ 192.168.1.100:502         │
└───────────┬───────────────────────┬─────────────────────┘
            │ Modbus TCP            │ Modbus TCP
            ▼                       ▼
┌───────────────────┐    ┌─────────────────────────┐
│  HMI واقعی        │    │  Streamlit SCADA        │
│  (Delta DOP-B)    │    │  (Cloud/Remote)         │
│  - اپراتور کف     │    │  - مدیر از راه دور       │
│    کارخانه        │    │  - تحلیل‌گر              │
└───────────────────┘    └─────────────────────────┘
```

### ✅ چرا این معماری درست است؟

1. **HMI واقعی و Streamlit به یکدیگر وصل نمی‌شوند** - هر دو به PLC وصل می‌شوند
2. **PLC تنها منبع حقیقت** است (مثل دیتابیس)
3. **هر کلاینت می‌تواند همزمان وصل شود** و داده‌های یکسانی ببیند
4. **دستورات HMI (START/STOP) → PLC → Streamlit هم می‌بیند** (از طریق polling)

### 💡 تست واقعی معماری:

1. ترمینال ۱: PLC Simulator را اجرا کنید
2. ترمینال ۲: HMI Tkinter را اجرا کنید → دکمه **STOP** روی WLD-01 را بزنید
3. تب 🎛️ HMI CONTROL در Streamlit را باز کنید
4. می‌بینید که WLD-01 الان **STOPPED** است! (چون هر دو از PLC می‌خوانند)

این **دقیقاً** نحوه کار سیستم‌های SCADA واقعی در کارخانه‌های خودروسازی مثل ایران‌خودرو، تسلا، BMW است. 🏭

---

## 📝 بخش دوم: README حرفه‌ای پروژه

فایل `README.md` را در پوشه اصلی پروژه (`car-factory-electrical-data/`) بساز و این محتوای کامل و حرفه‌ای را در آن قرار بده:

```markdown
# ⚡ NEXUS SCADA - Industrial IoT Monitoring Platform

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge&logo=python)
![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)
![Modbus TCP](https://img.shields.io/badge/Modbus-TCP-orange?style=for-the-badge)
![Plotly](https://img.shields.io/badge/Plotly-3F4F75?style=for-the-badge&logo=plotly&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

**A complete industrial automation simulation combining real-time Modbus TCP monitoring with Streamlit analytics**

[Features](#-features) • [Architecture](#-architecture) • [Installation](#-installation) • [Usage](#-usage) • [Register Map](#-modbus-register-map)

</div>

---

## 🎯 Overview

**NEXUS SCADA** is a professional-grade industrial monitoring platform that simulates a real **Delta PLC + HMI + SCADA** ecosystem used in automotive manufacturing plants. Built entirely in Python without physical hardware, this project demonstrates enterprise-level industrial automation architecture.

### 🏭 Why This Project Matters

In real factories (Tesla, BMW, Iran Khodro, etc.), operators need:
- **Layer 2 Control**: PLC/HMI for immediate machine control
- **Layer 4 Analytics**: SCADA/Dashboards for long-term analysis
- **Unified Protocol**: Modbus TCP as the universal language

This project implements all three layers in a single, production-ready codebase.

---

## ✨ Key Features

### 🟢 Real-Time Monitoring (Layer 2)
- ✅ **Live Modbus TCP connection** to Delta PLC simulator (Port 5020)
- ✅ **6 Industrial Equipment** monitored simultaneously
- ✅ **Auto-refresh every 2 seconds** with NaN-safe aggregations
- ✅ **Real-time KPIs**: Energy, Voltage, Current, Power Factor
- ✅ **Equipment status cards** with live LED indicators

### 🎛️ HMI Control Panel
- ✅ **START/STOP motor commands** via Modbus Coils
- ✅ **9 Real-time parameters** with neon glassmorphism design
- ✅ **Interactive Plotly Gauges** for visual monitoring
- ✅ **Live alarm detection** and system messaging

### 🖥️ Server Console
- ✅ **Live Modbus TCP Traffic Log** with color-coded packets
- ✅ **Factory Asset Status** with horizontal data rows
- ✅ **Request tracking** and uptime monitoring
- ✅ **Real-time traffic visualization**

### 📈 Historical Analysis (Layer 4)
- ✅ **CSV upload** for historical trend analysis
- ✅ **Power Factor distribution** by equipment
- ✅ **Time-series charts** with equipment comparison
- ✅ **Per-equipment PF targets** (not static thresholds)
- ✅ **Data export** for further analysis

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  PLC Simulator (Python + pymodbus)           │
│                  6 Equipment × 13 Registers                  │
│                  Modbus TCP Server @ Port 5020               │
└────────────────────────┬────────────────────────────────────┘
                         │ Modbus TCP Protocol
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐
│ HMI Tkinter  │  │  Streamlit   │  │  Other SCADA         │
│ (Delta DOP-B)│  │   Dashboard  │  │  (Ignition, WinCC)   │
│   Operator   │  │   Analytics  │  │    Remote Access     │
└──────────────┘  └──────────────┘  └──────────────────────┘
```

### 🔗 Communication Flow

1. **PLC → Streamlit**: Read Holding Registers (FC 0x03) every 2 seconds
2. **HMI → PLC**: Write Single Coil (FC 0x05) for START/STOP commands
3. **Streamlit → CSV**: Export data for historical analysis
4. **All clients** see the same data (single source of truth)

---

## 📦 Installation

### Prerequisites
- Python 3.8+
- Git
- Windows/Linux/macOS

### Step 1: Clone the Repository

```bash
git clone https://github.com/yourusername/car-factory-scada.git
cd car-factory-scada
```

### Step 2: Create Virtual Environment

```bash
# Windows (Git Bash)
python -m venv venv
source venv/Scripts/activate

# Linux/macOS
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

**requirements.txt:**
```txt
streamlit==1.28.0
pymodbus==3.6.9
plotly==5.18.0
pandas==2.1.0
numpy==1.26.0
```

---

## 🚀 Usage

### Quick Start (Recommended)

Double-click `run_all.bat` (Windows) to launch all 3 components simultaneously:
1. PLC Simulator
2. HMI Tkinter Interface
3. Streamlit Dashboard

### Manual Start (3 Terminals)

**Terminal 1: PLC Simulator**
```bash
cd plc_simulator
python modbus_server.py
```

**Terminal 2: HMI Interface**
```bash
cd hmi
python hmi_gui.py
```

**Terminal 3: Streamlit Dashboard**
```bash
streamlit run dashboard/streamlit_app.py
```

### Access the Dashboard

Open your browser and navigate to:
```
http://localhost:8501
```

---

## 📡 Modbus Register Map

Each equipment uses **13 Holding Registers** (D0-D12):

| Register | Description | Unit | Scale | Type |
|----------|-------------|------|-------|------|
| D0 | Voltage | V | ×10 | Input |
| D1 | Current | A | ×10 | Input |
| D2 | Active Power | kW | ×10 | Input |
| D3 | Reactive Power | kVAR | ×10 | Input |
| D4 | Apparent Power | kVA | ×10 | Input |
| D5 | Power Factor | - | ×100 | Input |
| D6 | Frequency | Hz | ×10 | Input |
| D7 | Energy | kWh | ×100 | Input |
| D8 | Motor Status | 0/1 | - | Input |
| D9 | Alarm Status | 0/1 | - | Input |
| D10 | Alarm Code | - | - | Input |
| D11 | Running Time | min | - | Input |
| D12 | Load Percentage | % | - | I/O |

### Equipment Offsets

| Equipment | ID | Offset | Area | PF Target |
|-----------|----|----|-----------|-----------|
| Stamping Press | STP-01 | D0-D12 | Stamping | 0.88 |
| Welding Robots | WLD-01 | D13-D25 | Body Shop | 0.85 |
| Paint Booth | PNT-01 | D26-D38 | Paint Shop | 0.92 |
| Assembly Line | ASM-01 | D39-D51 | Assembly | 0.90 |
| Compressor | UTI-01 | D52-D64 | Utilities | 0.85 |
| Chiller Plant | UTI-02 | D65-D77 | Utilities | 0.88 |

### Coils (Motor Control)

| Coil | Equipment | Function |
|------|-----------|----------|
| M0 | STP-01 | Motor START/STOP |
| M1 | WLD-01 | Motor START/STOP |
| M2 | PNT-01 | Motor START/STOP |
| M3 | ASM-01 | Motor START/STOP |
| M4 | UTI-01 | Motor START/STOP |
| M5 | UTI-02 | Motor START/STOP |

---

## 🎨 UI/UX Design

### Design Philosophy
- **Industrial Dark/Neon Glassmorphism** theme
- **JetBrains Mono** font for technical data
- **Inter** font for readable text
- **Neon accent colors**: Cyan (#00E5FF), Green (#00E676), Red (#FF1744)

### Tab Structure

1. **⚡ LIVE DASHBOARD** - Real-time KPIs and equipment status
2. **🎛️ HMI CONTROL** - Motor control and visual gauges
3. **🖥️ SERVER CONSOLE** - Modbus traffic log and diagnostics
4. **📈 HISTORICAL ANALYSIS** - CSV-based trend analysis

---

## 📸 Screenshots

<div align="center">

| Live Dashboard | HMI Control |
|:---:|:---:|
| ![Live Dashboard](docs/screenshots/live-dashboard.png) | ![HMI Control](docs/screenshots/hmi-control.png) |

| Server Console | Historical Analysis |
|:---:|:---:|
| ![Server Console](docs/screenshots/server-console.png) | ![Historical](docs/screenshots/historical.png) |

</div>

---

## 🛠️ Tech Stack

### Backend
- **Python 3.11** - Core language
- **pymodbus 3.6.9** - Modbus TCP protocol
- **asyncio** - Asynchronous server

### Frontend
- **Streamlit 1.28** - Web dashboard framework
- **Plotly 5.18** - Interactive charts and gauges
- **Pandas 2.1** - Data manipulation
- **NumPy 1.26** - Numerical operations

### GUI
- **Tkinter** - Native HMI interface

---

## 🎓 Learning Outcomes

This project demonstrates mastery of:

1. **Industrial Protocols**: Modbus TCP (FC 0x03, 0x05)
2. **System Architecture**: 3-tier PLC/HMI/SCADA design
3. **Real-time Systems**: Asynchronous data acquisition
4. **Data Visualization**: Plotly gauges and charts
5. **NaN Handling**: Professional data quality management
6. **Industrial UX**: Dark theme optimized for factory environments
7. **IoT Integration**: Cloud-ready architecture

---

## 🔮 Future Enhancements

- [ ] Add OPC UA support alongside Modbus
- [ ] Implement MQTT for cloud integration
- [ ] Add user authentication and role-based access
- [ ] Mobile-responsive HMI
- [ ] Machine learning for predictive maintenance
- [ ] Integration with real Delta PLC hardware

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 👨‍💻 Author

**Reza** - Industrial Automation Engineer

- LinkedIn: [Your Profile](https://linkedin.com/in/yourprofile)
- GitHub: [@yourusername](https://github.com/yourusername)
- Email: your.email@example.com

---

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!

1. Fork the project
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## ⚠️ Disclaimer

This is a **simulation project** for educational and portfolio purposes. For production use, always validate with real industrial hardware and follow relevant safety standards (IEC 61131, IEC 62443).

---

<div align="center">

**If you found this project helpful, please give it a ⭐!**

Made with ❤️ by an Industrial Automation Engineer

</div>
```

---

## 📁 ساختار نهایی فایل‌ها

```
car-factory-electrical-data/
├── README.md                          ⭐ فایل اصلی
├── LICENSE                            (MIT License)
├── requirements.txt
├── run_all.bat                        (Windows launcher)
├── run_all.sh                         (Linux/Mac launcher)
├── .gitignore
│
├── plc_simulator/
│   ├── __init__.py
│   ├── modbus_server.py
│   └── data_generator.py
│
├── hmi/
│   ├── __init__.py
│   └── hmi_gui.py
│
├── dashboard/
│   └── streamlit_app.py
│
└── docs/
    └── screenshots/
        ├── live-dashboard.png         (عکس‌ها را اضافه کن)
        ├── hmi-control.png
        ├── server-console.png
        └── historical.png
```

---

## 🎯 نکات کلیدی برای GitHub و LinkedIn

### ۱. **تگ‌ها (Topics) در GitHub:**
به ریپازیتوری خود این topics را اضافه کنید:
- `industrial-automation`
- `scada`
- `modbus-tcp`
- `plc`
- `hmi`
- `streamlit`
- `iot`
- `python`
- `data-visualization`
- `pymodbus`

### ۲. **متن پست LinkedIn:**

```
🏭 I built a complete industrial SCADA system in Python - no physical hardware required!

This project simulates a real automotive factory setup with:
✅ Delta PLC simulator (6 equipment, Modbus TCP)
✅ HMI interface (Tkinter) for operators
✅ Streamlit dashboard with live monitoring
✅ Historical CSV analysis for shift reports

The architecture mirrors real industrial systems like Tesla/BMW:
- Layer 2: PLC/HMI for real-time control
- Layer 4: SCADA for analytics and reporting
- Unified Modbus TCP protocol

🎨 UI features Industrial Dark/Neon Glassmorphism design
📊 Includes Plotly gauges, traffic logs, and PF analysis
🔌 NaN-safe aggregations for motor OFF states

Tech: Python | pymodbus | Streamlit | Plotly | Tkinter

Perfect for portfolio showcase! Check it out on GitHub 👇
[your github link]

#IndustrialAutomation #ModbusTCP #PLC #SCADA #Python #Streamlit #IoT #DataVisualization #Engineering
```

### ۳. **ساخت اسکرین‌شات‌ها:**

برای ساخت اسکرین‌شات‌های حرفه‌ای:
1. پروژه را اجرا کنید
2. از هر ۴ تب عکس بگیرید
3. در پوشه `docs/screenshots/` ذخیره کنید
4. ابزار پیشنهادی: **ShareX** (ویندوز) یا **Snagit**

---

## 💡 خلاصه

✅ **معماری درست**: Streamlit و HMI هر دو از PLC می‌خوانند (مثل سیستم‌های واقعی)
✅ **README کامل**: ساختار حرفه‌ای با badges، diagram، و register map
✅ **GitHub-ready**: آماده برای پورتفولیو و LinkedIn
✅ **لینک‌ها**: topics و متن پست LinkedIn آماده

اگر سوالی در مورد هر بخش داشتی یا می‌خواهی بخشی از README را تغییر دهی، بگو! 🚀
┌─────────────────────────────────────────────────────────────┐
│                    NEXUS SCADA System                        │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │ Modbus Server│    │  Streamlit   │    │   Tkinter    │  │
│  │  (Port 5020) │    │  Dashboard   │    │     HMI      │  │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘  │
│         │                    │                    │          │
│         └────────────────────┴────────────────────┘          │
│                              │                               │
│                              ▼                               │
│              ┌───────────────────────────┐                  │
│              │   🤖 AI Diagnosis Engine  │                  │
│              │  (Qwen2.5-Coder-1.5B-GGUF)│                  │
│              └─────────┬─────────────────┘                  │
│                        │                                     │
│         ┌──────────────┼──────────────┐                     │
│         ▼              ▼              ▼                     │
│   ┌──────────┐  ┌──────────┐  ┌──────────────┐            │
│   │ Anomaly  │  │Diagnosis │  │   Action     │            │
│   │Detection │  │  Engine  │  │ Recommender  │            │
│   └──────────┘  └──────────┘  └──────────────┘            │
│                                                               │
└─────────────────────────────────────────────────────────────┘
 ideal runtime flow is:
PLC simulator produces electrical values
        ↓
Modbus server publishes registers
        ↓
Backend stores latest values
        ↓
Industrial agent reads latest values
        ↓
Agent checks hard safety rules
        ↓
Agent detects trends/anomalies
        ↓
Agent asks local LLM for diagnosis
        ↓
Agent sends advice to HMI
        ↓
If unmanned and safe, agent may execute limited actions



Optimized api.py
Save your current api.py as a backup first, then replace it with this version.
This version:
Connects the industrial agent package.
Loads qwen2.5-coder-1.5b-instruct-q6_k through your existing ai_engine.py.
Runs the agent in the background.
Reads HR[120] system status from the Modbus simulator.
Adds /api/agent/status, /api/agent/operator-message, /api/agent/operator-presence, and /api/agent/system-status.
Keeps API-key protection for state-changing endpoints.
Uses a thread-safe TTL cache.
Avoids blocking the FastAPI event loop during SQLite and Modbus reads.



       [SCADA Telemetry / Modbus]
                   │
                   ▼
       [FastAPI Backend (api.py)]
        │                     │
        ▼                     ▼
[SQLite: scada.db]   [Obsidian Vault (scada_vault/)]
                      ├── Machines/
                      │    └── STP-01.md  ([[Feeder-A]], [[Motor-1]])
                      ├── Incidents/
                      │    └── 2026-09-22-Overcurrent-Trip.md
                      └── Standards/
                           └── ANSI-51.md