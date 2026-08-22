"""
Final full integration test
"""
import sys
sys.path.insert(0, '.')

from models.ai_engine import AIDiagnosisEngine
import pandas as pd
from datetime import datetime

def run_full_test():
    """Full test"""
    print("=" * 60)
    print("🧪 Final Full Integration Test")
    print("=" * 60)
    
    # Load model
    try:
        engine = AIDiagnosisEngine("models/qwen2.5-coder-1.5b-instruct-q6_k.gguf")
        print("✅ Diagnosis engine created")
    except Exception as e:
        print(f"❌ Error: {e}")
        return
    
    # Test scenarios
    test_scenarios = [
        {
            "name": "Disconnected",
            "session": {'connected': False},
            "errors": [{'time': '12:00', 'eq': 'STP-01', 'error': 'No Response'}]
        },
        {
            "name": "Overload",
            "session": {'connected': True},
            "data": {'current': 600, 'voltage': 380}
        },
        {
            "name": "E-STOP Active",
            "session": {'connected': True, 'estop_active': True},
            "errors": []
        }
    ]
    
    for scenario in test_scenarios:
        print(f"\n🧪 Scenario: {scenario['name']}")
        
        try:
            # Build mock data
            mock_data = pd.DataFrame({
                'timestamp': [datetime.now()],
                'equipment_id': ['STP-01'],
                'voltage': [scenario.get('data', {}).get('voltage', 380)],
                'current': [scenario.get('data', {}).get('current', 0)],
                'heartbeat': [1]
            })
            
            diagnosis = engine.analyze_system_state(
                session_state=scenario['session'],
                traffic_log=[],
                modbus_errors=scenario.get('errors', []),
                recent_data=mock_data
            )
            
            print(f"✅ Diagnosis: {diagnosis.get('diagnosis')}")
            print(f"   Severity: {diagnosis.get('severity')}")
            print(f"   Safety Level: {diagnosis.get('safety_level')}")
            
        except Exception as e:
            print(f"❌ Error: {e}")
    
    print("\n✅ Tests completed!")


if __name__ == "__main__":
    run_full_test()