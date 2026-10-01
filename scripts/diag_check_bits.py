import time
from pymodbus.client import ModbusTcpClient

HOST, PORT = "127.0.0.1", 5020
ARM_REG, ARM_VAL = 199, 0xA5A5
FAULT_BASE = 150
TARGET_IDX = 4 # UTI-01
FAULT_ADDR = FAULT_BASE + TARGET_IDX
TRIP_OFFSET = 10
ALARM_OFFSET = 9
BASE_MULTIPLIER = 18

EQ_ID = "UTI-01"
TRIP_ADDR = (TARGET_IDX * BASE_MULTIPLIER) + TRIP_OFFSET
ALARM_ADDR = (TARGET_IDX * BASE_MULTIPLIER) + ALARM_OFFSET

print(f"Checking {EQ_ID} (Idx {TARGET_IDX})")
print(f"Fault Reg: HR[{FAULT_ADDR}]")
print(f"Trip Reg:  HR[{TRIP_ADDR}]")
print(f"Alarm Reg: HR[{ALARM_ADDR}]")

try:
    c = ModbusTcpClient(HOST, port=PORT)
    if not c.connect():
        print("[FAIL] Cannot connect to PLC.")
        exit(1)

    # Arm
    c.write_register(ARM_REG, ARM_VAL, slave=1)
    time.sleep(0.2)

    # Inject Fault Code 1 (Thermal)
    print("Injecting Fault Code 1 (Thermal)...")
    c.write_register(FAULT_ADDR, 1, slave=1)
    time.sleep(1.5) # Wait for propagation

    # Read Words
    rr_trip = c.read_holding_registers(TRIP_ADDR, 1, slave=1)
    rr_alarm = c.read_holding_registers(ALARM_ADDR, 1, slave=1)

    if rr_trip.isError() or rr_alarm.isError():
        print("[FAIL] Read error on status registers.")
    else:
        trip_val = rr_trip.registers[0]
        alarm_val = rr_alarm.registers[0]
        
        print(f"Trip Word:  {bin(trip_val)} ({trip_val})")
        print(f"Alarm Word: {bin(alarm_val)} ({alarm_val})")
        
        # ANSI 49 is bit 49? No, ANSI numbers are device numbers. 
        # Usually mapped to specific bits in the word.
        # If using standard IEEE C37.90 mapping:
        # Bit 0 = ANSI 24, Bit 1 = ANSI 25... 
        # OR custom mapping. 
        # We just check if NON-ZERO.
        
        if trip_val == 0 and alarm_val == 0:
            print("\n[WARNING] Both words are ZERO.")
            print("The PLC simulator is NOT setting any ANSI flags for this fault.")
            print("ACTION REQUIRED: Update plc_simulator/data_generator.py or modbus_server.py")
            print("to set the appropriate bit in trip_word/alarm_word when inject_fault('thermal') is called.")
        else:
            print("\n[PASS] PLC Simulator IS setting ANSI flags.")
            print("Proceed to restart backend and run probe.")

    c.close()

except Exception as e:
    print(f"[ERROR] {e}")
