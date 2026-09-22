"""
Optional Modbus command adapter.

This adapter is intentionally conservative.
It only performs actions explicitly allowed by the action policy.
By default, writes are disabled.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

try:
    from pymodbus.client import ModbusTcpClient
except Exception:
    ModbusTcpClient = None


EQ_ORDER = ["STP-01", "WLD-01", "PNT-01", "ASM-01", "UTI-01", "UTI-02"]
REG_PER_EQ = 18
LOAD_OFFSET = 12


class ModbusSafetyAdapter:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5020,
        slave: int = 1,
        enable_writes: Optional[bool] = None,
    ):
        self.host = host
        self.port = port
        self.slave = slave

        if enable_writes is None:
            enable_writes = os.getenv("NEXUS_ENABLE_AGENT_WRITES", "0") == "1"
        self.enable_writes = bool(enable_writes)

        self.client = None
        if ModbusTcpClient is not None:
            self.client = ModbusTcpClient(host=host, port=port, timeout=3)

    def connect(self) -> bool:
        if self.client is None:
            return False
        try:
            return bool(self.client.connect())
        except Exception:
            return False

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass

    def execute(self, action: str, context: Dict[str, Any]) -> bool:
        if not self.enable_writes:
            print(f"[MODBUS ADAPTER] Writes disabled. Would execute: {action} {context}")
            return False

        if self.client is None:
            return False

        if not self.client.is_socket_open():
            if not self.connect():
                return False

        eq_id = context.get("eq_id")
        if eq_id not in EQ_ORDER:
            return False

        idx = EQ_ORDER.index(eq_id)

        try:
            if action == "stop_equipment":
                result = self.client.write_coil(address=idx, value=False, slave=self.slave)
                return not result.isError()

            if action == "reconnect_modbus":
                self.close()
                return self.connect()

            # reduce_load is not automatically implemented because safe target load
            # depends on process context. Add a controlled implementation here only
            # after factory engineering approval.
            return False

        except Exception as e:
            print(f"[MODBUS ADAPTER] Execution error: {e}")
            return False
