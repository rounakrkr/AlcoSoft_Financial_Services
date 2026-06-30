import os
import zipfile

def create_deploy_zip(output_filename):
    # Whitelisted top-level directories to include completely (except __pycache__ etc)
    allowed_dirs = {'core', 'screener', 'dashboard', 'config', 'scripts'}
    
    # Whitelisted top-level files
    allowed_root_files = {
        'main.py', 
        'requirements.txt', 
        'telegram_daemon.py',
        'alcosoft-engine.service',
        'alcosoft-dashboard.service',
        'alcosoft-telegram.service',
        'nginx-alcosoft'
    }
    
    # Things to globally exclude even within allowed directories
    global_excludes_dirs = {'__pycache__', 'tests', '.git', '.vscode', '.vs_code'}
    
    with zipfile.ZipFile(output_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk('.'):
            # Normalization
            rel_root = os.path.relpath(root, '.')
            
            # Filter directories
            if rel_root == '.':
                dirs[:] = [d for d in dirs if d in allowed_dirs]
            else:
                dirs[:] = [d for d in dirs if d not in global_excludes_dirs and not d.startswith('.')]
                
            for file in files:
                # Top level file filtering
                if rel_root == '.':
                    if file not in allowed_root_files:
                        continue
                        
                # Global file filtering
                if file.endswith('.pyc') or file.endswith('.md') or file.startswith('test_'):
                    continue
                if file.startswith('.'):
                    continue
                    
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, '.')
                zipf.write(file_path, arcname=arcname)
                print(f"Added: {arcname}")

if __name__ == '__main__':
    zip_name = 'cloud_deploy.zip'
    print("Building STRICT whitelist deployment package...")
    create_deploy_zip(zip_name)
    print(f"\nSuccessfully created {zip_name}. Ready for SCP transfer.")
