import os
import shutil

def clean_server():
    base_dir = '/home/ubuntu/alcosoft'
    
    # Directories to delete
    dirs_to_delete = [
        'tests', 'research', 'scratch', 'war_room', 'docs', 'reflection', 'GOAL'
    ]
    
    # Specific files to delete
    files_to_delete = [
        'COMPLETE_WORKFLOW_VALIDATION.py', 'PHASE_5_INTEGRATION_VERIFY.py', 'apply_fixes.py',
        'capture_ticks.py', 'check_conflicts.py', 'check_env.py', 'cleanup_test_data.py',
        'create_deploy.py', 'create_deploy_clean.py', 'deployment_dashboard.py', 'dump_orders.py',
        'emergency_cleanup.py', 'enhance_robustness.py', 'final_production_check.py',
        'fresh_system_reset.py', 'reset_system.py', 'quick_reset.py', 'list_tables.py',
        'manage_users.py', 'package_for_cloud.py', 'populate_realistic_data.py', 'pre_market_verification.py',
        'query.py', 'trigger_eso.py', 'verify_dependencies.py', 'yfinance_trace_reporter.py',
        'AUDIT_FINDINGS.json', 'dumped_orders.json', 'pip_list.json',
        'compatible_dryrun_report.json', 'incompatible_current_requirements_report.json',
        'neo_dryrun_report.json', 'neo_dryrun_report_alco_env.json'
    ]
    
    print("Starting cleanup...")
    os.chdir(base_dir)
    
    # Delete directories
    for d in dirs_to_delete:
        if os.path.isdir(d):
            print(f"Deleting directory: {d}")
            shutil.rmtree(d)
            
    # Delete pattern matched files at root
    for f in os.listdir('.'):
        if not os.path.isfile(f):
            continue
            
        delete = False
        if f.endswith('.md'): delete = True
        if f.endswith('.txt') and f != 'requirements.txt': delete = True
        if f.endswith('.tar.gz') or f.endswith('.zip') or f.endswith('.bat'): delete = True
        if f.startswith('audit_') and f.endswith('.py'): delete = True
        if f.startswith('fix_') and f.endswith('.py'): delete = True
        if f.startswith('test_') and f.endswith('.py'): delete = True
        if f in files_to_delete: delete = True
        
        if delete:
            print(f"Deleting file: {f}")
            os.remove(f)

    print("Cleanup complete.")

if __name__ == '__main__':
    clean_server()
