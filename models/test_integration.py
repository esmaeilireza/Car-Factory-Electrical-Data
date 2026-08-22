"""
Integration test for the model with the system
"""
import sys
sys.path.insert(0, '.')

from models.ai_engine import AIDiagnosisEngine
import pandas as pd
from datetime import datetime

def test_ai_engine():
    """Test the diagnosis engine"""
    print("=" * 50)
    print("🧪 Testing Diagnosis Engine")
    print("=" * 50)
    
    # Load model
    model_path = "models/qwen2.5-coder-1.5b-instruct-q6_k.gguf"
    try:
        engine = AIDiagnosisEngine(model_path)
        print("✅ Diagnosis engine created successfully")
    except Exception as e:
        print(f"❌ Error creating engine: {e}")
        return
    
    # Test 1: Diagnose connection issue
    print("\n🧪 Test 1: Diagnosing connection issue")
    
    mock_session_state = {
        'connected': False,
        'estop_active': False,
        'any_trip_active': False,
        'traffic_log': [],
        'modbus_errors': [
            {'time': '12:00:00', 'eq': 'STP-01', 'error': 'No Response received'}
        ]
    }
    
    mock_traffic_log = []
    mock_errors = [
        {'time': '12:00:00', 'eq': 'STP-01', 'error': 'No Response received'}
    ]
    
    mock_data = pd.DataFrame({
        'timestamp': [datetime.now()],
        'equipment_id': ['STP-01'],
        'voltage': [380.0],
        'current': [0.0],
        'heartbeat': [1]
    })
    
    try:
        diagnosis = engine.analyze_system_state(
            session_state=mock_session_state,
            traffic_log=mock_traffic_log,
            modbus_errors=mock_errors,
            recent_data=mock_data
        )
        
        print(f"✅ Diagnosis received:")
        print(f"   - Diagnosis: {diagnosis.get('diagnosis')}")
        print(f"   - Severity: {diagnosis.get('severity')}")
        print(f"   - Safety Level: {diagnosis.get('safety_level')}")
        print(f"   - Confidence: {diagnosis.get('confidence')}")
        
    except Exception as e:
        print(f"❌ Error in diagnosis: {e}")
    
    # Test 2: Diagnose thermal issue
    print("\n🧪 Test 2: Diagnosing thermal issue")
    
    mock_data_thermal = pd.DataFrame({
        'timestamp': [datetime.now()],
        'equipment_id': ['STP-01'],
        'voltage': [380.0],
        'current': [600.0],
        'heartbeat': [50]
    })
    
    try:
        diagnosis = engine.analyze_system_state(
            session_state={'connected': True},
            traffic_log=[],
            modbus_errors=[],
            recent_data=mock_data_thermal
        )
        
        print(f"✅ Diagnosis received:")
        print(f"   - Diagnosis: {diagnosis.get('diagnosis')}")
        print(f"   - Severity: {diagnosis.get('severity')}")
        
    except Exception as e:
        print(f"❌ Error in diagnosis: {e}")


if __name__ == "__main__":
    test_ai_engine()