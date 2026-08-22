"""
Enhanced Test: Load AI model and run multiple diagnostic scenarios
Tests: Normal operation, Disconnected state, Error state, Multi-equipment
"""
import sys
import os
sys.path.insert(0, '.')

from models.ai_engine import AIDiagnosisEngine
import pandas as pd
from datetime import datetime, timedelta
import time
import json

# Try to import psutil for memory tracking
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("Note: Install 'psutil' for memory tracking (optional)")


def get_memory_usage():
    """Get current memory usage in MB"""
    if PSUTIL_AVAILABLE:
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)
    return 0.0


def print_header(title):
    """Print a formatted header"""
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_diagnosis(diagnosis, scenario_name):
    """Print diagnosis results in a formatted way"""
    print(f"\n--- Scenario: {scenario_name} ---")
    print(f"Diagnosis:    {diagnosis.get('diagnosis', 'N/A')}")
    print(f"Root Cause:   {diagnosis.get('root_cause', 'N/A')}")
    print(f"Severity:     {diagnosis.get('severity', 'N/A')}")
    print(f"Safety Level: {diagnosis.get('safety_level', 'N/A')}")
    print(f"Confidence:   {diagnosis.get('confidence', 0.0):.2f}")
    print(f"Action:       {diagnosis.get('recommended_action', 'N/A')}")
    print(f"IEC Ref:      {diagnosis.get('iec_reference', 'N/A')}")
    print(f"Equipment:    {diagnosis.get('affected_equipment', [])}")
    print(f"Human Req:    {diagnosis.get('human_approval_required', False)}")


