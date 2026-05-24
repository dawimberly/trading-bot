import os

def check_and_clean(root_dir):
    # Files often considered "useless" or debris
    useless_extensions = {'.pyc', '.tmp', '.log', '.bak'}
    
    print(f"--- Scanning Directory: {root_dir} ---")
    
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for f in filenames:
            file_path = os.path.join(dirpath, f)
            
            # Check for duplicates or useless files
            ext = os.path.splitext(f)[1]
            if ext in useless_extensions:
                print(f"[DELETING] Useless file found: {f}")
                # os.remove(file_path) # Uncomment this line to actually delete
            else:
                print(f"[FILE] {f} (Location: {dirpath})")

    print("\n--- Scan Complete ---")

if __name__ == "__main__":
    check_and_clean(os.getcwd())