def run_test():
    """Main test runner"""
    print_header("NEXUS AI - Enhanced Diagnostic Test Suite")
    
    # ============================================================
    # STAGE 1: Load AI Engine
    # ============================================================
    print("\n[1/4] Loading AI engine (30-60 seconds expected)...")
    mem_before = get_memory_usage()
    start_time = time.time()
    
    try:
        engine = AIDiagnosisEngine()
        load_time = time.time() - start_time
        mem_after = get_memory_usage()
        
        print(f"OK: Engine loaded in {load_time:.2f} seconds")
        print(f"OK: Backend: {engine.backend}")
        if PSUTIL_AVAILABLE:
            print(f"OK: Memory usage: {mem_after:.1f} MB (delta: +{mem_after - mem_before:.1f} MB)")
    except Exception as e:
        print(f"FAIL: Failed to load engine: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # ============================================================
    # STAGE 2: Build Test Scenarios
    # ============================================================
    print("\n[2/4] Building test scenarios...")
    
    # Scenario A: Normal operation (all equipment healthy)
    now = datetime.now()
    normal_data = pd.DataFrame({
        'timestamp': [now] * 6,
        'equipment_id': ['STP-01', 'WLD-01', 'PNT-01', 'ASM-01', 'UTI-01', 'UTI-02'],
        'voltage': [380.0, 382.5, 379.8, 381.2, 380.5, 383.1],
        'current': [245.0, 180.5, 320.2, 150.8, 210.3, 290.7],
        'heartbeat': [10, 10, 10, 10, 10, 10]
    })
    
    normal_session = {
        'connected': True,
        'estop_active': False,
        'any_trip_active': False
    }
    
    # Scenario B: Disconnected state (communication failure)
    disconnected_session = {
        'connected': False,
        'estop_active': False,
        'any_trip_active': False
    }
    
    modbus_errors = [
        {'time': '12:00:01', 'eq': 'STP-01', 'error': 'No Response received from remote slave'},
        {'time': '12:00:02', 'eq': 'WLD-01', 'error': 'Socket timeout after 5000ms'}
    ]
    
    # Scenario C: E-STOP active (safety state)
    estop_session = {
        'connected': True,
        'estop_active': True,
        'any_trip_active': True
    }
    
    # Scenario D: Stale data (heartbeat frozen)
    stale_data = pd.DataFrame({
        'timestamp': [now - timedelta(seconds=45)] * 2,  # 45 seconds old
        'equipment_id': ['STP-01', 'WLD-01'],
        'voltage': [380.0, 382.5],
        'current': [245.0, 180.5],
        'heartbeat': [5, 5]  # Frozen heartbeat
    })
    
    stale_session = {
        'connected': True,
        'estop_active': False,
        'any_trip_active': False
    }
    
    scenarios = [
        ("Normal Operation", normal_session, [], normal_data),
        ("Disconnected State", disconnected_session, modbus_errors, normal_data),
        ("E-STOP Active", estop_session, [], normal_data),
        ("Stale Data / Frozen Heartbeat", stale_session, [], stale_data),
    ]
    
    print(f"OK: Built {len(scenarios)} test scenarios")
    
    # ============================================================
    # STAGE 3: Run Diagnostic Tests
    # ============================================================
    print("\n[3/4] Running AI diagnosis on all scenarios...")
    print("(Each diagnosis may take 10-30 seconds)\n")
    
    results = []
    total_start = time.time()
    
    for i, (name, session, errors, data) in enumerate(scenarios, 1):
        print(f"Running scenario {i}/{len(scenarios)}: {name}...")
        start_time = time.time()
        
        try:
            diagnosis = engine.analyze_system_state(
                session_state=session,
                traffic_log=[],
                modbus_errors=errors,
                recent_data=data
            )
            infer_time = time.time() - start_time
            
            print(f"  OK: Completed in {infer_time:.2f}s")
            print_diagnosis(diagnosis, name)
            
            results.append({
                'scenario': name,
                'success': True,
                'time': infer_time,
                'diagnosis': diagnosis
            })
            
        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()
            
            results.append({
                'scenario': name,
                'success': False,
                'error': str(e)
            })
        
        print()  # Blank line between scenarios
    
    total_time = time.time() - total_start
    
    # ============================================================
    # STAGE 4: Summary Report
    # ============================================================
    print_header("TEST SUMMARY")
    
    success_count = sum(1 for r in results if r['success'])
    total_count = len(results)
    
    print(f"Total scenarios:    {total_count}")
    print(f"Successful:         {success_count}")
    print(f"Failed:             {total_count - success_count}")
    print(f"Success rate:       {success_count/total_count*100:.1f}%")
    print(f"Total test time:    {total_time:.2f} seconds")
    print(f"Average time:       {total_time/total_count:.2f} seconds per diagnosis")
    
    if PSUTIL_AVAILABLE:
        final_mem = get_memory_usage()
        print(f"Final memory:       {final_mem:.1f} MB")
    
    print()
    print("Detailed Results:")
    for i, result in enumerate(results, 1):
        status = "PASS" if result['success'] else "FAIL"
        print(f"  {i}. [{status}] {result['scenario']}", end="")
        if result['success']:
            diag = result['diagnosis']
            print(f" -> {diag.get('severity', 'N/A').upper()} severity", end="")
            print(f" (Safety Level {diag.get('safety_level', 'N/A')})")
        else:
            print(f" -> {result.get('error', 'Unknown error')}")
    
    # ============================================================
    # Final Verdict
    # ============================================================
    print_header("FINAL VERDICT")
    
    if success_count == total_count:
        print("FULL TEST PASSED! AI engine is working correctly.")
        print()
        print("Next steps:")
        print("  1. Run: streamlit run dashboard/streamlit_app.py")
        print("  2. Navigate to HMI CONTROL tab")
        print("  3. Click 'Run AI Diagnosis' button")
        print("  4. Test with real Modbus data")
        sys.exit(0)
    elif success_count >= total_count * 0.75:
        print("PARTIAL SUCCESS - Most scenarios passed.")
        print(f"Some scenarios failed ({total_count - success_count} of {total_count}).")
        print("Review the failed scenarios above for details.")
        sys.exit(0)
    else:
        print("TEST FAILED - Too many scenarios failed.")
        print("Review the error messages above.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        run_test()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)